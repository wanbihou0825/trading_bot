"""
策略基类模块
============
定义所有交易策略的基类和通用接口。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional, Dict, Any
from datetime import datetime, timezone


class StrategyType(Enum):
    """策略类型"""
    ENDGAME_SWEEPER = "endgame_sweeper"      # 高概率收割策略
    DIRECTIONAL_MOMENTUM = "momentum"         # 动量追踪策略
    MEAN_REVERSION = "mean_reversion"         # 均值回归策略
    MARKET_MAKING = "market_making"           # 做市策略
    ARBITRAGE = "arbitrage"                   # 套利策略


class SignalType(Enum):
    """信号类型"""
    BUY_YES = "buy_yes"      # 买入YES
    BUY_NO = "buy_no"        # 买入NO
    SELL_YES = "sell_yes"    # 卖出YES
    SELL_NO = "sell_no"      # 卖出NO
    HOLD = "hold"            # 持有/不操作


@dataclass
class MarketData:
    """市场数据"""
    market_id: str
    question: str
    yes_price: Decimal
    no_price: Decimal
    volume_24h: Decimal
    liquidity: Decimal
    probability: Optional[Decimal] = None       # 市场隐含概率
    days_to_resolution: Optional[int] = None    # 距离结算天数
    
    @property
    def spread(self) -> Decimal:
        """价差"""
        return self.yes_price + self.no_price - Decimal("1")


@dataclass
class StrategyResult:
    """策略分析结果"""
    signal: SignalType
    confidence: Decimal                    # 置信度 0-1
    reason: str                            # 分析原因
    strategy_type: StrategyType
    market_id: str
    suggested_size: Optional[Decimal] = None
    stop_loss_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
    
    @property
    def should_trade(self) -> bool:
        """是否应该执行交易"""
        return self.signal != SignalType.HOLD


@dataclass
class WalletProfile:
    """钱包画像"""
    address: str
    wallet_type: str                        # "market_maker", "directional", "arbitrageur"
    win_rate: Decimal
    total_trades: int
    total_pnl: Decimal
    avg_position_size: Decimal
    preferred_markets: list                 # 偏好的市场类型
    last_active: datetime
    
    @property
    def quality_score(self) -> Decimal:
        """钱包质量评分"""
        # 综合评分：胜率 + 盈利 + 活跃度
        if self.total_trades == 0:
            return Decimal("0")
        
        score = (
            self.win_rate * Decimal("0.4") +
            min(self.total_pnl / Decimal("1000"), Decimal("1")) * Decimal("0.4") +
            min(Decimal(self.total_trades) / Decimal("100"), Decimal("1")) * Decimal("0.2")
        )
        return score.quantize(Decimal("0.01"))


class BaseStrategy(ABC):
    """
    策略基类
    
    所有交易策略都应继承此类并实现analyze方法。
    """
    
    def __init__(
        self,
        min_confidence: Decimal = Decimal("0.7"),
        name: str = "base_strategy"
    ):
        """
        初始化策略
        
        Args:
            min_confidence: 最小置信度阈值
            name: 策略名称
        """
        self.min_confidence = min_confidence
        self.name = name
        self._last_analysis_time: Optional[datetime] = None
    
    @abstractmethod
    def analyze(self, market_data: MarketData) -> StrategyResult:
        """
        分析市场数据，生成交易信号
        
        Args:
            market_data: 市场数据
        
        Returns:
            策略分析结果
        """
        pass
    
    @abstractmethod
    def get_strategy_type(self) -> StrategyType:
        """获取策略类型"""
        pass
    
    def validate_signal(self, result: StrategyResult) -> StrategyResult:
        """
        验证信号是否符合条件
        
        Args:
            result: 策略结果
        
        Returns:
            验证后的结果（可能变为HOLD）
        """
        # 置信度不足则不交易
        if result.confidence < self.min_confidence:
            return StrategyResult(
                signal=SignalType.HOLD,
                confidence=result.confidence,
                reason=f"置信度 {result.confidence:.2f} 低于阈值 {self.min_confidence}",
                strategy_type=result.strategy_type,
                market_id=result.market_id
            )
        
        return result
    
    def _create_result(
        self,
        signal: SignalType,
        confidence: Decimal,
        reason: str,
        market_data: MarketData,
        **kwargs
    ) -> StrategyResult:
        """
        创建策略结果的便捷方法
        
        Args:
            signal: 信号类型
            confidence: 置信度
            reason: 原因
            market_data: 市场数据
            **kwargs: 其他参数
        
        Returns:
            策略结果
        """
        return StrategyResult(
            signal=signal,
            confidence=confidence,
            reason=reason,
            strategy_type=self.get_strategy_type(),
            market_id=market_data.market_id,
            **kwargs
        )
    
    @property
    def last_analysis_time(self) -> Optional[datetime]:
        """获取上次分析时间"""
        return self._last_analysis_time
    
    def update_analysis_time(self) -> None:
        """更新分析时间"""
        self._last_analysis_time = datetime.now(timezone.utc)
