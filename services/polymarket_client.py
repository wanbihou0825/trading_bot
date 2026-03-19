"""
Polymarket 客户端模块
====================
与 Polymarket CLOB API 交互的客户端。
"""

import asyncio
import functools
from decimal import Decimal
from typing import Optional, Dict, List, Any, Callable
from dataclasses import dataclass
import aiohttp
from datetime import datetime, timezone

from utils.logger import get_logger
from utils.validation import validate_wallet_address
from strategies.base import MarketData

logger = get_logger(__name__)


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    retryable_exceptions: tuple = (aiohttp.ClientError, asyncio.TimeoutError)
):
    """
    重试装饰器
    
    Args:
        max_retries: 最大重试次数
        base_delay: 基础延迟（秒）
        max_delay: 最大延迟（秒）
        retryable_exceptions: 可重试的异常类型
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        # 指数退避
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.warning(
                            f"API调用失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}，"
                            f"{delay:.1f}秒后重试"
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"API调用失败，已达最大重试次数: {e}")
                except Exception as e:
                    # 非重试异常直接抛出
                    logger.error(f"API调用发生不可重试异常: {e}")
                    raise
            
            # 所有重试都失败了，抛出最后一个异常
            raise last_exception
        
        return wrapper
    return decorator


@dataclass
class OrderResult:
    """订单结果"""
    success: bool
    order_id: Optional[str] = None
    filled_size: Decimal = Decimal("0")
    filled_price: Decimal = Decimal("0")
    error: Optional[str] = None


class PolymarketClient:
    """
    Polymarket CLOB 客户端
    
    提供市场数据查询和交易功能。
    """
    
    API_BASE_URL = "https://clob.polymarket.com"
    
    def __init__(
        self,
        api_url: str = API_BASE_URL,
        dry_run: bool = True
    ):
        """
        初始化客户端
        
        Args:
            api_url: API基础URL
            dry_run: 是否为模拟模式
        """
        self.api_url = api_url
        self.dry_run = dry_run
        self._session: Optional[aiohttp.ClientSession] = None
        self._is_connected = False
        
        logger.info(f"Polymarket客户端初始化 | API: {api_url} | 模式: {'模拟' if dry_run else '实盘'}")
    
    async def connect(self) -> bool:
        """建立连接"""
        # Dry run 模式无需真实连接
        if self.dry_run:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
            self._is_connected = True
            logger.info("[模拟] Polymarket API 连接成功 (模拟模式)")
            return True
        
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
            
            # 测试连接
            async with self._session.get(f"{self.api_url}/markets") as response:
                if response.status == 200:
                    self._is_connected = True
                    logger.info("Polymarket API 连接成功")
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"连接失败: {e}")
            return False
    
    async def disconnect(self) -> None:
        """断开连接"""
        if self._session:
            await self._session.close()
            self._session = None
        self._is_connected = False
        logger.info("已断开 Polymarket 连接")
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_markets(
        self,
        limit: int = 100,
        active_only: bool = True
    ) -> List[Dict[str, Any]]:
        """
        获取市场列表
        
        Args:
            limit: 返回数量限制
            active_only: 是否只返回活跃市场
        
        Returns:
            市场列表
        """
        # Dry run 模式返回模拟数据
        if self.dry_run:
            logger.debug(f"[模拟] 获取 {limit} 个市场")
            return self._generate_mock_markets(limit)
        
        if not self._is_connected or not self._session:
            logger.warning("未连接到API")
            return []
        
        try:
            params = {"limit": limit}
            if active_only:
                params["active"] = "true"
            
            async with self._session.get(
                f"{self.api_url}/markets",
                params=params
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data if isinstance(data, list) else []
                
                logger.warning(f"获取市场失败: HTTP {response.status}")
                return []
                
        except Exception as e:
            logger.error(f"获取市场异常: {e}")
            return []
    
    def _generate_mock_markets(self, count: int) -> List[Dict[str, Any]]:
        """生成模拟市场数据用于 dry run - 包含符合策略条件的市场"""
        from datetime import timedelta
        import random
        
        now = datetime.now(timezone.utc)
        
        # 专门设计的市场数据，部分符合策略条件
        markets = [
            # 符合条件的: 高概率(96%), 短期(3天), 高流动性
            {
                "id": "market_endgame_1",
                "question": "Will the Fed announce rate decision by March 22, 2026?",
                "yes_price": 0.96,
                "no_price": 0.04,
                "volume_24h": 250000,
                "liquidity": 150000,
                "end_date": (now + timedelta(days=3)).isoformat(),
                "active": True,
            },
            # 符合条件的: 高概率NO(97%), 短期(5天)
            {
                "id": "market_endgame_2",
                "question": "Will Bitcoin drop below $50k before March 25, 2026?",
                "yes_price": 0.03,
                "no_price": 0.97,
                "volume_24h": 500000,
                "liquidity": 200000,
                "end_date": (now + timedelta(days=5)).isoformat(),
                "active": True,
            },
            # 不符合条件的: 概率不足
            {
                "id": "market_low_prob",
                "question": "Will Bitcoin reach $150,000 by end of 2026?",
                "yes_price": 0.55,
                "no_price": 0.45,
                "volume_24h": 100000,
                "liquidity": 50000,
                "end_date": (now + timedelta(days=180)).isoformat(),
                "active": True,
            },
            # 不符合条件的: 太长时间
            {
                "id": "market_long_term",
                "question": "Will ETH flip BTC market cap in 2027?",
                "yes_price": 0.15,
                "no_price": 0.85,
                "volume_24h": 80000,
                "liquidity": 40000,
                "end_date": (now + timedelta(days=365)).isoformat(),
                "active": True,
            },
            # 符合条件的: 98%概率, 2天
            {
                "id": "market_endgame_3",
                "question": "Will SpaceX complete Starship test by March 21, 2026?",
                "yes_price": 0.98,
                "no_price": 0.02,
                "volume_24h": 180000,
                "liquidity": 120000,
                "end_date": (now + timedelta(days=2)).isoformat(),
                "active": True,
            },
            # 不符合条件的: 流动性不足
            {
                "id": "market_low_liq",
                "question": "Will a new cryptocurrency reach top 10 by April 2026?",
                "yes_price": 0.96,
                "no_price": 0.04,
                "volume_24h": 2000,
                "liquidity": 5000,
                "end_date": (now + timedelta(days=5)).isoformat(),
                "active": True,
            },
            # 随机市场
            {
                "id": "market_random_1",
                "question": "Will Tesla stock exceed $500 in Q2 2026?",
                "yes_price": 0.62,
                "no_price": 0.38,
                "volume_24h": 150000,
                "liquidity": 80000,
                "end_date": (now + timedelta(days=90)).isoformat(),
                "active": True,
            },
            {
                "id": "market_random_2",
                "question": "Will OpenAI release GPT-5 in 2026?",
                "yes_price": 0.45,
                "no_price": 0.55,
                "volume_24h": 200000,
                "liquidity": 100000,
                "end_date": (now + timedelta(days=120)).isoformat(),
                "active": True,
            },
            {
                "id": "market_random_3",
                "question": "Will SOL reach $300 by June 2026?",
                "yes_price": 0.35,
                "no_price": 0.65,
                "volume_24h": 120000,
                "liquidity": 60000,
                "end_date": (now + timedelta(days=100)).isoformat(),
                "active": True,
            },
            {
                "id": "market_random_4",
                "question": "Will crypto market cap exceed $5T in 2026?",
                "yes_price": 0.28,
                "no_price": 0.72,
                "volume_24h": 300000,
                "liquidity": 150000,
                "end_date": (now + timedelta(days=200)).isoformat(),
                "active": True,
            },
        ]
        
        return markets[:count]
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_market(self, market_id: str) -> Optional[Dict[str, Any]]:
        """
        获取单个市场详情
        
        Args:
            market_id: 市场ID
        
        Returns:
            市场详情
        """
        if not self._is_connected or not self._session:
            return None
        
        try:
            async with self._session.get(
                f"{self.api_url}/markets/{market_id}"
            ) as response:
                if response.status == 200:
                    return await response.json()
                return None
                
        except Exception as e:
            logger.error(f"获取市场详情异常: {e}")
            return None
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_market_price(self, market_id: str) -> Optional[Dict[str, Decimal]]:
        """
        获取市场价格
        
        Args:
            market_id: 市场ID
        
        Returns:
            {"yes": Decimal, "no": Decimal}
        """
        # Dry run 模式返回模拟价格
        if self.dry_run:
            import random
            yes_price = Decimal(str(round(random.uniform(0.3, 0.7), 2)))
            return {
                "yes": yes_price,
                "no": Decimal("1") - yes_price
            }
        
        try:
            # 获取订单簿或价格数据
            async with self._session.get(
                f"{self.api_url}/price",
                params={"token_id": market_id}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return {
                        "yes": Decimal(str(data.get("yes_price", 0.5))),
                        "no": Decimal(str(data.get("no_price", 0.5)))
                    }
                return None
                
        except Exception as e:
            logger.error(f"获取价格异常: {e}")
            return None
    
    def parse_market_data(self, market_info: Dict[str, Any]) -> Optional[MarketData]:
        """
        解析市场数据
        
        Args:
            market_info: 原始市场信息
        
        Returns:
            MarketData 对象
        """
        try:
            # 计算距离结算天数
            end_date = market_info.get("end_date")
            if end_date:
                try:
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    days_to_resolution = (end_dt - datetime.now(timezone.utc)).days
                except Exception:
                    days_to_resolution = None
            else:
                days_to_resolution = None
            
            return MarketData(
                market_id=str(market_info.get("id", "")),
                question=market_info.get("question", ""),
                yes_price=Decimal(str(market_info.get("yes_price", 0.5))),
                no_price=Decimal(str(market_info.get("no_price", 0.5))),
                volume_24h=Decimal(str(market_info.get("volume_24h", 0))),
                liquidity=Decimal(str(market_info.get("liquidity", 0))),
                days_to_resolution=days_to_resolution
            )
            
        except Exception as e:
            logger.error(f"解析市场数据异常: {e}")
            return None
    
    @with_retry(max_retries=2, base_delay=0.5)  # 下单重试次数较少，避免重复下单
    async def place_order(
        self,
        market_id: str,
        side: str,
        size: Decimal,
        price: Decimal
    ) -> OrderResult:
        """
        下单
        
        Args:
            market_id: 市场ID
            side: 方向 ("YES" 或 "NO")
            size: 数量
            price: 价格
        
        Returns:
            订单结果
        """
        # 模拟模式
        if self.dry_run:
            logger.info(
                f"[模拟] 下单 | 市场: {market_id} | "
                f"方向: {side} | 数量: {size} | 价格: {price}"
            )
            return OrderResult(
                success=True,
                order_id=f"dry_run_{datetime.now().timestamp()}",
                filled_size=size,
                filled_price=price
            )
        
        # 实盘交易
        if not self._is_connected or not self._session:
            return OrderResult(
                success=False,
                error="未连接到API"
            )
        
        try:
            order_data = {
                "market": market_id,
                "side": side.lower(),
                "size": str(size),
                "price": str(price)
            }
            
            async with self._session.post(
                f"{self.api_url}/order",
                json=order_data
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return OrderResult(
                        success=True,
                        order_id=data.get("order_id"),
                        filled_size=Decimal(str(data.get("filled_size", 0))),
                        filled_price=Decimal(str(data.get("filled_price", 0)))
                    )
                
                error_text = await response.text()
                return OrderResult(
                    success=False,
                    error=f"HTTP {response.status}: {error_text}"
                )
                
        except Exception as e:
            logger.error(f"下单异常: {e}")
            return OrderResult(success=False, error=str(e))
    
    async def cancel_order(self, order_id: str) -> bool:
        """
        取消订单
        
        Args:
            order_id: 订单ID
        
        Returns:
            是否成功
        """
        if self.dry_run:
            logger.info(f"[模拟] 取消订单: {order_id}")
            return True
        
        try:
            async with self._session.delete(
                f"{self.api_url}/order/{order_id}"
            ) as response:
                return response.status == 200
                
        except Exception as e:
            logger.error(f"取消订单异常: {e}")
            return False
    
    async def get_account_balance(self) -> Optional[Decimal]:
        """
        获取账户余额
        
        Returns:
            账户余额 (USD)
        """
        if self.dry_run:
            # 模拟模式返回默认余额
            return Decimal("1000")
        
        try:
            async with self._session.get(
                f"{self.api_url}/balance"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return Decimal(str(data.get("balance", 0)))
                return None
                
        except Exception as e:
            logger.error(f"获取余额异常: {e}")
            return None
    
    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._is_connected
