from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import keyring
import requests

from backend.auth.cas_client import CasClient, SessionFactory, SessionExpired
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
        self._remove_callback: Any | None = None
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

    def set_remove_callback(self, callback: Any | None) -> None:
        self._remove_callback = callback

    def _cleanup_loop(self, interval: float) -> None:
        while not self._cleanup_stop.wait(interval):
            self.cleanup()

    def _dispose(self, context: WebAuthContext, *, timeout: float | None = None) -> None:
        callback = self._remove_callback
        if callback is not None:
            try:
                callback(context.context_id, timeout=timeout)
            except TypeError:
                callback(context.context_id)
            except Exception:
                self.logger.exception("清理会话关联同步任务失败: context_id=%s", context.context_id)
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
    def __init__(self, registry: SessionRegistry, logger: Any, database: Any | None = None) -> None:
        self.registry = registry
        self.logger = logger
        self.database = database
        self.cookies = PersistentCookieStore(registry.config, logger)

    def update_config(self, config: AppConfig) -> None:
        self.registry.config = config
        self.cookies = PersistentCookieStore(config, self.logger)

    def context(self, context_id: str | None) -> WebAuthContext:
        context = self.registry.get(context_id)
        return context or self.registry.create()

    def has_saved_session(self, login_id: str | None) -> bool:
        return bool(login_id and self.cookies.has(login_id))

    def restore(self, context: WebAuthContext, login_id: str) -> UserInfo:
        if not self.cookies.load(login_id, context.session):
            raise SessionExpired("没有可用的保存会话")
        try:
            user = CasClient(self.registry.config, context.session, self.logger).check_session(
                expected_login_id=login_id
            )
        except SessionExpired:
            self.cookies.clear(login_id)
            self.registry.remove(context.context_id)
            raise
        except requests.RequestException:
            self.logger.warning("恢复登录会话时上游网络暂不可用")
            raise
        context.user = user
        context.touch()
        if self.database is not None:
            self.database.upsert_saved_account(user.login_id, user.display_name)
        return user

    def list_saved_accounts(self) -> list[Any]:
        if self.database is None:
            return []
        return self.database.list_saved_accounts()

    def remove_saved_account(self, login_id: str) -> None:
        self.cookies.clear(login_id)
        if self.database is not None:
            self.database.remove_saved_account(login_id)

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
        user = CasClient(self.registry.config, context.session, self.logger).login(
            username, password, captcha, sms_code
        )
        context.user = user
        context.captcha_page = None
        context.captcha_verified = False
        context.touch()
        if not self.cookies.save(user.login_id, context.session):
            self.logger.warning("登录成功但保存会话失败: %s", user.login_id)
        elif self.database is not None:
            self.database.upsert_saved_account(user.login_id, user.display_name)
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
