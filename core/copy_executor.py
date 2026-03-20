"""
跟单执行器
==========
根据目标钱包的交易信号执行跟单操作。

核心修复:
1. 支持开仓跟单 (buy)
2. 支持平仓跟单 (sell) - 避免持仓失控!
3. 使用 Data API 监控目标钱包持仓变化
"""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List, Dict, Any, Set
from datetime import datetime, timezone
from enum import Enum

from utils.logger import get_logger
from core.risk_manager import RiskManager
from core.circuit_breaker import CircuitBreaker
from core.wallet_quality_scorer import QualityScore, WalletQualityScorer, WalletTier
from core.market_maker_detector import MarketMakerDetector
from core.red_flag_detector import RedFlagDetector, RedFlag
from services.polymarket_client import PolymarketClient, OrderResult
from services.telegram_service import TelegramService
from core.wallet_monitor import WalletTransaction
from utils.trade_persistence import TradePersistence  # 交易持久化

logger = get_logger(__name__)


class CopyMode(Enum):
    """跟单模式"""
    FULL = "full"
    PROPORTIONAL = "proportional"
    FIXED = "fixed"
    SMART = "smart"


class CopyAction(Enum):
    """跟单动作"""
    OPEN = "open"      # 开仓
    CLOSE = "close"    # 平仓
    ADJUST = "adjust"  # 调仓


@dataclass
class CopyConfig:
    """跟单配置"""
    mode: CopyMode = CopyMode.SMART
    fixed_amount: Decimal = Decimal("10")
    proportional_ratio: Decimal = Decimal("0.1")
    max_amount: Decimal = Decimal("50")
    min_amount: Decimal = Decimal("5")
    copy_delay_seconds: float = 1.0
    enabled: bool = True
    # 平仓跟单配置
    follow_close: bool = True  # 是否跟平仓
    close_on_target_close: bool = True  # 目标平仓时自动平仓
    position_sync_interval: int = 300  # 持仓同步间隔(秒)，可通过环境变量配置


@dataclass
class CopyTrade:
    """跟单交易记录"""
    source_wallet: str
    source_tx_hash: str
    market_id: str
    market_question: str
    side: str
    action: CopyAction  # OPEN / CLOSE
    original_size: Decimal
    copy_size: Decimal
    copy_price: Decimal
    order_id: Optional[str] = None
    status: str = "pending"
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    executed_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_wallet": self.source_wallet,
            "source_tx_hash": self.source_tx_hash,
            "market_id": self.market_id,
            "market_question": self.market_question,
            "side": self.side,
            "action": self.action.value,
            "original_size": str(self.original_size),
            "copy_size": str(self.copy_size),
            "copy_price": str(self.copy_price),
            "order_id": self.order_id,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
        }


@dataclass
class TrackedPosition:
    """跟踪的持仓"""
    source_wallet: str
    market_id: str
    side: str  # 目标钱包的持仓方向
    source_size: Decimal  # 目标钱包持仓量
    our_size: Decimal  # 我们的跟单持仓量
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class CopyExecutor:
    """
    跟单执行器
    
    功能:
    1. 接收目标钱包交易信号
    2. 质量评分和风险检查
    3. 计算跟单金额
    4. 执行跟单交易 (开仓 + 平仓!)
    5. 发送通知
    6. 定期同步持仓状态
    """
    
    def __init__(
        self,
        client: PolymarketClient,
        risk_manager: RiskManager,
        quality_scorer: WalletQualityScorer,
        market_maker_detector: MarketMakerDetector,
        warning_detector: RedFlagDetector,
        telegram: Optional[TelegramService] = None,
        copy_config: Optional[CopyConfig] = None,
        persistence: Optional[TradePersistence] = None,  # 交易持久化
    ):
        self.client = client
        self.risk_manager = risk_manager
        self.quality_scorer = quality_scorer
        self.market_maker_detector = market_maker_detector
        self.warning_detector = warning_detector
        self.telegram = telegram
        self.config = copy_config or CopyConfig()
        self.persistence = persistence  # 持久化管理器
        
        # 钱包评分缓存
        self._wallet_scores: Dict[str, QualityScore] = {}
        
        # 跟单记录
        self._copy_trades: List[CopyTrade] = []
        
        # 钱包交易历史缓存
        self._wallet_trades: Dict[str, List[Dict[str, Any]]] = {}
        
        # 跟踪的持仓: key = f"{wallet_address}:{market_id}"
        self._tracked_positions: Dict[str, TrackedPosition] = {}
        
        # 持仓同步任务
        self._sync_task: Optional[asyncio.Task] = None
        self._running = False
        
        logger.info(
            f"跟单执行器初始化 | 模式: {self.config.mode.value} | "
            f"延迟: {self.config.copy_delay_seconds}s | "
            f"跟平仓: {'是' if self.config.follow_close else '否'} | "
            f"持久化: {'启用' if self.persistence else '禁用'}"
        )
    
    async def start(self) -> None:
        """启动跟单执行器"""
        self._running = True
        # 启动持仓同步任务
        if self.config.close_on_target_close:
            self._sync_task = asyncio.create_task(self._position_sync_loop())
            logger.info("持仓同步任务已启动")
    
    async def stop(self) -> None:
        """停止跟单执行器"""
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
    
    # ═══════════════════════════════════════════════════════════════
    # 核心跟单逻辑
    # ═══════════════════════════════════════════════════════════════
    
    async def process_transaction(self, tx: WalletTransaction) -> Optional[CopyTrade]:
        """
        处理交易信号 (支持开仓和平仓!)
        
        Args:
            tx: 钱包交易
        
        Returns:
            跟单交易记录
        """
        if not self.config.enabled:
            return None
        
        # 幂等性检查 (防止重复跟单)
        if self.persistence:
            is_processed = await self.persistence.is_processed(tx.tx_hash)
            if is_processed:
                logger.info(
                    f"跳过已处理交易 | Hash: {tx.tx_hash[:10]}... | "
                    f"钱包: {tx.wallet_address[:10]}..."
                )
                return None
        
        logger.info(
            f"处理交易信号 | 钱包: {tx.wallet_address[:10]}... | "
            f"市场: {tx.market_question[:30]}... | "
            f"类型: {tx.tx_type}"
        )
        
        # 根据交易类型决定动作
        if tx.tx_type == "sell":
            # 平仓信号!
            return await self._handle_close_signal(tx)
        else:
            # 开仓信号
            return await self._handle_open_signal(tx)
    
    async def _handle_open_signal(self, tx: WalletTransaction) -> Optional[CopyTrade]:
        """
        处理开仓信号
        
        Args:
            tx: 交易信号
        """
        # 1. 获取钱包评分
        score = await self._get_wallet_score(tx.wallet_address)
        
        if not score.should_follow:
            logger.info(
                f"跳过跟单 | 钱包: {tx.wallet_address[:10]}... | "
                f"原因: 不建议跟单 (等级: {score.tier.value})"
            )
            return None
        
        # 2. 检测警告信号
        wallet_history = self._wallet_trades.get(tx.wallet_address, [])
        warning_flags = self.warning_detector.detect(tx.wallet_address, wallet_history)
        
        block, reason = self.warning_detector.should_block_trading(warning_flags)
        if block:
            logger.warning(f"阻止跟单: {reason}")
            return None
        
        # 3. 计算跟单金额
        copy_amount = self._calculate_copy_amount(tx, score)
        
        if copy_amount < self.config.min_amount:
            logger.info(f"跟单金额过小: ${copy_amount}")
            return None
        
        # 4. 检查是否已有该市场持仓
        existing_position = self.risk_manager.get_position(tx.market_id)
        if existing_position:
            logger.info(f"已有该市场持仓，跳过开仓")
            return None
        
        # 5. 风险检查
        risk_result = self.risk_manager.check_trade(
            market_id=tx.market_id,
            side=tx.side,
            requested_size=copy_amount,
            confidence_score=score.overall_score / Decimal("10"),
            price=tx.price
        )
        
        if not risk_result.allowed:
            logger.info(f"风险检查未通过: {risk_result.reason}")
            return None
        
        final_amount = risk_result.suggested_size or copy_amount
        
        # 6. 创建跟单记录
        copy_trade = CopyTrade(
            source_wallet=tx.wallet_address,
            source_tx_hash=tx.tx_hash,
            market_id=tx.market_id,
            market_question=tx.market_question,
            side=tx.side,
            action=CopyAction.OPEN,
            original_size=tx.size,
            copy_size=final_amount,
            copy_price=tx.price,
        )
        
        # 7. 添加延迟
        await asyncio.sleep(self.config.copy_delay_seconds)
        
        # 8. 执行开仓
        try:
            order_result = await self._execute_open(copy_trade)
            
            if order_result.success:
                copy_trade.order_id = order_result.order_id
                copy_trade.status = "filled"
                copy_trade.executed_at = datetime.now(timezone.utc)
                
                # 记录持仓
                self.risk_manager.open_position(
                    market_id=copy_trade.market_id,
                    market_question=copy_trade.market_question,
                    side=copy_trade.side,
                    price=order_result.filled_price,
                    size=order_result.filled_size
                )
                
                # 跟踪持仓
                self._track_position(
                    wallet_address=tx.wallet_address,
                    market_id=tx.market_id,
                    side=tx.side,
                    source_size=tx.size,
                    our_size=order_result.filled_size
                )
                
                logger.info(
                    f"跟单开仓成功 | 市场: {copy_trade.market_question[:30]}... | "
                    f"方向: {copy_trade.side} | 金额: ${copy_trade.copy_size}"
                )
                
                # 标记为已处理（幂等性）
                if self.persistence:
                    await self.persistence.mark_processed(
                        tx_hash=tx.tx_hash,
                        wallet_address=tx.wallet_address,
                        market_id=tx.market_id,
                        action="open",
                        status="success",
                        copy_size=order_result.filled_size,
                        copy_price=order_result.filled_price
                    )
                
                # 发送通知
                if self.telegram:
                    await self.telegram.send_trade_notification(
                        action="跟单开仓",
                        market_question=copy_trade.market_question,
                        side=copy_trade.side,
                        amount=copy_trade.copy_size,
                        price=order_result.filled_price,
                        confidence=score.overall_score / Decimal("10"),
                        strategy=f"copy_{score.tier.value}"
                    )
            else:
                copy_trade.status = "failed"
                copy_trade.error = order_result.error
                logger.error(f"跟单开仓失败: {order_result.error}")
                
        except Exception as e:
            copy_trade.status = "error"
            copy_trade.error = str(e)
            logger.error(f"跟单开仓异常: {e}")
        
        self._copy_trades.append(copy_trade)
        return copy_trade
    
    async def _handle_close_signal(self, tx: WalletTransaction) -> Optional[CopyTrade]:
        """
        处理平仓信号 (关键修复!)
        
        当目标钱包卖出时，我们也应该平仓
        """
        if not self.config.follow_close:
            logger.debug("平仓跟单已禁用")
            return None
        
        # 查找我们是否跟了这个持仓
        track_key = f"{tx.wallet_address.lower()}:{tx.market_id}"
        tracked = self._tracked_positions.get(track_key)
        
        if not tracked:
            logger.debug(f"未跟踪该持仓，跳过平仓: {track_key}")
            return None
        
        # 检查我们是否还有持仓
        our_position = self.risk_manager.get_position(tx.market_id)
        
        if not our_position:
            logger.info(f"已无该市场持仓，无需平仓")
            # 清理跟踪记录
            del self._tracked_positions[track_key]
            return None
        
        # 检查方向是否匹配
        if our_position.side != tracked.side:
            logger.warning(f"持仓方向不匹配，跳过平仓")
            return None
        
        # 创建平仓记录
        copy_trade = CopyTrade(
            source_wallet=tx.wallet_address,
            source_tx_hash=tx.tx_hash,
            market_id=tx.market_id,
            market_question=tx.market_question,
            side=our_position.side,  # 我们持有的方向
            action=CopyAction.CLOSE,
            original_size=tx.size,
            copy_size=our_position.size,
            copy_price=tx.price,
        )
        
        # 执行平仓
        try:
            order_result = await self.client.close_position(
                market_id=tx.market_id,
                side=our_position.side,
                size=our_position.size
            )
            
            if order_result.success:
                copy_trade.order_id = order_result.order_id
                copy_trade.status = "filled"
                copy_trade.executed_at = datetime.now(timezone.utc)
                
                # 记录平仓
                closed_position = self.risk_manager.close_position(
                    market_id=tx.market_id,
                    exit_price=order_result.filled_price
                )
                
                # 清理跟踪记录
                if track_key in self._tracked_positions:
                    del self._tracked_positions[track_key]
                
                logger.info(
                    f"跟单平仓成功 | 市场: {copy_trade.market_question[:30]}... | "
                    f"盈亏: ${closed_position.pnl if closed_position else 'N/A'}"
                )
                
                # 标记为已处理（幂等性）
                if self.persistence:
                    await self.persistence.mark_processed(
                        tx_hash=tx.tx_hash,
                        wallet_address=tx.wallet_address,
                        market_id=tx.market_id,
                        action="close",
                        status="success",
                        copy_size=our_position.size,
                        copy_price=order_result.filled_price
                    )
                
                # 发送通知
                if self.telegram and closed_position:
                    pnl_type = "盈利" if closed_position.pnl >= 0 else "亏损"
                    await self.telegram.send_message(
                        f"📤 跟单平仓\n"
                        f"市场: {copy_trade.market_question[:40]}...\n"
                        f"方向: {copy_trade.side}\n"
                        f"{pnl_type}: ${abs(closed_position.pnl):.2f}\n"
                        f"收益率: {closed_position.pnl_percentage:.1f}%"
                    )
            else:
                copy_trade.status = "failed"
                copy_trade.error = order_result.error
                logger.error(f"跟单平仓失败: {order_result.error}")
                
        except Exception as e:
            copy_trade.status = "error"
            copy_trade.error = str(e)
            logger.error(f"跟单平仓异常: {e}")
        
        self._copy_trades.append(copy_trade)
        return copy_trade
    
    async def _execute_open(self, copy_trade: CopyTrade) -> OrderResult:
        """执行开仓"""
        # 使用市价单快速跟单
        return await self.client.place_market_order(
            market_id=copy_trade.market_id,
            side=copy_trade.side,
            size=copy_trade.copy_size,
        )
    
    # ═══════════════════════════════════════════════════════════════
    # 持仓同步 (检测目标平仓我们未跟的情况)
    # ═══════════════════════════════════════════════════════════════
    
    async def _position_sync_loop(self) -> None:
        """
        持仓同步循环
        
        定期检查:
        1. 目标钱包已平仓但我们还持有 -> 自动平仓
        2. 持仓状态不一致 -> 告警
        """
        while self._running:
            try:
                await asyncio.sleep(self.config.position_sync_interval)
                
                if not self._running:
                    break
                
                await self._sync_positions()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"持仓同步异常: {e}")
    
    async def _sync_positions(self) -> None:
        """同步持仓状态"""
        # 获取所有跟踪的持仓
        positions_to_check = list(self._tracked_positions.items())
        
        for track_key, tracked in positions_to_check:
            try:
                # 获取目标钱包当前持仓
                target_positions = await self.client.get_user_positions(
                    wallet_address=tracked.source_wallet,
                    market_id=tracked.market_id
                )
                
                # 检查目标是否还有这个持仓
                target_has_position = False
                for pos in target_positions:
                    if pos.get("market") == tracked.market_id:
                        target_has_position = True
                        break
                
                if not target_has_position:
                    # 目标已平仓，检查我们是否还持有
                    our_position = self.risk_manager.get_position(tracked.market_id)
                    
                    if our_position:
                        logger.warning(
                            f"⚠️ 目标已平仓但我们还持有! | "
                            f"市场: {tracked.market_id} | "
                            f"自动平仓..."
                        )
                        
                        # 自动平仓
                        await self._auto_close_position(tracked)
                
            except Exception as e:
                logger.error(f"同步持仓失败 {track_key}: {e}")
    
    async def _auto_close_position(self, tracked: TrackedPosition) -> None:
        """自动平仓"""
        our_position = self.risk_manager.get_position(tracked.market_id)
        
        if not our_position:
            return
        
        # 创建平仓交易
        copy_trade = CopyTrade(
            source_wallet=tracked.source_wallet,
            source_tx_hash="auto_sync",
            market_id=tracked.market_id,
            market_question=our_position.market_question,
            side=our_position.side,
            action=CopyAction.CLOSE,
            original_size=tracked.source_size,
            copy_size=our_position.size,
            copy_price=our_position.current_price,
        )
        
        # 执行平仓
        order_result = await self.client.close_position(
            market_id=tracked.market_id,
            side=our_position.side,
            size=our_position.size
        )
        
        if order_result.success:
            copy_trade.status = "filled"
            closed = self.risk_manager.close_position(
                market_id=tracked.market_id,
                exit_price=order_result.filled_price
            )
            
            # 清理跟踪
            track_key = f"{tracked.source_wallet.lower()}:{tracked.market_id}"
            if track_key in self._tracked_positions:
                del self._tracked_positions[track_key]
            
            if self.telegram and closed:
                await self.telegram.send_message(
                    f"🔄 自动同步平仓\n"
                    f"市场: {our_position.market_question[:40]}...\n"
                    f"原因: 目标钱包已平仓"
                )
        
        self._copy_trades.append(copy_trade)
    
    # ═══════════════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════════════
    
    def _track_position(
        self,
        wallet_address: str,
        market_id: str,
        side: str,
        source_size: Decimal,
        our_size: Decimal,
    ) -> None:
        """跟踪持仓"""
        key = f"{wallet_address.lower()}:{market_id}"
        self._tracked_positions[key] = TrackedPosition(
            source_wallet=wallet_address,
            market_id=market_id,
            side=side,
            source_size=source_size,
            our_size=our_size,
        )
    
    async def _get_wallet_score(self, wallet_address: str) -> QualityScore:
        """获取钱包评分"""
        if wallet_address in self._wallet_scores:
            return self._wallet_scores[wallet_address]
        
        trades = await self._fetch_wallet_trades(wallet_address)
        self._wallet_trades[wallet_address] = trades
        
        score = self.quality_scorer.score_wallet(wallet_address, trades)
        
        mm_score = self.market_maker_detector.detect(wallet_address, trades)
        if mm_score.is_market_maker:
            score.is_market_maker = True
            logger.warning(f"检测到做市商: {wallet_address[:10]}...")
        
        self._wallet_scores[wallet_address] = score
        return score
    
    async def _fetch_wallet_trades(
        self,
        wallet_address: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """获取钱包交易历史"""
        return await self.client.get_user_trades(wallet_address, limit=limit)
    
    def _calculate_copy_amount(
        self,
        tx: WalletTransaction,
        score: QualityScore
    ) -> Decimal:
        """计算跟单金额"""
        if self.config.mode == CopyMode.FIXED:
            return self.config.fixed_amount
        
        elif self.config.mode == CopyMode.PROPORTIONAL:
            amount = tx.size * self.config.proportional_ratio
        
        elif self.config.mode == CopyMode.FULL:
            amount = tx.size
        
        else:  # SMART
            base_amount = self.config.fixed_amount
            
            tier_multipliers = {
                WalletTier.ELITE: Decimal("2.0"),
                WalletTier.EXPERT: Decimal("1.5"),
                WalletTier.GOOD: Decimal("1.0"),
                WalletTier.POOR: Decimal("0"),
            }
            
            multiplier = tier_multipliers.get(score.tier, Decimal("0"))
            amount = base_amount * multiplier
            
            confidence_multiplier = score.overall_score / Decimal("10")
            amount = amount * confidence_multiplier
        
        amount = max(self.config.min_amount, min(amount, self.config.max_amount))
        return amount
    
    def get_copy_stats(self) -> Dict[str, Any]:
        """获取跟单统计"""
        total = len(self._copy_trades)
        opens = sum(1 for t in self._copy_trades if t.action == CopyAction.OPEN)
        closes = sum(1 for t in self._copy_trades if t.action == CopyAction.CLOSE)
        success = sum(1 for t in self._copy_trades if t.status == "filled")
        failed = sum(1 for t in self._copy_trades if t.status in ["failed", "error"])
        
        total_volume = sum(
            t.copy_size for t in self._copy_trades
            if t.status == "filled"
        )
        
        return {
            "total_trades": total,
            "open_trades": opens,
            "close_trades": closes,
            "success_trades": success,
            "failed_trades": failed,
            "success_rate": success / total if total > 0 else 0,
            "total_volume": float(total_volume),
            "tracked_positions": len(self._tracked_positions),
            "wallet_scores_cached": len(self._wallet_scores),
        }
    
    def set_config(self, config: CopyConfig) -> None:
        """更新跟单配置"""
        self.config = config
        logger.info(f"更新跟单配置 | 模式: {config.mode.value} | 跟平仓: {config.follow_close}")
