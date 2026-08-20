from __future__ import annotations

import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, current_app, session

from backend.storage.database import Database
from shared.config import AppConfig, AppPaths, ConfigStore, configure_logging
from webapp.services.auth import SessionRegistry, WebAuthService
from webapp.services.database_maintenance import DatabaseMaintenanceService
from webapp.services.session_monitor import SessionMonitor
from webapp.services.sync import SyncJobManager


def _secret_key(paths: AppPaths, test_config: dict[str, Any] | None) -> str:
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
    app = Flask(__name__, template_folder="templates", static_folder="static")
    paths = AppPaths((test_config or {}).get("DATA_DIR") if test_config else None)
    logger = configure_logging(paths)
    config_store = ConfigStore(paths, logger)
    config = config_store.load()
    if test_config and test_config.get("APP_CONFIG"):
        config = test_config["APP_CONFIG"]

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

    database = Database(Path(app.config["DATA_DIR"]) / "monitor.sqlite3")
    registry = SessionRegistry(config, logger, ttl_seconds=int(app.config.get("AUTH_CONTEXT_TTL", 1800)))
    auth = WebAuthService(registry, logger, database)
    jobs = SyncJobManager(database, logger)
    registry.set_remove_callback(lambda context_id: jobs.cancel_context(context_id, wait=True))
    monitor = SessionMonitor(database, config, logger)
    maintenance = DatabaseMaintenanceService(database, config, logger, autostart=not bool(app.config.get("TESTING")))
    app.extensions.update({
        "paths": paths,
        "logger": logger,
        "config_store": config_store,
        "app_config": config,
        "database": database,
        "session_registry": registry,
        "web_auth": auth,
        "sync_jobs": jobs,
        "session_monitor": monitor,
        "database_maintenance": maintenance,
    })

    shutdown_lock = __import__("threading").Lock()
    shutdown_state = {"closed": False}

    def shutdown_resources() -> None:
        with shutdown_lock:
            if shutdown_state["closed"]:
                return
            shutdown_state["closed"] = True
        jobs.shutdown()
        monitor.shutdown()
        maintenance.shutdown()
        registry.shutdown()
        database.close()

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
