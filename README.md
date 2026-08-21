# 网格实时流程监控

面向“微网格实时优化流程”的单进程 Web 监控工具。应用负责登录上游平台、同步工单、查看统计信息、管理待领取任务，并将业务快照保存到 SQLite。

## 主要功能

- CAS 登录、验证码校验、短信登录和登录会话恢复；
- 使用操作系统凭据管理器保存上游 Cookies，不将 Cookies 写入项目文件；
- 按 `config.yaml` 中的标题关键词筛选目标城市工单；
- 工单分页同步、看板统计、工单详情和待领取任务管理；
- SQLite 工单快照、事件和同步运行记录；
- 后台数据库维护、保留周期和数据库大小监控；
- Windows 单文件可执行程序构建和 GitHub Release 发布。

## 配置规则

业务配置**只读取一个 `config.yaml` 文件**：

- 源码运行：读取项目根目录的 `config.yaml`；
- Windows 打包版：读取 `GridRealtimeMonitor.exe` 同目录的 `config.yaml`；
- 不读取环境变量指定的配置文件；
- 不读取 `settings.json`；
- 不使用代码默认值补齐缺失字段；
- 配置文件缺失、为空、格式错误、字段缺失、字段无效或包含未知字段时，应用拒绝启动。

当前示例配置只包含“阳江”：

```yaml
target_title_keywords:
  - 阳江
```

`config.yaml` 必须包含完整字段。修改配置后需要重启应用。历史数据库中的其他城市记录不会被删除，但在当前关键词范围下不会通过页面或 API 返回。

## 源码运行

要求 Python 3.10 或更高版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest
python run.py
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest
python run.py
```

开发服务器默认监听 `config.yaml` 中的 `web_host` 和 `web_port`。生产环境不要使用 Flask 开发服务器，应使用单进程、单 worker 的 Waitress、Gunicorn 或 uWSGI。详细说明见 [`DEPLOYMENT.md`](DEPLOYMENT.md)。

## 数据和登录会话位置

默认运行数据目录：

- 源码运行：项目根目录下的 `data/`；
- Windows 打包版：`%LOCALAPPDATA%\GridRealtimeMonitor\`。

目录中可能包含：

```text
monitor.sqlite3
app.log
.secret_key
```
登录 Cookies 不保存在上述目录，而是保存到当前用户的操作系统凭据管理器中。删除保存账号时，程序会删除对应的凭据。

如需指定运行数据目录，可设置 `GRID_MONITOR_DATA_DIR`；该变量只影响数据库、日志和密钥等运行数据位置，不影响业务配置来源。

## Windows 打包和下载

GitHub Actions 工作流位于 `.github/workflows/build-windows.yml`，会：

1. 安装依赖并运行完整测试；
2. 使用 PyInstaller 构建 `GridRealtimeMonitor.exe`；
3. 将 `webapp/templates` 和 `webapp/static` 内嵌到可执行文件；
4. 将外部 `config.yaml`、`DEPLOYMENT.md` 和 exe 组成 ZIP；
5. 生成 ZIP 的 SHA-256 校验文件；
6. 上传 Actions Artifact。

发布 ZIP 的内容：

```text
GridRealtimeMonitor-windows/
├── GridRealtimeMonitor.exe
├── config.yaml
└── DEPLOYMENT.md
```

`config.yaml` 不会内嵌到 exe，必须与 exe 放在同一目录。数据库、日志、Cookies、`.secret_key` 和其他本机运行数据不会被打包。

推送 `v*` 格式的 tag（例如 `v0.1.0`）后，工作流会自动构建 Windows 包并上传到对应的 GitHub Release：

```bash
git tag v0.1.0
git push origin v0.1.0
```

下载后可使用以下命令校验 ZIP：

```powershell
Get-FileHash .\GridRealtimeMonitor-windows.zip -Algorithm SHA256
Get-Content .\GridRealtimeMonitor-windows.zip.sha256
```

## GitHub Actions

普通推送、Pull Request 和手动触发会执行测试并生成构建 Artifact。版本 tag 会额外执行 Release 发布流程。当前流程只生成 Windows 可执行文件，macOS/Linux 请使用源码运行。

## 项目结构

```text
backend/       上游认证、平台客户端、同步和数据库层
shared/        配置、模型和通用工具
webapp/        Flask 应用、路由、模板和前端资源
tests/         自动化测试
config.yaml    唯一业务配置文件
run.py         Flask/WSGI 入口
DEPLOYMENT.md  生产部署和发布说明
```

## 许可证

本仓库当前未声明开源许可证。使用、修改或再发布前，请先确认仓库所有者的授权范围。
