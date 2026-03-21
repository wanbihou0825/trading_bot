"""
交易持久化模块
==============
使用 aiosqlite (异步) 存储已处理交易、跟单记录和持仓跟踪。

解决问题:
- 重启后不重复处理同一交易
- 跟单历史可追溯
- 持仓状态持久化（含 risk_manager 仓位恢复）
- 不阻塞 asyncio 事件循环
"""

import asyncio
import aiosqlite
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional, Dict, Any, List

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ProcessedTx:
    """已处理交易"""
    tx_hash: str
    wallet_address: str
    market_id: str
    action: str  # "open", "close"
    status: str  # "success", "failed"
    processed_at: datetime
    copy_size: Optional[Decimal] = None
    copy_price: Optional[Decimal] = None
    error: Optional[str] = None


class TradePersistence:
    """
    交易记录持久化管理（异步版）

    使用 aiosqlite 替代同步 sqlite3，避免阻塞事件循环。
    支持:
    - 已处理交易去重
    - 跟单历史查询
    - 持仓跟踪持久化
    - risk_manager 仓位持久化与恢复
    - 订单幂等性记录
    """

    DEFAULT_DB_PATH = "data/trades.db"

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path or self.DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._db: Optional[aiosqlite.Connection] = None
        logger.info(f"交易持久化初始化 | 数据库: {self.db_path}")

    # ═══════════════════════════════════════════════════════════════
    # 连接管理
    # ═══════════════════════════════════════════════════════════════

    async def connect(self) -> None:
        """打开数据库连接并初始化表结构"""
        self._db = await aiosqlite.connect(str(self.db_path), timeout=30.0)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._init_db()
        logger.info("交易持久化数据库已连接")

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None

    async def _ensure_connected(self) -> aiosqlite.Connection:
        """确保已连接，未连接则自动连接"""
        if self._db is None:
            await self.connect()
        return self._db

    async def _init_db(self) -> None:
        """初始化数据库表"""
        db = await self._ensure_connected()
        async with db.cursor() as cursor:
            # 已处理交易表
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_txs (
                    tx_hash TEXT PRIMARY KEY,
                    wallet_address TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    copy_size TEXT,
                    copy_price TEXT,
                    error TEXT,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 跟单历史表
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS copy_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_wallet TEXT NOT NULL,
                    source_tx_hash TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    market_question TEXT,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    original_size TEXT,
                    copy_size TEXT,
                    copy_price TEXT,
                    order_id TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TIMESTAMP,
                    executed_at TIMESTAMP
                )
            """)

            # 持仓跟踪表
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS tracked_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_wallet TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    source_size TEXT NOT NULL,
                    our_size TEXT NOT NULL,
                    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    UNIQUE(source_wallet, market_id)
                )
            """)

            # risk_manager 仓位持久化表
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS rm_positions (
                    market_id TEXT PRIMARY KEY,
                    market_question TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    current_price TEXT NOT NULL,
                    size TEXT NOT NULL,
                    stop_loss_price TEXT,
                    take_profit_price TEXT,
                    opened_at TIMESTAMP NOT NULL
                )
            """)

            # 订单幂等记录表
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS order_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 创建索引
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_processed_txs_wallet
                ON processed_txs(wallet_address)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_processed_txs_time
                ON processed_txs(processed_at)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_copy_trades_wallet
                ON copy_trades(source_wallet)
            """)

        await db.commit()
    
    # ═══════════════════════════════════════════════════════════════
    # 已处理交易管理
    # ═══════════════════════════════════════════════════════════════
    
    async def is_processed(self, tx_hash: str) -> bool:
        """检查交易是否已处理"""
        async with self._lock:
            db = await self._ensure_connected()
            async with db.execute(
                "SELECT 1 FROM processed_txs WHERE tx_hash = ?",
                (tx_hash,)
            ) as cursor:
                return (await cursor.fetchone()) is not None

    async def mark_processed(
        self,
        tx_hash: str,
        wallet_address: str,
        market_id: str,
        action: str,
        status: str,
        copy_size: Optional[Decimal] = None,
        copy_price: Optional[Decimal] = None,
        error: Optional[str] = None,
    ) -> None:
        """标记交易为已处理"""
        async with self._lock:
            db = await self._ensure_connected()
            await db.execute("""
                INSERT OR REPLACE INTO processed_txs
                (tx_hash, wallet_address, market_id, action, status, copy_size, copy_price, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tx_hash,
                wallet_address.lower(),
                market_id,
                action,
                status,
                str(copy_size) if copy_size else None,
                str(copy_price) if copy_price else None,
                error,
            ))
            await db.commit()

    async def get_recent_processed(
        self,
        wallet_address: Optional[str] = None,
        limit: int = 100,
    ) -> List[ProcessedTx]:
        """获取最近处理的交易"""
        db = await self._ensure_connected()

        if wallet_address:
            sql = "SELECT * FROM processed_txs WHERE wallet_address = ? ORDER BY processed_at DESC LIMIT ?"
            params = (wallet_address.lower(), limit)
        else:
            sql = "SELECT * FROM processed_txs ORDER BY processed_at DESC LIMIT ?"
            params = (limit,)

        async with db.execute(sql, params) as cursor:
            results = []
            async for row in cursor:
                results.append(ProcessedTx(
                    tx_hash=row["tx_hash"],
                    wallet_address=row["wallet_address"],
                    market_id=row["market_id"],
                    action=row["action"],
                    status=row["status"],
                    processed_at=datetime.fromisoformat(row["processed_at"]),
                    copy_size=Decimal(row["copy_size"]) if row["copy_size"] else None,
                    copy_price=Decimal(row["copy_price"]) if row["copy_price"] else None,
                    error=row["error"],
                ))
            return results

    async def cleanup_old_records(self, days: int = 30) -> int:
        """清理旧记录"""
        async with self._lock:
            db = await self._ensure_connected()
            cursor = await db.execute("""
                DELETE FROM processed_txs
                WHERE processed_at < datetime('now', ?)
            """, (f"-{days} days",))
            deleted = cursor.rowcount
            await db.commit()
            if deleted > 0:
                logger.info(f"清理了 {deleted} 条旧记录")
            return deleted
    
    # ═══════════════════════════════════════════════════════════════
    # 跟单记录管理
    # ═══════════════════════════════════════════════════════════════
    
    async def save_copy_trade(self, trade: Dict[str, Any]) -> int:
        """保存跟单记录"""
        async with self._lock:
            db = await self._ensure_connected()
            cursor = await db.execute("""
                INSERT INTO copy_trades
                (source_wallet, source_tx_hash, market_id, market_question,
                 side, action, original_size, copy_size, copy_price,
                 order_id, status, error, created_at, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.get("source_wallet", "").lower(),
                trade.get("source_tx_hash", ""),
                trade.get("market_id", ""),
                trade.get("market_question", ""),
                trade.get("side", ""),
                trade.get("action", ""),
                str(trade.get("original_size", "0")),
                str(trade.get("copy_size", "0")),
                str(trade.get("copy_price", "0")),
                trade.get("order_id"),
                trade.get("status", ""),
                trade.get("error"),
                trade.get("created_at"),
                trade.get("executed_at"),
            ))
            await db.commit()
            return cursor.lastrowid

    async def get_copy_trades(
        self,
        wallet_address: Optional[str] = None,
        market_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """获取跟单记录"""
        db = await self._ensure_connected()

        query = "SELECT * FROM copy_trades WHERE 1=1"
        params: list = []

        if wallet_address:
            query += " AND source_wallet = ?"
            params.append(wallet_address.lower())

        if market_id:
            query += " AND market_id = ?"
            params.append(market_id)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        async with db.execute(query, params) as cursor:
            results = []
            async for row in cursor:
                results.append(dict(row))
            return results
    
    # ═══════════════════════════════════════════════════════════════
    # 持仓跟踪管理
    # ═══════════════════════════════════════════════════════════════
    
    async def save_tracked_position(
        self,
        source_wallet: str,
        market_id: str,
        side: str,
        source_size: Decimal,
        our_size: Decimal,
    ) -> None:
        """保存跟踪的持仓"""
        async with self._lock:
            db = await self._ensure_connected()
            await db.execute("""
                INSERT OR REPLACE INTO tracked_positions
                (source_wallet, market_id, side, source_size, our_size, opened_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                source_wallet.lower(),
                market_id,
                side,
                str(source_size),
                str(our_size),
                datetime.now(timezone.utc).isoformat(),
            ))
            await db.commit()

    async def close_tracked_position(
        self,
        source_wallet: str,
        market_id: str,
    ) -> None:
        """关闭跟踪的持仓"""
        async with self._lock:
            db = await self._ensure_connected()
            await db.execute("""
                UPDATE tracked_positions
                SET closed_at = ?
                WHERE source_wallet = ? AND market_id = ?
            """, (
                datetime.now(timezone.utc).isoformat(),
                source_wallet.lower(),
                market_id,
            ))
            await db.commit()

    async def get_tracked_positions(
        self,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """获取跟踪的持仓"""
        db = await self._ensure_connected()

        if active_only:
            sql = "SELECT * FROM tracked_positions WHERE closed_at IS NULL ORDER BY opened_at DESC"
        else:
            sql = "SELECT * FROM tracked_positions ORDER BY opened_at DESC"

        async with db.execute(sql) as cursor:
            results = []
            async for row in cursor:
                results.append({
                    "source_wallet": row["source_wallet"],
                    "market_id": row["market_id"],
                    "side": row["side"],
                    "source_size": Decimal(row["source_size"]),
                    "our_size": Decimal(row["our_size"]),
                    "opened_at": row["opened_at"],
                    "closed_at": row["closed_at"],
                })
            return results

    async def get_active_tracked_count(self) -> int:
        """获取活跃跟踪持仓数量"""
        db = await self._ensure_connected()
        async with db.execute(
            "SELECT COUNT(*) FROM tracked_positions WHERE closed_at IS NULL"
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]

    # ═══════════════════════════════════════════════════════════════
    # risk_manager 仓位持久化
    # ═══════════════════════════════════════════════════════════════

    async def save_rm_position(self, position_data: Dict[str, Any]) -> None:
        """保存 risk_manager 仓位"""
        async with self._lock:
            db = await self._ensure_connected()
            await db.execute("""
                INSERT OR REPLACE INTO rm_positions
                (market_id, market_question, side, entry_price,
                 current_price, size, stop_loss_price, take_profit_price, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position_data["market_id"],
                position_data["market_question"],
                position_data["side"],
                str(position_data["entry_price"]),
                str(position_data["current_price"]),
                str(position_data["size"]),
                str(position_data["stop_loss_price"]) if position_data.get("stop_loss_price") else None,
                str(position_data["take_profit_price"]) if position_data.get("take_profit_price") else None,
                position_data["opened_at"],
            ))
            await db.commit()

    async def remove_rm_position(self, market_id: str) -> None:
        """删除 risk_manager 仓位"""
        async with self._lock:
            db = await self._ensure_connected()
            await db.execute("DELETE FROM rm_positions WHERE market_id = ?", (market_id,))
            await db.commit()

    async def load_rm_positions(self) -> List[Dict[str, Any]]:
        """加载所有 risk_manager 仓位（用于崩溃恢复）"""
        db = await self._ensure_connected()
        async with db.execute("SELECT * FROM rm_positions") as cursor:
            results = []
            async for row in cursor:
                results.append({
                    "market_id": row["market_id"],
                    "market_question": row["market_question"],
                    "side": row["side"],
                    "entry_price": Decimal(row["entry_price"]),
                    "current_price": Decimal(row["current_price"]),
                    "size": Decimal(row["size"]),
                    "stop_loss_price": Decimal(row["stop_loss_price"]) if row["stop_loss_price"] else None,
                    "take_profit_price": Decimal(row["take_profit_price"]) if row["take_profit_price"] else None,
                    "opened_at": row["opened_at"],
                })
            return results

    # ═══════════════════════════════════════════════════════════════
    # 订单幂等性
    # ═══════════════════════════════════════════════════════════════

    async def check_idempotency(self, key: str) -> Optional[Dict[str, Any]]:
        """检查幂等键是否已存在"""
        db = await self._ensure_connected()
        async with db.execute(
            "SELECT * FROM order_idempotency WHERE idempotency_key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def save_idempotency(
        self,
        key: str,
        market_id: str,
        side: str,
        size: Decimal,
        status: str,
        order_id: Optional[str] = None,
    ) -> None:
        """保存幂等记录"""
        async with self._lock:
            db = await self._ensure_connected()
            await db.execute("""
                INSERT OR REPLACE INTO order_idempotency
                (idempotency_key, market_id, side, size, status, order_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (key, market_id, side, str(size), status, order_id))
            await db.commit()
    
    # ═══════════════════════════════════════════════════════════════
    # 统计信息
    # ═══════════════════════════════════════════════════════════════
    
    async def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        db = await self._ensure_connected()

        async with db.execute("SELECT COUNT(*) FROM processed_txs") as c:
            processed_count = (await c.fetchone())[0]

        async with db.execute("SELECT COUNT(*) FROM copy_trades") as c:
            trades_count = (await c.fetchone())[0]

        async with db.execute(
            "SELECT status, COUNT(*) as cnt FROM copy_trades GROUP BY status"
        ) as c:
            status_counts = {row["status"]: row["cnt"] async for row in c}

        async with db.execute(
            "SELECT COUNT(*) FROM tracked_positions WHERE closed_at IS NULL"
        ) as c:
            active_tracked = (await c.fetchone())[0]

        return {
            "processed_txs_count": processed_count,
            "copy_trades_count": trades_count,
            "success_count": status_counts.get("filled", 0),
            "failed_count": status_counts.get("failed", 0) + status_counts.get("error", 0),
            "active_tracked_positions": active_tracked,
        }

    async def get_total_volume(self) -> Decimal:
        """获取总交易量"""
        db = await self._ensure_connected()
        async with db.execute("""
            SELECT SUM(CAST(copy_size AS REAL)) as total
            FROM copy_trades
            WHERE status = 'filled'
        """) as cursor:
            result = (await cursor.fetchone())[0]
            return Decimal(str(result or 0))

    # ═══════════════════════════════════════════════════════════════
    # 导出功能
    # ═══════════════════════════════════════════════════════════════

    async def export_to_csv(
        self,
        output_path: str,
        table: str = "copy_trades",
        days: int = 30,
    ) -> str:
        """导出数据到 CSV"""
        import csv

        db = await self._ensure_connected()

        if table == "copy_trades":
            sql = """SELECT * FROM copy_trades
                     WHERE created_at >= datetime('now', ?)
                     ORDER BY created_at DESC"""
        else:
            sql = """SELECT * FROM processed_txs
                     WHERE processed_at >= datetime('now', ?)
                     ORDER BY processed_at DESC"""

        async with db.execute(sql, (f"-{days} days",)) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            logger.warning("没有数据可导出")
            return ""

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows([dict(row) for row in rows])

        logger.info(f"导出 {len(rows)} 条记录到 {output_file}")
        return str(output_file)
