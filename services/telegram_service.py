"""
Telegram 服务模块
=================
发送交易通知和告警到 Telegram。
"""

import asyncio
from typing import Optional
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from utils.logger import get_logger, mask_wallet_address
from utils.validation import validate_wallet_address as validate_addr

logger = get_logger(__name__)


@dataclass
class TelegramConfig:
    """Telegram 配置"""
    bot_token: str
    chat_id: str
    enabled: bool = True


class TelegramService:
    """
    Telegram 通知服务
    
    支持发送各种类型的交易通知。
    """
    
    API_BASE_URL = "https://api.telegram.org/bot{token}/{method}"
    
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: bool = True
    ):
        """
        初始化 Telegram 服务
        
        Args:
            bot_token: Bot Token
            chat_id: 聊天ID
            enabled: 是否启用
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        
        if self.enabled:
            logger.info(f"Telegram 服务已启用 | Chat ID: {chat_id}")
        else:
            logger.info("Telegram 服务未启用")
    
    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        发送消息
        
        Args:
            text: 消息文本
            parse_mode: 解析模式 (HTML, Markdown)
        
        Returns:
            是否发送成功
        """
        if not self.enabled:
            logger.debug(f"[Telegram未启用] 消息: {text[:100]}...")
            return False
        
        try:
            import aiohttp
            
            url = self.API_BASE_URL.format(
                token=self.bot_token,
                method="sendMessage"
            )
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": parse_mode
                    }
                ) as response:
                    if response.status == 200:
                        logger.debug("Telegram 消息发送成功")
                        return True
                    else:
                        error = await response.text()
                        logger.warning(f"Telegram 发送失败: {error}")
                        return False
                        
        except Exception as e:
            logger.error(f"Telegram 发送异常: {e}")
            return False
    
    async def send_startup_notification(
        self,
        wallet_address: str,
        dry_run: bool,
        max_daily_loss: Decimal
    ) -> None:
        """发送启动通知"""
        masked_wallet = mask_wallet_address(wallet_address) if wallet_address else "未配置"
        
        message = f"""
<b>Bot 已启动</b>

<b>钱包:</b> <code>{masked_wallet}</code>
<b>模式:</b> {'模拟' if dry_run else '实盘'}
<b>日损失限制:</b> ${max_daily_loss}
<b>时间:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC
"""
        await self.send_message(message)
    
    async def send_shutdown_notification(self, reason: str) -> None:
        """发送关闭通知"""
        message = f"""
<b>Bot 已停止</b>

<b>原因:</b> {reason}
<b>时间:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC
"""
        await self.send_message(message)
    
    async def send_trade_notification(
        self,
        action: str,
        market_question: str,
        side: str,
        amount: Decimal,
        price: Decimal,
        confidence: Decimal,
        strategy: str
    ) -> None:
        """发送交易通知"""
        message = f"""
<b>交易执行</b>

<b>动作:</b> {action}
<b>市场:</b> {market_question[:100]}...
<b>方向:</b> {side}
<b>数量:</b> ${amount:.2f}
<b>价格:</b> {price:.4f}
<b>置信度:</b> {confidence*100:.1f}%
<b>策略:</b> {strategy}
"""
        await self.send_message(message)
    
    async def send_position_closed_notification(
        self,
        market_question: str,
        pnl: Decimal,
        pnl_percentage: Decimal,
        reason: str
    ) -> None:
        """发送平仓通知"""
        pnl_sign = "+" if pnl >= 0 else ""
        emoji = "✅" if pnl >= 0 else "❌"
        
        message = f"""
<b>{emoji} 平仓</b>

<b>市场:</b> {market_question[:100]}...
<b>盈亏:</b> {pnl_sign}${abs(pnl):.2f} ({pnl_sign}{pnl_percentage:.1f}%)
<b>原因:</b> {reason}
"""
        await self.send_message(message)
    
    async def send_circuit_breaker_notification(
        self,
        reason: str,
        daily_loss: Decimal,
        daily_trades: int
    ) -> None:
        """发送熔断器通知"""
        message = f"""
<b>⚠️ 熔断器触发</b>

<b>原因:</b> {reason}
<b>日累计损失:</b> ${daily_loss:.2f}
<b>日交易次数:</b> {daily_trades}

<b>交易已暂停</b>
"""
        await self.send_message(message)
    
    async def send_error_notification(
        self,
        error_type: str,
        error_message: str
    ) -> None:
        """发送错误通知"""
        message = f"""
<b>❌ 错误告警</b>

<b>类型:</b> {error_type}
<b>消息:</b> {error_message[:200]}
<b>时间:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC
"""
        await self.send_message(message)
    
    async def send_daily_summary(
        self,
        total_trades: int,
        winning_trades: int,
        total_pnl: Decimal,
        win_rate: Decimal
    ) -> None:
        """发送每日摘要"""
        pnl_sign = "+" if total_pnl >= 0 else ""
        emoji = "📈" if total_pnl >= 0 else "📉"
        
        message = f"""
<b>{emoji} 每日摘要</b>

<b>交易次数:</b> {total_trades}
<b>胜率:</b> {win_rate*100:.1f}%
<b>盈亏:</b> {pnl_sign}${abs(total_pnl):.2f}
<b>时间:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d')}
"""
        await self.send_message(message)
