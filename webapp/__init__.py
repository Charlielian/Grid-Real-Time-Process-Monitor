from __future__ import annotations

# Web 层应用工厂：集中装配 Flask、鉴权上下文和后台会话监控。
# 本模块只负责生命周期与依赖关系；具体 HTTP 输入校验和响应格式由 routes 子模块处理。

import secrets
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, current_app, session

from shared.config import AppConfig, AppPaths, ConfigStore, configure_logging
from webapp.services.auth import SessionRegistry, WebAuthService
from webapp.services.session_monitor import SessionMonitor


def _secret_key(paths: AppPaths, test_config: dict[str, Any] | None) -> str:
    """按配置、环境变量、磁盘文件的优先级取得 Flask 会话签名密钥。

    密钥文件仅在没有外部配置时生成，并尽量收紧权限；文件系统不可写时退回
    到进程内随机值，保证应用仍能启动，但该退回值不会跨进程或重启保留。
    """
    configured = (test_config or {}).get("SECRET_KEY") or __import__("os").environ.get("GRID_MONITOR_SECRET_KEY")
    if configured:
        return str(configured)
    secret_path = paths.root / ".secret_key"
    try:
        value = secret_path.read_text(encoding="ascii").strip() if secret_path.exists() else ""
        if value:
            return value
        value = secrets.token_hex(32)
        secret_path.write_text(value, encoding="ascii")
        secret_path.chmod(0o600)
        return value
    except OSError:
        return secrets.token_hex(32)


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    """创建并装配 Web 应用，同时启动非测试环境需要的后台服务。

    ``test_config`` 用于测试时覆盖路径、配置和 Flask 选项。应用关闭时通过统一
    shutdown 回调按会话监控、会话注册表的顺序释放资源。
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    paths = AppPaths((test_config or {}).get("DATA_DIR") if test_config else None)
    logger = configure_logging(paths)
    config_store = ConfigStore(paths, logger)
    config = test_config["APP_CONFIG"] if test_config and test_config.get("APP_CONFIG") else config_store.load()

    app.config.from_mapping(
        SECRET_KEY=_secret_key(paths, test_config),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool((test_config or {}).get("SESSION_COOKIE_SECURE", False)),
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        DATA_DIR=str(paths.root),
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)

    registry = SessionRegistry(config, logger, ttl_seconds=int(app.config.get("AUTH_CONTEXT_TTL", 1800)))
    auth = WebAuthService(registry, logger)
    monitor = SessionMonitor(config, logger)
    if not app.config.get("TESTING"):
        monitor.start()
    app.extensions.update({
        "paths": paths,
        "logger": logger,
        "config_store": config_store,
        "app_config": config,
        "session_registry": registry,
        "web_auth": auth,
        "session_monitor": monitor,
    })

    shutdown_lock = __import__("threading").Lock()
    shutdown_state = {"closed": False}

    def shutdown_resources() -> None:
        with shutdown_lock:
            if shutdown_state["closed"]:
                return
            shutdown_state["closed"] = True
        deadline = time.monotonic() + 10
        try:
            monitor.shutdown(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            logger.exception("会话监控关闭失败")
        try:
            registry.shutdown(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            logger.exception("会话注册表关闭失败")

    app.extensions["shutdown"] = shutdown_resources

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return {"csrf_token": token, "current_user": getattr(__import__("flask").request, "web_user", None)}

    @app.teardown_appcontext
    def cleanup(_exception: BaseException | None) -> None:
        if app.config.get("TESTING"):
            return

    from webapp.routes.auth import bp as auth_bp
    from webapp.routes.pages import bp as pages_bp
    from webapp.routes.api import bp as api_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)

    @app.errorhandler(400)
    def bad_request(error: Any) -> Any:
        if __import__("flask").request.path.startswith("/api/"):
            return __import__("flask").jsonify({"error": "bad_request", "message": "请求无效，请检查 CSRF 校验和请求格式"}), 400
        return "请求无效，请检查 CSRF 校验和请求格式", 400

    return app
