"""
自适应策略管理器
================
根据钱包类型和市场状态自动选择最优策略。
"""

from decimal import Decimal
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime, timezone

from utils.logger import get_logger
from .base import (
    BaseStrategy,
    StrategyType,
    StrategyResult,
    SignalType,
    MarketData,
    WalletProfile
)
from .endgame import EndgameSweeperStrategy

logger = get_logger(__name__)


@dataclass
class StrategyPerformance:
    """策略表现"""
    strategy_type: StrategyType
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: Decimal = Decimal("0")
    avg_confidence: Decimal = Decimal("0")
    last_used: Optional[datetime] = None
    
    @property
    def win_rate(self) -> Decimal:
        if self.total_trades == 0:
            return Decimal("0")
        return Decimal(self.winning_trades) / Decimal(self.total_trades)


class AdaptiveStrategyManager:
    """
    自适应策略管理器
    
    功能:
    1. 根据钱包类型匹配策略
    2. 根据市场状态调整策略
    3. 跟踪策略表现
    4. 动态选择最优策略
    """
    
    # 钱包类型到策略的映射
    WALLET_STRATEGY_MAP = {
        "market_maker": [StrategyType.MARKET_MAKING],
        "directional": [StrategyType.DIRECTIONAL_MOMENTUM, StrategyType.MEAN_REVERSION],
        "arbitrageur": [StrategyType.ARBITRAGE],
    }
    
    # 市场状态到策略的映射
    MARKET_STATE_STRATEGY_MAP = {
        "endgame": StrategyType.ENDGAME_SWEEPER,        # 接近结算
        "trending": StrategyType.DIRECTIONAL_MOMENTUM,  # 趋势市场
        "ranging": StrategyType.MEAN_REVERSION,         # 震荡市场
        "high_spread": StrategyType.MARKET_MAKING,      # 高价差市场
    }
    
    def __init__(
        self,
        min_confidence: Decimal = Decimal("0.7")
    ):
        """
        初始化策略管理器
        
        Args:
            min_confidence: 最小置信度阈值
        """
        self.min_confidence = min_confidence
        
        # 初始化策略实例
        self._strategies: Dict[StrategyType, BaseStrategy] = {}
        self._register_strategy(EndgameSweeperStrategy(min_confidence=min_confidence))
        
        # 策略表现跟踪
        self._performance: Dict[StrategyType, StrategyPerformance] = {
            st: StrategyPerformance(strategy_type=st)
            for st in StrategyType
        }
        
        logger.info(f"自适应策略管理器初始化，已注册 {len(self._strategies)} 个策略")
    
    def _register_strategy(self, strategy: BaseStrategy) -> None:
        """注册策略"""
        self._strategies[strategy.get_strategy_type()] = strategy
        logger.debug(f"注册策略: {strategy.name}")
    
    def get_strategy(self, strategy_type: StrategyType) -> Optional[BaseStrategy]:
        """获取策略实例"""
        return self._strategies.get(strategy_type)
    
    def analyze_market(
        self,
        market_data: MarketData,
        wallet_profile: Optional[WalletProfile] = None
    ) -> StrategyResult:
        """
        分析市场并选择最优策略
        
        Args:
            market_data: 市场数据
            wallet_profile: 钱包画像（可选）
        
        Returns:
            最优策略的分析结果
        """
        # 1. 检测市场状态
        market_state = self._detect_market_state(market_data)
        
        # 2. 确定候选策略
        candidate_strategies = self._get_candidate_strategies(market_state, wallet_profile)
        
        # 3. 运行所有候选策略并收集结果
        results: List[tuple[StrategyType, StrategyResult]] = []
        
        for strategy_type in candidate_strategies:
            strategy = self._strategies.get(strategy_type)
            if strategy is None:
                continue
            
            result = strategy.analyze(market_data)
            if result.should_trade:
                results.append((strategy_type, result))
        
        # 4. 选择最佳结果
        if not results:
            return StrategyResult(
                signal=SignalType.HOLD,
                confidence=Decimal("0"),
                reason="没有策略产生交易信号",
                strategy_type=StrategyType.ENDGAME_SWEEPER,
                market_id=market_data.market_id
            )
        
        # 按置信度排序
        results.sort(key=lambda x: x[1].confidence, reverse=True)
        best_strategy_type, best_result = results[0]
        
        logger.info(
            f"策略选择 | 市场: {market_data.question[:30]}... | "
            f"状态: {market_state} | 选择策略: {best_strategy_type.value} | "
            f"置信度: {best_result.confidence:.2f}"
        )
        
        return best_result
    
    def _detect_market_state(self, market_data: MarketData) -> str:
        """
        检测市场状态
        
        Args:
            market_data: 市场数据
        
        Returns:
            市场状态字符串
        """
        # 接近结算 -> Endgame
        if (market_data.days_to_resolution is not None and 
            market_data.days_to_resolution <= 7):
            return "endgame"
        
        # 高价差 -> 做市机会
        if market_data.spread > Decimal("0.02"):
            return "high_spread"
        
        # 默认趋势市场
        return "trending"
    
    def _get_candidate_strategies(
        self,
        market_state: str,
        wallet_profile: Optional[WalletProfile] = None
    ) -> List[StrategyType]:
        """
        获取候选策略列表
        
        Args:
            market_state: 市场状态
            wallet_profile: 钱包画像
        
        Returns:
            候选策略类型列表
        """
        candidates = []
        
        # 1. 添加市场状态对应的策略
        if market_state in self.MARKET_STATE_STRATEGY_MAP:
            candidates.append(self.MARKET_STATE_STRATEGY_MAP[market_state])
        
        # 2. 添加钱包类型对应的策略
        if wallet_profile:
            wallet_strategies = self.WALLET_STRATEGY_MAP.get(
                wallet_profile.wallet_type, []
            )
            candidates.extend(wallet_strategies)
        
        # 3. 去重
        candidates = list(dict.fromkeys(candidates))
        
        # 4. 只返回已注册的策略
        return [s for s in candidates if s in self._strategies]
    
    def record_trade_result(
        self,
        strategy_type: StrategyType,
        pnl: Decimal,
        confidence: Decimal
    ) -> None:
        """
        记录交易结果用于策略表现跟踪
        
        Args:
            strategy_type: 策略类型
            pnl: 盈亏
            confidence: 当时的置信度
        """
        if strategy_type not in self._performance:
            return
        
        perf = self._performance[strategy_type]
        perf.total_trades += 1
        perf.total_pnl += pnl
        perf.last_used = datetime.now(timezone.utc)
        
        if pnl > 0:
            perf.winning_trades += 1
        
        # 更新平均置信度
        if perf.total_trades > 0:
            perf.avg_confidence = (
                (perf.avg_confidence * (perf.total_trades - 1) + confidence) /
                perf.total_trades
            )
    
    def get_best_performing_strategy(self) -> Optional[StrategyType]:
        """
        获取表现最好的策略
        
        Returns:
            表现最好的策略类型
        """
        valid_perfs = [
            (st, p) for st, p in self._performance.items()
            if p.total_trades > 0
        ]
        
        if not valid_perfs:
            return None
        
        # 按胜率和盈利综合排序
        valid_perfs.sort(
            key=lambda x: (x[1].win_rate, x[1].total_pnl),
            reverse=True
        )
        
        return valid_perfs[0][0]
    
    def get_strategy_stats(self) -> Dict[str, dict]:
        """获取所有策略的统计信息"""
        return {
            st.value: {
                "total_trades": p.total_trades,
                "win_rate": float(p.win_rate),
                "total_pnl": float(p.total_pnl),
                "avg_confidence": float(p.avg_confidence),
            }
            for st, p in self._performance.items()
            if p.total_trades > 0
        }
