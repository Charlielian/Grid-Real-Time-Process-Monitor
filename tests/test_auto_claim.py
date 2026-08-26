"""待领取分页查询与后台自动领取服务的回归测试。

测试用 fake 平台客户端、fake Cookie 和账号存储注入 AutoClaimService，
避免真实访问上游平台。
"""

from __future__ import annotations

import json
import logging
import threading

import pytest

from shared.config import AppConfig
from shared.models import TodoTask, TodoTaskPage
from webapp.services.auto_claim import AutoClaimService
from webapp.services.pending_tasks import claimable_tasks, query_all_todo_tasks


def _task(task_id: str, title: str) -> TodoTask:
    return TodoTask(task_id=task_id, title=title, number=task_id)


class FakeClient:
    """按页返回预置待领取任务的 fake 平台客户端。"""

    def __init__(self, pages: list[list[TodoTask]]) -> None:
        self.pages = pages
        self.assign_calls: list[tuple[str, list[str]]] = []

    def query_todo_tasks(self, login_id: str, *, assigned: bool, page_index: int, page_size: int) -> TodoTaskPage:
        index = page_index - 1
        if index >= len(self.pages):
            return TodoTaskPage(items=[], total=0, page_index=page_index, page_size=page_size)
        items = self.pages[index]
        total = sum(len(page) for page in self.pages)
        return TodoTaskPage(items=items, total=total, page_index=page_index, page_size=page_size)

    def assign_tasks(self, assignee: str, task_ids: list[str]) -> dict[str, object]:
        self.assign_calls.append((assignee, task_ids))
        return {"stat": "1"}


def test_query_all_todo_tasks_merges_pages() -> None:
    client = FakeClient([
        [_task("t1", "阳江 A"), _task("t2", "广州 B")],
        [_task("t3", "阳江 C")],
    ])
    result = query_all_todo_tasks(client, "user1", assigned=False, config=AppConfig(), cities=())
    assert [task.task_id for task in result] == ["t1", "t2", "t3"]


def test_query_all_todo_tasks_filters_cities() -> None:
    client = FakeClient([[_task("t1", "阳江 A"), _task("t2", "广州 B")]])
    result = query_all_todo_tasks(client, "user1", assigned=False, config=AppConfig(), cities=("阳江",))
    assert [task.task_id for task in result] == ["t1"]


def test_claimable_tasks_only_matches_keywords() -> None:
    pending = [_task("t1", "阳江 A"), _task("t2", "广州 B"), _task("t3", "阳江优化")]
    result = claimable_tasks(pending, ("阳江",))
    assert [task.task_id for task in result] == ["t1", "t3"]
    assert claimable_tasks(pending, ()) == []


class _FakeCookies:
    def __init__(self, loaded: bool = True) -> None:
        self.loaded = loaded

    def load(self, login_id: str, session: object) -> bool:
        return self.loaded

    def save(self, login_id: str, session: object) -> bool:
        return True


class _FakeAccounts:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records

    def list(self) -> list[dict[str, object]]:
        return self.records


class _FakeCasClient:
    def __init__(self) -> None:
        self.checked: list[str] = []

    def check_session(self, expected_login_id: str | None = None) -> object:
        self.checked.append(expected_login_id or "")
        return object()


class _FakePlatformClient:
    def __init__(self, pending: list[TodoTask]) -> None:
        self.pending = pending
        self.assign_calls: list[tuple[str, list[str]]] = []

    def query_todo_tasks(self, login_id: str, *, assigned: bool, page_index: int, page_size: int) -> TodoTaskPage:
        if page_index == 1:
            return TodoTaskPage(items=self.pending, total=len(self.pending), page_index=1, page_size=page_size)
        return TodoTaskPage(items=[], total=len(self.pending), page_index=page_index, page_size=page_size)

    def assign_tasks(self, assignee: str, task_ids: list[str]) -> dict[str, object]:
        self.assign_calls.append((assignee, task_ids))
        return {"stat": "1"}


def _make_service(monkeypatch, *, enabled: bool = True, pending: list[TodoTask] | None = None, cookies_loaded: bool = True, data_dir=None):
    config = AppConfig(
        auto_claim_pending_tasks=enabled,
        auto_claim_interval_seconds=5,
    )
    service = AutoClaimService(config, interval_seconds=1000, data_dir=data_dir)
    service.cookies = _FakeCookies(loaded=cookies_loaded)
    service.accounts = _FakeAccounts([
        {"login_id": "account-1", "heartbeat_status": "unknown"},
    ])
    fake_cas = _FakeCasClient()
    fake_platform = _FakePlatformClient(pending or [])

    class _FakeSessionFactory:
        def __init__(self, *args, **kwargs):
            pass

        def create(self):
            return object()

    monkeypatch.setattr("webapp.services.auto_claim.SessionFactory", _FakeSessionFactory)
    monkeypatch.setattr("webapp.services.auto_claim.CasClient", lambda config, session, logger: fake_cas)

    class _FakePlatformClientFactory:
        instance = fake_platform

        def __call__(self, config, session, logger):
            return self.instance

    monkeypatch.setattr("webapp.services.auto_claim.PlatformClient", _FakePlatformClientFactory())
    return service, fake_cas, fake_platform


def test_auto_claim_claims_only_keyword_tasks(monkeypatch) -> None:
    pending = [_task("t1", "阳江 A"), _task("t2", "广州 B"), _task("t3", "阳江优化")]
    service, fake_cas, fake_platform = _make_service(monkeypatch, pending=pending)

    service._claim_for_account("account-1")

    assert fake_cas.checked == ["account-1"]
    # 只领取标题含“阳江”的任务
    assert fake_platform.assign_calls == [("account-1", ["t1", "t3"])]


def test_auto_claim_skips_account_without_cookies(monkeypatch) -> None:
    service, fake_cas, fake_platform = _make_service(monkeypatch, cookies_loaded=False)
    service._claim_for_account("account-1")
    assert fake_cas.checked == []
    assert fake_platform.assign_calls == []


def test_auto_claim_cycle_skips_expired_accounts(monkeypatch) -> None:
    service, fake_cas, fake_platform = _make_service(monkeypatch)
    service.accounts = _FakeAccounts([
        {"login_id": "account-1", "heartbeat_status": "expired"},
    ])
    service._run_cycle()
    assert fake_cas.checked == []
    assert fake_platform.assign_calls == []


def test_auto_claim_disabled_does_nothing(monkeypatch) -> None:
    service, fake_cas, fake_platform = _make_service(monkeypatch, enabled=False, pending=[_task("t1", "阳江 A")])
    service._run_cycle()
    assert fake_cas.checked == []
    assert fake_platform.assign_calls == []


def test_auto_claim_records_statistics(monkeypatch, tmp_path) -> None:
    pending = [_task("t1", "阳江 A"), _task("t2", "广州 B"), _task("t3", "阳江优化")]
    service, _fake_cas, _fake_platform = _make_service(monkeypatch, pending=pending, data_dir=tmp_path)

    service._claim_for_account("account-1")
    service._claim_for_account("account-1")

    stats = service.stats()
    assert stats["total_claimed"] == 4
    assert stats["session_claimed"] == 4
    assert stats["per_account"]["account-1"] == 4
    assert stats["last_claim"] is not None
    assert len(stats["history"]) == 2

    stats_file = tmp_path / "auto_claim_stats.json"
    assert stats_file.exists()
    saved = json.loads(stats_file.read_text(encoding="utf-8"))
    assert saved["total_claimed"] == 4
    # 文件里不写本次启动计数（跨重启不可靠）
    assert "session_claimed" not in saved


def test_auto_claim_stats_survives_restart(tmp_path) -> None:
    config = AppConfig(auto_claim_pending_tasks=True, auto_claim_interval_seconds=5)
    first = AutoClaimService(config, interval_seconds=1000, data_dir=tmp_path)
    first._record_claim("account-1", ["t1", "t3"])

    restored = AutoClaimService(config, interval_seconds=1000, data_dir=tmp_path)
    assert restored.stats()["total_claimed"] == 0
    restored.start()
    restored.shutdown()
    stats = restored.stats()
    assert stats["total_claimed"] == 2
    assert stats["session_claimed"] == 0
    assert stats["per_account"]["account-1"] == 2
    assert stats["history"][0]["time"]


def test_auto_claim_records_claimed_task_details(monkeypatch, tmp_path) -> None:
    pending = [_task("t1", "阳江 A"), _task("t2", "广州 B")]
    service, _fake_cas, _fake_platform = _make_service(monkeypatch, pending=pending, data_dir=tmp_path)

    service._claim_for_account("account-1")

    stats = service.stats()
    # 明细只记录实际领取的（标题含关键词）任务
    assert [t["number"] for t in stats["recent_tasks"]] == ["t1"]
    assert [t["title"] for t in stats["recent_tasks"]] == ["阳江 A"]
    assert stats["recent_tasks"][0]["login_id"] == "account-1"

    saved = json.loads((tmp_path / "auto_claim_stats.json").read_text(encoding="utf-8"))
    assert len(saved["recent_tasks"]) == 1
    assert saved["recent_tasks"][0]["number"] == "t1"


def test_auto_claim_stats_file_unwritable_still_works(monkeypatch, tmp_path) -> None:
    pending = [_task("t1", "阳江 A")]
    service, _fake_cas, _fake_platform = _make_service(monkeypatch, pending=pending, data_dir=tmp_path)

    def fail_write(_text, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("webapp.services.auto_claim.Path.write_text", fail_write)
    service._claim_for_account("account-1")

    assert service.stats()["total_claimed"] == 1


def test_auto_claim_cycle_logs_scan_results(monkeypatch, caplog) -> None:
    service, _fake_cas, _fake_platform = _make_service(
        monkeypatch,
        pending=[_task("t1", "广州 A")],
    )

    with caplog.at_level(logging.INFO, logger=service.logger.name):
        service._run_cycle()

    messages = [record.getMessage() for record in caplog.records]
    assert any("开始扫描" in message for message in messages)
    assert any("账号 account-1 扫描完成，待领取=1，可领取=0" in message for message in messages)
    assert any("扫描完成，账号总数=1，实际扫描=1，跳过=0" in message for message in messages)


def test_auto_claim_start_scans_immediately(monkeypatch) -> None:
    service = AutoClaimService(AppConfig(auto_claim_pending_tasks=True), interval_seconds=3600)
    first_cycle = threading.Event()
    cycle_count = 0

    def fake_cycle() -> None:
        nonlocal cycle_count
        cycle_count += 1
        first_cycle.set()

    monkeypatch.setattr(service, "_run_cycle", fake_cycle)
    service.start()
    try:
        assert first_cycle.wait(timeout=1), "自动领取服务启动后未立即扫描"
        assert cycle_count == 1
    finally:
        service.shutdown()


def test_auto_claim_shutdown_is_idempotent() -> None:
    service = AutoClaimService(AppConfig(auto_claim_pending_tasks=True), interval_seconds=1000)
    service.shutdown()
    service.shutdown()
    assert service._closed