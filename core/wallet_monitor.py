"""
钱包监控器
==========
实时监控目标钱包的交易活动。
支持轮询和WebSocket两种模式。
"""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List, Dict, Any, Callable, Set
from datetime import datetime, timezone, timedelta
from enum import Enum
import aiohttp
import json

from utils.logger import get_logger
from utils.validation import validate_wallet_address

logger = get_logger(__name__)


class MonitorMode(Enum):
    """监控模式"""
    POLLING = "polling"      # 轮询模式
    WEBSOCKET = "websocket"  # WebSocket实时模式
    HYBRID = "hybrid"        # 混合模式


@dataclass
class WalletTransaction:
    """钱包交易记录"""
    tx_hash: str
    wallet_address: str
    market_id: str
    market_question: str
    side: str  # "YES" or "NO"
    size: Decimal
    price: Decimal
    timestamp: datetime
    tx_type: str  # "buy", "sell"
    pnl: Optional[Decimal] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "tx_hash": self.tx_hash,
            "wallet_address": self.wallet_address,
            "market_id": self.market_id,
            "market_question": self.market_question,
            "side": self.side,
            "size": str(self.size),
            "price": str(self.price),
            "timestamp": self.timestamp.isoformat(),
            "tx_type": self.tx_type,
            "pnl": str(self.pnl) if self.pnl else None,
        }


@dataclass
class WalletInfo:
    """钱包信息"""
    address: str
    alias: Optional[str] = None
    quality_score: Optional[Decimal] = None
    tier: Optional[str] = None
    is_market_maker: bool = False
    last_checked: Optional[datetime] = None
    total_trades: int = 0
    last_trade: Optional[datetime] = None
    enabled: bool = True


class WalletMonitor:
    """
    钱包监控器
    
    功能:
    1. 监控多个目标钱包的交易活动
    2. 支持轮询和WebSocket两种模式
    3. 自动发现和过滤交易
    4. 触发跟单回调
    """
    
    POLYGONSCAN_API = "https://api.polygonscan.com/api"
    
    def __init__(
        self,
        polygonscan_api_key: str,
        mode: MonitorMode = MonitorMode.HYBRID,
        poll_interval: int = 30,
        ws_url: str = "",  # 留空，默认禁用WebSocket，使用轮询模式
    ):
        """
        初始化钱包监控器
        
        Args:
            polygonscan_api_key: Polygonscan API密钥
            mode: 监控模式
            poll_interval: 轮询间隔(秒)
            ws_url: WebSocket URL
        """
        self.api_key = polygonscan_api_key
        self.mode = mode
        self.poll_interval = poll_interval
        self.ws_url = ws_url
        
        # 监控的钱包列表
        self._wallets: Dict[str, WalletInfo] = {}
        
        # 交易回调
        self._trade_callbacks: List[Callable] = []
        
        # 已处理的交易缓存
        self._processed_txs: Set[str] = set()
        self._max_cache_size = 10000
        
        # 运行状态
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        
        # 混合模式任务跟踪
        self._hybrid_tasks: List[asyncio.Task] = []
        
        logger.info(
            f"钱包监控器初始化 | 模式: {mode.value} | "
            f"轮询间隔: {poll_interval}s"
        )
    
    def add_wallet(
        self,
        address: str,
        alias: Optional[str] = None,
        quality_score: Optional[Decimal] = None,
        tier: Optional[str] = None,
        is_market_maker: bool = False,
    ) -> bool:
        """
        添加监控钱包
        
        Args:
            address: 钱包地址
            alias: 别名
            quality_score: 质量评分
            tier: 等级
            is_market_maker: 是否做市商
        
        Returns:
            是否添加成功
        """
        if not validate_wallet_address(address):
            logger.error(f"无效的钱包地址: {address}")
            return False
        
        if address.lower() in self._wallets:
            logger.warning(f"钱包已存在: {address[:10]}...")
            return False
        
        wallet_info = WalletInfo(
            address=address.lower(),
            alias=alias,
            quality_score=quality_score,
            tier=tier,
            is_market_maker=is_market_maker,
        )
        
        self._wallets[address.lower()] = wallet_info
        
        logger.info(
            f"添加监控钱包 | {address[:10]}... | "
            f"别名: {alias or 'N/A'} | 等级: {tier or 'N/A'}"
        )
        
        return True
    
    def remove_wallet(self, address: str) -> bool:
        """移除监控钱包"""
        if address.lower() in self._wallets:
            del self._wallets[address.lower()]
            logger.info(f"移除监控钱包: {address[:10]}...")
            return True
        return False
    
    def get_wallets(self) -> List[WalletInfo]:
        """获取所有监控的钱包"""
        return list(self._wallets.values())
    
    def on_trade(self, callback: Callable) -> None:
        """注册交易回调"""
        self._trade_callbacks.append(callback)
    
    async def start(self) -> None:
        """启动监控"""
        if self._running:
            logger.warning("监控器已在运行")
            return
        
        self._running = True
        self._session = aiohttp.ClientSession()
        
        logger.info(f"钱包监控器启动 | 模式: {self.mode.value}")
        
        try:
            if self.mode == MonitorMode.WEBSOCKET:
                await self._run_websocket()
            elif self.mode == MonitorMode.POLLING:
                await self._run_polling()
            else:  # HYBRID
                await self._run_hybrid()
        except Exception as e:
            logger.error(f"监控器异常: {e}")
        finally:
            await self.stop()
    
    async def stop(self) -> None:
        """停止监控"""
        self._running = False
        
        # 取消混合模式任务
        if self._hybrid_tasks:
            logger.debug(f'正在取消{len(self._hybrid_tasks)}个混合模式任务...')
            for task in self._hybrid_tasks:
                if not task.done():
                    task.cancel()
            # 等待任务取消完成
            try:
                await asyncio.gather(*self._hybrid_tasks, return_exceptions=True)
            except Exception as e:
                logger.debug(f'任务取消异常(忽略): {e}')
            self._hybrid_tasks = []
        
        if self._ws:
            await self._ws.close()
            self._ws = None
        
        if self._session:
            await self._session.close()
            self._session = None
        
        logger.info("钱包监控器已停止")
    
    async def _run_polling(self) -> None:
        """轮询模式"""
        while self._running:
            try:
                await self._poll_wallets()
                # 分次睡眠，每1秒检查一次运行状态
                for _ in range(self.poll_interval):
                    if not self._running:
                        break
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"轮询异常: {e}")
                if self._running:
                    for _ in range(5):
                        if not self._running:
                            break
                        await asyncio.sleep(1)
    
    async def _run_websocket(self) -> None:
        """WebSocket模式"""
        # 如果ws_url为空，直接回退到轮询模式
        if not self.ws_url:
            logger.warning("WebSocket URL未配置，自动回退到轮询模式")
            await self._run_polling()
            return
        
        retry_count = 0
        max_retries = 5
        
        while self._running and retry_count < max_retries:
            try:
                await self._connect_websocket()
                retry_count = 0  # 重置重试计数
            except Exception as e:
                retry_count += 1
                logger.error(f"WebSocket连接失败 ({retry_count}/{max_retries}): {e}")
                if self._running:
                    # 使用更短的睡眠间隔，并更频繁检查运行状态
                    sleep_time = min(30, 5 * retry_count)
                    for _ in range(int(sleep_time)):
                        if not self._running:
                            break
                        await asyncio.sleep(1)
        
        # WebSocket失败后回退到轮询
        if retry_count >= max_retries and self._running:
            logger.warning("WebSocket连接失败，回退到轮询模式")
            await self._run_polling()
    
    async def _run_hybrid(self) -> None:
        """混合模式: WebSocket为主，轮询为辅"""
        # 如果ws_url为空，只运行轮询任务
        if not self.ws_url:
            logger.warning("WebSocket URL未配置，混合模式自动回退到轮询模式")
            await self._run_polling()
            return
        
        # 创建两个任务
        ws_task = asyncio.create_task(self._run_websocket())
        poll_task = asyncio.create_task(self._periodic_poll())
        
        # 保存任务引用
        self._hybrid_tasks = [ws_task, poll_task]
        
        try:
            await asyncio.gather(ws_task, poll_task)
        except Exception as e:
            logger.error(f"混合模式异常: {e}")
        finally:
            self._hybrid_tasks = []
    
    async def _periodic_poll(self) -> None:
        """定期轮询(作为WebSocket的补充)"""
        while self._running:
            # 分次睡眠，每1秒检查一次运行状态
            sleep_time = self.poll_interval * 10
            for _ in range(sleep_time):
                if not self._running:
                    break
                await asyncio.sleep(1)
            
            if not self._running:
                break
                
            try:
                await self._poll_wallets()
            except Exception as e:
                logger.error(f"定期轮询异常: {e}")
    
    async def _connect_websocket(self) -> None:
        """连接WebSocket"""
        if not self._session:
            return
        
        # 如果ws_url为空，抛出异常
        if not self.ws_url:
            raise ValueError("WebSocket URL未配置，请使用轮询模式或配置有效的WebSocket URL")
        
        logger.info(f"连接WebSocket: {self.ws_url}")
        
        async with self._session.ws_connect(self.ws_url) as ws:
            self._ws = ws
            logger.info("WebSocket连接成功")
            
            # 订阅钱包地址
            for address in self._wallets.keys():
                subscribe_msg = {
                    "method": "eth_subscribe",
                    "params": ["logs", {"address": address}],
                    "id": 1
                }
                await ws.send_json(subscribe_msg)
            
            # 接收消息
            async for msg in ws:
                if not self._running:
                    break
                
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_ws_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket错误: {ws.exception()}")
                    break
    
    async def _handle_ws_message(self, data: str) -> None:
        """处理WebSocket消息"""
        try:
            msg = json.loads(data)
            
            # 解析交易数据
            if msg.get("method") == "eth_subscription":
                params = msg.get("params", {})
                await self._process_ws_transaction(params)
                
        except json.JSONDecodeError:
            logger.warning(f"无效的WebSocket消息: {data[:100]}")
        except Exception as e:
            logger.error(f"处理WebSocket消息异常: {e}")
    
    async def _process_ws_transaction(self, params: Dict[str, Any]) -> None:
        """处理WebSocket交易"""
        # 提取交易信息
        log = params.get("result", {})
        tx_hash = log.get("transactionHash")
        
        if not tx_hash or tx_hash in self._processed_txs:
            return
        
        self._processed_txs.add(tx_hash)
        self._cleanup_cache()
        
        # 获取完整交易详情
        tx_details = await self._get_transaction_details(tx_hash)
        if tx_details:
            await self._notify_trade(tx_details)
    
    async def _poll_wallets(self) -> None:
        """轮询所有钱包"""
        for address, wallet_info in self._wallets.items():
            if not wallet_info.enabled:
                continue
            
            try:
                txs = await self._fetch_wallet_transactions(address)
                
                for tx in txs:
                    if tx.tx_hash not in self._processed_txs:
                        self._processed_txs.add(tx.tx_hash)
                        await self._notify_trade(tx)
                
                wallet_info.last_checked = datetime.now(timezone.utc)
                
            except Exception as e:
                logger.error(f"轮询钱包 {address[:10]}... 异常: {e}")
    
    async def _fetch_wallet_transactions(
        self,
        address: str,
        limit: int = 10
    ) -> List[WalletTransaction]:
        """获取钱包交易记录"""
        if not self._session:
            return []
        
        try:
            # 调用Polygonscan API
            params = {
                "module": "account",
                "action": "tokentx",
                "address": address,
                "contractaddress": "0x...",  # Polymarket合约地址
                "page": 1,
                "offset": limit,
                "sort": "desc",
                "apikey": self.api_key,
            }
            
            async with self._session.get(
                self.POLYGONSCAN_API,
                params=params
            ) as response:
                if response.status != 200:
                    logger.warning(f"API请求失败: {response.status}")
                    return []
                
                data = await response.json()
                
                if data.get("status") != "1":
                    return []
                
                # 解析交易
                transactions = []
                for item in data.get("result", []):
                    tx = self._parse_transaction(address, item)
                    if tx:
                        transactions.append(tx)
                
                return transactions
                
        except Exception as e:
            logger.error(f"获取交易记录异常: {e}")
            return []
    
    def _parse_transaction(
        self,
        address: str,
        item: Dict[str, Any]
    ) -> Optional[WalletTransaction]:
        """解析交易数据"""
        try:
            # 简化解析，实际需要根据Polymarket合约事件解析
            tx = WalletTransaction(
                tx_hash=item.get("hash", ""),
                wallet_address=address,
                market_id=item.get("tokenID", ""),
                market_question=item.get("tokenSymbol", ""),
                side="YES",  # 需要从事件数据解析
                size=Decimal(item.get("value", "0")) / Decimal("10**6"),
                price=Decimal("0.5"),  # 需要从事件数据解析
                timestamp=datetime.fromtimestamp(int(item.get("timeStamp", 0))),
                tx_type="buy" if item.get("to", "").lower() == address.lower() else "sell",
            )
            return tx
        except Exception as e:
            logger.error(f"解析交易异常: {e}")
            return None
    
    async def _get_transaction_details(self, tx_hash: str) -> Optional[WalletTransaction]:
        """获取交易详情"""
        # 实际实现需要调用API获取完整交易详情
        return None
    
    async def _notify_trade(self, tx: WalletTransaction) -> None:
        """通知交易回调"""
        wallet_info = self._wallets.get(tx.wallet_address.lower())
        if wallet_info:
            wallet_info.last_trade = tx.timestamp
            wallet_info.total_trades += 1
        
        for callback in self._trade_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(tx)
                else:
                    callback(tx)
            except Exception as e:
                logger.error(f"回调执行异常: {e}")
    
    def _cleanup_cache(self) -> None:
        """清理缓存"""
        if len(self._processed_txs) > self._max_cache_size:
            # 移除一半的旧记录
            to_remove = len(self._processed_txs) - self._max_cache_size // 2
            for _ in range(to_remove):
                if self._processed_txs:
                    self._processed_txs.pop()
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    @property
    def wallet_count(self) -> int:
        return len(self._wallets)
