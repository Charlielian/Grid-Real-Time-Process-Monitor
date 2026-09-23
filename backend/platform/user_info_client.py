"""账号用户信息与归属地市查询客户端。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import requests

from backend.auth.cas_client import SessionExpired
from backend.platform.client import PlatformError
from shared.config import AppConfig


class UserInfoClient:
    """调用门户用户信息接口并提取账号归属地市。"""

    _CLIENT_ID = "cmcw"
    _SECRET = "12345678"

    def __init__(self, config: AppConfig, session: requests.Session, logger: Any = None) -> None:
        self.config = config
        self.session = session
        self.logger = logger
        self.timeout = (10, 45)
        self.url = f"{config.base_url.rstrip('/')}/pro-portal/rest/user/getUserInfo"

    @classmethod
    def _token(cls, date_text: str) -> str:
        value = f"restusergetUserInfo{date_text}{cls._SECRET}"
        return hashlib.md5(value.encode("utf-8")).hexdigest()

    def get_user_info(self, login_id: str) -> dict[str, Any]:
        if not login_id:
            raise ValueError("缺少当前用户")
        date_text = datetime.now().strftime("%Y-%m-%d")
        payload = {
            "clientId": self._CLIENT_ID,
            "apiToken": self._token(date_text),
            "loginId": login_id,
        }
        try:
            response = self.session.post(self.url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise PlatformError("用户信息服务请求失败") from exc
        if response.status_code in (401, 403) or "/cas/login" in str(getattr(response, "url", "")):
            raise SessionExpired("用户信息服务会话已失效")
        if response.status_code >= 400:
            raise PlatformError(f"用户信息服务返回 HTTP {response.status_code}")
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise PlatformError("用户信息响应格式无效") from exc
        if not isinstance(body, dict) or body.get("state") not in (None, "9999", 9999):
            raise PlatformError("用户信息查询失败")
        info = body.get("userInfo")
        if not isinstance(info, dict):
            raise PlatformError("用户信息响应缺少 userInfo")
        returned_id = str(info.get("loginId") or "")
        if returned_id and returned_id != login_id:
            raise PlatformError("用户信息账号校验失败")
        return info

    def get_cities(self, login_id: str) -> tuple[str, ...]:
        info = self.get_user_info(login_id)
        mt = info.get("mt")
        if not isinstance(mt, list):
            return ()
        cities: list[str] = []
        for item in mt:
            if not isinstance(item, dict):
                continue
            city = str(item.get("cityName") or "").strip()
            if city and city not in cities:
                cities.append(city)
        return tuple(cities)
