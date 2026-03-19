"""
结构化日志模块
==============
JSON格式日志、日志轮转、交易流水可追溯。
"""

import logging
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any, Dict
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pythonjsonlogger import jsonlogger

from utils.logger import mask_sensitive_data, mask_wallet_address


class StructuredJsonFormatter(jsonlogger.JsonFormatter):
    """
    结构化JSON日志格式化器
    
    输出格式:
    {
        "timestamp": "2026-03-20T10:30:00Z",
        "level": "INFO",
        "logger": "core.risk_manager",
        "message": "开仓成功",
        "market_id": "market_1",
        "side": "YES",
        ...
    }
    """
    
    def add_fields(self, log_record: dict, record: logging.LogRecord, message_dict: dict) -> None:
        """添加自定义字段"""
        super().add_fields(log_record, record, message_dict)
        
        # 时间戳（ISO格式）
        log_record['timestamp'] = datetime.now(timezone.utc).isoformat()
        
        # 日志级别
        log_record['level'] = record.levelname
        
        # 日志器名称
        log_record['logger'] = record.name
        
        # 添加额外字段
        if hasattr(record, 'trade_data'):
            log_record['trade'] = record.trade_data
        
        if hasattr(record, 'market_id'):
            log_record['market_id'] = record.market_id
        
        if hasattr(record, 'wallet_address'):
            log_record['wallet_address'] = mask_wallet_address(record.wallet_address)
        
        if hasattr(record, 'error_type'):
            log_record['error_type'] = record.error_type


class TradeLogger:
    """
    交易日志记录器
    
    专门用于记录交易流水，支持:
    1. 独立的交易日志文件
    2. JSON格式，便于分析
    3. 关键字段（tx_hash, pnl, market等）
    """
    
    def __init__(
        self,
        log_dir: str = "logs",
        log_file: str = "trades.log",
        max_bytes: int = 10 * 1024 * 1024,  # 10MB
        backup_count: int = 10,
    ):
        """
        初始化交易日志记录器
        
        Args:
            log_dir: 日志目录
            log_file: 日志文件名
            max_bytes: 单文件最大字节数
            backup_count: 保留备份数量
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.logger = logging.getLogger("trade_logger")
        self.logger.setLevel(logging.INFO)
        
        # 清除现有处理器
        self.logger.handlers.clear()
        
        # JSON格式文件处理器（轮转）
        log_path = self.log_dir / log_file
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setFormatter(StructuredJsonFormatter())
        self.logger.addHandler(file_handler)
        
        # 防止传播到根日志器
        self.logger.propagate = False
    
    def log_trade(
        self,
        action: str,
        market_id: str,
        market_question: str,
        side: str,
        size: Any,
        price: Any,
        order_id: Optional[str] = None,
        tx_hash: Optional[str] = None,
        pnl: Optional[float] = None,
        strategy: Optional[str] = None,
        confidence: Optional[float] = None,
        wallet_address: Optional[str] = None,
        **extra_fields
    ) -> None:
        """
        记录交易日志
        
        Args:
            action: 交易动作（开仓、平仓、跟单开仓等）
            market_id: 市场ID
            market_question: 市场问题
            side: 方向
            size: 数量
            price: 价格
            order_id: 订单ID
            tx_hash: 交易哈希
            pnl: 盈亏
            strategy: 策略类型
            confidence: 置信度
            wallet_address: 钱包地址（跟单时）
            **extra_fields: 额外字段
        """
        trade_data = {
            "action": action,
            "market_id": market_id,
            "market_question": market_question[:100] if market_question else None,
            "side": side,
            "size": str(size),
            "price": str(price),
            "order_id": order_id,
            "tx_hash": tx_hash,
            "pnl": pnl,
            "strategy": strategy,
            "confidence": confidence,
            "wallet_address": mask_wallet_address(wallet_address) if wallet_address else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **extra_fields
        }
        
        # 创建日志记录
        record = self.logger.makeRecord(
            name="trade_logger",
            level=logging.INFO,
            fn="",
            lno=0,
            msg=f"交易记录: {action}",
            args=(),
            exc_info=None
        )
        record.trade_data = trade_data
        
        self.logger.handle(record)
    
    def log_position_open(
        self,
        market_id: str,
        market_question: str,
        side: str,
        size: Any,
        entry_price: Any,
        stop_loss: Optional[Any] = None,
        take_profit: Optional[Any] = None,
        strategy: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> None:
        """记录开仓"""
        self.log_trade(
            action="开仓",
            market_id=market_id,
            market_question=market_question,
            side=side,
            size=size,
            price=entry_price,
            stop_loss=str(stop_loss) if stop_loss else None,
            take_profit=str(take_profit) if take_profit else None,
            strategy=strategy,
            confidence=confidence
        )
    
    def log_position_close(
        self,
        market_id: str,
        market_question: str,
        side: str,
        size: Any,
        exit_price: Any,
        entry_price: Any,
        pnl: float,
        pnl_pct: float,
        reason: str,
        tx_hash: Optional[str] = None,
    ) -> None:
        """记录平仓"""
        self.log_trade(
            action="平仓",
            market_id=market_id,
            market_question=market_question,
            side=side,
            size=size,
            price=exit_price,
            entry_price=str(entry_price),
            pnl=pnl,
            pnl_pct=pnl_pct,
            close_reason=reason,
            tx_hash=tx_hash
        )
    
    def log_copy_trade(
        self,
        source_wallet: str,
        source_tx_hash: str,
        market_id: str,
        market_question: str,
        side: str,
        original_size: Any,
        copy_size: Any,
        copy_price: Any,
        quality_score: float,
        order_id: Optional[str] = None,
    ) -> None:
        """记录跟单交易"""
        self.log_trade(
            action="跟单开仓",
            market_id=market_id,
            market_question=market_question,
            side=side,
            size=copy_size,
            price=copy_price,
            order_id=order_id,
            wallet_address=source_wallet,
            source_tx_hash=source_tx_hash,
            original_size=str(original_size),
            quality_score=quality_score
        )


class AuditLogger:
    """
    审计日志记录器
    
    记录关键操作和状态变更，用于问题追溯
    """
    
    def __init__(
        self,
        log_dir: str = "logs",
        log_file: str = "audit.log",
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 20,
    ):
        """
        初始化审计日志记录器
        
        Args:
            log_dir: 日志目录
            log_file: 日志文件名
            max_bytes: 单文件最大字节数
            backup_count: 保留备份数量
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.logger = logging.getLogger("audit_logger")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        
        log_path = self.log_dir / log_file
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setFormatter(StructuredJsonFormatter())
        self.logger.addHandler(file_handler)
        
        self.logger.propagate = False
    
    def log_event(
        self,
        event_type: str,
        description: str,
        severity: str = "INFO",
        **details
    ) -> None:
        """
        记录审计事件
        
        Args:
            event_type: 事件类型
            description: 描述
            severity: 严重程度
            **details: 详细信息
        """
        event_data = {
            "event_type": event_type,
            "description": description,
            "severity": severity,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **details
        }
        
        record = self.logger.makeRecord(
            name="audit_logger",
            level=getattr(logging, severity, logging.INFO),
            fn="",
            lno=0,
            msg=description,
            args=(),
            exc_info=None
        )
        
        for key, value in event_data.items():
            setattr(record, key, value)
        
        self.logger.handle(record)
    
    def log_config_change(self, config_name: str, old_value: Any, new_value: Any) -> None:
        """记录配置变更"""
        self.log_event(
            event_type="CONFIG_CHANGE",
            description=f"配置变更: {config_name}",
            config_name=config_name,
            old_value=str(old_value),
            new_value=str(new_value)
        )
    
    def log_circuit_breaker_trigger(self, reason: str) -> None:
        """记录熔断器触发"""
        self.log_event(
            event_type="CIRCUIT_BREAKER_TRIGGER",
            description=f"熔断器触发: {reason}",
            severity="WARNING",
            reason=reason
        )
    
    def log_emergency_stop(self, reason: str) -> None:
        """记录紧急停止"""
        self.log_event(
            event_type="EMERGENCY_STOP",
            description=f"紧急停止: {reason}",
            severity="CRITICAL",
            reason=reason
        )
    
    def log_balance_anomaly(self, expected: float, actual: float, threshold: float) -> None:
        """记录余额异常"""
        self.log_event(
            event_type="BALANCE_ANOMALY",
            description="余额异常检测",
            severity="WARNING",
            expected_balance=expected,
            actual_balance=actual,
            threshold=threshold,
            deviation=abs(expected - actual)
        )


def setup_structured_logging(
    log_dir: str = "logs",
    log_level: str = "INFO",
    enable_json_logs: bool = True,
    enable_trade_logs: bool = True,
    enable_audit_logs: bool = True,
) -> tuple:
    """
    设置结构化日志系统
    
    Args:
        log_dir: 日志目录
        log_level: 日志级别
        enable_json_logs: 启用JSON格式主日志
        enable_trade_logs: 启用交易日志
        enable_audit_logs: 启用审计日志
    
    Returns:
        (trade_logger, audit_logger) 或 (None, None)
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # 配置根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root_logger.handlers.clear()
    
    # 控制台处理器（彩色格式）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_format = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_format)
    root_logger.addHandler(console_handler)
    
    # 文件处理器（JSON格式或普通格式）
    main_log_file = log_path / "bot.log"
    
    if enable_json_logs:
        file_handler = RotatingFileHandler(
            main_log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=10,
            encoding='utf-8'
        )
        file_handler.setFormatter(StructuredJsonFormatter())
    else:
        file_handler = RotatingFileHandler(
            main_log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
            encoding='utf-8'
        )
        file_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
    
    root_logger.addHandler(file_handler)
    
    # 创建交易和审计日志记录器
    trade_logger = TradeLogger(log_dir=log_dir) if enable_trade_logs else None
    audit_logger = AuditLogger(log_dir=log_dir) if enable_audit_logs else None
    
    return trade_logger, audit_logger
