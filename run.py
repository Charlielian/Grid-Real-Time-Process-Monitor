"""Flask/WSGI 入口。

生产环境应由单 worker 的 Waitress、Gunicorn 或 uWSGI 导入 ``run:app``；仅在
本地开发时执行文件本身。create_app 已负责非测试后台服务的幂等启动和统一关闭。
"""

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
    try:
        app.run(host=config.web_host, port=config.web_port, debug=False)
    finally:
        app.extensions["shutdown"]()
