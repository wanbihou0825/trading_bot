"""
熔断器模块
==========
实现交易熔断机制，在达到风险限制时暂停交易。
"""

from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Optional, List
from dataclasses import dataclass, field

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DailyStats:
    """每日交易统计"""
    date: date
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: Decimal = Decimal("0")
    total_volume: Decimal = Decimal("0")
    
    @property
    def win_rate(self) -> Decimal:
        """胜率"""
        if self.total_trades == 0:
            return Decimal("0")
        return Decimal(self.winning_trades) / Decimal(self.total_trades)


@dataclass
class CircuitBreakerState:
    """熔断器状态"""
    is_active: bool = False
    triggered_at: Optional[datetime] = None
    reason: str = ""
    daily_loss: Decimal = Decimal("0")
    consecutive_losses: int = 0
    current_date: date = field(default_factory=lambda: date.today())


class CircuitBreaker:
    """
    熔断器
    
    在以下情况触发:
    1. 日累计损失达到 max_daily_loss
    2. 连续亏损次数达到 max_consecutive_losses
    3. 单笔亏损超过 max_single_loss
    
    触发后需要手动重置或等待次日自动重置。
    """
    
    def __init__(
        self,
        max_daily_loss: Decimal = Decimal("100"),
        max_consecutive_losses: int = 5,
        max_single_loss: Decimal = Decimal("50"),
        cooldown_hours: int = 24
    ):
        """
        初始化熔断器
        
        Args:
            max_daily_loss: 每日最大亏损限制
            max_consecutive_losses: 最大连续亏损次数
            max_single_loss: 单笔最大亏损
            cooldown_hours: 冷却时间（小时）
        """
        self.max_daily_loss = max_daily_loss
        self.max_consecutive_losses = max_consecutive_losses
        self.max_single_loss = max_single_loss
        self.cooldown_hours = cooldown_hours
        
        self._state = CircuitBreakerState()
        self._daily_stats = DailyStats(date=date.today())
        
        # 触发回调列表
        self._trigger_callbacks: List[callable] = []
        
        logger.info(
            f"熔断器初始化 | "
            f"日损失限制: ${max_daily_loss} | "
            f"连亏限制: {max_consecutive_losses}次 | "
            f"单笔限制: ${max_single_loss}"
        )
    
    def on_trigger(self, callback: callable) -> None:
        """
        注册熔断触发回调
        
        Args:
            callback: 异步回调函数，接收 reason 参数
        """
        self._trigger_callbacks.append(callback)
    
    def check_can_trade(self) -> tuple[bool, str]:
        """
        检查是否允许交易
        
        Returns:
            (是否允许, 原因)
        """
        # 检查日期是否变化，需要重置日统计
        self._check_date_reset()
        
        if self._state.is_active:
            # 检查冷却时间
            if self._state.triggered_at:
                elapsed = datetime.now(timezone.utc) - self._state.triggered_at
                hours_elapsed = elapsed.total_seconds() / 3600
                
                if hours_elapsed >= self.cooldown_hours:
                    self.reset()
                    return True, "冷却期已过，熔断器已重置"
            
            return False, f"熔断器激活中: {self._state.reason}"
        
        return True, "可以交易"
    
    def record_trade_result(
        self,
        pnl: Decimal,
        volume: Decimal
    ) -> bool:
        """
        记录交易结果
        
        Args:
            pnl: 盈亏金额
            volume: 交易量
        
        Returns:
            是否触发了熔断器
        """
        # 检查日期是否变化
        self._check_date_reset()
        
        # 更新统计
        self._daily_stats.total_trades += 1
        self._daily_stats.total_pnl += pnl
        self._daily_stats.total_volume += volume
        
        if pnl > 0:
            self._daily_stats.winning_trades += 1
            self._state.consecutive_losses = 0
        else:
            self._daily_stats.losing_trades += 1
            self._state.consecutive_losses += 1
        
        # 更新日损失
        if pnl < 0:
            self._state.daily_loss += abs(pnl)
        
        # 检查熔断条件
        triggered = False
        reason = ""
        
        # 条件1: 日累计损失
        if self._state.daily_loss >= self.max_daily_loss:
            triggered = True
            reason = f"日累计损失 ${self._state.daily_loss} 达到限制 ${self.max_daily_loss}"
        
        # 条件2: 连续亏损
        elif self._state.consecutive_losses >= self.max_consecutive_losses:
            triggered = True
            reason = f"连续亏损 {self._state.consecutive_losses} 次达到限制 {self.max_consecutive_losses}"
        
        # 条件3: 单笔亏损
        elif pnl < 0 and abs(pnl) >= self.max_single_loss:
            triggered = True
            reason = f"单笔亏损 ${abs(pnl)} 达到限制 ${self.max_single_loss}"
        
        if triggered:
            self._trigger(reason)
        
        # 记录日志
        trade_type = "盈利" if pnl >= 0 else "亏损"
        logger.info(
            f"交易记录 | {trade_type}: ${abs(pnl)} | "
            f"日累计: ${self._state.daily_loss} | "
            f"连亏: {self._state.consecutive_losses}次"
        )
        
        return triggered
    
    def _trigger(self, reason: str) -> None:
        """触发熔断器"""
        import asyncio
        
        self._state.is_active = True
        self._state.triggered_at = datetime.now(timezone.utc)
        self._state.reason = reason
        
        logger.warning(f"⚠️ 熔断器触发 | 原因: {reason}")
        
        # 调用所有回调（异常隔离，防止回调失败影响熔断逻辑）
        for callback in self._trigger_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    # 在后台运行，但包裹异常处理
                    async def _safe_callback(cb=callback, r=reason):
                        try:
                            await cb(r)
                        except Exception as e:
                            logger.error(f"熔断回调异步执行失败: {e}")
                    asyncio.create_task(_safe_callback())
                else:
                    callback(reason)
            except Exception as e:
                logger.error(f"熔断回调执行失败: {e}")
    
    def reset(self) -> None:
        """重置熔断器"""
        self._state.is_active = False
        self._state.triggered_at = None
        self._state.reason = ""
        self._state.consecutive_losses = 0
        
        logger.info("熔断器已重置")
    
    def _check_date_reset(self) -> None:
        """检查日期变化，重置日统计"""
        today = date.today()
        if today != self._daily_stats.date:
            logger.info(
                f"日期变化，重置日统计 | "
                f"昨日盈亏: ${self._daily_stats.total_pnl} | "
                f"交易次数: {self._daily_stats.total_trades}"
            )
            
            # 重置日统计
            self._daily_stats = DailyStats(date=today)
            
            # 重置日损失计数
            self._state.daily_loss = Decimal("0")
            self._state.current_date = today
            
            # 如果熔断器是当天触发的，保持激活
            # 如果是昨天触发的，可以重置
            if self._state.is_active and self._state.triggered_at:
                trigger_date = self._state.triggered_at.date()
                if trigger_date < today:
                    self.reset()
    
    @property
    def is_active(self) -> bool:
        """熔断器是否激活"""
        return self._state.is_active
    
    @property
    def daily_stats(self) -> DailyStats:
        """获取日统计"""
        self._check_date_reset()
        return self._daily_stats
    
    def get_status(self) -> dict:
        """获取熔断器状态"""
        return {
            "is_active": self._state.is_active,
            "triggered_at": self._state.triggered_at.isoformat() if self._state.triggered_at else None,
            "reason": self._state.reason,
            "daily_loss": float(self._state.daily_loss),
            "consecutive_losses": self._state.consecutive_losses,
            "daily_trades": self._daily_stats.total_trades,
            "daily_pnl": float(self._daily_stats.total_pnl),
            "win_rate": float(self._daily_stats.win_rate),
        }
