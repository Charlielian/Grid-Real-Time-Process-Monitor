# 生产部署说明

## 单 worker 限制（必须遵守）

本应用的登录上下文、同步任务状态和会话心跳后台任务都保存在当前 Python 进程的内存中：

- `SessionRegistry` 保存登录上下文和上游会话 Cookies；
- `SyncJobManager` 保存任务状态、取消信号和任务句柄；
- `SessionMonitor` 在进程内运行后台会话心跳线程。

因此，生产环境**必须使用单进程、单 worker**。Gunicorn、uWSGI、Supervisor、systemd 等启动器不得启动多个应用 worker，也不要通过多进程扩展实例。多个 worker 会导致请求落到不同进程时找不到登录上下文或同步任务；进程重启也会丢失这些内存状态。数据库只保存业务数据和同步运行记录，不能替代上述内存状态。

不要把 `SyncJobManager` 的 `max_workers=2` 理解为 Web worker 数量；它只是单个应用进程内同步任务使用的线程数。

## WSGI 启动

生产环境应导入 `run:app`，不要执行 `python run.py`。`python run.py` 使用 Flask 开发服务器，仅适合本地或开发环境；当 `FLASK_ENV=production` 时会主动拒绝启动。

以下命令均明确配置为单 worker。请在项目根目录执行，并按实际环境设置 `GRID_MONITOR_SECRET_KEY`、`GRID_MONITOR_HOST` 和 `GRID_MONITOR_PORT`：

### Waitress

安装生产依赖：

```bash
pip install -e '.[production]'
```

启动：

```bash
waitress-serve --listen=${GRID_MONITOR_HOST:-127.0.0.1}:${GRID_MONITOR_PORT:-5000} run:app
```

Waitress 的单进程模型适合本应用的内存状态约束。

### Gunicorn

```bash
gunicorn --workers 1 --bind ${GRID_MONITOR_HOST:-127.0.0.1}:${GRID_MONITOR_PORT:-5000} run:app
```

`--workers 1` 是强制要求，不得改为更大的值。

### uWSGI

```bash
uwsgi --http ${GRID_MONITOR_HOST:-127.0.0.1}:${GRID_MONITOR_PORT:-5000} --processes 1 --module run:app
```

`--processes 1` 是强制要求；不要配置额外的 worker、进程或多实例启动。

## 会话监控生命周期

当前 `run.py` 直接运行模式会启动并在退出时关闭 `SessionMonitor`。通过外部 WSGI 服务器导入 `run:app` 时，应用对象可以正常提供请求服务，但不会执行 `run.py` 的 `__main__` 启动分支。若生产环境需要自动会话心跳，请使用单 worker 的服务管理方式，并在部署前确认所需的心跳启动/停止生命周期已接入；不要为此启动第二个应用 worker。

## 数据库历史保留与监控

SQLite 数据库只保留 `work_orders` 的当前快照；`raw_json` 会随当前工单更新，不建立原始响应历史链。后台数据库维护线程按 `config.yaml` 中的保留周期批量删除过期工单、事件和已完成的同步运行记录，未完成的 `sync_runs` 永远不会被清理。

相关配置：

- `work_order_retention_days`、`work_order_event_retention_days`、`sync_run_retention_days`：保留天数，`0` 表示永久保留；
- `database_cleanup_interval_seconds`、`database_cleanup_batch_size`：维护周期和单批删除上限；
- `database_max_size_mb`、`wal_max_size_mb`：数据库文件和 WAL 文件告警阈值，WAL 超阈值时执行被动 checkpoint。

维护服务在应用工厂创建并自动启动，关闭应用时会先停止同步任务和维护线程，再关闭数据库。应通过应用日志监控清理数量、数据库文件大小和 WAL 告警；默认不频繁执行 `VACUUM`，如需回收删除后的磁盘空间应在离线维护窗口执行。


业务和轮询配置位于根目录 `config.yaml`，修改后重启应用生效。生产部署时应通过反向代理或服务管理器提供外部访问，并确保只有一个 `run:app` 应用进程在运行。
