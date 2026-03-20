"""
配置管理模块
============
集中管理所有配置项，支持环境变量加载和验证。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List
import os
from pathlib import Path
from dotenv import load_dotenv


@dataclass
class RiskConfig:
    """风险管理配置"""
    # 每日最大损失限制 (USD)
    max_daily_loss: Decimal = Decimal("100")
    # 单笔最大仓位 (USD)
    max_position_size: Decimal = Decimal("50")
    # 最大仓位占账户余额比例
    max_position_pct: Decimal = Decimal("0.03")  # 3%
    # 最大并发仓位数
    max_concurrent_positions: int = 5
    # 最大总敞口 (USD) - 所有持仓价值之和的上限
    max_total_exposure: Decimal = Decimal("200")
    # 默认止损比例
    default_stop_loss_pct: Decimal = Decimal("0.15")  # 15%
    # 默认止盈比例
    default_take_profit_pct: Decimal = Decimal("0.25")  # 25%
    # 最大滑点容忍
    max_slippage: Decimal = Decimal("0.02")  # 2%


@dataclass
class EndgameConfig:
    """Endgame Sweeper策略配置"""
    # 最小入场概率
    min_probability: Decimal = Decimal("0.95")  # 95%
    # 最大距离结算天数
    max_days_to_resolution: int = 7
    # 最小日流动性 (USD)
    min_daily_liquidity: Decimal = Decimal("10000")
    # 最小年化收益
    min_annualized_return: Decimal = Decimal("0.20")  # 20%
    # 黑天鹅保护概率阈值
    black_swan_probability: Decimal = Decimal("0.998")  # 99.8%
    # 止损比例
    stop_loss_pct: Decimal = Decimal("0.10")  # 10%


@dataclass
class StrategyConfig:
    """策略选择配置"""
    # 最小信号置信度
    min_confidence_score: Decimal = Decimal("0.7")
    # 更新间隔 (小时)
    leaderboard_update_interval: int = 6
    # 最大跟踪钱包数
    max_tracked_wallets: int = 10
    # 最小钱包历史交易数
    min_wallet_transactions: int = 20


@dataclass
class MonitoringConfig:
    """监控配置"""
    # 交易监控间隔 (秒)
    trade_monitor_interval: int = 10
    # 仓位检查间隔 (秒)
    position_check_interval: int = 30
    # 健康检查间隔 (秒)
    health_check_interval: int = 60
    # 钱包扫描间隔 (秒)
    wallet_scan_interval: int = 300  # 5分钟


@dataclass
class WalletQualityConfig:
    """钱包质量评分配置"""
    # 最小交易次数
    min_trades: int = 20
    # 最小胜率
    min_win_rate: Decimal = Decimal("0.55")
    # 最小盈亏比
    min_profit_factor: Decimal = Decimal("1.2")
    # 最大回撤阈值
    max_drawdown_threshold: Decimal = Decimal("0.30")
    # 最小质量评分 (用于自动发现)
    min_quality_score: Decimal = Decimal("70")


@dataclass
class CopyTradingConfig:
    """跟单配置"""
    # 启用跟单
    enabled: bool = True
    # 自动发现高质量钱包 (无需手动配置目标钱包)
    auto_discover: bool = True
    # 交易数据库路径 (用于幂等性和历史记录)
    trade_db_path: str = "data/trades.db"
    # 最大跟单钱包数
    max_following_wallets: int = 10
    # 跟单模式: full, proportional, fixed, smart
    mode: str = "smart"
    # 固定金额
    fixed_amount: Decimal = Decimal("10")
    # 比例跟单比例
    proportional_ratio: Decimal = Decimal("0.1")
    # 最大跟单金额
    max_amount: Decimal = Decimal("50")
    # 最小跟单金额
    min_amount: Decimal = Decimal("5")
    # 跟单延迟(秒)
    copy_delay_seconds: float = 1.0
    # 是否跟平仓 (避免持仓失控的关键配置)
    follow_close: bool = True
    # 目标平仓时自动平仓
    close_on_target_close: bool = True
    # 持仓同步间隔(秒)
    position_sync_interval: int = 300
    # 目标钱包列表 (可选，auto_discover=True时可留空)
    target_wallets: List[str] = field(default_factory=list)


@dataclass
class WebSocketConfig:
    """WebSocket配置"""
    # 启用WebSocket
    enabled: bool = True
    # Polymarket WebSocket URL
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    # 钱包监控WebSocket URL (Polygonscan或Polygon RPC)
    wallet_monitor_ws_url: str = ""
    # 重连间隔(秒)
    reconnect_interval: int = 5
    # 最大重连次数
    max_reconnect_attempts: int = 10
    # 心跳间隔(秒)
    heartbeat_interval: int = 30
    # 消息超时(秒)
    message_timeout: int = 60


@dataclass
class Settings:
    """主配置类"""

    # API配置
    polygon_rpc_url: str = ""
    polymarket_api_url: str = "https://clob.polymarket.com"
    polygonscan_api_key: str = ""

    # 钱包配置
    private_key: str = ""
    wallet_address: str = ""  # EOA 地址（用于签名，通常是 MetaMask 地址）
    funder_address: str = ""  # Proxy 地址（资金存放地址，从 polymarket.com/settings 获取）

    # Polymarket 签名类型 (0=EOA, 1=Poly Proxy, 2=Gnosis Safe)
    # 如果留空，会根据 funder_address 自动判断
    polymarket_signature_type: Optional[int] = None

    # Telegram配置 (可选)
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # 运行模式
    dry_run: bool = True  # 默认干运行模式
    
    # 子配置
    risk: RiskConfig = field(default_factory=RiskConfig)
    endgame: EndgameConfig = field(default_factory=EndgameConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    wallet_quality: WalletQualityConfig = field(default_factory=WalletQualityConfig)
    copy_trading: CopyTradingConfig = field(default_factory=CopyTradingConfig)
    websocket: WebSocketConfig = field(default_factory=WebSocketConfig)
    
    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量加载配置"""
        # 加载.env文件
        env_path = Path(".") / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        
        settings = cls(
            polygon_rpc_url=os.getenv("POLYGON_RPC_URL", ""),
            polymarket_api_url=os.getenv("POLYMARKET_API_URL", "https://clob.polymarket.com"),
            polygonscan_api_key=os.getenv("POLYGONSCAN_API_KEY", ""),
            private_key=os.getenv("PRIVATE_KEY", ""),
            wallet_address=os.getenv("WALLET_ADDRESS", ""),
            funder_address=os.getenv("FUNDER_ADDRESS", ""),  # Proxy 地址
            polymarket_signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "-1")) if os.getenv("POLYMARKET_SIGNATURE_TYPE") else None,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
        )
        
        # 加载风险配置
        settings.risk = RiskConfig(
            max_daily_loss=Decimal(os.getenv("MAX_DAILY_LOSS", "100")),
            max_position_size=Decimal(os.getenv("MAX_POSITION_SIZE", "50")),
            max_position_pct=Decimal(os.getenv("MAX_POSITION_PCT", "0.03")),
            max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "5")),
            max_total_exposure=Decimal(os.getenv("MAX_TOTAL_EXPOSURE", "200")),
        )
        
        # 加载Endgame配置
        settings.endgame = EndgameConfig(
            min_probability=Decimal(os.getenv("ENDGAME_MIN_PROB", "0.95")),
            max_days_to_resolution=int(os.getenv("ENDGAME_MAX_DAYS", "7")),
            min_daily_liquidity=Decimal(os.getenv("ENDGAME_MIN_LIQUIDITY", "10000")),
        )
        
        # 加载钱包质量配置
        settings.wallet_quality = WalletQualityConfig(
            min_trades=int(os.getenv("WALLET_MIN_TRADES", "20")),
            min_win_rate=Decimal(os.getenv("WALLET_MIN_WIN_RATE", "0.55")),
            min_profit_factor=Decimal(os.getenv("WALLET_MIN_PROFIT_FACTOR", "1.2")),
            min_quality_score=Decimal(os.getenv("WALLET_MIN_QUALITY_SCORE", "70")),
        )
        
        # 加载跟单配置
        settings.copy_trading = CopyTradingConfig(
            enabled=os.getenv("COPY_TRADING_ENABLED", "true").lower() == "true",
            auto_discover=os.getenv("COPY_AUTO_DISCOVER", "true").lower() == "true",
            max_following_wallets=int(os.getenv("COPY_MAX_WALLETS", "10")),
            mode=os.getenv("COPY_TRADING_MODE", "smart"),
            fixed_amount=Decimal(os.getenv("COPY_FIXED_AMOUNT", "10")),
            max_amount=Decimal(os.getenv("COPY_MAX_AMOUNT", "50")),
            min_amount=Decimal(os.getenv("COPY_MIN_AMOUNT", "5")),
            copy_delay_seconds=float(os.getenv("COPY_DELAY_SECONDS", "1.0")),
            proportional_ratio=Decimal(os.getenv("COPY_PROPORTIONAL_RATIO", "0.1")),
            follow_close=os.getenv("COPY_FOLLOW_CLOSE", "true").lower() == "true",
            close_on_target_close=os.getenv("COPY_CLOSE_ON_TARGET_CLOSE", "true").lower() == "true",
            position_sync_interval=int(os.getenv("COPY_POSITION_SYNC_INTERVAL", "300")),
        )

        # 加载目标钱包列表
        wallets_str = os.getenv("TARGET_WALLETS", "")
        if wallets_str:
            settings.copy_trading.target_wallets = [
                w.strip() for w in wallets_str.split(",") if w.strip()
            ]
        
        # 加载WebSocket配置
        settings.websocket = WebSocketConfig(
            enabled=os.getenv("WEBSOCKET_ENABLED", "true").lower() == "true",
            polymarket_ws_url=os.getenv("POLYMARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            wallet_monitor_ws_url=os.getenv("WALLET_MONITOR_WS_URL", ""),
            reconnect_interval=int(os.getenv("WS_RECONNECT_INTERVAL", "5")),
            max_reconnect_attempts=int(os.getenv("WS_MAX_RECONNECT", "10")),
        )
        
        return settings
    
    def validate(self) -> list[str]:
        """验证配置完整性，返回错误列表"""
        errors = []
        
        if not self.polygon_rpc_url:
            errors.append("缺少 POLYGON_RPC_URL 配置")
        
        if not self.private_key:
            errors.append("缺少 PRIVATE_KEY 配置")
        
        if not self.wallet_address:
            errors.append("缺少 WALLET_ADDRESS 配置")
        
        # 验证私钥格式
        if self.private_key:
            key = self.private_key.lower()
            if key.startswith("0x"):
                key = key[2:]
            if len(key) != 64 or not all(c in "0123456789abcdef" for c in key):
                errors.append("PRIVATE_KEY 格式无效 (需要64位十六进制)")
        
        # 验证钱包地址格式
        if self.wallet_address:
            addr = self.wallet_address.lower()
            if not addr.startswith("0x") or len(addr) != 42:
                errors.append("WALLET_ADDRESS 格式无效 (需要0x开头的42位地址)")
        
        # 验证目标钱包地址
        for wallet in self.copy_trading.target_wallets:
            if not wallet.startswith("0x") or len(wallet) != 42:
                errors.append(f"目标钱包地址格式无效: {wallet[:10]}...")
        
        return errors


# 全局配置实例
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """获取全局配置实例"""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reload_settings() -> Settings:
    """重新加载配置"""
    global _settings
    _settings = Settings.from_env()
    return _settings
