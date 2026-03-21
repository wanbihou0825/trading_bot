"""
跟单执行器单元测试
==================
测试跟单逻辑和交易执行流程。
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from core.copy_executor import CopyExecutor, CopyConfig, CopyMode, CopyTrade, CopyAction
from core.risk_manager import RiskManager
from core.circuit_breaker import CircuitBreaker
from core.wallet_quality_scorer import WalletQualityScorer, QualityScore, WalletTier, TradingStats
from core.market_maker_detector import MarketMakerDetector, MarketMakerScore, MarketMakerType
from core.red_flag_detector import RedFlagDetector, RedFlag, RedFlagType
from services.polymarket_client import PolymarketClient, OrderResult
from core.wallet_monitor import WalletTransaction


class TestCopyExecutor:
    """跟单执行器测试类"""
    
    @pytest.fixture
    def copy_executor(self, mock_polymarket_client, mock_risk_manager, mock_telegram_service):
        """创建跟单执行器实例"""
        quality_scorer = MagicMock(spec=WalletQualityScorer)
        quality_scorer.score_wallet.return_value = QualityScore(
            wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
            tier=WalletTier.EXPERT,
            overall_score=Decimal("8.0"),
            win_rate_score=Decimal("8.0"),
            profit_factor_score=Decimal("8.0"),
            consistency_score=Decimal("8.0"),
            risk_score=Decimal("8.0"),
            specialty_score=Decimal("8.0"),
            stats=TradingStats(
                total_trades=100,
                winning_trades=65,
                losing_trades=35,
                total_profit=Decimal("130"),
                total_loss=Decimal("72"),
                max_drawdown=Decimal("10")
            )
        )
        
        market_maker_detector = MagicMock(spec=MarketMakerDetector)
        market_maker_detector.detect.return_value = MarketMakerScore(
            wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
            is_market_maker=False,
            maker_type=MarketMakerType.UNKNOWN,
            confidence=Decimal("0.2"),
            patterns=[],
            stats={},
            recommendation="neutral"
        )
        
        warning_detector = MagicMock(spec=RedFlagDetector)
        warning_detector.detect.return_value = []
        warning_detector.should_block_trading.return_value = (False, "")
        
        config = CopyConfig(
            mode=CopyMode.SMART,
            fixed_amount=Decimal("10"),
            max_amount=Decimal("50"),
            min_amount=Decimal("5"),
            copy_delay_seconds=0.1  # 测试时使用短延迟
        )
        
        executor = CopyExecutor(
            client=mock_polymarket_client,
            risk_manager=mock_risk_manager,
            quality_scorer=quality_scorer,
            market_maker_detector=market_maker_detector,
            warning_detector=warning_detector,
            telegram=mock_telegram_service,
            copy_config=config
        )
        
        return executor
    
    @pytest.mark.asyncio
    async def test_process_transaction_success(self, copy_executor, sample_wallet_transaction):
        """测试成功处理交易"""
        result = await copy_executor.process_transaction(sample_wallet_transaction)
        
        assert result is not None
        assert result.status == "filled"
        assert result.order_id is not None
    
    @pytest.mark.asyncio
    async def test_process_transaction_disabled(self, copy_executor, sample_wallet_transaction):
        """测试禁用跟单"""
        copy_executor.config.enabled = False
        
        result = await copy_executor.process_transaction(sample_wallet_transaction)
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_process_transaction_low_quality(self, copy_executor, sample_wallet_transaction):
        """测试低质量钱包跳过"""
        # 修改返回低质量评分
        copy_executor.quality_scorer.score_wallet.return_value = QualityScore(
            wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
            tier=WalletTier.POOR,
            overall_score=Decimal("3.0"),
            win_rate_score=Decimal("3.0"),
            profit_factor_score=Decimal("2.0"),
            consistency_score=Decimal("2.0"),
            risk_score=Decimal("3.0"),
            specialty_score=Decimal("2.0"),
            stats=TradingStats(
                total_trades=10,
                winning_trades=4,
                losing_trades=6,
                total_profit=Decimal("20"),
                total_loss=Decimal("40"),
                max_drawdown=Decimal("30")
            )
        )
        
        result = await copy_executor.process_transaction(sample_wallet_transaction)
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_process_transaction_risk_blocked(self, copy_executor, sample_wallet_transaction):
        """测试风险检查阻止"""
        # 触发熔断器
        copy_executor.risk_manager.circuit_breaker._trigger("测试触发")
        
        result = await copy_executor.process_transaction(sample_wallet_transaction)
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_process_transaction_warning_blocked(self, copy_executor, sample_wallet_transaction):
        """测试警告检测阻止"""
        copy_executor.warning_detector.detect.return_value = [
            RedFlag(flag_type=RedFlagType.SUSPICIOUS_TIMING, severity="high", description="可疑模式")
        ]
        copy_executor.warning_detector.should_block_trading.return_value = (True, "检测到可疑活动")
        
        result = await copy_executor.process_transaction(sample_wallet_transaction)
        
        assert result is None
    
    def test_calculate_copy_amount_fixed(self, mock_polymarket_client, mock_risk_manager):
        """测试固定金额模式"""
        config = CopyConfig(mode=CopyMode.FIXED, fixed_amount=Decimal("25"))
        
        executor = CopyExecutor(
            client=mock_polymarket_client,
            risk_manager=mock_risk_manager,
            quality_scorer=MagicMock(),
            market_maker_detector=MagicMock(),
            warning_detector=MagicMock(),
            copy_config=config
        )
        
        tx = WalletTransaction(
            wallet_address="0x1234",
            tx_hash="0x5678",
            market_id="market_1",
            market_question="Test?",
            side="YES",
            size=Decimal("1000"),  # 源交易金额大
            price=Decimal("0.5"),
            timestamp=datetime.now(timezone.utc),
            tx_type="buy"
        )
        
        score = QualityScore(
            wallet_address="0x1234",
            tier=WalletTier.EXPERT,
            overall_score=Decimal("8"),
            win_rate_score=Decimal("8"),
            profit_factor_score=Decimal("8"),
            consistency_score=Decimal("8"),
            risk_score=Decimal("8"),
            specialty_score=Decimal("8"),
            stats=MagicMock()
        )
        
        amount = executor._calculate_copy_amount(tx, score)
        
        assert amount == Decimal("25")  # 固定金额，忽略源交易大小
    
    def test_calculate_copy_amount_proportional(self, mock_polymarket_client, mock_risk_manager):
        """测试比例模式"""
        config = CopyConfig(
            mode=CopyMode.PROPORTIONAL,
            proportional_ratio=Decimal("0.1"),
            max_amount=Decimal("100")
        )
        
        executor = CopyExecutor(
            client=mock_polymarket_client,
            risk_manager=mock_risk_manager,
            quality_scorer=MagicMock(),
            market_maker_detector=MagicMock(),
            warning_detector=MagicMock(),
            copy_config=config
        )
        
        tx = WalletTransaction(
            wallet_address="0x1234",
            tx_hash="0x5678",
            market_id="market_1",
            market_question="Test?",
            side="YES",
            size=Decimal("200"),
            price=Decimal("0.5"),
            timestamp=datetime.now(timezone.utc),
            tx_type="buy"
        )
        
        score = QualityScore(
            wallet_address="0x1234",
            tier=WalletTier.EXPERT,
            overall_score=Decimal("8"),
            win_rate_score=Decimal("8"),
            profit_factor_score=Decimal("8"),
            consistency_score=Decimal("8"),
            risk_score=Decimal("8"),
            specialty_score=Decimal("8"),
            stats=MagicMock()
        )
        
        amount = executor._calculate_copy_amount(tx, score)
        
        assert amount == Decimal("20")  # 200 * 0.1
    
    def test_calculate_copy_amount_smart(self, copy_executor, sample_wallet_transaction):
        """测试智能模式"""
        # Expert级别，乘数1.5
        score = QualityScore(
            wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
            tier=WalletTier.EXPERT,
            overall_score=Decimal("8.0"),  # 置信度0.8
            win_rate_score=Decimal("8.0"),
            profit_factor_score=Decimal("8.0"),
            consistency_score=Decimal("8.0"),
            risk_score=Decimal("8.0"),
            specialty_score=Decimal("8.0"),
            stats=MagicMock()
        )
        
        amount = copy_executor._calculate_copy_amount(sample_wallet_transaction, score)
        
        # 基础金额10 * Expert乘数1.5 * 置信度0.8 = 12
        # 但会被限制在min和max之间
        assert copy_executor.config.min_amount <= amount <= copy_executor.config.max_amount
    
    def test_calculate_copy_amount_elite_tier(self, mock_polymarket_client, mock_risk_manager):
        """测试Elite级别乘数"""
        config = CopyConfig(
            mode=CopyMode.SMART,
            fixed_amount=Decimal("10"),
            max_amount=Decimal("100")
        )
        
        executor = CopyExecutor(
            client=mock_polymarket_client,
            risk_manager=mock_risk_manager,
            quality_scorer=MagicMock(),
            market_maker_detector=MagicMock(),
            warning_detector=MagicMock(),
            copy_config=config
        )
        
        tx = WalletTransaction(
            wallet_address="0x1234",
            tx_hash="0x5678",
            market_id="market_1",
            market_question="Test?",
            side="YES",
            size=Decimal("100"),
            price=Decimal("0.5"),
            timestamp=datetime.now(timezone.utc),
            tx_type="buy"
        )
        
        score = QualityScore(
            wallet_address="0x1234",
            tier=WalletTier.ELITE,
            overall_score=Decimal("9.5"),  # 高置信度
            win_rate_score=Decimal("9.5"),
            profit_factor_score=Decimal("9.5"),
            consistency_score=Decimal("9.5"),
            risk_score=Decimal("9.5"),
            specialty_score=Decimal("9.5"),
            stats=MagicMock()
        )
        
        amount = executor._calculate_copy_amount(tx, score)
        
        # Elite乘数2.0 * 基础10 * 置信度0.95 = 19
        assert amount > Decimal("15")
    
    def test_get_copy_stats(self, copy_executor):
        """测试获取统计"""
        # 添加一些交易记录
        copy_executor._copy_trades = [
            CopyTrade(
                source_wallet="0x1234",
                source_tx_hash="0x5678",
                market_id="m1",
                market_question="Test 1",
                side="YES",
                action=CopyAction.OPEN,
                original_size=Decimal("100"),
                copy_size=Decimal("10"),
                copy_price=Decimal("0.5"),
                status="filled"
            ),
            CopyTrade(
                source_wallet="0x1234",
                source_tx_hash="0x5679",
                market_id="m2",
                market_question="Test 2",
                side="NO",
                action=CopyAction.CLOSE,
                original_size=Decimal("100"),
                copy_size=Decimal("10"),
                copy_price=Decimal("0.5"),
                status="failed"
            ),
        ]
        
        stats = copy_executor.get_copy_stats()
        
        assert stats["total_trades"] == 2
        assert stats["success_trades"] == 1
        assert stats["failed_trades"] == 1
        assert stats["success_rate"] == 0.5
    
    def test_set_config(self, copy_executor):
        """测试更新配置"""
        new_config = CopyConfig(
            mode=CopyMode.FIXED,
            fixed_amount=Decimal("20"),
            enabled=False
        )
        
        copy_executor.set_config(new_config)
        
        assert copy_executor.config.mode == CopyMode.FIXED
        assert copy_executor.config.fixed_amount == Decimal("20")
        assert copy_executor.config.enabled is False


class TestCopyTrade:
    """跟单交易记录测试类"""
    
    def test_to_dict(self):
        """测试转换为字典"""
        trade = CopyTrade(
            source_wallet="0x1234567890",
            source_tx_hash="0xabcdef",
            market_id="market_1",
            market_question="Test question?",
            side="YES",
            action=CopyAction.OPEN,
            original_size=Decimal("100"),
            copy_size=Decimal("10"),
            copy_price=Decimal("0.65")
        )
        
        data = trade.to_dict()
        
        assert data["source_wallet"] == "0x1234567890"
        assert data["side"] == "YES"
        assert data["original_size"] == "100"
        assert data["copy_size"] == "10"
        assert data["copy_price"] == "0.65"
        assert data["status"] == "pending"
        assert "created_at" in data


class TestCopyConfig:
    """跟单配置测试类"""
    
    def test_default_config(self):
        """测试默认配置"""
        config = CopyConfig()
        
        assert config.mode == CopyMode.SMART
        assert config.enabled is True
        assert config.fixed_amount == Decimal("10")
        assert config.max_amount == Decimal("50")
    
    def test_custom_config(self):
        """测试自定义配置"""
        config = CopyConfig(
            mode=CopyMode.PROPORTIONAL,
            proportional_ratio=Decimal("0.2"),
            copy_delay_seconds=2.0
        )
        
        assert config.mode == CopyMode.PROPORTIONAL
        assert config.proportional_ratio == Decimal("0.2")
        assert config.copy_delay_seconds == 2.0
