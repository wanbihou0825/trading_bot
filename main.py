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
- ─── 生产级安全功能 ───
- 紧急停止文件检测
- 强制平仓兜底
- 结构化日志+交易流水
- 监控告警（心跳、余额异常）
- Slippage/价格偏差保护

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
from typing import Any, Optional

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

# ─── 生产级安全功能导入 ───
from utils.emergency_stop import EmergencyStop, ForcedLiquidation
from utils.structured_logging import TradeLogger, AuditLogger, setup_structured_logging
from utils.monitoring import MonitoringService, HealthStatus, ErrorNotifier
from utils.slippage_protection import SlippageProtection, OrderValidator
from utils.trade_persistence import TradePersistence  # 交易持久化

logger = get_logger(__name__)


class TradingBot:
    """
    交易机器人主类
    
    协调各个组件完成交易流程，支持策略交易和跟单交易。
    集成生产级安全功能：紧急停止、强制平仓、监控告警等。
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
        
        # 初始化日志系统（结构化日志）
        self.trade_logger, self.audit_logger = setup_structured_logging(
            log_dir="logs",
            log_level="INFO",
            enable_json_logs=True,
            enable_trade_logs=True,
            enable_audit_logs=True
        )
        
        # 初始化组件
        self._init_components()
        
        # 初始化生产级安全功能
        self._init_safety_features()
        
        logger.info(
            f"交易机器人初始化 | "
            f"模式: {'模拟' if settings.dry_run else '实盘'} | "
            f"钱包: {mask_wallet_address(settings.wallet_address)} | "
            f"跟单: {'启用' if settings.copy_trading.enabled else '禁用'}"
        )

    def _get_signature_type(self):
        """
        确定签名类型

        优先级:
        1. 如果显式配置了 POLYMARKET_SIGNATURE_TYPE，使用该值
        2. 如果配置了 FUNDER_ADDRESS，使用 Gnosis Safe (type=2)
        3. 否则使用 EOA (type=0)

        说明:
        - 大多数通过 MetaMask 等浏览器钱包登录 Polymarket 的用户使用 Proxy Wallet
        - FUNDER_ADDRESS 应该从 polymarket.com/settings 获取
        """
        from services.polymarket_client import SignatureType

        # 1. 优先使用显式配置的签名类型
        if self.settings.polymarket_signature_type is not None:
            sig_type = self.settings.polymarket_signature_type
            logger.info(f"使用显式配置的签名类型: {sig_type}")
            # 将整数转换为 SignatureType 枚举
            for st in SignatureType:
                if st.value == sig_type:
                    return st
            logger.warning(f"未知的签名类型: {sig_type}，使用默认值")

        # 2. 如果配置了 funder_address，默认使用 Gnosis Safe 模式
        if self.settings.funder_address:
            logger.info(f"检测到 FUNDER_ADDRESS，使用 Gnosis Safe 模式 (type=2)")
            return SignatureType.POLY_GNOSIS_SAFE

        # 3. 默认使用 EOA 模式
        logger.info(f"未配置 FUNDER_ADDRESS 或签名类型，使用 EOA 模式 (type=0)")
        return SignatureType.EOA

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
            private_key=self.settings.private_key,
            wallet_address=self.settings.wallet_address,
            funder_address=self.settings.funder_address,  # Proxy 地址（资金存放位置）
            signature_type=self._get_signature_type(),
            dry_run=self.settings.dry_run
        )
        
        # 5. Telegram 服务
        self.telegram = TelegramService(
            bot_token=self.settings.telegram_bot_token,
            chat_id=self.settings.telegram_chat_id,
            enabled=bool(self.settings.telegram_bot_token)
        )
        
        # 注册熔断器触发回调（异常隔离）
        async def on_circuit_breaker_trigger(reason: str):
            try:
                await self.telegram.send_message(
                    f"🚨 熔断器触发\n"
                    f"原因: {reason}\n"
                    f"时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                    f"交易已暂停，请检查策略或手动重置。"
                )
            except Exception as e:
                logger.error(f"熔断器通知发送失败: {e}")
        
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
        
        # 11. 交易持久化 (幂等性保证)
        self.persistence = TradePersistence(
            db_path=self.settings.copy_trading.trade_db_path
        )
        
        # 12. 跟单执行器
        copy_config = CopyConfig(
            mode=CopyMode(self.settings.copy_trading.mode),
            fixed_amount=self.settings.copy_trading.fixed_amount,
            proportional_ratio=self.settings.copy_trading.proportional_ratio,
            max_amount=self.settings.copy_trading.max_amount,
            min_amount=self.settings.copy_trading.min_amount,
            copy_delay_seconds=self.settings.copy_trading.copy_delay_seconds,
            enabled=self.settings.copy_trading.enabled,
            follow_close=self.settings.copy_trading.follow_close,
            close_on_target_close=self.settings.copy_trading.close_on_target_close,
            position_sync_interval=self.settings.copy_trading.position_sync_interval,
        )
        
        self.copy_executor = CopyExecutor(
            client=self.client,
            risk_manager=self.risk_manager,
            quality_scorer=self.quality_scorer,
            market_maker_detector=self.market_maker_detector,
            warning_detector=self.warning_detector,
            telegram=self.telegram,
            copy_config=copy_config,
            persistence=self.persistence,  # 传入持久化管理器
        )
        
        # 13. 钱包扫描器 - 自动发现高质量钱包
        self.wallet_scanner = WalletScanner(
            quality_scorer=self.quality_scorer,
            mm_detector=self.market_maker_detector,
            warning_detector=self.warning_detector,
            polygonscan_api_key=self.settings.polygonscan_api_key,
            polymarket_client=self.client,
            seed_wallets=self.settings.copy_trading.seed_wallets,
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
    
    def _init_safety_features(self) -> None:
        """初始化生产级安全功能"""
        
        # 1. 紧急停止机制
        self.emergency_stop = EmergencyStop(
            stop_file_path="EMERGENCY_STOP",
            check_interval=5.0,
            on_stop_callback=self._on_emergency_stop
        )
        
        # 2. 强制平仓管理器
        self.forced_liquidation = ForcedLiquidation(
            client=self.client,
            risk_manager=self.risk_manager,
            telegram=self.telegram
        )
        
        # 3. Slippage保护
        self.slippage_protection = SlippageProtection(
            client=self.client,
            max_slippage=self.settings.risk.max_slippage,
            max_price_deviation=Decimal("0.05")
        )
        
        # 4. 订单验证器
        self.order_validator = OrderValidator(
            client=self.client,
            risk_manager=self.risk_manager,
            slippage_protection=self.slippage_protection
        )
        
        # 5. 监控服务
        self.monitoring = MonitoringService(
            telegram=self.telegram,
            heartbeat_interval=300.0,
            balance_check_interval=300.0
        )
        
        # 6. 错误通知器
        self.error_notifier = ErrorNotifier(telegram=self.telegram)
        
        logger.info("生产级安全功能初始化完成")
    
    async def _on_emergency_stop(self) -> None:
        """紧急停止回调"""
        logger.critical("🚨 紧急停止触发，执行强制平仓...")
        
        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log_emergency_stop("检测到紧急停止文件")
        
        # 强制平仓
        await self.forced_liquidation.liquidate_all(reason="紧急停止")
        
        # 停止机器人
        self._shutdown_requested = True
        self._running = False
    
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
        
        # 连接持久化数据库
        await self.persistence.connect()
        
        # 注入持久化到 risk_manager
        self.risk_manager.set_persistence(self.persistence)
        
        # 从数据库恢复仓位（崩溃恢复）
        restored = await self.risk_manager.restore_positions()
        if restored:
            logger.info(f"✅ 恢复了 {restored} 个持仓")
        
        # 获取初始余额
        balance = await self.client.get_account_balance()
        if balance is not None:
            self.risk_manager.update_balance(balance)
            logger.info(f"账户余额: ${balance:.2f}")
            
            # 设置预期余额（用于监控）
            self.monitoring.set_expected_balance(balance)
        
        # 添加目标钱包 (如果有手动配置)
        for wallet in self.settings.copy_trading.target_wallets:
            self.wallet_monitor.add_wallet(wallet)
        
        # 启动钱包扫描器 (自动发现高质量钱包)
        if self.settings.copy_trading.auto_discover:
            await self.wallet_scanner.start()
            logger.info("钱包扫描器已启动 - 自动发现高质量钱包")
        
        # 注册跟单回调
        self.wallet_monitor.on_trade(self._on_wallet_trade)
        
        # ─── 启动生产级安全功能 ───
        
        # 启动紧急停止监控
        await self.emergency_stop.start()
        
        # 启动监控服务
        self.monitoring.set_balance_provider(self._get_balance)
        self.monitoring.register_component("client", self._check_client_health)
        self.monitoring.register_component("risk_manager", self._check_risk_manager_health)
        await self.monitoring.start()
        
        # 发送启动通知
        await self.telegram.send_startup_notification(
            wallet_address=self.settings.wallet_address,
            dry_run=self.settings.dry_run,
            max_daily_loss=self.settings.risk.max_daily_loss
        )
        
        self._running = True
        logger.info("交易机器人已启动")
        
        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log_event(
                event_type="BOT_START",
                description="交易机器人启动",
                dry_run=self.settings.dry_run
            )
        
        try:
            await self._run_main_loop()
        except GracefulShutdown:
            logger.info("收到关闭信号，正在停止...")
        finally:
            await self.stop()
    
    async def _get_balance(self) -> Decimal:
        """获取余额（用于监控）"""
        return await self.client.get_account_balance() or Decimal("0")
    
    async def _check_client_health(self) -> HealthStatus:
        """检查客户端健康状态"""
        is_connected = self.client.is_connected
        return HealthStatus(
            component="client",
            is_healthy=is_connected,
            last_check=datetime.now(timezone.utc),
            message="已连接" if is_connected else "未连接"
        )
    
    async def _check_risk_manager_health(self) -> HealthStatus:
        """检查风险管理器健康状态"""
        can_trade, reason = self.circuit_breaker.check_can_trade()
        return HealthStatus(
            component="risk_manager",
            is_healthy=can_trade,
            last_check=datetime.now(timezone.utc),
            message=reason if not can_trade else "正常"
        )
    
    async def stop(self) -> None:
        """停止机器人"""
        if not self._running:
            return
        
        self._running = False
        logger.info("正在停止交易机器人...")
        
        # ─── 停止生产级安全功能 ───
        
        try:
            await self.emergency_stop.stop()
        except Exception as e:
            logger.error(f"停止紧急停止监控异常: {e}")
        
        try:
            await self.monitoring.stop()
        except Exception as e:
            logger.error(f"停止监控服务异常: {e}")
        
        # 取消所有任务
        if self._main_tasks:
            logger.info(f'正在取消{len(self._main_tasks)}个后台任务...')
            for task in self._main_tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*self._main_tasks, return_exceptions=True)
            except Exception as e:
                logger.debug(f'任务取消异常(忽略): {e}')
            self._main_tasks = []
        
        # 停止钱包扫描器
        try:
            await self.wallet_scanner.stop()
        except Exception as e:
            logger.error(f"停止钱包扫描器异常: {e}")
        
        # 停止钱包监控
        try:
            await self.wallet_monitor.stop()
        except Exception as e:
            logger.error(f"停止钱包监控异常: {e}")
        
        # 断开WebSocket
        if self.ws_manager:
            try:
                await self.ws_manager.disconnect()
            except Exception as e:
                logger.error(f"断开WebSocket异常: {e}")
        
        # ─── 强制平仓所有持仓（兜底机制，带超时）───
        try:
            await asyncio.wait_for(
                self.forced_liquidation.liquidate_all(reason="机器人关闭"),
                timeout=30
            )
        except asyncio.TimeoutError:
            logger.error("强制平仓超时 (30s)，跳过")
        except Exception as e:
            logger.error(f"强制平仓异常: {e}")
        
        # 关闭持久化数据库
        try:
            await self.persistence.close()
        except Exception as e:
            logger.error(f"关闭数据库异常: {e}")
        
        # 断开连接
        try:
            await self.client.disconnect()
        except Exception as e:
            logger.error(f"断开连接异常: {e}")
        
        # 发送停止通知
        try:
            await self.telegram.send_shutdown_notification("用户请求停止")
        except Exception as e:
            logger.error(f"发送停止通知异常: {e}")
        
        # 记录审计日志
        if self.audit_logger:
            self.audit_logger.log_event(
                event_type="BOT_STOP",
                description="交易机器人停止"
            )
        
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
                
                # asyncio.sleep 本身支持被 cancel，会立即抛 CancelledError
                await asyncio.sleep(scan_interval)
                
            except asyncio.CancelledError:
                logger.info("策略交易循环收到取消信号，正在退出...")
                break
            except Exception as e:
                logger.error(f"策略交易循环异常: {e}")
                if self._running and not self._shutdown_requested:
                    await asyncio.sleep(10)
    
    async def _websocket_loop(self) -> None:
        """WebSocket循环"""
        if not self.ws_manager:
            return
        
        # 注册事件处理器
        self.ws_manager.on_event("connect", self._on_ws_connect)
        self.ws_manager.on_event("disconnect", self._on_ws_disconnect)
        self.ws_manager.on_event("error", self._on_ws_error)
        
        # 连接WebSocket
        try:
            await self.ws_manager.connect()
        except asyncio.CancelledError:
            logger.info("WebSocket循环收到取消信号，正在退出...")
            await self.ws_manager.disconnect()
            raise
    
    async def _position_check_loop(self) -> None:
        """持仓检查循环"""
        check_interval = self.settings.monitoring.position_check_interval
        
        while self._running and not self._shutdown_requested:
            try:
                await self._check_position_exits()
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                logger.info("持仓检查循环收到取消信号，正在退出...")
                break
            except Exception as e:
                logger.error(f"持仓检查异常: {e}")
                if self._running and not self._shutdown_requested:
                    await asyncio.sleep(10)
    
    async def _periodic_report_loop(self) -> None:
        """定期报告循环"""
        report_interval = 3600  # 每小时报告一次
        
        while self._running and not self._shutdown_requested:
            try:
                await asyncio.sleep(report_interval)
                
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
                
            except asyncio.CancelledError:
                logger.info("定期报告循环收到取消信号，正在退出...")
                break
            except Exception as e:
                logger.error(f"定期报告异常: {e}")
    
    async def _on_wallet_trade(self, tx) -> None:
        """钱包交易回调"""
        try:
            await self.copy_executor.process_transaction(tx)
        except Exception as e:
            logger.error(f"处理钱包交易异常: {e}")
    
    async def _on_wallet_discovered(self, wallet_address: str, wallet_info: dict[str, Any]) -> None:
        """发现高质量钱包回调 - 自动添加到监控列表"""
        try:
            # 添加到钱包监控
            self.wallet_monitor.add_wallet(wallet_address)
            
            quality = wallet_info.get("quality")
            
            # 发送通知
            if quality:
                await self.telegram.send_message(
                    f"🎯 发现高质量钱包\n"
                    f"地址: {wallet_address[:10]}...\n"
                    f"评分: {getattr(quality, 'overall_score', 'N/A')}\n"
                    f"胜率: {getattr(getattr(quality, 'stats', None), 'win_rate', 0)*100:.1f}%\n"
                    f"盈亏比: {getattr(getattr(quality, 'stats', None), 'profit_factor', 0):.2f}\n"
                    f"已自动添加到监控列表"
                )
                
                logger.info(
                    f"自动添加高质量钱包到监控: {wallet_address[:10]}... "
                    f"(评分: {getattr(quality, 'overall_score', 'N/A')}, "
                    f"胜率: {getattr(getattr(quality, 'stats', None), 'win_rate', 0)*100:.1f}%)"
                )
            else:
                await self.telegram.send_message(
                    f"🎯 发现钱包\n"
                    f"地址: {wallet_address[:10]}...\n"
                    f"已添加到监控列表"
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
    
    async def _analyze_and_trade(self, market_info: dict[str, Any]) -> None:
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
        
        # ─── Slippage保护：下单前价格检查 ───
        price_check = await self.slippage_protection.check_price_before_order(
            market_id=market_data.market_id,
            side=side,
            expected_price=price,
            size=final_size
        )
        
        if price_check.action == "cancel":
            logger.warning(f"订单取消（价格偏差过大）: {price_check.reason}")
            return
        
        # 使用调整后的价格（如果有）
        final_price = price_check.adjusted_price or price
        
        # 发送交易通知
        await self.telegram.send_trade_notification(
            action="开仓",
            market_question=market_data.question,
            side=side,
            amount=final_size,
            price=final_price,
            confidence=result.confidence,
            strategy=result.strategy_type.value
        )
        
        # 执行订单
        order_result = await self.client.place_order(
            market_id=market_data.market_id,
            side=side,
            size=final_size,
            price=final_price
        )
        
        if order_result.success:
            # 开仓记录
            position = await self.risk_manager.open_position(
                market_id=market_data.market_id,
                market_question=market_data.question,
                side=side,
                price=order_result.filled_price,
                size=order_result.filled_size
            )
            
            # ─── 记录交易日志 ───
            if self.trade_logger:
                self.trade_logger.log_position_open(
                    market_id=market_data.market_id,
                    market_question=market_data.question,
                    side=side,
                    size=order_result.filled_size,
                    entry_price=order_result.filled_price,
                    stop_loss=position.stop_loss_price if position else None,
                    take_profit=position.take_profit_price if position else None,
                    strategy=result.strategy_type.value,
                    confidence=float(result.confidence)
                )
            
            logger.info(
                f"开仓成功 | 市场: {market_data.question[:30]}... | "
                f"方向: {side} | 价格: {order_result.filled_price} | "
                f"仓位: ${order_result.filled_size}"
            )
            
            # 更新预期余额
            cost = order_result.filled_size * order_result.filled_price
            self.monitoring.balance.update_expected_balance(-cost)
            
        else:
            logger.error(f"下单失败: {order_result.error}")
            
            # 记录失败日志
            if self.audit_logger:
                self.audit_logger.log_event(
                    event_type="ORDER_FAILED",
                    description=f"下单失败: {order_result.error}",
                    severity="WARNING",
                    market_id=market_data.market_id
                )
    
    async def _check_position_exits(self) -> None:
        """检查持仓退出条件"""
        positions = self.risk_manager.get_positions()
        if not positions:
            return
        
        # 获取所有持仓市场的当前价格
        market_prices = {}
        for market_id, position in list(positions.items()):  # 用 list() 复制防止迭代中修改
            price_data = await self.client.get_market_price(market_id)
            if price_data:
                # 根据持仓方向用对应价格（YES用yes价，NO用no价）
                if position.side == "YES":
                    price = price_data.get("yes")
                else:
                    price = price_data.get("no")
                if price is not None:
                    market_prices[market_id] = price
                else:
                    logger.warning(f"获取市场 {market_id} 价格异常，跳过退出检查")
            else:
                logger.warning(f"无法获取市场 {market_id} 价格，跳过退出检查")
        
        # 检查退出条件
        exits = self.risk_manager.check_position_exits(market_prices)
        
        for exit_info in exits:
            await self._close_position(exit_info)
    
    async def _close_position(self, exit_info: dict[str, Any]) -> None:
        """平仓"""
        market_id = exit_info["market_id"]
        exit_price = exit_info["exit_price"]
        reason = exit_info["reason"]
        
        position = await self.risk_manager.close_position(market_id, exit_price)
        if position:
            # ─── 记录交易日志 ───
            if self.trade_logger:
                self.trade_logger.log_position_close(
                    market_id=market_id,
                    market_question=position.market_question,
                    side=position.side,
                    size=position.size,
                    exit_price=exit_price,
                    entry_price=position.entry_price,
                    pnl=float(position.pnl),
                    pnl_pct=float(position.pnl_percentage),
                    reason=reason
                )
            
            await self.telegram.send_position_closed_notification(
                market_question=position.market_question,
                pnl=position.pnl,
                pnl_percentage=position.pnl_percentage,
                reason=reason
            )
            
            # 更新预期余额：加回净盈亏，不是全额收入
            pnl = position.pnl  # (exit_price - entry_price) * size
            self.monitoring.balance.update_expected_balance(pnl)
    
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
    
    # ─── asyncio 事件循环信号处理（官方推荐方式）───
    loop = asyncio.get_running_loop()
    
    def shutdown_handler(sig):
        """信号处理器：优雅关闭所有任务"""
        logger.info(f"收到信号 {sig}，启动优雅关闭...")
        bot.request_shutdown(f"收到信号 {sig}")
        
        # 立即取消所有仍在运行的任务（关键！）
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task(loop):
                task.cancel()
    
    # 注册到事件循环
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler, sig)
    
    # 运行
    try:
        await bot.start()
    except asyncio.CancelledError:
        logger.info("主协程被取消（优雅关闭完成）")
    except InitializationError as e:
        logger.error(f"初始化失败: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"运行异常: {e}")
        sys.exit(1)
    finally:
        # 确保 stop 被调用
        if bot._running:
            await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
