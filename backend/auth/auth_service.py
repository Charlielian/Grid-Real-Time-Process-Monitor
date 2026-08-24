"""认证服务编排模块。

本模块负责把 CAS 客户端、会话工厂和操作系统凭据存储组合起来，向上层提供
验证码获取、短信发送、登录、会话恢复及注销能力。这里不直接实现认证协议，
而是确保会话生命周期和失效后的清理行为保持一致：恢复失败时删除旧凭据并
创建干净会话，登录成功后才持久化会话 Cookie。
"""

from __future__ import annotations

import logging

import requests

from backend.auth.cas_client import CasClient, SessionFactory, SessionExpired
from backend.auth.cookie_store import CookieStore
from shared.config import AppConfig
from shared.models import UserInfo


SERVICE_NAME = "grid-realtime-monitor"


class CredentialStore(CookieStore):
    """认证服务使用的 Cookie 存储适配器。

    通过保留语义更明确的 ``save_session``/``load_session`` 名称，兼容认证
    编排层，同时复用 :class:`CookieStore` 对 keyring 数据的校验与容错。
    """

    def save_session(self, username: str, session: requests.Session) -> bool:
        """保存指定账号的会话 Cookie，返回 keyring 写入是否成功。

        参数 ``username`` 是 keyring 中的账号键，``session`` 必须包含登录后
        的 Cookie；空账号或空 Cookie 会由底层存储拒绝。
        """
        return self.save(username, session)

    def load_session(self, username: str, session: requests.Session) -> bool:
        """将账号保存的 Cookie 加载到会话，返回数据是否完整且可用。"""
        return self.load(username, session)


class AuthService:
    """管理一次登录会话及其可选的持久化凭据。"""

    def __init__(self, config: AppConfig, logger: logging.Logger | None = None) -> None:
        """创建认证服务。

        参数：``config`` 提供 CAS 地址和 CA 校验设置；``logger`` 可注入调用方
        日志器。初始化会创建空会话，但不会发起网络请求或读取凭据。
        """
        self.logger = logger or logging.getLogger(__name__)
        self.config = config
        self.session_factory = SessionFactory(config, self.logger)
        self.credentials = CredentialStore(SERVICE_NAME, self.logger)
        self.session = self.session_factory.create()
        self.user: UserInfo | None = None

    def restore(self, username: str) -> UserInfo | None:
        """恢复并验证账号会话，成功返回用户信息，否则返回 ``None``。

        keyring 读取失败、会话过期或网络请求异常都不会向上抛出；已知失效的
        Cookie 会被清理并替换为新会话，避免后续请求继续复用坏状态。
        """
        # 先恢复 Cookie；没有可用凭据时不请求远端，避免无意义的认证探测。
        if not self.credentials.load_session(username, self.session):
            return None
        try:
            self.user = CasClient(self.config, self.session, self.logger).check_session(username)
            return self.user
        except (requests.RequestException, SessionExpired):
            # 失效会话不能继续复用；清理持久化凭据并重建 Session。
            self.credentials.clear(username)
            self.session = self.session_factory.create()
            return None

    def get_captcha(self) -> bytes:
        """获取验证码图片二进制内容；非图片响应会抛出认证异常。"""
        return CasClient(self.config, self.session, self.logger).get_captcha()

    def verify_captcha(self, username: str, password: str, captcha: str) -> bool:
        """校验验证码。

        ``username`` 和 ``password`` 仅交给 CAS 客户端加密后传输；返回值表示
        平台返回的校验码是否成功，网络或协议错误仍以异常形式报告。
        """
        client = CasClient(self.config, self.session, self.logger)
        return client.verify_captcha(username, password, captcha, client.get_login_page())

    def send_sms(self, username: str, password: str) -> bool:
        """请求 CAS 向账号发送短信验证码，并返回平台是否接受请求。"""
        client = CasClient(self.config, self.session, self.logger)
        return client.send_sms(username, password, client.get_login_page())

    def login(self, username: str, password: str, captcha: str, sms_code: str) -> UserInfo:
        """执行完整登录并保存成功会话，返回服务端确认的用户信息。

        每次登录前创建全新 Session，避免把旧账号 Cookie 带入新账号；只有
        ``CasClient.login`` 成功且会话已验证后才写入 keyring。
        """
        self.session = self.session_factory.create()
        user = CasClient(self.config, self.session, self.logger).login(
            username, password, captcha, sms_code
        )
        self.credentials.save_session(username, self.session)
        self.user = user
        return user

    def logout(self) -> None:
        """清除当前账号的持久化凭据、内存 Cookie 和用户信息。"""
        # 仅在已确认用户存在时删除对应 keyring 条目。
        if self.user:
            self.credentials.clear(self.user.login_id)
        self.session.cookies.clear()
        self.user = None
