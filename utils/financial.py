"""
金融计算工具模块
================
提供精确的金融计算功能，使用Decimal确保精度。
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from datetime import datetime, timezone


# 计算精度设置
DECIMAL_PRECISION = 28  # 28位有效数字
ROUNDING_MODE = ROUND_HALF_UP  # 银行家舍入


def to_decimal(value) -> Decimal:
    """
    将值转换为Decimal类型
    
    Args:
        value: 要转换的值 (int, float, str, Decimal)
    
    Returns:
        Decimal类型的值
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # 浮点数先转字符串避免精度损失
        return Decimal(str(value))
    return Decimal(value)


def calculate_position_size(
    account_balance: Decimal,
    risk_percentage: Decimal,
    confidence_score: Decimal,
    max_position_size: Decimal,
    max_position_pct: Decimal
) -> Decimal:
    """
    计算建议仓位大小
    
    公式: position = balance * risk_pct * confidence
    限制: min(max_position_size, balance * max_position_pct)
    
    Args:
        account_balance: 账户余额 (USD)
        risk_percentage: 风险比例 (如 0.02 表示2%)
        confidence_score: 信号置信度 (0-1)
        max_position_size: 最大仓位限制 (USD)
        max_position_pct: 最大仓位占余额比例
    
    Returns:
        建议仓位大小 (USD)
    """
    balance = to_decimal(account_balance)
    risk_pct = to_decimal(risk_percentage)
    confidence = to_decimal(confidence_score)
    max_size = to_decimal(max_position_size)
    max_pct = to_decimal(max_position_pct)
    
    # 基础仓位计算
    base_position = balance * risk_pct * confidence
    
    # 应用最大比例限制
    max_by_pct = balance * max_pct
    
    # 取最小值作为最终仓位
    position = min(base_position, max_by_pct, max_size)
    
    # 确保非负
    return max(position, Decimal("0")).quantize(
        Decimal("0.01"),
        rounding=ROUNDING_MODE
    )


def calculate_pnl(
    entry_price: Decimal,
    exit_price: Decimal,
    position_size: Decimal,
    side: str  # "BUY" or "SELL"
) -> Decimal:
    """
    计算盈亏
    
    Args:
        entry_price: 入场价格
        exit_price: 出场价格
        position_size: 仓位大小
        side: 交易方向 ("BUY" 或 "SELL")
    
    Returns:
        盈亏金额 (正数为盈利，负数为亏损)
    """
    entry = to_decimal(entry_price)
    exit_p = to_decimal(exit_price)
    size = to_decimal(position_size)
    
    if side.upper() == "BUY":
        # 买入：价格上涨盈利
        pnl = (exit_p - entry) * size / entry
    else:
        # 卖出：价格下跌盈利
        pnl = (entry - exit_p) * size / entry
    
    return pnl.quantize(Decimal("0.01"), rounding=ROUNDING_MODE)


def calculate_annualized_return(
    current_price: Decimal,
    target_price: Decimal,
    days_to_resolution: int
) -> Decimal:
    """
    计算年化收益率
    
    公式: ((target_price / current_price) ^ (365 / days)) - 1
    
    Args:
        current_price: 当前价格
        target_price: 目标价格 (通常为1.0表示完全胜出)
        days_to_resolution: 距离结算的天数
    
    Returns:
        年化收益率 (如 0.20 表示20%)
    """
    if days_to_resolution <= 0:
        return Decimal("0")
    
    current = to_decimal(current_price)
    target = to_decimal(target_price)
    
    # 计算收益率
    if current <= 0:
        return Decimal("0")
    
    # 使用幂运算计算年化收益
    try:
        ratio = target / current
        annualized = ratio ** (Decimal("365") / Decimal(days_to_resolution)) - 1
        return annualized.quantize(Decimal("0.0001"), rounding=ROUNDING_MODE)
    except Exception:
        return Decimal("0")


def calculate_slippage(
    expected_price: Decimal,
    actual_price: Decimal
) -> Decimal:
    """
    计算滑点
    
    Args:
        expected_price: 预期价格
        actual_price: 实际价格
    
    Returns:
        滑点比例 (如 0.01 表示1%滑点)
    """
    expected = to_decimal(expected_price)
    actual = to_decimal(actual_price)
    
    if expected <= 0:
        return Decimal("0")
    
    slippage = abs(actual - expected) / expected
    return slippage.quantize(Decimal("0.0001"), rounding=ROUNDING_MODE)


def calculate_stop_loss_price(
    entry_price: Decimal,
    stop_loss_pct: Decimal,
    side: str
) -> Decimal:
    """
    计算止损价格
    
    Args:
        entry_price: 入场价格
        stop_loss_pct: 止损比例 (如 0.15 表示15%)
        side: 交易方向
    
    Returns:
        止损价格
    """
    entry = to_decimal(entry_price)
    pct = to_decimal(stop_loss_pct)
    
    if side.upper() == "BUY":
        # 买入止损：价格下跌
        stop_price = entry * (Decimal("1") - pct)
    else:
        # 卖出止损：价格上涨
        stop_price = entry * (Decimal("1") + pct)
    
    return stop_price.quantize(Decimal("0.0001"), rounding=ROUNDING_MODE)


def calculate_take_profit_price(
    entry_price: Decimal,
    take_profit_pct: Decimal,
    side: str
) -> Decimal:
    """
    计算止盈价格
    
    Args:
        entry_price: 入场价格
        take_profit_pct: 止盈比例 (如 0.25 表示25%)
        side: 交易方向
    
    Returns:
        止盈价格
    """
    entry = to_decimal(entry_price)
    pct = to_decimal(take_profit_pct)
    
    if side.upper() == "BUY":
        # 买入止盈：价格上涨
        profit_price = entry * (Decimal("1") + pct)
    else:
        # 卖出止盈：价格下跌
        profit_price = entry * (Decimal("1") - pct)
    
    return profit_price.quantize(Decimal("0.0001"), rounding=ROUNDING_MODE)


def is_price_in_range(
    price: Decimal,
    lower_bound: Decimal,
    upper_bound: Decimal
) -> bool:
    """
    检查价格是否在范围内
    
    Args:
        price: 要检查的价格
        lower_bound: 下限
        upper_bound: 上限
    
    Returns:
        是否在范围内
    """
    p = to_decimal(price)
    lower = to_decimal(lower_bound)
    upper = to_decimal(upper_bound)
    
    return lower <= p <= upper
