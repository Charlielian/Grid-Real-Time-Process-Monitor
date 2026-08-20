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
from backend.platform.client import PlatformClient
from shared.config import AppConfig
from shared.models import UserInfo


class PersistentCookieStore:
    """Store upstream session cookies in the current user's OS keyring."""

    service_prefix = "grid-realtime-monitor-web"

    def __init__(self, config: AppConfig, logger: Any) -> None:
        self.logger = logger
        origin_key = hashlib.sha256(config.origin.encode("utf-8")).hexdigest()[:20]
        self.service_name = f"{self.service_prefix}-{origin_key}"

    def _read(self, login_id: str) -> list[dict[str, Any]] | None:
        try:
            raw = keyring.get_password(self.service_name, login_id)
        except Exception as exc:
            self.logger.warning("读取保存的登录会话失败: %s", type(exc).__name__)
            return None
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, list) else None

    def has(self, login_id: str) -> bool:
        return bool(login_id and self._read(login_id))

    def save(self, login_id: str, session: requests.Session) -> bool:
        if not login_id:
            return False
        cookies: list[dict[str, Any]] = []
        for cookie in session.cookies:
            cookies.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
                "rest": dict(cookie._rest),
            })
        if not cookies:
            return False
        try:
            keyring.set_password(
                self.service_name,
                login_id,
                json.dumps(cookies, ensure_ascii=False, separators=(",", ":")),
            )
            return True
        except Exception as exc:
            self.logger.warning("保存登录会话失败: %s", type(exc).__name__)
            return False

    def load(self, login_id: str, session: requests.Session) -> bool:
        cookies = self._read(login_id)
        if not cookies:
            return False
        try:
            for item in cookies:
                if not isinstance(item, dict):
                    return False
                name = item.get("name")
                value = item.get("value")
                domain = item.get("domain")
                if not all(isinstance(part, str) and part for part in (name, value, domain)):
                    return False
                session.cookies.set(
                    name,
                    value,
                    domain=domain,
                    path=item.get("path") or "/",
                    expires=item.get("expires"),
                    secure=bool(item.get("secure", False)),
                    rest=item.get("rest") if isinstance(item.get("rest"), dict) else None,
                )
            return True
        except (TypeError, ValueError, requests.exceptions.RequestException):
            return False

    def clear(self, login_id: str) -> None:
        if not login_id:
            return
        try:
            keyring.delete_password(self.service_name, login_id)
        except Exception:
            pass


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

    def __init__(self, config: AppConfig, logger: Any, ttl_seconds: int = 1800) -> None:
        self.config = config
        self.logger = logger
        self.ttl_seconds = ttl_seconds
        self._contexts: dict[str, WebAuthContext] = {}
        self._lock = threading.RLock()

    def create(self) -> WebAuthContext:
        context_id = secrets.token_urlsafe(32)
        context = WebAuthContext(
            context_id=context_id,
            session=SessionFactory(self.config, self.logger).create(),
        )
        with self._lock:
            self._contexts[context_id] = context
        return context

    def get(self, context_id: str | None) -> WebAuthContext | None:
        if not context_id:
            return None
        with self._lock:
            context = self._contexts.get(context_id)
            if context is None:
                return None
            if context.expired(self.ttl_seconds):
                self._contexts.pop(context_id, None)
                context.session.cookies.clear()
                return None
            context.touch()
            return context

    def remove(self, context_id: str | None) -> None:
        if not context_id:
            return
        with self._lock:
            context = self._contexts.pop(context_id, None)
            if context:
                context.session.cookies.clear()
                context.user = None

    def cleanup(self) -> None:
        with self._lock:
            expired = [key for key, value in self._contexts.items() if value.expired(self.ttl_seconds)]
            for key in expired:
                context = self._contexts.pop(key)
                context.session.cookies.clear()


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
        except Exception:
            self.cookies.clear(login_id)
            self.registry.remove(context.context_id)
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
        self.cookies.save(user.login_id, context.session)
        if self.database is not None:
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
        except Exception:
            self.registry.remove(context.context_id)
            raise
        context.user = user
        context.touch()
        return context, user

    def platform(self, context: WebAuthContext) -> PlatformClient:
        if context.user is None:
            raise SessionExpired("请先登录")
        return PlatformClient(self.registry.config, context.session, self.logger)
