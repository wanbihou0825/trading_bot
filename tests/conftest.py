"""
pytest配置和共享fixtures
========================
提供测试所需的共享配置和模拟对象。
"""

import asyncio
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

# 异步测试支持
@pytest.fixture(scope="session")
def event_loop():
    """创建事件循环"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_settings():
    """模拟配置"""
    from config.settings import Settings, RiskConfig, WalletQualityConfig, CopyTradingConfig
    
    settings = Settings(
        dry_run=True,
        wallet_address="0x1234567890123456789012345678901234567890",
        private_key="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        polygon_rpc_url="https://polygon-rpc.com",
        telegram_bot_token="test_token",
        telegram_chat_id="test_chat_id",
    )
    return settings


@pytest.fixture
def mock_circuit_breaker():
    """模拟熔断器"""
    from core.circuit_breaker import CircuitBreaker
    
    cb = CircuitBreaker(
        max_daily_loss=Decimal("100"),
        max_consecutive_losses=5,
        max_single_loss=Decimal("50")
    )
    return cb


@pytest.fixture
def mock_risk_manager(mock_circuit_breaker):
    """模拟风险管理器"""
    from core.risk_manager import RiskManager
    
    rm = RiskManager(
        circuit_breaker=mock_circuit_breaker,
        max_position_size=Decimal("50"),
        max_position_pct=Decimal("0.03"),
        max_concurrent_positions=5,
        max_total_exposure=Decimal("200")
    )
    rm.update_balance(Decimal("1000"))
    return rm


@pytest.fixture
def mock_polymarket_client():
    """模拟Polymarket客户端"""
    from services.polymarket_client import PolymarketClient, OrderResult
    
    client = MagicMock(spec=PolymarketClient)
    client.dry_run = True
    client.is_connected = True
    client.connect = AsyncMock(return_value=True)
    client.disconnect = AsyncMock()
    client.get_markets = AsyncMock(return_value=[])
    client.get_market_price = AsyncMock(return_value={"yes": Decimal("0.5"), "no": Decimal("0.5")})
    client.place_order = AsyncMock(return_value=OrderResult(
        success=True,
        order_id="test_order_123",
        filled_size=Decimal("10"),
        filled_price=Decimal("0.5")
    ))
    client.place_market_order = AsyncMock(return_value=OrderResult(
        success=True,
        order_id="test_order_123",
        filled_size=Decimal("10"),
        filled_price=Decimal("0.5")
    ))
    client.get_account_balance = AsyncMock(return_value=Decimal("1000"))
    client.get_user_trades = AsyncMock(return_value=[])
    return client


@pytest.fixture
def mock_telegram_service():
    """模拟Telegram服务"""
    from services.telegram_service import TelegramService
    
    telegram = MagicMock(spec=TelegramService)
    telegram.enabled = True
    telegram.send_message = AsyncMock()
    telegram.send_trade_notification = AsyncMock()
    telegram.send_startup_notification = AsyncMock()
    telegram.send_shutdown_notification = AsyncMock()
    return telegram


@pytest.fixture
def sample_market_data():
    """示例市场数据"""
    from strategies.base import MarketData
    
    return MarketData(
        market_id="test_market_1",
        question="Will the event happen by March 2026?",
        yes_price=Decimal("0.65"),
        no_price=Decimal("0.35"),
        volume_24h=Decimal("100000"),
        liquidity=Decimal("50000"),
        days_to_resolution=7
    )


@pytest.fixture
def sample_wallet_transaction():
    """示例钱包交易"""
    from core.wallet_monitor import WalletTransaction
    
    return WalletTransaction(
        wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
        tx_hash="0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        market_id="test_market_1",
        market_question="Test market question?",
        side="YES",
        size=Decimal("100"),
        price=Decimal("0.65"),
        timestamp=datetime.now(timezone.utc),
        tx_type="buy"
    )
