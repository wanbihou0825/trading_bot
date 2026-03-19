"""
Slippage/价格偏差保护模块
========================
下单前检查最新价格，偏差超过阈值则取消或调整。
"""

import asyncio
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PriceCheck:
    """价格检查结果"""
    market_id: str
    expected_price: Decimal
    actual_price: Decimal
    deviation: Decimal  # 偏差百分比
    is_acceptable: bool
    action: str  # "proceed", "adjust", "cancel"
    adjusted_price: Optional[Decimal] = None
    reason: str = ""


class SlippageProtection:
    """
    Slippage保护器
    
    下单前再次检查最新价格，防止价格滑点导致的损失。
    """
    
    def __init__(
        self,
        client,  # PolymarketClient
        max_slippage: Decimal = Decimal("0.02"),  # 2%
        max_price_deviation: Decimal = Decimal("0.05"),  # 5%
        retry_count: int = 3,
        retry_delay: float = 0.5,
    ):
        """
        初始化Slippage保护器
        
        Args:
            client: Polymarket客户端
            max_slippage: 最大可接受滑点
            max_price_deviation: 最大价格偏差
            retry_count: 重试次数
            retry_delay: 重试延迟
        """
        self.client = client
        self.max_slippage = max_slippage
        self.max_price_deviation = max_price_deviation
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        
        self._price_cache: Dict[str, Tuple[Decimal, datetime]] = {}
        
        logger.info(
            f"Slippage保护初始化 | 最大滑点: {max_slippage*100}% | "
            f"最大偏差: {max_price_deviation*100}%"
        )
    
    async def check_price_before_order(
        self,
        market_id: str,
        side: str,
        expected_price: Decimal,
        size: Decimal,
    ) -> PriceCheck:
        """
        下单前检查价格
        
        Args:
            market_id: 市场ID
            side: 方向
            expected_price: 预期价格
            size: 数量
        
        Returns:
            价格检查结果
        """
        # 获取最新价格
        actual_price = await self._get_latest_price(market_id, side)
        
        if actual_price is None:
            return PriceCheck(
                market_id=market_id,
                expected_price=expected_price,
                actual_price=expected_price,
                deviation=Decimal("0"),
                is_acceptable=False,
                action="cancel",
                reason="无法获取最新价格"
            )
        
        # 计算偏差
        if expected_price > 0:
            deviation = abs(actual_price - expected_price) / expected_price
        else:
            deviation = Decimal("0")
        
        # 判断是否可接受
        is_acceptable = deviation <= self.max_price_deviation
        
        # 决定动作
        if is_acceptable:
            action = "proceed"
            reason = f"价格偏差 {float(deviation)*100:.2f}% 在可接受范围内"
        else:
            # 检查是否可以调整
            if deviation <= self.max_slippage * 2:
                # 稍微调整价格
                action = "adjust"
                # 根据方向调整
                if side == "YES":
                    adjusted_price = actual_price * (1 + self.max_slippage / 2)
                else:
                    adjusted_price = actual_price * (1 - self.max_slippage / 2)
                reason = f"价格偏差 {float(deviation)*100:.2f}% 超出阈值，已调整"
            else:
                action = "cancel"
                reason = f"价格偏差 {float(deviation)*100:.2f}% 过大，取消订单"
        
        result = PriceCheck(
            market_id=market_id,
            expected_price=expected_price,
            actual_price=actual_price,
            deviation=deviation,
            is_acceptable=is_acceptable,
            action=action,
            adjusted_price=adjusted_price if action == "adjust" else None,
            reason=reason
        )
        
        logger.info(
            f"价格检查 | 市场: {market_id} | 预期: {expected_price} | "
            f"实际: {actual_price} | 偏差: {float(deviation)*100:.2f}% | "
            f"动作: {action}"
        )
        
        return result
    
    async def _get_latest_price(self, market_id: str, side: str) -> Optional[Decimal]:
        """
        获取最新价格（带重试）
        
        Args:
            market_id: 市场ID
            side: 方向
        
        Returns:
            最新价格
        """
        for attempt in range(self.retry_count):
            try:
                price_data = await self.client.get_market_price(market_id)
                
                if price_data:
                    price_key = "yes" if side == "YES" else "no"
                    return Decimal(str(price_data.get(price_key, 0)))
                
            except Exception as e:
                logger.warning(f"获取价格失败 (尝试 {attempt+1}/{self.retry_count}): {e}")
                
                if attempt < self.retry_count - 1:
                    await asyncio.sleep(self.retry_delay)
        
        logger.error(f"获取最新价格失败: {market_id}")
        return None
    
    def update_cache(self, market_id: str, price: Decimal) -> None:
        """更新价格缓存"""
        self._price_cache[market_id] = (price, datetime.now(timezone.utc))
    
    def get_cached_price(self, market_id: str, max_age_seconds: float = 60.0) -> Optional[Decimal]:
        """获取缓存价格"""
        if market_id not in self._price_cache:
            return None
        
        price, timestamp = self._price_cache[market_id]
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        
        if age > max_age_seconds:
            return None
        
        return price


class OrderValidator:
    """
    订单验证器
    
    下单前进行全面验证。
    """
    
    def __init__(
        self,
        client,
        risk_manager,
        slippage_protection: SlippageProtection,
        min_liquidity: Decimal = Decimal("10000"),
        min_volume: Decimal = Decimal("1000"),
    ):
        """
        初始化订单验证器
        
        Args:
            client: Polymarket客户端
            risk_manager: 风险管理器
            slippage_protection: Slippage保护器
            min_liquidity: 最小流动性
            min_volume: 最小成交量
        """
        self.client = client
        self.risk_manager = risk_manager
        self.slippage_protection = slippage_protection
        self.min_liquidity = min_liquidity
        self.min_volume = min_volume
    
    async def validate_order(
        self,
        market_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        market_info: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        验证订单
        
        Args:
            market_id: 市场ID
            side: 方向
            size: 数量
            price: 价格
            market_info: 市场信息
        
        Returns:
            (是否通过, 原因, 调整后的参数)
        """
        adjustments = {}
        
        # 1. 风险检查
        # (由RiskManager处理)
        
        # 2. 价格检查
        price_check = await self.slippage_protection.check_price_before_order(
            market_id=market_id,
            side=side,
            expected_price=price,
            size=size
        )
        
        if price_check.action == "cancel":
            return False, price_check.reason, None
        
        if price_check.action == "adjust":
            adjustments["price"] = price_check.adjusted_price
            price = price_check.adjusted_price
        
        # 3. 市场状态检查
        if market_info:
            # 检查流动性
            liquidity = Decimal(str(market_info.get("liquidity", 0)))
            if liquidity < self.min_liquidity:
                return False, f"流动性不足: ${liquidity} < ${self.min_liquidity}", None
            
            # 检查成交量
            volume = Decimal(str(market_info.get("volume_24h", 0)))
            if volume < self.min_volume:
                return False, f"成交量不足: ${volume} < ${self.min_volume}", None
            
            # 检查市场是否活跃
            if not market_info.get("active", True):
                return False, "市场不活跃", None
        
        # 4. 余额检查
        balance = self.risk_manager._account_balance
        required = size * price
        
        if required > balance:
            # 调整大小
            adjusted_size = balance / price
            if adjusted_size < Decimal("1"):
                return False, f"余额不足: 需要 ${required}，可用 ${balance}", None
            
            adjustments["size"] = adjusted_size
            logger.info(f"调整订单大小: {size} -> {adjusted_size}")
        
        return True, "验证通过", adjustments if adjustments else None


class PositionMonitor:
    """
    持仓监控器
    
    监控持仓的价格变化，触发止损/止盈。
    """
    
    def __init__(
        self,
        client,
        risk_manager,
        check_interval: float = 30.0,
        price_change_alert_threshold: Decimal = Decimal("0.10"),  # 10%
    ):
        """
        初始化持仓监控器
        
        Args:
            client: Polymarket客户端
            risk_manager: 风险管理器
            check_interval: 检查间隔
            price_change_alert_threshold: 价格变化告警阈值
        """
        self.client = client
        self.risk_manager = risk_manager
        self.check_interval = check_interval
        self.price_change_alert_threshold = price_change_alert_threshold
        
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._entry_prices: Dict[str, Decimal] = {}  # 记录入场价
    
    def record_entry_price(self, market_id: str, price: Decimal) -> None:
        """记录入场价格"""
        self._entry_prices[market_id] = price
    
    async def start(self) -> None:
        """启动监控"""
        if self._running:
            return
        
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        
        logger.info("持仓监控已启动")
    
    async def stop(self) -> None:
        """停止监控"""
        self._running = False
        
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        
        logger.info("持仓监控已停止")
    
    async def _monitor_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                positions = self.risk_manager.get_positions()
                
                for market_id, position in positions.items():
                    # 获取当前价格
                    price_data = await self.client.get_market_price(market_id)
                    
                    if price_data:
                        current_price = Decimal(str(price_data.get(
                            "yes" if position.side == "YES" else "no",
                            position.current_price
                        )))
                        
                        # 更新持仓价格
                        position.current_price = current_price
                        
                        # 检查价格变化
                        if market_id in self._entry_prices:
                            entry_price = self._entry_prices[market_id]
                            change = abs(current_price - entry_price) / entry_price
                            
                            if change >= self.price_change_alert_threshold:
                                logger.warning(
                                    f"价格大幅变动 | 市场: {position.market_question[:30]}... | "
                                    f"入场: {entry_price} | 当前: {current_price} | "
                                    f"变化: {float(change)*100:.1f}%"
                                )
                
                await asyncio.sleep(self.check_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"持仓监控异常: {e}")
                await asyncio.sleep(self.check_interval)
