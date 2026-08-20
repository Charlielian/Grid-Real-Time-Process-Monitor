from __future__ import annotations

from pathlib import Path

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
def test_database_upsert_records_changes(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    is_new, events = db.upsert_work_order(make_order())
    assert is_new is True
    assert events[0][0] == "added"
    db.commit()

    is_new, events = db.upsert_work_order(make_order(status="已办结", node="节点B"))
    assert is_new is False
    assert {event[0] for event in events} == {"status_changed", "node_changed"}
    db.commit()
    assert len(db.list_work_orders()) == 1
    db.close()
