"""
WebSocket 管理器
================
管理WebSocket连接，支持自动重连、心跳、消息路由。
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, Any, List
from datetime import datetime, timezone
from enum import Enum
import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)


class ConnectionState(Enum):
    """连接状态"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSING = "closing"


@dataclass
class Subscription:
    """订阅信息"""
    channel: str
    params: Dict[str, Any]
    callback: Callable
    active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WebSocketManager:
    """
    WebSocket 管理器
    
    功能:
    1. 自动连接和重连
    2. 心跳保活
    3. 消息路由和分发
    4. 订阅管理
    5. 连接池管理
    """
    
    def __init__(
        self,
        url: str,
        reconnect_interval: int = 5,
        max_reconnect_attempts: int = 10,
        heartbeat_interval: int = 30,
        message_timeout: int = 60,
    ):
        """
        初始化WebSocket管理器
        
        Args:
            url: WebSocket URL
            reconnect_interval: 重连间隔(秒)
            max_reconnect_attempts: 最大重连次数
            heartbeat_interval: 心跳间隔(秒)
            message_timeout: 消息超时(秒)
        """
        self.url = url
        self.reconnect_interval = reconnect_interval
        self.max_reconnect_attempts = max_reconnect_attempts
        self.heartbeat_interval = heartbeat_interval
        self.message_timeout = message_timeout
        
        # 连接状态
        self._state = ConnectionState.DISCONNECTED
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        
        # 订阅管理
        self._subscriptions: Dict[str, Subscription] = {}
        
        # 消息处理
        self._message_handlers: Dict[str, List[Callable]] = {}
        
        # 运行任务
        self._receive_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        
        # 统计信息
        self._reconnect_count = 0
        self._messages_received = 0
        self._last_message_time: Optional[datetime] = None
        
        logger.info(f"WebSocket管理器初始化 | URL: {url}")
    
    @property
    def state(self) -> ConnectionState:
        return self._state
    
    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED and self._ws is not None
    
    def on_message(self, msg_type: str, handler: Callable) -> None:
        """
        注册消息处理器
        
        Args:
            msg_type: 消息类型
            handler: 处理函数
        """
        if msg_type not in self._message_handlers:
            self._message_handlers[msg_type] = []
        self._message_handlers[msg_type].append(handler)
    
    def on_event(self, event: str, handler: Callable) -> None:
        """
        注册事件处理器
        
        Args:
            event: 事件名称 ("connect", "disconnect", "error", "reconnect")
            handler: 处理函数
        """
        self.on_message(f"_event_{event}", handler)
    
    async def subscribe(
        self,
        channel: str,
        params: Dict[str, Any],
        callback: Callable
    ) -> bool:
        """
        订阅频道
        
        Args:
            channel: 频道名称
            params: 订阅参数
            callback: 回调函数
        
        Returns:
            是否订阅成功
        """
        sub_key = self._get_subscription_key(channel, params)
        
        if sub_key in self._subscriptions:
            logger.warning(f"订阅已存在: {channel}")
            return False
        
        subscription = Subscription(
            channel=channel,
            params=params,
            callback=callback
        )
        
        self._subscriptions[sub_key] = subscription
        
        # 如果已连接，立即发送订阅请求
        if self.is_connected:
            await self._send_subscribe(subscription)
        
        logger.info(f"添加订阅 | 频道: {channel}")
        return True
    
    async def unsubscribe(self, channel: str, params: Dict[str, Any]) -> bool:
        """
        取消订阅
        
        Args:
            channel: 频道名称
            params: 订阅参数
        
        Returns:
            是否取消成功
        """
        sub_key = self._get_subscription_key(channel, params)
        
        if sub_key not in self._subscriptions:
            return False
        
        subscription = self._subscriptions.pop(sub_key)
        subscription.active = False
        
        # 如果已连接，发送取消订阅请求
        if self.is_connected:
            await self._send_unsubscribe(subscription)
        
        logger.info(f"取消订阅 | 频道: {channel}")
        return True
    
    async def connect(self) -> bool:
        """
        建立连接
        
        Returns:
            是否连接成功
        """
        if self._state == ConnectionState.CONNECTED:
            return True
        
        self._state = ConnectionState.CONNECTING
        
        try:
            if not self._session:
                self._session = aiohttp.ClientSession()
            
            logger.info(f"正在连接WebSocket: {self.url}")
            
            self._ws = await self._session.ws_connect(
                self.url,
                heartbeat=self.heartbeat_interval,
                receive_timeout=self.message_timeout
            )
            
            self._state = ConnectionState.CONNECTED
            self._reconnect_count = 0
            
            logger.info("WebSocket连接成功")
            
            # 触发连接事件
            await self._emit_event("connect")
            
            # 重新订阅所有频道
            await self._resubscribe_all()
            
            # 启动接收任务
            self._receive_task = asyncio.create_task(self._receive_loop())
            
            return True
            
        except Exception as e:
            logger.error(f"WebSocket连接失败: {e}")
            self._state = ConnectionState.DISCONNECTED
            await self._emit_event("error", {"error": str(e)})
            return False
    
    async def disconnect(self) -> None:
        """断开连接"""
        if self._state == ConnectionState.DISCONNECTED:
            return
        
        self._state = ConnectionState.CLOSING
        
        # 取消任务
        if self._receive_task:
            self._receive_task.cancel()
            self._receive_task = None
        
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        
        # 关闭WebSocket
        if self._ws:
            await self._ws.close()
            self._ws = None
        
        self._state = ConnectionState.DISCONNECTED
        
        # 触发断开事件
        await self._emit_event("disconnect")
        
        logger.info("WebSocket已断开")
    
    async def send(self, data: Dict[str, Any]) -> bool:
        """
        发送消息
        
        Args:
            data: 消息数据
        
        Returns:
            是否发送成功
        """
        if not self.is_connected or not self._ws:
            logger.warning("WebSocket未连接，无法发送消息")
            return False
        
        try:
            await self._ws.send_json(data)
            logger.debug(f"发送消息: {data.get('type', data.get('method', 'unknown'))}")
            return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False
    
    async def _receive_loop(self) -> None:
        """接收消息循环"""
        while self.is_connected and self._ws:
            try:
                msg = await self._ws.receive()
                
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                    
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await self._handle_message(msg.data.decode())
                    
                elif msg.type == aiohttp.WSMsgType.PING:
                    await self._ws.pong()
                    
                elif msg.type == aiohttp.WSMsgType.PONG:
                    pass
                    
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                    logger.info("WebSocket关闭消息接收")
                    break
                    
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket错误: {self._ws.exception()}")
                    break
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"接收消息异常: {e}")
                break
        
        # 连接断开，尝试重连
        if self._state == ConnectionState.CONNECTED:
            await self._handle_disconnect()
    
    async def _handle_message(self, data: str) -> None:
        """处理消息"""
        self._messages_received += 1
        self._last_message_time = datetime.now(timezone.utc)
        
        try:
            msg = json.loads(data)
            msg_type = msg.get("type", msg.get("method", "unknown"))
            
            # 调用消息处理器
            handlers = self._message_handlers.get(msg_type, [])
            for handler in handlers:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(msg)
                    else:
                        handler(msg)
                except Exception as e:
                    logger.error(f"消息处理器异常: {e}")
            
            # 调用订阅回调
            channel = msg.get("channel", msg.get("params", {}).get("channel", ""))
            if channel:
                for sub in self._subscriptions.values():
                    if sub.channel == channel and sub.active:
                        try:
                            if asyncio.iscoroutinefunction(sub.callback):
                                await sub.callback(msg)
                            else:
                                sub.callback(msg)
                        except Exception as e:
                            logger.error(f"订阅回调异常: {e}")
            
            logger.debug(f"处理消息 | 类型: {msg_type}")
            
        except json.JSONDecodeError:
            logger.warning(f"无效的JSON消息: {data[:100]}")
        except Exception as e:
            logger.error(f"处理消息异常: {e}")
    
    async def _handle_disconnect(self) -> None:
        """处理断开连接"""
        logger.warning("WebSocket连接断开")
        
        self._state = ConnectionState.RECONNECTING
        await self._emit_event("disconnect")
        
        # 尝试重连
        try:
            for attempt in range(1, self.max_reconnect_attempts + 1):
                logger.info(f"尝试重连 ({attempt}/{self.max_reconnect_attempts})...")
                
                self._reconnect_count += 1
                
                if await self.connect():
                    logger.info("重连成功")
                    await self._emit_event("reconnect", {"attempt": attempt})
                    return
                
                # 指数退避
                wait_time = min(self.reconnect_interval * (2 ** min(attempt - 1, 5)), 60)
                await asyncio.sleep(wait_time)
            
            logger.error(f"重连失败，已达最大尝试次数 {self.max_reconnect_attempts}")
            self._state = ConnectionState.DISCONNECTED
            await self._emit_event("error", {"error": "重连失败"})
            
        except asyncio.CancelledError:
            logger.info("重连循环被取消")
            self._state = ConnectionState.DISCONNECTED
    
    async def _emit_event(self, event: str, data: Dict[str, Any] = None) -> None:
        """触发事件"""
        handlers = self._message_handlers.get(f"_event_{event}", [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data or {})
                else:
                    handler(data or {})
            except Exception as e:
                logger.error(f"事件处理器异常: {e}")
    
    async def _send_subscribe(self, subscription: Subscription) -> None:
        """发送订阅请求"""
        msg = {
            "type": "subscribe",
            "channel": subscription.channel,
            **subscription.params
        }
        await self.send(msg)
    
    async def _send_unsubscribe(self, subscription: Subscription) -> None:
        """发送取消订阅请求"""
        msg = {
            "type": "unsubscribe",
            "channel": subscription.channel,
            **subscription.params
        }
        await self.send(msg)
    
    async def _resubscribe_all(self) -> None:
        """重新订阅所有频道"""
        for subscription in self._subscriptions.values():
            if subscription.active:
                await self._send_subscribe(subscription)
    
    def _get_subscription_key(self, channel: str, params: Dict[str, Any]) -> str:
        """获取订阅键"""
        params_str = json.dumps(params, sort_keys=True)
        return f"{channel}:{params_str}"
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "state": self._state.value,
            "is_connected": self.is_connected,
            "reconnect_count": self._reconnect_count,
            "messages_received": self._messages_received,
            "last_message_time": self._last_message_time.isoformat() if self._last_message_time else None,
            "subscription_count": len(self._subscriptions),
        }


class PolymarketWebSocket(WebSocketManager):
    """
    Polymarket WebSocket 客户端
    
    专门用于订阅Polymarket市场数据
    """
    
    DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    def __init__(self, url: str = None, **kwargs):
        """
        初始化Polymarket WebSocket客户端
        
        Args:
            url: WebSocket URL，如果为None则使用默认值
            **kwargs: 其他WebSocketManager参数
        """
        final_url = url or self.DEFAULT_WS_URL
        super().__init__(url=final_url, **kwargs)
        logger.info(f"Polymarket WebSocket客户端初始化 | URL: {final_url}")
    
    async def subscribe_market(self, market_id: str, callback: Callable) -> bool:
        """订阅市场更新"""
        return await self.subscribe(
            channel="market",
            params={"market_id": market_id},
            callback=callback
        )
    
    async def subscribe_orderbook(self, market_id: str, callback: Callable) -> bool:
        """订阅订单簿更新"""
        return await self.subscribe(
            channel="orderbook",
            params={"market_id": market_id},
            callback=callback
        )
    
    async def subscribe_trades(self, market_id: str, callback: Callable) -> bool:
        """订阅交易流"""
        return await self.subscribe(
            channel="trades",
            params={"market_id": market_id},
            callback=callback
        )
    
    async def subscribe_user_orders(self, address: str, callback: Callable) -> bool:
        """订阅用户订单"""
        return await self.subscribe(
            channel="user_orders",
            params={"address": address},
            callback=callback
        )
