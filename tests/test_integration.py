"""
集成测试
========
测试完整交易流程和组件间协作。
"""

import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from core.risk_manager import RiskManager
from core.circuit_breaker import CircuitBreaker
from core.copy_executor import CopyExecutor, CopyConfig, CopyMode
from services.polymarket_client import PolymarketClient, OrderResult
from strategies.adaptive import AdaptiveStrategyManager
from strategies.base import MarketData, SignalType


class TestTradingFlow:
    """完整交易流程测试"""
    
    @pytest.fixture
    def trading_system(self, mock_polymarket_client):
        """创建完整交易系统"""
        # 熔断器
        circuit_breaker = CircuitBreaker(
            max_daily_loss=Decimal("100"),
            max_consecutive_losses=5,
            max_single_loss=Decimal("50")
        )
        
        # 风险管理器
        risk_manager = RiskManager(
            circuit_breaker=circuit_breaker,
            max_position_size=Decimal("50"),
            max_concurrent_positions=5,
            max_total_exposure=Decimal("200")
        )
        risk_manager.update_balance(Decimal("1000"))
        
        # 策略管理器
        strategy_manager = AdaptiveStrategyManager(min_confidence=Decimal("0.7"))
        
        return {
            "circuit_breaker": circuit_breaker,
            "risk_manager": risk_manager,
            "strategy_manager": strategy_manager,
            "client": mock_polymarket_client
        }
    
    @pytest.mark.asyncio
    async def test_complete_buy_flow(self, trading_system):
        """测试完整买入流程"""
        rm = trading_system["risk_manager"]
        sm = trading_system["strategy_manager"]
        client = trading_system["client"]
        
        # 1. 创建市场数据
        market_data = MarketData(
            market_id="market_endgame_1",
            question="Will Fed announce rate decision by March 22, 2026?",
            yes_price=Decimal("0.99"),
            no_price=Decimal("0.01"),
            volume_24h=Decimal("250000"),
            liquidity=Decimal("150000"),
            days_to_resolution=1
        )
        
        # 2. 策略分析
        result = sm.analyze_market(market_data)
        
        assert result.should_trade is True
        assert result.signal in [SignalType.BUY_YES, SignalType.BUY_NO]
        
        # 3. 风险检查
        side = "YES" if result.signal == SignalType.BUY_YES else "NO"
        price = market_data.yes_price if side == "YES" else market_data.no_price
        
        risk_result = rm.check_trade(
            market_id=market_data.market_id,
            side=side,
            requested_size=Decimal("30"),
            confidence_score=result.confidence,
            price=price
        )
        
        assert risk_result.allowed is True
        
        # 4. 执行订单
        order = await client.place_order(
            market_id=market_data.market_id,
            side=side,
            size=risk_result.suggested_size,
            price=price
        )
        
        assert order.success is True
        
        # 5. 记录持仓
        position = await rm.open_position(
            market_id=market_data.market_id,
            market_question=market_data.question,
            side=side,
            price=order.filled_price,
            size=order.filled_size
        )
        
        assert position is not None
        assert rm.get_position(market_data.market_id) is not None
    
    @pytest.mark.asyncio
    async def test_position_exit_flow(self, trading_system):
        """测试平仓流程"""
        rm = trading_system["risk_manager"]
        client = trading_system["client"]
        
        # 1. 开仓
        await rm.open_position(
            market_id="market_1",
            market_question="Test?",
            side="YES",
            price=Decimal("0.50"),
            size=Decimal("100")
        )
        
        # 2. 模拟价格变动 (触发止盈)
        exits = rm.check_position_exits({
            "market_1": Decimal("0.70")  # YES方向盈利
        })
        
        assert len(exits) > 0
        
        # 3. 执行平仓
        for exit_info in exits:
            position = await rm.close_position(
                market_id=exit_info["market_id"],
                exit_price=exit_info["exit_price"]
            )
            
            assert position.pnl > 0
        
        # 4. 验证持仓已清除
        assert rm.get_position("market_1") is None
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_triggers(self, trading_system):
        """测试熔断器触发"""
        cb = trading_system["circuit_breaker"]
        rm = trading_system["risk_manager"]
        
        # 模拟连续亏损
        for i in range(6):
            cb.record_trade_result(pnl=Decimal("-20"), volume=Decimal("50"))
        
        # 检查熔断器状态
        can_trade, reason = cb.check_can_trade()
        
        assert can_trade is False
        assert "日累计损失" in reason or "连续亏损" in reason
        
        # 验证风险检查阻止交易
        risk_result = rm.check_trade(
            market_id="test",
            side="YES",
            requested_size=Decimal("10"),
            confidence_score=Decimal("0.8"),
            price=Decimal("0.5")
        )
        
        assert risk_result.allowed is False
        assert "熔断器" in risk_result.reason
    
    @pytest.mark.asyncio
    async def test_risk_limits_enforced(self, trading_system):
        """测试风险限制执行"""
        rm = trading_system["risk_manager"]
        
        # 测试最大仓位限制
        for i in range(5):
            await rm.open_position(
                market_id=f"market_{i}",
                market_question=f"Test {i}",
                side="YES",
                price=Decimal("0.5"),
                size=Decimal("20")
            )
        
        # 尝试开第6个仓位
        risk_result = rm.check_trade(
            market_id="market_6",
            side="YES",
            requested_size=Decimal("10"),
            confidence_score=Decimal("0.8"),
            price=Decimal("0.5")
        )
        
        assert risk_result.allowed is False
        assert "最大并发仓位" in risk_result.reason


class TestCopyTradingFlow:
    """跟单交易流程测试"""
    
    @pytest.mark.asyncio
    async def test_copy_trading_flow(
        self,
        mock_polymarket_client,
        mock_risk_manager,
        mock_telegram_service,
        sample_wallet_transaction
    ):
        """测试完整跟单流程"""
        from core.wallet_quality_scorer import QualityScore, WalletTier, TradingStats
        from core.market_maker_detector import MarketMakerScore, MarketMakerType
        
        # 设置mock返回值
        quality_scorer = MagicMock()
        quality_scorer.score_wallet.return_value = QualityScore(
            wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
            tier=WalletTier.EXPERT,
            overall_score=Decimal("8.5"),
            win_rate_score=Decimal("8.5"),
            profit_factor_score=Decimal("8.5"),
            consistency_score=Decimal("8.5"),
            risk_score=Decimal("8.5"),
            specialty_score=Decimal("8.5"),
            stats=TradingStats(
                total_trades=100,
                winning_trades=68,
                losing_trades=32,
                total_profit=Decimal("150"),
                total_loss=Decimal("71"),
                max_drawdown=Decimal("10")
            )
        )
        
        market_maker_detector = MagicMock()
        market_maker_detector.detect.return_value = MarketMakerScore(
            wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
            is_market_maker=False,
            maker_type=MarketMakerType.UNKNOWN,
            confidence=Decimal("0.1"),
            patterns=[],
            stats={},
            recommendation="neutral"
        )
        
        warning_detector = MagicMock()
        warning_detector.detect.return_value = []
        warning_detector.should_block_trading.return_value = (False, "")
        
        config = CopyConfig(
            mode=CopyMode.SMART,
            enabled=True,
            copy_delay_seconds=0.1
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
        
        # 执行跟单
        result = await executor.process_transaction(sample_wallet_transaction)
        
        # 验证结果
        assert result is not None
        assert result.status == "filled"
        
        # 验证通知发送
        mock_telegram_service.send_trade_notification.assert_called_once()


class TestErrorRecovery:
    """错误恢复测试"""
    
    @pytest.mark.asyncio
    async def test_api_error_recovery(self, mock_polymarket_client, mock_risk_manager):
        """测试API错误恢复"""
        from services.polymarket_client import OrderResult
        
        # 模拟第一次失败，第二次成功
        mock_polymarket_client.place_order.side_effect = [
            OrderResult(success=False, error="Network error"),
            OrderResult(success=True, order_id="order_123", filled_size=Decimal("10"), filled_price=Decimal("0.5"))
        ]
        
        # 验证重试逻辑
        result = await mock_polymarket_client.place_order(
            market_id="test",
            side="YES",
            size=Decimal("10"),
            price=Decimal("0.5")
        )
        
        assert result.success is False
        
        # 第二次调用
        result = await mock_polymarket_client.place_order(
            market_id="test",
            side="YES",
            size=Decimal("10"),
            price=Decimal("0.5")
        )
        
        assert result.success is True
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_recovery(self):
        """测试熔断器恢复"""
        from core.circuit_breaker import CircuitBreaker
        
        cb = CircuitBreaker(
            max_daily_loss=Decimal("50"),
            max_consecutive_losses=3
        )
        
        # 触发熔断
        for _ in range(4):
            cb.record_trade_result(pnl=Decimal("-20"), volume=Decimal("10"))
        
        can_trade, _ = cb.check_can_trade()
        assert can_trade is False
        
        # 模拟时间过去（重置）
        cb.reset()
        
        can_trade, _ = cb.check_can_trade()
        assert can_trade is True
