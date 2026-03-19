"""
红旗检测器
==========
检测钱包的可疑行为和潜在风险。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from enum import Enum

from utils.logger import get_logger

logger = get_logger(__name__)


class RedFlagType(Enum):
    """红旗类型"""
    NEW_WALLET_SYNDROME = "new_wallet_syndrome"          # 新钱包大额交易
    LUCK_FACTOR = "luck_factor"                          # 运气因素
    WASH_TRADING = "wash_trading"                        # 对敲交易
    NEGATIVE_PROFIT_FACTOR = "negative_profit_factor"    # 负盈亏比
    SCATTERED_STRATEGY = "scattered_strategy"            # 策略分散
    EXCESSIVE_DRAWDOWN = "excessive_drawdown"            # 过大回撤
    LOW_WIN_RATE = "low_win_rate"                        # 低胜率
    INSUFFICIENT_HISTORY = "insufficient_history"        # 历史不足
    SUSPICIOUS_TIMING = "suspicious_timing"              # 可疑时机
    INSIDER_PATTERN = "insider_pattern"                  # 内幕模式
    HIGH_FREQUENCY = "high_frequency"                    # 高频交易
    ABNORMAL_SIZE = "abnormal_size"                      # 异常仓位


@dataclass
class RedFlag:
    """红旗警告"""
    flag_type: RedFlagType
    severity: str  # "low", "medium", "high", "critical"
    description: str
    details: Dict[str, Any] = field(default_factory=dict)
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def is_critical(self) -> bool:
        return self.severity == "critical"
    
    @property
    def should_block(self) -> bool:
        """是否应该阻止跟单"""
        return self.severity in ["high", "critical"]


class RedFlagDetector:
    """
    红旗检测器
    
    检测9种主要红旗类型:
    1. 新钱包综合症
    2. 运气因素
    3. 对敲交易
    4. 负盈亏比
    5. 策略分散
    6. 过大回撤
    7. 低胜率
    8. 历史不足
    9. 内幕交易模式
    """
    
    SEVERITY_THRESHOLDS = {
        "low": 1,
        "medium": 2,
        "high": 3,
        "critical": 4,
    }
    
    def __init__(
        self,
        min_trades: int = 20,
        min_win_rate: Decimal = Decimal("0.50"),
        min_profit_factor: Decimal = Decimal("1.0"),
        max_drawdown_pct: Decimal = Decimal("0.30"),
        max_categories: int = 8,
        min_trading_days: int = 30,
    ):
        """
        初始化红旗检测器
        
        Args:
            min_trades: 最小交易次数
            min_win_rate: 最小胜率
            min_profit_factor: 最小盈亏比
            max_drawdown_pct: 最大回撤百分比
            max_categories: 最大分类数
            min_trading_days: 最小交易天数
        """
        self.min_trades = min_trades
        self.min_win_rate = min_win_rate
        self.min_profit_factor = min_profit_factor
        self.max_drawdown_pct = max_drawdown_pct
        self.max_categories = max_categories
        self.min_trading_days = min_trading_days
        
        logger.info("红旗检测器初始化完成")
    
    def detect(
        self,
        wallet_address: str,
        trades: List[Dict[str, Any]],
        wallet_stats: Optional[Dict[str, Any]] = None
    ) -> List[RedFlag]:
        """
        检测红旗
        
        Args:
            wallet_address: 钱包地址
            trades: 交易历史
            wallet_stats: 钱包统计信息
        
        Returns:
            检测到的红旗列表
        """
        flags = []
        
        if not trades:
            flags.append(RedFlag(
                flag_type=RedFlagType.INSUFFICIENT_HISTORY,
                severity="high",
                description="无交易历史",
                details={"trade_count": 0}
            ))
            return flags
        
        # 计算基础统计
        stats = self._calculate_basic_stats(trades)
        
        # 检测各类红旗
        flags.extend(self._detect_new_wallet_syndrome(trades, stats))
        flags.extend(self._detect_luck_factor(trades, stats))
        flags.extend(self._detect_wash_trading(trades, stats))
        flags.extend(self._detect_negative_profit_factor(trades, stats))
        flags.extend(self._detect_scattered_strategy(trades, stats))
        flags.extend(self._detect_excessive_drawdown(trades, stats))
        flags.extend(self._detect_low_win_rate(trades, stats))
        flags.extend(self._detect_insufficient_history(trades, stats))
        flags.extend(self._detect_suspicious_timing(trades, stats))
        flags.extend(self._detect_high_frequency(trades, stats))
        flags.extend(self._detect_abnormal_size(trades, stats))
        
        if flags:
            logger.warning(
                f"检测到红旗 | 钱包: {wallet_address[:10]}... | "
                f"数量: {len(flags)} | "
                f"类型: {[f.flag_type.value for f in flags]}"
            )
        
        return flags
    
    def _calculate_basic_stats(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """计算基础统计"""
        total_trades = len(trades)
        winning = sum(1 for t in trades if Decimal(str(t.get("pnl", 0))) > 0)
        total_pnl = sum(Decimal(str(t.get("pnl", 0))) for t in trades)
        
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
            first_trade = min(timestamps)
            last_trade = max(timestamps)
            trading_days = (last_trade - first_trade).days + 1
        else:
            trading_days = 0
        
        # 分类
        categories = {}
        for t in trades:
            cat = t.get("category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1
        
        return {
            "total_trades": total_trades,
            "winning_trades": winning,
            "win_rate": Decimal(winning) / Decimal(total_trades) if total_trades > 0 else Decimal("0"),
            "total_pnl": total_pnl,
            "trading_days": trading_days,
            "categories": categories,
            "category_count": len(categories),
        }
    
    def _detect_new_wallet_syndrome(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测新钱包综合症"""
        flags = []
        
        # 新钱包(<30天)但有大量大额交易
        if stats["trading_days"] < self.min_trading_days:
            avg_trade_size = abs(stats["total_pnl"]) / max(stats["total_trades"], 1)
            
            if avg_trade_size > 100 and stats["total_trades"] > 5:
                flags.append(RedFlag(
                    flag_type=RedFlagType.NEW_WALLET_SYNDROME,
                    severity="high",
                    description="新钱包进行大额交易",
                    details={
                        "trading_days": stats["trading_days"],
                        "avg_trade_size": float(avg_trade_size),
                        "trade_count": stats["total_trades"],
                    }
                ))
        
        return flags
    
    def _detect_luck_factor(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测运气因素"""
        flags = []
        
        # 交易次数少但胜率极高
        if stats["total_trades"] < 10 and stats["win_rate"] > Decimal("0.90"):
            flags.append(RedFlag(
                flag_type=RedFlagType.LUCK_FACTOR,
                severity="medium",
                description="交易次数少但胜率极高，可能是运气",
                details={
                    "trade_count": stats["total_trades"],
                    "win_rate": float(stats["win_rate"]),
                }
            ))
        
        return flags
    
    def _detect_wash_trading(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测对敲交易"""
        flags = []
        
        # 检测同一市场的频繁买卖
        market_trades = {}
        for t in trades:
            market_id = t.get("market_id", "unknown")
            if market_id not in market_trades:
                market_trades[market_id] = []
            market_trades[market_id].append(t)
        
        # 检查是否有在同一市场反复交易的模式
        wash_trading_markets = 0
        for market_id, market_trade_list in market_trades.items():
            if len(market_trade_list) > 5:
                # 检查买卖方向交替
                sides = [t.get("side", "") for t in market_trade_list]
                alternations = sum(1 for i in range(len(sides)-1) if sides[i] != sides[i+1])
                
                if alternations > len(sides) * 0.7:  # 70%交替
                    wash_trading_markets += 1
        
        if wash_trading_markets > 0:
            flags.append(RedFlag(
                flag_type=RedFlagType.WASH_TRADING,
                severity="high",
                description="疑似对敲交易",
                details={
                    "wash_trading_markets": wash_trading_markets,
                }
            ))
        
        return flags
    
    def _detect_negative_profit_factor(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测负盈亏比"""
        flags = []
        
        # 计算盈亏比
        gross_profit = sum(Decimal(str(t.get("pnl", 0))) for t in trades if Decimal(str(t.get("pnl", 0))) > 0)
        gross_loss = abs(sum(Decimal(str(t.get("pnl", 0))) for t in trades if Decimal(str(t.get("pnl", 0))) < 0))
        
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        else:
            profit_factor = Decimal("999") if gross_profit > 0 else Decimal("0")
        
        if profit_factor < self.min_profit_factor:
            severity = "critical" if profit_factor < Decimal("0.5") else "high"
            flags.append(RedFlag(
                flag_type=RedFlagType.NEGATIVE_PROFIT_FACTOR,
                severity=severity,
                description=f"盈亏比过低: {profit_factor:.2f}",
                details={
                    "profit_factor": float(profit_factor),
                    "gross_profit": float(gross_profit),
                    "gross_loss": float(gross_loss),
                }
            ))
        
        return flags
    
    def _detect_scattered_strategy(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测策略分散"""
        flags = []
        
        # 交易类别过多
        if stats["category_count"] > self.max_categories:
            flags.append(RedFlag(
                flag_type=RedFlagType.SCATTERED_STRATEGY,
                severity="medium",
                description=f"交易类别过多: {stats['category_count']}",
                details={
                    "category_count": stats["category_count"],
                    "categories": list(stats["categories"].keys()),
                }
            ))
        
        return flags
    
    def _detect_excessive_drawdown(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测过大回撤"""
        flags = []
        
        # 计算最大回撤
        peak = Decimal("0")
        current = Decimal("0")
        max_dd = Decimal("0")
        
        for t in sorted(trades, key=lambda x: x.get("timestamp", "")):
            pnl = Decimal(str(t.get("pnl", 0)))
            current += pnl
            if current > peak:
                peak = current
            dd = peak - current
            if dd > max_dd:
                max_dd = dd
        
        # 计算回撤比例
        total_pnl = abs(stats["total_pnl"])
        if total_pnl > 0 and max_dd > 0:
            dd_pct = max_dd / total_pnl
            
            if dd_pct > self.max_drawdown_pct:
                flags.append(RedFlag(
                    flag_type=RedFlagType.EXCESSIVE_DRAWDOWN,
                    severity="high",
                    description=f"回撤过大: {dd_pct*100:.1f}%",
                    details={
                        "max_drawdown": float(max_dd),
                        "drawdown_pct": float(dd_pct),
                    }
                ))
        
        return flags
    
    def _detect_low_win_rate(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测低胜率"""
        flags = []
        
        if stats["win_rate"] < self.min_win_rate and stats["total_trades"] >= self.min_trades:
            severity = "critical" if stats["win_rate"] < Decimal("0.40") else "high"
            flags.append(RedFlag(
                flag_type=RedFlagType.LOW_WIN_RATE,
                severity=severity,
                description=f"胜率过低: {stats['win_rate']*100:.1f}%",
                details={
                    "win_rate": float(stats["win_rate"]),
                    "total_trades": stats["total_trades"],
                }
            ))
        
        return flags
    
    def _detect_insufficient_history(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测历史不足"""
        flags = []
        
        if stats["total_trades"] < self.min_trades:
            flags.append(RedFlag(
                flag_type=RedFlagType.INSUFFICIENT_HISTORY,
                severity="medium",
                description=f"交易历史不足: {stats['total_trades']}笔",
                details={
                    "trade_count": stats["total_trades"],
                    "min_required": self.min_trades,
                }
            ))
        
        return flags
    
    def _detect_suspicious_timing(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测可疑时机"""
        flags = []
        
        # 检查是否有在极短时间内获得极高收益的交易
        suspicious_count = 0
        for t in trades:
            pnl_pct = Decimal(str(t.get("pnl_pct", 0)))
            hold_hours = t.get("hold_hours", 24)
            
            # 极高收益且极短持仓
            if pnl_pct > Decimal("0.5") and hold_hours < 0.5:  # 50%收益且持仓<30分钟
                suspicious_count += 1
        
        if suspicious_count >= 3:
            flags.append(RedFlag(
                flag_type=RedFlagType.SUSPICIOUS_TIMING,
                severity="high",
                description="存在可疑的高收益短持仓交易",
                details={
                    "suspicious_count": suspicious_count,
                }
            ))
        
        return flags
    
    def _detect_high_frequency(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测高频交易"""
        flags = []
        
        # 计算日均交易次数
        if stats["trading_days"] > 0:
            daily_avg = stats["total_trades"] / stats["trading_days"]
            
            if daily_avg > 50:  # 日均超过50笔
                flags.append(RedFlag(
                    flag_type=RedFlagType.HIGH_FREQUENCY,
                    severity="medium",
                    description=f"高频交易: 日均{daily_avg:.1f}笔",
                    details={
                        "daily_avg": daily_avg,
                    }
                ))
        
        return flags
    
    def _detect_abnormal_size(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> List[RedFlag]:
        """检测异常仓位"""
        flags = []
        
        # 检查仓位大小异常
        sizes = [Decimal(str(t.get("size", 0))) for t in trades if t.get("size")]
        if sizes:
            avg_size = sum(sizes) / len(sizes)
            max_size = max(sizes)
            
            # 最大仓位远大于平均仓位
            if max_size > avg_size * 10 and max_size > 1000:
                flags.append(RedFlag(
                    flag_type=RedFlagType.ABNORMAL_SIZE,
                    severity="low",
                    description="存在异常大额交易",
                    details={
                        "max_size": float(max_size),
                        "avg_size": float(avg_size),
                    }
                ))
        
        return flags
    
    def should_block_trading(self, flags: List[RedFlag]) -> tuple[bool, str]:
        """
        判断是否应该阻止跟单
        
        Args:
            flags: 检测到的红旗列表
        
        Returns:
            (是否阻止, 原因)
        """
        if not flags:
            return False, ""
        
        # 检查是否有阻塞性红旗
        blocking_flags = [f for f in flags if f.should_block]
        if blocking_flags:
            reasons = [f.description for f in blocking_flags]
            return True, "; ".join(reasons)
        
        # 检查红旗总数
        if len(flags) >= 3:
            return True, f"检测到{len(flags)}个风险信号"
        
        return False, ""
