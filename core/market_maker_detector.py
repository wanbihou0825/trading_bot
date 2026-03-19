"""
做市商检测器
============
识别市场中的做市商行为模式。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from enum import Enum

from utils.logger import get_logger

logger = get_logger(__name__)


class MarketMakerType(Enum):
    """做市商类型"""
    PROFESSIONAL = "professional"    # 专业做市商
    RETAIL = "retail"                # 散户做市商
    ARBITRAGEUR = "arbitrageur"      # 套利者
    UNKNOWN = "unknown"


@dataclass
class MarketMakerPattern:
    """做市商模式"""
    pattern_type: str
    confidence: Decimal
    description: str
    indicators: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketMakerScore:
    """做市商评分结果"""
    wallet_address: str
    is_market_maker: bool
    maker_type: MarketMakerType
    confidence: Decimal
    patterns: List[MarketMakerPattern]
    stats: Dict[str, Any]
    recommendation: str  # "avoid", "monitor", "neutral"
    
    @property
    def should_avoid(self) -> bool:
        """是否应该避免跟单"""
        return self.is_market_maker and self.confidence > Decimal("0.7")


class MarketMakerDetector:
    """
    做市商检测器
    
    做市商特征:
    1. 高交易频率 (>500笔)
    2. 短持仓时间 (<1小时平均)
    3. 胜率接近50%
    4. 单笔利润极低 (<1%)
    5. 双向交易 (同时买卖)
    6. 连续报价模式
    """
    
    # 做市商特征阈值
    MM_THRESHOLDS = {
        "min_trades": 500,                    # 最小交易次数
        "max_avg_hold_hours": 1.0,            # 最大平均持仓时间
        "win_rate_min": 0.45,                 # 胜率下限
        "win_rate_max": 0.55,                 # 胜率上限
        "max_profit_per_trade": 0.01,         # 最大单笔利润率
        "min_daily_trades": 20,               # 最小日均交易
        "bid_ask_ratio_min": 0.8,             # 买卖比下限
        "bid_ask_ratio_max": 1.2,             # 买卖比上限
    }
    
    def __init__(
        self,
        min_trades: int = 500,
        max_avg_hold_hours: float = 1.0,
        win_rate_range: tuple = (0.45, 0.55),
        max_profit_per_trade: Decimal = Decimal("0.01"),
    ):
        """
        初始化做市商检测器
        
        Args:
            min_trades: 最小交易次数阈值
            max_avg_hold_hours: 最大平均持仓时间(小时)
            win_rate_range: 做市商典型胜率范围
            max_profit_per_trade: 最大单笔利润率
        """
        self.min_trades = min_trades
        self.max_avg_hold_hours = max_avg_hold_hours
        self.win_rate_min, self.win_rate_max = win_rate_range
        self.max_profit_per_trade = max_profit_per_trade
        
        logger.info(
            f"做市商检测器初始化 | "
            f"最小交易: {min_trades} | "
            f"最大持仓: {max_avg_hold_hours}h | "
            f"胜率范围: {win_rate_range}"
        )
    
    def detect(
        self,
        wallet_address: str,
        trades: List[Dict[str, Any]]
    ) -> MarketMakerScore:
        """
        检测钱包是否为做市商
        
        Args:
            wallet_address: 钱包地址
            trades: 交易历史
        
        Returns:
            做市商评分结果
        """
        if not trades:
            return self._create_result(
                wallet_address=wallet_address,
                is_maker=False,
                confidence=Decimal("0"),
                patterns=[],
                stats={},
                recommendation="neutral"
            )
        
        # 计算统计指标
        stats = self._calculate_stats(trades)
        
        # 检测各种做市商模式
        patterns = []
        patterns.extend(self._detect_high_frequency_pattern(stats))
        patterns.extend(self._detect_short_hold_pattern(stats))
        patterns.extend(self._detect_balanced_winrate_pattern(stats))
        patterns.extend(self._detect_low_margin_pattern(stats))
        patterns.extend(self._detect_bidirectional_pattern(trades, stats))
        patterns.extend(self._detect_continuous_quotes_pattern(trades, stats))
        
        # 计算综合置信度
        if patterns:
            avg_confidence = sum(p.confidence for p in patterns) / len(patterns)
            pattern_count = len(patterns)
            # 模式越多，置信度越高
            confidence = avg_confidence * min(Decimal(pattern_count) / Decimal("3"), Decimal("1"))
        else:
            confidence = Decimal("0")
        
        # 判断是否为做市商
        is_maker = confidence > Decimal("0.5") and len(patterns) >= 2
        
        # 确定做市商类型
        maker_type = self._determine_maker_type(patterns, stats) if is_maker else MarketMakerType.UNKNOWN
        
        # 生成建议
        if is_maker:
            recommendation = "avoid" if confidence > Decimal("0.7") else "monitor"
        else:
            recommendation = "neutral"
        
        result = self._create_result(
            wallet_address=wallet_address,
            is_maker=is_maker,
            confidence=confidence,
            patterns=patterns,
            stats=stats,
            recommendation=recommendation,
            maker_type=maker_type
        )
        
        if is_maker:
            logger.warning(
                f"检测到做市商 | {wallet_address[:10]}... | "
                f"类型: {maker_type.value} | "
                f"置信度: {confidence:.2%} | "
                f"模式数: {len(patterns)}"
            )
        
        return result
    
    def _calculate_stats(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """计算统计指标"""
        total_trades = len(trades)
        winning = sum(1 for t in trades if Decimal(str(t.get("pnl", 0))) > 0)
        losing = sum(1 for t in trades if Decimal(str(t.get("pnl", 0))) < 0)
        
        total_pnl = sum(Decimal(str(t.get("pnl", 0))) for t in trades)
        total_volume = sum(Decimal(str(t.get("size", 0))) for t in trades)
        
        # 持仓时间
        hold_times = []
        for t in trades:
            if "hold_hours" in t:
                hold_times.append(float(t["hold_hours"]))
            elif "opened_at" in t and "closed_at" in t:
                try:
                    opened = datetime.fromisoformat(t["opened_at"].replace("Z", "+00:00"))
                    closed = datetime.fromisoformat(t["closed_at"].replace("Z", "+00:00"))
                    hold_times.append((closed - opened).total_seconds() / 3600)
                except Exception:
                    pass
        
        avg_hold_hours = sum(hold_times) / len(hold_times) if hold_times else 24.0
        
        # 时间范围
        timestamps = []
        for t in trades:
            if "timestamp" in t:
                try:
                    ts = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))
                    timestamps.append(ts)
                except Exception:
                    pass
        
        if timestamps:
            trading_days = (max(timestamps) - min(timestamps)).days + 1
        else:
            trading_days = 1
        
        daily_avg_trades = total_trades / max(trading_days, 1)
        
        # 买卖比例
        buy_count = sum(1 for t in trades if t.get("side", "").upper() == "YES")
        sell_count = sum(1 for t in trades if t.get("side", "").upper() == "NO")
        
        if sell_count > 0:
            bid_ask_ratio = buy_count / sell_count
        else:
            bid_ask_ratio = 999
        
        # 单笔利润率
        profit_rates = []
        for t in trades:
            if t.get("size") and Decimal(str(t.get("size", 0))) > 0:
                pnl = Decimal(str(t.get("pnl", 0)))
                size = Decimal(str(t.get("size", 0)))
                profit_rates.append(abs(pnl / size))
        
        avg_profit_rate = sum(profit_rates) / len(profit_rates) if profit_rates else Decimal("0")
        
        return {
            "total_trades": total_trades,
            "winning_trades": winning,
            "losing_trades": losing,
            "win_rate": Decimal(winning) / Decimal(total_trades) if total_trades > 0 else Decimal("0"),
            "total_pnl": total_pnl,
            "total_volume": total_volume,
            "avg_hold_hours": avg_hold_hours,
            "trading_days": trading_days,
            "daily_avg_trades": daily_avg_trades,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "bid_ask_ratio": bid_ask_ratio,
            "avg_profit_rate": avg_profit_rate,
        }
    
    def _detect_high_frequency_pattern(
        self,
        stats: Dict[str, Any]
    ) -> List[MarketMakerPattern]:
        """检测高频交易模式"""
        patterns = []
        
        if stats["total_trades"] >= self.min_trades:
            confidence = min(
                Decimal(stats["total_trades"]) / Decimal(self.min_trades * 2),
                Decimal("1")
            )
            
            patterns.append(MarketMakerPattern(
                pattern_type="high_frequency",
                confidence=confidence,
                description=f"高频交易: {stats['total_trades']}笔交易",
                indicators={
                    "total_trades": stats["total_trades"],
                    "daily_avg": stats["daily_avg_trades"],
                }
            ))
        
        elif stats["daily_avg_trades"] >= self.MM_THRESHOLDS["min_daily_trades"]:
            patterns.append(MarketMakerPattern(
                pattern_type="high_daily_frequency",
                confidence=Decimal("0.6"),
                description=f"日均高频: {stats['daily_avg_trades']:.1f}笔/天",
                indicators={
                    "daily_avg_trades": stats["daily_avg_trades"],
                }
            ))
        
        return patterns
    
    def _detect_short_hold_pattern(
        self,
        stats: Dict[str, Any]
    ) -> List[MarketMakerPattern]:
        """检测短持仓模式"""
        patterns = []
        
        if stats["avg_hold_hours"] <= self.max_avg_hold_hours:
            confidence = Decimal("1") - Decimal(str(stats["avg_hold_hours"])) / Decimal(str(self.max_avg_hold_hours))
            
            patterns.append(MarketMakerPattern(
                pattern_type="short_hold",
                confidence=min(confidence, Decimal("1")),
                description=f"短持仓: 平均{stats['avg_hold_hours']:.2f}小时",
                indicators={
                    "avg_hold_hours": stats["avg_hold_hours"],
                }
            ))
        
        return patterns
    
    def _detect_balanced_winrate_pattern(
        self,
        stats: Dict[str, Any]
    ) -> List[MarketMakerPattern]:
        """检测平衡胜率模式"""
        patterns = []
        
        win_rate = float(stats["win_rate"])
        
        if self.win_rate_min <= win_rate <= self.win_rate_max:
            # 胜率越接近50%，置信度越高
            distance_from_50 = abs(win_rate - 0.5)
            confidence = Decimal("1") - Decimal(str(distance_from_50 * 10))
            
            patterns.append(MarketMakerPattern(
                pattern_type="balanced_winrate",
                confidence=max(confidence, Decimal("0.5")),
                description=f"平衡胜率: {win_rate*100:.1f}%",
                indicators={
                    "win_rate": win_rate,
                }
            ))
        
        return patterns
    
    def _detect_low_margin_pattern(
        self,
        stats: Dict[str, Any]
    ) -> List[MarketMakerPattern]:
        """检测低利润率模式"""
        patterns = []
        
        if stats["avg_profit_rate"] <= self.max_profit_per_trade:
            confidence = Decimal("1") - stats["avg_profit_rate"] / self.max_profit_per_trade
            
            patterns.append(MarketMakerPattern(
                pattern_type="low_margin",
                confidence=min(confidence, Decimal("1")),
                description=f"低利润率: {stats['avg_profit_rate']*100:.2f}%",
                indicators={
                    "avg_profit_rate": float(stats["avg_profit_rate"]),
                }
            ))
        
        return patterns
    
    def _detect_bidirectional_pattern(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[MarketMakerPattern]:
        """检测双向交易模式"""
        patterns = []
        
        bid_ask_ratio = stats["bid_ask_ratio"]
        min_ratio = self.MM_THRESHOLDS["bid_ask_ratio_min"]
        max_ratio = self.MM_THRESHOLDS["bid_ask_ratio_max"]
        
        if min_ratio <= bid_ask_ratio <= max_ratio:
            # 买卖比例越接近1，置信度越高
            distance_from_1 = abs(bid_ask_ratio - 1)
            confidence = Decimal("1") - Decimal(str(distance_from_1))
            
            patterns.append(MarketMakerPattern(
                pattern_type="bidirectional",
                confidence=max(confidence, Decimal("0.5")),
                description=f"双向交易: 买/卖={bid_ask_ratio:.2f}",
                indicators={
                    "bid_ask_ratio": bid_ask_ratio,
                    "buy_count": stats["buy_count"],
                    "sell_count": stats["sell_count"],
                }
            ))
        
        return patterns
    
    def _detect_continuous_quotes_pattern(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[MarketMakerPattern]:
        """检测连续报价模式"""
        patterns = []
        
        # 检查交易时间间隔的规律性
        if len(trades) < 10:
            return patterns
        
        # 按时间排序
        sorted_trades = sorted(
            trades,
            key=lambda x: x.get("timestamp", "")
        )
        
        # 计算交易间隔
        intervals = []
        for i in range(1, min(len(sorted_trades), 100)):
            try:
                t1 = datetime.fromisoformat(sorted_trades[i-1]["timestamp"].replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(sorted_trades[i]["timestamp"].replace("Z", "+00:00"))
                intervals.append((t2 - t1).total_seconds())
            except Exception:
                pass
        
        if intervals:
            # 计算间隔的标准差
            avg_interval = sum(intervals) / len(intervals)
            if avg_interval > 0:
                variance = sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)
                std_dev = variance ** 0.5
                cv = std_dev / avg_interval  # 变异系数
                
                # 变异系数越小，说明交易间隔越规律
                if cv < 0.5:  # 规律性强
                    patterns.append(MarketMakerPattern(
                        pattern_type="continuous_quotes",
                        confidence=Decimal("0.7"),
                        description=f"规律报价: 间隔变异系数={cv:.2f}",
                        indicators={
                            "avg_interval_seconds": avg_interval,
                            "cv": cv,
                        }
                    ))
        
        return patterns
    
    def _determine_maker_type(
        self,
        patterns: List[MarketMakerPattern],
        stats: Dict[str, Any]
    ) -> MarketMakerType:
        """确定做市商类型"""
        pattern_types = {p.pattern_type for p in patterns}
        
        # 专业做市商: 具备所有主要特征
        if len(pattern_types) >= 4:
            return MarketMakerType.PROFESSIONAL
        
        # 套利者: 高频 + 双向
        if "high_frequency" in pattern_types and "bidirectional" in pattern_types:
            return MarketMakerType.ARBITRAGEUR
        
        # 散户做市商: 部分特征
        return MarketMakerType.RETAIL
    
    def _create_result(
        self,
        wallet_address: str,
        is_maker: bool,
        confidence: Decimal,
        patterns: List[MarketMakerPattern],
        stats: Dict[str, Any],
        recommendation: str,
        maker_type: MarketMakerType = MarketMakerType.UNKNOWN
    ) -> MarketMakerScore:
        """创建检测结果"""
        return MarketMakerScore(
            wallet_address=wallet_address,
            is_market_maker=is_maker,
            maker_type=maker_type,
            confidence=confidence,
            patterns=patterns,
            stats=stats,
            recommendation=recommendation
        )
