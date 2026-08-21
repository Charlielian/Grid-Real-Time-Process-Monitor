from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from backend.storage.database import Database, DatabaseMaintenanceStats
from shared.config import AppConfig


class DatabaseMaintenanceService:
    """Periodically removes retained history and reports SQLite file growth."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        logger: logging.Logger | None = None,
        *,
        autostart: bool = False,
    ) -> None:
        self.database = database
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._closed = False
        if autostart:
            self.start()

    def _cutoff(self, days: int) -> str | None:
        if days <= 0:
            return None
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def run_once(self) -> DatabaseMaintenanceStats:
        stats = self.database.cleanup_retention(
            work_order_cutoff=self._cutoff(self.config.work_order_retention_days),
            event_cutoff=self._cutoff(self.config.work_order_event_retention_days),
            sync_run_cutoff=self._cutoff(self.config.sync_run_retention_days),
            batch_size=self.config.database_cleanup_batch_size,
        )
        if stats.wal_size_bytes > self.config.wal_max_size_mb * 1024 * 1024:
            self.logger.warning(
                "SQLite WAL 文件超过阈值: size_bytes=%d threshold_mb=%d",
                stats.wal_size_bytes,
                self.config.wal_max_size_mb,
            )
            self.database.checkpoint_wal()
        if stats.database_size_bytes > self.config.database_max_size_mb * 1024 * 1024:
            self.logger.warning(
                "SQLite 数据库文件超过阈值: size_bytes=%d threshold_mb=%d",
                stats.database_size_bytes,
                self.config.database_max_size_mb,
            )
        if stats.work_orders_deleted or stats.events_deleted or stats.sync_runs_deleted:
            self.logger.info(
                "SQLite 历史清理完成: work_orders=%d events=%d sync_runs=%d",
                stats.work_orders_deleted,
                stats.events_deleted,
                stats.sync_runs_deleted,
            )
        return stats

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    self.logger.exception("SQLite 周期维护失败")
                if self._stop.wait(self.config.database_cleanup_interval_seconds):
                    break
        finally:
            with self._lock:
                self._thread = None

    def start(self) -> None:
        with self._lock:
            if self._closed or (self._thread and self._thread.is_alive()):
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="database-maintenance",
                daemon=True,
            )
            self._thread.start()

    def shutdown(self, timeout: float | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
