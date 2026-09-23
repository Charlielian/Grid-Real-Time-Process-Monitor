"""基于系统密钥环的会话 Cookie 持久化。

Cookie 以 JSON 列表形式存入操作系统提供的 keyring，而不是写入项目目录，
避免认证材料出现在普通配置或日志中。读取、写入和删除均采用失败可恢复的
策略：密钥环不可用或数据格式不兼容时返回失败并记录类型信息，但不让应用
因凭据存储故障直接崩溃。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import keyring
import requests


class CookieStore:
    """Persist requests session cookies in the OS keyring."""

    def __init__(self, service_name: str, logger: logging.Logger | Any | None = None) -> None:
        """初始化 keyring 服务名和日志器，不执行读写。"""
        """初始化 keyring 服务名和日志器，不执行读写。"""
        """初始化 keyring 服务名和日志器，不执行读写。"""
        self.service_name = service_name
        self.logger = logger or logging.getLogger(__name__)

    def _read(self, login_id: str) -> list[dict[str, Any]] | None:
        """读取并校验账号 Cookie 列表；密钥环或 JSON 错误返回 ``None``。"""
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
        """判断账号是否存在非空、可解析的保存会话。"""
        return bool(self._read(login_id))

    def save(self, login_id: str, session: requests.Session) -> bool:
        """序列化 Session Cookie 到系统 keyring，返回写入是否成功。

        不保存空账号或空 Cookie；异常只记录异常类型，不记录 Cookie 内容。
        """
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
        """将已保存 Cookie 恢复到 Session，并拒绝结构不完整的条目。"""
        cookies = self._read(login_id)
        if not cookies:
            return False
        try:
            for item in cookies:
                # 任一条目损坏都整体失败，避免恢复出半套认证状态。
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
        """删除账号凭据；密钥环已无该条目时保持幂等。"""
        if not login_id:
            return
        try:
            keyring.delete_password(self.service_name, login_id)
        except Exception:
            pass
