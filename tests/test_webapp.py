from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from shared.config import AppConfig
from shared.models import UserInfo
from webapp import create_app
from webapp.routes.api import _parse_bool


@pytest.fixture()
def app():
    with TemporaryDirectory() as directory:
        application = create_app({
            "TESTING": True,
            "DATA_DIR": directory,
            "SECRET_KEY": "test-secret",
            "APP_CONFIG": AppConfig(),
        })
        yield application
        application.extensions["shutdown"]()
        for handler in application.extensions["logger"].handlers:
            handler.flush()
            if hasattr(handler, "baseFilename"):
                handler.close()
        application.extensions["logger"].handlers.clear()


def test_parse_bool_accepts_supported_values() -> None:
    assert _parse_bool(True, "auto_sync") is True
    assert _parse_bool(False, "auto_sync") is False
    assert _parse_bool(" TRUE ", "auto_sync") is True
    assert _parse_bool("false", "auto_sync") is False
    assert _parse_bool(1, "auto_sync") is True
    assert _parse_bool(0, "auto_sync") is False


@pytest.mark.parametrize("value", [None, "yes", "", 2, [], {}])
def test_parse_bool_rejects_unsupported_values(value) -> None:
    with pytest.raises(ValueError, match="auto_sync"):
        _parse_bool(value, "auto_sync")


def test_settings_api_parses_boolean_string(app, monkeypatch) -> None:
    context = SimpleNamespace(context_id="context-1")
    user = UserInfo(login_id="user-1", display_name="测试用户")
    monkeypatch.setattr(
        app.extensions["web_auth"],
        "require_user",
        lambda _context_id: (context, user),
    )
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["auth_context_id"] = context.context_id
        browser_session["csrf_token"] = "expected"

    response = client.put(
        "/api/v1/settings",
        json={"auto_sync": "false"},
        headers={"X-CSRF-Token": "expected"},
    )

    assert response.status_code == 200
    assert response.get_json()["auto_sync"] is False
    assert app.extensions["app_config"].auto_sync is False


def test_settings_api_rejects_invalid_boolean(app, monkeypatch) -> None:
    context = SimpleNamespace(context_id="context-1")
    user = UserInfo(login_id="user-1", display_name="测试用户")
    monkeypatch.setattr(
        app.extensions["web_auth"],
        "require_user",
        lambda _context_id: (context, user),
    )
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["auth_context_id"] = context.context_id
        browser_session["csrf_token"] = "expected"

    response = client.put(
        "/api/v1/settings",
        json={"auto_sync": "not-a-boolean"},
        headers={"X-CSRF-Token": "expected"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_settings"
def test_web_pages_and_unauthorized_api(app) -> None:
    client = app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/login").status_code == 200
    assert client.get("/api/v1/session").get_json() == {
        "authenticated": False,
        "user": None,
    }
    assert client.get("/api/v1/orders").status_code == 401
    assert client.get("/dashboard").status_code == 302


def test_csrf_token_is_required_for_post(app) -> None:
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["auth_context_id"] = "unknown"
        browser_session["csrf_token"] = "expected"
    response = client.post("/auth/captcha/verify", json={
        "username": "demo",
        "password": "secret",
        "captcha": "1234",
    })
    assert response.status_code == 400
    assert "CSRF" in response.get_data(as_text=True)


def test_title_scope_and_scroll_layout(app) -> None:
    from shared.models import WorkOrder
    database = app.extensions["database"]
    database.upsert_work_order(WorkOrder(order_id="target", title="阳江目标工单", raw={}))
    database.upsert_work_order(WorkOrder(order_id="other", title="【广州】其他工单", raw={}))
    with app.test_request_context():
        from flask import session
        session["auth_context_id"] = "not-authenticated"
    template = (app.root_path + "/templates/orders.html")
    css = (app.root_path + "/static/css/app.css")
    assert "orders-table" in open(template, encoding="utf-8").read()
    template_text = open(template, encoding="utf-8").read()
    assert "data-poll-interval" in template_text
    assert "new-orders-bubble" in template_text
    login_template = open(app.root_path + "/templates/login.html", encoding="utf-8").read()
    assert "使用已保存 Cookies 登录" in login_template
    orders_js = open(app.root_path + "/static/js/orders.js", encoding="utf-8").read()
    assert "summary?.added" in orders_js
    assert "window.location.reload" in orders_js
    css_text = open(css, encoding="utf-8").read()
    assert "overflow: auto" in css_text
    assert "position: fixed" in css_text
    assert "new-orders-bubble" in css_text


def test_frontend_uses_shared_api_and_handles_failures(app) -> None:
    static = app.root_path + "/static/js/"
    api = open(static + "api.js", encoding="utf-8").read()
    assert "AbortController" in api
    assert "response.status === 401" in api
    assert "2 ** attempt" in api
    assert "Accept" in api
    for name in ("login.js", "dashboard.js", "orders.js", "pending_tasks.js"):
        assert "fetch(" not in open(static + name, encoding="utf-8").read()
    base = open(app.root_path + "/templates/base.html", encoding="utf-8").read()
    assert "js/api.js" in base


def test_pending_page_requires_auth_and_has_navigation(app) -> None:
    client = app.test_client()
    assert client.get("/pending-tasks").status_code == 302
    template = open(app.root_path + "/templates/base.html", encoding="utf-8").read()
    assert "待领取" in template
    pending = open(app.root_path + "/templates/pending_tasks.html", encoding="utf-8").read()
    assert "人工领取" in pending


def test_pending_api_requires_auth_and_csrf(app) -> None:
    client = app.test_client()
    assert client.get("/api/v1/pending-tasks").status_code == 401
    with client.session_transaction() as browser_session:
        browser_session["auth_context_id"] = "unknown"
        browser_session["csrf_token"] = "expected"
    assert client.post("/api/v1/pending-tasks/claim", json={"task_ids": ["task-1"]}).status_code == 401


def test_database_api_returns_explicit_fields_only(app) -> None:
    from shared.models import WorkOrder
    database = app.extensions["database"]
    database.upsert_work_order(WorkOrder(
        order_id="order-1",
        number="WO-1",
        title="脱敏工单",
        status="处理中",
        current_node="节点A",
        assignee="用户A",
        created_at="2026-08-20T10:00:00+00:00",
        raw={"internal": "must-not-be-exposed"},
    ))
    with app.test_request_context():
        from flask import session
        session["auth_context_id"] = "not-authenticated"
    assert database.get_work_order("order-1")["order_id"] == "order-1"
