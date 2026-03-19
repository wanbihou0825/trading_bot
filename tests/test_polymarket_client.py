"""
Polymarket客户端单元测试
========================
测试API调用和订单执行逻辑。
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock
import aiohttp

from services.polymarket_client import PolymarketClient, OrderResult, with_retry


class TestPolymarketClient:
    """Polymarket客户端测试类"""
    
    def test_initialization_dry_run(self):
        """测试初始化 - 模拟模式"""
        client = PolymarketClient(dry_run=True)
        
        assert client.dry_run is True
        assert client.api_url == "https://clob.polymarket.com"
    
    def test_initialization_production(self):
        """测试初始化 - 生产模式"""
        client = PolymarketClient(
            api_url="https://custom.api.com",
            dry_run=False
        )
        
        assert client.dry_run is False
        assert client.api_url == "https://custom.api.com"
    
    @pytest.mark.asyncio
    async def test_connect_dry_run(self):
        """测试连接 - 模拟模式"""
        client = PolymarketClient(dry_run=True)
        
        result = await client.connect()
        
        assert result is True
        assert client.is_connected is True
    
    @pytest.mark.asyncio
    async def test_disconnect(self):
        """测试断开连接"""
        client = PolymarketClient(dry_run=True)
        await client.connect()
        
        await client.disconnect()
        
        assert client.is_connected is False
    
    @pytest.mark.asyncio
    async def test_get_markets_dry_run(self):
        """测试获取市场列表 - 模拟模式"""
        client = PolymarketClient(dry_run=True)
        await client.connect()
        
        markets = await client.get_markets(limit=5)
        
        assert isinstance(markets, list)
        assert len(markets) <= 5
        # 检查模拟数据结构
        for market in markets:
            assert "id" in market
            assert "question" in market
            assert "yes_price" in market
            assert "no_price" in market
    
    @pytest.mark.asyncio
    async def test_get_market_price_dry_run(self):
        """测试获取市场价格 - 模拟模式"""
        client = PolymarketClient(dry_run=True)
        await client.connect()
        
        price = await client.get_market_price("test_market")
        
        assert price is not None
        assert "yes" in price
        assert "no" in price
        assert price["yes"] + price["no"] == Decimal("1")
    
    @pytest.mark.asyncio
    async def test_place_order_dry_run(self):
        """测试下单 - 模拟模式"""
        client = PolymarketClient(dry_run=True)
        await client.connect()
        
        result = await client.place_order(
            market_id="test_market",
            side="YES",
            size=Decimal("10"),
            price=Decimal("0.65")
        )
        
        assert result.success is True
        assert result.order_id is not None
        assert result.filled_size == Decimal("10")
        assert result.filled_price == Decimal("0.65")
    
    @pytest.mark.asyncio
    async def test_cancel_order_dry_run(self):
        """测试取消订单 - 模拟模式"""
        client = PolymarketClient(dry_run=True)
        await client.connect()
        
        result = await client.cancel_order("test_order")
        
        assert result is True
    
    @pytest.mark.asyncio
    async def test_get_account_balance_dry_run(self):
        """测试获取账户余额 - 模拟模式"""
        client = PolymarketClient(dry_run=True)
        await client.connect()
        
        balance = await client.get_account_balance()
        
        assert balance == Decimal("1000")
    
    def test_parse_market_data(self):
        """测试解析市场数据"""
        client = PolymarketClient(dry_run=True)
        
        market_info = {
            "id": "test_market",
            "question": "Will X happen?",
            "yes_price": 0.65,
            "no_price": 0.35,
            "volume_24h": 100000,
            "liquidity": 50000,
            "end_date": "2026-03-25T00:00:00Z"
        }
        
        market_data = client.parse_market_data(market_info)
        
        assert market_data is not None
        assert market_data.market_id == "test_market"
        assert market_data.question == "Will X happen?"
        assert market_data.yes_price == Decimal("0.65")
        assert market_data.no_price == Decimal("0.35")
    
    def test_parse_market_data_invalid(self):
        """测试解析无效市场数据"""
        client = PolymarketClient(dry_run=True)
        
        # 缺少必要字段
        market_info = {"id": "test"}
        
        market_data = client.parse_market_data(market_info)
        
        # 应该返回MarketData对象，即使数据不完整
        assert market_data is not None


class TestOrderResult:
    """订单结果测试类"""
    
    def test_success_result(self):
        """测试成功订单结果"""
        result = OrderResult(
            success=True,
            order_id="order_123",
            filled_size=Decimal("10"),
            filled_price=Decimal("0.65")
        )
        
        assert result.success is True
        assert result.error is None
    
    def test_failure_result(self):
        """测试失败订单结果"""
        result = OrderResult(
            success=False,
            error="Insufficient balance"
        )
        
        assert result.success is False
        assert result.error == "Insufficient balance"
        assert result.filled_size == Decimal("0")


class TestRetryDecorator:
    """重试装饰器测试类"""
    
    @pytest.mark.asyncio
    async def test_retry_on_timeout(self):
        """测试超时重试"""
        call_count = 0
        
        @with_retry(max_retries=2, base_delay=0.1)
        async def failing_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise asyncio.TimeoutError("Connection timeout")
            return "success"
        
        result = await failing_function()
        
        assert result == "success"
        assert call_count == 3
    
    @pytest.mark.asyncio
    async def test_retry_max_attempts(self):
        """测试达到最大重试次数"""
        call_count = 0
        
        @with_retry(max_retries=2, base_delay=0.1)
        async def always_failing():
            nonlocal call_count
            call_count += 1
            raise aiohttp.ClientError("Network error")
        
        with pytest.raises(aiohttp.ClientError):
            await always_failing()
        
        assert call_count == 3  # 1次初始 + 2次重试
    
    @pytest.mark.asyncio
    async def test_no_retry_on_non_retryable(self):
        """测试非重试异常不重试"""
        call_count = 0
        
        @with_retry(max_retries=3, base_delay=0.1)
        async def non_retryable_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("Invalid input")
        
        with pytest.raises(ValueError):
            await non_retryable_error()
        
        assert call_count == 1  # 只调用一次，不重试
    
    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        """测试成功时不重试"""
        call_count = 0
        
        @with_retry(max_retries=3)
        async def successful_function():
            nonlocal call_count
            call_count += 1
            return "success"
        
        result = await successful_function()
        
        assert result == "success"
        assert call_count == 1
