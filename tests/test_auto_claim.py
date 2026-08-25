"""待领取分页查询与后台自动领取服务的回归测试。

测试用 fake 平台客户端、fake Cookie 和账号存储注入 AutoClaimService，
避免真实访问上游平台。
"""

from __future__ import annotations

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


def _make_service(monkeypatch, *, enabled: bool = True, pending: list[TodoTask] | None = None, cookies_loaded: bool = True):
    config = AppConfig(
        auto_claim_pending_tasks=enabled,
        auto_claim_interval_seconds=5,
    )
    service = AutoClaimService(config, interval_seconds=1000)
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


def test_auto_claim_shutdown_is_idempotent() -> None:
    service = AutoClaimService(AppConfig(auto_claim_pending_tasks=True), interval_seconds=1000)
    service.shutdown()
    service.shutdown()
    assert service._closed