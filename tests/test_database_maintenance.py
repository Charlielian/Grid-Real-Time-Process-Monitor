from __future__ import annotations

import logging
import threading
import time

from backend.storage.database import DatabaseMaintenanceStats
from shared.config import AppConfig
from webapp.services.database_maintenance import DatabaseMaintenanceService


class FakeDatabase:
    def __init__(self, stats: DatabaseMaintenanceStats) -> None:
        self.stats = stats
        self.calls = 0
        self.checkpoints = 0
        self.called = threading.Event()

    def cleanup_retention(self, **kwargs):
        self.calls += 1
        self.called.set()
        return self.stats

    def checkpoint_wal(self) -> None:
        self.checkpoints += 1


def test_database_maintenance_run_once_reports_thresholds_and_checkpoints(caplog) -> None:
    database = FakeDatabase(DatabaseMaintenanceStats(
        work_orders_deleted=2,
        events_deleted=3,
        sync_runs_deleted=1,
        database_size_bytes=2,
        wal_size_bytes=3,
    ))
    config = AppConfig(database_max_size_mb=0, wal_max_size_mb=0)
    service = DatabaseMaintenanceService(database, config, logging.getLogger("maintenance-test"))

    with caplog.at_level(logging.WARNING):
        stats = service.run_once()

    assert stats.work_orders_deleted == 2
    assert database.calls == 1
    assert database.checkpoints == 1
    assert "WAL 文件超过阈值" in caplog.text
    assert "数据库文件超过阈值" in caplog.text


def test_database_maintenance_start_and_shutdown_are_idempotent() -> None:
    database = FakeDatabase(DatabaseMaintenanceStats())
    service = DatabaseMaintenanceService(database, AppConfig())

    service.start()
    service.start()
    assert database.called.wait(timeout=1)
    service.shutdown()
    service.shutdown()

    assert database.calls == 1
    assert service._thread is None
