"""
多Provider支持模块
==================
多RPC提供商、多WebSocket failover机制。
"""

import asyncio
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass
import random

from web3 import Web3
from web3.exceptions import TransactionNotFound

from utils.logger import get_logger
from utils.retry import with_blockchain_retry, BlockchainError

logger = get_logger(__name__)


@dataclass
class ProviderStatus:
    """Provider状态"""
    url: str
    name: str
    is_healthy: bool = True
    latency_ms: float = 0.0
    last_check: Optional[datetime] = None
    error_count: int = 0
    success_count: int = 0


class MultiRPCProvider:
    """
    多RPC提供商管理器
    
    支持多个RPC节点轮询和自动切换：
    - Alchemy
    - QuickNode
    - Ankr
    - 公共节点
    """
    
    # 默认Polygon RPC节点
    DEFAULT_PROVIDERS = [
        ("alchemy", "https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY"),
        ("quicknode", "https://polygon.quicknode.com/YOUR_KEY"),
        ("ankr", "https://rpc.ankr.com/polygon"),
        ("polygon-rpc", "https://polygon-rpc.com"),
        ("matic", "https://rpc-mainnet.matic.network"),
    ]
    
    def __init__(
        self,
        providers: Optional[List[tuple]] = None,
        api_keys: Optional[Dict[str, str]] = None,
        max_latency_ms: float = 2000.0,
        max_errors: int = 5,
        health_check_interval: float = 60.0,
    ):
        """
        初始化多RPC提供商
        
        Args:
            providers: [(name, url_template), ...]
            api_keys: {name: key, ...}
            max_latency_ms: 最大延迟阈值
            max_errors: 最大连续错误次数
            health_check_interval: 健康检查间隔
        """
        self.providers: List[ProviderStatus] = []
        self.max_latency_ms = max_latency_ms
        self.max_errors = max_errors
        self.health_check_interval = health_check_interval
        
        self._current_index = 0
        self._web3_instances: Dict[str, Web3] = {}
        self._running = False
        self._health_task: Optional[asyncio.Task] = None
        
        # 初始化provider列表
        provider_list = providers or self.DEFAULT_PROVIDERS
        api_keys = api_keys or {}
        
        for name, url_template in provider_list:
            # 替换API密钥
            url = url_template
            if name in api_keys:
                url = url.replace("YOUR_KEY", api_keys[name])
            
            # 跳过没有密钥的付费节点
            if "YOUR_KEY" in url:
                continue
            
            self.providers.append(ProviderStatus(url=url, name=name))
        
        if not self.providers:
            raise ValueError("没有可用的RPC提供商")
        
        logger.info(f"多RPC提供商初始化 | 可用节点: {len(self.providers)}")
    
    async def start(self) -> None:
        """启动健康检查"""
        if self._running:
            return
        
        self._running = True
        
        # 初始健康检查
        for provider in self.providers:
            await self._check_provider_health(provider)
        
        # 启动定期检查
        self._health_task = asyncio.create_task(self._health_check_loop())
        
        logger.info("多RPC提供商已启动")
    
    async def stop(self) -> None:
        """停止"""
        self._running = False
        
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        
        logger.info("多RPC提供商已停止")
    
    async def _health_check_loop(self) -> None:
        """健康检查循环"""
        while self._running:
            try:
                for provider in self.providers:
                    await self._check_provider_health(provider)
                
                await asyncio.sleep(self.health_check_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"健康检查异常: {e}")
                await asyncio.sleep(self.health_check_interval)
    
    async def _check_provider_health(self, provider: ProviderStatus) -> bool:
        """检查单个provider健康状态"""
        try:
            w3 = self._get_web3(provider.name)
            
            start = asyncio.get_event_loop().time()
            block_number = await asyncio.to_thread(w3.eth.block_number)
            latency = (asyncio.get_event_loop().time() - start) * 1000
            
            provider.is_healthy = True
            provider.latency_ms = latency
            provider.last_check = datetime.now(timezone.utc)
            provider.error_count = 0
            provider.success_count += 1
            
            logger.debug(
                f"Provider健康: {provider.name} | "
                f"延迟: {latency:.0f}ms | 块高: {block_number}"
            )
            return True
            
        except Exception as e:
            provider.is_healthy = False
            provider.error_count += 1
            provider.last_check = datetime.now(timezone.utc)
            
            logger.warning(
                f"Provider不健康: {provider.name} | 错误: {e}"
            )
            return False
    
    def _get_web3(self, name: str) -> Web3:
        """获取或创建Web3实例"""
        if name not in self._web3_instances:
            provider = next((p for p in self.providers if p.name == name), None)
            if provider:
                self._web3_instances[name] = Web3(Web3.HTTPProvider(provider.url))
        
        return self._web3_instances.get(name)
    
    def get_best_provider(self) -> Optional[ProviderStatus]:
        """
        获取最佳provider
        
        优先选择健康且延迟最低的
        """
        healthy = [p for p in self.providers if p.is_healthy]
        
        if not healthy:
            # 如果没有健康的，重置所有并返回第一个
            logger.warning("没有健康的RPC节点，尝试重置")
            for p in self.providers:
                p.is_healthy = True
                p.error_count = 0
            return self.providers[0] if self.providers else None
        
        # 按延迟排序，选择最快的
        healthy.sort(key=lambda p: p.latency_ms)
        return healthy[0]
    
    def get_next_provider(self) -> Optional[ProviderStatus]:
        """轮询获取下一个provider"""
        healthy = [p for p in self.providers if p.is_healthy]
        
        if not healthy:
            return self.get_best_provider()
        
        # 轮询
        self._current_index = (self._current_index + 1) % len(healthy)
        return healthy[self._current_index]
    
    async def call(
        self,
        method: str,
        *args,
        **kwargs
    ) -> Any:
        """
        调用RPC方法（自动选择最佳节点）
        
        Args:
            method: 方法名（如 eth.block_number, eth.get_balance）
            *args: 方法参数
            **kwargs: 额外参数
        
        Returns:
            方法返回值
        """
        provider = self.get_best_provider()
        if not provider:
            raise BlockchainError("没有可用的RPC节点")
        
        w3 = self._get_web3(provider.name)
        
        try:
            # 获取方法
            method_parts = method.split('.')
            obj = w3
            for part in method_parts[:-1]:
                obj = getattr(obj, part)
            func = getattr(obj, method_parts[-1])
            
            # 执行调用
            result = await asyncio.to_thread(func, *args, **kwargs)
            
            provider.success_count += 1
            return result
            
        except Exception as e:
            provider.error_count += 1
            provider.is_healthy = False
            
            # 尝试切换到备用节点
            logger.warning(f"RPC调用失败 ({provider.name}): {e}，尝试备用节点")
            return await self._fallback_call(method, *args, **kwargs)
    
    async def _fallback_call(self, method: str, *args, **kwargs) -> Any:
        """备用节点调用"""
        tried = set()
        
        for _ in range(len(self.providers) - 1):
            provider = self.get_next_provider()
            if not provider or provider.name in tried:
                continue
            
            tried.add(provider.name)
            w3 = self._get_web3(provider.name)
            
            try:
                method_parts = method.split('.')
                obj = w3
                for part in method_parts[:-1]:
                    obj = getattr(obj, part)
                func = getattr(obj, method_parts[-1])
                
                result = await asyncio.to_thread(func, *args, **kwargs)
                provider.success_count += 1
                return result
                
            except Exception as e:
                provider.error_count += 1
                provider.is_healthy = False
                logger.warning(f"备用节点也失败 ({provider.name}): {e}")
        
        raise BlockchainError("所有RPC节点都失败")
    
    @with_blockchain_retry()
    async def get_block_number(self) -> int:
        """获取区块高度"""
        return await self.call("eth.block_number")
    
    @with_blockchain_retry()
    async def get_balance(self, address: str, block: str = "latest") -> int:
        """获取余额"""
        return await self.call("eth.get_balance", address, block)
    
    @with_blockchain_retry()
    async def get_transaction_receipt(self, tx_hash: str) -> Optional[dict]:
        """获取交易回执"""
        try:
            return await self.call("eth.get_transaction_receipt", tx_hash)
        except TransactionNotFound:
            return None
    
    def get_status(self) -> Dict[str, Any]:
        """获取状态"""
        return {
            "total_providers": len(self.providers),
            "healthy_providers": sum(1 for p in self.providers if p.is_healthy),
            "providers": [
                {
                    "name": p.name,
                    "healthy": p.is_healthy,
                    "latency_ms": p.latency_ms,
                    "errors": p.error_count,
                }
                for p in self.providers
            ]
        }


class WebSocketFailover:
    """
    WebSocket故障转移管理器
    
    管理多个WebSocket连接，自动切换。
    """
    
    def __init__(
        self,
        urls: List[str],
        on_message: Callable,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None,
        reconnect_interval: float = 5.0,
        max_reconnect_attempts: int = 10,
    ):
        """
        初始化WebSocket故障转移
        
        Args:
            urls: WebSocket URL列表（按优先级）
            on_message: 消息处理回调
            on_connect: 连接成功回调
            on_disconnect: 断开连接回调
            reconnect_interval: 重连间隔
            max_reconnect_attempts: 最大重连次数
        """
        self.urls = urls
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.reconnect_interval = reconnect_interval
        self.max_reconnect_attempts = max_reconnect_attempts
        
        self._current_url_index = 0
        self._running = False
        self._ws = None
        self._ws_task: Optional[asyncio.Task] = None
        
        logger.info(f"WebSocket故障转移初始化 | URLs: {len(urls)}")
    
    @property
    def current_url(self) -> str:
        """获取当前URL"""
        return self.urls[self._current_url_index]
    
    async def start(self) -> None:
        """启动"""
        if self._running:
            return
        
        self._running = True
        self._ws_task = asyncio.create_task(self._run_loop())
        
        logger.info("WebSocket故障转移已启动")
    
    async def stop(self) -> None:
        """停止"""
        self._running = False
        
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        
        if self._ws:
            await self._ws.close()
        
        logger.info("WebSocket故障转移已停止")
    
    async def _run_loop(self) -> None:
        """运行循环"""
        import aiohttp
        
        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.current_url) as ws:
                        self._ws = ws
                        
                        logger.info(f"WebSocket已连接: {self.current_url[:50]}...")
                        
                        if self.on_connect:
                            await self.on_connect({"url": self.current_url})
                        
                        async for msg in ws:
                            if not self._running:
                                break
                            
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self.on_message(msg.data)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                logger.error(f"WebSocket错误: {ws.exception()}")
                                break
                        
                        if self.on_disconnect:
                            await self.on_disconnect({"url": self.current_url})
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WebSocket异常 ({self.current_url[:30]}...): {e}")
                
                # 切换到下一个URL
                if len(self.urls) > 1:
                    self._current_url_index = (self._current_url_index + 1) % len(self.urls)
                    logger.info(f"切换到备用WebSocket: {self.current_url[:30]}...")
                
                await asyncio.sleep(self.reconnect_interval)
    
    def switch_to_next(self) -> None:
        """手动切换到下一个URL"""
        self._current_url_index = (self._current_url_index + 1) % len(self.urls)
        logger.info(f"切换WebSocket: {self.current_url[:30]}...")
