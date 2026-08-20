from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, fields
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
    work_order_retention_days: int = 90
    work_order_event_retention_days: int = 180
    sync_run_retention_days: int = 90
    database_cleanup_interval_seconds: int = 3600
    database_cleanup_batch_size: int = 500
    database_max_size_mb: int = 1024
    wal_max_size_mb: int = 256

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
        retention_fields = (
            self.work_order_retention_days,
            self.work_order_event_retention_days,
            self.sync_run_retention_days,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in retention_fields):
            raise ValueError("保留周期必须是大于等于 0 的整数")
        maintenance_integer_fields = (
            ("database_cleanup_interval_seconds", self.database_cleanup_interval_seconds, 60, 86400),
            ("database_cleanup_batch_size", self.database_cleanup_batch_size, 1, 10000),
            ("database_max_size_mb", self.database_max_size_mb, 0, None),
            ("wal_max_size_mb", self.wal_max_size_mb, 0, None),
        )
        for name, value, minimum, maximum in maintenance_integer_fields:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} 必须是整数")
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError(f"{name} 超出允许范围")

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
    _LOCAL_SETTINGS_KEYS = frozenset({
        "base_url",
        "poll_interval_seconds",
        "heartbeat_interval_seconds",
        "lookback_hours",
        "page_size",
        "auto_sync",
        "ca_bundle",
    })

    def __init__(self, paths: AppPaths, logger: logging.Logger | None = None) -> None:
        self.paths = paths
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def _default_values() -> dict[str, Any]:
        config = AppConfig()
        return {field.name: getattr(config, field.name) for field in fields(AppConfig)}

    @staticmethod
    def _coerce_field(name: str, value: Any) -> Any:
        if name in {
            "web_port", "poll_interval_seconds", "heartbeat_interval_seconds", "lookback_hours", "page_size",
            "work_order_retention_days", "work_order_event_retention_days", "sync_run_retention_days",
            "database_cleanup_interval_seconds", "database_cleanup_batch_size", "database_max_size_mb", "wal_max_size_mb",
        }:
            if isinstance(value, bool):
                raise ValueError("必须是整数")
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("必须是整数") from exc
        if name in {"auto_sync", "auto_claim_pending_tasks"}:
            if not isinstance(value, bool):
                raise ValueError("必须是布尔值")
            return value
        if name == "ca_bundle":
            if value in (None, ""):
                return None
            if not isinstance(value, str):
                raise ValueError("必须是文件路径或 null")
            return value
        if name == "target_title_keywords":
            if isinstance(value, list):
                value = tuple(value)
            if not isinstance(value, tuple) or not value or any(
                not isinstance(keyword, str) or not keyword.strip() for keyword in value
            ):
                raise ValueError("必须是非空字符串列表")
            return value
        if name in {"base_url", "web_host", "target_process_title", "target_process_key"}:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("必须是非空字符串")
            return value
        raise ValueError("未知配置字段")

    @staticmethod
    def _validate_field(name: str, value: Any) -> None:
        if name == "base_url":
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("必须是 HTTPS 地址")
        elif name == "web_port" and not 1 <= value <= 65535:
            raise ValueError("超出允许范围[1,65535]")
        elif name == "poll_interval_seconds" and not 5 <= value <= 3600:
            raise ValueError("超出允许范围[5,3600]")
        elif name == "heartbeat_interval_seconds" and not 30 <= value <= 86400:
            raise ValueError("超出允许范围[30,86400]")
        elif name == "lookback_hours" and not 1 <= value <= 720:
            raise ValueError("超出允许范围[1,720]")
        elif name == "page_size" and not 10 <= value <= 500:
            raise ValueError("超出允许范围[10,500]")
        elif name in {"work_order_retention_days", "work_order_event_retention_days", "sync_run_retention_days"} and value < 0:
            raise ValueError("必须大于等于 0")
        elif name == "database_cleanup_interval_seconds" and not 60 <= value <= 86400:
            raise ValueError("超出允许范围[60,86400]")
        elif name == "database_cleanup_batch_size" and not 1 <= value <= 10000:
            raise ValueError("超出允许范围[1,10000]")
        elif name in {"database_max_size_mb", "wal_max_size_mb"} and value < 0:
            raise ValueError("必须大于等于 0")
        elif name == "ca_bundle" and value and not Path(value).is_file():
            raise ValueError("指定的 CA 文件不存在")

    def _parse_source(self, path: Path, source: str) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
            raw = yaml.safe_load(text) if source == "config.yaml" else json.loads(text)
            if raw is None:
                return {}
            if not isinstance(raw, dict):
                raise ValueError("顶层必须是对象")
            return dict(raw)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, yaml.YAMLError) as exc:
            self.logger.warning("配置源解析失败: source=%s reason=%s", source, type(exc).__name__)
            return {}

    def _apply_source(
        self,
        values: dict[str, Any],
        raw: dict[str, Any],
        source: str,
        allowed_keys: set[str],
    ) -> None:
        if source == "config.yaml" and "target_title_keywords" not in raw and "target_title_keyword" in raw:
            raw = dict(raw)
            raw["target_title_keywords"] = (raw["target_title_keyword"],)
            raw.pop("target_title_keyword", None)
        for key in sorted(set(raw) - allowed_keys):
            self.logger.warning("忽略未知配置字段: source=%s field=%s", source, key)
        for key in sorted(set(raw) & allowed_keys):
            try:
                candidate = self._coerce_field(key, raw[key])
                self._validate_field(key, candidate)
            except (TypeError, ValueError) as exc:
                self.logger.warning(
                    "配置字段无效: source=%s field=%s reason=%s fallback=default",
                    source, key, str(exc),
                )
                continue
            values[key] = candidate

    def load(self) -> AppConfig:
        allowed_keys = {field.name for field in fields(AppConfig)}

        # Priority is config.yaml > code defaults > local settings.json.
        # Parse the lowest layer for diagnostics, but never let it replace a
        # code default; local settings are retained for backward-compatible
        # reading and may be used if a future field has no code default.
        values: dict[str, Any] = {}
        if self.paths.settings.exists():
            self._apply_source(
                values,
                self._parse_source(self.paths.settings, "settings.json"),
                "settings.json",
                set(self._LOCAL_SETTINGS_KEYS),
            )
        values.update(self._default_values())
        if self.paths.yaml.exists():
            self._apply_source(
                values,
                self._parse_source(self.paths.yaml, "config.yaml"),
                "config.yaml",
                allowed_keys,
            )

        try:
            return AppConfig(**values)
        except (TypeError, ValueError) as exc:
            self.logger.error("内部配置默认值无效: reason=%s", type(exc).__name__)
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
    r"(?i)(?:password|passwd|captcha|msgcode|castgc|jsessionid|tgc|ticket|token|cookie|"
    r"authorization|set-cookie|api[_-]?key|secret|sms[_-]?code|verification[_-]?code|"
    r"session[_-]?id|access[_-]?token|refresh[_-]?token)"
)
_SENSITIVE_KEY_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![\w-])(?:password|passwd|captcha|msgcode|castgc|jsessionid|tgc|ticket|token|cookie|"
    r"authorization|set-cookie|api[_-]?key|secret|sms[_-]?code|verification[_-]?code|session[_-]?id|"
    r"access[_-]?token|refresh[_-]?token)[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
_SENSITIVE_UNQUOTED_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![\w-])(?:password|passwd|captcha|msgcode|castgc|jsessionid|tgc|ticket|token|cookie|"
    r"authorization|set-cookie|api[_-]?key|secret|sms[_-]?code|verification[_-]?code|session[_-]?id|"
    r"access[_-]?token|refresh[_-]?token)(?:[\"']?\s*[:=]\s*)"
    r"(?![\"'])"
    r")(?P<value>(?!\[REDACTED\])[^\s,;&}\]]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)(?!\[REDACTED\])[^\s,;]+")
_COOKIE_HEADER_RE = re.compile(r"(?i)(\b(?:Cookie|Set-Cookie):\s*)([^\r\n]+)")


def _redact_quoted(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"


def _redact_cookie_header(match: re.Match[str]) -> str:
    header, value = match.groups()
    return header + redact_sensitive_data(value)


def redact_sensitive_data(text: str) -> str:
    """Redact credential values in log messages, headers, URLs, and tracebacks."""
    if not text:
        return text
    cookie_values: list[str] = []

    def hold_cookie(match: re.Match[str]) -> str:
        cookie_values.append(_redact_cookie_header(match))
        return f"__REDACTED_COOKIE_{len(cookie_values) - 1}__"

    redacted = _COOKIE_HEADER_RE.sub(hold_cookie, text)
    redacted = _BEARER_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _SENSITIVE_KEY_VALUE_RE.sub(_redact_quoted, redacted)
    redacted = _SENSITIVE_UNQUOTED_VALUE_RE.sub(r"\g<prefix>[REDACTED]", redacted)
    for index, value in enumerate(cookie_values):
        redacted = redacted.replace(f"__REDACTED_COOKIE_{index}__", value)
    return redacted


def configure_logging(paths: AppPaths) -> logging.Logger:
    logger = logging.getLogger("grid_monitor")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_path = paths.log.resolve()
    existing_file_handlers = [
        handler for handler in logger.handlers
        if isinstance(handler, logging.FileHandler)
    ]
    if existing_file_handlers and all(
        Path(handler.baseFilename).resolve() == log_path and handler.stream is not None
        for handler in existing_file_handlers
    ):
        return logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    class RedactingFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            message = super().format(record)
            return redact_sensitive_data(message)

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
        "work_order_retention_days": config.work_order_retention_days,
        "work_order_event_retention_days": config.work_order_event_retention_days,
        "sync_run_retention_days": config.sync_run_retention_days,
        "database_cleanup_interval_seconds": config.database_cleanup_interval_seconds,
        "database_cleanup_batch_size": config.database_cleanup_batch_size,
        "database_max_size_mb": config.database_max_size_mb,
        "wal_max_size_mb": config.wal_max_size_mb,
    }
