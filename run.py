from __future__ import annotations

import os

from webapp import create_app

app = create_app()


if __name__ == "__main__":
    if os.environ.get("FLASK_ENV") == "production":
        raise SystemExit("生产环境请使用 Waitress、Gunicorn 或其他 WSGI 服务器")
    config = app.extensions["app_config"]
    host = os.environ.get("GRID_MONITOR_HOST", config.web_host)
    port_value = os.environ.get("GRID_MONITOR_PORT")
    try:
        port = int(port_value) if port_value else config.web_port
    except ValueError as exc:
        raise SystemExit("GRID_MONITOR_PORT 必须是数字") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("网页端口必须在 1 到 65535 之间")
    monitor = app.extensions["session_monitor"]
    monitor.start()
    try:
        app.run(host=host, port=port, debug=False)
    finally:
        monitor.shutdown()
