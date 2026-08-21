from __future__ import annotations

from io import BytesIO

import requests

from flask import Blueprint, current_app, jsonify, redirect, request, session, url_for

from backend.auth.cas_client import AuthError, SessionExpired
from backend.platform.client import PlatformError
from webapp.routes.decorators import check_csrf

bp = Blueprint("auth", __name__)


def _context():
    auth = current_app.extensions["web_auth"]
    context = auth.registry.get(session.get("auth_context_id"))
    if context is None:
        context = auth.registry.create()
        session["auth_context_id"] = context.context_id
    return context


def _payload() -> dict[str, str]:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return {str(key): str(value) for key, value in data.items() if value is not None}
    return {key: value for key, value in request.form.items()}


@bp.get("/auth/captcha")
def captcha():
    context = _context()
    try:
        image = current_app.extensions["web_auth"].captcha(context)
    except SessionExpired:
        return jsonify({"error": "unauthorized", "message": "登录会话已失效"}), 401
    except (PlatformError, requests.RequestException):
        return jsonify({"error": "upstream_unavailable", "message": "验证码获取失败，请稍后重试"}), 502
    except Exception:
        current_app.extensions["logger"].exception("获取验证码失败")
        return jsonify({"error": "internal_error", "message": "验证码获取失败"}), 500
    response = current_app.response_class(image, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/auth/captcha/verify")
def verify_captcha():
    check_csrf()
    data = _payload()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    captcha_value = data.get("captcha", "").strip()
    if not username or not password or not captcha_value:
        return jsonify({"ok": False, "message": "请输入账号、密码和图形验证码"}), 400
    try:
        ok = current_app.extensions["web_auth"].verify_captcha(
            _context(), username, password, captcha_value
        )
    except (AuthError, ValueError):
        ok = False
    return jsonify({"ok": ok, "message": "图形验证码校验通过" if ok else "图形验证码校验失败"})


@bp.post("/auth/sms")
def send_sms():
    check_csrf()
    data = _payload()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return jsonify({"ok": False, "message": "请输入账号和密码"}), 400
    try:
        ok = current_app.extensions["web_auth"].send_sms(_context(), username, password)
    except (AuthError, ValueError):
        ok = False
    return jsonify({"ok": ok, "message": "短信验证码已发送" if ok else "请先完成图形验证码校验"}), (200 if ok else 400)


@bp.post("/auth/login")
def login_submit():
    check_csrf()
    data = _payload()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    captcha_value = data.get("captcha", "").strip()
    sms_code = data.get("sms_code", "").strip()
    if not all((username, password, captcha_value, sms_code)):
        return jsonify({"ok": False, "message": "请完整填写登录信息"}), 400
    try:
        user = current_app.extensions["web_auth"].login(
            _context(), username, password, captcha_value, sms_code
        )
    except SessionExpired:
        return jsonify({"ok": False, "message": "登录会话已失效，请刷新验证码"}), 401
    except (AuthError, ValueError):
        return jsonify({"ok": False, "message": "登录失败，请检查验证码和短信验证码"}), 401
    session.permanent = True
    session["saved_login_id"] = user.login_id
    current_app.extensions["session_monitor"].check_now(user.login_id)
    return jsonify({"ok": True, "user": {"login_id": user.login_id, "display_name": user.display_name}, "redirect": url_for("web.dashboard")})


@bp.post("/auth/restore")
def restore_saved_session():
    check_csrf()
    data = _payload()
    login_id = data.get("login_id", "").strip() or session.get("saved_login_id")
    if not login_id:
        return jsonify({"ok": False, "message": "没有可恢复的登录会话"}), 404
    if current_app.extensions["web_auth"].database.get_saved_account(login_id) is None:
        return jsonify({"ok": False, "message": "未找到该保存账号"}), 404
    auth = current_app.extensions["web_auth"]
    context = _context()
    try:
        user = auth.restore(context, login_id)
    except SessionExpired:
        session.pop("auth_context_id", None)
        return jsonify({"ok": False, "message": "保存的 Cookies 已失效，请重新登录"}), 401
    except (PlatformError, requests.RequestException):
        return jsonify({"ok": False, "message": "网络暂时不可用，请稍后重试"}), 503
    except Exception:
        current_app.extensions["logger"].exception("恢复保存会话失败")
        return jsonify({"ok": False, "message": "恢复登录会话失败，请稍后重试"}), 500
    session.permanent = True
    session["saved_login_id"] = user.login_id
    return jsonify({"ok": True, "user": {"login_id": user.login_id, "display_name": user.display_name}, "redirect": url_for("web.dashboard")})


@bp.get("/auth/saved-accounts")
def saved_accounts():
    rows = current_app.extensions["web_auth"].list_saved_accounts()
    return jsonify({"accounts": [
        {
            "login_id": row["login_id"],
            "display_name": row["display_name"],
            "last_used_at": row["last_used_at"],
            "last_heartbeat_at": row["last_heartbeat_at"],
            "heartbeat_status": row["heartbeat_status"],
            "consecutive_failures": row["consecutive_failures"],
            "last_error": row["last_error"],
        }
        for row in rows
    ]})


@bp.delete("/auth/saved-accounts/<login_id>")
def delete_saved_account(login_id: str):
    check_csrf()
    if not login_id or current_app.extensions["web_auth"].database.get_saved_account(login_id) is None:
        return jsonify({"ok": False, "message": "未找到该保存账号"}), 404
    current_app.extensions["web_auth"].remove_saved_account(login_id)
    if session.get("saved_login_id") == login_id:
        session.pop("saved_login_id", None)
    return jsonify({"ok": True})


@bp.post("/auth/heartbeat/<login_id>")
def heartbeat(login_id: str):
    check_csrf()
    result = current_app.extensions["session_monitor"].check_now(login_id)
    if result is None:
        return jsonify({"ok": False, "message": "未找到该保存账号"}), 404
    return jsonify({"ok": True, "account": {
        "login_id": result["login_id"],
        "display_name": result["display_name"],
        "last_heartbeat_at": result["last_heartbeat_at"],
        "heartbeat_status": result["heartbeat_status"],
        "consecutive_failures": result["consecutive_failures"],
        "last_error": result["last_error"],
    }})


@bp.get("/api/v1/session")
def session_info():
    context = current_app.extensions["session_registry"].get(session.get("auth_context_id"))
    user = context.user if context else None
    return jsonify({
        "authenticated": bool(user),
        "user": {"login_id": user.login_id, "display_name": user.display_name} if user else None,
    })
