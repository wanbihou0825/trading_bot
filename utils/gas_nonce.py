"""
Nonce管理和Gas优化模块
======================
防nonce冲突、Gas价格动态优化。
"""

import asyncio
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from dataclasses import dataclass

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class GasPrice:
    """Gas价格信息"""
    low: int  # Gwei
    standard: int
    fast: int
    instant: int
    base_fee: Optional[int] = None  # EIP-1559
    priority_fee: Optional[int] = None
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)


class NonceManager:
    """
    Nonce管理器
    
    防止nonce冲突，管理交易序列号。
    """
    
    def __init__(
        self,
        web3_provider,  # Web3实例或MultiRPCProvider
        address: str,
        pending_timeout: float = 300.0,  # 5分钟
    ):
        """
        初始化Nonce管理器
        
        Args:
            web3_provider: Web3提供者
            address: 钱包地址
            pending_timeout: 待处理交易超时时间
        """
        self.web3_provider = web3_provider
        self.address = address
        self.pending_timeout = pending_timeout
        
        self._pending_nonces: Dict[int, datetime] = {}  # nonce -> 提交时间
        self._current_nonce: Optional[int] = None
        self._lock = asyncio.Lock()
        
        logger.info(f"Nonce管理器初始化 | 地址: {address[:10]}...")
    
    async def get_next_nonce(self) -> int:
        """
        获取下一个可用nonce
        
        Returns:
            下一个nonce值
        """
        async with self._lock:
            # 如果是首次调用，从链上获取
            if self._current_nonce is None:
                self._current_nonce = await self._get_onchain_nonce()
            
            # 清理超时的pending nonce
            self._cleanup_pending()
            
            # 找到可用的nonce
            nonce = self._current_nonce
            
            while nonce in self._pending_nonces:
                nonce += 1
            
            # 记录为pending
            self._pending_nonces[nonce] = datetime.now(timezone.utc)
            
            logger.debug(f"分配nonce: {nonce}")
            return nonce
    
    async def confirm_nonce(self, nonce: int) -> None:
        """
        确认nonce已使用
        
        Args:
            nonce: 已确认的nonce
        """
        async with self._lock:
            if nonce in self._pending_nonces:
                del self._pending_nonces[nonce]
            
            # 更新当前nonce
            if self._current_nonce is not None and nonce >= self._current_nonce:
                self._current_nonce = nonce + 1
            
            logger.debug(f"确认nonce: {nonce}")
    
    async def reset_nonce(self) -> None:
        """重置nonce（从链上重新获取）"""
        async with self._lock:
            self._current_nonce = None
            self._pending_nonces.clear()
            self._current_nonce = await self._get_onchain_nonce()
            logger.info(f"Nonce已重置: {self._current_nonce}")
    
    def _cleanup_pending(self) -> None:
        """清理超时的pending nonce"""
        now = datetime.now(timezone.utc)
        expired = [
            nonce for nonce, time in self._pending_nonces.items()
            if (now - time).total_seconds() > self.pending_timeout
        ]
        
        for nonce in expired:
            del self._pending_nonces[nonce]
            logger.warning(f"清理超时pending nonce: {nonce}")
    
    async def _get_onchain_nonce(self) -> int:
        """从链上获取nonce"""
        try:
            if hasattr(self.web3_provider, 'call'):
                # MultiRPCProvider
                return await self.web3_provider.call(
                    "eth.get_transaction_count",
                    self.address,
                    "pending"
                )
            else:
                # Web3实例
                return await asyncio.to_thread(
                    self.web3_provider.eth.get_transaction_count,
                    self.address,
                    "pending"
                )
        except Exception as e:
            logger.error(f"获取链上nonce失败: {e}")
            raise


class GasOptimizer:
    """
    Gas价格动态优化器
    
    在Gas war时自动上调priority fee。
    """
    
    # Polygon Gas站API
    GAS_STATION_URL = "https://gasstation-mainnet.matic.network/v2"
    
    def __init__(
        self,
        web3_provider=None,
        update_interval: float = 30.0,
        max_gas_price: int = 500,  # 最大Gas价格 Gwei
        priority_fee_multiplier: float = 1.2,  # Priority fee乘数
    ):
        """
        初始化Gas优化器
        
        Args:
            web3_provider: Web3提供者
            update_interval: 更新间隔
            max_gas_price: 最大Gas价格
            priority_fee_multiplier: Priority fee乘数（用于Gas war）
        """
        self.web3_provider = web3_provider
        self.update_interval = update_interval
        self.max_gas_price = max_gas_price
        self.priority_fee_multiplier = priority_fee_multiplier
        
        self._current_gas: Optional[GasPrice] = None
        self._running = False
        self._update_task: Optional[asyncio.Task] = None
        
        logger.info(
            f"Gas优化器初始化 | 最大Gas: {max_gas_price} Gwei | "
            f"Priority乘数: {priority_fee_multiplier}x"
        )
    
    async def start(self) -> None:
        """启动"""
        if self._running:
            return
        
        self._running = True
        await self._update_gas_price()
        self._update_task = asyncio.create_task(self._update_loop())
        
        logger.info("Gas优化器已启动")
    
    async def stop(self) -> None:
        """停止"""
        self._running = False
        
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Gas优化器已停止")
    
    async def _update_loop(self) -> None:
        """更新循环"""
        while self._running:
            try:
                await self._update_gas_price()
                await asyncio.sleep(self.update_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"更新Gas价格异常: {e}")
                await asyncio.sleep(self.update_interval)
    
    async def _update_gas_price(self) -> None:
        """更新Gas价格"""
        try:
            import aiohttp
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.GAS_STATION_URL,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        self._current_gas = GasPrice(
                            low=int(data.get("safeLow", {}).get("maxPriorityFee", 1)),
                            standard=int(data.get("standard", {}).get("maxPriorityFee", 2)),
                            fast=int(data.get("fast", {}).get("maxPriorityFee", 3)),
                            instant=int(data.get("fast", {}).get("maxPriorityFee", 5)),
                            base_fee=int(data.get("estimatedBaseFee", 30)),
                            priority_fee=int(data.get("fast", {}).get("maxPriorityFee", 3)),
                        )
                        
                        logger.debug(
                            f"Gas价格更新 | Standard: {self._current_gas.standard} Gwei | "
                            f"Base: {self._current_gas.base_fee} Gwei"
                        )
                        return
            
            # 如果API失败，从链上获取
            await self._update_from_chain()
            
        except Exception as e:
            logger.warning(f"获取Gas站数据失败: {e}")
            await self._update_from_chain()
    
    async def _update_from_chain(self) -> None:
        """从链上获取Gas价格"""
        try:
            if hasattr(self.web3_provider, 'call'):
                gas_price = await self.web3_provider.call("eth.gas_price")
            else:
                gas_price = await asyncio.to_thread(
                    self.web3_provider.eth.gas_price
                )
            
            # 转换为Gwei
            gas_gwei = gas_price // 10**9
            
            self._current_gas = GasPrice(
                low=gas_gwei,
                standard=gas_gwei,
                fast=int(gas_gwei * 1.2),
                instant=int(gas_gwei * 1.5),
            )
            
        except Exception as e:
            logger.error(f"从链上获取Gas价格失败: {e}")
    
    def get_gas_price(self, speed: str = "fast") -> int:
        """
        获取Gas价格
        
        Args:
            speed: 速度 (low, standard, fast, instant)
        
        Returns:
            Gas价格（Gwei）
        """
        if not self._current_gas:
            # 默认值
            return 30
        
        price_map = {
            "low": self._current_gas.low,
            "standard": self._current_gas.standard,
            "fast": self._current_gas.fast,
            "instant": self._current_gas.instant,
        }
        
        return min(price_map.get(speed, self._current_gas.fast), self.max_gas_price)
    
    def get_eip1559_params(self, speed: str = "fast") -> Dict[str, int]:
        """
        获取EIP-1559交易参数
        
        Args:
            speed: 速度
        
        Returns:
            {"maxFeePerGas": int, "maxPriorityFeePerGas": int}
        """
        if not self._current_gas:
            return {
                "maxFeePerGas": 40 * 10**9,  # 40 Gwei
                "maxPriorityFeePerGas": 3 * 10**9,  # 3 Gwei
            }
        
        base_fee = self._current_gas.base_fee or 30
        priority_fee = self.get_gas_price(speed)
        
        # 应用乘数（用于Gas war）
        priority_fee = int(priority_fee * self.priority_fee_multiplier)
        
        # 限制最大值
        priority_fee = min(priority_fee, self.max_gas_price)
        
        max_fee = base_fee + priority_fee
        
        return {
            "maxFeePerGas": max_fee * 10**9,  # 转换为Wei
            "maxPriorityFeePerGas": priority_fee * 10**9,
        }
    
    def is_gas_war(self, threshold: int = 100) -> bool:
        """
        检测是否处于Gas war
        
        Args:
            threshold: Gas价格阈值（Gwei）
        
        Returns:
            是否处于Gas war
        """
        if not self._current_gas:
            return False
        
        return self._current_gas.fast > threshold
    
    def get_status(self) -> Dict[str, Any]:
        """获取状态"""
        if not self._current_gas:
            return {"status": "unavailable"}
        
        return {
            "low": self._current_gas.low,
            "standard": self._current_gas.standard,
            "fast": self._current_gas.fast,
            "instant": self._current_gas.instant,
            "base_fee": self._current_gas.base_fee,
            "is_gas_war": self.is_gas_war(),
            "updated": self._current_gas.timestamp.isoformat() if self._current_gas.timestamp else None
        }
