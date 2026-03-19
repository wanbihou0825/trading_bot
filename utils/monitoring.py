"""
监控告警模块
============
心跳检测、余额异常告警、错误堆栈Telegram通知。
"""

import asyncio
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, Awaitable, Dict, Any, List
from dataclasses import dataclass, field
import traceback

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class HealthStatus:
    """健康状态"""
    component: str
    is_healthy: bool
    last_check: datetime
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class HeartbeatMonitor:
    """
    心跳监控器
    
    定期检查各组件健康状态，发送心跳消息。
    """
    
    def __init__(
        self,
        check_interval: float = 60.0,
        heartbeat_interval: float = 300.0,  # 5分钟发送一次心跳
        max_missed_beats: int = 3,
        on_unhealthy_callback: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ):
        """
        初始化心跳监控器
        
        Args:
            check_interval: 检查间隔（秒）
            heartbeat_interval: 心跳发送间隔（秒）
            max_missed_beats: 最大错过心跳次数
            on_unhealthy_callback: 不健康时的回调
        """
        self.check_interval = check_interval
        self.heartbeat_interval = heartbeat_interval
        self.max_missed_beats = max_missed_beats
        self.on_unhealthy_callback = on_unhealthy_callback
        
        self._running = False
        self._components: Dict[str, HealthStatus] = {}
        self._last_heartbeat: Optional[datetime] = None
        self._missed_beats = 0
        self._check_task: Optional[asyncio.Task] = None
        
        logger.info(
            f"心跳监控器初始化 | 检查间隔: {check_interval}s | "
            f"心跳间隔: {heartbeat_interval}s"
        )
    
    def register_component(self, name: str, check_func: Callable[[], Awaitable[HealthStatus]]) -> None:
        """
        注册组件
        
        Args:
            name: 组件名称
            check_func: 健康检查函数
        """
        self._components[name] = HealthStatus(
            component=name,
            is_healthy=True,
            last_check=datetime.now(timezone.utc),
            message="已注册"
        )
        self._components[name].check_func = check_func
        logger.info(f"注册组件: {name}")
    
    async def start(self) -> None:
        """启动监控"""
        if self._running:
            return
        
        self._running = True
        self._check_task = asyncio.create_task(self._monitor_loop())
        logger.info("心跳监控已启动")
    
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
        
        logger.info("心跳监控已停止")
    
    async def _monitor_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                # 检查所有组件
                for name, status in list(self._components.items()):
                    if hasattr(status, 'check_func'):
                        try:
                            new_status = await asyncio.wait_for(
                                status.check_func(),
                                timeout=10.0
                            )
                            self._components[name] = new_status
                            
                            if not new_status.is_healthy:
                                await self._handle_unhealthy(name, new_status)
                                
                        except asyncio.TimeoutError:
                            self._components[name].is_healthy = False
                            self._components[name].message = "检查超时"
                            await self._handle_unhealthy(name, self._components[name])
                            
                        except Exception as e:
                            self._components[name].is_healthy = False
                            self._components[name].message = f"检查异常: {e}"
                            await self._handle_unhealthy(name, self._components[name])
                
                # 检查心跳
                await self._check_heartbeat()
                
                await asyncio.sleep(self.check_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"监控循环异常: {e}")
                await asyncio.sleep(self.check_interval)
    
    async def _handle_unhealthy(self, component_name: str, status: HealthStatus) -> None:
        """处理不健康组件"""
        logger.warning(
            f"组件不健康: {component_name} | "
            f"消息: {status.message}"
        )
        
        if self.on_unhealthy_callback:
            try:
                await self.on_unhealthy_callback(component_name, status.message)
            except Exception as e:
                logger.error(f"不健康回调异常: {e}")
    
    async def _check_heartbeat(self) -> None:
        """检查心跳"""
        now = datetime.now(timezone.utc)
        
        if self._last_heartbeat is None:
            self._last_heartbeat = now
            return
        
        elapsed = (now - self._last_heartbeat).total_seconds()
        
        if elapsed >= self.heartbeat_interval:
            # 应该发送心跳
            all_healthy = all(s.is_healthy for s in self._components.values())
            
            if all_healthy:
                self._missed_beats = 0
                self._last_heartbeat = now
                logger.info("💓 心跳: 所有组件健康")
            else:
                self._missed_beats += 1
                if self._missed_beats >= self.max_missed_beats:
                    logger.error(f"💔 心跳丢失 {self._missed_beats} 次")
    
    def get_health_report(self) -> Dict[str, Any]:
        """获取健康报告"""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "components": {
                name: {
                    "healthy": status.is_healthy,
                    "message": status.message,
                    "last_check": status.last_check.isoformat()
                }
                for name, status in self._components.items()
            },
            "all_healthy": all(s.is_healthy for s in self._components.values()),
            "missed_beats": self._missed_beats
        }


class BalanceMonitor:
    """
    余额监控器
    
    检测余额异常（如意外减少），发送告警。
    """
    
    def __init__(
        self,
        telegram=None,
        check_interval: float = 300.0,  # 5分钟
        anomaly_threshold: float = 0.05,  # 5%偏差视为异常
        min_balance: float = 10.0,  # 最低余额告警
    ):
        """
        初始化余额监控器
        
        Args:
            telegram: Telegram服务
            check_interval: 检查间隔
            anomaly_threshold: 异常阈值（比例）
            min_balance: 最低余额
        """
        self.telegram = telegram
        self.check_interval = check_interval
        self.anomaly_threshold = anomaly_threshold
        self.min_balance = min_balance
        
        self._running = False
        self._expected_balance: Optional[Decimal] = None
        self._last_balance: Optional[Decimal] = None
        self._check_task: Optional[asyncio.Task] = None
        self._get_balance_func: Optional[Callable[[], Awaitable[Decimal]]] = None
        
        logger.info(
            f"余额监控器初始化 | 检查间隔: {check_interval}s | "
            f"异常阈值: {anomaly_threshold*100}%"
        )
    
    def set_balance_provider(self, func: Callable[[], Awaitable[Decimal]]) -> None:
        """设置余额获取函数"""
        self._get_balance_func = func
    
    def set_expected_balance(self, balance: Decimal) -> None:
        """设置预期余额"""
        self._expected_balance = balance
        self._last_balance = balance
        logger.info(f"设置预期余额: ${balance}")
    
    async def start(self) -> None:
        """启动监控"""
        if self._running:
            return
        
        self._running = True
        self._check_task = asyncio.create_task(self._monitor_loop())
        logger.info("余额监控已启动")
    
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
        
        logger.info("余额监控已停止")
    
    async def _monitor_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                if self._get_balance_func:
                    balance = await self._get_balance_func()
                    await self._check_balance(balance)
                
                await asyncio.sleep(self.check_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"余额监控异常: {e}")
                await asyncio.sleep(self.check_interval)
    
    async def _check_balance(self, current_balance: Decimal) -> None:
        """检查余额"""
        # 检查最低余额
        if current_balance < Decimal(str(self.min_balance)):
            await self._send_alert(
                "⚠️ 低余额告警",
                f"当前余额: ${current_balance:.2f}\n"
                f"最低阈值: ${self.min_balance}"
            )
        
        # 检查异常变化
        if self._expected_balance is not None:
            expected = self._expected_balance
            deviation = abs(current_balance - expected) / expected if expected > 0 else Decimal("0")
            
            if deviation > Decimal(str(self.anomaly_threshold)):
                await self._send_alert(
                    "🚨 余额异常告警",
                    f"预期余额: ${expected:.2f}\n"
                    f"实际余额: ${current_balance:.2f}\n"
                    f"偏差: {float(deviation)*100:.1f}%"
                )
                
                logger.warning(
                    f"余额异常 | 预期: ${expected} | "
                    f"实际: ${current_balance} | 偏差: {float(deviation)*100:.1f}%"
                )
            else:
                # 更新预期余额（正常交易导致的变动）
                self._expected_balance = current_balance
        
        self._last_balance = current_balance
    
    async def _send_alert(self, title: str, message: str) -> None:
        """发送告警"""
        logger.warning(f"{title}\n{message}")
        
        if self.telegram:
            try:
                await self.telegram.send_message(f"{title}\n{message}")
            except Exception as e:
                logger.error(f"发送告警失败: {e}")
    
    def update_expected_balance(self, delta: Decimal) -> None:
        """更新预期余额（交易后调用）"""
        if self._expected_balance is not None:
            self._expected_balance += delta


class ErrorNotifier:
    """
    错误通知器
    
    发送错误堆栈到Telegram。
    """
    
    def __init__(
        self,
        telegram=None,
        min_severity: str = "ERROR",
        include_traceback: bool = True,
        cooldown_seconds: float = 60.0,  # 同类错误冷却时间
    ):
        """
        初始化错误通知器
        
        Args:
            telegram: Telegram服务
            min_severity: 最低严重级别
            include_traceback: 是否包含堆栈
            cooldown_seconds: 冷却时间
        """
        self.telegram = telegram
        self.min_severity = min_severity
        self.include_traceback = include_traceback
        self.cooldown_seconds = cooldown_seconds
        
        self._last_notifications: Dict[str, datetime] = {}
    
    async def notify_error(
        self,
        error: Exception,
        context: Optional[str] = None,
        severity: str = "ERROR",
    ) -> None:
        """
        发送错误通知
        
        Args:
            error: 异常
            context: 上下文
            severity: 严重级别
        """
        # 检查严重级别
        levels = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
        if levels.get(severity, 0) < levels.get(self.min_severity, 3):
            return
        
        # 检查冷却
        error_key = f"{type(error).__name__}:{str(error)[:50]}"
        now = datetime.now(timezone.utc)
        
        if error_key in self._last_notifications:
            elapsed = (now - self._last_notifications[error_key]).total_seconds()
            if elapsed < self.cooldown_seconds:
                logger.debug(f"错误通知冷却中: {error_key}")
                return
        
        self._last_notifications[error_key] = now
        
        # 构建消息
        message_parts = [
            f"🔴 错误通知",
            f"时间: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"级别: {severity}",
            f"类型: {type(error).__name__}",
            f"消息: {str(error)[:200]}"
        ]
        
        if context:
            message_parts.append(f"上下文: {context[:200]}")
        
        if self.include_traceback:
            tb = traceback.format_exc()
            if tb and tb != "NoneType: None\n":
                message_parts.append(f"堆栈:\n```\n{tb[:500]}\n```")
        
        message = "\n".join(message_parts)
        
        logger.error(message)
        
        if self.telegram:
            try:
                await self.telegram.send_message(message)
            except Exception as e:
                logger.error(f"发送错误通知失败: {e}")
    
    async def notify_critical(self, error: Exception, context: str = "") -> None:
        """发送严重错误通知"""
        await self.notify_error(error, context, "CRITICAL")
    
    async def notify_warning(self, message: str, context: str = "") -> None:
        """发送警告通知"""
        error = Exception(message)
        await self.notify_error(error, context, "WARNING")


class MonitoringService:
    """
    监控服务
    
    整合心跳、余额、错误监控。
    """
    
    def __init__(
        self,
        telegram=None,
        heartbeat_interval: float = 300.0,
        balance_check_interval: float = 300.0,
        balance_anomaly_threshold: float = 0.05,
    ):
        """
        初始化监控服务
        
        Args:
            telegram: Telegram服务
            heartbeat_interval: 心跳间隔
            balance_check_interval: 余额检查间隔
            balance_anomaly_threshold: 余额异常阈值
        """
        self.telegram = telegram
        
        self.heartbeat = HeartbeatMonitor(
            heartbeat_interval=heartbeat_interval,
            on_unhealthy_callback=self._on_unhealthy
        )
        
        self.balance = BalanceMonitor(
            telegram=telegram,
            check_interval=balance_check_interval,
            anomaly_threshold=balance_anomaly_threshold
        )
        
        self.error_notifier = ErrorNotifier(telegram=telegram)
        
        self._running = False
    
    async def start(self) -> None:
        """启动监控"""
        if self._running:
            return
        
        self._running = True
        await self.heartbeat.start()
        await self.balance.start()
        logger.info("监控服务已启动")
    
    async def stop(self) -> None:
        """停止监控"""
        self._running = False
        await self.heartbeat.stop()
        await self.balance.stop()
        logger.info("监控服务已停止")
    
    async def _on_unhealthy(self, component: str, message: str) -> None:
        """组件不健康回调"""
        if self.telegram:
            try:
                await self.telegram.send_message(
                    f"⚠️ 组件不健康\n"
                    f"组件: {component}\n"
                    f"消息: {message}"
                )
            except Exception:
                pass
    
    def register_component(self, name: str, check_func: Callable[[], Awaitable[HealthStatus]]) -> None:
        """注册组件到心跳监控"""
        self.heartbeat.register_component(name, check_func)
    
    def set_balance_provider(self, func: Callable[[], Awaitable[Decimal]]) -> None:
        """设置余额获取函数"""
        self.balance.set_balance_provider(func)
    
    def set_expected_balance(self, balance: Decimal) -> None:
        """设置预期余额"""
        self.balance.set_expected_balance(balance)
    
    async def notify_error(self, error: Exception, context: str = "") -> None:
        """发送错误通知"""
        await self.error_notifier.notify_error(error, context)
    
    def get_health_report(self) -> Dict[str, Any]:
        """获取健康报告"""
        return self.heartbeat.get_health_report()
