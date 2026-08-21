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
    work_orders_deleted: int = 0
    events_deleted: int = 0
    sync_runs_deleted: int = 0
    database_size_bytes: int = 0
    wal_size_bytes: int = 0


class Database:
    """SQLite repository with short-lived connections per operation."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
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
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
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
        database_size = self.path.stat().st_size if self.path.exists() else 0
        wal_path = Path(f"{self.path}-wal")
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
        return database_size, wal_size

    def checkpoint_wal(self) -> None:
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
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO sync_runs(started_at) VALUES (?)",
                (started_at or datetime.now(timezone.utc).isoformat(),),
            )
            return int(cursor.lastrowid)

    def finish_sync_run(self, run_id: int, *, total: int = 0, added: int = 0, changed: int = 0, error: str | None = None) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE sync_runs SET finished_at = ?, total = ?, added = ?, changed = ?, error = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), total, added, changed, error, run_id),
            )

    def latest_sync_run(self) -> sqlite3.Row | None:
        with self._read() as connection:
            return connection.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()

    def upsert_saved_account(self, login_id: str, display_name: str = "") -> None:
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
        with self._read() as connection:
            return list(connection.execute(
                "SELECT * FROM saved_accounts ORDER BY last_used_at DESC, login_id ASC"
            ))

    def get_saved_account(self, login_id: str) -> sqlite3.Row | None:
        with self._read() as connection:
            return connection.execute(
                "SELECT * FROM saved_accounts WHERE login_id = ?", (login_id,)
            ).fetchone()

    def remove_saved_account(self, login_id: str) -> None:
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
        with self._read() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def save_setting(self, key: str, value: str) -> None:
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
