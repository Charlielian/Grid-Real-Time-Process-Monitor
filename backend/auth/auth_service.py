from __future__ import annotations

import json
import logging
from typing import Any

import keyring
import requests

from backend.auth.cas_client import CasClient, SessionFactory, SessionExpired
from shared.config import AppConfig
from shared.models import UserInfo


SERVICE_NAME = "grid-realtime-monitor"


class CredentialStore:
    def __init__(self, service_name: str = SERVICE_NAME, logger: logging.Logger | None = None) -> None:
        self.service_name = service_name
        self.logger = logger or logging.getLogger(__name__)

    def save_session(self, username: str, session: requests.Session) -> None:
        cookies = []
        for cookie in session.cookies:
            cookies.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
            })
        keyring.set_password(self.service_name, username, json.dumps(cookies, separators=(",", ":")))

    def load_session(self, username: str, session: requests.Session) -> bool:
        raw = keyring.get_password(self.service_name, username)
        if not raw:
            return False
        try:
            cookies = json.loads(raw)
            if not isinstance(cookies, list):
                return False
            for item in cookies:
                if not isinstance(item, dict):
                    return False
                name = item.get("name")
                value = item.get("value")
                domain = item.get("domain")
                path = item.get("path", "/")
                if not all(isinstance(v, str) and v for v in (name, value, domain)):
                    return False
                session.cookies.set(name, value, domain=domain, path=path)
            return True
        except (json.JSONDecodeError, TypeError, ValueError):
            self.logger.warning("保存的会话格式无效")
            return False

    def clear(self, username: str) -> None:
        try:
            keyring.delete_password(self.service_name, username)
        except keyring.errors.PasswordDeleteError:
            pass


class AuthService:
    def __init__(self, config: AppConfig, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.config = config
        self.session_factory = SessionFactory(config, self.logger)
        self.credentials = CredentialStore(logger=self.logger)
        self.session = self.session_factory.create()
        self.user: UserInfo | None = None

    def restore(self, username: str) -> UserInfo | None:
        if not self.credentials.load_session(username, self.session):
            return None
        try:
            self.user = CasClient(self.config, self.session, self.logger).check_session(username)
            return self.user
        except (requests.RequestException, SessionExpired):
            self.credentials.clear(username)
            self.session = self.session_factory.create()
            return None

    def get_captcha(self) -> bytes:
        return CasClient(self.config, self.session, self.logger).get_captcha()

    def verify_captcha(self, username: str, password: str, captcha: str) -> bool:
        client = CasClient(self.config, self.session, self.logger)
        return client.verify_captcha(username, password, captcha, client.get_login_page())

    def send_sms(self, username: str, password: str) -> bool:
        client = CasClient(self.config, self.session, self.logger)
        return client.send_sms(username, password, client.get_login_page())

    def login(self, username: str, password: str, captcha: str, sms_code: str) -> UserInfo:
        self.session = self.session_factory.create()
        user = CasClient(self.config, self.session, self.logger).login(
            username, password, captcha, sms_code
        )
        self.credentials.save_session(username, self.session)
        self.user = user
        return user

    def logout(self) -> None:
        if self.user:
            self.credentials.clear(self.user.login_id)
        self.session.cookies.clear()
        self.user = None
