"""为前端提供 JSON API 的路由模块。

接口统一执行会话鉴权和 CSRF 校验，并把平台异常转换成稳定的 JSON 错误码。
工单筛选参数与页面路由共用 parse_order_filters，保证首屏和局部刷新语义一致。
工单列表与详情均实时查询上游，本地不保留任何快照。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request

from backend.auth.cas_client import SessionExpired
from backend.platform.client import PlatformBusinessError, PlatformError
from backend.platform.user_info_client import UserInfoClient
from shared.config import config_to_dict, with_config_updates
from shared.filters import parse_order_filters
from shared.models import TodoTask, WorkOrder
from webapp.routes.decorators import api_login_required, check_csrf
from webapp.services.orders import fetch_work_orders
from webapp.services.pending_tasks import claimable_tasks, query_all_todo_tasks

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _logger() -> Any:
    return current_app.extensions["logger"]


def _error(error: str, message: str, status: int):
    return jsonify({"error": error, "message": message}), status


def _row(order: WorkOrder) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "number": order.number,
        "title": order.title,
        "status": order.status,
        "current_node": order.current_node,
        "assignee": order.assignee,
        "created_at": order.created_at,
        "due_at": order.due_at,
        "process_instance_id": order.process_instance_id,
        "task_id": order.task_id,
        "process_version": order.process_version,
        "updated_at": "",
    }


def _task_row(task: TodoTask, cities: tuple[str, ...]) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "order_id": task.order_id,
        "number": task.number,
        "title": task.title,
        "current_node": task.current_node,
        "assignee": task.assignee,
        "process_instance_id": task.process_instance_id,
        "process_definition_key": task.process_definition_key,
        "created_at": task.created_at,
        "due_at": task.due_at,
        "claimable": bool(cities and any(city in task.title for city in cities)),
    }


def _task_ids_payload() -> list[str] | None:
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "assignee" in data:
        return None
    task_ids = data.get("task_ids")
    if not isinstance(task_ids, list) or not task_ids:
        return None
    if any(not isinstance(task_id, str) or not task_id.strip() for task_id in task_ids):
        return None
    if len(task_ids) != len(set(task_ids)):
        return None
    return [task_id.strip() for task_id in task_ids]


def _query_all_todo_tasks(client: Any, login_id: str, *, assigned: bool, config: Any, cities: tuple[str, ...] = ()) -> list[TodoTask]:
    """Read every upstream page before applying the requested city scope."""
    return query_all_todo_tasks(client, login_id, assigned=assigned, config=config, cities=cities)


def _parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "false"}:
            return normalized == "true"
    raise ValueError(f"{field} 必须是 true/false 或 1/0")


@bp.get("/pending-tasks")
@api_login_required
def pending_tasks():
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(100, max(1, request.args.get("page_size", 50, type=int)))
    try:
        client = current_app.extensions["web_auth"].platform(request.web_auth_context)
        config = current_app.extensions["app_config"]
        try:
            filters = parse_order_filters(request.args)
        except ValueError as exc:
            return _error("invalid_filter", str(exc), 400)
        cities = filters["city"]
        items = _query_all_todo_tasks(client, request.web_user.login_id, assigned=False, config=config, cities=cities)
        account_cities = UserInfoClient(config, request.web_auth_context.session, _logger()).get_cities(request.web_user.login_id)
        claimable = claimable_tasks(items, account_cities)
        claimable_ids = [task.task_id for task in claimable if task.task_id]
        start = (page - 1) * page_size
        return jsonify({
            "items": [_task_row(task, account_cities) for task in items[start:start + page_size]],
            "total": len(items),
            "page": page,
            "page_size": page_size,
            "process_key": config.target_process_key,
            "account_cities": list(account_cities),
            "claimable_ids": claimable_ids,
        })
    except SessionExpired:
        return jsonify({"error": "session_expired", "message": "平台会话已失效"}), 401
    except (PlatformError, ValueError):
        return jsonify({"error": "upstream_unavailable", "message": "待领取任务暂时不可用"}), 502


@bp.post("/pending-tasks/claim")
@api_login_required
def claim_pending_tasks():
    check_csrf()
    task_ids = _task_ids_payload()
    if task_ids is None:
        return jsonify({"error": "invalid_request", "message": "task_ids 必须是非空且不重复的数组，且不得指定 assignee"}), 400
    login_id = request.web_user.login_id
    try:
        client = current_app.extensions["web_auth"].platform(request.web_auth_context)
        config = current_app.extensions["app_config"]
        pending = _query_all_todo_tasks(client, login_id, assigned=False, config=config)
        account_cities = UserInfoClient(config, request.web_auth_context.session, _logger()).get_cities(login_id)
        claimable = claimable_tasks(pending, account_cities)
        claimable_by_id = {task.task_id: task for task in claimable if task.task_id}
        missing = [task_id for task_id in task_ids if task_id not in claimable_by_id]
        if missing:
            return jsonify({"error": "task_unavailable", "message": "部分任务已被领取或不可领取", "task_ids": missing}), 409
        client.assign_tasks(login_id, task_ids)
        assigned = _query_all_todo_tasks(client, login_id, assigned=True, config=config)
        assigned_ids = {
            task.task_id for task in assigned
            if task.task_id and task.assignee == login_id
        }
        if any(task_id not in assigned_ids for task_id in task_ids):
            return jsonify({"error": "claim_unconfirmed", "message": "领取结果未确认，请刷新后重试"}), 409
        return jsonify({"message": "领取成功", "task_ids": task_ids, "assignee": login_id})
    except SessionExpired:
        return jsonify({"error": "session_expired", "message": "平台会话已失效"}), 401
    except PlatformBusinessError:
        _logger().exception("领取任务业务失败")
        return _error("claim_failed", "领取任务失败，请稍后重试", 409)
    except (PlatformError, ValueError):
        return jsonify({"error": "upstream_unavailable", "message": "领取服务暂时不可用"}), 502


@bp.get("/orders")
@api_login_required
def orders():
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(500, max(10, request.args.get("page_size", 50, type=int)))
    try:
        filters = parse_order_filters(request.args)
    except ValueError as exc:
        return _error("invalid_filter", str(exc), 400)
    try:
        client = current_app.extensions["web_auth"].platform(request.web_auth_context)
        config = current_app.extensions["app_config"]
        items = fetch_work_orders(
            client,
            request.web_user.login_id,
            config,
            keyword=filters["keyword"],
            status=filters["status"],
            node=filters["node"],
            cities=filters["city"],
            start_time=filters["start_time"],
            end_time=filters["end_time"],
        )
    except SessionExpired:
        return jsonify({"error": "session_expired", "message": "平台会话已失效"}), 401
    except (PlatformError, ValueError):
        return jsonify({"error": "upstream_unavailable", "message": "工单服务暂时不可用"}), 502
    start = (page - 1) * page_size
    return jsonify({
        "items": [_row(order) for order in items[start:start + page_size]],
        "total": len(items),
        "page": page,
        "page_size": page_size,
    })


@bp.get("/orders/<order_id>")
@api_login_required
def order_detail(order_id: str):
    try:
        client = current_app.extensions["web_auth"].platform(request.web_auth_context)
        config = current_app.extensions["app_config"]
        detail = client.get_detail(order_id)
    except SessionExpired:
        return jsonify({"error": "session_expired", "message": "平台会话已失效"}), 401
    except (PlatformError, ValueError):
        return jsonify({"error": "upstream_unavailable", "message": "工单详情暂时不可用"}), 502
    if not isinstance(detail, dict) or not detail:
        return jsonify({"error": "not_found", "message": "工单不存在"}), 404
    return jsonify({"order": detail})


@bp.get("/process")
@api_login_required
def process_metadata():
    try:
        client = current_app.extensions["web_auth"].platform(request.web_auth_context)
        return jsonify(client.load_process_metadata())
    except SessionExpired:
        return jsonify({"error": "session_expired", "message": "平台会话已失效"}), 401
    except Exception:
        _logger().exception("加载流程信息失败")
        return _error("upstream_unavailable", "流程信息暂时不可用", 502)


@bp.get("/settings")
@api_login_required
def get_settings():
    return jsonify(config_to_dict(current_app.extensions["app_config"]))


@bp.put("/settings")
@api_login_required
def update_settings():
    check_csrf()
    data = request.get_json(silent=True) or {}
    current = current_app.extensions["app_config"]
    try:
        from shared.config import AppConfig
        updated = with_config_updates(
            current,
            poll_interval_seconds=int(data.get("poll_interval_seconds", current.poll_interval_seconds)),
            lookback_hours=int(data.get("lookback_hours", current.lookback_hours)),
            page_size=int(data.get("page_size", current.page_size)),
            auto_sync=_parse_bool(data["auto_sync"], "auto_sync") if "auto_sync" in data else current.auto_sync,
            auto_claim_pending_tasks=_parse_bool(data["auto_claim_pending_tasks"], "auto_claim_pending_tasks") if "auto_claim_pending_tasks" in data else current.auto_claim_pending_tasks,
            auto_claim_interval_seconds=int(data.get("auto_claim_interval_seconds", current.auto_claim_interval_seconds)),
        )
    except (TypeError, ValueError):
        _logger().exception("更新设置失败")
        return _error("invalid_settings", "设置参数无效", 400)
    try:
        current_app.extensions["config_store"].save(updated)
    except OSError:
        _logger().exception("配置文件写入失败")
        return _error("config_write_failed", "配置文件无法写入，请检查 config.yaml 所在目录权限或文件占用", 500)
    current_app.extensions["app_config"] = updated
    current_app.extensions["web_auth"].update_config(updated)
    current_app.extensions["session_monitor"].update_config(updated)
    current_app.extensions["auto_claim"].update_config(updated)
    return jsonify(config_to_dict(updated))


@bp.get("/auto-claim-stats")
@api_login_required
def auto_claim_stats():
    """返回后端自动领取服务的统计信息。"""
    service = current_app.extensions["auto_claim"]
    return jsonify(service.stats())