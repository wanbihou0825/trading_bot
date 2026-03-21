"""
紧急停止机制
============
检测紧急停止文件，强制平仓所有持仓。
"""

import os
import asyncio
from pathlib import Path
from typing import Optional, Callable, Awaitable
from datetime import datetime, timezone

from utils.logger import get_logger

logger = get_logger(__name__)


class EmergencyStop:
    """
    紧急停止机制
    
    功能:
    1. 检测紧急停止文件
    2. 触发时强制平仓所有持仓
    3. 发送通知
    """
    
    # 默认停止文件路径
    DEFAULT_STOP_FILE = "EMERGENCY_STOP"
    
    def __init__(
        self,
        stop_file_path: Optional[str] = None,
        check_interval: float = 5.0,
        on_stop_callback: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        """
        初始化紧急停止机制
        
        Args:
            stop_file_path: 停止文件路径
            check_interval: 检查间隔（秒）
            on_stop_callback: 停止时的回调函数
        """
        self.stop_file_path = Path(stop_file_path or self.DEFAULT_STOP_FILE)
        self.check_interval = check_interval
        self.on_stop_callback = on_stop_callback
        
        self._running = False
        self._stop_requested = False
        self._check_task: Optional[asyncio.Task] = None
        
        logger.info(
            f"紧急停止机制初始化 | "
            f"停止文件: {self.stop_file_path.absolute()} | "
            f"检查间隔: {check_interval}s"
        )
    
    async def start(self) -> None:
        """启动监控"""
        if self._running:
            return
        
        self._running = True
        self._check_task = asyncio.create_task(self._check_loop())
        
        logger.info("紧急停止监控已启动")
    
    async def stop(self) -> None:
        """停止监控"""
        self._running = False
        
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
            self._check_task = None
        
        logger.info("紧急停止监控已停止")
    
    async def _check_loop(self) -> None:
        """检查循环"""
        while self._running:
            try:
                if self._check_stop_file():
                    await self._trigger_stop()
                    break
                
                await asyncio.sleep(self.check_interval)
                
            except asyncio.CancelledError:
                logger.info("紧急停止检查循环被取消")
                break
            except Exception as e:
                logger.error(f"紧急停止检查异常: {e}")
                await asyncio.sleep(self.check_interval)
    
    def _check_stop_file(self) -> bool:
        """
        检查停止文件是否存在
        
        Returns:
            是否检测到停止信号
        """
        try:
            if self.stop_file_path.exists():
                # 读取文件内容（可选：包含原因）
                try:
                    content = self.stop_file_path.read_text(encoding='utf-8').strip()
                    logger.warning(f"检测到紧急停止文件: {content or '无原因'}")
                except Exception:
                    logger.warning("检测到紧急停止文件")
                
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"检查停止文件异常: {e}")
            return False
    
    async def _trigger_stop(self) -> None:
        """触发停止"""
        if self._stop_requested:
            return
        
        self._stop_requested = True
        
        logger.critical(
            "🚨 紧急停止触发 🚨\n"
            f"时间: {datetime.now(timezone.utc).isoformat()}\n"
            "正在执行紧急停止流程..."
        )
        
        if self.on_stop_callback:
            try:
                await self.on_stop_callback()
            except Exception as e:
                logger.error(f"紧急停止回调异常: {e}")
        
        # 停止监控
        await self.stop()
    
    @staticmethod
    def create_stop_file(reason: str = "") -> None:
        """
        创建停止文件
        
        Args:
            reason: 停止原因
        """
        stop_file = Path(EmergencyStop.DEFAULT_STOP_FILE)
        timestamp = datetime.now(timezone.utc).isoformat()
        content = f"{timestamp}\n{reason}" if reason else timestamp
        stop_file.write_text(content, encoding='utf-8')
        logger.warning(f"已创建紧急停止文件: {stop_file.absolute()}")
    
    @staticmethod
    def remove_stop_file() -> None:
        """删除停止文件"""
        stop_file = Path(EmergencyStop.DEFAULT_STOP_FILE)
        if stop_file.exists():
            stop_file.unlink()
            logger.info("已删除紧急停止文件")
    
    @property
    def is_stop_requested(self) -> bool:
        """是否已请求停止"""
        return self._stop_requested


class ForcedLiquidation:
    """
    强制平仓管理
    
    在shutdown时强制平掉所有持仓
    """
    
    def __init__(
        self,
        client,  # PolymarketClient
        risk_manager,  # RiskManager
        telegram=None,  # TelegramService
    ):
        """
        初始化强制平仓管理器
        
        Args:
            client: Polymarket客户端
            risk_manager: 风险管理器
            telegram: Telegram服务
        """
        self.client = client
        self.risk_manager = risk_manager
        self.telegram = telegram
        
        logger.info("强制平仓管理器初始化完成")
    
    async def liquidate_all(
        self,
        reason: str = "紧急停止",
        timeout_per_position: float = 30.0,
    ) -> dict:
        """
        平掉所有持仓
        
        Args:
            reason: 平仓原因
            timeout_per_position: 每个持仓的超时时间
        
        Returns:
            平仓结果统计
        """
        positions = self.risk_manager.get_positions()
        
        if not positions:
            logger.info("无持仓需要平仓")
            return {"liquidated": 0, "failed": 0, "total_pnl": 0}
        
        logger.warning(
            f"⚠️ 开始强制平仓 ⚠️\n"
            f"原因: {reason}\n"
            f"持仓数: {len(positions)}"
        )
        
        # 发送通知
        if self.telegram:
            try:
                await self.telegram.send_message(
                    f"🚨 强制平仓\n"
                    f"原因: {reason}\n"
                    f"持仓数: {len(positions)}\n"
                    f"正在处理..."
                )
            except Exception as e:
                logger.error(f"发送Telegram通知失败: {e}")
        
        results = {
            "liquidated": 0,
            "failed": 0,
            "total_pnl": 0,
            "positions": []
        }
        
        for market_id, position in positions.items():
            try:
                # 获取当前价格
                price_data = await asyncio.wait_for(
                    self.client.get_market_price(market_id),
                    timeout=timeout_per_position
                )
                
                if not price_data:
                    raise ValueError("无法获取市场价格")
                
                exit_price = price_data.get("yes", position.current_price)
                
                # 执行平仓订单
                if not self.client.dry_run:
                    order = await asyncio.wait_for(
                        self.client.place_order(
                            market_id=market_id,
                            side="NO" if position.side == "YES" else "YES",
                            size=position.size,
                            price=exit_price
                        ),
                        timeout=timeout_per_position
                    )
                    
                    if not order.success:
                        raise ValueError(f"平仓订单失败: {order.error}")
                
                # 记录平仓
                closed_position = await self.risk_manager.close_position(
                    market_id=market_id,
                    exit_price=exit_price
                )
                
                if closed_position:
                    results["liquidated"] += 1
                    results["total_pnl"] += float(closed_position.pnl)
                    
                    logger.info(
                        f"平仓成功 | 市场: {closed_position.market_question[:30]}... | "
                        f"盈亏: ${closed_position.pnl:.2f}"
                    )
                    
                    results["positions"].append({
                        "market_id": market_id,
                        "market_question": closed_position.market_question,
                        "pnl": float(closed_position.pnl),
                        "exit_price": float(exit_price)
                    })
                
            except asyncio.TimeoutError:
                logger.error(f"平仓超时: {market_id}")
                results["failed"] += 1
                
            except Exception as e:
                logger.error(f"平仓失败: {market_id} | 错误: {e}")
                results["failed"] += 1
        
        # 发送结果通知
        if self.telegram:
            pnl_type = "盈利" if results["total_pnl"] >= 0 else "亏损"
            try:
                await self.telegram.send_message(
                    f"✅ 强制平仓完成\n"
                    f"成功: {results['liquidated']}\n"
                    f"失败: {results['failed']}\n"
                    f"总{pnl_type}: ${abs(results['total_pnl']):.2f}"
                )
            except Exception:
                pass
        
        logger.info(
            f"强制平仓完成 | 成功: {results['liquidated']} | "
            f"失败: {results['failed']} | "
            f"总盈亏: ${results['total_pnl']:.2f}"
        )
        
        return results
    
    async def liquidate_position(
        self,
        market_id: str,
        reason: str = "",
        timeout: float = 30.0,
    ) -> bool:
        """
        平掉单个持仓
        
        Args:
            market_id: 市场ID
            reason: 平仓原因
            timeout: 超时时间
        
        Returns:
            是否成功
        """
        position = self.risk_manager.get_position(market_id)
        if not position:
            logger.warning(f"未找到持仓: {market_id}")
            return False
        
        try:
            # 获取价格
            price_data = await asyncio.wait_for(
                self.client.get_market_price(market_id),
                timeout=timeout
            )
            
            if not price_data:
                raise ValueError("无法获取价格")
            
            exit_price = price_data.get("yes", position.current_price)
            
            # 下单平仓
            if not self.client.dry_run:
                order = await asyncio.wait_for(
                    self.client.place_order(
                        market_id=market_id,
                        side="NO" if position.side == "YES" else "YES",
                        size=position.size,
                        price=exit_price
                    ),
                    timeout=timeout
                )
                
                if not order.success:
                    raise ValueError(f"订单失败: {order.error}")
            
            # 记录平仓
            closed = await self.risk_manager.close_position(market_id, exit_price)
            
            if closed:
                logger.info(
                    f"平仓成功 | 市场: {closed.market_question[:30]}... | "
                    f"盈亏: ${closed.pnl:.2f} | 原因: {reason}"
                )
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"平仓失败: {market_id} | 错误: {e}")
            return False
