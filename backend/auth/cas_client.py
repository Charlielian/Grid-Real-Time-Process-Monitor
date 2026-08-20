from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes, serialization

from shared.config import AppConfig, TARGET_MODULE, TARGET_PORTAL_PID
from shared.models import UserInfo


class AuthError(RuntimeError):
    pass


class SessionExpired(AuthError):
    pass


@dataclass(frozen=True)
class LoginPage:
    execution: str
    public_key: str


def rsa_encrypt_pkcs1(value: str, public_key_text: str) -> str:
    if not value or not public_key_text:
        raise ValueError("加密参数不能为空")
    key_text = public_key_text.strip()
    if "BEGIN PUBLIC KEY" not in key_text:
        key_text = f"-----BEGIN PUBLIC KEY-----\n{key_text}\n-----END PUBLIC KEY-----"
    try:
        key = serialization.load_pem_public_key(key_text.encode("ascii"))
        if not isinstance(key, rsa.RSAPublicKey):
            raise ValueError("登录公钥类型错误")
        encrypted = key.encrypt(value.encode("utf-8"), padding.PKCS1v15())
    except (ValueError, TypeError) as exc:
        raise AuthError("登录公钥无效") from exc
    return base64.b64encode(encrypted).decode("ascii")


def parse_login_page(html: str) -> LoginPage:
    soup = BeautifulSoup(html, "html.parser")
    execution_node = soup.select_one("#fm1 input[name='execution']")
    execution = execution_node.get("value", "").strip() if execution_node else ""
    if not execution:
        execution_node = soup.select_one("#fm1 input")
        execution = execution_node.get("value", "").strip() if execution_node else ""
    match = re.search(r"setPublicKey\(\s*[\"']([^\"']+)[\"']\s*\)", html)
    if not execution or not match:
        raise AuthError("登录页缺少必要参数")
    return LoginPage(execution=execution, public_key=match.group(1))


class SessionFactory:
    def __init__(self, config: AppConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    def create(self) -> requests.Session:
        session = requests.Session()
        session.verify = self.config.ca_bundle or True
        session.headers.update({
            "User-Agent": "GridRealtimeMonitor/0.1",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        return session


class CasClient:
    def __init__(self, config: AppConfig, session: requests.Session, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.session = session
        self.logger = logger or logging.getLogger(__name__)
        self.timeout = (10, 30)

    @property
    def login_url(self) -> str:
        service = f"{self.config.origin}/pro-portal/"
        return f"{self.config.base_url}/cas/login?{urlencode({'service': service})}"

    def get_login_page(self) -> LoginPage:
        response = self.session.get(self.login_url, timeout=self.timeout)
        response.raise_for_status()
        return parse_login_page(response.text)

    def get_captcha(self) -> bytes:
        response = self.session.get(f"{self.config.base_url}/cas/captcha.jpg", timeout=self.timeout)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not content_type.lower().startswith("image/"):
            raise AuthError("验证码响应不是图片")
        return response.content

    def verify_captcha(self, username: str, password: str, captcha: str, page: LoginPage) -> bool:
        payload = {
            "password": rsa_encrypt_pkcs1(password, page.public_key),
            "loginId": rsa_encrypt_pkcs1(username, page.public_key),
            "captcha": captcha.strip(),
        }
        response = self.session.post(
            f"{self.config.base_url}/cas/getConfig",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        return isinstance(data, dict) and str(data.get("code")) == "1"

    def send_sms(self, username: str, password: str, page: LoginPage) -> bool:
        payload = {
            "loginId": rsa_encrypt_pkcs1(username, page.public_key),
            "password": rsa_encrypt_pkcs1(password, page.public_key),
        }
        response = self.session.post(
            f"{self.config.base_url}/cas/sendCode1",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        return isinstance(data, dict) and data.get("msg") == "success"

    def login(self, username: str, password: str, captcha: str, sms_code: str) -> UserInfo:
        page = self.get_login_page()
        encrypted_username = rsa_encrypt_pkcs1(username, page.public_key)
        encrypted_password = rsa_encrypt_pkcs1(password, page.public_key)
        response = self.session.post(
            self.login_url,
            data={
                "password": encrypted_password,
                "username": encrypted_username,
                "msgCode": sms_code.strip(),
                "captcha": captcha.strip(),
                "uuid": "",
                "execution": page.execution,
                "_eventId": "submit",
                "geolocation": "",
            },
            timeout=self.timeout,
            allow_redirects=True,
        )
        if response.url and "/cas/login" in response.url:
            raise AuthError("短信验证码错误或登录失败")
        return self.check_session(expected_login_id=username)

    def check_session(self, expected_login_id: str | None = None) -> UserInfo:
        response = self.session.get(
            f"{self.config.base_url}/pro-wfm-biz-server-fak/cas/login/info",
            timeout=self.timeout,
        )
        if response.status_code in (401, 403):
            raise SessionExpired("会话已失效")
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise SessionExpired("会话验证响应无效") from exc
        body = data.get("data") if isinstance(data, dict) else None
        if not isinstance(body, dict) or not body.get("loginId"):
            raise SessionExpired("会话验证失败")
        login_id = str(body["loginId"])
        if expected_login_id and login_id != expected_login_id:
            raise AuthError("登录账号校验失败")
        return UserInfo(login_id=login_id, display_name=str(body.get("userName", "")), raw=body)

    def enter_portal(self) -> None:
        query = urlencode({"url": TARGET_MODULE, "__PID": TARGET_PORTAL_PID})
        response = self.session.get(
            f"{self.config.base_url}/pro-portal/pure/urlAction.action?{query}",
            timeout=self.timeout,
            allow_redirects=True,
        )
        if response.status_code in (401, 403) or "/cas/login" in response.url:
            raise SessionExpired("进入业务门户时会话已失效")
        response.raise_for_status()
