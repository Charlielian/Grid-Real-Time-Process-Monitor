from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.storage.database import Database
from backend.sync.service import sync_work_orders
from shared.config import AppConfig
from shared.models import UserInfo, WorkOrder, WorkOrderPage


class FakeClient:
    def query_work_orders(self, user_id: str, **kwargs: object) -> WorkOrderPage:
        return WorkOrderPage(
            items=[
                WorkOrder(order_id="target", title="广州实时优化", raw={}),
                WorkOrder(order_id="other", title="【阳江】实时优化", raw={}),
            ],
            total=2,
            page_index=1,
            page_size=50,
        )


def test_sync_only_persists_target_title(tmp_path: Path) -> None:
    database = Database(tmp_path / "sync.sqlite3")
    summary = sync_work_orders(
        FakeClient(),
        database,
        UserInfo(login_id="user-1"),
        AppConfig(target_title_keywords=("广州",)),
    )

    assert summary.total == 1
    assert database.count_work_orders() == 1
    assert database.get_work_order("target") is not None
    assert database.get_work_order("other") is None
