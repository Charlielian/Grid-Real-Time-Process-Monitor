from __future__ import annotations

import logging

import pytest

from backend.platform.client import PlatformBusinessError, PlatformClient
from shared.config import AppConfig


class FakeResponse:
    def __init__(self, payload, *, status_code=200, content_type="application/json", url="https://example.test/api"):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.url = url
        self.text = payload if isinstance(payload, str) else ""

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def make_client(response):
    session = FakeSession(response)
    return PlatformClient(AppConfig(target_process_key="custom-key"), session, logging.getLogger("test")), session


def test_platform_client_distinguishes_login_html_from_proxy_html():
    from backend.auth.cas_client import SessionExpired
    from backend.platform.client import PlatformError

    login_client, _ = make_client(FakeResponse('<form id="fm1">login</form>', content_type="text/html"))
    with pytest.raises(SessionExpired):
        login_client.find_process()

    proxy_client, _ = make_client(FakeResponse("Bad Gateway", content_type="text/html"))
    with pytest.raises(PlatformError):
        proxy_client.find_process()


    client, session = make_client(FakeResponse({
        "objects": [{
            "id": "order-1",
            "code": "WO-1",
            "title": "阳江工单",
            "state": "处理中",
            "currentNodeName": "方案制定",
            "assignee": "user-1",
            "createTime": "2026-08-20 17:00:00",
            "dueDate": "2026-08-21 17:00:00",
            "processInstanceId": "instance-1",
            "processDefinitionVersion": "3",
            "taskId": "task-1",
        }],
        "totalObjects": 1,
    }))

    result = client.query_work_orders(
        "user-1",
        page_index=1,
        page_size=50,
        start_time="2026-08-20 00:00:00",
        end_time="2026-08-20 23:59:59",
    )

    assert result.total == 1
    order = result.items[0]
    assert order.order_id == "order-1"
    assert order.number == "WO-1"
    assert order.title == "阳江工单"
    assert order.status == "处理中"
    assert order.current_node == "方案制定"
    assert order.created_at == "2026-08-20 17:00:00"
    assert order.due_at == "2026-08-21 17:00:00"
    assert order.process_instance_id == "instance-1"
    assert order.task_id == "task-1"
    assert order.process_version == "3"
    assert session.calls[0][2]["json"] == {
        "userId": "user-1",
        "woStatus": 0,
        "key": ["custom-key"],
        "createTimeStart": "2026-08-20 00:00:00",
        "createTimeEnd": "2026-08-20 23:59:59",
        "pageIndex": 1,
        "pageSize": 50,
    }


def test_query_work_orders_accepts_nested_data_and_invalid_total():
    client, _ = make_client(FakeResponse({
        "data": {"list": [{"id": "order-2", "code": "WO-2"}]},
        "total": "not-a-number",
    }))

    result = client.query_work_orders("user-1", 1, 50, "start", "end")

    assert result.total == 1
    assert result.items[0].order_id == "order-2"
    assert result.items[0].number == "WO-2"


def test_query_todo_tasks_maps_har_shape():
    client, session = make_client(FakeResponse({
        "data": [{
            "taskId": "task-1",
            "nodeName": "方案制定",
            "assignee": "",
            "processInstanceId": "instance-1",
            "processDefinitionKey": "custom-key",
            "woCommon": {
                "wo_id": "order-1",
                "wo_code": "WO-1",
                "wo_title": "阳江工单",
                "wo_createTime": "2026-08-20 17:00:00",
                "wo_dueDate": "2026-08-21 17:00:00",
            },
        }],
        "total": 1,
    }))

    result = client.query_todo_tasks("user-1", page_index=2, page_size=10)

    assert result.total == 1
    assert result.items[0].task_id == "task-1"
    assert result.items[0].order_id == "order-1"
    assert result.items[0].number == "WO-1"
    assert result.items[0].title == "阳江工单"
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/bpmn/runtime/task/work-order/user-1/todo")
    assert kwargs["json"] == {"key": ["custom-key"], "assigned": False, "pageIndex": 2, "pageSize": 10}


def test_assign_tasks_sends_expected_payload():
    client, session = make_client(FakeResponse({"data": "", "message": "操作成功", "stat": "1"}))

    result = client.assign_tasks("user-1", ["task-1", "task-2"])

    assert result["stat"] == "1"
    assert session.calls[0][2]["json"] == {"assignee": "user-1", "tasks": ["task-1", "task-2"]}
    assert session.calls[0][1].endswith("/bpmn/task/assignee/batch")


def test_query_todo_tasks_rejects_invalid_response():
    client, _ = make_client(FakeResponse([]))

    with pytest.raises(Exception, match="待领取任务响应格式无效"):
        client.query_todo_tasks("user-1")


def test_assign_tasks_rejects_business_failure():
    client, _ = make_client(FakeResponse({"message": "任务已被领取", "stat": "0"}))

    with pytest.raises(PlatformBusinessError, match="任务已被领取"):
        client.assign_tasks("user-1", ["task-1"])


def test_assign_tasks_rejects_empty_tasks():
    client, _ = make_client(FakeResponse({"stat": "1"}))

    with pytest.raises(ValueError):
        client.assign_tasks("user-1", [])
