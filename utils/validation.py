"""
输入验证模块
============
提供各种输入数据的验证功能。
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple


def validate_wallet_address(address: str) -> Tuple[bool, Optional[str]]:
    """
    验证钱包地址格式
    
    Args:
        address: 钱包地址字符串
    
    Returns:
        (是否有效, 错误信息)
    """
    if not address:
        return False, "钱包地址不能为空"
    
    # 转换为小写进行验证
    addr = address.lower()
    
    # 检查格式: 0x开头，后跟40位十六进制字符
    if not addr.startswith("0x"):
        return False, "钱包地址必须以0x开头"
    
    if len(addr) != 42:
        return False, f"钱包地址长度无效 (期望42位，实际{len(addr)}位)"
    
    # 检查是否为有效十六进制
    hex_part = addr[2:]
    if not all(c in "0123456789abcdef" for c in hex_part):
        return False, "钱包地址包含无效字符"
    
    return True, None


def validate_private_key(key: str) -> Tuple[bool, Optional[str]]:
    """
    验证私钥格式
    
    Args:
        key: 私钥字符串
    
    Returns:
        (是否有效, 错误信息)
    """
    if not key:
        return False, "私钥不能为空"
    
    # 转换为小写
    k = key.lower()
    
    # 移除0x前缀（如果有）
    if k.startswith("0x"):
        k = k[2:]
    
    # 检查长度: 64位十六进制
    if len(k) != 64:
        return False, f"私钥长度无效 (期望64位十六进制，实际{len(k)}位)"
    
    # 检查是否为有效十六进制
    if not all(c in "0123456789abcdef" for c in k):
        return False, "私钥包含无效字符"
    
    return True, None


def validate_amount(
    amount,
    min_value: Decimal = Decimal("0.01"),
    max_value: Decimal = Decimal("1000000")
) -> Tuple[bool, Optional[str]]:
    """
    验证金额
    
    Args:
        amount: 金额值
        min_value: 最小值
        max_value: 最大值
    
    Returns:
        (是否有效, 错误信息)
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return False, "金额格式无效"
    
    if value < min_value:
        return False, f"金额不能小于 {min_value}"
    
    if value > max_value:
        return False, f"金额不能超过 {max_value}"
    
    return True, None


def validate_price(
    price,
    min_price: Decimal = Decimal("0.01"),
    max_price: Decimal = Decimal("0.99")
) -> Tuple[bool, Optional[str]]:
    """
    验证预测市场价格（0.01-0.99范围）
    
    Args:
        price: 价格值
        min_price: 最小价格
        max_price: 最大价格
    
    Returns:
        (是否有效, 错误信息)
    """
    try:
        value = Decimal(str(price))
    except (InvalidOperation, ValueError):
        return False, "价格格式无效"
    
    if value < min_price or value > max_price:
        return False, f"价格必须在 {min_price} 到 {max_price} 之间"
    
    return True, None


def validate_probability(probability) -> Tuple[bool, Optional[str]]:
    """
    验证概率值（0-1范围）
    
    Args:
        probability: 概率值
    
    Returns:
        (是否有效, 错误信息)
    """
    try:
        value = Decimal(str(probability))
    except (InvalidOperation, ValueError):
        return False, "概率格式无效"
    
    if value < Decimal("0") or value > Decimal("1"):
        return False, "概率必须在 0 到 1 之间"
    
    return True, None


def validate_market_id(market_id: str) -> Tuple[bool, Optional[str]]:
    """
    验证市场ID格式
    
    Args:
        market_id: 市场ID
    
    Returns:
        (是否有效, 错误信息)
    """
    if not market_id:
        return False, "市场ID不能为空"
    
    # Polymarket市场ID通常是数字或特定格式的字符串
    # 这里只做基本验证
    if len(market_id) > 100:
        return False, "市场ID过长"
    
    return True, None


def sanitize_input(value: str, max_length: int = 1000) -> str:
    """
    清理输入字符串，移除危险字符
    
    Args:
        value: 输入字符串
        max_length: 最大允许长度
    
    Returns:
        清理后的字符串
    """
    if not value:
        return ""
    
    # 截断过长输入
    result = value[:max_length]
    
    # 移除控制字符（保留换行和制表符）
    result = "".join(c for c in result if c.isprintable() or c in "\n\t")
    
    return result.strip()
