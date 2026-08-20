from __future__ import annotations

import json
import logging
from typing import Any

import keyring
import requests


class CookieStore:
    """Persist requests session cookies in the OS keyring."""

    def __init__(self, service_name: str, logger: logging.Logger | Any | None = None) -> None:
        self.service_name = service_name
        self.logger = logger or logging.getLogger(__name__)

    def _read(self, login_id: str) -> list[dict[str, Any]] | None:
        if not login_id:
            return None
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
            self.logger.warning("保存的会话格式无效")
            return None
        if not isinstance(value, list) or not value:
            return None
        return value

    def has(self, login_id: str) -> bool:
        return bool(self._read(login_id))

    def save(self, login_id: str, session: requests.Session) -> bool:
        if not login_id:
            return False
        cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
                "rest": dict(cookie._rest),
            }
            for cookie in session.cookies
        ]
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
                rest = item.get("rest")
                session.cookies.set(
                    name,
                    value,
                    domain=domain,
                    path=item.get("path") or "/",
                    expires=item.get("expires"),
                    secure=bool(item.get("secure", False)),
                    rest=rest if isinstance(rest, dict) else None,
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
