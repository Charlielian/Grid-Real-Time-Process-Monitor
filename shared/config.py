"""应用配置、路径、日志脱敏及 YAML 持久化工具。

配置模型集中定义服务地址、轮询/心跳、分页、流程匹配和数据库保留策略，
构造时执行范围与类型校验。配置文件加载要求字段完整并拒绝未知字段；保存
采用临时文件写入、刷盘后原子替换，以避免进程中断留下半份配置，同时尽量
保留既有 YAML 引号和格式。日志格式化阶段会脱敏认证令牌、Cookie、密码等
敏感值，防止请求异常被记录时泄露凭据。
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError as RuamelYAMLError


APP_NAME = "GridRealtimeMonitor"
DEFAULT_BASE_URL = "https://nqi.gmcc.net:20443"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 5000
TARGET_PORTAL_PID = "JZFXYHLC"
TARGET_MODULE = "pro-wfm-biz-client-fak"
DEFAULT_TARGET_PROCESS_TITLE = "微网格实时优化流程"
DEFAULT_TARGET_PROCESS_KEY = "proc_wwg_ssyhlc"
DEFAULT_TARGET_TITLE_KEYWORDS = ("阳江",)
GUANGDONG_CITIES = (
    "广州", "深圳", "珠海", "汕头", "佛山", "韶关", "湛江", "肇庆", "江门", "茂名",
    "惠州", "梅州", "汕尾", "河源", "阳江", "清远", "东莞", "中山", "潮州", "揭阳", "云浮",
)


def normalize_cities(value: Any) -> tuple[str, ...]:
    """校验并去重广东地市配置，返回稳定顺序的元组。

    ``None`` 表示未筛选并返回空元组；字符串会兼容为单元素输入。非列表/
    元组、空名称和不在白名单的城市均抛出 ``ValueError``。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        raise ValueError("city 必须是广东地市列表")
    normalized: list[str] = []
    for city in value:
        # 白名单校验同时防止空值进入标题匹配条件。
        if not isinstance(city, str) or not city.strip():
            raise ValueError("city 必须是广东地市列表")
        city = city.strip()
        if city not in GUANGDONG_CITIES:
            raise ValueError(f"不支持的地市: {city}")
        if city not in normalized:
            normalized.append(city)
    return tuple(normalized)


def normalize_title_keywords(value: Any) -> tuple[str, ...]:
    """规范化标题片段，不自动合并默认关键词。

    输入必须是非空 list/tuple；每个关键词去除首尾空白并保持首次出现顺序，
    重复项被丢弃，空项或错误类型抛出 ``ValueError``。
    """
    if isinstance(value, list):
        value = tuple(value)
    if not isinstance(value, tuple):
        raise ValueError("target_title_keywords 必须是非空字符串列表")
    normalized: list[str] = []
    for keyword in value:
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("target_title_keywords 必须是非空字符串列表")
        keyword = keyword.strip()
        if keyword not in normalized:
            normalized.append(keyword)
    if not normalized:
        raise ValueError("target_title_keywords 必须是非空字符串列表")
    return tuple(normalized)


# Compatibility exports for callers that still import the old names.
DEFAULT_TARGET_TITLE_KEYWORD = DEFAULT_TARGET_TITLE_KEYWORDS[0]
TARGET_PROCESS_TITLE = DEFAULT_TARGET_PROCESS_TITLE
TARGET_PROCESS_KEY = DEFAULT_TARGET_PROCESS_KEY
TARGET_TITLE_KEYWORD = DEFAULT_TARGET_TITLE_KEYWORD


def matches_title_keywords(title: str, keywords: tuple[str, ...] | list[str]) -> bool:
    """判断标题是否包含任一关键词；空关键词集合始终返回 ``False``。"""
    return any(keyword in title for keyword in keywords)


@dataclass(frozen=True)
class AppConfig:
    """应用运行时不可变配置及其边界校验。"""
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
        """规范化并校验配置，确保网络、轮询、分页和维护参数可安全使用。"""
        # frozen dataclass 仍需在构造阶段把关键词统一成去重元组。
        object.__setattr__(self, "target_title_keywords", normalize_title_keywords(self.target_title_keywords))
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

        # 指定 CA 文件必须在启动时存在，否则请求会在运行中才失败。
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            raise ValueError("指定的 CA 文件不存在")

    @property
    def origin(self) -> str:
        """返回不含路径和查询参数的协议加主机地址。"""
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"


def with_config_updates(config: AppConfig, **updates: Any) -> AppConfig:
    """基于现有配置创建新实例，只替换给定字段并重新执行全部校验。"""
    values = {field.name: getattr(config, field.name) for field in fields(AppConfig)}
    values.update(updates)
    return AppConfig(**values)


class AppPaths:
    """应用数据路径，不依赖桌面 UI 框架。"""

    def __init__(self, root: Path | str | None = None) -> None:
        """初始化应用数据路径；未指定 root 时使用环境和打包布局推导默认值。"""
        self.root = Path(root or self._default_root()).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.project_root = self._project_root()
        self.config_path = self._config_path()
        # Compatibility alias retained for callers and tests.
        self.yaml = self.config_path
        self.database = self.root / "monitor.sqlite3"
        self.log = self.root / "app.log"

    @classmethod
    def _config_path(cls) -> Path:
        """按源码或冻结可执行文件布局定位唯一配置文件。"""
        # The YAML beside the executable (or at the project root in source
        # mode) is the only supported configuration source.
        import sys
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent / "config.yaml"
        return cls._project_root() / "config.yaml"

    @staticmethod
    def _project_root() -> Path:
        """在源码和 PyInstaller 打包布局中定位资源根目录。"""
        bundle_root = Path(getattr(__import__("sys"), "_MEIPASS", Path(__file__).resolve().parents[1]))
        return bundle_root

    @staticmethod
    def _default_root() -> Path:
        """根据环境变量、打包环境和源码布局确定数据目录。"""
        override = os.environ.get("GRID_MONITOR_DATA_DIR")
        if override:
            return Path(override)
        if getattr(__import__("sys"), "frozen", False):
            # 打包为 exe 时，数据库和日志放在 exe 同目录下
            import sys
            return Path(sys.executable).resolve().parent / "data"
        # 默认将运行数据放在项目根目录的 data/ 下；环境变量可覆盖该位置。
        return Path(__file__).resolve().parents[1] / "data"


class ConfigStore:
    """负责配置 YAML 的严格加载、兼容转换和原子保存。"""

    def __init__(self, paths: AppPaths, logger: logging.Logger | None = None) -> None:
        """绑定路径和日志器；实际读写延迟到 ``load``/``save``。"""
        self.paths = paths
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def _coerce_field(name: str, value: Any) -> Any:
        """按字段类型转换 YAML 值，并拒绝未知字段或布尔冒充整数。"""
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
            return normalize_title_keywords(value)
        if name in {"base_url", "web_host", "target_process_title", "target_process_key"}:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("必须是非空字符串")
            return value
        raise ValueError("未知配置字段")

    @staticmethod
    def _validate_field(name: str, value: Any) -> None:
        """校验单个已转换字段的范围和外部文件存在性。"""
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

    def _parse_source(self, path: Path) -> dict[str, Any]:
        """读取 YAML 顶层对象并转换解析错误为带路径的 ``ValueError``。"""
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if raw is None:
                return {}
            if not isinstance(raw, dict):
                raise ValueError("顶层必须是对象")
            return dict(raw)
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            raise ValueError(f"配置文件解析失败: {path}: {type(exc).__name__}") from exc

    def _apply_source(self, values: dict[str, Any], raw: dict[str, Any], allowed_keys: set[str]) -> None:
        """将原始字段转换、校验后写入配置值；兼容旧的单关键词字段名。"""
        # 旧配置只允许一个关键词，读取时转换为新列表字段以保持兼容。
        if "target_title_keywords" not in raw and "target_title_keyword" in raw:
            raw = dict(raw)
            raw["target_title_keywords"] = (raw["target_title_keyword"],)
            raw.pop("target_title_keyword", None)
        for key in sorted(set(raw) - allowed_keys):
            raise ValueError(f"配置文件包含未知字段: {key}")
        for key, value in raw.items():
            try:
                candidate = self._coerce_field(key, value)
                self._validate_field(key, candidate)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"配置字段无效: {key}: {exc}") from exc
            values[key] = candidate

    def load(self) -> AppConfig:
        """加载完整 YAML 配置；文件不存在、为空、缺字段或有未知字段均报错。"""
        allowed_keys = {field.name for field in fields(AppConfig)}
        if not self.paths.yaml.exists():
            raise FileNotFoundError(f"必须提供配置文件: {self.paths.yaml}")
        raw = self._parse_source(self.paths.yaml)
        if not raw:
            raise ValueError(f"配置文件为空或无效: {self.paths.yaml}")
        values: dict[str, Any] = {}
        self._apply_source(values, raw, allowed_keys)
        missing = sorted((allowed_keys - {"target_title_keywords"}) - set(values))
        if missing:
            raise ValueError(f"配置文件缺少字段: {', '.join(missing)}")
        config = AppConfig(**values)
        self.logger.info(
            "配置已加载: path=%s data_dir=%s",
            self.paths.yaml.resolve(), self.paths.root.resolve(),
        )
        return config

    def save(self, config: AppConfig) -> None:
        """原子保存配置并尽量保留既有 YAML 格式和文件权限。

        先写同目录临时文件并 ``fsync``，再用 ``os.replace`` 替换目标；失败时
        清理临时文件并抛出带绝对路径的 ``OSError``，避免破坏原配置。
        """
        target = self.paths.yaml
        payload = config_to_dict(config)
        document: Any
        if target.exists():
            # round-trip 解析失败时退回空文档，但仍以完整配置覆盖受支持字段。
            try:
                roundtrip_yaml = YAML(typ="rt")
                roundtrip_yaml.preserve_quotes = True
                with target.open("r", encoding="utf-8") as source:
                    document = roundtrip_yaml.load(source)
                if not isinstance(document, dict):
                    document = {}
            except (OSError, TypeError, ValueError, yaml.YAMLError, RuamelYAMLError):
                document = {}
        else:
            document = {}

        for key, value in payload.items():
            document[key] = value
        if isinstance(document, dict):
            document.pop("target_title_keyword", None)
            document.pop("target_title_keywords", None)

        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        original_mode: int | None = None
        try:
            original_mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            pass

        temp_name: str | None = None
        try:
            file_descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=parent
            )
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as stream:
                roundtrip_yaml = YAML(typ="rt")
                roundtrip_yaml.preserve_quotes = True
                roundtrip_yaml.default_flow_style = False
                roundtrip_yaml.allow_unicode = True
                roundtrip_yaml.dump(document, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if original_mode is not None:
                os.chmod(temp_name, original_mode)
            os.replace(temp_name, target)
            temp_name = None
        except OSError as exc:
            self.logger.exception("配置文件写入失败: path=%s", target.resolve())
            raise OSError(f"配置文件写入失败: {target.resolve()}: {exc}") from exc
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass


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
    """将带引号敏感值替换为固定占位符。"""
    return f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"


def _redact_cookie_header(match: re.Match[str]) -> str:
    """递归脱敏 Cookie/Set-Cookie 头的值部分。"""
    header, value = match.groups()
    return header + redact_sensitive_data(value)


def redact_sensitive_data(text: str) -> str:
    """脱敏日志、请求头、URL 和 traceback 中的凭据值。"""
    if not text:
        return text
    cookie_values: list[str] = []

    def hold_cookie(match: re.Match[str]) -> str:
        """暂存脱敏 Cookie，避免后续通用正则破坏头部结构。"""
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
    """配置文件日志和控制台日志，并为两者安装统一脱敏格式器。"""
    logger = logging.getLogger("grid_monitor")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_path = paths.log.resolve()
    existing_file_handlers = [
        handler for handler in logger.handlers
        if isinstance(handler, logging.FileHandler)
    ]
    # 已经指向同一路径的文件处理器可复用，避免重复写入同一条日志。
    if existing_file_handlers and all(
        Path(handler.baseFilename).resolve() == log_path and handler.stream is not None
        for handler in existing_file_handlers
    ):
        return logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    class RedactingFormatter(logging.Formatter):
        """在标准日志格式化后统一执行敏感信息脱敏。"""

        def format(self, record: logging.LogRecord) -> str:
            """格式化日志记录并隐藏凭据内容。"""
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
    """将配置转换为可序列化的 YAML 字典，不包含兼容旧字段。"""
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
        "auto_claim_pending_tasks": config.auto_claim_pending_tasks,
        "work_order_retention_days": config.work_order_retention_days,
        "work_order_event_retention_days": config.work_order_event_retention_days,
        "sync_run_retention_days": config.sync_run_retention_days,
        "database_cleanup_interval_seconds": config.database_cleanup_interval_seconds,
        "database_cleanup_batch_size": config.database_cleanup_batch_size,
        "database_max_size_mb": config.database_max_size_mb,
        "wal_max_size_mb": config.wal_max_size_mb,
    }
