from __future__ import annotations

import logging

import requests

from backend.auth.cas_client import CasClient, SessionFactory, SessionExpired
from backend.auth.cookie_store import CookieStore
from shared.config import AppConfig
from shared.models import UserInfo


SERVICE_NAME = "grid-realtime-monitor"


class CredentialStore(CookieStore):
    def save_session(self, username: str, session: requests.Session) -> bool:
        return self.save(username, session)

    def load_session(self, username: str, session: requests.Session) -> bool:
        return self.load(username, session)


class AuthService:
    def __init__(self, config: AppConfig, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.config = config
        self.session_factory = SessionFactory(config, self.logger)
        self.credentials = CredentialStore(SERVICE_NAME, self.logger)
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
