from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


APP_NAME = "GridRealtimeMonitor"
DEFAULT_BASE_URL = "https://nqi.gmcc.net:20443"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 5000
TARGET_PORTAL_PID = "JZFXYHLC"
TARGET_MODULE = "pro-wfm-biz-client-fak"
DEFAULT_TARGET_PROCESS_TITLE = "微网格实时优化流程"
DEFAULT_TARGET_PROCESS_KEY = "proc_wwg_ssyhlc"
DEFAULT_TARGET_TITLE_KEYWORDS = ("阳江",)

# Compatibility exports for callers that still import the old names.
DEFAULT_TARGET_TITLE_KEYWORD = DEFAULT_TARGET_TITLE_KEYWORDS[0]
TARGET_PROCESS_TITLE = DEFAULT_TARGET_PROCESS_TITLE
TARGET_PROCESS_KEY = DEFAULT_TARGET_PROCESS_KEY
TARGET_TITLE_KEYWORD = DEFAULT_TARGET_TITLE_KEYWORD


def matches_title_keywords(title: str, keywords: tuple[str, ...] | list[str]) -> bool:
    return any(keyword in title for keyword in keywords)


@dataclass(frozen=True)
class AppConfig:
    base_url: str = DEFAULT_BASE_URL
    web_host: str = DEFAULT_WEB_HOST
    web_port: int = DEFAULT_WEB_PORT
    poll_interval_seconds: int = 60
    heartbeat_interval_seconds: int = 300
    lookback_hours: int = 24
    page_size: int = 50
    auto_sync: bool = True
    ca_bundle: str | None = None
    target_process_title: str = DEFAULT_TARGET_PROCESS_TITLE
    target_process_key: str = DEFAULT_TARGET_PROCESS_KEY
    target_title_keywords: tuple[str, ...] = DEFAULT_TARGET_TITLE_KEYWORDS
    auto_claim_pending_tasks: bool = False

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("base_url 必须是 HTTPS 地址")
        if not isinstance(self.web_host, str) or not self.web_host.strip():
            raise ValueError("web_host 不能为空")
        if not 1 <= self.web_port <= 65535:
            raise ValueError("web_port 超出允许范围")
        if not 5 <= self.poll_interval_seconds <= 3600:
            raise ValueError("poll_interval_seconds 超出允许范围")
        if not 30 <= self.heartbeat_interval_seconds <= 86400:
            raise ValueError("heartbeat_interval_seconds 超出允许范围")
        if not 1 <= self.lookback_hours <= 720:
            raise ValueError("lookback_hours 超出允许范围")
        if not 10 <= self.page_size <= 500:
            raise ValueError("page_size 超出允许范围")
        if not isinstance(self.target_process_title, str) or not self.target_process_title.strip():
            raise ValueError("target_process_title 不能为空")
        if not isinstance(self.target_process_key, str) or not self.target_process_key.strip():
            raise ValueError("target_process_key 不能为空")
        if not isinstance(self.target_title_keywords, tuple):
            raise ValueError("target_title_keywords 必须是非空字符串列表")
        if not self.target_title_keywords or any(
            not isinstance(keyword, str) or not keyword.strip() for keyword in self.target_title_keywords
        ):
            raise ValueError("target_title_keywords 必须是非空字符串列表")
        if not isinstance(self.auto_claim_pending_tasks, bool):
            raise ValueError("auto_claim_pending_tasks 必须是布尔值")
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            raise ValueError("指定的 CA 文件不存在")

    @property
    def origin(self) -> str:
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"


class AppPaths:
    """应用数据路径，不依赖桌面 UI 框架。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or self._default_root()).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.project_root = self._project_root()
        self.yaml = self.project_root / "config.yaml"
        self.settings = self.root / "settings.json"
        self.database = self.root / "monitor.sqlite3"
        self.log = self.root / "app.log"

    @staticmethod
    def _project_root() -> Path:
        """Locate bundled resources in both source and PyInstaller layouts."""
        bundle_root = Path(getattr(__import__("sys"), "_MEIPASS", Path(__file__).resolve().parents[1]))
        return bundle_root

    @staticmethod
    def _default_root() -> Path:
        override = os.environ.get("GRID_MONITOR_DATA_DIR")
        if override:
            return Path(override)
        if getattr(__import__("sys"), "frozen", False):
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                return Path(local_app_data) / "GridRealtimeMonitor"
        # 默认将运行数据放在项目根目录的 data/ 下；环境变量可覆盖该位置。
        return Path(__file__).resolve().parents[1] / "data"


class ConfigStore:
    def __init__(self, paths: AppPaths, logger: logging.Logger | None = None) -> None:
        self.paths = paths
        self.logger = logger or logging.getLogger(__name__)

    def load(self) -> AppConfig:
        values: dict[str, Any] = {
            "base_url": DEFAULT_BASE_URL,
            "web_host": DEFAULT_WEB_HOST,
            "web_port": DEFAULT_WEB_PORT,
            "poll_interval_seconds": 60,
            "heartbeat_interval_seconds": 300,
            "lookback_hours": 24,
            "page_size": 50,
            "auto_sync": True,
            "ca_bundle": None,
            "target_process_title": DEFAULT_TARGET_PROCESS_TITLE,
            "target_process_key": DEFAULT_TARGET_PROCESS_KEY,
            "target_title_keywords": DEFAULT_TARGET_TITLE_KEYWORDS,
            "auto_claim_pending_tasks": False,
        }
        if self.paths.yaml.exists():
            try:
                raw_yaml = yaml.safe_load(self.paths.yaml.read_text(encoding="utf-8"))
                if raw_yaml is None:
                    raw_yaml = {}
                if not isinstance(raw_yaml, dict):
                    raise ValueError("YAML 配置顶层必须是对象")
                values.update(raw_yaml)
                if "target_title_keywords" not in raw_yaml and "target_title_keyword" in raw_yaml:
                    values["target_title_keywords"] = (raw_yaml["target_title_keyword"],)
                values.pop("target_title_keyword", None)
            except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
                self.logger.warning("根目录 YAML 配置无效，将使用默认配置: %s", type(exc).__name__)
        if self.paths.settings.exists():
            try:
                raw = json.loads(self.paths.settings.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("设置文件格式错误")
                for key in (
                    "base_url", "poll_interval_seconds", "heartbeat_interval_seconds",
                    "lookback_hours", "page_size", "auto_sync", "ca_bundle",
                ):
                    if key in raw:
                        values[key] = raw[key]
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self.logger.warning("本地设置无效，将忽略运行时设置: %s", type(exc).__name__)
        try:
            for key in ("web_port", "poll_interval_seconds", "heartbeat_interval_seconds", "lookback_hours", "page_size"):
                values[key] = int(values[key])
            if isinstance(values["target_title_keywords"], list):
                values["target_title_keywords"] = tuple(values["target_title_keywords"])
            if not isinstance(values["auto_sync"], bool):
                raise ValueError("auto_sync 必须是布尔值")
            if not isinstance(values["auto_claim_pending_tasks"], bool):
                raise ValueError("auto_claim_pending_tasks 必须是布尔值")
            values["ca_bundle"] = values.get("ca_bundle") or None
            return AppConfig(**values)
        except (ValueError, TypeError) as exc:
            self.logger.warning("配置值无效，将使用默认配置: %s", type(exc).__name__)
            return AppConfig()

    def save(self, config: AppConfig) -> None:
        payload = {
            "base_url": config.base_url,
            "poll_interval_seconds": config.poll_interval_seconds,
            "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
            "lookback_hours": config.lookback_hours,
            "page_size": config.page_size,
            "auto_sync": config.auto_sync,
            "ca_bundle": config.ca_bundle,
        }
        temp = self.paths.settings.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.paths.settings)


SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passwd|captcha|msgCode|CASTGC|JSESSIONID|TGC|ticket|token|cookie)"
)


def configure_logging(paths: AppPaths) -> logging.Logger:
    logger = logging.getLogger("grid_monitor")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    class RedactingFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            message = super().format(record)
            return SENSITIVE_KEY_RE.sub("[REDACTED]", message)

    handler = logging.FileHandler(paths.log, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    logger.addHandler(console)
    return logger


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    return {
        "base_url": config.base_url,
        "web_host": config.web_host,
        "web_port": config.web_port,
        "poll_interval_seconds": config.poll_interval_seconds,
        "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
        "lookback_hours": config.lookback_hours,
        "page_size": config.page_size,
        "auto_sync": config.auto_sync,
        "ca_bundle": config.ca_bundle,
        "target_process_title": config.target_process_title,
        "target_process_key": config.target_process_key,
        "target_title_keywords": list(config.target_title_keywords),
        "auto_claim_pending_tasks": config.auto_claim_pending_tasks,
    }
