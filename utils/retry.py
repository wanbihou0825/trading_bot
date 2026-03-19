"""
生产级重试机制模块
==================
使用tenacity库实现全面的超时、重试、熔断机制。
"""

import asyncio
import functools
from typing import Callable, TypeVar, ParamSpec, Optional, Any
from decimal import Decimal

from tenacity import (
    retry,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
    wait_random,
    retry_if_exception_type,
    retry_if_result,
    before_sleep_log,
    after_log,
    RetryError,
    TryAgain
)
import aiohttp
from web3.exceptions import TransactionNotFound, TimeExhausted

from utils.logger import get_logger

logger = get_logger(__name__)

P = ParamSpec('P')
T = TypeVar('T')


# ─── 自定义异常 ───

class RetryableError(Exception):
    """可重试错误基类"""
    pass


class NonRetryableError(Exception):
    """不可重试错误"""
    pass


class APIError(RetryableError):
    """API错误（可重试）"""
    pass


class NetworkError(RetryableError):
    """网络错误（可重试）"""
    pass


class BlockchainError(RetryableError):
    """区块链错误（可重试）"""
    pass


class InsufficientBalanceError(NonRetryableError):
    """余额不足（不可重试）"""
    pass


class InvalidParameterError(NonRetryableError):
    """参数无效（不可重试）"""
    pass


# ─── 重试策略工厂 ───

def create_retry_strategy(
    max_attempts: int = 3,
    max_delay_seconds: float = 60.0,
    min_wait: float = 1.0,
    max_wait: float = 10.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple = (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        ConnectionError,
        APIError,
        NetworkError,
        BlockchainError,
        TransactionNotFound,
        TimeExhausted,
    ),
    on_retry_callback: Optional[Callable] = None
):
    """
    创建重试策略装饰器
    
    Args:
        max_attempts: 最大重试次数
        max_delay_seconds: 最大总延迟时间（秒）
        min_wait: 最小等待时间（秒）
        max_wait: 最大等待时间（秒）
        exponential_base: 指数退避基数
        retryable_exceptions: 可重试的异常类型元组
        on_retry_callback: 重试时的回调函数
    
    Returns:
        装饰器
    """
    
    def should_retry(retry_state):
        """判断是否应该重试"""
        exception = retry_state.outcome.exception()
        if exception is None:
            return False
        return isinstance(exception, retryable_exceptions)
    
    def before_sleep(retry_state):
        """重试前的回调"""
        exception = retry_state.outcome.exception()
        attempt = retry_state.attempt_number
        
        logger.warning(
            f"重试 {attempt}/{max_attempts} | "
            f"异常: {type(exception).__name__}: {exception} | "
            f"等待: {retry_state.next_action.sleep:.1f}s"
        )
        
        if on_retry_callback:
            try:
                on_retry_callback(attempt, exception)
            except Exception as e:
                logger.error(f"重试回调异常: {e}")
    
    return retry(
        retry=retry_if_exception_type(retryable_exceptions),
        stop=(
            stop_after_attempt(max_attempts) |
            stop_after_delay(max_delay_seconds)
        ),
        wait=wait_exponential(
            multiplier=min_wait,
            min=min_wait,
            max=max_wait,
            exponential_base=exponential_base
        ) + wait_random(0, 1),  # 添加随机抖动避免惊群效应
        before_sleep=before_sleep,
        reraise=True,
    )


# ─── 预定义重试装饰器 ───

def with_api_retry(
    max_attempts: int = 3,
    timeout_seconds: float = 30.0
):
    """
    API调用重试装饰器
    
    用于外部API调用（如Polymarket、Polygonscan等）
    """
    return create_retry_strategy(
        max_attempts=max_attempts,
        max_delay_seconds=timeout_seconds,
        min_wait=1.0,
        max_wait=15.0,
    )


def with_blockchain_retry(
    max_attempts: int = 5,
    timeout_seconds: float = 120.0
):
    """
    区块链操作重试装饰器
    
    用于链上交易和查询，给予更长的重试时间
    """
    return create_retry_strategy(
        max_attempts=max_attempts,
        max_delay_seconds=timeout_seconds,
        min_wait=2.0,
        max_wait=30.0,
        exponential_base=2.0,
    )


def with_order_retry(
    max_attempts: int = 2,
    timeout_seconds: float = 15.0
):
    """
    订单操作重试装饰器
    
    订单操作重试次数较少，避免重复下单
    """
    return create_retry_strategy(
        max_attempts=max_attempts,
        max_delay_seconds=timeout_seconds,
        min_wait=0.5,
        max_wait=5.0,
    )


def with_websocket_retry(
    max_attempts: int = 10,
    timeout_seconds: float = 300.0
):
    """
    WebSocket重试装饰器
    
    给予WebSocket更长的重试时间和更多次数
    """
    return create_retry_strategy(
        max_attempts=max_attempts,
        max_delay_seconds=timeout_seconds,
        min_wait=1.0,
        max_wait=30.0,
    )


# ─── 超时控制 ───

def with_timeout(seconds: float):
    """
    超时装饰器
    
    Args:
        seconds: 超时秒数
    """
    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=seconds
                )
            except asyncio.TimeoutError:
                logger.error(f"操作超时: {func.__name__} (超时: {seconds}s)")
                raise
        
        return wrapper
    return decorator


# ─── 全局超时配置 ───

class TimeoutConfig:
    """超时配置"""
    
    # API超时
    API_CONNECT_TIMEOUT: float = 10.0
    API_READ_TIMEOUT: float = 30.0
    API_TOTAL_TIMEOUT: float = 60.0
    
    # 区块链超时
    BLOCKCHAIN_QUERY_TIMEOUT: float = 30.0
    BLOCKCHAIN_TX_TIMEOUT: float = 120.0
    
    # WebSocket超时
    WS_CONNECT_TIMEOUT: float = 15.0
    WS_MESSAGE_TIMEOUT: float = 60.0
    WS_HEARTBEAT_TIMEOUT: float = 30.0
    
    # 订单超时
    ORDER_PLACE_TIMEOUT: float = 15.0
    ORDER_CANCEL_TIMEOUT: float = 10.0
    
    # 监控超时
    WALLET_SCAN_TIMEOUT: float = 60.0
    MARKET_SCAN_TIMEOUT: float = 30.0


# ─── 熔断增强 ───

class EnhancedCircuitBreaker:
    """
    增强版熔断器
    
    集成错误率统计和自动恢复
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        error_rate_threshold: float = 0.5,  # 50%错误率触发
        window_size: int = 20,  # 统计窗口大小
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.error_rate_threshold = error_rate_threshold
        self.window_size = window_size
        
        self._is_open = False
        self._last_failure_time: Optional[float] = None
        self._failure_count = 0
        self._success_count = 0
        self._result_window: list = []  # 最近N次结果
        
    def record_success(self):
        """记录成功"""
        self._success_count += 1
        self._result_window.append(True)
        if len(self._result_window) > self.window_size:
            self._result_window.pop(0)
        
        # 检查是否可以关闭熔断器
        if self._is_open:
            self._try_close()
    
    def record_failure(self):
        """记录失败"""
        self._failure_count += 1
        self._result_window.append(False)
        if len(self._result_window) > self.window_size:
            self._result_window.pop(0)
        
        self._last_failure_time = asyncio.get_event_loop().time()
        
        # 检查是否需要打开熔断器
        self._check_should_open()
    
    def _check_should_open(self):
        """检查是否应该打开熔断器"""
        # 基于失败次数
        if self._failure_count >= self.failure_threshold:
            self._is_open = True
            logger.warning(f"熔断器打开: 失败次数 {self._failure_count} >= {self.failure_threshold}")
            return
        
        # 基于错误率
        if len(self._result_window) >= self.window_size:
            error_rate = 1 - (sum(self._result_window) / len(self._result_window))
            if error_rate >= self.error_rate_threshold:
                self._is_open = True
                logger.warning(f"熔断器打开: 错误率 {error_rate:.1%} >= {self.error_rate_threshold:.1%}")
    
    def _try_close(self):
        """尝试关闭熔断器"""
        if not self._is_open or self._last_failure_time is None:
            return
        
        elapsed = asyncio.get_event_loop().time() - self._last_failure_time
        if elapsed >= self.recovery_timeout:
            self._is_open = False
            self._failure_count = 0
            logger.info(f"熔断器关闭: 恢复超时 {self.recovery_timeout}s 已过")
    
    def is_open(self) -> bool:
        """检查熔断器是否打开"""
        if self._is_open:
            self._try_close()
        return self._is_open
    
    def reset(self):
        """重置熔断器"""
        self._is_open = False
        self._failure_count = 0
        self._success_count = 0
        self._result_window = []
        self._last_failure_time = None
        logger.info("熔断器已重置")


# ─── 便捷函数 ───

def get_http_timeout() -> aiohttp.ClientTimeout:
    """获取HTTP客户端超时配置"""
    return aiohttp.ClientTimeout(
        total=TimeoutConfig.API_TOTAL_TIMEOUT,
        connect=TimeoutConfig.API_CONNECT_TIMEOUT,
        sock_read=TimeoutConfig.API_READ_TIMEOUT,
    )
