from __future__ import annotations

import os

from webapp import create_app

app = create_app()


if __name__ == "__main__":
    if os.environ.get("FLASK_ENV") == "production":
        raise SystemExit(
            "生产环境请使用单 worker 的 Waitress、Gunicorn 或其他 WSGI 服务器；"
            "禁止直接使用 Flask 开发服务器或多 worker 部署"
        )
    config = app.extensions["app_config"]
    monitor = app.extensions["session_monitor"]
    monitor.start()
    try:
        app.run(host=config.web_host, port=config.web_port, debug=False)
    finally:
        app.extensions["shutdown"]()
