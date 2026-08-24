"""CAS 认证协议客户端。

模块封装登录页参数解析、RSA 加密、验证码和短信校验，以及登录态检查。
请求使用同一个 :class:`requests.Session`，从而保留 CAS 重定向过程中产生的
Cookie；调用方应根据 :class:`SessionExpired` 判断是否需要重新登录。网络
异常和协议响应异常不会被静默吞掉，以便上层区分会话失效与认证失败。
"""

from __future__ import annotations

import base64
import json
import logging
import random
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
    """认证协议或登录参数不符合预期时抛出的异常。"""



class SessionExpired(AuthError):
    """远端确认当前会话失效，需要重新登录时抛出的异常。"""



@dataclass(frozen=True)
class LoginPage:
    """登录页中供后续请求使用的 CAS execution 和 RSA 公钥。"""
    execution: str
    public_key: str


def rsa_encrypt_pkcs1(value: str, public_key_text: str) -> str:
    """使用登录页公钥按 PKCS#1 v1.5 加密字符串并返回 Base64 文本。

    参数不能为空；服务端可能返回裸 Base64 公钥，因此会在缺少 PEM 头时补齐
    标记。密钥格式、类型或加载失败统一转换为 ``AuthError``，不泄露原始密钥
    内容。返回值可直接作为 CAS 表单或 JSON 字段传输。
    """
    # 先拒绝空值，避免把无效凭据交给密码库后得到难以定位的异常。
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
    """从 CAS 登录 HTML 提取 execution 和 ``setPublicKey`` 中的公钥。

    页面结构存在兼容差异：优先读取 ``#fm1`` 的命名 input，缺失时回退到
    表单内第一个 input。任一必要参数缺失都抛出 ``AuthError``。
    """
    soup = BeautifulSoup(html, "html.parser")
    execution_node = soup.select_one("#fm1 input[name='execution']")
    execution = execution_node.get("value", "").strip() if execution_node else ""
    if not execution:
        execution_node = soup.select_one("#fm1 input")
        execution = execution_node.get("value", "").strip() if execution_node else ""
    match = re.search(r"setPublicKey\(\s*[\"']([^\"']+)[\"']\s*\)", html)
    # CAS 页面缺参数通常意味着登录页改版或已被网关替换，不能盲目提交。
    if not execution or not match:
        raise AuthError("登录页缺少必要参数")
    return LoginPage(execution=execution, public_key=match.group(1))


class SessionFactory:
    """按应用配置创建带默认请求头和 TLS 校验策略的 HTTP 会话。"""

    def __init__(self, config: AppConfig, logger: logging.Logger | None = None) -> None:
        """保存配置和日志器；构造本身不创建连接。"""
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    def create(self) -> requests.Session:
        """创建新 Session，并应用 CA bundle、语言和客户端标识。"""
        session = requests.Session()
        session.verify = self.config.ca_bundle or True
        session.headers.update({
            "User-Agent": "GridRealtimeMonitor/0.1",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        return session


class CasClient:
    """面向 CAS 与门户跳转接口的认证客户端。"""

    def __init__(self, config: AppConfig, session: requests.Session, logger: logging.Logger | None = None) -> None:
        """绑定配置和可复用 Session；所有请求使用固定连接超时。"""
        self.config = config
        self.session = session
        self.logger = logger or logging.getLogger(__name__)
        self.timeout = (10, 30)

    @property
    def login_url(self) -> str:
        """返回带业务门户 service 参数的 CAS 登录地址。"""
        service = f"{self.config.origin}/pro-portal/"
        return f"{self.config.base_url}/cas/login?{urlencode({'service': service})}"

    def get_login_page(self) -> LoginPage:
        """请求并解析登录页；HTTP 错误由 ``raise_for_status`` 抛出。"""
        response = self.session.get(self.login_url, timeout=self.timeout)
        response.raise_for_status()
        return parse_login_page(response.text)

    def get_captcha(self) -> bytes:
        """下载验证码图片并返回原始字节；响应必须声明 image 类型。"""
        response = self.session.get(f"{self.config.base_url}/cas/captcha.jpg", timeout=self.timeout)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        # 网关可能返回 HTML 登录页或错误页；即使 HTTP 为 200 也不能当图片使用。
        if not content_type.lower().startswith("image/"):
            raise AuthError("验证码响应不是图片")
        return response.content

    def verify_captcha(self, username: str, password: str, captcha: str, page: LoginPage) -> bool:
        """调用 CAS 配置接口校验验证码并返回业务码是否为成功 ``1``。"""
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
        """提交加密账号密码请求短信验证码，返回平台消息是否为 success。"""
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
        """完成 CAS 表单登录并校验最终会话，成功返回用户信息。

        登录页的 execution、公钥和 Session Cookie 必须来自同一流程；若重定向
        仍停留在 ``/cas/login``，视为短信验证码或登录失败并抛出 ``AuthError``。
        """
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
        # allow_redirects 后仍回到登录页，说明认证未建立，不能继续使用 Cookie。
        if response.url and "/cas/login" in response.url:
            raise AuthError("短信验证码错误或登录失败")
        return self.check_session(expected_login_id=username)

    def check_session(self, expected_login_id: str | None = None) -> UserInfo:
        """向 CAS 信息接口确认会话，并可校验返回账号与预期账号一致。

        401/403、无效 JSON、缺少 loginId 均视为 ``SessionExpired``；账号不一致
        则是认证结果异常，抛出 ``AuthError``。返回值包含显示名和完整原始数据。
        """
        response = self.session.get(
            f"{self.config.base_url}/pro-wfm-biz-server-fak/cas/login/info",
            timeout=self.timeout,
        )
        # 权限状态码是明确的会话失效信号，与普通接口错误区分处理。
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
        """访问目标业务门户入口，确认认证态可完成门户跳转。

        该方法只关心跳转和会话有效性，不解析门户页面内容；失效会话抛出
        ``SessionExpired``，其他 HTTP 错误由 ``raise_for_status`` 抛出。
        """
        params: dict[str, str] = {"url": TARGET_MODULE, "__PID": TARGET_PORTAL_PID}
        # 浏览器请求 urlAction 时会附带 CASTGC 作为 token 参数，兼容
        # 尚缺少 portal 上下文 JSESSIONID 的场景。从当前 Cookie 中提取。
        for cookie in self.session.cookies:
            if cookie.name == "CASTGC":
                params["token"] = cookie.value
                break
        query = urlencode(params)
        response = self.session.get(
            f"{self.config.base_url}/pro-portal/pure/urlAction.action?{query}",
            timeout=self.timeout,
            allow_redirects=True,
        )
        # 既检查状态码，也检查重定向 URL，兼容网关用 200 返回登录页的情况。
        if response.status_code in (401, 403) or "/cas/login" in response.url:
            raise SessionExpired("进入业务门户时会话已失效")
        response.raise_for_status()
