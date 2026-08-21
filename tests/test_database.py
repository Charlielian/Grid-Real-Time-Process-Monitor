from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend.storage.database import Database
from shared.models import WorkOrder


def make_order(status: str = "处理中", node: str = "节点A", assignee: str = "张三", *, order_id: str = "id-1", title: str = "测试工单") -> WorkOrder:
    return WorkOrder(
        order_id=order_id, number=f"WO-{order_id}", title=title, status=status,
        current_node=node, assignee=assignee, created_at="2026-08-20T10:00:00",
        raw={"id": order_id},
    )


def test_database_filters_by_title_keyword(tmp_path: Path) -> None:
    db = Database(tmp_path / "filter.sqlite3")
    db.upsert_work_order(make_order(order_id="yangjiang", title="阳江微网格优化"))
    db.upsert_work_order(make_order(order_id="other", title="【广州】微网格优化"))

    db.upsert_work_order(make_order(order_id="jiangmen", title="江门微网格优化"))

    assert [row["order_id"] for row in db.list_work_orders(title_keywords=("阳江", "江门"))] == ["jiangmen", "yangjiang"]
    assert db.count_work_orders(title_keywords=("阳江", "江门")) == 2
    assert db.get_work_order("other", title_keywords=("阳江", "江门")) is None
    assert db.dashboard_stats(title_keywords=("阳江", "江门"))["total"] == 2
def test_database_upsert_orders_batches_in_one_transaction(tmp_path: Path) -> None:
    db = Database(tmp_path / "batch.sqlite3")
    orders = [make_order(order_id=f"id-{index}") for index in range(3)]

    with patch.object(db, "_connect", wraps=db._connect) as connect:
        total, added, changed = db.upsert_orders(orders)

    assert connect.call_count == 1

    assert (total, added, changed) == (3, 3, 0)
    assert db.count_work_orders() == 3

    updated = [make_order(order_id=f"id-{index}", status="已办结") for index in range(3)]
    assert db.upsert_orders(updated) == (3, 0, 3)
    assert {row["event_type"] for row in db.list_events("id-0")} == {"added", "status_changed"}


def test_database_upsert_orders_rolls_back_failed_batch(tmp_path: Path) -> None:
    db = Database(tmp_path / "batch-rollback.sqlite3")
    valid = make_order(order_id="valid")
    invalid = make_order(order_id="invalid")
    object.__setattr__(invalid, "raw", {"not": object()})

    import pytest
    with pytest.raises(TypeError):
        db.upsert_orders([valid, invalid])

    assert db.count_work_orders() == 0


def test_database_cleanup_retention_removes_history_but_keeps_active_rows(tmp_path: Path) -> None:
    db = Database(tmp_path / "cleanup.sqlite3")
    db.upsert_work_order(make_order(order_id="old", title="旧工单"))
    db.upsert_work_order(make_order(order_id="new", title="新工单"))
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=10)).isoformat()
    cutoff = (now - timedelta(days=1)).isoformat()
    with db._transaction() as connection:
        connection.execute("UPDATE work_orders SET updated_at = ? WHERE order_id = 'old'", (old,))
        connection.execute("UPDATE work_order_events SET created_at = ? WHERE order_id = 'old'", (old,))
        connection.execute("INSERT INTO sync_runs(started_at, finished_at) VALUES (?, ?)", (old, old))
        connection.execute("INSERT INTO sync_runs(started_at) VALUES (?)", (old,))

    stats = db.cleanup_retention(
        work_order_cutoff=cutoff,
        event_cutoff=cutoff,
        sync_run_cutoff=cutoff,
        batch_size=10,
    )

    assert stats.work_orders_deleted == 1
    assert stats.events_deleted >= 1
    assert stats.sync_runs_deleted == 1
    assert db.get_work_order("old") is None
    assert db.get_work_order("new") is not None
    assert db.latest_sync_run()["finished_at"] is None


def test_database_cleanup_zero_cutoffs_preserve_records(tmp_path: Path) -> None:
    db = Database(tmp_path / "retain.sqlite3")
    db.upsert_work_order(make_order())
    assert db.cleanup_retention(batch_size=1).work_orders_deleted == 0
    assert db.get_work_order("id-1") is not None
    assert db.file_sizes()[0] > 0
