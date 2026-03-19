"""
风险管理器单元测试
==================
测试核心风险控制逻辑。
"""

import pytest
from decimal import Decimal
from datetime import datetime, timezone

from core.risk_manager import RiskManager, Position, RiskCheckResult
from core.circuit_breaker import CircuitBreaker


class TestRiskManager:
    """风险管理器测试类"""
    
    def test_initialization(self, mock_circuit_breaker):
        """测试初始化"""
        rm = RiskManager(
            circuit_breaker=mock_circuit_breaker,
            max_position_size=Decimal("50"),
            max_concurrent_positions=5
        )
        
        assert rm.max_position_size == Decimal("50")
        assert rm.max_concurrent_positions == 5
        assert len(rm.get_positions()) == 0
    
    def test_update_balance(self, mock_risk_manager):
        """测试余额更新"""
        mock_risk_manager.update_balance(Decimal("2000"))
        status = mock_risk_manager.get_status()
        assert status["account_balance"] == 2000.0
    
    def test_check_trade_allowed(self, mock_risk_manager):
        """测试交易检查 - 允许"""
        result = mock_risk_manager.check_trade(
            market_id="market_1",
            side="YES",
            requested_size=Decimal("30"),
            confidence_score=Decimal("0.8"),
            price=Decimal("0.65")
        )
        
        assert result.allowed is True
        assert "风险检查通过" in result.reason
        assert result.suggested_size is not None
    
    def test_check_trade_max_positions(self, mock_risk_manager):
        """测试交易检查 - 超过最大仓位数"""
        # 开满仓位
        for i in range(5):
            mock_risk_manager.open_position(
                market_id=f"market_{i}",
                market_question=f"Test {i}",
                side="YES",
                price=Decimal("0.5"),
                size=Decimal("10")
            )
        
        # 尝试再开仓
        result = mock_risk_manager.check_trade(
            market_id="market_6",
            side="YES",
            requested_size=Decimal("10"),
            confidence_score=Decimal("0.8"),
            price=Decimal("0.5")
        )
        
        assert result.allowed is False
        assert "最大并发仓位" in result.reason
    
    def test_check_trade_total_exposure(self, mock_risk_manager):
        """测试交易检查 - 超过总敞口限制"""
        # 开一个大仓位
        mock_risk_manager.open_position(
            market_id="market_1",
            market_question="Test",
            side="YES",
            price=Decimal("0.5"),
            size=Decimal("180")  # 180 * 0.5 = 90 敞口
        )
        
        # 尝试再开大仓位
        result = mock_risk_manager.check_trade(
            market_id="market_2",
            side="YES",
            requested_size=Decimal("150"),
            confidence_score=Decimal("0.8"),
            price=Decimal("0.5")
        )
        
        assert result.allowed is False
        assert "总敞口超限" in result.reason
    
    def test_check_trade_circuit_breaker(self, mock_risk_manager, mock_circuit_breaker):
        """测试交易检查 - 熔断器触发"""
        # 触发熔断器
        mock_circuit_breaker.trigger("测试触发")
        
        result = mock_risk_manager.check_trade(
            market_id="market_1",
            side="YES",
            requested_size=Decimal("10"),
            confidence_score=Decimal("0.8"),
            price=Decimal("0.5")
        )
        
        assert result.allowed is False
        assert "熔断器激活" in result.reason
    
    def test_open_position(self, mock_risk_manager):
        """测试开仓"""
        position = mock_risk_manager.open_position(
            market_id="market_1",
            market_question="Test question?",
            side="YES",
            price=Decimal("0.65"),
            size=Decimal("20")
        )
        
        assert position.market_id == "market_1"
        assert position.side == "YES"
        assert position.entry_price == Decimal("0.65")
        assert position.size == Decimal("20")
        assert position.stop_loss_price is not None
        assert position.take_profit_price is not None
        
        # 验证持仓已记录
        positions = mock_risk_manager.get_positions()
        assert "market_1" in positions
    
    def test_close_position(self, mock_risk_manager):
        """测试平仓"""
        # 先开仓
        mock_risk_manager.open_position(
            market_id="market_1",
            market_question="Test question?",
            side="YES",
            price=Decimal("0.50"),
            size=Decimal("100")
        )
        
        # 平仓 (价格上涨到0.70)
        position = mock_risk_manager.close_position(
            market_id="market_1",
            exit_price=Decimal("0.70")
        )
        
        assert position is not None
        assert position.pnl > 0  # YES方向盈利
        assert "market_1" not in mock_risk_manager.get_positions()
    
    def test_position_pnl_calculation(self):
        """测试持仓盈亏计算"""
        # YES方向盈利
        position_yes = Position(
            market_id="test",
            market_question="Test",
            side="YES",
            entry_price=Decimal("0.50"),
            current_price=Decimal("0.70"),
            size=Decimal("100")
        )
        assert position_yes.pnl == Decimal("20")  # (0.70 - 0.50) * 100
        
        # NO方向盈利
        position_no = Position(
            market_id="test",
            market_question="Test",
            side="NO",
            entry_price=Decimal("0.50"),
            current_price=Decimal("0.30"),
            size=Decimal("100")
        )
        assert position_no.pnl == Decimal("20")  # (0.50 - 0.30) * 100
    
    def test_check_position_exits_stop_loss(self, mock_risk_manager):
        """测试止损触发"""
        # 开YES仓位，入场0.60
        mock_risk_manager.open_position(
            market_id="market_1",
            market_question="Test?",
            side="YES",
            price=Decimal("0.60"),
            size=Decimal("10"),
            stop_loss_pct=Decimal("0.10")
        )
        
        # 价格下跌到0.50（触发止损）
        exits = mock_risk_manager.check_position_exits({
            "market_1": Decimal("0.50")
        })
        
        assert len(exits) == 1
        assert exits[0]["reason"] == "止损触发"
    
    def test_check_position_exits_take_profit(self, mock_risk_manager):
        """测试止盈触发"""
        # 开YES仓位，入场0.50
        mock_risk_manager.open_position(
            market_id="market_1",
            market_question="Test?",
            side="YES",
            price=Decimal("0.50"),
            size=Decimal("10"),
            take_profit_pct=Decimal("0.30")
        )
        
        # 价格上涨到0.70（触发止盈）
        exits = mock_risk_manager.check_position_exits({
            "market_1": Decimal("0.70")
        })
        
        assert len(exits) == 1
        assert exits[0]["reason"] == "止盈触发"
    
    def test_get_total_exposure(self, mock_risk_manager):
        """测试总敞口计算"""
        # 开两个仓位
        mock_risk_manager.open_position(
            market_id="m1",
            market_question="Test 1",
            side="YES",
            price=Decimal("0.50"),
            size=Decimal("100")  # 敞口 50
        )
        mock_risk_manager.open_position(
            market_id="m2",
            market_question="Test 2",
            side="NO",
            price=Decimal("0.50"),
            size=Decimal("100")  # 敞口 50
        )
        
        exposure = mock_risk_manager.get_total_exposure()
        assert exposure == Decimal("100")  # 50 + 50
    
    def test_duplicate_position_blocked(self, mock_risk_manager):
        """测试禁止重复开仓同一市场"""
        # 开仓
        mock_risk_manager.open_position(
            market_id="market_1",
            market_question="Test?",
            side="YES",
            price=Decimal("0.5"),
            size=Decimal("10")
        )
        
        # 尝试再次开仓同一市场
        result = mock_risk_manager.check_trade(
            market_id="market_1",
            side="YES",
            requested_size=Decimal("10"),
            confidence_score=Decimal("0.8"),
            price=Decimal("0.5")
        )
        
        assert result.allowed is False
        assert "已有持仓" in result.reason


class TestPosition:
    """持仓测试类"""
    
    def test_pnl_percentage(self):
        """测试盈亏百分比计算"""
        position = Position(
            market_id="test",
            market_question="Test",
            side="YES",
            entry_price=Decimal("0.50"),
            current_price=Decimal("0.60"),
            size=Decimal("100")
        )
        
        # 盈利 10，成本 50，盈亏比 20%
        assert position.pnl == Decimal("10")
        assert position.pnl_percentage == Decimal("20")
    
    def test_pnl_percentage_zero_entry(self):
        """测试入场价为0时的盈亏百分比"""
        position = Position(
            market_id="test",
            market_question="Test",
            side="YES",
            entry_price=Decimal("0"),
            current_price=Decimal("0.60"),
            size=Decimal("100")
        )
        
        assert position.pnl_percentage == Decimal("0")
