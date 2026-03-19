"""
异常处理模块
============
定义项目中使用的所有自定义异常类。
"""

from typing import Optional


class BotException(Exception):
    """基础异常类，所有项目异常都继承此类"""
    
    def __init__(self, message: str, details: Optional[dict] = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)
    
    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | 详情: {self.details}"
        return self.message


class ConfigurationError(BotException):
    """配置错误"""
    pass


class InitializationError(BotException):
    """初始化错误"""
    pass


class TradeError(BotException):
    """交易执行错误"""
    pass


class RiskLimitError(BotException):
    """风险限制触发错误"""
    pass


class CircuitBreakerError(BotException):
    """熔断器触发错误
    
    当达到风险限制时抛出，表示交易应该暂停。
    """
    pass


class GracefulShutdown(Exception):
    """优雅关闭信号
    
    用于通知程序进行优雅关闭，非错误情况。
    """
    
    def __init__(self, reason: str = "用户请求关闭"):
        self.reason = reason
        super().__init__(reason)
