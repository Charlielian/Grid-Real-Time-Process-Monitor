"""Web 会话上下文、持久化 Cookie、账号索引和会话注册表服务。

浏览器 session 只保存短标识，实际平台客户端和 Cookie 存在进程内注册表中。
注册表使用锁保护并发访问；关闭或失效时清理回调与资源，避免多个请求共享
已被释放的会话对象。已保存账号的元数据（显示名、最近使用和心跳状态）放在
系统 keyring 的固定条目里，随 Cookie 一起跨进程保留。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

import keyring
import requests

from backend.auth.cas_client import AuthError, CasClient, SessionFactory, SessionExpired
from backend.auth.cookie_store import CookieStore
from backend.platform.client import PlatformClient
from shared.config import AppConfig
from shared.models import UserInfo


class PersistentCookieStore(CookieStore):
    """Store upstream session cookies in the current user's OS keyring."""

    service_prefix = "grid-realtime-monitor-web"

    def __init__(self, config: AppConfig, logger: Any) -> None:
        origin_key = hashlib.sha256(config.origin.encode("utf-8")).hexdigest()[:20]
        super().__init__(f"{self.service_prefix}-{origin_key}", logger)


class AccountIndexStore:
    """Persist saved-account metadata in the OS keyring as a single JSON list.

    Only metadata lives here; session cookies are stored per-login_id by
    ``CookieStore`` under the same keyring service. Keyring failures degrade to
    empty reads and no-op writes, never crashing the login flow.
    """

    service_prefix = "grid-realtime-monitor-web"
    username = "index"
    field_order = (
        "login_id", "display_name", "last_used_at", "last_heartbeat_at",
        "heartbeat_status", "consecutive_failures", "last_error",
    )

    def __init__(self, config: AppConfig, logger: Any) -> None:
        origin_key = hashlib.sha256(config.origin.encode("utf-8")).hexdigest()[:20]
        self.service_name = f"{self.service_prefix}-{origin_key}"
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _read(self) -> list[dict[str, Any]]:
        """读取账号索引列表；密钥环或 JSON 异常时退回空列表。"""
        try:
            raw = keyring.get_password(self.service_name, self.username)
        except Exception as exc:
            self.logger.warning("读取保存账号索引失败: %s", type(exc).__name__)
            return []
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.logger.warning("保存账号索引格式无效")
            return []
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def _write(self, records: list[dict[str, Any]]) -> bool:
        """覆写账号索引；密钥环异常时只记录类型并返回失败。"""
        try:
            keyring.set_password(
                self.service_name,
                self.username,
                json.dumps(records, ensure_ascii=False, separators=(",", ":")),
            )
            return True
        except Exception as exc:
            self.logger.warning("保存账号索引失败: %s", type(exc).__name__)
            return False

    @staticmethod
    def _normalize(record: dict[str, Any]) -> dict[str, Any]:
        """补齐缺失字段并保持固定字段顺序，便于前端与心跳读写。"""
        normalized = {field_name: record.get(field_name) for field_name in AccountIndexStore.field_order}
        return normalized

    def upsert(self, login_id: str, display_name: str = "") -> None:
        """记录一次登录/恢复；已存在的账号只更新显示名和最近使用时间。"""
        if not login_id:
            return
        records = self._read()
        now = self._now()
        for record in records:
            if record.get("login_id") == login_id:
                record["display_name"] = display_name
                record["last_used_at"] = now
                self._write(records)
                return
        records.append(self._normalize({
            "login_id": login_id,
            "display_name": display_name,
            "last_used_at": now,
            "last_heartbeat_at": None,
            "heartbeat_status": "unknown",
            "consecutive_failures": 0,
            "last_error": None,
        }))
        self._write(records)

    def get(self, login_id: str) -> dict[str, Any] | None:
        """按账号返回完整索引记录；不存在或索引损坏返回 ``None``。"""
        for record in self._read():
            if record.get("login_id") == login_id:
                return self._normalize(record)
        return None

    def list(self) -> list[dict[str, Any]]:
        """按最近使用时间倒序返回全部账号索引记录。"""
        records = [self._normalize(record) for record in self._read()]
        return sorted(records, key=lambda record: str(record.get("last_used_at") or ""), reverse=True)

    def remove(self, login_id: str) -> None:
        """从索引中移除账号；不存在的账号保持幂等。"""
        records = self._read()
        remaining = [record for record in records if record.get("login_id") != login_id]
        if len(remaining) != len(records):
            self._write(remaining)

    def has(self, login_id: str) -> bool:
        """判断账号是否存在于索引中。"""
        return self.get(login_id) is not None

    def update_heartbeat(
        self, login_id: str, *, status: str, heartbeat_at: str, error: str | None, consecutive_failures: int
    ) -> None:
        """写入一次心跳结果；账号不在索引中时忽略。"""
        records = self._read()
        for record in records:
            if record.get("login_id") == login_id:
                record["last_heartbeat_at"] = heartbeat_at
                record["heartbeat_status"] = status
                record["consecutive_failures"] = consecutive_failures
                record["last_error"] = error
                self._write(records)
                return


@dataclass
class WebAuthContext:
    context_id: str
    session: requests.Session
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    user: UserInfo | None = None
    captcha_page: Any | None = None
    captcha_verified: bool = False
    captcha_expires_at: float = 0.0
    sms_sent_at: float = 0.0
    username: str = ""

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def expired(self, ttl_seconds: int) -> bool:
        return time.monotonic() - self.last_used > ttl_seconds


class SessionRegistry:
    """In-memory service-side session registry for a single Flask process."""

    def __init__(
        self,
        config: AppConfig,
        logger: Any,
        ttl_seconds: int = 1800,
        *,
        cleanup_interval_seconds: float | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.ttl_seconds = ttl_seconds
        self._contexts: dict[str, WebAuthContext] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._cleanup_stop = threading.Event()
        interval = cleanup_interval_seconds or min(max(ttl_seconds / 2, 1), 60)
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            args=(max(0.1, interval),),
            name="auth-session-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()

    def _cleanup_loop(self, interval: float) -> None:
        while not self._cleanup_stop.wait(interval):
            self.cleanup()

    def _dispose(self, context: WebAuthContext, *, timeout: float | None = None) -> None:
        context.user = None
        context.captcha_page = None
        context.captcha_verified = False
        context.username = ""
        try:
            context.session.cookies.clear()
            context.session.close()
        except Exception:
            self.logger.exception("关闭登录会话失败: context_id=%s", context.context_id)

    def create(self) -> WebAuthContext:
        with self._lock:
            if self._closed:
                raise RuntimeError("会话注册表已关闭")
        context_id = secrets.token_urlsafe(32)
        context = WebAuthContext(
            context_id=context_id,
            session=SessionFactory(self.config, self.logger).create(),
        )
        with self._lock:
            if self._closed:
                context.session.close()
                raise RuntimeError("会话注册表已关闭")
            self._contexts[context_id] = context
        return context

    def get(self, context_id: str | None) -> WebAuthContext | None:
        if not context_id:
            return None
        expired: WebAuthContext | None = None
        with self._lock:
            context = self._contexts.get(context_id)
            if context is None:
                return None
            if context.expired(self.ttl_seconds):
                expired = self._contexts.pop(context_id, None)
            else:
                context.touch()
                return context
        if expired:
            self._dispose(expired)
        return None

    def remove(self, context_id: str | None) -> None:
        if not context_id:
            return
        with self._lock:
            context = self._contexts.pop(context_id, None)
        if context:
            self._dispose(context)

    def cleanup(self) -> None:
        with self._lock:
            expired = [
                self._contexts.pop(key)
                for key, value in list(self._contexts.items())
                if value.expired(self.ttl_seconds)
            ]
        for context in expired:
            self._dispose(context)

    def shutdown(self, timeout: float | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            contexts = list(self._contexts.values())
            self._contexts.clear()
        deadline = time.monotonic() + timeout if timeout is not None else None
        self._cleanup_stop.set()
        if self._cleanup_thread is not threading.current_thread():
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            self._cleanup_thread.join(timeout=remaining)
        for context in contexts:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            self._dispose(context, timeout=remaining)


class WebAuthService:
    def __init__(self, registry: SessionRegistry, logger: Any) -> None:
        self.registry = registry
        self.logger = logger
        self.cookies = PersistentCookieStore(registry.config, logger)
        self.accounts = AccountIndexStore(registry.config, logger)

    def update_config(self, config: AppConfig) -> None:
        self.registry.config = config
        self.cookies = PersistentCookieStore(config, self.logger)
        self.accounts = AccountIndexStore(config, self.logger)

    def context(self, context_id: str | None) -> WebAuthContext:
        context = self.registry.get(context_id)
        return context or self.registry.create()

    def has_saved_session(self, login_id: str | None) -> bool:
        return bool(login_id and self.cookies.has(login_id))

    def restore(self, context: WebAuthContext, login_id: str) -> UserInfo:
        if not self.cookies.load(login_id, context.session):
            raise SessionExpired("没有可用的保存会话")
        client = CasClient(self.registry.config, context.session, self.logger)
        try:
            user = client.check_session(expected_login_id=login_id)
        except SessionExpired:
            self.cookies.clear(login_id)
            self.registry.remove(context.context_id)
            raise
        except requests.RequestException:
            self.logger.warning("恢复登录会话时上游网络暂不可用")
            raise
        context.user = user
        # 恢复的保存会话同样缺少业务上下文（engine-extend 的 JSESSIONID），
        # 这里重新进入一次门户补齐；失败时按会话失效处理，避免带残缺会话继续。
        try:
            client.enter_portal()
        except (SessionExpired, AuthError):
            self.cookies.clear(login_id)
            self.registry.remove(context.context_id)
            raise SessionExpired("恢复登录会话时进入业务门户失败")
        except requests.RequestException:
            self.logger.warning("恢复登录会话时进入业务门户网络暂不可用")
        context.touch()
        self.accounts.upsert(user.login_id, user.display_name)
        return user

    def list_saved_accounts(self) -> list[dict[str, Any]]:
        return self.accounts.list()

    def remove_saved_account(self, login_id: str) -> None:
        self.cookies.clear(login_id)
        self.accounts.remove(login_id)

    def captcha(self, context: WebAuthContext) -> bytes:
        client = CasClient(self.registry.config, context.session, self.logger)
        context.captcha_page = client.get_login_page()
        context.captcha_verified = False
        context.captcha_expires_at = time.monotonic() + 300
        context.touch()
        return client.get_captcha()

    def verify_captcha(self, context: WebAuthContext, username: str, password: str, captcha: str) -> bool:
        if not captcha.strip() or time.monotonic() > context.captcha_expires_at:
            return False
        page = context.captcha_page
        if page is None:
            page = CasClient(self.registry.config, context.session, self.logger).get_login_page()
            context.captcha_page = page
        result = CasClient(self.registry.config, context.session, self.logger).verify_captcha(
            username, password, captcha, page
        )
        context.captcha_verified = result
        context.username = username
        context.touch()
        return result

    def send_sms(self, context: WebAuthContext, username: str, password: str) -> bool:
        page = context.captcha_page
        if not context.captcha_verified or context.username != username or page is None:
            return False
        result = CasClient(self.registry.config, context.session, self.logger).send_sms(
            username, password, page
        )
        if result:
            context.sms_sent_at = time.monotonic()
            context.touch()
        return result

    def login(self, context: WebAuthContext, username: str, password: str, captcha: str, sms_code: str) -> UserInfo:
        if not context.captcha_verified or context.username != username or not context.sms_sent_at:
            raise ValueError("请先完成图形验证码校验并发送短信")
        client = CasClient(self.registry.config, context.session, self.logger)
        user = client.login(username, password, captcha, sms_code)
        context.user = user
        context.captcha_page = None
        context.captcha_verified = False
        # 进入业务门户以建立各业务上下文的会话（如 engine-extend 的
        # JSESSIONID）；浏览器也是先打开门户再请求业务接口。失效视为登录不
        # 完整，网络异常则保留用户态，让后续业务请求去发现会话问题。
        try:
            client.enter_portal()
        except (SessionExpired, AuthError):
            context.user = None
            self.registry.remove(context.context_id)
            raise
        except requests.RequestException:
            self.logger.warning("进入业务门户时上游网络暂不可用: %s", username)
        context.touch()
        if not self.cookies.save(user.login_id, context.session):
            self.logger.warning("登录成功但保存会话失败: %s", user.login_id)
        else:
            self.accounts.upsert(user.login_id, user.display_name)
        return user

    def require_user(self, context_id: str | None) -> tuple[WebAuthContext, UserInfo]:
        context = self.registry.get(context_id)
        if context is None or context.user is None:
            raise SessionExpired("请先登录")
        try:
            user = CasClient(self.registry.config, context.session, self.logger).check_session(
                expected_login_id=context.user.login_id
            )
        except SessionExpired:
            self.registry.remove(context.context_id)
            raise
        except requests.RequestException:
            self.logger.warning("检查登录会话时上游网络暂不可用")
            raise
        context.user = user
        context.touch()
        return context, user

    def platform(self, context: WebAuthContext) -> PlatformClient:
        if context.user is None:
            raise SessionExpired("请先登录")
        return PlatformClient(self.registry.config, context.session, self.logger)
