from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

import pytest

from backend.storage.database import Database
from backend.sync.service import sync_work_orders
from shared.config import AppConfig
from shared.models import UserInfo, WorkOrder, WorkOrderPage
from webapp.services.sync import SyncJobManager, SyncJobSnapshot


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


class PagedFakeClient:
    def __init__(self, pages: list[list[WorkOrder]], page_size: int = 2) -> None:
        self.pages = pages
        self.page_size = page_size
        self.calls: list[int] = []

    def query_work_orders(self, user_id: str, **kwargs: object) -> WorkOrderPage:
        page_index = int(kwargs["page_index"])
        self.calls.append(page_index)
        items = self.pages[page_index - 1] if page_index <= len(self.pages) else []
        return WorkOrderPage(
            items=items,
            total=sum(len(page) for page in self.pages),
            page_index=page_index,
            page_size=self.page_size,
        )


class ManagerFakeClient:
    def __init__(self, started: Event | None = None, release: Event | None = None) -> None:
        self.started = started
        self.release = release
        self.calls = 0

    def query_work_orders(self, user_id: str, **kwargs: object) -> WorkOrderPage:
        self.calls += 1
        if self.started:
            self.started.set()
        if self.release:
            self.release.wait(timeout=2)
        return WorkOrderPage(
            items=[WorkOrder(order_id="target", title="广州实时优化", raw={})],
            total=1,
            page_index=1,
            page_size=50,
        )


def _wait_for_terminal(manager: SyncJobManager, job_id: str):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = manager.get(job_id)
        assert snapshot is not None
        if snapshot.status not in {"queued", "running"}:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("同步任务未在期限内结束")


def test_sync_persists_all_target_process_orders(tmp_path: Path) -> None:
    database = Database(tmp_path / "sync.sqlite3")
    summary = sync_work_orders(
        FakeClient(),
        database,
        UserInfo(login_id="user-1"),
        AppConfig(target_title_keywords=("广州",)),
    )

    assert summary.total == 2
    assert database.count_work_orders() == 2
    assert database.get_work_order("target") is not None
    assert database.get_work_order("other") is not None


def test_sync_uses_batch_upserts_across_pages(tmp_path: Path) -> None:
    client = PagedFakeClient([
        [WorkOrder(order_id="one", title="阳江一"), WorkOrder(order_id="other", title="广州一")],
        [WorkOrder(order_id="two", title="阳江二"), WorkOrder(order_id="three", title="阳江三")],
    ])
    database = Database(tmp_path / "paged.sqlite3")
    calls: list[int] = []
    original = database.upsert_orders

    def tracked(orders: list[WorkOrder]) -> tuple[int, int, int]:
        calls.append(len(orders))
        return original(orders)

    database.upsert_orders = tracked  # type: ignore[method-assign]
    summary = sync_work_orders(
        client,
        database,
        UserInfo(login_id="user-1"),
        AppConfig(target_title_keywords=("阳江",), page_size=10),
    )

    assert client.calls == [1, 2]
    assert calls == [2, 2]
    assert (summary.total, summary.added, summary.changed) == (4, 4, 0)
    assert database.count_work_orders() == 4
    assert database.latest_sync_run()["error"] is None


def test_sync_batch_performance_smoke(tmp_path: Path, capsys) -> None:
    orders = [WorkOrder(order_id=f"order-{index}", title="阳江批量工单") for index in range(1000)]
    client = PagedFakeClient([orders], page_size=1000)
    database = Database(tmp_path / "performance.sqlite3")
    started = time.perf_counter()

    summary = sync_work_orders(
        client,
        database,
        UserInfo(login_id="user-1"),
        AppConfig(target_title_keywords=("阳江",), page_size=10),
    )

    elapsed = time.perf_counter() - started
    print(f"同步性能基准: {summary.total} 条, {elapsed:.4f}s, {summary.total / max(elapsed, 1e-9):.0f} 条/秒")
    assert summary.total == 1000
    assert database.count_work_orders() == 1000
    assert "同步性能基准" in capsys.readouterr().out


    manager = SyncJobManager(
        Database(tmp_path / "manager.sqlite3"),
        SimpleNamespace(warning=lambda *args: None),
        cleanup_interval_seconds=10,
    )
    client = ManagerFakeClient()
    config = AppConfig(target_title_keywords=("广州",))
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            snapshots = list(pool.map(
                lambda _: manager.submit("context-1", client, UserInfo(login_id="u"), config),
                range(8),
            ))
        assert len({snapshot.job_id for snapshot in snapshots}) == 1
        assert all(isinstance(snapshot, SyncJobSnapshot) for snapshot in snapshots)
        terminal = _wait_for_terminal(manager, snapshots[0].job_id)
        assert terminal.status == "succeeded"
        assert terminal.summary is not None
        assert client.calls == 1
    finally:
        manager.shutdown()


def test_manager_snapshot_is_immutable_and_cancelled_job_not_succeeded(tmp_path: Path) -> None:
    started = Event()
    release = Event()
    manager = SyncJobManager(
        Database(tmp_path / "cancel.sqlite3"),
        SimpleNamespace(warning=lambda *args: None),
        cleanup_interval_seconds=10,
    )
    client = ManagerFakeClient(started, release)
    try:
        job = manager.submit(
            "context-1", client, UserInfo(login_id="u"),
            AppConfig(target_title_keywords=("广州",)),
        )
        assert isinstance(job, SyncJobSnapshot)
        assert started.wait(timeout=2)
        assert manager.cancel(job.job_id)
        release.set()
        terminal = _wait_for_terminal(manager, job.job_id)
        assert terminal.status == "cancelled"
        assert terminal.summary is None
        with pytest.raises(AttributeError):
            terminal.status = "succeeded"
    finally:
        release.set()
        manager.shutdown()
