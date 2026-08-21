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

`create_app()` 在非测试环境导入时会自动启动 `SessionMonitor` 和数据库维护线程；因此 Waitress、Gunicorn、uWSGI 导入 `run:app` 时也会启动心跳。应用对象暴露的 `app.extensions["shutdown"]()` 用于优雅停止同步任务、心跳、维护线程和会话注册表。生产服务管理器必须在停止 worker 前调用该关闭入口；如果所用 WSGI 服务器没有应用级退出回调，请使用外层包装器或服务管理器的停止脚本调用它，再结束进程。

不要为补偿心跳而启动第二个应用 worker。单 worker 是内存登录上下文和任务状态的一项硬约束。进程异常退出或被强制终止时，内存中的登录上下文和同步任务会丢失；重启后应检查最新同步记录和心跳状态，必要时重新登录，并确认没有活动同步任务后再手动发起新的同步。

## 数据库历史保留与监控

SQLite 数据库只保留 `work_orders` 的当前快照；`raw_json` 会随当前工单更新，不建立原始响应历史链。后台数据库维护线程按 `config.yaml` 中的保留周期批量删除过期工单、事件和已完成的同步运行记录，未完成的 `sync_runs` 永远不会被清理。

相关配置：

- `work_order_retention_days`、`work_order_event_retention_days`、`sync_run_retention_days`：保留天数，`0` 表示永久保留；
- `database_cleanup_interval_seconds`、`database_cleanup_batch_size`：维护周期和单批删除上限；
- `database_max_size_mb`、`wal_max_size_mb`：数据库文件和 WAL 文件告警阈值，WAL 超阈值时执行被动 checkpoint。

维护服务在应用工厂创建并自动启动，关闭应用时会先停止同步任务和维护线程，再关闭数据库。应通过应用日志监控清理数量、数据库文件大小和 WAL 告警；默认不频繁执行 `VACUUM`，如需回收删除后的磁盘空间应在离线维护窗口执行。


## Windows 可执行文件发布

GitHub Actions 会在 `main`/`master` 的推送、Pull Request 和手动触发时运行测试并构建 Windows 单文件程序。构建成功后会产生两个 Artifact：

- `GridRealtimeMonitor-windows-executable`：仅包含 `GridRealtimeMonitor.exe`；
- `GridRealtimeMonitor-windows-package`：包含 exe、`config.yaml`、本部署说明和 SHA-256 校验文件的 ZIP 包。

创建并推送 `v*` 格式的版本 tag（例如 `v0.1.0`）后，工作流会构建并将 ZIP 包及其 `.sha256` 校验文件上传到对应的 GitHub Release；如果 Release 尚不存在，工作流会自动创建并生成发布说明。下载 ZIP 后解压到独立目录，确保 `GridRealtimeMonitor.exe` 与 `config.yaml` 位于同一目录，再启动程序。设置页保存配置需要该目录和 `config.yaml` 对当前用户可写；不要直接放在 `Program Files`、受控文件夹或其他只读目录中，建议解压到用户有 Modify 权限的目录。若出现配置写入失败，请检查目录和文件权限，并关闭可能锁定 `config.yaml` 的编辑器、云同步软件或安全软件。不要把数据库、日志、Cookies 或其他运行数据放进发布包。

macOS/Linux 可使用源码方式运行；当前 GitHub Actions 仅生成 Windows 可执行文件。

验证下载包：

```powershell
Get-FileHash .\GridRealtimeMonitor-windows.zip -Algorithm SHA256
Get-Content .\GridRealtimeMonitor-windows.zip.sha256
```

两者的哈希值必须一致。
