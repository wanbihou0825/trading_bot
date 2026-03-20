"""
交易持久化模块
==============
使用 SQLite 存储已处理交易、跟单记录和持仓跟踪。

解决问题:
- 重启后不重复处理同一交易
- 跟单历史可追溯
- 持仓状态持久化
"""

import asyncio
import sqlite3
from contextlib import contextmanager
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
    交易记录持久化管理
    
    使用 SQLite 存储，支持:
    - 已处理交易去重
    - 跟单历史查询
    - 持仓跟踪持久化
    """
    
    DEFAULT_DB_PATH = "data/trades.db"
    
    def __init__(self, db_path: Optional[str] = None):
        """
        初始化持久化管理器
        
        Args:
            db_path: 数据库路径
        """
        self.db_path = Path(db_path or self.DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._lock = asyncio.Lock()
        self._init_db()
        
        logger.info(f"交易持久化初始化 | 数据库: {self.db_path}")
    
    @contextmanager
    def _get_connection(self):
        """获取数据库连接"""
        # 使用 WAL 模式提高并发性能
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,  # 增加超时时间
            check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        
        # 启用 WAL 模式
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception as e:
            logger.warning(f"无法启用 WAL 模式: {e}")
        
        try:
            yield conn
        finally:
            conn.close()
    
    def _init_db(self) -> None:
        """初始化数据库表"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 已处理交易表
            cursor.execute("""
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
            cursor.execute("""
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
            cursor.execute("""
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
            
            # 创建索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_processed_txs_wallet 
                ON processed_txs(wallet_address)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_processed_txs_time 
                ON processed_txs(processed_at)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_copy_trades_wallet 
                ON copy_trades(source_wallet)
            """)
            
            conn.commit()
    
    # ═══════════════════════════════════════════════════════════════
    # 已处理交易管理
    # ═══════════════════════════════════════════════════════════════
    
    async def is_processed(self, tx_hash: str) -> bool:
        """检查交易是否已处理"""
        async with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM processed_txs WHERE tx_hash = ?",
                    (tx_hash,)
                )
                return cursor.fetchone() is not None
    
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
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
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
                conn.commit()
    
    async def get_recent_processed(
        self,
        wallet_address: Optional[str] = None,
        limit: int = 100,
    ) -> List[ProcessedTx]:
        """获取最近处理的交易"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            if wallet_address:
                cursor.execute("""
                    SELECT * FROM processed_txs 
                    WHERE wallet_address = ?
                    ORDER BY processed_at DESC 
                    LIMIT ?
                """, (wallet_address.lower(), limit))
            else:
                cursor.execute("""
                    SELECT * FROM processed_txs 
                    ORDER BY processed_at DESC 
                    LIMIT ?
                """, (limit,))
            
            results = []
            for row in cursor.fetchall():
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
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    DELETE FROM processed_txs 
                    WHERE processed_at < datetime('now', ?)
                """, (f"-{days} days",))
                deleted = cursor.rowcount
                conn.commit()
                
                if deleted > 0:
                    logger.info(f"清理了 {deleted} 条旧记录")
                
                return deleted
    
    # ═══════════════════════════════════════════════════════════════
    # 跟单记录管理
    # ═══════════════════════════════════════════════════════════════
    
    async def save_copy_trade(self, trade: Dict[str, Any]) -> int:
        """保存跟单记录"""
        async with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
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
                conn.commit()
                return cursor.lastrowid
    
    async def get_copy_trades(
        self,
        wallet_address: Optional[str] = None,
        market_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """获取跟单记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM copy_trades WHERE 1=1"
            params = []
            
            if wallet_address:
                query += " AND source_wallet = ?"
                params.append(wallet_address.lower())
            
            if market_id:
                query += " AND market_id = ?"
                params.append(market_id)
            
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            
            cursor.execute(query, params)
            
            results = []
            for row in cursor.fetchall():
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
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
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
                conn.commit()
    
    async def close_tracked_position(
        self,
        source_wallet: str,
        market_id: str,
    ) -> None:
        """关闭跟踪的持仓"""
        async with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE tracked_positions 
                    SET closed_at = ?
                    WHERE source_wallet = ? AND market_id = ?
                """, (
                    datetime.now(timezone.utc).isoformat(),
                    source_wallet.lower(),
                    market_id,
                ))
                conn.commit()
    
    async def get_tracked_positions(
        self,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """获取跟踪的持仓"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            if active_only:
                cursor.execute("""
                    SELECT * FROM tracked_positions 
                    WHERE closed_at IS NULL
                    ORDER BY opened_at DESC
                """)
            else:
                cursor.execute("""
                    SELECT * FROM tracked_positions 
                    ORDER BY opened_at DESC
                """)
            
            results = []
            for row in cursor.fetchall():
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
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM tracked_positions 
                WHERE closed_at IS NULL
            """)
            return cursor.fetchone()[0]
    
    # ═══════════════════════════════════════════════════════════════
    # 统计信息
    # ═══════════════════════════════════════════════════════════════
    
    async def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 已处理交易数
            cursor.execute("SELECT COUNT(*) FROM processed_txs")
            processed_count = cursor.fetchone()[0]
            
            # 跟单记录数
            cursor.execute("SELECT COUNT(*) FROM copy_trades")
            trades_count = cursor.fetchone()[0]
            
            # 成功/失败数
            cursor.execute("""
                SELECT status, COUNT(*) as cnt 
                FROM copy_trades 
                GROUP BY status
            """)
            status_counts = {row["status"]: row["cnt"] for row in cursor.fetchall()}
            
            # 活跃跟踪持仓数
            cursor.execute("""
                SELECT COUNT(*) FROM tracked_positions 
                WHERE closed_at IS NULL
            """)
            active_tracked = cursor.fetchone()[0]
            
            return {
                "processed_txs_count": processed_count,
                "copy_trades_count": trades_count,
                "success_count": status_counts.get("filled", 0),
                "failed_count": status_counts.get("failed", 0) + status_counts.get("error", 0),
                "active_tracked_positions": active_tracked,
            }
    
    async def get_total_volume(self) -> Decimal:
        """获取总交易量"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT SUM(CAST(copy_size AS REAL)) as total 
                FROM copy_trades 
                WHERE status = 'filled'
            """)
            result = cursor.fetchone()[0]
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
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            if table == "copy_trades":
                cursor.execute("""
                    SELECT * FROM copy_trades 
                    WHERE created_at >= datetime('now', ?)
                    ORDER BY created_at DESC
                """, (f"-{days} days",))
            else:
                cursor.execute("""
                    SELECT * FROM processed_txs 
                    WHERE processed_at >= datetime('now', ?)
                    ORDER BY processed_at DESC
                """, (f"-{days} days",))
            
            rows = cursor.fetchall()
            
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
