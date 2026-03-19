"""
钱包质量评分器
==============
评估目标钱包的交易质量，区分可持续盈利者和做市商。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from enum import Enum

from utils.logger import get_logger

logger = get_logger(__name__)


class WalletTier(Enum):
    """钱包质量等级"""
    ELITE = "elite"      # 9.0-10.0: 顶级交易者
    EXPERT = "expert"    # 7.0-8.9: 专家级
    GOOD = "good"        # 5.0-6.9: 良好
    POOR = "poor"        # <5.0: 不建议跟单


@dataclass
class TradingStats:
    """交易统计"""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_profit: Decimal = Decimal("0")
    total_loss: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    avg_hold_time_hours: float = 0.0
    categories: Dict[str, int] = field(default_factory=dict)
    first_trade_at: Optional[datetime] = None
    last_trade_at: Optional[datetime] = None
    
    @property
    def win_rate(self) -> Decimal:
        """胜率"""
        if self.total_trades == 0:
            return Decimal("0")
        return Decimal(self.winning_trades) / Decimal(self.total_trades)
    
    @property
    def profit_factor(self) -> Decimal:
        """盈亏比"""
        if self.total_loss == 0:
            return Decimal("999") if self.total_profit > 0 else Decimal("0")
        return self.total_profit / self.total_loss
    
    @property
    def avg_profit_per_trade(self) -> Decimal:
        """平均每笔利润"""
        if self.total_trades == 0:
            return Decimal("0")
        return (self.total_profit - self.total_loss) / self.total_trades
    
    @property
    def trading_days(self) -> int:
        """交易天数"""
        if not self.first_trade_at or not self.last_trade_at:
            return 0
        return (self.last_trade_at - self.first_trade_at).days + 1


@dataclass
class QualityScore:
    """质量评分结果"""
    wallet_address: str
    tier: WalletTier
    overall_score: Decimal
    win_rate_score: Decimal
    profit_factor_score: Decimal
    consistency_score: Decimal
    risk_score: Decimal
    specialty_score: Decimal
    stats: TradingStats
    red_flags: List[str] = field(default_factory=list)
    is_market_maker: bool = False
    recommended_allocation: Decimal = Decimal("0")
    max_position_size: Decimal = Decimal("0")
    
    @property
    def should_follow(self) -> bool:
        """是否应该跟单"""
        return (
            self.tier in [WalletTier.ELITE, WalletTier.EXPERT, WalletTier.GOOD]
            and len(self.red_flags) == 0
            and not self.is_market_maker
        )


class WalletQualityScorer:
    """
    钱包质量评分器
    
    评分维度:
    1. 胜率 (20%)
    2. 盈亏比 (25%)
    3. 稳定性 (20%)
    4. 风险控制 (15%)
    5. 专业领域 (20%)
    """
    
    # 等级阈值
    TIER_THRESHOLDS = {
        WalletTier.ELITE: Decimal("9.0"),
        WalletTier.EXPERT: Decimal("7.0"),
        WalletTier.GOOD: Decimal("5.0"),
        WalletTier.POOR: Decimal("0"),
    }
    
    # 等级对应的配置
    TIER_CONFIG = {
        WalletTier.ELITE: {
            "max_allocation": Decimal("0.15"),  # 15%
            "max_daily_trades": 10,
            "multiplier": Decimal("2.0"),
        },
        WalletTier.EXPERT: {
            "max_allocation": Decimal("0.10"),  # 10%
            "max_daily_trades": 8,
            "multiplier": Decimal("1.5"),
        },
        WalletTier.GOOD: {
            "max_allocation": Decimal("0.07"),  # 7%
            "max_daily_trades": 5,
            "multiplier": Decimal("1.0"),
        },
        WalletTier.POOR: {
            "max_allocation": Decimal("0"),
            "max_daily_trades": 0,
            "multiplier": Decimal("0"),
        },
    }
    
    def __init__(
        self,
        min_trades: int = 20,
        min_win_rate: Decimal = Decimal("0.55"),
        min_profit_factor: Decimal = Decimal("1.2"),
        max_drawdown_threshold: Decimal = Decimal("0.30"),
    ):
        """
        初始化评分器
        
        Args:
            min_trades: 最小交易次数
            min_win_rate: 最小胜率
            min_profit_factor: 最小盈亏比
            max_drawdown_threshold: 最大回撤阈值
        """
        self.min_trades = min_trades
        self.min_win_rate = min_win_rate
        self.min_profit_factor = min_profit_factor
        self.max_drawdown_threshold = max_drawdown_threshold
        
        logger.info(
            f"钱包质量评分器初始化 | "
            f"最小交易: {min_trades} | "
            f"最小胜率: {min_win_rate*100}% | "
            f"最小盈亏比: {min_profit_factor}"
        )
    
    def score_wallet(
        self,
        wallet_address: str,
        trades: List[Dict[str, Any]]
    ) -> QualityScore:
        """
        评估钱包质量
        
        Args:
            wallet_address: 钱包地址
            trades: 交易历史列表
        
        Returns:
            质量评分结果
        """
        # 计算统计数据
        stats = self._calculate_stats(trades)
        
        # 检测红旗
        red_flags = self._detect_red_flags(stats, trades)
        
        # 检测做市商
        is_market_maker = self._detect_market_maker(stats)
        
        # 计算各维度分数
        win_rate_score = self._score_win_rate(stats)
        profit_factor_score = self._score_profit_factor(stats)
        consistency_score = self._score_consistency(stats, trades)
        risk_score = self._score_risk(stats)
        specialty_score = self._score_specialty(stats)
        
        # 计算综合分数
        overall_score = (
            win_rate_score * Decimal("0.20") +
            profit_factor_score * Decimal("0.25") +
            consistency_score * Decimal("0.20") +
            risk_score * Decimal("0.15") +
            specialty_score * Decimal("0.20")
        )
        
        # 确定等级
        tier = self._determine_tier(overall_score, red_flags, is_market_maker)
        
        # 计算建议配置
        config = self.TIER_CONFIG[tier]
        
        score = QualityScore(
            wallet_address=wallet_address,
            tier=tier,
            overall_score=overall_score,
            win_rate_score=win_rate_score,
            profit_factor_score=profit_factor_score,
            consistency_score=consistency_score,
            risk_score=risk_score,
            specialty_score=specialty_score,
            stats=stats,
            red_flags=red_flags,
            is_market_maker=is_market_maker,
            recommended_allocation=config["max_allocation"],
            max_position_size=Decimal("50") * config["multiplier"],
        )
        
        logger.info(
            f"钱包评分完成 | {wallet_address[:10]}... | "
            f"等级: {tier.value} | 分数: {overall_score:.2f} | "
            f"红旗: {len(red_flags)} | 做市商: {is_market_maker}"
        )
        
        return score
    
    def _calculate_stats(self, trades: List[Dict[str, Any]]) -> TradingStats:
        """计算交易统计"""
        stats = TradingStats()
        
        if not trades:
            return stats
        
        total_profit = Decimal("0")
        total_loss = Decimal("0")
        hold_times = []
        peak_pnl = Decimal("0")
        current_drawdown = Decimal("0")
        max_drawdown = Decimal("0")
        
        for trade in trades:
            pnl = Decimal(str(trade.get("pnl", 0)))
            stats.total_trades += 1
            
            if pnl >= 0:
                stats.winning_trades += 1
                total_profit += pnl
            else:
                stats.losing_trades += 1
                total_loss += abs(pnl)
            
            # 计算回撤
            current_drawdown += pnl
            if current_drawdown > peak_pnl:
                peak_pnl = current_drawdown
            drawdown = peak_pnl - current_drawdown
            if drawdown > max_drawdown:
                max_drawdown = drawdown
            
            # 持仓时间
            if "opened_at" in trade and "closed_at" in trade:
                try:
                    opened = datetime.fromisoformat(trade["opened_at"].replace("Z", "+00:00"))
                    closed = datetime.fromisoformat(trade["closed_at"].replace("Z", "+00:00"))
                    hold_times.append((closed - opened).total_seconds() / 3600)
                except Exception:
                    pass
            
            # 交易时间
            if "timestamp" in trade:
                try:
                    ts = datetime.fromisoformat(trade["timestamp"].replace("Z", "+00:00"))
                    if stats.first_trade_at is None or ts < stats.first_trade_at:
                        stats.first_trade_at = ts
                    if stats.last_trade_at is None or ts > stats.last_trade_at:
                        stats.last_trade_at = ts
                except Exception:
                    pass
            
            # 分类统计
            category = trade.get("category", "unknown")
            stats.categories[category] = stats.categories.get(category, 0) + 1
        
        stats.total_profit = total_profit
        stats.total_loss = total_loss
        stats.max_drawdown = max_drawdown
        
        if hold_times:
            stats.avg_hold_time_hours = sum(hold_times) / len(hold_times)
        
        return stats
    
    def _detect_red_flags(
        self,
        stats: TradingStats,
        trades: List[Dict[str, Any]]
    ) -> List[str]:
        """检测红旗警告"""
        flags = []
        
        # 1. 新钱包综合症
        if stats.trading_days < 30 and stats.total_trades > 10:
            avg_trade_size = (stats.total_profit + stats.total_loss) / stats.total_trades
            if avg_trade_size > 100:  # 大额交易
                flags.append("new_wallet_syndrome")
        
        # 2. 运气因素
        if stats.total_trades < 10 and stats.win_rate > Decimal("0.9"):
            flags.append("luck_factor")
        
        # 3. 负盈亏比
        if stats.profit_factor < Decimal("1.0"):
            flags.append("negative_profit_factor")
        
        # 4. 策略分散
        if len(stats.categories) > 10 and stats.total_trades < 100:
            flags.append("scattered_strategy")
        
        # 5. 过大回撤
        total_pnl = stats.total_profit - stats.total_loss
        if total_pnl > 0 and stats.max_drawdown / total_pnl > self.max_drawdown_threshold:
            flags.append("excessive_drawdown")
        
        # 6. 低胜率
        if stats.win_rate < self.min_win_rate:
            flags.append("low_win_rate")
        
        # 7. 历史不足
        if stats.total_trades < self.min_trades:
            flags.append("insufficient_history")
        
        # 8. 内幕交易模式 (简化检测)
        # 检查是否有在重大事件前后的异常交易
        # 这里简化为检查持仓时间过短的高盈利交易
        for trade in trades[:20]:  # 检查最近20笔
            pnl_pct = Decimal(str(trade.get("pnl_pct", 0)))
            hold_hours = trade.get("hold_hours", 24)
            if pnl_pct > Decimal("0.5") and hold_hours < 1:  # 50%收益且持仓<1小时
                if "suspicious_timing" not in flags:
                    flags.append("suspicious_timing")
                break
        
        return flags
    
    def _detect_market_maker(self, stats: TradingStats) -> bool:
        """检测做市商"""
        # 做市商特征:
        # 1. 交易次数极高
        # 2. 平均持仓时间极短
        # 3. 胜率接近50%
        # 4. 单笔利润极低
        
        if stats.total_trades > 500:
            if stats.avg_hold_time_hours < 1:  # 平均持仓<1小时
                if Decimal("0.45") <= stats.win_rate <= Decimal("0.55"):
                    if stats.avg_profit_per_trade < Decimal("1"):
                        logger.warning(
                            f"检测到做市商特征 | "
                            f"交易: {stats.total_trades} | "
                            f"平均持仓: {stats.avg_hold_time_hours:.1f}h | "
                            f"胜率: {stats.win_rate*100:.1f}%"
                        )
                        return True
        
        return False
    
    def _score_win_rate(self, stats: TradingStats) -> Decimal:
        """评分胜率"""
        if stats.total_trades == 0:
            return Decimal("0")
        
        # 胜率越高分数越高
        # 60% = 6分, 70% = 8分, 80%+ = 10分
        if stats.win_rate >= Decimal("0.80"):
            return Decimal("10")
        elif stats.win_rate >= Decimal("0.70"):
            return Decimal("8")
        elif stats.win_rate >= Decimal("0.60"):
            return Decimal("6")
        elif stats.win_rate >= Decimal("0.55"):
            return Decimal("4")
        else:
            return Decimal("2")
    
    def _score_profit_factor(self, stats: TradingStats) -> Decimal:
        """评分盈亏比"""
        if stats.total_trades == 0:
            return Decimal("0")
        
        pf = stats.profit_factor
        
        # 盈亏比越高分数越高
        if pf >= Decimal("3.0"):
            return Decimal("10")
        elif pf >= Decimal("2.0"):
            return Decimal("8")
        elif pf >= Decimal("1.5"):
            return Decimal("6")
        elif pf >= Decimal("1.2"):
            return Decimal("4")
        else:
            return Decimal("2")
    
    def _score_consistency(
        self,
        stats: TradingStats,
        trades: List[Dict[str, Any]]
    ) -> Decimal:
        """评分稳定性"""
        if stats.total_trades < 10:
            return Decimal("5")  # 数据不足给中等分
        
        # 计算滚动胜率的标准差
        window_size = min(10, stats.total_trades // 2)
        if window_size < 3:
            return Decimal("5")
        
        # 简化: 使用整体胜率作为稳定性代理
        # 实际应该计算滚动窗口的标准差
        if stats.win_rate >= Decimal("0.55") and stats.win_rate <= Decimal("0.75"):
            # 稳定的胜率区间
            return Decimal("8")
        elif stats.win_rate > Decimal("0.75"):
            # 可能有运气成分
            return Decimal("6")
        else:
            return Decimal("4")
    
    def _score_risk(self, stats: TradingStats) -> Decimal:
        """评分风险控制"""
        if stats.total_trades == 0:
            return Decimal("5")
        
        # 基于回撤评分
        total_pnl = stats.total_profit - stats.total_loss
        if total_pnl > 0:
            drawdown_ratio = stats.max_drawdown / total_pnl
            
            if drawdown_ratio <= Decimal("0.10"):
                return Decimal("10")
            elif drawdown_ratio <= Decimal("0.20"):
                return Decimal("8")
            elif drawdown_ratio <= Decimal("0.30"):
                return Decimal("6")
            else:
                return Decimal("3")
        
        return Decimal("5")
    
    def _score_specialty(self, stats: TradingStats) -> Decimal:
        """评分专业领域"""
        if not stats.categories:
            return Decimal("5")
        
        # 找出最擅长的领域
        max_category_count = max(stats.categories.values())
        total_categorized = sum(stats.categories.values())
        
        if total_categorized == 0:
            return Decimal("5")
        
        specialty_ratio = Decimal(max_category_count) / Decimal(total_categorized)
        
        # 专业化程度越高分数越高
        if specialty_ratio >= Decimal("0.70"):
            return Decimal("10")
        elif specialty_ratio >= Decimal("0.50"):
            return Decimal("8")
        elif specialty_ratio >= Decimal("0.30"):
            return Decimal("6")
        else:
            return Decimal("4")
    
    def _determine_tier(
        self,
        score: Decimal,
        red_flags: List[str],
        is_market_maker: bool
    ) -> WalletTier:
        """确定钱包等级"""
        # 做市商或有红旗的降级
        if is_market_maker:
            return WalletTier.POOR
        
        if len(red_flags) >= 2:
            return WalletTier.POOR
        
        if len(red_flags) == 1:
            # 有一个红旗降一级
            if score >= self.TIER_THRESHOLDS[WalletTier.ELITE]:
                return WalletTier.EXPERT
            elif score >= self.TIER_THRESHOLDS[WalletTier.EXPERT]:
                return WalletTier.GOOD
            else:
                return WalletTier.POOR
        
        # 根据分数确定等级
        if score >= self.TIER_THRESHOLDS[WalletTier.ELITE]:
            return WalletTier.ELITE
        elif score >= self.TIER_THRESHOLDS[WalletTier.EXPERT]:
            return WalletTier.EXPERT
        elif score >= self.TIER_THRESHOLDS[WalletTier.GOOD]:
            return WalletTier.GOOD
        else:
            return WalletTier.POOR
