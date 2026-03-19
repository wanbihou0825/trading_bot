"""
日志系统模块
===========
提供统一的日志记录功能，支持敏感数据脱敏。
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
import os


# 敏感数据脱敏函数
def mask_sensitive_data(value: str, visible_chars: int = 4) -> str:
    """脱敏敏感数据，只显示首尾各visible_chars个字符"""
    if not value or len(value) <= visible_chars * 2:
        return "***"
    return f"{value[:visible_chars]}...{value[-visible_chars:]}"


def mask_wallet_address(address: str) -> str:
    """脱敏钱包地址，格式: 0x1234...5678"""
    if not address:
        return "N/A"
    if address.startswith("0x") and len(address) == 42:
        return f"{address[:6]}...{address[-4:]}"
    return mask_sensitive_data(address)


class SensitiveDataFilter(logging.Filter):
    """敏感数据过滤器，自动脱敏日志中的敏感信息"""
    
    SENSITIVE_PATTERNS = [
        "private_key",
        "PRIVATE_KEY",
        "secret",
        "SECRET",
        "api_key",
        "API_KEY",
        "token",
        "TOKEN",
        "password",
        "PASSWORD",
    ]
    
    def filter(self, record: logging.LogRecord) -> bool:
        # 检查消息中是否包含敏感关键词
        msg = str(record.getMessage())
        for pattern in self.SENSITIVE_PATTERNS:
            if pattern in msg:
                # 标记需要脱敏处理
                record.msg = self._mask_sensitive_in_message(msg)
                break
        return True
    
    def _mask_sensitive_in_message(self, message: str) -> str:
        """在消息中脱敏敏感值"""
        import re
        # 脱敏类似 key=value 的模式
        pattern = r'(private_key|PRIVATE_KEY|secret|SECRET|api_key|API_KEY|token|TOKEN|password|PASSWORD)\s*[=:]\s*[^\s,]+'
        return re.sub(pattern, r'\1=***MASKED***', message)


class ColoredFormatter(logging.Formatter):
    """彩色日志格式化器"""
    
    # ANSI颜色代码
    COLORS = {
        "DEBUG": "\033[36m",     # 青色
        "INFO": "\033[32m",      # 绿色
        "WARNING": "\033[33m",   # 黄色
        "ERROR": "\033[31m",     # 红色
        "CRITICAL": "\033[35m",  # 紫色
    }
    RESET = "\033[0m"
    
    def format(self, record: logging.LogRecord) -> str:
        # 添加颜色
        if sys.stdout.isatty():  # 只在终端中添加颜色
            color = self.COLORS.get(record.levelname, self.RESET)
            record.levelname = f"{color}{record.levelname}{self.RESET}"
        
        return super().format(record)


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    log_dir: str = "logs"
) -> None:
    """
    设置日志系统
    
    Args:
        level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: 日志文件名，如果为None则只输出到控制台
        log_dir: 日志文件目录
    """
    # 创建根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # 清除现有处理器
    root_logger.handlers.clear()
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_format = ColoredFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_format)
    console_handler.addFilter(SensitiveDataFilter())
    root_logger.addHandler(console_handler)
    
    # 文件处理器（如果指定了日志文件）
    if log_file:
        log_path = Path(log_dir) / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler.setFormatter(file_format)
        file_handler.addFilter(SensitiveDataFilter())
        root_logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的日志器
    
    Args:
        name: 日志器名称，通常使用 __name__
    
    Returns:
        配置好的Logger实例
    """
    return logging.getLogger(name)


# 便捷日志函数
def log_trade(logger: logging.Logger, action: str, details: dict) -> None:
    """记录交易日志"""
    # 脱敏钱包地址
    if "wallet" in details:
        details["wallet"] = mask_wallet_address(details["wallet"])
    
    logger.info(f"[TRADE] {action} | {details}")


def log_error(logger: logging.Logger, error: Exception, context: Optional[dict] = None) -> None:
    """记录错误日志"""
    context_str = f" | 上下文: {context}" if context else ""
    logger.error(f"[ERROR] {type(error).__name__}: {error}{context_str}")
