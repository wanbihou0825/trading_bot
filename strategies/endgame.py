"""
Endgame Sweeper 策略模块
=======================
高概率收割策略，专注于接近结算的高概率市场。
"""

from decimal import Decimal
from typing import Optional
from datetime import datetime, timezone, timedelta

from utils.logger import get_logger
from utils.financial import calculate_annualized_return
from .base import (
    BaseStrategy,
    StrategyType,
    StrategyResult,
    SignalType,
    MarketData
)

logger = get_logger(__name__)


class EndgameSweeperStrategy(BaseStrategy):
    """
    Endgame Sweeper 策略
    
    核心逻辑:
    1. 寻找高概率 (>95%) 的市场
    2. 接近结算 (≤7天)
    3. 流动性充足 (≥$10,000 日流动性)
    4. 年化收益合理 (≥20%)
    
    入场条件:
    - 概率 >= min_probability
    - 距离结算 <= max_days_to_resolution
    - 日流动性 >= min_daily_liquidity
    - 年化收益 >= min_annualized_return
    
    出场条件:
    - 概率达到 black_swan_probability (99.8%) - 黑天鹅保护
    - 价格反向移动 >= stop_loss_pct
    - 市场已结算
    """
    
    def __init__(
        self,
        min_probability: Decimal = Decimal("0.95"),
        max_days_to_resolution: int = 7,
        min_daily_liquidity: Decimal = Decimal("10000"),
        min_annualized_return: Decimal = Decimal("0.20"),
        black_swan_probability: Decimal = Decimal("0.998"),
        stop_loss_pct: Decimal = Decimal("0.10"),
        min_confidence: Decimal = Decimal("0.7")
    ):
        """
        初始化 Endgame Sweeper 策略
        
        Args:
            min_probability: 最小入场概率 (默认95%)
            max_days_to_resolution: 最大距离结算天数 (默认7天)
            min_daily_liquidity: 最小日流动性 (默认$10,000)
            min_annualized_return: 最小年化收益 (默认20%)
            black_swan_probability: 黑天鹅保护概率 (默认99.8%)
            stop_loss_pct: 止损比例 (默认10%)
            min_confidence: 最小置信度阈值
        """
        super().__init__(min_confidence=min_confidence, name="EndgameSweeper")
        
        self.min_probability = min_probability
        self.max_days_to_resolution = max_days_to_resolution
        self.min_daily_liquidity = min_daily_liquidity
        self.min_annualized_return = min_annualized_return
        self.black_swan_probability = black_swan_probability
        self.stop_loss_pct = stop_loss_pct
        
        logger.info(
            f"Endgame Sweeper 初始化 | "
            f"最小概率: {min_probability*100}% | "
            f"最大天数: {max_days_to_resolution}天 | "
            f"最小流动性: ${min_daily_liquidity}"
        )
    
    def get_strategy_type(self) -> StrategyType:
        return StrategyType.ENDGAME_SWEEPER
    
    def analyze(self, market_data: MarketData) -> StrategyResult:
        """
        分析市场数据
        
        Args:
            market_data: 市场数据
        
        Returns:
            策略分析结果
        """
        self.update_analysis_time()
        
        # 1. 检查基本条件
        checks = self._check_entry_conditions(market_data)
        
        # 如果条件不满足，返回HOLD
        if not checks["passed"]:
            return self._create_result(
                signal=SignalType.HOLD,
                confidence=Decimal("0"),
                reason=checks["reason"],
                market_data=market_data
            )
        
        # 2. 确定交易方向
        signal = self._determine_signal(market_data)
        
        # 3. 计算置信度
        confidence = self._calculate_confidence(market_data)
        
        # 4. 创建结果
        result = self._create_result(
            signal=signal,
            confidence=confidence,
            reason=checks["reason"],
            market_data=market_data,
            metadata={
                "days_to_resolution": market_data.days_to_resolution,
                "annualized_return": checks["annualized_return"],
                "probability": checks["probability"],
            }
        )
        
        # 5. 验证信号
        return self.validate_signal(result)
    
    def _check_entry_conditions(self, market_data: MarketData) -> dict:
        """检查入场条件"""
        # 检查距离结算天数
        if market_data.days_to_resolution is None:
            return {
                "passed": False,
                "reason": "无法获取距离结算天数"
            }
        
        if market_data.days_to_resolution > self.max_days_to_resolution:
            return {
                "passed": False,
                "reason": f"距离结算 {market_data.days_to_resolution} 天超过限制 {self.max_days_to_resolution} 天"
            }
        
        # 检查流动性
        if market_data.liquidity < self.min_daily_liquidity:
            return {
                "passed": False,
                "reason": f"流动性 ${market_data.liquidity} 低于要求 ${self.min_daily_liquidity}"
            }
        
        # 确定概率和方向
        yes_prob = market_data.yes_price
        no_prob = market_data.no_price
        
        # 选择概率较高的一方
        if yes_prob >= self.min_probability:
            probability = yes_prob
            side = "YES"
        elif no_prob >= self.min_probability:
            probability = no_prob
            side = "NO"
        else:
            return {
                "passed": False,
                "reason": f"概率不足: YES={yes_prob*100:.1f}%, NO={no_prob*100:.1f}% (需要 ≥{self.min_probability*100}%)"
            }
        
        # 计算年化收益
        target_price = Decimal("1.0")  # 完全胜出
        current_price = yes_prob if side == "YES" else no_prob
        
        annualized_return = calculate_annualized_return(
            current_price=current_price,
            target_price=target_price,
            days_to_resolution=market_data.days_to_resolution
        )
        
        if annualized_return < self.min_annualized_return:
            return {
                "passed": False,
                "reason": f"年化收益 {annualized_return*100:.1f}% 低于要求 {self.min_annualized_return*100}%"
            }
        
        return {
            "passed": True,
            "reason": f"符合入场条件: {side} 概率 {probability*100:.1f}%, 年化收益 {annualized_return*100:.1f}%",
            "probability": probability,
            "side": side,
            "annualized_return": annualized_return
        }
    
    def _determine_signal(self, market_data: MarketData) -> SignalType:
        """确定交易信号"""
        yes_prob = market_data.yes_price
        no_prob = market_data.no_price
        
        if yes_prob >= self.min_probability:
            return SignalType.BUY_YES
        elif no_prob >= self.min_probability:
            return SignalType.BUY_NO
        
        return SignalType.HOLD
    
    def _calculate_confidence(self, market_data: MarketData) -> Decimal:
        """
        计算置信度
        
        基于:
        1. 概率越高，置信度越高
        2. 距离结算越近，置信度越高
        3. 流动性越高，置信度越高
        """
        # 基础概率得分 (概率越高越好)
        yes_prob = market_data.yes_price
        no_prob = market_data.no_price
        max_prob = max(yes_prob, no_prob)
        
        prob_score = (max_prob - self.min_probability) / (Decimal("1") - self.min_probability)
        
        # 时间得分 (越近越好)
        if market_data.days_to_resolution:
            time_score = Decimal("1") - (
                Decimal(market_data.days_to_resolution) / Decimal(self.max_days_to_resolution)
            )
            time_score = max(time_score, Decimal("0"))
        else:
            time_score = Decimal("0.5")
        
        # 流动性得分 (越高越好)
        liquidity_score = min(
            market_data.liquidity / self.min_daily_liquidity,
            Decimal("2")
        ) / Decimal("2")
        
        # 综合置信度
        confidence = (
            prob_score * Decimal("0.5") +
            time_score * Decimal("0.3") +
            liquidity_score * Decimal("0.2")
        )
        
        return min(max(confidence, Decimal("0")), Decimal("1"))
    
    def should_exit(
        self,
        market_data: MarketData,
        entry_price: Decimal,
        entry_side: str
    ) -> tuple[bool, str]:
        """
        检查是否应该出场
        
        Args:
            market_data: 当前市场数据
            entry_price: 入场价格
            entry_side: 入场方向
        
        Returns:
            (是否应该出场, 原因)
        """
        current_price = market_data.yes_price if entry_side == "YES" else market_data.no_price
        
        # 1. 黑天鹅保护 - 概率达到99.8%
        if current_price >= self.black_swan_probability:
            return True, f"黑天鹅保护触发: 概率达到 {current_price*100:.1f}%"
        
        # 2. 止损检查 - 价格反向移动10%
        price_change = abs(current_price - entry_price) / entry_price
        if price_change >= self.stop_loss_pct:
            return True, f"止损触发: 价格移动 {price_change*100:.1f}%"
        
        # 3. 市场已结算
        if market_data.days_to_resolution is not None and market_data.days_to_resolution <= 0:
            return True, "市场已结算"
        
        return False, "继续持有"
