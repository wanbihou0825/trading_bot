"""
Polymarket 客户端模块
====================
与 Polymarket CLOB API 交互的客户端。

支持:
- Gamma API (市场元数据)
- Data API (用户活动/持仓)
- CLOB API (交易执行，L2 认证 — 通过官方 py-clob-client)
"""

import asyncio
import functools
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
    - CLOB API: 交易执行 (通过官方 py-clob-client 处理 L2 认证和订单签名)
    """
    
    def __init__(
        self,
        private_key: str = "",
        wallet_address: str = "",
        signature_type: SignatureType = SignatureType.POLY_GNOSIS_SAFE,
        funder_address: str = "",
        dry_run: bool = True,
        chain_id: int = 137,
    ):
        """
        初始化客户端

        Args:
            private_key: 钱包私钥 (用于派生 API credentials，通常是 MetaMask EOA 私钥)
            wallet_address: 签名地址 (通常是 MetaMask EOA 地址，用于签名)
            signature_type: 签名类型 (默认 2 = Gnosis Safe Proxy)
            funder_address: Proxy wallet 地址 (资金存放地址，从 polymarket.com/settings 获取)
            dry_run: 是否为模拟模式
            chain_id: 链 ID (Polygon = 137)
        """
        self.private_key = private_key
        # 保留 lowercase 用于内部比较和 Data API 查询
        self.wallet_address = wallet_address.lower() if wallet_address else ""
        self.signature_type = signature_type
        self.funder_address = funder_address.lower() if funder_address else self.wallet_address
        self.dry_run = dry_run
        self.chain_id = chain_id

        # API sessions
        self._session: Optional[aiohttp.ClientSession] = None
        self._is_connected = False

        # L2 API credentials (用于 CLOB 交易)
        self._credentials: Optional[ApiCredentials] = None

        # 官方 py-clob-client 实例 (处理 EIP-712 签名和订单构建)
        self._clob_client = None

        # 日志显示地址信息
        eoa_addr = wallet_address[:10] + "..." if wallet_address else "N/A"
        proxy_addr = (funder_address[:10] + "...") if funder_address else eoa_addr

        logger.info(
            f"Polymarket客户端初始化 | "
            f"模式: {'模拟' if dry_run else '实盘'} | "
            f"签名类型: {signature_type.name} ({signature_type.value}) | "
            f"EOA地址: {eoa_addr} | "
            f"Proxy地址: {proxy_addr}"
        )

        if signature_type == SignatureType.POLY_GNOSIS_SAFE:
            logger.info(
                "✓ 使用 Gnosis Safe Proxy 模式\n"
                "  资金存放在 Proxy 地址，交易 gasless 执行"
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

        # 实盘模式: 派生 L2 credentials（带重试）
        try:
            # 先验证配置
            if not self.private_key:
                raise ValueError("未配置私钥 (PRIVATE_KEY)")
            if not self.wallet_address:
                raise ValueError("未配置钱包地址 (WALLET_ADDRESS)")

            logger.info("开始连接 Polymarket API...")

            # 验证私钥格式
            from eth_account import Account
            try:
                account = Account.from_key(self.private_key)
                logger.info("✓ 私钥验证成功")
                logger.info(f"✓ 私钥对应地址: {account.address}")

                if account.address.lower() != self.wallet_address.lower():
                    raise ValueError(
                        f"私钥地址与配置地址不匹配!\n"
                        f"私钥地址: {account.address}\n"
                        f"配置地址: {self.wallet_address}\n"
                        f"请检查 .env 文件中的 WALLET_ADDRESS 和 PRIVATE_KEY"
                    )
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(f"私钥格式错误: {e}")

            # 测试 Gamma API 连接
            logger.info("测试 Gamma API 连接...")
            async with self._session.get(
                f"{PolymarketAPI.GAMMA_API}/markets?limit=1",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status != 200:
                    raise ConnectionError(f"Gamma API 连接失败: {response.status}")
                logger.info("✓ Gamma API 连接成功")

            # 初始化官方 ClobClient 并派生 L2 credentials（重试3次）
            max_retries = 3
            last_error = None
            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"派生 L2 API credentials (尝试 {attempt}/{max_retries})...")
                    self._credentials = await asyncio.wait_for(
                        self._derive_api_credentials(),
                        timeout=30
                    )
                    if self._credentials:
                        break
                    last_error = RuntimeError("derive 返回空")
                except asyncio.TimeoutError:
                    last_error = asyncio.TimeoutError("L2 credential derivation 超时 (30s)")
                    logger.warning(f"L2 派生超时 (尝试 {attempt}/{max_retries})")
                except Exception as e:
                    last_error = e
                    logger.warning(f"L2 派生失败 (尝试 {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    delay = 2 ** attempt
                    logger.info(f"{delay}s 后重试...")
                    await asyncio.sleep(delay)

            if not self._credentials:
                raise RuntimeError(f"无法派生 L2 API credentials (已重试{max_retries}次): {last_error}")

            self._is_connected = True
            logger.info("✓ Polymarket API 连接成功")
            return True

        except ValueError as e:
            logger.error(f"配置错误: {e}")
            logger.error("请检查 .env 文件中的配置")
            await self._cleanup_session_on_failure()
            return False
        except Exception as e:
            logger.error(f"连接失败: {e}")
            import traceback
            logger.error(f"异常详情:\n{traceback.format_exc()}")
            await self._cleanup_session_on_failure()
            return False
    
    async def _cleanup_session_on_failure(self) -> None:
        """connect 失败时清理 session"""
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        self._is_connected = False
    
    async def disconnect(self) -> None:
        """断开连接"""
        self._is_connected = False
        self._credentials = None
        self._clob_client = None
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("已断开 Polymarket 连接")
    
    def _check_session(self) -> aiohttp.ClientSession:
        """检查 session 可用性"""
        if not self._is_connected or not self._session:
            raise RuntimeError("未连接到 Polymarket API，请先调用 connect()")
        return self._session
    
    @property
    def is_connected(self) -> bool:
        return self._is_connected
    
    # ═══════════════════════════════════════════════════════════════
    # L2 API 认证 (使用官方 py-clob-client)
    # ═══════════════════════════════════════════════════════════════
    
    async def _derive_api_credentials(self) -> Optional[ApiCredentials]:
        """
        使用官方 py-clob-client 派生 L2 API credentials

        内部流程 (由 py-clob-client 自动处理):
        1. 构造 EIP-712 结构化签名 (ClobAuth domain)
        2. 使用正确的 header 名称 (POLY_ADDRESS 等)
        3. 请求 /auth/derive-api-key 端点
        """
        try:
            from py_clob_client.client import ClobClient

            # 构建 funder 参数 (Gnosis Safe 模式需要)
            funder = None
            if self.signature_type == SignatureType.POLY_GNOSIS_SAFE and self.funder_address:
                funder = self.funder_address
                logger.info(f"使用 Proxy 地址作为 funder: {funder[:10]}...")

            # 初始化官方客户端
            self._clob_client = ClobClient(
                host=PolymarketAPI.CLOB_API,
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=self.signature_type.value,
                funder=funder,
            )

            logger.info("正在通过 py-clob-client 派生 credentials...")

            # 在线程中运行同步调用 (py-clob-client 使用 requests 库)
            creds = await asyncio.to_thread(self._clob_client.derive_api_key)

            if not creds:
                logger.warning("derive_api_key 返回空，尝试 create_or_derive...")
                creds = await asyncio.to_thread(self._clob_client.create_or_derive_api_creds)

            if creds:
                # 将 credentials 设置到 clob_client 上，后续订单签名需要
                self._clob_client.set_api_creds(creds)

                credentials = ApiCredentials(
                    api_key=creds.api_key,
                    api_secret=creds.api_secret,
                    api_passphrase=creds.api_passphrase,
                )
                logger.info(f"✓ API credentials 派生成功: {credentials.api_key[:8]}...")
                return credentials
            else:
                logger.error("派生 credentials 失败: py-clob-client 返回空")
                return None

        except Exception as e:
            logger.error(f"派生 API credentials 异常: {e}")
            import traceback
            logger.error(f"异常堆栈: {traceback.format_exc()}")
            return None

    def _get_l2_headers(self) -> Dict[str, str]:
        """
        生成 L2 认证请求头 (用于非订单类的认证 API 调用)
        """
        if not self._clob_client or not self._credentials:
            return {}

        try:
            # py-clob-client 内部通过 set_api_creds 已设置 creds
            # 直接使用 HMAC 签名构造 headers
            import hmac
            import hashlib
            timestamp = str(int(time.time()))
            method = "GET"
            request_path = "/data/balance"
            body = ""
            message = f"{timestamp}{method}{request_path}{body}"
            signature = hmac.new(
                self._credentials.api_secret.encode(),
                message.encode(),
                hashlib.sha256
            ).hexdigest()
            return {
                "POLY_API_KEY": self._credentials.api_key,
                "POLY_SIGNATURE": signature,
                "POLY_TIMESTAMP": timestamp,
                "POLY_PASSPHRASE": self._credentials.api_passphrase,
            }
        except Exception as e:
            logger.error(f"生成 L2 headers 失败: {e}")
            return {}
    
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
        """
        if self.dry_run:
            return self._generate_mock_markets(limit)
        
        params = {"limit": limit}
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"
        if slug:
            params["slug"] = slug
        
        session = self._check_session()
        async with session.get(
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
        session = self._check_session()
        async with session.get(
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
        """获取用户交易历史 (跟单核心!)"""
        params = {
            "user": wallet_address.lower(),
            "limit": limit,
            "sort": "desc",
        }
        if market_id:
            params["market"] = market_id
        
        try:
            session = self._check_session()
            async with session.get(
                f"{PolymarketAPI.DATA_API}/trades",
                params=params,
                timeout=aiohttp.ClientTimeout(total=10)
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
        """获取用户当前持仓 (平仓检测核心!)"""
        params = {"user": wallet_address.lower()}
        if market_id:
            params["market"] = market_id
        
        try:
            session = self._check_session()
            async with session.get(
                f"{PolymarketAPI.DATA_API}/positions",
                params=params,
                timeout=aiohttp.ClientTimeout(total=10)
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
        """获取用户活动日志"""
        params = {
            "user": wallet_address.lower(),
            "limit": limit,
        }
        
        session = self._check_session()
        async with session.get(
            f"{PolymarketAPI.DATA_API}/activity",
            params=params,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            if response.status == 200:
                return await response.json()
            return []
    
    # ═══════════════════════════════════════════════════════════════
    # CLOB API - 价格和订单簿 (公开)
    # ═══════════════════════════════════════════════════════════════
    
    @with_retry(max_retries=3, base_delay=1.0)
    async def get_market_price(self, market_id: str) -> Optional[Dict[str, Decimal]]:
        """获取市场价格"""
        if self.dry_run:
            import random
            yes_price = Decimal(str(round(random.uniform(0.3, 0.7), 2)))
            return {
                "yes": yes_price,
                "no": Decimal("1") - yes_price,
            }
        
        try:
            token_ids = await self.get_token_ids(market_id)
            if not token_ids:
                return None
            
            yes_token = token_ids.get("yes", "")
            params = {"token_id": yes_token}
            
            session = self._check_session()
            async with session.get(
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
        """获取订单簿"""
        token_ids = await self.get_token_ids(market_id)
        token_id = token_ids.get(side.lower(), "")
        
        if not token_id:
            return None
        
        session = self._check_session()
        async with session.get(
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
        """检查流动性是否足够"""
        orderbook = await self.get_orderbook(market_id, side)
        
        if not orderbook:
            return {"sufficient": False, "available_size": Decimal("0"), "depth": {}}
        
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
    # CLOB API - 交易执行 (通过 py-clob-client 签名)
    # ═══════════════════════════════════════════════════════════════
    
    @with_retry(max_retries=2, base_delay=0.5)
    async def place_order(
        self,
        market_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        order_type: str = "GTC",
    ) -> OrderResult:
        """
        下单 (通过 py-clob-client 处理 EIP-712 订单签名)
        
        Args:
            market_id: 市场ID
            side: 方向 ("YES" 或 "NO")
            size: 数量 (shares)
            price: 价格 (0-1)
            order_type: 订单类型 (GTC, GTD, FOK)
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
        
        if not self._clob_client or not self._credentials:
            return OrderResult(
                success=False,
                error="未认证: 缺少 L2 API credentials 或 ClobClient 未初始化"
            )
        
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType as ClobOrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            # 获取 token ID
            token_ids = await self.get_token_ids(market_id)
            token_id = token_ids.get(side.lower())
            
            if not token_id:
                return OrderResult(
                    success=False,
                    error=f"无法获取 {side} token ID"
                )
            
            # 构建订单参数 — BUY 用于开仓买入，SELL 由 close_position 使用
            order_args = OrderArgs(
                price=float(price),
                size=float(size),
                side=BUY,
                token_id=token_id,
            )
            
            # 映射订单类型
            clob_order_type = ClobOrderType.GTC
            if order_type == "FOK":
                clob_order_type = ClobOrderType.FOK
            elif order_type == "GTD":
                clob_order_type = ClobOrderType.GTD

            logger.info(
                f"下单 | 市场: {market_id} | 方向: {side} | "
                f"数量: {size} | 价格: {price} | 类型: {order_type}"
            )

            # 在线程中执行 (py-clob-client 使用同步 requests)
            resp = await asyncio.to_thread(
                self._clob_client.create_and_post_order,
                order_args,
                clob_order_type,
            )

            if resp:
                order_id = resp.get("orderID") or resp.get("id", "")
                status = resp.get("status", "live")
                size_matched = resp.get("sizeMatched", "0")
                
                logger.info(f"✓ 下单成功 | 订单ID: {order_id} | 状态: {status}")
                
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    filled_size=Decimal(str(size_matched)) if size_matched else Decimal("0"),
                    filled_price=price,
                    avg_price=price,
                    status=status,
                )
            else:
                return OrderResult(success=False, error="py-clob-client 返回空响应")
                
        except Exception as e:
            logger.error(f"下单异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return OrderResult(success=False, error=str(e))
    
    async def place_market_order(
        self,
        market_id: str,
        side: str,
        size: Decimal,
        max_slippage: Decimal = Decimal("0.02"),
    ) -> OrderResult:
        """
        市价单 (FOK)
        
        快速跟单推荐使用此方法
        """
        price_data = await self.get_market_price(market_id)
        if not price_data:
            return OrderResult(success=False, error="无法获取市场价格")
        
        if side.upper() == "YES":
            price = price_data["yes_ask"] if "yes_ask" in price_data else price_data["yes"]
            price = price * (1 + max_slippage)
        else:
            price = price_data["no_ask"] if "no_ask" in price_data else price_data["no"]
            price = price * (1 + max_slippage)
        
        price = min(Decimal("1"), price)
        
        return await self.place_order(
            market_id=market_id,
            side=side,
            size=size,
            price=price,
            order_type="FOK",
        )
    
    async def close_position(
        self,
        market_id: str,
        side: str,
        size: Decimal,
    ) -> OrderResult:
        """
        平仓 — 卖出持有的 shares
        
        Args:
            market_id: 市场ID
            side: 你持有的方向 ("YES" 或 "NO")
            size: 平仓数量
        """
        if self.dry_run:
            logger.info(f"[模拟] 平仓 | 市场: {market_id} | 持仓: {side} | 数量: {size}")
            return OrderResult(
                success=True,
                order_id=f"dry_run_close_{int(time.time())}",
                filled_size=size,
                status="matched",
            )

        if not self._clob_client or not self._credentials:
            return OrderResult(
                success=False,
                error="未认证: 缺少 L2 API credentials"
            )

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType as ClobOrderType
            from py_clob_client.order_builder.constants import SELL

            # 获取持有方向的 token ID
            token_ids = await self.get_token_ids(market_id)
            token_id = token_ids.get(side.lower())
            
            if not token_id:
                return OrderResult(success=False, error=f"无法获取 {side} token ID")

            # 获取当前价格作为卖出参考
            price_data = await self.get_market_price(market_id)
            if not price_data:
                return OrderResult(success=False, error="无法获取市场价格")

            # 使用 bid 价格（买方最高出价），更接近实际成交价
            side_lower = side.lower()
            bid_key = f"{side_lower}_bid"
            current_price = price_data.get(bid_key, price_data.get(side_lower, Decimal("0.5")))
            sell_price = float(current_price * Decimal("0.98"))  # 允许 2% 滑点
            sell_price = max(0.01, min(0.99, sell_price))  # 限制在合理范围

            order_args = OrderArgs(
                price=sell_price,
                size=float(size),
                side=SELL,
                token_id=token_id,
            )

            logger.info(
                f"平仓 | 市场: {market_id} | 持仓: {side} | "
                f"数量: {size} | 卖出价格: {sell_price:.4f}"
            )

            resp = await asyncio.to_thread(
                self._clob_client.create_and_post_order,
                order_args,
                ClobOrderType.FOK,
            )

            if resp:
                order_id = resp.get("orderID") or resp.get("id", "")
                status = resp.get("status", "live")
                logger.info(f"✓ 平仓成功 | 订单ID: {order_id} | 状态: {status}")
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    filled_size=Decimal(str(resp.get("sizeMatched", size))),
                    filled_price=Decimal(str(sell_price)),
                    status=status,
                )
            else:
                return OrderResult(success=False, error="平仓响应为空")

        except Exception as e:
            logger.error(f"平仓异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return OrderResult(success=False, error=str(e))
    
    async def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        if self.dry_run:
            return True

        if not self._clob_client:
            logger.error("ClobClient 未初始化，无法取消订单")
            return False

        try:
            resp = await asyncio.to_thread(self._clob_client.cancel, order_id)
            return bool(resp)
        except Exception as e:
            logger.error(f"取消订单异常: {e}")
            return False
    
    async def cancel_all_orders(self) -> bool:
        """取消所有订单"""
        if self.dry_run:
            return True

        if not self._clob_client:
            logger.error("ClobClient 未初始化，无法取消订单")
            return False

        try:
            resp = await asyncio.to_thread(self._clob_client.cancel_all)
            return bool(resp)
        except Exception as e:
            logger.error(f"取消所有订单异常: {e}")
            return False
    
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
        """
        获取账户 USDC 余额

        通过链上查询 Proxy 地址 (funder) 的 USDC 余额。
        Polymarket 使用 Polygon 上的 USDC (PoS)。
        注意: 需要先在 polymarket.com 存入资金，直接转 USDC 到 funder 地址不一定可用。
        """
        if self.dry_run:
            return Decimal("1000")

        if not self._is_connected or not self._session:
            logger.warning("未连接到 Polymarket API，无法获取余额")
            return None

        address_to_query = self.funder_address if self.signature_type == SignatureType.POLY_GNOSIS_SAFE else self.wallet_address

        logger.debug(f"查询余额地址: {address_to_query[:10]}...")

        # 方法1: 通过 py-clob-client 查询 CTF Exchange 余额
        if self._clob_client:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                balance_info = await asyncio.to_thread(
                    self._clob_client.get_balance_allowance, params
                )
                if balance_info:
                    # py-clob-client 返回的 balance 已是字符串格式的原始值 (6 decimals)
                    raw = balance_info.get("balance", "0") if isinstance(balance_info, dict) else getattr(balance_info, "balance", "0")
                    raw_val = int(str(raw))
                    balance = Decimal(raw_val) / Decimal("1000000")
                    logger.info(f"账户余额: ${balance:.2f} (地址: {address_to_query[:10]}...)")
                    return balance
            except ImportError:
                logger.debug("py-clob-client 无 BalanceAllowanceParams，尝试备用方式")
            except Exception as e:
                logger.debug(f"通过 py-clob-client 查余额失败: {e}, 尝试备用方式")

        # 方法2: 直接调 CLOB API /balance-allowance 端点
        try:
            headers = self._get_l2_headers()
            params = {
                "asset_type": "COLLATERAL",
                "signature_type": str(self.signature_type.value),
            }
            session = self._check_session()
            async with session.get(
                f"{PolymarketAPI.CLOB_API}/balance-allowance",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    raw = data.get("balance", "0")
                    raw_val = int(str(raw))
                    balance = Decimal(raw_val) / Decimal("1000000")
                    logger.info(f"账户余额: ${balance:.2f} (地址: {address_to_query[:10]}...)")
                    return balance
                else:
                    error_text = await response.text()
                    logger.warning(f"CLOB 余额查询失败: {response.status} - {error_text[:200]}")
        except Exception as e:
            logger.debug(f"CLOB 余额查询异常: {e}")

        # 方法3: 链上查 Proxy 地址的 USDC 裸余额 (兜底)
        try:
            usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            data = f"0x70a08231000000000000000000000000{address_to_query[2:].lower()}"
            async with session.post(
                "https://polygon-rpc.com",
                json={
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{"to": usdc_address, "data": data}, "latest"],
                    "id": 1,
                },
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    hex_balance = result.get("result", "0x0")
                    raw_balance = int(hex_balance, 16)
                    balance = Decimal(raw_balance) / Decimal("1000000")
                    logger.info(f"账户余额 (链上USDC): ${balance:.2f} (地址: {address_to_query[:10]}...)")
                    return balance
        except Exception as e:
            logger.error(f"获取余额异常: {e}")

        logger.error("所有余额查询方式均失败")
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