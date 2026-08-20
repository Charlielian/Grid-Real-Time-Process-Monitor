from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from webapp.routes.decorators import check_csrf, web_login_required

bp = Blueprint("web", __name__)


@bp.get("/")
def index() -> str:
    return redirect(url_for("web.login"))


@bp.get("/login")
def login() -> str:
    from flask import current_app
    context_id = session.get("auth_context_id")
    if context_id:
        try:
            current_app.extensions["web_auth"].require_user(context_id)
            return redirect(url_for("web.dashboard"))
        except Exception:
            current_app.extensions["session_registry"].remove(context_id)
            session.pop("auth_context_id", None)
    saved_login_id = session.get("saved_login_id")
    saved_accounts = current_app.extensions["web_auth"].list_saved_accounts()
    saved_session_available = bool(saved_accounts) or current_app.extensions["web_auth"].has_saved_session(saved_login_id)
    return render_template(
        "login.html",
        saved_session_available=saved_session_available,
        saved_accounts=saved_accounts,
    )


@bp.get("/dashboard")
@web_login_required
def dashboard() -> str:
    from flask import current_app
    config = current_app.extensions["app_config"]
    stats = current_app.extensions["database"].dashboard_stats(title_keywords=config.target_title_keywords)
    latest = current_app.extensions["database"].latest_sync_run()
    return render_template("dashboard.html", stats=stats, latest_sync=latest)


@bp.get("/pending-tasks")
@web_login_required
def pending_tasks() -> str:
    from flask import current_app
    config = current_app.extensions["app_config"]
    return render_template(
        "pending_tasks.html",
        poll_interval_seconds=config.poll_interval_seconds,
        auto_claim_pending_tasks=config.auto_claim_pending_tasks,
        target_title_keywords=config.target_title_keywords,
    )


@bp.get("/orders")
@web_login_required
def orders() -> str:
    from flask import current_app
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
    pages = max(1, (total + page_size - 1) // page_size)
    return render_template(
        "orders.html",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        page_size=page_size,
        filters=filters,
        title_keywords=config.target_title_keywords,
        poll_interval_seconds=config.poll_interval_seconds,
        auto_sync=config.auto_sync,
    )


@bp.get("/orders/<order_id>")
@web_login_required
def order_detail(order_id: str) -> str:
    from flask import current_app
    config = current_app.extensions["app_config"]
    db = current_app.extensions["database"]
    row = db.get_work_order(order_id, title_keywords=config.target_title_keywords)
    if row is None:
        return render_template("not_found.html", message="工单不存在"), 404
    return render_template("order_detail.html", order=row, events=db.list_events(order_id))


@bp.route("/settings", methods=["GET", "POST"])
@web_login_required
def settings() -> str:
    from flask import current_app
    if request.method == "POST":
        check_csrf()
        try:
            current = current_app.extensions["app_config"]
            from shared.config import AppConfig
            updated = AppConfig(
                base_url=current.base_url,
                web_host=current.web_host,
                web_port=current.web_port,
                poll_interval_seconds=int(request.form.get("poll_interval_seconds", current.poll_interval_seconds)),
                heartbeat_interval_seconds=current.heartbeat_interval_seconds,
                lookback_hours=int(request.form.get("lookback_hours", current.lookback_hours)),
                page_size=int(request.form.get("page_size", current.page_size)),
                auto_sync=request.form.get("auto_sync") == "on",
                ca_bundle=current.ca_bundle,
                target_process_title=current.target_process_title,
                target_process_key=current.target_process_key,
                target_title_keywords=current.target_title_keywords,
                auto_claim_pending_tasks=current.auto_claim_pending_tasks,
            )
            current_app.extensions["config_store"].save(updated)
            current_app.extensions["app_config"] = updated
            current_app.extensions["web_auth"].update_config(updated)
            current_app.extensions["session_monitor"].update_config(updated)
            flash("设置已保存", "success")
        except (TypeError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("web.settings"))
    return render_template("settings.html", config=current_app.extensions["app_config"])


@bp.post("/logout")
@web_login_required
def logout() -> str:
    check_csrf()
    from flask import current_app
    context_id = session.pop("auth_context_id", None)
    current_app.extensions["session_registry"].remove(context_id)
    saved_login_id = session.get("saved_login_id")
    session.clear()
    if saved_login_id:
        session["saved_login_id"] = saved_login_id
    flash("已退出登录，可在登录页使用保存的 Cookies 登录", "success")
    return redirect(url_for("web.login"))
