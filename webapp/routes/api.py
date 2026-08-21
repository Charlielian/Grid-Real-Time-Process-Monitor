from __future__ import annotations

import json
from typing import Any

from flask import Blueprint, current_app, jsonify, request, session

from backend.auth.cas_client import SessionExpired
from backend.platform.client import PlatformBusinessError, PlatformError
from shared.config import config_to_dict, with_config_updates
from shared.filters import parse_order_filters
from webapp.routes.decorators import api_login_required, check_csrf

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _logger() -> Any:
    return current_app.extensions["logger"]


def _error(error: str, message: str, status: int):
    return jsonify({"error": error, "message": message}), status


def _row(row: Any) -> dict[str, Any]:
    return {
        "order_id": row["order_id"],
        "number": row["number"],
        "title": row["title"],
        "status": row["status"],
        "current_node": row["current_node"],
        "assignee": row["assignee"],
        "created_at": row["created_at"],
        "due_at": row["due_at"],
        "process_instance_id": row["process_instance_id"],
        "task_id": row["task_id"],
        "process_version": row["process_version"],
        "updated_at": row["updated_at"],
    }


def _task_row(task: Any) -> dict[str, Any]:
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


def _query_all_todo_tasks(client: Any, login_id: str, *, assigned: bool, config: Any, cities: tuple[str, ...] = ()) -> list[Any]:
    """Read every upstream page before applying the requested city scope."""
    page_index = 1
    page_size = 100
    tasks: list[Any] = []
    seen_pages: set[int] = set()
    while page_index not in seen_pages:
        seen_pages.add(page_index)
        result = client.query_todo_tasks(
            login_id,
            assigned=assigned,
            page_index=page_index,
            page_size=page_size,
        )
        tasks.extend(
            task for task in result.items
            if not cities or any(city in task.title for city in cities)
        )
        if not result.items or len(result.items) < max(1, result.page_size):
            break
        page_index += 1
    return tasks


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
        start = (page - 1) * page_size
        return jsonify({
            "items": [_task_row(task) for task in items[start:start + page_size]],
            "total": len(items),
            "page": page,
            "page_size": page_size,
            "process_key": config.target_process_key,
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
        pending_by_id = {task.task_id: task for task in pending}
        missing = [task_id for task_id in task_ids if task_id not in pending_by_id]
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
        config = current_app.extensions["app_config"]
        db = current_app.extensions["database"]
        from shared.models import WorkOrder
        records = []
        updated = []
        for task_id in task_ids:
            row = db.get_work_order_by_task_id(task_id)
            if row is not None and row["assignee"] != login_id:
                records.append(WorkOrder(
                    order_id=row["order_id"], number=row["number"], title=row["title"], status=row["status"],
                    current_node=row["current_node"], assignee=login_id, created_at=row["created_at"], due_at=row["due_at"],
                    process_instance_id=row["process_instance_id"], task_id=row["task_id"], process_version=row["process_version"],
                    raw=json.loads(row["raw_json"] or "{}"),
                ))
            updated.append(task_id)
        if records:
            db.upsert_orders(records)
        return jsonify({"message": "领取成功", "task_ids": updated, "assignee": login_id})
    except SessionExpired:
        return jsonify({"error": "session_expired", "message": "平台会话已失效"}), 401
    except PlatformBusinessError:
        _logger().exception("领取任务业务失败")
        return _error("claim_failed", "领取任务失败，请稍后重试", 409)
    except (PlatformError, ValueError):
        return jsonify({"error": "upstream_unavailable", "message": "领取服务暂时不可用"}), 502



@bp.get("/dashboard")
@api_login_required
def dashboard():
    config = current_app.extensions["app_config"]
    db = current_app.extensions["database"]
    latest = db.latest_sync_run()
    return jsonify({
        "stats": db.dashboard_stats(),
        "latest_sync": dict(latest) if latest else None,
    })


@bp.get("/orders")
@api_login_required
def orders():
    db = current_app.extensions["database"]
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(500, max(10, request.args.get("page_size", 50, type=int)))
    try:
        filters = parse_order_filters(request.args)
    except ValueError as exc:
        return _error("invalid_filter", str(exc), 400)
    city_keywords = filters.pop("city")
    filters.pop("start_date")
    filters.pop("end_date")
    rows = db.list_work_orders(limit=page_size, offset=(page - 1) * page_size, title_keywords=city_keywords, **filters)
    total = db.count_work_orders(title_keywords=city_keywords, **filters)
    return jsonify({
        "items": [_row(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@bp.get("/orders/<order_id>")
@api_login_required
def order_detail(order_id: str):
    db = current_app.extensions["database"]
    row = db.get_work_order(order_id)
    if row is None:
        return jsonify({"error": "not_found", "message": "工单不存在"}), 404
    return jsonify({"order": _row(row), "events": [dict(event) for event in db.list_events(order_id)]})


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


def _sync_job_payload(job: Any) -> dict[str, Any]:
    result = {
        "job_id": job.job_id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
    }
    if job.summary:
        result["summary"] = {
            "total": job.summary.total,
            "added": job.summary.added,
            "changed": job.summary.changed,
        }
    return result


@bp.post("/sync")
@api_login_required
def start_sync():
    check_csrf()
    try:
        auth_context = request.web_auth_context
        client = current_app.extensions["web_auth"].platform(auth_context)
        job = current_app.extensions["sync_jobs"].submit(
            auth_context.context_id,
            client,
            request.web_user,
            current_app.extensions["app_config"],
        )
    except RuntimeError:
        _logger().exception("提交同步任务失败")
        return _error("sync_unavailable", "同步服务暂不可用", 503)
    return jsonify(_sync_job_payload(job)), 202


@bp.get("/sync/<job_id>")
@api_login_required
def sync_status(job_id: str):
    job = current_app.extensions["sync_jobs"].get(job_id)
    if job is None or job.context_id != request.web_auth_context.context_id:
        return jsonify({"error": "not_found", "message": "同步任务不存在"}), 404
    return jsonify(_sync_job_payload(job))


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
    return jsonify(config_to_dict(updated))
