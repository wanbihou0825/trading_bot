"""
钱包扫描器 - 自动发现高质量可跟单钱包

数据源:
1. Polymarket 排行榜 (最高质量)
2. CLOB 市场交易活动 (链上活跃度)
3. Polygonscan 合约交互 (辅助发现)
4. 手动种子钱包 (用户指定)

评分数据: 通过 Data API 获取真实交易记录 (含 pnl/side/market 等字段)
"""
import asyncio
import aiohttp
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Any, TYPE_CHECKING
from decimal import Decimal
import logging
import random

from .wallet_quality_scorer import WalletQualityScorer, QualityScore
from .market_maker_detector import MarketMakerDetector
from .red_flag_detector import RedFlagDetector as WarningDetector

if TYPE_CHECKING:
    from services.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)


class WalletScanner:
    """自动扫描发现高质量可跟单钱包"""
    
    def __init__(
        self,
        quality_scorer: WalletQualityScorer,
        mm_detector: MarketMakerDetector,
        warning_detector: WarningDetector,
        polygonscan_api_key: str,
        polymarket_client: Optional["PolymarketClient"] = None,
        seed_wallets: Optional[List[str]] = None,
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
        self.polymarket_client = polymarket_client
        self.seed_wallets = [w.lower().strip() for w in (seed_wallets or []) if w.strip()]
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
        
        # Data API 基地址
        self._data_api = "https://data-api.polymarket.com"
        
        # API 限速: 每秒最多2个请求
        self._rate_limiter = asyncio.Semaphore(2)
        self._last_request_time: float = 0
        
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
    
    async def _rate_limited_request(self):
        """简单的请求限速 (避免被Data API限流)"""
        import time
        async with self._rate_limiter:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < 0.5:  # 至少间隔500ms
                await asyncio.sleep(0.5 - elapsed)
            self._last_request_time = time.monotonic()
    
    async def _discover_wallets(self):
        """发现新的高质量钱包 (4个数据源)"""
        logger.info("开始扫描发现高质量钱包...")
        
        # 数据源1 (最高优先级): 手动种子钱包
        seed_candidates = [w for w in self.seed_wallets if w not in self._scanned_wallets]
        
        # 数据源2 (高质量): Polymarket 排行榜
        leaderboard_traders = await self._get_leaderboard_traders()
        
        # 数据源3: CLOB 市场活跃交易者
        active_traders = await self._get_active_traders_from_markets()
        
        # 数据源4 (辅助): Polygonscan 合约交互者
        contract_interactors = await self._get_contract_interactors()
        
        # 按优先级合并 (种子 > 排行榜 > 活跃交易 > 合约交互)
        # 使用有序列表去重，保持优先级
        seen = set()
        ordered_candidates = []
        for wallet in seed_candidates + leaderboard_traders + active_traders + contract_interactors:
            w = wallet.lower()
            if w not in seen and w not in self._scanned_wallets:
                seen.add(w)
                ordered_candidates.append(w)
        
        logger.info(
            f"发现 {len(ordered_candidates)} 个新钱包待分析 | "
            f"种子: {len(seed_candidates)} | 排行榜: {len(leaderboard_traders)} | "
            f"CLOB: {len(active_traders)} | 合约: {len(contract_interactors)}"
        )
        
        # 分析新钱包
        for wallet in ordered_candidates:
            if not self._running:
                break
                
            if len(self._active_wallets) >= self.max_following_wallets:
                break
            
            await self._analyze_wallet(wallet)
            self._scanned_wallets.add(wallet)
    
    async def _get_leaderboard_traders(self) -> List[str]:
        """从 Polymarket 排行榜获取高质量交易者 (最优质数据源)"""
        if self.dry_run:
            return self._generate_mock_traders(10)
        
        traders = []
        
        try:
            if not self._session:
                return []
            
            # Polymarket 排行榜 API (获取盈利最高的交易者)
            # 尝试多个可能的端点
            endpoints = [
                f"{self._data_api}/leaderboard",
                f"{self._data_api}/rankings",
            ]
            
            for url in endpoints:
                try:
                    await self._rate_limited_request()
                    async with self._session.get(
                        url,
                        params={"limit": 50, "period": "all"},
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as response:
                        if response.status != 200:
                            continue
                        
                        data = await response.json()
                        
                        # 解析排行榜数据 (兼容多种返回格式)
                        entries = data if isinstance(data, list) else data.get("data", data.get("leaderboard", data.get("rankings", [])))
                        
                        if not isinstance(entries, list) or not entries:
                            continue
                        
                        for entry in entries:
                            addr = (
                                entry.get("address")
                                or entry.get("user")
                                or entry.get("wallet")
                                or entry.get("proxyWallet")
                            )
                            if addr and addr.startswith("0x"):
                                traders.append(addr.lower())
                        
                        if traders:
                            logger.info(f"从排行榜获取 {len(traders)} 个交易者")
                            return list(set(traders))
                            
                except Exception as e:
                    logger.debug(f"排行榜端点 {url} 失败: {e}")
                    continue
            
            # 备选: 从 Activity 端点获取高活跃度钱包
            if not traders:
                try:
                    await self._rate_limited_request()
                    async with self._session.get(
                        f"{self._data_api}/activity",
                        params={"limit": 100},
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as response:
                        if response.status == 200:
                            activities = await response.json()
                            if isinstance(activities, list):
                                for act in activities:
                                    addr = act.get("user") or act.get("address")
                                    if addr and addr.startswith("0x"):
                                        traders.append(addr.lower())
                                if traders:
                                    logger.info(f"从活动端点获取 {len(traders)} 个交易者")
                except Exception as e:
                    logger.debug(f"Activity 端点失败: {e}")
            
            return list(set(traders))
            
        except Exception as e:
            logger.error(f"获取排行榜交易者失败: {e}")
            return []
    
    async def _get_active_traders_from_markets(self) -> List[str]:
        """从 CLOB 市场交易获取活跃交易者"""
        if self.dry_run:
            return self._generate_mock_traders(15)
        
        try:
            url = "https://clob.polymarket.com/markets"
            
            if not self._session:
                return []
            
            await self._rate_limited_request()
            async with self._session.get(url) as response:
                if response.status != 200:
                    return []
                
                data = await response.json()
                traders = []
                
                markets = data if isinstance(data, list) else data.get("data", data.get("markets", []))
                if not isinstance(markets, list):
                    logger.warning(f"markets API 返回非列表类型: {type(markets)}")
                    return []
                
                for market in markets[:20]:
                    market_id = market.get("condition_id")
                    if market_id:
                        await self._rate_limited_request()
                        history_url = f"https://clob.polymarket.com/trades?market={market_id}"
                        
                        try:
                            async with self._session.get(history_url) as hist_response:
                                if hist_response.status == 200:
                                    trades = await hist_response.json()
                                    # CLOB trades 可能返回 {"data": [...]} 或列表
                                    trade_list = trades if isinstance(trades, list) else trades.get("data", [])
                                    for trade in trade_list[:30]:
                                        # CLOB trades 字段: maker, taker, 或 address
                                        for field in ["maker", "taker", "address", "user"]:
                                            addr = trade.get(field)
                                            if addr and addr.startswith("0x"):
                                                traders.append(addr.lower())
                        except Exception as e:
                            logger.debug(f"获取市场 {market_id[:10]}... 交易失败: {e}")
                
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
        """获取钱包在 Polymarket 的交易历史 (使用 Data API 获取真实交易数据)"""
        if self.dry_run:
            return self._generate_mock_trades(wallet_address)
        
        # 优先使用 PolymarketClient (已有完整的认证和重试)
        if self.polymarket_client:
            try:
                trades = await self.polymarket_client.get_user_trades(
                    wallet_address=wallet_address,
                    limit=200
                )
                if trades:
                    return self._normalize_trade_data(trades)
            except Exception as e:
                logger.warning(f"PolymarketClient获取交易失败，回退到直接请求: {e}")
        
        # 回退: 直接请求 Data API
        try:
            if not self._session:
                return []
            
            await self._rate_limited_request()
            async with self._session.get(
                f"{self._data_api}/trades",
                params={
                    "user": wallet_address.lower(),
                    "limit": 200,
                    "sort": "desc",
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    logger.warning(f"Data API /trades 返回 {response.status}")
                    return []
                
                trades = await response.json()
                if isinstance(trades, list):
                    return self._normalize_trade_data(trades)
                return []
                
        except Exception as e:
            logger.error(f"获取钱包交易历史失败: {e}")
            return []
    
    def _normalize_trade_data(self, trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        标准化 Data API 交易数据，确保与 quality_scorer 兼容
        
        Data API /trades 返回字段 (典型):
          - id, market, asset, side, size, price, fee,
          - outcome, timestamp, status, type
        
        quality_scorer._calculate_stats() 需要的字段:
          - pnl (盈亏)
          - pnl_pct (盈亏百分比)
          - category (市场类别)
          - timestamp (ISO或Unix时间戳)
          - opened_at / closed_at (持仓时间)
          - hold_hours (持仓小时数)
        """
        normalized = []
        
        for trade in trades:
            # 提取或计算 PnL
            pnl = trade.get("pnl") or trade.get("profit") or trade.get("realized_pnl")
            if pnl is None:
                # 对于买入交易没有直接PnL (需要匹配卖出才能算)
                # 尝试从 outcome 判断
                outcome = trade.get("outcome", "").upper()
                side = trade.get("side", "").upper()
                price = float(trade.get("price", 0) or 0)
                size = float(trade.get("size", 0) or 0)
                
                if outcome in ["YES", "NO"] and price > 0 and size > 0:
                    if trade.get("type", "").lower() == "sell":
                        # 卖出 = 平仓，简化估算PnL
                        pnl = size * price - size * 0.5  # 粗略估算
                    else:
                        pnl = 0  # 买入时PnL尚未实现
                else:
                    pnl = 0
            
            pnl = float(pnl)
            price = float(trade.get("price", 0) or 0)
            size = float(trade.get("size", 0) or 0)
            cost = price * size if price > 0 and size > 0 else 1
            pnl_pct = pnl / cost if cost > 0 else 0
            
            # 时间戳标准化
            timestamp = trade.get("timestamp") or trade.get("created_at") or trade.get("time")
            if isinstance(timestamp, (int, float)) and timestamp > 1e12:
                # 毫秒级时间戳 → 转ISO
                from datetime import datetime as dt
                timestamp = dt.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
            elif isinstance(timestamp, (int, float)):
                from datetime import datetime as dt
                timestamp = dt.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
            
            # 市场类别 (Data API 可能不直接提供，使用市场信息估算)
            category = (
                trade.get("category")
                or trade.get("market_category")
                or trade.get("market_type")
                or "general"
            )
            
            normalized.append({
                # 原始字段保留
                **trade,
                # 评分器需要的标准字段
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "category": category,
                "timestamp": timestamp or "",
                "hold_hours": trade.get("hold_hours", 24),  # 默认24h (Data API通常不提供)
            })
        
        return normalized
    
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
        """生成模拟交易历史用于测试 (匹配标准化格式)"""
        import random
        
        trades = []
        base_time = int(datetime.now(timezone.utc).timestamp()) - 86400 * 30
        categories = ["politics", "sports", "crypto", "entertainment", "general"]
        
        for i in range(random.randint(25, 50)):
            is_win = random.random() < 0.6  # 60%胜率
            pnl = random.uniform(5, 50) if is_win else -random.uniform(5, 30)
            price = round(random.uniform(0.3, 0.8), 2)
            size = round(random.uniform(10, 100), 2)
            ts = base_time + i * 3600 * random.randint(1, 24)
            
            trades.append({
                "market": f"mock_market_{i % 10}",
                "side": random.choice(["YES", "NO"]),
                "price": price,
                "size": size,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / (price * size), 4) if price * size > 0 else 0,
                "category": random.choice(categories),
                "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "hold_hours": random.uniform(1, 72),
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
