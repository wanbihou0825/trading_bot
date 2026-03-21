"""
风险管理模块
============
管理交易风险，包括仓位控制、止损止盈等。
"""

import asyncio
from decimal import Decimal
from typing import Optional, Dict, List, TYPE_CHECKING
from dataclasses import dataclass
from datetime import datetime, timezone

from utils.logger import get_logger
from utils.financial import (
    calculate_position_size,
    calculate_stop_loss_price,
    calculate_take_profit_price
)
from .circuit_breaker import CircuitBreaker

if TYPE_CHECKING:
    from utils.trade_persistence import TradePersistence

logger = get_logger(__name__)


@dataclass
class Position:
    """持仓信息"""
    market_id: str
    market_question: str
    side: str  # "YES" or "NO"
    entry_price: Decimal
    current_price: Decimal
    size: Decimal
    stop_loss_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None
    opened_at: datetime = None
    
    def __post_init__(self):
        if self.opened_at is None:
            self.opened_at = datetime.now(timezone.utc)
    
    @property
    def pnl(self) -> Decimal:
        """当前盈亏"""
        if self.side == "YES":
            return (self.current_price - self.entry_price) * self.size
        else:
            return (self.entry_price - self.current_price) * self.size
    
    @property
    def pnl_percentage(self) -> Decimal:
        """盈亏百分比"""
        if self.entry_price == 0:
            return Decimal("0")
        return self.pnl / (self.entry_price * self.size) * 100


@dataclass
class RiskCheckResult:
    """风险检查结果"""
    allowed: bool
    reason: str
    suggested_size: Optional[Decimal] = None
    warnings: List[str] = None
    
    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


class RiskManager:
    """
    风险管理器
    
    负责:
    1. 仓位大小计算
    2. 风险限制检查
    3. 止损止盈管理
    4. 与熔断器集成
    """
    
    def __init__(
        self,
        circuit_breaker: CircuitBreaker,
        max_position_size: Decimal = Decimal("50"),
        max_position_pct: Decimal = Decimal("0.03"),
        max_concurrent_positions: int = 5,
        default_stop_loss_pct: Decimal = Decimal("0.15"),
        default_take_profit_pct: Decimal = Decimal("0.25"),
        max_slippage: Decimal = Decimal("0.02"),
        max_total_exposure: Decimal = Decimal("200")
    ):
        """
        初始化风险管理器
        
        Args:
            circuit_breaker: 熔断器实例
            max_position_size: 最大仓位大小
            max_position_pct: 最大仓位占余额比例
            max_concurrent_positions: 最大并发仓位
            default_stop_loss_pct: 默认止损比例
            default_take_profit_pct: 默认止盈比例
            max_slippage: 最大滑点
            max_total_exposure: 最大总敞口
        """
        self.circuit_breaker = circuit_breaker
        self.max_position_size = max_position_size
        self.max_position_pct = max_position_pct
        self.max_concurrent_positions = max_concurrent_positions
        self.default_stop_loss_pct = default_stop_loss_pct
        self.default_take_profit_pct = default_take_profit_pct
        self.max_slippage = max_slippage
        self.max_total_exposure = max_total_exposure
        
        # 持仓跟踪
        self._positions: Dict[str, Position] = {}
        self._positions_lock = asyncio.Lock()
        
        # 持久化（可选，由外部注入）
        self._persistence: Optional["TradePersistence"] = None
        
        # 账户余额（需要外部更新）
        self._account_balance = Decimal("0")
        
        logger.info(
            f"风险管理器初始化 | "
            f"最大仓位: ${max_position_size} | "
            f"最大并发: {max_concurrent_positions} | "
            f"总敞口限制: ${max_total_exposure}"
        )
    
    def set_persistence(self, persistence: "TradePersistence") -> None:
        """设置持久化管理器"""
        self._persistence = persistence

    async def restore_positions(self) -> int:
        """从数据库恢复仓位（崩溃恢复）"""
        if not self._persistence:
            return 0
        rows = await self._persistence.load_rm_positions()
        async with self._positions_lock:
            for row in rows:
                pos = Position(
                    market_id=row["market_id"],
                    market_question=row["market_question"],
                    side=row["side"],
                    entry_price=row["entry_price"],
                    current_price=row["current_price"],
                    size=row["size"],
                    stop_loss_price=row.get("stop_loss_price"),
                    take_profit_price=row.get("take_profit_price"),
                    opened_at=datetime.fromisoformat(row["opened_at"]) if isinstance(row["opened_at"], str) else row["opened_at"],
                )
                self._positions[pos.market_id] = pos
        if rows:
            logger.info(f"从数据库恢复了 {len(rows)} 个仓位")
        return len(rows)

    def update_balance(self, balance: Decimal) -> None:
        """更新账户余额"""
        self._account_balance = balance
        logger.debug(f"账户余额更新: ${balance}")
    
    def check_trade(
        self,
        market_id: str,
        side: str,
        requested_size: Decimal,
        confidence_score: Decimal,
        price: Decimal
    ) -> RiskCheckResult:
        """
        检查交易是否允许
        
        Args:
            market_id: 市场ID
            side: 交易方向
            requested_size: 请求的仓位大小
            confidence_score: 置信度评分
            price: 当前价格
        
        Returns:
            风险检查结果
        """
        warnings = []
        
        # 1. 检查熔断器
        can_trade, reason = self.circuit_breaker.check_can_trade()
        if not can_trade:
            return RiskCheckResult(
                allowed=False,
                reason=f"熔断器激活: {reason}"
            )
        
        # 2. 检查并发仓位
        if len(self._positions) >= self.max_concurrent_positions:
            return RiskCheckResult(
                allowed=False,
                reason=f"已达到最大并发仓位限制 ({self.max_concurrent_positions})"
            )
        
        # 3. 检查总敞口
        current_exposure = self.get_total_exposure()
        if current_exposure + requested_size > self.max_total_exposure:
            return RiskCheckResult(
                allowed=False,
                reason=f"总敞口超限 (当前: ${current_exposure:.2f}, "
                       f"请求: ${requested_size}, 限制: ${self.max_total_exposure})"
            )
        
        # 4. 检查是否已有该市场仓位
        if market_id in self._positions:
            return RiskCheckResult(
                allowed=False,
                reason="该市场已有持仓"
            )
        
        # 4. 计算建议仓位大小
        suggested_size = calculate_position_size(
            account_balance=self._account_balance,
            risk_percentage=Decimal("0.02"),  # 单笔风险2%
            confidence_score=confidence_score,
            max_position_size=self.max_position_size,
            max_position_pct=self.max_position_pct
        )
        
        # 5. 如果请求大小超过建议，发出警告
        if requested_size > suggested_size:
            warnings.append(
                f"请求仓位 ${requested_size} 超过建议仓位 ${suggested_size:.2f}"
            )
        
        # 6. 检查账户余额
        required_amount = min(requested_size, suggested_size)
        if required_amount > self._account_balance:
            return RiskCheckResult(
                allowed=False,
                reason=f"账户余额不足 (需要 ${required_amount}, 可用 ${self._account_balance})"
            )
        
        # 7. 确定最终仓位大小
        final_size = min(requested_size, suggested_size)
        
        return RiskCheckResult(
            allowed=True,
            reason="风险检查通过",
            suggested_size=final_size,
            warnings=warnings
        )
    
    async def open_position(
        self,
        market_id: str,
        market_question: str,
        side: str,
        price: Decimal,
        size: Decimal,
        stop_loss_pct: Optional[Decimal] = None,
        take_profit_pct: Optional[Decimal] = None
    ) -> Position:
        """
        开仓
        """
        sl_pct = stop_loss_pct or self.default_stop_loss_pct
        tp_pct = take_profit_pct or self.default_take_profit_pct
        
        stop_loss = calculate_stop_loss_price(price, sl_pct, side)
        take_profit = calculate_take_profit_price(price, tp_pct, side)
        
        position = Position(
            market_id=market_id,
            market_question=market_question,
            side=side,
            entry_price=price,
            current_price=price,
            size=size,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit
        )
        
        async with self._positions_lock:
            self._positions[market_id] = position
        
        # 持久化
        if self._persistence:
            try:
                await self._persistence.save_rm_position({
                    "market_id": market_id,
                    "market_question": market_question,
                    "side": side,
                    "entry_price": price,
                    "current_price": price,
                    "size": size,
                    "stop_loss_price": stop_loss,
                    "take_profit_price": take_profit,
                    "opened_at": position.opened_at.isoformat(),
                })
            except Exception as e:
                logger.error(f"持久化仓位失败: {e}")
        
        logger.info(
            f"开仓成功 | 市场: {market_question[:30]}... | "
            f"方向: {side} | 价格: {price} | "
            f"仓位: ${size} | 止损: {stop_loss} | 止盈: {take_profit}"
        )
        
        return position
    
    async def close_position(
        self,
        market_id: str,
        exit_price: Decimal
    ) -> Optional[Position]:
        """
        平仓
        """
        async with self._positions_lock:
            if market_id not in self._positions:
                logger.warning(f"未找到持仓: {market_id}")
                return None
            position = self._positions.pop(market_id)
        
        position.current_price = exit_price
        
        # 记录交易结果到熔断器
        self.circuit_breaker.record_trade_result(
            pnl=position.pnl,
            volume=position.size
        )
        
        # 从持久化中删除
        if self._persistence:
            try:
                await self._persistence.remove_rm_position(market_id)
            except Exception as e:
                logger.error(f"删除持久化仓位失败: {e}")
        
        pnl_type = "盈利" if position.pnl >= 0 else "亏损"
        logger.info(
            f"平仓成功 | 市场: {position.market_question[:30]}... | "
            f"{pnl_type}: ${abs(position.pnl):.2f} | "
            f"收益率: {position.pnl_percentage:.1f}%"
        )
        
        return position
    
    def check_position_exits(
        self,
        market_prices: Dict[str, Decimal]
    ) -> List[dict]:
        """
        检查持仓是否触发止损/止盈
        
        Args:
            market_prices: 市场ID -> 当前价格映射
        
        Returns:
            需要平仓的列表
        """
        exits = []
        
        for market_id, position in self._positions.items():
            if market_id not in market_prices:
                continue
            
            current_price = market_prices[market_id]
            position.current_price = current_price
            
            # 检查止损
            if position.stop_loss_price:
                if position.side == "YES" and current_price <= position.stop_loss_price:
                    exits.append({
                        "market_id": market_id,
                        "reason": "止损触发",
                        "exit_price": current_price
                    })
                elif position.side == "NO" and current_price >= position.stop_loss_price:
                    exits.append({
                        "market_id": market_id,
                        "reason": "止损触发",
                        "exit_price": current_price
                    })
            
            # 检查止盈
            if position.take_profit_price:
                if position.side == "YES" and current_price >= position.take_profit_price:
                    exits.append({
                        "market_id": market_id,
                        "reason": "止盈触发",
                        "exit_price": current_price
                    })
                elif position.side == "NO" and current_price <= position.take_profit_price:
                    exits.append({
                        "market_id": market_id,
                        "reason": "止盈触发",
                        "exit_price": current_price
                    })
        
        return exits
    
    def get_positions(self) -> Dict[str, Position]:
        """获取所有持仓（返回 snapshot ）"""
        return self._positions.copy()
    
    def get_position(self, market_id: str) -> Optional[Position]:
        """获取指定市场的持仓"""
        return self._positions.get(market_id)
    
    @property
    def total_position_value(self) -> Decimal:
        """总持仓价值"""
        positions = self._positions.copy()
        return sum(p.size * p.current_price for p in positions.values())
    
    @property
    def total_pnl(self) -> Decimal:
        """总盈亏"""
        positions = self._positions.copy()
        return sum(p.pnl for p in positions.values())
    
    def get_total_exposure(self) -> Decimal:
        """获取当前总敞口"""
        positions = self._positions.copy()
        return sum(p.size * p.entry_price for p in positions.values())
    
    def get_status(self) -> dict:
        """获取风险管理器状态"""
        return {
            "account_balance": float(self._account_balance),
            "positions_count": len(self._positions),
            "total_position_value": float(self.total_position_value),
            "total_pnl": float(self.total_pnl),
            "max_position_size": float(self.max_position_size),
            "max_concurrent_positions": self.max_concurrent_positions,
            "circuit_breaker": self.circuit_breaker.get_status(),
        }
