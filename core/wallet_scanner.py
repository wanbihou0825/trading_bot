"""
钱包扫描器 - 自动发现高质量可跟单钱包
"""
import asyncio
import aiohttp
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Any
from decimal import Decimal
import logging
import random

from .wallet_quality_scorer import WalletQualityScorer, QualityScore
from .market_maker_detector import MarketMakerDetector
from .red_flag_detector import RedFlagDetector as WarningDetector

logger = logging.getLogger(__name__)


class WalletScanner:
    """自动扫描发现高质量可跟单钱包"""
    
    def __init__(
        self,
        quality_scorer: WalletQualityScorer,
        mm_detector: MarketMakerDetector,
        warning_detector: WarningDetector,
        polygonscan_api_key: str,
        min_quality_score: Decimal = Decimal("70"),
        min_win_rate: Decimal = Decimal("55"),
        min_trades: int = 20,
        min_profit_factor: Decimal = Decimal("1.3"),
        max_following_wallets: int = 10,
        scan_interval_minutes: int = 60,
        dry_run: bool = False
    ):
        self.quality_scorer = quality_scorer
        self.mm_detector = mm_detector
        self.warning_detector = warning_detector
        self.polygonscan_api_key = polygonscan_api_key
        self.min_quality_score = min_quality_score
        self.min_win_rate = min_win_rate
        self.min_trades = min_trades
        self.min_profit_factor = min_profit_factor
        self.max_following_wallets = max_following_wallets
        self.scan_interval = scan_interval_minutes * 60
        self.dry_run = dry_run
        
        # Polymarket 合约地址 (Polygon)
        self.polymarket_contracts = {
            "ctf_exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
            "conditional_tokens": "0x4D97DCd97eC945f40cF65F87097ACe5EA0473084",
        }
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._discovered_wallets: Dict[str, Dict[str, Any]] = {}  # wallet_address -> info
        self._active_wallets: Set[str] = set()  # 当前跟单的钱包
        self._scanned_wallets: Set[str] = set()  # 已扫描过的钱包
        self._running = False
        self._scan_task = None  # 扫描任务引用
        self._on_wallet_discovered = None  # 回调函数
    
    def set_discovery_callback(self, callback):
        """设置发现新钱包的回调函数"""
        self._on_wallet_discovered = callback
    
    async def start(self):
        """启动扫描器"""
        if self._running:
            return
        
        self._running = True
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        
        logger.info("钱包扫描器启动 - 自动发现高质量钱包")
        
        # 启动后台扫描任务并保存引用
        self._scan_task = asyncio.create_task(self._scan_loop())
    
    async def stop(self):
        """停止扫描器"""
        self._running = False
        
        # 取消扫描任务
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        
        if self._session:
            await self._session.close()
        
        logger.info("钱包扫描器已停止")
    
    async def _scan_loop(self):
        """扫描循环"""
        try:
            while self._running:
                try:
                    await self._discover_wallets()
                    
                    # 维护跟单列表
                    await self._maintain_following_list()
                    
                    await asyncio.sleep(self.scan_interval)
                    
                except asyncio.CancelledError:
                    logger.info("扫描循环被取消")
                    break
                except Exception as e:
                    logger.error(f"扫描异常: {e}")
                    if self._running:
                        await asyncio.sleep(60)  # 异常后等待1分钟
        except asyncio.CancelledError:
            logger.info("扫描器退出")
    
    async def _discover_wallets(self):
        """发现新的高质量钱包"""
        logger.info("开始扫描发现高质量钱包...")
        
        # 方法1: 从 Polymarket API 获取活跃市场
        active_traders = await self._get_active_traders_from_markets()
        
        # 方法2: 从 Polygonscan 获取 Polymarket 合约交互者
        contract_interactors = await self._get_contract_interactors()
        
        # 合并所有候选钱包
        candidates = set(active_traders + contract_interactors)
        new_wallets = candidates - self._scanned_wallets
        
        logger.info(f"发现 {len(new_wallets)} 个新钱包待分析")
        
        # 分析新钱包
        for wallet in new_wallets:
            if not self._running:
                break
                
            if len(self._active_wallets) >= self.max_following_wallets:
                break
            
            await self._analyze_wallet(wallet)
            self._scanned_wallets.add(wallet)
    
    async def _get_active_traders_from_markets(self) -> List[str]:
        """从 Polymarket API 获取活跃交易者"""
        if self.dry_run:
            return self._generate_mock_traders(15)
        
        try:
            # Polymarket 公开 API
            url = "https://clob.polymarket.com/markets"
            
            if not self._session:
                return []
            
            async with self._session.get(url) as response:
                if response.status != 200:
                    return []
                
                data = await response.json()
                traders = []
                
                for market in data[:20]:  # 前20个活跃市场
                    market_id = market.get("condition_id")
                    if market_id:
                        # 获取市场交易历史
                        history_url = f"https://clob.polymarket.com/trades?market={market_id}"
                        
                        async with self._session.get(history_url) as hist_response:
                            if hist_response.status == 200:
                                trades = await hist_response.json()
                                for trade in trades[:30]:
                                    trader = trade.get("address")
                                    if trader:
                                        traders.append(trader.lower())
                
                return list(set(traders))
                
        except Exception as e:
            logger.error(f"获取活跃交易者失败: {e}")
            return []
    
    async def _get_contract_interactors(self) -> List[str]:
        """从 Polygonscan 获取与 Polymarket 合约交互的钱包"""
        if self.dry_run:
            return self._generate_mock_traders(10)
        
        traders = []
        
        try:
            if not self._session:
                return []
            
            for contract_name, contract_addr in self.polymarket_contracts.items():
                url = f"https://api.polygonscan.com/api"
                params = {
                    "module": "account",
                    "action": "txlist",
                    "address": contract_addr,
                    "startblock": 0,
                    "endblock": 99999999,
                    "page": 1,
                    "offset": 100,
                    "sort": "desc",
                    "apikey": self.polygonscan_api_key
                }
                
                async with self._session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("status") == "1":
                            for tx in data.get("result", []):
                                # 排除合约调用，只保留 EOA
                                if not tx.get("from", "").startswith("0x"):
                                    continue
                                traders.append(tx["from"].lower())
            
            return list(set(traders))
            
        except Exception as e:
            logger.error(f"获取合约交互者失败: {e}")
            return []
    
    async def _analyze_wallet(self, wallet_address: str) -> bool:
        """分析钱包是否值得跟单"""
        try:
            # 检查是否仍在运行
            if not self._running:
                return False
            
            # 1. 获取钱包交易历史
            trades = await self._get_wallet_trades(wallet_address)
            
            if len(trades) < self.min_trades:
                logger.debug(f"钱包 {wallet_address[:8]}... 交易次数不足: {len(trades)}")
                return False
            
            # 2. 计算质量评分
            quality = self.quality_scorer.score_wallet(wallet_address, trades)
            
            if quality.overall_score < self.min_quality_score:
                logger.debug(f"钱包 {wallet_address[:8]}... 评分不足: {quality.overall_score}")
                return False
            
            if quality.stats.win_rate < self.min_win_rate:
                logger.debug(f"钱包 {wallet_address[:8]}... 胜率不足: {quality.stats.win_rate}")
                return False
            
            if quality.stats.profit_factor < self.min_profit_factor:
                logger.debug(f"钱包 {wallet_address[:8]}... 盈亏比不足: {quality.stats.profit_factor}")
                return False
            
            # 3. 检查是否为做市商
            mm_score = self.mm_detector.detect(wallet_address, trades)
            is_mm = mm_score.is_market_maker
            mm_confidence = float(mm_score.confidence)
            
            if is_mm and mm_confidence > 0.7:
                logger.debug(f"钱包 {wallet_address[:8]}... 疑似做市商，跳过")
                return False
            
            # 4. 检查警告信号
            warnings = self.warning_detector.detect(wallet_address, trades)
            
            critical_warnings = [w for w in warnings if w.severity == "critical"]
            if critical_warnings:
                logger.debug(f"钱包 {wallet_address[:8]}... 存在严重警告信号")
                return False
            
            # 5. 添加到发现列表
            wallet_info = {
                "address": wallet_address,
                "quality": quality,
                "is_market_maker": is_mm,
                "mm_confidence": mm_confidence,
                "warnings": warnings,
                "discovered_at": datetime.now(timezone.utc),
                "trades_count": len(trades),
            }
            
            self._discovered_wallets[wallet_address] = wallet_info
            
            logger.info(
                f"发现高质量钱包: {wallet_address[:10]}... "
                f"评分={quality.overall_score:.1f} "
                f"胜率={quality.stats.win_rate*100:.1f}% "
                f"盈亏比={quality.stats.profit_factor:.2f} "
                f"交易次数={len(trades)}"
            )
            
            # 触发回调
            if self._on_wallet_discovered:
                await self._on_wallet_discovered(wallet_address, wallet_info)
            
            return True
            
        except Exception as e:
            logger.error(f"分析钱包 {wallet_address} 异常: {e}")
            return False
    
    async def _get_wallet_trades(self, wallet_address: str) -> List[Dict[str, Any]]:
        """获取钱包在 Polymarket 的交易历史"""
        if self.dry_run:
            return self._generate_mock_trades(wallet_address)
        
        try:
            # 使用 Polygonscan API 获取交易历史
            if not self._session:
                return []
            
            url = "https://api.polygonscan.com/api"
            params = {
                "module": "account",
                "action": "txlist",
                "address": wallet_address,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 200,
                "sort": "asc",
                "apikey": self.polygonscan_api_key
            }
            
            async with self._session.get(url, params=params) as response:
                if response.status != 200:
                    return []
                
                data = await response.json()
                
                if data.get("status") != "1":
                    return []
                
                # 筛选 Polymarket 相关交易
                trades = []
                polymarket_addresses = set(
                    addr.lower() for addr in self.polymarket_contracts.values()
                )
                
                for tx in data.get("result", []):
                    to_addr = tx.get("to", "").lower()
                    if to_addr in polymarket_addresses:
                        trades.append({
                            "hash": tx.get("hash"),
                            "timestamp": int(tx.get("timeStamp", 0)),
                            "from": tx.get("from"),
                            "to": tx.get("to"),
                            "value": int(tx.get("value", 0)),
                            "gas_used": int(tx.get("gasUsed", 0)),
                            "gas_price": int(tx.get("gasPrice", 0)),
                            "method_id": tx.get("methodId", ""),
                            "input": tx.get("input", ""),
                        })
                
                return trades
                
        except Exception as e:
            logger.error(f"获取钱包交易历史失败: {e}")
            return []
    
    async def _maintain_following_list(self):
        """维护跟单钱包列表"""
        # 按评分排序
        sorted_wallets = sorted(
            self._discovered_wallets.items(),
            key=lambda x: x[1]["quality"].overall_score,
            reverse=True
        )
        
        # 选择前 N 个钱包
        new_active = set()
        for wallet_addr, info in sorted_wallets[:self.max_following_wallets]:
            new_active.add(wallet_addr)
        
        # 检查变化
        added = new_active - self._active_wallets
        removed = self._active_wallets - new_active
        
        if added:
            logger.info(f"新增跟单钱包: {[w[:10]+'...' for w in added]}")
        
        if removed:
            logger.info(f"移除跟单钱包: {[w[:10]+'...' for w in removed]}")
        
        self._active_wallets = new_active
    
    def _generate_mock_traders(self, count: int) -> List[str]:
        """生成模拟交易者地址用于测试"""
        import random
        traders = []
        for j in range(count):
            # 生成随机钱包地址
            addr = "0x" + "".join(random.choice("0123456789abcdef") for _ in range(40))
            traders.append(addr)
        return traders
    
    def _generate_mock_trades(self, wallet_address: str) -> List[Dict[str, Any]]:
        """生成模拟交易历史用于测试"""
        import random
        
        trades = []
        base_time = int(datetime.now(timezone.utc).timestamp()) - 86400 * 30
        
        for i in range(random.randint(25, 50)):
            trades.append({
                "hash": f"0x{random.randint(100000, 999999):x}",
                "timestamp": base_time + i * 3600 * random.randint(1, 24),
                "from": wallet_address,
                "to": self.polymarket_contracts["ctf_exchange"],
                "value": random.randint(1000000, 50000000),
                "gas_used": random.randint(100000, 500000),
                "gas_price": random.randint(10000000000, 50000000000),
            })
        
        return trades
    
    def get_active_wallets(self) -> List[str]:
        """获取当前跟单的钱包列表"""
        return list(self._active_wallets)
    
    def get_discovered_wallets(self) -> Dict[str, Dict[str, Any]]:
        """获取所有发现的钱包信息"""
        return self._discovered_wallets.copy()
    
    def get_wallet_info(self, wallet_address: str) -> Optional[Dict[str, Any]]:
        """获取特定钱包信息"""
        return self._discovered_wallets.get(wallet_address)
    
    async def force_scan(self) -> int:
        """强制立即扫描"""
        await self._discover_wallets()
        return len(self._discovered_wallets)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取扫描统计"""
        return {
            "total_discovered": len(self._discovered_wallets),
            "active_wallets": len(self._active_wallets),
            "scanned_wallets": len(self._scanned_wallets),
            "active_wallet_addresses": list(self._active_wallets),
        }
