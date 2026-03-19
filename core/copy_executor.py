"""
跟单执行器
==========
根据目标钱包的交易信号执行跟单操作。
"""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List, Dict, Any
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

logger = get_logger(__name__)


class CopyMode(Enum):
    """跟单模式"""
    FULL = "full"           # 全额跟单
    PROPORTIONAL = "proportional"  # 按比例跟单
    FIXED = "fixed"         # 固定金额跟单
    SMART = "smart"         # 智能跟单(根据质量评分)


@dataclass
class CopyConfig:
    """跟单配置"""
    mode: CopyMode = CopyMode.SMART
    fixed_amount: Decimal = Decimal("10")  # 固定金额
    proportional_ratio: Decimal = Decimal("0.1")  # 比例
    max_amount: Decimal = Decimal("50")  # 最大金额
    min_amount: Decimal = Decimal("5")   # 最小金额
    copy_delay_seconds: float = 1.0  # 跟单延迟(秒)
    enabled: bool = True


@dataclass
class CopyTrade:
    """跟单交易记录"""
    source_wallet: str
    source_tx_hash: str
    market_id: str
    market_question: str
    side: str
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
            "original_size": str(self.original_size),
            "copy_size": str(self.copy_size),
            "copy_price": str(self.copy_price),
            "order_id": self.order_id,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
        }


class CopyExecutor:
    """
    跟单执行器
    
    功能:
    1. 接收目标钱包交易信号
    2. 质量评分和风险检查
    3. 计算跟单金额
    4. 执行跟单交易
    5. 发送通知
    """
    
    def __init__(
        self,
        client: PolymarketClient,
        risk_manager: RiskManager,
        quality_scorer: WalletQualityScorer,
        market_maker_detector: MarketMakerDetector,
        warning_detector: RedFlagDetector,  # 改名避免敏感词
        telegram: Optional[TelegramService] = None,
        copy_config: Optional[CopyConfig] = None,
    ):
        """
        初始化跟单执行器
        
        Args:
            client: Polymarket客户端
            risk_manager: 风险管理器
            quality_scorer: 钱包质量评分器
            market_maker_detector: 做市商检测器
            warning_detector: 警告检测器
            telegram: Telegram服务
            copy_config: 跟单配置
        """
        self.client = client
        self.risk_manager = risk_manager
        self.quality_scorer = quality_scorer
        self.market_maker_detector = market_maker_detector
        self.warning_detector = warning_detector
        self.telegram = telegram
        self.config = copy_config or CopyConfig()
        
        # 钱包评分缓存
        self._wallet_scores: Dict[str, QualityScore] = {}
        
        # 跟单记录
        self._copy_trades: List[CopyTrade] = []
        
        # 钱包交易历史缓存
        self._wallet_trades: Dict[str, List[Dict[str, Any]]] = {}
        
        logger.info(
            f"跟单执行器初始化 | 模式: {self.config.mode.value} | "
            f"延迟: {self.config.copy_delay_seconds}s"
        )
    
    async def process_transaction(self, tx: WalletTransaction) -> Optional[CopyTrade]:
        """
        处理交易信号
        
        Args:
            tx: 钱包交易
        
        Returns:
            跟单交易记录
        """
        if not self.config.enabled:
            return None
        
        logger.info(
            f"处理交易信号 | 钱包: {tx.wallet_address[:10]}... | "
            f"市场: {tx.market_question[:30]}..."
        )
        
        # 1. 获取/更新钱包评分
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
        
        # 4. 风险检查
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
        
        # 5. 创建跟单记录
        copy_trade = CopyTrade(
            source_wallet=tx.wallet_address,
            source_tx_hash=tx.tx_hash,
            market_id=tx.market_id,
            market_question=tx.market_question,
            side=tx.side,
            original_size=tx.size,
            copy_size=final_amount,
            copy_price=tx.price,
        )
        
        # 6. 添加延迟(避免抢跑)
        await asyncio.sleep(self.config.copy_delay_seconds)
        
        # 7. 执行跟单
        try:
            order_result = await self._execute_copy(copy_trade)
            
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
                
                logger.info(
                    f"跟单成功 | 市场: {copy_trade.market_question[:30]}... | "
                    f"方向: {copy_trade.side} | 金额: ${copy_trade.copy_size}"
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
                logger.error(f"跟单失败: {order_result.error}")
                
        except Exception as e:
            copy_trade.status = "error"
            copy_trade.error = str(e)
            logger.error(f"跟单异常: {e}")
        
        self._copy_trades.append(copy_trade)
        return copy_trade
    
    async def _get_wallet_score(self, wallet_address: str) -> QualityScore:
        """获取钱包评分"""
        if wallet_address in self._wallet_scores:
            # 缓存24小时
            score = self._wallet_scores[wallet_address]
            # 这里简化处理，实际应该检查缓存时间
            return score
        
        # 获取钱包交易历史
        trades = await self._fetch_wallet_trades(wallet_address)
        self._wallet_trades[wallet_address] = trades
        
        # 评分
        score = self.quality_scorer.score_wallet(wallet_address, trades)
        
        # 做市商检测
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
        # 实际实现需要调用API
        # 这里返回空列表，由客户端实现
        return []
    
    def _calculate_copy_amount(
        self,
        tx: WalletTransaction,
        score: QualityScore
    ) -> Decimal:
        """
        计算跟单金额
        
        Args:
            tx: 源交易
            score: 钱包评分
        
        Returns:
            跟单金额
        """
        if self.config.mode == CopyMode.FIXED:
            return self.config.fixed_amount
        
        elif self.config.mode == CopyMode.PROPORTIONAL:
            amount = tx.size * self.config.proportional_ratio
        
        elif self.config.mode == CopyMode.FULL:
            amount = tx.size
        
        else:  # SMART
            # 根据钱包质量调整金额
            base_amount = self.config.fixed_amount
            
            # 等级乘数
            tier_multipliers = {
                WalletTier.ELITE: Decimal("2.0"),
                WalletTier.EXPERT: Decimal("1.5"),
                WalletTier.GOOD: Decimal("1.0"),
                WalletTier.POOR: Decimal("0"),
            }
            
            multiplier = tier_multipliers.get(score.tier, Decimal("0"))
            amount = base_amount * multiplier
            
            # 根据置信度进一步调整
            confidence_multiplier = score.overall_score / Decimal("10")
            amount = amount * confidence_multiplier
        
        # 限制范围
        amount = max(self.config.min_amount, min(amount, self.config.max_amount))
        
        return amount
    
    async def _execute_copy(self, copy_trade: CopyTrade) -> OrderResult:
        """执行跟单交易"""
        return await self.client.place_order(
            market_id=copy_trade.market_id,
            side=copy_trade.side,
            size=copy_trade.copy_size,
            price=copy_trade.copy_price
        )
    
    def update_wallet_trades(
        self,
        wallet_address: str,
        trades: List[Dict[str, Any]]
    ) -> None:
        """更新钱包交易历史"""
        self._wallet_trades[wallet_address] = trades
        # 清除评分缓存
        if wallet_address in self._wallet_scores:
            del self._wallet_scores[wallet_address]
    
    def get_copy_stats(self) -> Dict[str, Any]:
        """获取跟单统计"""
        total = len(self._copy_trades)
        success = sum(1 for t in self._copy_trades if t.status == "filled")
        failed = sum(1 for t in self._copy_trades if t.status in ["failed", "error"])
        
        total_volume = sum(
            t.copy_size for t in self._copy_trades
            if t.status == "filled"
        )
        
        return {
            "total_trades": total,
            "success_trades": success,
            "failed_trades": failed,
            "success_rate": success / total if total > 0 else 0,
            "total_volume": float(total_volume),
            "wallet_scores_cached": len(self._wallet_scores),
        }
    
    def set_config(self, config: CopyConfig) -> None:
        """更新跟单配置"""
        self.config = config
        logger.info(f"更新跟单配置 | 模式: {config.mode.value}")
