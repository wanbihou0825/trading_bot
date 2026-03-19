"""
核心模块
========
包含交易机器人的核心组件。
"""

from .exceptions import (
    BotException,
    InitializationError,
    ConfigurationError,
    TradeError as TradingError,
    RiskLimitError as RiskLimitExceeded,
    CircuitBreakerError as CircuitBreakerTriggered,
    GracefulShutdown,
)
from .circuit_breaker import CircuitBreaker, CircuitBreakerState as CircuitState
from .risk_manager import RiskManager, Position, RiskCheckResult
from .wallet_quality_scorer import (
    WalletQualityScorer,
    QualityScore,
    TradingStats,
    WalletTier,
)
from .market_maker_detector import (
    MarketMakerDetector,
    MarketMakerScore,
    MarketMakerType,
    MarketMakerPattern,
)
from .red_flag_detector import (
    RedFlagDetector,
    RedFlag,
    RedFlagType,
)
from .wallet_monitor import (
    WalletMonitor,
    WalletInfo,
    WalletTransaction,
    MonitorMode,
)
from .websocket_manager import (
    WebSocketManager,
    PolymarketWebSocket,
    ConnectionState,
    Subscription,
)
from .copy_executor import (
    CopyExecutor,
    CopyConfig,
    CopyMode,
    CopyTrade,
)
from .wallet_scanner import WalletScanner

__all__ = [
    # 异常
    "BotException",
    "InitializationError",
    "ConfigurationError",
    "TradingError",
    "RiskLimitExceeded",
    "CircuitBreakerTriggered",
    "GracefulShutdown",
    # 熔断器
    "CircuitBreaker",
    "CircuitState",
    # 风险管理
    "RiskManager",
    "Position",
    "RiskCheckResult",
    # 钱包质量评分
    "WalletQualityScorer",
    "QualityScore",
    "TradingStats",
    "WalletTier",
    # 做市商检测
    "MarketMakerDetector",
    "MarketMakerScore",
    "MarketMakerType",
    "MarketMakerPattern",
    # 警告检测
    "RedFlagDetector",
    "RedFlag",
    "RedFlagType",
    # 钱包监控
    "WalletMonitor",
    "WalletInfo",
    "WalletTransaction",
    "MonitorMode",
    # WebSocket
    "WebSocketManager",
    "PolymarketWebSocket",
    "ConnectionState",
    "Subscription",
    # 跟单执行
    "CopyExecutor",
    "CopyConfig",
    "CopyMode",
    "CopyTrade",
    # 钱包扫描器
    "WalletScanner",
]
