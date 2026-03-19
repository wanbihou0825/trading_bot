#!/usr/bin/env python3
"""
Polymarket 自适应跟单机器人
==========================
基于策略信号自动执行交易，支持风险管理和熔断机制。

功能:
- 自适应策略选择
- Endgame Sweeper 高概率收割
- 钱包跟单交易
- WebSocket实时监控
- 风险管理和熔断保护
- Telegram 通知

使用:
    python main.py              # 实盘模式
    python main.py --dry-run    # 模拟模式
"""

import argparse
import asyncio
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from config import get_settings, Settings
from core.exceptions import GracefulShutdown, InitializationError
from core.circuit_breaker import CircuitBreaker
from core.risk_manager import RiskManager
from core.wallet_quality_scorer import WalletQualityScorer
from core.market_maker_detector import MarketMakerDetector
from core.red_flag_detector import RedFlagDetector
from core.wallet_monitor import WalletMonitor, MonitorMode
from core.websocket_manager import PolymarketWebSocket
from core.copy_executor import CopyExecutor, CopyConfig, CopyMode
from core.wallet_scanner import WalletScanner
from strategies.adaptive import AdaptiveStrategyManager
from strategies.base import MarketData, SignalType
from services.polymarket_client import PolymarketClient
from services.telegram_service import TelegramService
from utils.logger import setup_logging, get_logger, mask_wallet_address

logger = get_logger(__name__)


class TradingBot:
    """
    交易机器人主类
    
    协调各个组件完成交易流程，支持策略交易和跟单交易。
    """
    
    def __init__(self, settings: Settings):
        """
        初始化机器人
        
        Args:
            settings: 配置实例
        """
        self.settings = settings
        self._running = False
        self._shutdown_requested = False
        self._main_tasks = []
        
        # 初始化组件
        self._init_components()
        
        logger.info(
            f"交易机器人初始化 | "
            f"模式: {'模拟' if settings.dry_run else '实盘'} | "
            f"钱包: {mask_wallet_address(settings.wallet_address)} | "
            f"跟单: {'启用' if settings.copy_trading.enabled else '禁用'}"
        )
    
    def _init_components(self) -> None:
        """初始化所有组件"""
        # 1. 熔断器
        self.circuit_breaker = CircuitBreaker(
            max_daily_loss=self.settings.risk.max_daily_loss,
            max_consecutive_losses=5,
            max_single_loss=self.settings.risk.max_position_size
        )
        
        # 2. 风险管理器
        self.risk_manager = RiskManager(
            circuit_breaker=self.circuit_breaker,
            max_position_size=self.settings.risk.max_position_size,
            max_position_pct=self.settings.risk.max_position_pct,
            max_concurrent_positions=self.settings.risk.max_concurrent_positions,
            default_stop_loss_pct=self.settings.risk.default_stop_loss_pct,
            default_take_profit_pct=self.settings.risk.default_take_profit_pct,
            max_total_exposure=self.settings.risk.max_total_exposure
        )
        
        # 3. 策略管理器
        self.strategy_manager = AdaptiveStrategyManager(
            min_confidence=self.settings.strategy.min_confidence_score
        )
        
        # 4. Polymarket 客户端
        self.client = PolymarketClient(
            api_url=self.settings.polymarket_api_url,
            dry_run=self.settings.dry_run
        )
        
        # 5. Telegram 服务
        self.telegram = TelegramService(
            bot_token=self.settings.telegram_bot_token,
            chat_id=self.settings.telegram_chat_id,
            enabled=bool(self.settings.telegram_bot_token)
        )
        
        # 注册熔断器触发回调
        async def on_circuit_breaker_trigger(reason: str):
            await self.telegram.send_message(
                f"🚨 熔断器触发\n"
                f"原因: {reason}\n"
                f"时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                f"交易已暂停，请检查策略或手动重置。"
            )
        
        self.circuit_breaker.on_trigger(on_circuit_breaker_trigger)
        
        # 6. 钱包质量评分器
        self.quality_scorer = WalletQualityScorer(
            min_trades=self.settings.wallet_quality.min_trades,
            min_win_rate=self.settings.wallet_quality.min_win_rate,
            min_profit_factor=self.settings.wallet_quality.min_profit_factor,
        )
        
        # 7. 做市商检测器
        self.market_maker_detector = MarketMakerDetector()
        
        # 8. 警告检测器
        self.warning_detector = RedFlagDetector(
            min_trades=self.settings.wallet_quality.min_trades,
            min_win_rate=self.settings.wallet_quality.min_win_rate,
        )
        
        # 9. 钱包监控器
        self.wallet_monitor = WalletMonitor(
            polygonscan_api_key=self.settings.polygonscan_api_key,
            mode=MonitorMode.HYBRID if self.settings.websocket.enabled else MonitorMode.POLLING,
            poll_interval=self.settings.monitoring.wallet_scan_interval,
            ws_url=self.settings.websocket.wallet_monitor_ws_url,
        )
        
        # 10. WebSocket管理器
        if self.settings.websocket.enabled:
            self.ws_manager = PolymarketWebSocket(
                url=self.settings.websocket.polymarket_ws_url,
                reconnect_interval=self.settings.websocket.reconnect_interval,
                max_reconnect_attempts=self.settings.websocket.max_reconnect_attempts,
            )
        else:
            self.ws_manager = None
        
        # 11. 跟单执行器
        copy_config = CopyConfig(
            mode=CopyMode(self.settings.copy_trading.mode),
            fixed_amount=self.settings.copy_trading.fixed_amount,
            proportional_ratio=self.settings.copy_trading.proportional_ratio,
            max_amount=self.settings.copy_trading.max_amount,
            min_amount=self.settings.copy_trading.min_amount,
            copy_delay_seconds=self.settings.copy_trading.copy_delay_seconds,
            enabled=self.settings.copy_trading.enabled,
        )
        
        self.copy_executor = CopyExecutor(
            client=self.client,
            risk_manager=self.risk_manager,
            quality_scorer=self.quality_scorer,
            market_maker_detector=self.market_maker_detector,
            warning_detector=self.warning_detector,
            telegram=self.telegram,
            copy_config=copy_config,
        )
        
        # 12. 钱包扫描器 - 自动发现高质量钱包
        self.wallet_scanner = WalletScanner(
            quality_scorer=self.quality_scorer,
            mm_detector=self.market_maker_detector,
            warning_detector=self.warning_detector,
            polygonscan_api_key=self.settings.polygonscan_api_key,
            min_quality_score=self.settings.wallet_quality.min_quality_score,
            min_win_rate=self.settings.wallet_quality.min_win_rate,
            min_trades=self.settings.wallet_quality.min_trades,
            min_profit_factor=self.settings.wallet_quality.min_profit_factor,
            max_following_wallets=self.settings.copy_trading.max_following_wallets,
            scan_interval_minutes=self.settings.monitoring.wallet_scan_interval // 60,
            dry_run=self.settings.dry_run,
        )
        
        # 设置钱包发现回调
        self.wallet_scanner.set_discovery_callback(self._on_wallet_discovered)
    
    async def start(self) -> None:
        """启动机器人"""
        logger.info("正在启动交易机器人...")
        
        # 验证配置
        errors = self.settings.validate()
        if errors:
            for error in errors:
                logger.error(f"配置错误: {error}")
            raise InitializationError(f"配置验证失败: {errors}")
        
        # 连接 Polymarket
        if not await self.client.connect():
            raise InitializationError("无法连接到 Polymarket API")
        
        # 获取初始余额
        balance = await self.client.get_account_balance()
        if balance:
            self.risk_manager.update_balance(balance)
            logger.info(f"账户余额: ${balance:.2f}")
        
        # 添加目标钱包 (如果有手动配置)
        for wallet in self.settings.copy_trading.target_wallets:
            self.wallet_monitor.add_wallet(wallet)
        
        # 启动钱包扫描器 (自动发现高质量钱包)
        if self.settings.copy_trading.auto_discover:
            await self.wallet_scanner.start()
            logger.info("钱包扫描器已启动 - 自动发现高质量钱包")
        
        # 注册跟单回调
        self.wallet_monitor.on_trade(self._on_wallet_trade)
        
        # 发送启动通知
        await self.telegram.send_startup_notification(
            wallet_address=self.settings.wallet_address,
            dry_run=self.settings.dry_run,
            max_daily_loss=self.settings.risk.max_daily_loss
        )
        
        self._running = True
        logger.info("交易机器人已启动")
        
        try:
            await self._run_main_loop()
        except GracefulShutdown:
            logger.info("收到关闭信号，正在停止...")
        finally:
            await self.stop()
    
    async def stop(self) -> None:
        """停止机器人"""
        if not self._running:
            return
        
        self._running = False
        logger.info("正在停止交易机器人...")
        
        # 取消所有任务
        if self._main_tasks:
            logger.info(f'正在取消{len(self._main_tasks)}个后台任务...')
            for task in self._main_tasks:
                if not task.done():
                    task.cancel()
            # 等待任务取消完成
            try:
                await asyncio.gather(*self._main_tasks, return_exceptions=True)
            except Exception as e:
                logger.debug(f'任务取消异常(忽略): {e}')
            self._main_tasks = []
        
        # 停止钱包扫描器
        await self.wallet_scanner.stop()
        
        # 停止钱包监控
        await self.wallet_monitor.stop()
        
        # 断开WebSocket
        if self.ws_manager:
            await self.ws_manager.disconnect()
        
        # 平掉所有持仓
        await self._close_all_positions("机器人关闭")
        
        # 断开连接
        await self.client.disconnect()
        
        # 发送停止通知
        await self.telegram.send_shutdown_notification("用户请求停止")
        
        logger.info("交易机器人已停止")
    
    async def _run_main_loop(self) -> None:
        """主循环"""
        tasks = []
        
        # 策略交易任务
        tasks.append(asyncio.create_task(self._strategy_trading_loop()))
        
        # 钱包监控任务 (跟单模式启用时)
        if self.settings.copy_trading.enabled:
            tasks.append(asyncio.create_task(self.wallet_monitor.start()))
        
        # WebSocket连接任务
        if self.ws_manager:
            tasks.append(asyncio.create_task(self._websocket_loop()))
        
        # 持仓检查任务
        tasks.append(asyncio.create_task(self._position_check_loop()))
        
        # 定期报告任务
        tasks.append(asyncio.create_task(self._periodic_report_loop()))
        
        # 保存任务引用，用于停止时取消
        self._main_tasks = tasks
        
        # 等待所有任务
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info('主循环任务被取消')
            raise
        finally:
            self._main_tasks = []
    
    async def _strategy_trading_loop(self) -> None:
        """策略交易循环"""
        scan_interval = self.settings.monitoring.wallet_scan_interval
        
        while self._running and not self._shutdown_requested:
            try:
                # 扫描市场机会
                await self._scan_markets()
                
                # 等待下一次扫描，分次睡眠以便快速响应停止
                for _ in range(scan_interval):
                    if not self._running or self._shutdown_requested:
                        break
                    await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"策略交易循环异常: {e}")
                if self._running and not self._shutdown_requested:
                    for _ in range(10):
                        if not self._running or self._shutdown_requested:
                            break
                        await asyncio.sleep(1)
    
    async def _websocket_loop(self) -> None:
        """WebSocket循环"""
        if not self.ws_manager:
            return
        
        # 注册事件处理器
        self.ws_manager.on_event("connect", self._on_ws_connect)
        self.ws_manager.on_event("disconnect", self._on_ws_disconnect)
        self.ws_manager.on_event("error", self._on_ws_error)
        
        # 连接WebSocket
        await self.ws_manager.connect()
    
    async def _position_check_loop(self) -> None:
        """持仓检查循环"""
        check_interval = self.settings.monitoring.position_check_interval
        
        while self._running and not self._shutdown_requested:
            try:
                await self._check_position_exits()
                # 分次睡眠，每秒检查停止标志
                for _ in range(check_interval):
                    if not self._running or self._shutdown_requested:
                        break
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"持仓检查异常: {e}")
                if self._running and not self._shutdown_requested:
                    for _ in range(10):
                        if not self._running or self._shutdown_requested:
                            break
                        await asyncio.sleep(1)
    
    async def _periodic_report_loop(self) -> None:
        """定期报告循环"""
        report_interval = 3600  # 每小时报告一次
        
        while self._running and not self._shutdown_requested:
            try:
                # 分次睡眠，每秒检查停止标志
                for _ in range(report_interval):
                    if not self._running or self._shutdown_requested:
                        break
                    await asyncio.sleep(1)
                
                if not self._running or self._shutdown_requested:
                    break
                    
                # 获取扫描器统计
                scanner_stats = self.wallet_scanner.get_stats()
                
                # 发送报告
                await self.telegram.send_message(
                    f"📊 运行状态报告\n"
                    f"监控钱包数: {scanner_stats['active_wallets']}\n"
                    f"已发现钱包: {scanner_stats['total_discovered']}\n"
                    f"已扫描钱包: {scanner_stats['scanned_wallets']}\n"
                    f"当前持仓: {len(self.risk_manager.get_positions())}"
                )
                
            except Exception as e:
                logger.error(f"定期报告异常: {e}")
    
    async def _on_wallet_trade(self, tx) -> None:
        """钱包交易回调"""
        try:
            await self.copy_executor.process_transaction(tx)
        except Exception as e:
            logger.error(f"处理钱包交易异常: {e}")
    
    async def _on_wallet_discovered(self, wallet_address: str, wallet_info: dict) -> None:
        """发现高质量钱包回调 - 自动添加到监控列表"""
        try:
            # 添加到钱包监控
            self.wallet_monitor.add_wallet(wallet_address)
            
            quality = wallet_info.get("quality")
            
            # 发送通知
            await self.telegram.send_message(
                f"🎯 发现高质量钱包\n"
                f"地址: {wallet_address[:10]}...\n"
                f"评分: {quality.overall_score:.1f}\n"
                f"胜率: {quality.stats.win_rate*100:.1f}%\n"
                f"盈亏比: {quality.stats.profit_factor:.2f}\n"
                f"已自动添加到监控列表"
            )
            
            logger.info(
                f"自动添加高质量钱包到监控: {wallet_address[:10]}... "
                f"(评分: {quality.overall_score:.1f}, 胜率: {quality.stats.win_rate*100:.1f}%)"
            )
            
        except Exception as e:
            logger.error(f"处理钱包发现回调异常: {e}")
    
    async def _on_ws_connect(self, data) -> None:
        """WebSocket连接事件"""
        logger.info("WebSocket已连接")
    
    async def _on_ws_disconnect(self, data) -> None:
        """WebSocket断开事件"""
        logger.warning("WebSocket已断开")
    
    async def _on_ws_error(self, data) -> None:
        """WebSocket错误事件"""
        logger.error(f"WebSocket错误: {data}")
    
    async def _scan_markets(self) -> None:
        """扫描市场寻找机会"""
        # 检查熔断器
        can_trade, reason = self.circuit_breaker.check_can_trade()
        if not can_trade:
            logger.debug(f"熔断器激活中: {reason}")
            return
        
        # 获取活跃市场
        markets = await self.client.get_markets(limit=50)
        if not markets:
            logger.debug("未获取到市场数据")
            return
        
        logger.info(f"扫描了 {len(markets)} 个市场")
        
        # 分析每个市场
        for market_info in markets[:10]:  # 限制处理数量
            if self._shutdown_requested:
                break
            
            await self._analyze_and_trade(market_info)
    
    async def _analyze_and_trade(self, market_info: dict) -> None:
        """分析市场并执行交易"""
        # 解析市场数据
        market_data = self.client.parse_market_data(market_info)
        if not market_data:
            return
        
        # 策略分析
        result = self.strategy_manager.analyze_market(market_data)
        
        if not result.should_trade:
            return
        
        # 执行交易
        await self._execute_signal(market_data, result)
    
    async def _execute_signal(
        self,
        market_data: MarketData,
        result
    ) -> None:
        """执行交易信号"""
        # 确定交易参数
        side = "YES" if result.signal == SignalType.BUY_YES else "NO"
        price = market_data.yes_price if side == "YES" else market_data.no_price
        size = result.suggested_size or self.settings.risk.max_position_size * result.confidence
        
        # 风险检查
        risk_result = self.risk_manager.check_trade(
            market_id=market_data.market_id,
            side=side,
            requested_size=size,
            confidence_score=result.confidence,
            price=price
        )
        
        if not risk_result.allowed:
            logger.info(f"风险检查未通过: {risk_result.reason}")
            return
        
        # 使用建议仓位
        final_size = risk_result.suggested_size or size
        
        # 发送交易通知
        await self.telegram.send_trade_notification(
            action="开仓",
            market_question=market_data.question,
            side=side,
            amount=final_size,
            price=price,
            confidence=result.confidence,
            strategy=result.strategy_type.value
        )
        
        # 执行订单
        order_result = await self.client.place_order(
            market_id=market_data.market_id,
            side=side,
            size=final_size,
            price=price
        )
        
        if order_result.success:
            # 开仓记录
            self.risk_manager.open_position(
                market_id=market_data.market_id,
                market_question=market_data.question,
                side=side,
                price=order_result.filled_price,
                size=order_result.filled_size
            )
            
            logger.info(
                f"开仓成功 | 市场: {market_data.question[:30]}... | "
                f"方向: {side} | 价格: {order_result.filled_price} | "
                f"仓位: ${order_result.filled_size}"
            )
        else:
            logger.error(f"下单失败: {order_result.error}")
    
    async def _check_position_exits(self) -> None:
        """检查持仓退出条件"""
        positions = self.risk_manager.get_positions()
        if not positions:
            return
        
        # 获取所有持仓市场的当前价格
        market_prices = {}
        for market_id in positions:
            price_data = await self.client.get_market_price(market_id)
            if price_data:
                market_prices[market_id] = price_data.get("yes", Decimal("0.5"))
        
        # 检查退出条件
        exits = self.risk_manager.check_position_exits(market_prices)
        
        for exit_info in exits:
            await self._close_position(exit_info)
    
    async def _close_position(self, exit_info: dict) -> None:
        """平仓"""
        market_id = exit_info["market_id"]
        exit_price = exit_info["exit_price"]
        reason = exit_info["reason"]
        
        position = self.risk_manager.close_position(market_id, exit_price)
        if position:
            await self.telegram.send_position_closed_notification(
                market_question=position.market_question,
                pnl=position.pnl,
                pnl_percentage=position.pnl_percentage,
                reason=reason
            )
    
    async def _close_all_positions(self, reason: str) -> None:
        """平掉所有持仓"""
        positions = self.risk_manager.get_positions()
        
        for market_id, position in positions.items():
            # 使用当前价格平仓
            await self._close_position({
                "market_id": market_id,
                "exit_price": position.current_price,
                "reason": reason
            })
    
    def request_shutdown(self, reason: str = "用户请求") -> None:
        """请求关闭"""
        logger.info(f"收到关闭请求: {reason}")
        self._shutdown_requested = True
        self._running = False


async def main() -> None:
    """主入口"""
    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="Polymarket 自适应跟单机器人"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模拟模式（不执行实际交易）"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别"
    )
    args = parser.parse_args()
    
    # 设置日志
    setup_logging(
        level=args.log_level,
        log_file="trading_bot.log",
        log_dir="logs"
    )
    
    # 加载配置
    settings = get_settings()
    
    # 命令行参数覆盖配置
    if args.dry_run:
        settings.dry_run = True
    
    # 创建机器人
    bot = TradingBot(settings)
    
    # 设置信号处理
    def signal_handler(sig, frame):
        logger.info(f'收到信号: {sig}，正在停止...')
        bot.request_shutdown('收到中断信号')
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 运行
    try:
        await bot.start()
    except InitializationError as e:
        logger.error(f"初始化失败: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"运行异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
