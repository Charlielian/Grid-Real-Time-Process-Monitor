from __future__ import annotations

import json
from typing import Any

from flask import Blueprint, current_app, jsonify, request, session

from backend.auth.cas_client import SessionExpired
from backend.platform.client import PlatformBusinessError, PlatformError
from shared.config import config_to_dict, matches_title_keywords
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
        result = client.query_todo_tasks(
            request.web_user.login_id,
            assigned=False,
            page_index=page,
            page_size=page_size,
        )
        config = current_app.extensions["app_config"]
        items = [task for task in result.items if matches_title_keywords(task.title, config.target_title_keywords)]
        return jsonify({
            "items": [_task_row(task) for task in items],
            "total": len(items),
            "page": result.page_index,
            "page_size": result.page_size,
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
        pending = client.query_todo_tasks(login_id, assigned=False, page_index=1, page_size=100)
        pending_by_id = {
            task.task_id: task
            for task in pending.items
            if matches_title_keywords(task.title, config.target_title_keywords)
        }
        missing = [task_id for task_id in task_ids if task_id not in pending_by_id]
        if missing:
            return jsonify({"error": "task_unavailable", "message": "部分任务已被领取或不可领取", "task_ids": missing}), 409
        client.assign_tasks(login_id, task_ids)
        assigned = client.query_todo_tasks(login_id, assigned=True, page_index=1, page_size=100)
        assigned_ids = {
            task.task_id for task in assigned.items
            if task.task_id and task.assignee == login_id
            and matches_title_keywords(task.title, config.target_title_keywords)
        }
        if any(task_id not in assigned_ids for task_id in task_ids):
            return jsonify({"error": "claim_unconfirmed", "message": "领取结果未确认，请刷新后重试"}), 409
        config = current_app.extensions["app_config"]
        db = current_app.extensions["database"]
        from shared.models import WorkOrder
        records = []
        updated = []
        for task_id in task_ids:
            row = db.get_work_order_by_task_id(task_id, title_keywords=config.target_title_keywords)
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
        "stats": db.dashboard_stats(title_keywords=config.target_title_keywords),
        "latest_sync": dict(latest) if latest else None,
    })


@bp.get("/orders")
@api_login_required
def orders():
    config = current_app.extensions["app_config"]
    db = current_app.extensions["database"]
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(500, max(10, request.args.get("page_size", 50, type=int)))
    filters = {
        "keyword": request.args.get("keyword", "", type=str).strip(),
        "status": request.args.get("status", "", type=str).strip(),
        "node": request.args.get("node", "", type=str).strip(),
    }
    rows = db.list_work_orders(limit=page_size, offset=(page - 1) * page_size, title_keywords=config.target_title_keywords, **filters)
    total = db.count_work_orders(title_keywords=config.target_title_keywords, **filters)
    return jsonify({
        "items": [_row(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@bp.get("/orders/<order_id>")
@api_login_required
def order_detail(order_id: str):
    config = current_app.extensions["app_config"]
    db = current_app.extensions["database"]
    row = db.get_work_order(order_id, title_keywords=config.target_title_keywords)
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
        updated = AppConfig(
            base_url=current.base_url,
            web_host=current.web_host,
            web_port=current.web_port,
            poll_interval_seconds=int(data.get("poll_interval_seconds", current.poll_interval_seconds)),
            heartbeat_interval_seconds=current.heartbeat_interval_seconds,
            lookback_hours=int(data.get("lookback_hours", current.lookback_hours)),
            page_size=int(data.get("page_size", current.page_size)),
            auto_sync=_parse_bool(data["auto_sync"], "auto_sync") if "auto_sync" in data else current.auto_sync,
            ca_bundle=current.ca_bundle,
            target_process_title=current.target_process_title,
            target_process_key=current.target_process_key,
            target_title_keywords=current.target_title_keywords,
            auto_claim_pending_tasks=current.auto_claim_pending_tasks,
            work_order_retention_days=current.work_order_retention_days,
            work_order_event_retention_days=current.work_order_event_retention_days,
            sync_run_retention_days=current.sync_run_retention_days,
            database_cleanup_interval_seconds=current.database_cleanup_interval_seconds,
            database_cleanup_batch_size=current.database_cleanup_batch_size,
            database_max_size_mb=current.database_max_size_mb,
            wal_max_size_mb=current.wal_max_size_mb,
        )
    except (TypeError, ValueError):
        _logger().exception("更新设置失败")
        return _error("invalid_settings", "设置参数无效", 400)
    current_app.extensions["config_store"].save(updated)
    current_app.extensions["app_config"] = updated
    current_app.extensions["web_auth"].update_config(updated)
    current_app.extensions["session_monitor"].update_config(updated)
    return jsonify(config_to_dict(updated))
