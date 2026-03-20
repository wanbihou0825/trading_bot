"""
Polymarket 客户端模块
====================
与 Polymarket CLOB API 交互的客户端。

支持:
- Gamma API (市场元数据)
- Data API (用户活动/持仓)
- CLOB API (交易执行，L2 认证)
"""

import asyncio
import functools
import hashlib
import hmac
import time
from decimal import Decimal
from typing import Optional, Dict, List, Any, Callable
from dataclasses import dataclass
from enum import Enum
import aiohttp
from datetime import datetime, timezone
import json

from utils.logger import get_logger
from utils.validation import validate_wallet_address
from strategies.base import MarketData

logger = get_logger(__name__)


# ─── API Endpoints ───

class PolymarketAPI:
    """API Endpoints"""
    GAMMA_API = "https://gamma-api.polymarket.com"  # 市场元数据 (公开)
    DATA_API = "https://data-api.polymarket.com"    # 用户活动/持仓 (公开/轻认证)
    CLOB_API = "https://clob.polymarket.com"        # 交易执行 (需 L2 认证)


class SignatureType(Enum):
    """签名类型"""
    EOA = 0          # MetaMask 等标准钱包
    POLY_PROXY = 1   # Poly Proxy
    POLY_GNOSIS_SAFE = 2  # Gnosis Safe


@dataclass
class ApiCredentials:
    """L2 API 凭证"""
    api_key: str
    api_secret: str
    api_passphrase: str
    

@dataclass
class OrderResult:
    """订单结果"""
    success: bool
    order_id: Optional[str] = None
    filled_size: Decimal = Decimal("0")
    filled_price: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    error: Optional[str] = None
    status: str = "pending"  # pending, matched, partial, failed


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    retryable_exceptions: tuple = (aiohttp.ClientError, asyncio.TimeoutError)
):
    """重试装饰器"""
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
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.warning(
                            f"API调用失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}，"
                            f"{delay:.1f}秒后重试"
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"API调用失败，已达最大重试次数: {e}")
                except Exception as e:
                    logger.error(f"API调用发生不可重试异常: {e}")
                    raise
            
            raise last_exception
        
        return wrapper
    return decorator


class PolymarketClient:
    """
    Polymarket 客户端
    
    集成三个 API:
    - Gamma API: 市场发现和元数据
    - Data API: 用户活动、交易历史、持仓查询
    - CLOB API: 交易执行 (需要 L2 认证)
    """
    
    def __init__(
        self,
        private_key: str = "",
        wallet_address: str = "",
        signature_type: SignatureType = SignatureType.EOA,
        dry_run: bool = True,
        chain_id: int = 137,  # Polygon Mainnet
    ):
        """
        初始化客户端
        
        Args:
            private_key: 钱包私钥 (用于派生 API credentials)
            wallet_address: 钱包地址
            signature_type: 签名类型
            dry_run: 是否为模拟模式
            chain_id: 链 ID (Polygon = 137)
        """
        self.private_key = private_key
        self.wallet_address = wallet_address.lower() if wallet_address else ""
        self.signature_type = signature_type
        self.dry_run = dry_run
        self.chain_id = chain_id
        
        # API sessions
        self._session: Optional[aiohttp.ClientSession] = None
        self._is_connected = False
        
        # L2 API credentials (用于 CLOB 交易)
        self._credentials: Optional[ApiCredentials] = None
        
        logger.info(
            f"Polymarket客户端初始化 | "
            f"模式: {'模拟' if dry_run else '实盘'} | "
            f"钱包: {wallet_address[:10] if wallet_address else 'N/A'}..."
        )
    
    # ═══════════════════════════════════════════════════════════════
    # 连接管理
    # ═══════════════════════════════════════════════════════════════
    
    async def connect(self) -> bool:
        """建立连接并派生 API credentials"""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        
        # Dry run 模式
        if self.dry_run:
            self._is_connected = True
            logger.info("[模拟] Polymarket API 连接成功 (模拟模式)")
            return True
        
        # 实盘模式: 派生 L2 credentials
        try:
            # 测试 Gamma API 连接
            async with self._session.get(
                f"{PolymarketAPI.GAMMA_API}/markets?limit=1"
            ) as response:
                if response.status != 200:
                    raise ConnectionError(f"Gamma API 连接失败: {response.status}")
            
            # 派生 L2 API credentials
            if self.private_key and self.wallet_address:
                self._credentials = await self._derive_api_credentials()
                if self._credentials:
                    logger.info("L2 API credentials 派生成功")
                else:
                    raise RuntimeError("无法派生 L2 API credentials")
            
            self._is_connected = True
            logger.info("Polymarket API 连接成功")
            return True
            
        except Exception as e:
            logger.error(f"连接失败: {e}")
            return False
    
    async def disconnect(self) -> None:
        """断开连接"""
        if self._session:
            await self._session.close()
            self._session = None
        self._is_connected = False
        self._credentials = None
        logger.info("已断开 Polymarket 连接")
    
    @property
    def is_connected(self) -> bool:
        return self._is_connected
    
    # ═══════════════════════════════════════════════════════════════
    # L2 API 认证 (关键修复!)
    # ═══════════════════════════════════════════════════════════════
    
    async def _derive_api_credentials(self) -> Optional[ApiCredentials]:
        """
        派生 L2 API credentials
        
        参考: py-clob-client 的 create_or_derive_api_creds()
        通过 wallet 签名派生 apiKey, secret, passphrase
        """
        try:
            # 1. 生成 nonce/timestamp
            timestamp = int(time.time())
            nonce = f"{timestamp}-{self.wallet_address[:8]}"
            
            # 2. 创建签名消息
            message = self._create_auth_message(nonce)
            
            # 3. 使用私钥签名
            signature = await self._sign_message(message)
            
            # 4. 调用 CLOB API 获取 credentials
            payload = {
                "message": message,
                "signature": signature,
                "wallet": self.wallet_address,
                "timestamp": timestamp,
                "signatureType": self.signature_type.value,
                "chainId": self.chain_id,
            }
            
            async with self._session.post(
                f"{PolymarketAPI.CLOB_API}/auth/api-key",
                json=payload
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    credentials = ApiCredentials(
                        api_key=data.get("apiKey", ""),
                        api_secret=data.get("secret", ""),
                        api_passphrase=data.get("passphrase", ""),
                    )
                    logger.info(f"API credentials 派生成功: {credentials.api_key[:8]}...")
                    return credentials
                else:
                    error_text = await response.text()
                    logger.error(f"派生 credentials 失败: {response.status} - {error_text}")
                    return None
                    
        except Exception as e:
            logger.error(f"派生 API credentials 异常: {e}")
            return None
    
    def _create_auth_message(self, nonce: str) -> str:
        """创建认证消息"""
        return f"Polymarket API Authentication\nNonce: {nonce}\nTimestamp: {int(time.time())}"
    
    async def _sign_message(self, message: str) -> str:
        """
        使用私钥签名消息
        
        使用 eth_account 的标准 EIP-191 签名方法
        """
        try:
            from eth_account import Account
            from eth_account.messages import encode_defunct
            
            # 使用 eth_account 签名
            account = Account.from_key(self.private_key)
            encoded_message = encode_defunct(text=message)
            signed = account.sign_message(encoded_message)
            
            return signed.signature.hex()
            
        except ImportError as e:
            logger.error(f"eth_account 未安装，无法签名: {e}")
            raise RuntimeError(
                "L2 API 认证需要 eth_account 包。"
                "请运行: pip install eth_account>=0.10.0"
            ) from e
        except Exception as e:
            logger.error(f"签名失败: {e}")
            raise

    def _sign_request(
        self,
        method: str,
        path: str,
        body: str = ""
    ) -> Dict[str, str]:
        """
        签名 API 请求
        
        Returns:
            包含认证头的字典
        """
        if not self._credentials:
            return {}
        
        timestamp = str(int(time.time()))
        
        # 创建签名字符串
        message = f"{timestamp}{method}{path}{body}"
        
        # HMAC-SHA256 签名
        signature = hmac.new(
            self._credentials.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return {
            "POLY-API-KEY": self._credentials.api_key,
            "POLY-SIGNATURE": signature,
            "POLY-TIMESTAMP": timestamp,
            "POLY-PASSPHRASE": self._credentials.api_passphrase,
        }
    
    # ═══════════════════════════════════════════════════════════════
    # Gamma API - 市场元数据 (公开)
    # ═══════════════════════════════════════════════════════════════
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_markets(
        self,
        limit: int = 100,
        active_only: bool = True,
        slug: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取市场列表
        
        Args:
            limit: 返回数量限制
            active_only: 是否只返回活跃市场
            slug: 市场 slug (可选)
        """
        if self.dry_run:
            return self._generate_mock_markets(limit)
        
        params = {"limit": limit}
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"
        if slug:
            params["slug"] = slug
        
        async with self._session.get(
            f"{PolymarketAPI.GAMMA_API}/markets",
            params=params
        ) as response:
            if response.status == 200:
                data = await response.json()
                return data if isinstance(data, list) else []
            return []
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_market(self, market_id_or_slug: str) -> Optional[Dict[str, Any]]:
        """获取单个市场详情"""
        async with self._session.get(
            f"{PolymarketAPI.GAMMA_API}/markets/{market_id_or_slug}"
        ) as response:
            if response.status == 200:
                return await response.json()
            return None
    
    async def get_token_ids(self, market_id: str) -> Dict[str, str]:
        """
        获取市场的 token IDs (YES/NO)
        
        Returns:
            {"yes": token_id, "no": token_id}
        """
        market = await self.get_market(market_id)
        if not market:
            return {}
        
        tokens = market.get("tokens", [])
        result = {}
        for token in tokens:
            outcome = token.get("outcome", "").upper()
            if outcome in ["YES", "NO"]:
                result[outcome.lower()] = token.get("token_id", "")
        
        return result
    
    # ═══════════════════════════════════════════════════════════════
    # Data API - 用户活动/持仓 (跟单核心!)
    # ═══════════════════════════════════════════════════════════════
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_user_trades(
        self,
        wallet_address: str,
        market_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        获取用户交易历史 (跟单核心!)
        
        Args:
            wallet_address: 目标钱包地址
            market_id: 市场ID (可选)
            limit: 返回数量
        
        Returns:
            交易列表 [{tx_hash, market_id, side, size, price, type, timestamp}, ...]
        """
        params = {
            "user": wallet_address.lower(),
            "limit": limit,
            "sort": "desc",  # 最新优先
        }
        if market_id:
            params["market"] = market_id
        
        try:
            async with self._session.get(
                f"{PolymarketAPI.DATA_API}/trades",
                params=params
            ) as response:
                if response.status == 200:
                    return await response.json()
                logger.warning(f"获取用户交易失败: {response.status}")
                return []
        except Exception as e:
            logger.error(f"获取用户交易异常: {e}")
            return []
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_user_positions(
        self,
        wallet_address: str,
        market_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取用户当前持仓 (平仓检测核心!)
        
        Args:
            wallet_address: 目标钱包地址
            market_id: 市场ID (可选)
        
        Returns:
            持仓列表 [{market_id, side, size, avg_price, current_value}, ...]
        """
        params = {"user": wallet_address.lower()}
        if market_id:
            params["market"] = market_id
        
        try:
            async with self._session.get(
                f"{PolymarketAPI.DATA_API}/positions",
                params=params
            ) as response:
                if response.status == 200:
                    return await response.json()
                return []
        except Exception as e:
            logger.error(f"获取用户持仓异常: {e}")
            return []
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_user_activity(
        self,
        wallet_address: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        获取用户活动日志
        
        包括: trades, LP, redeem 等
        """
        params = {
            "user": wallet_address.lower(),
            "limit": limit,
        }
        
        async with self._session.get(
            f"{PolymarketAPI.DATA_API}/activity",
            params=params
        ) as response:
            if response.status == 200:
                return await response.json()
            return []
    
    # ═══════════════════════════════════════════════════════════════
    # CLOB API - 价格和订单簿 (公开)
    # ═══════════════════════════════════════════════════════════════
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_market_price(self, market_id: str) -> Optional[Dict[str, Decimal]]:
        """
        获取市场价格
        
        Returns:
            {"yes": Decimal, "no": Decimal, "yes_bid": Decimal, "yes_ask": Decimal, ...}
        """
        if self.dry_run:
            import random
            yes_price = Decimal(str(round(random.uniform(0.3, 0.7), 2)))
            return {
                "yes": yes_price,
                "no": Decimal("1") - yes_price,
            }
        
        try:
            # 获取 token IDs
            token_ids = await self.get_token_ids(market_id)
            if not token_ids:
                return None
            
            yes_token = token_ids.get("yes", "")
            
            # 获取价格
            params = {"token_id": yes_token}
            
            async with self._session.get(
                f"{PolymarketAPI.CLOB_API}/price",
                params=params
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    yes_price = Decimal(str(data.get("price", 0.5)))
                    return {
                        "yes": yes_price,
                        "no": Decimal("1") - yes_price,
                        "yes_bid": Decimal(str(data.get("bid", yes_price))),
                        "yes_ask": Decimal(str(data.get("ask", yes_price))),
                    }
                return None
                
        except Exception as e:
            logger.error(f"获取价格异常: {e}")
            return None
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_orderbook(
        self,
        market_id: str,
        side: str = "YES",
    ) -> Optional[Dict[str, Any]]:
        """
        获取订单簿
        
        Args:
            market_id: 市场ID
            side: 方向 (YES/NO)
        
        Returns:
            {"bids": [{price, size}], "asks": [{price, size}]}
        """
        token_ids = await self.get_token_ids(market_id)
        token_id = token_ids.get(side.lower(), "")
        
        if not token_id:
            return None
        
        async with self._session.get(
            f"{PolymarketAPI.CLOB_API}/book",
            params={"token_id": token_id}
        ) as response:
            if response.status == 200:
                return await response.json()
            return None
    
    async def check_liquidity(
        self,
        market_id: str,
        side: str,
        size: Decimal,
    ) -> Dict[str, Any]:
        """
        检查流动性是否足够
        
        Returns:
            {"sufficient": bool, "available_size": Decimal, "depth": Dict}
        """
        orderbook = await self.get_orderbook(market_id, side)
        
        if not orderbook:
            return {"sufficient": False, "available_size": Decimal("0"), "depth": {}}
        
        # 计算可用流动性 (asks side)
        asks = orderbook.get("asks", [])
        available = Decimal("0")
        depth = {"levels": len(asks)}
        
        for level in asks:
            level_size = Decimal(str(level.get("size", 0)))
            available += level_size
        
        return {
            "sufficient": available >= size,
            "available_size": available,
            "depth": depth,
        }
    
    # ═══════════════════════════════════════════════════════════════
    # CLOB API - 交易执行 (需认证)
    # ═══════════════════════════════════════════════════════════════
    
    @with_retry(max_retries=2, base_delay=0.5)
    async def place_order(
        self,
        market_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        order_type: str = "GTC",  # GTC, GTD, FOK, IOC
    ) -> OrderResult:
        """
        下单 (Limit Order)
        
        Args:
            market_id: 市场ID
            side: 方向 ("YES" 或 "NO")
            size: 数量 (shares)
            price: 价格 (0-1)
            order_type: 订单类型
        
        Returns:
            订单结果
        """
        if self.dry_run:
            logger.info(
                f"[模拟] 下单 | 市场: {market_id} | "
                f"方向: {side} | 数量: {size} | 价格: {price}"
            )
            return OrderResult(
                success=True,
                order_id=f"dry_run_{int(time.time())}",
                filled_size=size,
                filled_price=price,
                status="matched",
            )
        
        # 检查认证
        if not self._credentials:
            return OrderResult(
                success=False,
                error="未认证: 缺少 L2 API credentials"
            )
        
        try:
            # 获取 token ID
            token_ids = await self.get_token_ids(market_id)
            token_id = token_ids.get(side.lower())
            
            if not token_id:
                return OrderResult(
                    success=False,
                    error=f"无法获取 {side} token ID"
                )
            
            # 构建订单
            order_data = {
                "token_id": token_id,
                "side": "BUY",  # 总是 BUY shares
                "size": str(size),
                "price": str(price),
                "expiration": int(time.time()) + 86400,  # 24h
                "type": order_type,
            }
            
            # 签名请求
            body = json.dumps(order_data)
            headers = self._sign_request("POST", "/order", body)
            
            async with self._session.post(
                f"{PolymarketAPI.CLOB_API}/order",
                json=order_data,
                headers=headers
            ) as response:
                if response.status in [200, 201]:
                    data = await response.json()
                    return OrderResult(
                        success=True,
                        order_id=data.get("orderID") or data.get("id"),
                        filled_size=Decimal(str(data.get("sizeMatched", size))),
                        filled_price=Decimal(str(data.get("avgPrice", price))),
                        avg_price=Decimal(str(data.get("avgPrice", price))),
                        status=data.get("status", "matched"),
                    )
                
                error_text = await response.text()
                return OrderResult(
                    success=False,
                    error=f"HTTP {response.status}: {error_text}"
                )
                
        except Exception as e:
            logger.error(f"下单异常: {e}")
            return OrderResult(success=False, error=str(e))
    
    async def place_market_order(
        self,
        market_id: str,
        side: str,
        size: Decimal,
        max_slippage: Decimal = Decimal("0.02"),
    ) -> OrderResult:
        """
        市价单 (FOK/IOC)
        
        快速跟单推荐使用此方法
        
        Args:
            market_id: 市场ID
            side: 方向
            size: 数量
            max_slippage: 最大滑点
        """
        # 获取当前价格
        price_data = await self.get_market_price(market_id)
        if not price_data:
            return OrderResult(success=False, error="无法获取市场价格")
        
        # 根据方向和滑点计算价格
        if side.upper() == "YES":
            price = price_data["yes_ask"] if "yes_ask" in price_data else price_data["yes"]
            price = price * (1 + max_slippage)  # 买入稍高
        else:
            price = price_data["no_ask"] if "no_ask" in price_data else price_data["no"]
            price = price * (1 + max_slippage)
        
        price = min(Decimal("1"), price)  # 不能超过 1
        
        # 使用 IOC (Immediate or Cancel) 订单
        return await self.place_order(
            market_id=market_id,
            side=side,
            size=size,
            price=price,
            order_type="IOC",
        )
    
    async def close_position(
        self,
        market_id: str,
        side: str,
        size: Decimal,
    ) -> OrderResult:
        """
        平仓
        
        Args:
            market_id: 市场ID
            side: 你持有的方向 ("YES" 或 "NO")
            size: 平仓数量
        
        Returns:
            订单结果
        """
        # 平仓 = 卖出持有的 shares = 买入相反方向的 shares
        opposite_side = "NO" if side.upper() == "YES" else "YES"
        
        # 获取当前价格
        price_data = await self.get_market_price(market_id)
        if not price_data:
            return OrderResult(success=False, error="无法获取市场价格")
        
        # 平仓价格
        if side.upper() == "YES":
            # 持有 YES, 卖出 = 买 NO
            price = price_data["no_ask"] if "no_ask" in price_data else price_data["no"]
        else:
            # 持有 NO, 卖出 = 买 YES
            price = price_data["yes_ask"] if "yes_ask" in price_data else price_data["yes"]
        
        logger.info(
            f"平仓 | 市场: {market_id} | "
            f"持仓: {side} | 平仓价格: {price}"
        )
        
        return await self.place_order(
            market_id=market_id,
            side=opposite_side,
            size=size,
            price=price,
            order_type="IOC",
        )
    
    async def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        if self.dry_run:
            return True
        
        headers = self._sign_request("DELETE", f"/order/{order_id}")
        
        async with self._session.delete(
            f"{PolymarketAPI.CLOB_API}/order/{order_id}",
            headers=headers
        ) as response:
            return response.status == 200
    
    async def cancel_all_orders(self) -> bool:
        """取消所有订单"""
        if self.dry_run:
            return True
        
        headers = self._sign_request("DELETE", "/orders")
        
        async with self._session.delete(
            f"{PolymarketAPI.CLOB_API}/orders",
            headers=headers
        ) as response:
            return response.status == 200
    
    # ═══════════════════════════════════════════════════════════════
    # 工具方法
    # ═══════════════════════════════════════════════════════════════
    
    def parse_market_data(self, market_info: Dict[str, Any]) -> Optional[MarketData]:
        """解析市场数据"""
        try:
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
    
    async def get_account_balance(self) -> Optional[Decimal]:
        """获取账户余额"""
        if self.dry_run:
            return Decimal("1000")
        
        # 通过 CLOB API 获取余额
        headers = self._sign_request("GET", "/balance")
        
        async with self._session.get(
            f"{PolymarketAPI.CLOB_API}/balance",
            headers=headers
        ) as response:
            if response.status == 200:
                data = await response.json()
                return Decimal(str(data.get("balance", 0)))
            return None
    
    def _generate_mock_markets(self, count: int) -> List[Dict[str, Any]]:
        """生成模拟市场数据"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        
        markets = [
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
        ]
        return markets[:count]
