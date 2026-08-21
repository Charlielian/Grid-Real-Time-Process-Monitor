from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import current_app, jsonify, redirect, request, session, url_for


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        import secrets
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def check_csrf() -> None:
    expected = session.get("csrf_token")
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not supplied or supplied != expected:
        from werkzeug.exceptions import BadRequest
        raise BadRequest("CSRF 校验失败")


def web_login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        auth = current_app.extensions["web_auth"]
        try:
            context, user = auth.require_user(session.get("auth_context_id"))
        except Exception as exc:
            from backend.auth.cas_client import SessionExpired
            import requests
            if isinstance(exc, SessionExpired):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "unauthorized", "message": "请先登录"}), 401
                return redirect(url_for("web.login", next=request.path))
            if isinstance(exc, requests.RequestException):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "upstream_unavailable", "message": "服务暂时不可用"}), 503
                return "服务暂时不可用，请稍后重试", 503
            current_app.extensions["logger"].exception("认证检查失败")
            if request.path.startswith("/api/"):
                return jsonify({"error": "internal_error", "message": "服务暂时不可用"}), 500
            return "服务暂时不可用，请稍后重试", 500
        request.web_auth_context = context
        request.web_user = user
        return view(*args, **kwargs)
    return wrapped


def api_login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        auth = current_app.extensions["web_auth"]
        try:
            context, user = auth.require_user(session.get("auth_context_id"))
        except Exception as exc:
            from backend.auth.cas_client import SessionExpired
            import requests
            if isinstance(exc, SessionExpired):
                return jsonify({"error": "unauthorized", "message": "请先登录"}), 401
            if isinstance(exc, requests.RequestException):
                return jsonify({"error": "upstream_unavailable", "message": "服务暂时不可用"}), 503
            current_app.extensions["logger"].exception("认证检查失败")
            return jsonify({"error": "internal_error", "message": "服务暂时不可用"}), 500
        request.web_auth_context = context
        request.web_user = user
        return view(*args, **kwargs)
    return wrapped
