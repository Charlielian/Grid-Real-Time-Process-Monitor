"""Flask/WSGI 入口。

生产环境使用 Waitress 单 worker 启动；仅在本地开发时执行文件本身。
create_app 已负责非测试后台服务的幂等启动和统一关闭。
"""

from __future__ import annotations

import os

from webapp import create_app

app = create_app()


if __name__ == "__main__":
    config = app.extensions["app_config"]
    try:
        from waitress import serve
        serve(app, host=config.web_host, port=config.web_port)
    finally:
        app.extensions["shutdown"]()
