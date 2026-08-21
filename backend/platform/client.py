from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import requests

from backend.auth.cas_client import SessionExpired
from shared.config import AppConfig
from shared.models import TodoTask, TodoTaskPage, WorkOrder, WorkOrderPage


class PlatformError(RuntimeError):
    pass


class PlatformBusinessError(PlatformError):
    pass


class PlatformClient:
    def __init__(self, config: AppConfig, session: requests.Session, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.session = session
        self.logger = logger or logging.getLogger(__name__)
        self.timeout = (10, 45)
        self.engine_base = f"{config.base_url.rstrip('/')}/pro-wfm-engine-extend-fak"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.session.request(method, urljoin(self.engine_base + "/", path.lstrip("/")), timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise PlatformError("业务接口请求失败") from exc
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code in (401, 403) or "/cas/login" in response.url:
            raise SessionExpired("业务会话已失效")
        if response.status_code >= 400:
            raise PlatformError(f"业务接口返回 HTTP {response.status_code}")
        if "text/html" in content_type and "json" not in content_type:
            body = str(getattr(response, "text", "")).lower()
            if any(marker in body for marker in ("/cas/login", "j_username", "j_password", 'id="fm1"')):
                raise SessionExpired("业务接口返回登录页")
            raise PlatformError("业务接口返回非 JSON 页面")
        try:
            return response.json()
        except ValueError as exc:
            raise PlatformError("业务接口返回格式无效") from exc

    def find_process(self) -> dict[str, Any]:
        data = self._request("GET", "/manage/category/tree?createOrder=false&includeDefinition=false")
        matches: list[dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                title = str(value.get("title", value.get("name", "")))
                key = str(value.get("key", value.get("id", "")))
                if title == self.config.target_process_title and key == self.config.target_process_key:
                    matches.append(value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(data)
        if not matches:
            raise PlatformError(f"未找到目标流程: {self.config.target_process_title}")
        return matches[0]

    def load_process_metadata(self) -> dict[str, Any]:
        definition = self._request("GET", f"/bpmn/repository/process-definition?key={self.config.target_process_key}")
        nodes = self._request("GET", f"/bpmn/repository/node/process-definition?procDefKey={self.config.target_process_key}&isOnlyUserNode=true")
        return {"definition": definition, "nodes": nodes}

    def query_work_orders(
        self,
        user_id: str,
        page_index: int,
        page_size: int,
        start_time: str,
        end_time: str,
        status: int = 0,
    ) -> WorkOrderPage:
        if not user_id:
            raise ValueError("缺少当前用户")
        payload = {
            "userId": user_id,
            "woStatus": status,
            "key": [self.config.target_process_key],
            "createTimeStart": start_time,
            "createTimeEnd": end_time,
            "pageIndex": page_index,
            "pageSize": page_size,
        }
        data = self._request("POST", "/bpmn/runtime/task/work-order/all", json=payload)
        if not isinstance(data, dict):
            raise PlatformError("工单响应格式无效")
        rows = data.get("objects", data.get("data", []))
        if isinstance(rows, dict):
            rows = rows.get("objects", rows.get("list", []))
        if not isinstance(rows, list):
            rows = []
        items = [self._parse_work_order(row) for row in rows if isinstance(row, dict)]
        total = data.get("totalObjects", data.get("total", len(items)))
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = len(items)
        return WorkOrderPage(items=items, total=total, page_index=page_index, page_size=page_size)

    def query_todo_tasks(
        self,
        login_id: str,
        *,
        assigned: bool = False,
        page_index: int = 1,
        page_size: int = 50,
    ) -> TodoTaskPage:
        if not login_id:
            raise ValueError("缺少当前用户")
        if page_index < 1 or page_size < 1:
            raise ValueError("分页参数无效")
        payload = {
            "key": [self.config.target_process_key],
            "assigned": bool(assigned),
            "pageIndex": page_index,
            "pageSize": page_size,
        }
        data = self._request(
            "POST",
            f"/bpmn/runtime/task/work-order/{login_id}/todo",
            json=payload,
        )
        if not isinstance(data, dict):
            raise PlatformError("待领取任务响应格式无效")
        rows = data.get("data", [])
        if isinstance(rows, dict):
            rows = rows.get("data", rows.get("list", []))
        if not isinstance(rows, list):
            rows = []
        items = [self._parse_todo_task(row) for row in rows if isinstance(row, dict)]
        total = data.get("total", data.get("totalObjects", len(items)))
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = len(items)
        return TodoTaskPage(items=items, total=total, page_index=page_index, page_size=page_size)

    @staticmethod
    def _parse_todo_task(row: dict[str, Any]) -> TodoTask:
        common = row.get("woCommon")
        common = common if isinstance(common, dict) else {}

        def text(*keys: str, source: dict[str, Any] | None = None) -> str:
            values = source if source is not None else row
            for key in keys:
                value = values.get(key)
                if value is not None:
                    return str(value)
            return ""

        return TodoTask(
            task_id=text("taskId"),
            order_id=text("wo_id", "woId", source=common),
            number=text("wo_code", "woCode", source=common),
            title=text("wo_title", "woTitle", source=common),
            current_node=text("nodeName", "currentNode"),
            assignee=text("assignee", "assigneeName"),
            process_instance_id=text("processInstanceId", "executionId"),
            process_definition_key=text("processDefinitionKey"),
            created_at=text("wo_createTime", "createTime", source=common),
            due_at=text("wo_dueDate", "dueDate", source=common),
            raw=row,
        )

    def assign_tasks(self, assignee: str, task_ids: list[str]) -> dict[str, Any]:
        if not assignee:
            raise ValueError("缺少领取人")
        if not isinstance(task_ids, list) or not task_ids or any(not isinstance(task_id, str) or not task_id.strip() for task_id in task_ids):
            raise ValueError("任务列表不能为空")
        payload = {"assignee": assignee, "tasks": task_ids}
        data = self._request("POST", "/bpmn/task/assignee/batch", json=payload)
        if not isinstance(data, dict):
            raise PlatformError("领取响应格式无效")
        if str(data.get("stat", "")) != "1":
            message = str(data.get("message") or "平台拒绝了领取请求")
            raise PlatformBusinessError(message)
        return data

    @staticmethod
    def _parse_work_order(row: dict[str, Any]) -> WorkOrder:
        def text(*keys: str) -> str:
            for key in keys:
                value = row.get(key)
                if value is not None:
                    return str(value)
            return ""

        return WorkOrder(
            order_id=text("id", "workOrderId", "taskId", "businessId"),
            number=text("code", "number", "orderNo", "woNo"),
            title=text("title", "name", "workOrderName"),
            status=text("state", "status", "woStatus"),
            current_node=text("currentNodeName", "currentNode", "nodeName", "taskName"),
            assignee=text("assignee", "assigneeName", "handlerName", "userName"),
            created_at=text("createTime", "createdAt"),
            due_at=text("dueDate", "dueAt", "dueTime"),
            process_instance_id=text("processInstanceId", "procInstId"),
            task_id=text("taskId"),
            process_version=text("processDefinitionVersion", "processVersion", "version"),
            raw=row,
        )

    def get_detail(self, order_id: str) -> dict[str, Any]:
        if not order_id:
            raise ValueError("工单 ID 不能为空")
        return self._request("GET", f"/bpmn/runtime/task/work-order/info/{order_id}")
