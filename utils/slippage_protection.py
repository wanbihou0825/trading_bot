"""
Slippage/价格偏差保护模块
========================
下单前检查最新价格，偏差超过阈值则取消或调整。

改进:
- 收紧默认滑点 (2% -> 1%)
- 动态滑点调整 (根据流动性)
- 订单簿深度检查
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
    effective_slippage: Decimal = Decimal("0.01")  # 实际使用的滑点阈值


class SlippageProtection:
    """
    Slippage保护器
    
    下单前再次检查最新价格，防止价格滑点导致的损失。
    支持根据流动性动态调整滑点阈值。
    """
    
    def __init__(
        self,
        client,  # PolymarketClient
        max_slippage: Decimal = Decimal("0.01"),  # 1% - 收紧默认值
        max_price_deviation: Decimal = Decimal("0.03"),  # 3% - 收紧
        min_liquidity: Decimal = Decimal("10000"),  # 最小流动性 $10k
        retry_count: int = 3,
        retry_delay: float = 0.5,
        dynamic_slippage: bool = True,  # 启用动态滑点
    ):
        """
        初始化Slippage保护器
        
        Args:
            client: Polymarket客户端
            max_slippage: 最大可接受滑点 (收紧到1%)
            max_price_deviation: 最大价格偏差 (收紧到3%)
            min_liquidity: 最小流动性阈值
            retry_count: 重试次数
            retry_delay: 重试延迟
            dynamic_slippage: 是否启用动态滑点调整
        """
        self.client = client
        self.max_slippage = max_slippage
        self.max_price_deviation = max_price_deviation
        self.min_liquidity = min_liquidity
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.dynamic_slippage = dynamic_slippage
        
        self._price_cache: Dict[str, Tuple[Decimal, datetime]] = {}
        
        logger.info(
            f"Slippage保护初始化 | 最大滑点: {max_slippage*100}% | "
            f"最大偏差: {max_price_deviation*100}% | "
            f"动态调整: {'启用' if dynamic_slippage else '禁用'}"
        )
    
    async def check_price_before_order(
        self,
        market_id: str,
        side: str,
        expected_price: Decimal,
        size: Decimal,
        market_info: Optional[Dict[str, Any]] = None,
    ) -> PriceCheck:
        """
        下单前检查价格 (含流动性感知)
        
        Args:
            market_id: 市场ID
            side: 方向
            expected_price: 预期价格
            size: 数量
            market_info: 市场信息 (含流动性)
        
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
        
        # 动态调整滑点阈值 (关键改进!)
        effective_slippage = self.max_slippage
        effective_deviation = self.max_price_deviation
        
        if self.dynamic_slippage and market_info:
            effective_slippage, effective_deviation = self._calculate_dynamic_thresholds(
                market_info=market_info,
                size=size,
            )
        
        # 计算偏差
        if expected_price > 0:
            deviation = abs(actual_price - expected_price) / expected_price
        else:
            deviation = Decimal("0")
        
        # 判断是否可接受
        is_acceptable = deviation <= effective_deviation
        
        # 决定动作
        adjusted_price = None
        
        if is_acceptable:
            action = "proceed"
            reason = f"价格偏差 {float(deviation)*100:.2f}% 在可接受范围内"
        else:
            # 检查是否可以调整
            if deviation <= effective_slippage * 2:
                # 稍微调整价格
                action = "adjust"
                # 根据方向调整
                if side == "YES":
                    adjusted_price = actual_price * (1 + effective_slippage / 2)
                else:
                    adjusted_price = actual_price * (1 - effective_slippage / 2)
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
            adjusted_price=adjusted_price,
            reason=reason,
            effective_slippage=effective_slippage,
        )
        
        logger.info(
            f"价格检查 | 市场: {market_id} | 预期: {expected_price} | "
            f"实际: {actual_price} | 偏差: {float(deviation)*100:.2f}% | "
            f"有效滑点: {float(effective_slippage)*100:.1f}% | "
            f"动作: {action}"
        )
        
        return result
    
    def _calculate_dynamic_thresholds(
        self,
        market_info: Dict[str, Any],
        size: Decimal,
    ) -> Tuple[Decimal, Decimal]:
        """
        根据流动性动态计算滑点阈值
        
        规则:
        - 高流动性 (> $100k): 使用默认滑点 (1%)
        - 中流动性 ($10k - $100k): 滑点 +50% (1.5%)
        - 低流动性 (< $10k): 滑点 +100% (2%) 或警告
        
        Args:
            market_info: 市场信息
            size: 订单大小
        
        Returns:
            (effective_slippage, effective_deviation)
        """
        liquidity = Decimal(str(market_info.get("liquidity", 0)))
        
        # 根据流动性调整
        if liquidity >= Decimal("100000"):
            # 高流动性: 使用默认值
            multiplier = Decimal("1.0")
        elif liquidity >= Decimal("50000"):
            # 中高流动性: +25%
            multiplier = Decimal("1.25")
        elif liquidity >= Decimal("10000"):
            # 中流动性: +50%
            multiplier = Decimal("1.5")
        else:
            # 低流动性: +100%
            multiplier = Decimal("2.0")
            logger.warning(f"低流动性市场: ${liquidity}，滑点扩大到 {float(self.max_slippage * multiplier)*100:.1f}%")
        
        # 根据订单大小相对流动性调整
        if liquidity > 0:
            size_ratio = size / liquidity
            if size_ratio > Decimal("0.01"):  # 订单超过流动性的1%
                # 大单，进一步放宽
                multiplier *= Decimal("1.2")
                logger.debug(f"大单警告: 订单占流动性 {float(size_ratio)*100:.1f}%")
        
        effective_slippage = self.max_slippage * multiplier
        effective_deviation = self.max_price_deviation * multiplier
        
        # 上限保护
        effective_slippage = min(effective_slippage, Decimal("0.05"))  # 最大 5%
        effective_deviation = min(effective_deviation, Decimal("0.10"))  # 最大 10%
        
        return effective_slippage, effective_deviation
    
    async def _get_latest_price(self, market_id: str, side: str) -> Optional[Decimal]:
        """获取最新价格（带重试）"""
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
    
    async def check_liquidity_depth(
        self,
        market_id: str,
        side: str,
        size: Decimal,
    ) -> Tuple[bool, str]:
        """
        检查订单簿深度是否足够
        
        Args:
            market_id: 市场ID
            side: 方向
            size: 订单大小
        
        Returns:
            (是否足够, 原因)
        """
        try:
            liquidity_info = await self.client.check_liquidity(market_id, side, size)
            
            if not liquidity_info.get("sufficient", False):
                available = liquidity_info.get("available_size", Decimal("0"))
                return False, f"流动性不足: 需要 {size}, 可用 {available}"
            
            return True, "流动性充足"
            
        except Exception as e:
            logger.error(f"检查流动性异常: {e}")
            return False, f"检查失败: {e}"
    
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
        
        # 1. 价格检查
        price_check = await self.slippage_protection.check_price_before_order(
            market_id=market_id,
            side=side,
            expected_price=price,
            size=size,
            market_info=market_info
        )
        
        if price_check.action == "cancel":
            return False, price_check.reason, None
        
        if price_check.action == "adjust":
            adjustments["price"] = price_check.adjusted_price
            price = price_check.adjusted_price
        
        # 2. 流动性检查
        if market_info:
            liquidity = Decimal(str(market_info.get("liquidity", 0)))
            if liquidity < self.min_liquidity:
                return False, f"流动性不足: ${liquidity} < ${self.min_liquidity}", None
            
            # 检查订单簿深度
            sufficient, reason = await self.slippage_protection.check_liquidity_depth(
                market_id, side, size
            )
            if not sufficient:
                return False, reason, None
            
            # 检查成交量
            volume = Decimal(str(market_info.get("volume_24h", 0)))
            if volume < self.min_volume:
                return False, f"成交量不足: ${volume} < ${self.min_volume}", None
            
            # 检查市场是否活跃
            if not market_info.get("active", True):
                return False, "市场不活跃", None
        
        # 3. 余额检查
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
