"""
钱包扫描器 - 自动发现高质量可跟单钱包

数据源:
1. Polymarket 官方排行榜 API (/v1/leaderboard) — 主要数据源
2. 手动种子钱包 (用户指定)
3. Polygonscan 合约交互 (辅助, V1 API 已废弃，当前基本不可用)

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
        max_scan_wallets: int = 100,
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
        self.max_scan_wallets = max_scan_wallets
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
        self._scanned_wallets: Dict[str, float] = {}  # wallet_address -> last_scan_timestamp
        self._rescan_interval = 4 * 3600  # 4小时后允许重新扫描已分析的钱包
        self._running = False
        self._scan_task = None  # 扫描任务引用
        self._on_wallet_discovered = None  # 回调函数
        self._scan_count = 0  # 扫描轮次计数
    
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
    
    def _is_scan_expired(self, wallet: str) -> bool:
        """检查钱包是否可以重新扫描 (超过 rescan_interval 则允许重扫)"""
        import time
        last_scan = self._scanned_wallets.get(wallet)
        if last_scan is None:
            return True
        return (time.monotonic() - last_scan) > self._rescan_interval
    
    async def _discover_wallets(self):
        """发现并分析排名前 N 的高质量钱包
        
        策略:
        1. 种子钱包 (最高优先级，始终分析)
        2. 从 Data API 获取 3000 笔最新交易，按交易次数排名取 Top N
        3. Polygonscan 合约交互 (辅助补充)
        4. 合并去重后，只分析排名前 max_scan_wallets 个钱包
        """
        import time
        self._scan_count += 1
        logger.info(f"开始第 {self._scan_count} 轮扫描 (只分析排名前 {self.max_scan_wallets} 个钱包)...")
        
        # 数据源1 (最高优先级): 手动种子钱包 (不受排名限制)
        seed_candidates = [w for w in self.seed_wallets if self._is_scan_expired(w)]
        
        # 数据源2 (核心): 从 Data API 3000 笔交易中排名 Top N
        ranked_traders = await self._get_top_ranked_traders()
        
        # 数据源3 (辅助): Polygonscan 合约交互者
        contract_interactors = await self._get_contract_interactors()
        
        # 按优先级合并: 种子 > 排名交易者 > 合约交互
        # 种子钱包不占排名配额
        seen = set()
        ordered_candidates = []
        
        # 种子钱包始终优先
        for w in seed_candidates:
            w = w.lower()
            if w not in seen:
                seen.add(w)
                ordered_candidates.append(w)
        
        # 排名交易者 + 合约交互者，限制总数为 max_scan_wallets
        ranked_count = 0
        for wallet in ranked_traders + contract_interactors:
            w = wallet.lower()
            if w not in seen and self._is_scan_expired(w):
                seen.add(w)
                ordered_candidates.append(w)
                ranked_count += 1
                if ranked_count >= self.max_scan_wallets:
                    break
        
        logger.info(
            f"待分析 {len(ordered_candidates)} 个钱包 | "
            f"种子: {len(seed_candidates)} | "
            f"排名Top{self.max_scan_wallets}: {len(ranked_traders)} | "
            f"合约: {len(contract_interactors)} | "
            f"已缓存: {len(self._scanned_wallets)} | 轮次: {self._scan_count}"
        )
        
        # 当所有源都返回0时，输出诊断提示
        if not ordered_candidates and not self._active_wallets:
            logger.warning(
                "所有发现源返回 0 个钱包。请检查: "
                "(1) SEED_WALLETS 环境变量是否配置 "
                "(2) 网络是否能连接 data-api.polymarket.com "
                "(3) POLYGONSCAN_API_KEY 是否配置"
            )
        
        # 逐个分析候选钱包
        analyzed = 0
        for wallet in ordered_candidates:
            if not self._running:
                break
            
            await self._analyze_wallet(wallet)
            self._scanned_wallets[wallet] = time.monotonic()
            analyzed += 1
        
        # 每 5 轮重新评估已跟单的钱包 (用最新交易数据刷新评分)
        if self._scan_count % 5 == 0 and self._active_wallets:
            logger.info(f"第 {self._scan_count} 轮: 重新评估 {len(self._active_wallets)} 个已跟单钱包")
            for wallet in list(self._active_wallets):
                if not self._running:
                    break
                await self._analyze_wallet(wallet)
                self._scanned_wallets[wallet] = time.monotonic()
        
        if analyzed:
            logger.info(f"本轮分析了 {analyzed} 个钱包 (排名前 {self.max_scan_wallets})")
    
    async def _get_top_ranked_traders(self) -> List[str]:
        """从 Polymarket 官方排行榜 API 获取排名前 N 的钱包
        
        端点: GET /v1/leaderboard (公开，无需认证)
        策略: 
        1. 按 PNL 排序取 top 50 (盈利能力最强)
        2. 按 VOLUME 排序取 top 50 (交易量最大)
        3. 合并去重，返回排名前 max_scan_wallets 个
        
        API 文档: https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings
        """
        if self.dry_run:
            return self._generate_mock_traders(min(self.max_scan_wallets, 25))
        
        try:
            if not self._session:
                return []
            
            seen = set()
            ranked_wallets = []  # 保持排名顺序
            
            # 请求多个维度的排行榜，合并发现更多高质量钱包
            queries = [
                {"category": "OVERALL", "timePeriod": "WEEK", "orderBy": "PNL", "limit": 50},
                {"category": "OVERALL", "timePeriod": "WEEK", "orderBy": "VOLUME", "limit": 50},
                {"category": "OVERALL", "timePeriod": "MONTH", "orderBy": "PNL", "limit": 50},
                {"category": "OVERALL", "timePeriod": "DAY", "orderBy": "PNL", "limit": 50},
            ]
            
            for params in queries:
                await self._rate_limited_request()
                try:
                    async with self._session.get(
                        f"{self._data_api}/v1/leaderboard",
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as response:
                        if response.status != 200:
                            logger.warning(
                                f"排行榜 API ({params['timePeriod']}/{params['orderBy']}) "
                                f"返回 HTTP {response.status}"
                            )
                            continue
                        
                        data = await response.json()
                        if not isinstance(data, list):
                            continue
                        
                        for entry in data:
                            addr = entry.get("proxyWallet", "")
                            if addr and addr.startswith("0x"):
                                addr = addr.lower()
                                if addr not in seen:
                                    seen.add(addr)
                                    ranked_wallets.append(addr)
                        
                        desc = f"{params['timePeriod']}/{params['orderBy']}"
                        logger.debug(f"排行榜 {desc}: 获取 {len(data)} 名交易者")
                        
                except Exception as e:
                    logger.warning(f"排行榜 API 请求异常: {e}")
                    continue
            
            # 截取前 max_scan_wallets 个
            result = ranked_wallets[:self.max_scan_wallets]
            
            if result:
                logger.info(
                    f"官方排行榜: 获取 {len(ranked_wallets)} 个唯一钱包, "
                    f"取前 {len(result)} 名"
                )
            else:
                logger.warning("官方排行榜 API 未返回任何钱包")
            
            return result
            
        except Exception as e:
            logger.error(f"获取排行榜交易者失败: {e}")
            return []
    
    async def _get_contract_interactors(self) -> List[str]:
        """从 Polygonscan 获取与 Polymarket 合约交互的钱包"""
        if self.dry_run:
            return self._generate_mock_traders(10)
        
        if not self.polygonscan_api_key:
            logger.warning("POLYGONSCAN_API_KEY 未配置，跳过合约交互者发现")
            return []
        
        traders = []
        
        try:
            if not self._session:
                return []
            
            for contract_name, contract_addr in self.polymarket_contracts.items():
                url = "https://api.polygonscan.com/api"
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
                
                await self._rate_limited_request()
                async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        logger.warning(f"Polygonscan API 返回 HTTP {response.status} (合约: {contract_name})")
                        continue
                    
                    data = await response.json()
                    if data.get("status") == "1":
                        txs = data.get("result", [])
                        for tx in txs:
                            if tx.get("from", "").startswith("0x"):
                                traders.append(tx["from"].lower())
                        logger.info(f"Polygonscan {contract_name}: {len(txs)} 笔交易")
                    else:
                        msg = data.get('message', 'unknown')
                        result = data.get('result', '')
                        logger.warning(f"Polygonscan {contract_name} 返回错误: {msg} - {result}")
            
            unique = list(set(traders))
            if unique:
                logger.info(f"从 Polygonscan 获取 {len(unique)} 个唯一交易者")
            return unique
            
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
        
        Data API /trades 实际返回字段:
          - proxyWallet, side ("BUY"/"SELL"), size, price, outcome,
          - outcomeIndex, timestamp, transactionHash, conditionId,
          - title, slug, eventSlug, name, asset
        
        quality_scorer._calculate_stats() 需要的字段:
          - pnl (盈亏)
          - pnl_pct (盈亏百分比)
          - category (市场类别)
          - timestamp (ISO或Unix时间戳)
          - hold_hours (持仓小时数)
        
        PnL 估算策略 (二元市场):
          - BUY: 买入成本 = price * size, 潜在最大收益 = (1-price)*size
            好的买入: price < 0.5 (低价买入=高期望值)
          - SELL: 卖出收入 = price * size, 估算PnL = (price - 0.5) * size
            price > 0.5 = 盈利平仓, price < 0.5 = 亏损平仓
        """
        normalized = []
        
        for trade in trades:
            # 提取或计算 PnL
            pnl = trade.get("pnl") or trade.get("profit") or trade.get("realized_pnl")
            
            side = str(trade.get("side", "")).upper()
            price = float(trade.get("price", 0) or 0)
            size = float(trade.get("size", 0) or 0)
            
            if pnl is None and price > 0 and size > 0:
                if side == "SELL":
                    # SELL = 平仓: 估算 PnL = (卖出价 - 假设成本0.5) * size
                    pnl = (price - 0.5) * size
                elif side == "BUY":
                    # BUY = 开仓: 用价格偏离度估算交易质量
                    # 低价买入(price<0.4): 高期望值 → 正向信号
                    # 高价买入(price>0.7): 接盘 → 负向信号
                    # 中间价(0.4-0.7): 中性
                    if price < 0.35:
                        pnl = size * 0.15   # 好的入场
                    elif price > 0.75:
                        pnl = size * -0.10  # 差的入场 (追高)
                    else:
                        pnl = size * 0.02   # 中性
                else:
                    pnl = 0
            
            pnl = float(pnl or 0)
            cost = price * size if price > 0 and size > 0 else 1
            pnl_pct = pnl / cost if cost > 0 else 0
            
            # 时间戳标准化
            timestamp = trade.get("timestamp") or trade.get("created_at") or trade.get("time")
            if isinstance(timestamp, (int, float)):
                from datetime import datetime as dt
                if timestamp > 1e12:
                    timestamp = dt.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
                else:
                    timestamp = dt.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
            
            # 市场类别: 从 title/eventSlug 提取
            category = trade.get("category") or trade.get("market_category")
            if not category:
                category = self._infer_category(trade)
            
            normalized.append({
                **trade,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "category": category,
                "timestamp": timestamp or "",
                "hold_hours": trade.get("hold_hours", 24),
            })
        
        return normalized
    
    @staticmethod
    def _infer_category(trade: Dict[str, Any]) -> str:
        """从交易的 title/slug/eventSlug 推断市场类别"""
        text = " ".join([
            str(trade.get("title", "")),
            str(trade.get("slug", "")),
            str(trade.get("eventSlug", "")),
        ]).lower()
        
        if not text.strip():
            return "general"
        
        # 关键词 → 类别映射
        categories = {
            "crypto": ["bitcoin", "btc", "eth", "ethereum", "crypto", "token", "defi", "solana", "sol"],
            "politics": ["president", "election", "trump", "biden", "vote", "congress", "senate", "governor", "political", "democrat", "republican"],
            "sports": ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball", "baseball", "champion", "playoff", "ncaa", "ufc", "fight"],
            "finance": ["fed", "interest rate", "inflation", "gdp", "stock", "market cap", "s&p", "nasdaq", "recession"],
            "entertainment": ["oscar", "grammy", "movie", "film", "album", "music", "celebrity", "emmy"],
            "technology": ["ai", "openai", "gpt", "apple", "google", "meta", "microsoft", "tesla", "spacex"],
            "world": ["war", "ukraine", "russia", "china", "nato", "un ", "climate", "earthquake"],
        }
        
        for cat, keywords in categories.items():
            if any(kw in text for kw in keywords):
                return cat
        
        return "general"
    
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
            "scanned_wallets_cached": len(self._scanned_wallets),
            "scan_count": self._scan_count,
            "active_wallet_addresses": list(self._active_wallets),
        }
