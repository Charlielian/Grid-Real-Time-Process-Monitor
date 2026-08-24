"""SQLite 数据访问层。

数据库使用每次操作短连接和 WAL 日志模式：读操作独立关闭连接，写操作通过
事务上下文在成功时提交、异常时回滚。工单采用主键 upsert，并在状态、节点或
处理人变化时记录事件；批量同步可复用同一连接，避免每条记录单独提交。查询
方法通过参数绑定处理筛选条件，分页上限和清理批次上限用于控制资源占用。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterator

from shared.models import WorkOrder


_COMPLETED_STATUSES = ("已办结", "completed", "done")


@dataclass(frozen=True, slots=True)
class DatabaseMaintenanceStats:
    """一次保留策略清理操作的删除数量和文件大小快照。"""
    work_orders_deleted: int = 0
    events_deleted: int = 0
    sync_runs_deleted: int = 0
    database_size_bytes: int = 0
    wal_size_bytes: int = 0


class Database:
    """SQLite repository with short-lived connections per operation."""

    def __init__(self, path: Path | str) -> None:
        """初始化数据库路径并创建缺失的父目录和表结构。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        """创建短生命周期 SQLite 连接。

        WAL 允许读写并发，busy_timeout 为写锁竞争提供最多 30 秒等待；调用方
        必须通过 ``_transaction`` 或 ``_read`` 管理连接关闭。
        """
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """提供事务连接，正常退出提交，任意异常回滚后重新抛出。"""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """提供只读语义的短连接；退出时关闭连接，不执行提交。"""
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        """以幂等 DDL 创建工单、事件、同步运行和账号设置表及索引。"""
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_orders (
                    order_id TEXT PRIMARY KEY,
                    number TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_node TEXT NOT NULL,
                    assignee TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    process_instance_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    process_version TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_work_orders_updated_at ON work_orders(updated_at);
                CREATE INDEX IF NOT EXISTS idx_work_orders_task_id ON work_orders(task_id);
                CREATE INDEX IF NOT EXISTS idx_work_orders_status ON work_orders(status);
                CREATE INDEX IF NOT EXISTS idx_work_orders_node ON work_orders(current_node);
                CREATE TABLE IF NOT EXISTS work_order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_work_order_events_order ON work_order_events(order_id, id);
                CREATE INDEX IF NOT EXISTS idx_work_order_events_created_at ON work_order_events(created_at);
                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    total INTEGER NOT NULL DEFAULT 0,
                    added INTEGER NOT NULL DEFAULT 0,
                    changed INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sync_runs_finished_at ON sync_runs(finished_at);
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS saved_accounts (
                    login_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    last_heartbeat_at TEXT,
                    heartbeat_status TEXT NOT NULL DEFAULT 'unknown',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_saved_accounts_last_used ON saved_accounts(last_used_at DESC);
                """
            )

    def file_sizes(self) -> tuple[int, int]:
        """返回主数据库文件和 WAL 文件当前字节数，缺失文件按零计。"""
        database_size = self.path.stat().st_size if self.path.exists() else 0
        wal_path = Path(f"{self.path}-wal")
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
        return database_size, wal_size

    def checkpoint_wal(self) -> None:
        """以 PASSIVE 模式请求 WAL checkpoint，不阻塞现有读写事务。"""
        with self._read() as connection:
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def cleanup_retention(
        self,
        *,
        work_order_cutoff: str | None = None,
        event_cutoff: str | None = None,
        sync_run_cutoff: str | None = None,
        batch_size: int = 500,
    ) -> DatabaseMaintenanceStats:
        """按截止时间分批删除历史数据并返回维护统计。

        每次调用在一个事务中完成，``batch_size`` 被限制在 1 到 10000，防止
        单次 DELETE 持锁过久。工单删除前先删除关联事件；同步运行只删除已
        完成且早于截止时间的记录。提交后再读取文件大小，因此统计反映提交态。
        """
        batch_size = max(1, min(int(batch_size), 10000))
        work_orders_deleted = events_deleted = sync_runs_deleted = 0
        with self._transaction() as connection:
            if work_order_cutoff:
                ids = [row["order_id"] for row in connection.execute(
                    "SELECT order_id FROM work_orders WHERE updated_at < ? ORDER BY updated_at LIMIT ?",
                    (work_order_cutoff, batch_size),
                )]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    events_deleted += connection.execute(
                        f"DELETE FROM work_order_events WHERE order_id IN ({placeholders})", ids
                    ).rowcount
                    work_orders_deleted += connection.execute(
                        f"DELETE FROM work_orders WHERE order_id IN ({placeholders})", ids
                    ).rowcount
            if event_cutoff:
                events_deleted += connection.execute(
                    "DELETE FROM work_order_events WHERE created_at < ? AND id IN "
                    "(SELECT id FROM work_order_events WHERE created_at < ? ORDER BY created_at, id LIMIT ?)",
                    (event_cutoff, event_cutoff, batch_size),
                ).rowcount
            if sync_run_cutoff:
                sync_runs_deleted += connection.execute(
                    "DELETE FROM sync_runs WHERE finished_at IS NOT NULL AND finished_at < ? AND id IN "
                    "(SELECT id FROM sync_runs WHERE finished_at IS NOT NULL AND finished_at < ? ORDER BY finished_at, id LIMIT ?)",
                    (sync_run_cutoff, sync_run_cutoff, batch_size),
                ).rowcount
        database_size, wal_size = self.file_sizes()
        return DatabaseMaintenanceStats(
            work_orders_deleted=work_orders_deleted,
            events_deleted=events_deleted,
            sync_runs_deleted=sync_runs_deleted,
            database_size_bytes=database_size,
            wal_size_bytes=wal_size,
        )

    def upsert_work_order(self, order: WorkOrder, connection: sqlite3.Connection | None = None) -> tuple[bool, list[tuple[str, str, str | None]]]:
        """插入或更新单个工单，并返回是否新增及生成的事件。

        传入连接时复用调用方事务，不单独提交或关闭；不传连接时自行管理短
        事务。事件只记录状态、节点、处理人变化，新增工单生成 ``added`` 事件。
        """
        owns_connection = connection is None
        if owns_connection:
            connection = self._connect()
        assert connection is not None
        try:
            now = datetime.now(timezone.utc).isoformat()
            existing = connection.execute(
                "SELECT * FROM work_orders WHERE order_id = ?", (order.order_id,)
            ).fetchone()
            events: list[tuple[str, str, str | None]] = []
            if existing is None:
                events.append(("added", "", order.status))
            else:
                # 仅业务状态字段变化才写事件，避免每次轮询都制造噪声。
                for field, event_type in (
                    ("status", "status_changed"),
                    ("current_node", "node_changed"),
                    ("assignee", "assignee_changed"),
                ):
                    old = str(existing[field] or "")
                    new = str(getattr(order, field) or "")
                    if old != new:
                        events.append((event_type, old, new))
            connection.execute(
                """
                INSERT INTO work_orders
                (order_id, number, title, status, current_node, assignee, created_at, due_at,
                 process_instance_id, task_id, process_version, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                  number=excluded.number, title=excluded.title, status=excluded.status,
                  current_node=excluded.current_node, assignee=excluded.assignee,
                  created_at=excluded.created_at, due_at=excluded.due_at,
                  process_instance_id=excluded.process_instance_id, task_id=excluded.task_id,
                  process_version=excluded.process_version, raw_json=excluded.raw_json,
                  updated_at=excluded.updated_at
                """,
                (
                    order.order_id, order.number, order.title, order.status,
                    order.current_node, order.assignee, order.created_at, order.due_at,
                    order.process_instance_id, order.task_id, order.process_version,
                    json.dumps(order.raw, ensure_ascii=False), now,
                ),
            )
            for event_type, old, new in events:
                connection.execute(
                    "INSERT INTO work_order_events (order_id, event_type, old_value, new_value, created_at) VALUES (?, ?, ?, ?, ?)",
                    (order.order_id, event_type, old, new, now),
                )
            if owns_connection:
                connection.commit()
            return existing is None, events
        except Exception:
            if owns_connection:
                connection.rollback()
            raise
        finally:
            if owns_connection:
                connection.close()

    def upsert_orders(self, orders: list[WorkOrder]) -> tuple[int, int, int]:
        """在一个事务中批量 upsert，返回总数、新增数和变化事件数。"""
        added = 0
        changed = 0
        with self._transaction() as connection:
            for order in orders:
                is_new, events = self.upsert_work_order(order, connection)
                added += int(is_new)
                changed += len(events) - int(is_new)
        return len(orders), added, changed

    @staticmethod
    def _work_order_filter_clause(
        *,
        keyword: str = "",
        status: str = "",
        node: str = "",
        start_time: str = "",
        end_time: str = "",
        title_keywords: tuple[str, ...] | list[str] = (),
    ) -> tuple[str, list[Any]]:
        """构造参数化工单 WHERE 子句及其参数，不直接拼接用户值。"""
        where: list[str] = []
        params: list[Any] = []
        if title_keywords:
            placeholders = " OR ".join("title LIKE ?" for _ in title_keywords)
            where.append(f"({placeholders})")
            params.extend(f"%{title}%" for title in title_keywords)
        if keyword:
            where.append("(number LIKE ? OR title LIKE ? OR assignee LIKE ? OR order_id LIKE ?)")
            params.extend([f"%{keyword}%"] * 4)
        if status:
            if status == "active":
                where.append("status NOT IN (?, ?, ?)")
                params.extend(_COMPLETED_STATUSES)
            elif status == "completed":
                where.append("status IN (?, ?, ?)")
                params.extend(_COMPLETED_STATUSES)
            else:
                where.append("status = ?")
                params.append(status)
        if node:
            where.append("current_node LIKE ?")
            params.append(f"%{node}%")
        if start_time:
            where.append("created_at >= ?")
            params.append(start_time)
        if end_time:
            where.append("created_at <= ?")
            params.append(end_time)
        return (f"WHERE {' AND '.join(where)}" if where else "", params)

    def list_work_orders(
        self,
        limit: int = 500,
        *,
        offset: int = 0,
        keyword: str = "",
        status: str = "",
        node: str = "",
        start_time: str = "",
        end_time: str = "",
        title_keywords: tuple[str, ...] | list[str] = (),
        title_keyword: str = "",
    ) -> list[sqlite3.Row]:
        """按筛选条件倒序分页读取工单，限制单页最多 500 条。"""
        keywords = tuple(title_keywords) or ((title_keyword,) if title_keyword else ())
        clause, params = self._work_order_filter_clause(
            keyword=keyword,
            status=status,
            node=node,
            start_time=start_time,
            end_time=end_time,
            title_keywords=keywords,
        )
        with self._read() as connection:
            return list(connection.execute(
                f"SELECT * FROM work_orders {clause} ORDER BY updated_at DESC, order_id DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 500)), max(0, offset)),
            ))

    def count_work_orders(
        self,
        *,
        keyword: str = "",
        status: str = "",
        node: str = "",
        start_time: str = "",
        end_time: str = "",
        title_keywords: tuple[str, ...] | list[str] = (),
        title_keyword: str = "",
    ) -> int:
        """统计与筛选条件匹配的工单数量。"""
        keywords = tuple(title_keywords) or ((title_keyword,) if title_keyword else ())
        clause, params = self._work_order_filter_clause(
            keyword=keyword,
            status=status,
            node=node,
            start_time=start_time,
            end_time=end_time,
            title_keywords=keywords,
        )
        with self._read() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS count FROM work_orders {clause}", params).fetchone()
            return int(row["count"])

    def get_work_order(
        self,
        order_id: str,
        *,
        title_keywords: tuple[str, ...] | list[str] = (),
        title_keyword: str = "",
    ) -> sqlite3.Row | None:
        """按工单 ID 查询单条记录，可额外按标题关键词过滤。"""
        keywords = tuple(title_keywords) or ((title_keyword,) if title_keyword else ())
        with self._read() as connection:
            if keywords:
                clause = " OR ".join("title LIKE ?" for _ in keywords)
                return connection.execute(
                    f"SELECT * FROM work_orders WHERE order_id = ? AND ({clause})",
                    (order_id, *(f"%{keyword}%" for keyword in keywords)),
                ).fetchone()
            return connection.execute("SELECT * FROM work_orders WHERE order_id = ?", (order_id,)).fetchone()

    def get_work_order_by_task_id(
        self,
        task_id: str,
        *,
        title_keywords: tuple[str, ...] | list[str] = (),
        title_keyword: str = "",
    ) -> sqlite3.Row | None:
        """按平台任务 ID 查询工单，并兼容标题关键词过滤。"""
        keywords = tuple(title_keywords) or ((title_keyword,) if title_keyword else ())
        with self._read() as connection:
            if keywords:
                clause = " OR ".join("title LIKE ?" for _ in keywords)
                return connection.execute(
                    f"SELECT * FROM work_orders WHERE task_id = ? AND ({clause})",
                    (task_id, *(f"%{keyword}%" for keyword in keywords)),
                ).fetchone()
            return connection.execute("SELECT * FROM work_orders WHERE task_id = ?", (task_id,)).fetchone()

    def list_events(self, order_id: str, limit: int = 100) -> list[sqlite3.Row]:
        """按工单读取最近事件，结果倒序且单次最多 500 条。"""
        with self._read() as connection:
            return list(connection.execute(
                "SELECT * FROM work_order_events WHERE order_id = ? ORDER BY id DESC LIMIT ?",
                (order_id, max(1, min(limit, 500))),
            ))

    def dashboard_stats(
        self,
        *,
        title_keywords: tuple[str, ...] | list[str] = (),
        title_keyword: str = "",
    ) -> dict[str, Any]:
        """返回总量、活动量、今日新增量和按当前节点聚合的看板统计。"""
        today = datetime.now().date().isoformat()
        keywords = tuple(title_keywords) or ((title_keyword,) if title_keyword else ())
        clause, params = self._work_order_filter_clause(title_keywords=keywords)
        with self._read() as connection:
            total = connection.execute(f"SELECT COUNT(*) AS count FROM work_orders {clause}", params).fetchone()["count"]
            active_clause = f"{clause} {'AND' if clause else 'WHERE'} status NOT IN (?, ?, ?)"
            active = connection.execute(
                f"SELECT COUNT(*) AS count FROM work_orders {active_clause}", (*params, *_COMPLETED_STATUSES)
            ).fetchone()["count"]
            today_clause = f"{clause} {'AND' if clause else 'WHERE'} created_at LIKE ?"
            today_count = connection.execute(
                f"SELECT COUNT(*) AS count FROM work_orders {today_clause}", (*params, f"{today}%")
            ).fetchone()["count"]
            nodes = connection.execute(
                f"SELECT COALESCE(NULLIF(current_node, ''), '未知') AS node, COUNT(*) AS count FROM work_orders {clause} GROUP BY node ORDER BY count DESC",
                params,
            ).fetchall()
            return {
                "total": int(total),
                "active": int(active),
                "today": int(today_count),
                "nodes": [{"node": row["node"], "count": int(row["count"])} for row in nodes],
            }

    def start_sync_run(self, started_at: str | None = None) -> int:
        """创建同步运行记录并返回自增 ID。"""
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO sync_runs(started_at) VALUES (?)",
                (started_at or datetime.now(timezone.utc).isoformat(),),
            )
            return int(cursor.lastrowid)

    def finish_sync_run(self, run_id: int, *, total: int = 0, added: int = 0, changed: int = 0, error: str | None = None) -> None:
        """以当前 UTC 时间完成同步记录，并保存统计或错误标记。"""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE sync_runs SET finished_at = ?, total = ?, added = ?, changed = ?, error = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), total, added, changed, error, run_id),
            )

    def latest_sync_run(self) -> sqlite3.Row | None:
        """返回最近创建的一次同步运行记录。"""
        with self._read() as connection:
            return connection.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()

    def upsert_saved_account(self, login_id: str, display_name: str = "") -> None:
        """新增或更新保存账号，并刷新其最后使用时间；空账号忽略。"""
        if not login_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO saved_accounts(login_id, display_name, created_at, last_used_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(login_id) DO UPDATE SET
                  display_name=excluded.display_name,
                  last_used_at=excluded.last_used_at
                """ ,
                (login_id, display_name, now, now),
            )

    def list_saved_accounts(self) -> list[sqlite3.Row]:
        """按最近使用时间倒序返回保存账号。"""
        with self._read() as connection:
            return list(connection.execute(
                "SELECT * FROM saved_accounts ORDER BY last_used_at DESC, login_id ASC"
            ))

    def get_saved_account(self, login_id: str) -> sqlite3.Row | None:
        """按登录 ID 返回保存账号，找不到时返回 ``None``。"""
        with self._read() as connection:
            return connection.execute(
                "SELECT * FROM saved_accounts WHERE login_id = ?", (login_id,)
            ).fetchone()

    def remove_saved_account(self, login_id: str) -> None:
        """删除保存账号记录；事务提交后生效。"""
        with self._transaction() as connection:
            connection.execute("DELETE FROM saved_accounts WHERE login_id = ?", (login_id,))

    def update_heartbeat(
        self,
        login_id: str,
        *,
        status: str,
        heartbeat_at: str | None = None,
        error: str | None = None,
        consecutive_failures: int | None = None,
    ) -> None:
        """更新账号心跳状态、错误和连续失败次数。

        ``heartbeat_at`` 未提供时使用当前 UTC 时间；失败次数为 ``None`` 时保留
        原值，允许只更新状态或错误。不存在的账号不会被隐式创建。
        """
        if not login_id:
            return
        now = heartbeat_at or datetime.now(timezone.utc).isoformat()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE saved_accounts
                SET last_heartbeat_at = ?, heartbeat_status = ?,
                    consecutive_failures = COALESCE(?, consecutive_failures), last_error = ?
                WHERE login_id = ?
                """,
                (now, status, consecutive_failures, error, login_id),
            )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """读取应用设置，不存在时返回调用方提供的默认值。"""
        with self._read() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def save_setting(self, key: str, value: str) -> None:
        """以主键 upsert 保存应用设置。"""
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def commit(self) -> None:
        """保留旧桌面调用兼容接口；每个 Web 操作已自行提交事务。"""
        return None

    def close(self) -> None:
        """保留桌面版本兼容接口；Web 版本连接按操作自动关闭。"""
        return None
