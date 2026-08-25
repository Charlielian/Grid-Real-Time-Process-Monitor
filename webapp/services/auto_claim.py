"""后台自动领取服务，独立于浏览器页面运行。

使用已保存的 keyring Cookie 为每个账号创建会话，按固定间隔轮询上游待领取
任务，仅领取标题匹配 target_title_keywords（默认阳江）的任务。该服务进
程启动即运行，页面关闭不影响其执行。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import requests

from backend.auth.cas_client import CasClient, SessionExpired, SessionFactory
from backend.platform.client import PlatformBusinessError, PlatformClient, PlatformError
from shared.config import AppConfig
from webapp.services.auth import AccountIndexStore, PersistentCookieStore
from webapp.services.pending_tasks import claimable_tasks, query_all_todo_tasks


class AutoClaimService:
    """后台线程，按固定间隔轮询上游并自动领取匹配关键词的任务。

    与页面 JS 自动领取互不依赖；页面关闭后该服务继续运行。仅在所有已保存
    账号上执行，跳过心跳状态为 expired 的账号。
    """

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger | None = None,
        *,
        interval_seconds: int | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.interval_seconds = interval_seconds or config.auto_claim_interval_seconds
        self.cookies = PersistentCookieStore(config, self.logger)
        self.accounts = AccountIndexStore(config, self.logger)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._sessions: dict[str, requests.Session] = {}
        self._thread: threading.Thread | None = None
        self._closed = False

    def update_config(self, config: AppConfig) -> None:
        """热更新配置；会话在下一次轮询时重建。"""
        with self._lock:
            self.config = config
            self.interval_seconds = config.auto_claim_interval_seconds
            self.cookies = PersistentCookieStore(config, self.logger)
            self.accounts = AccountIndexStore(config, self.logger)
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    def _session_for(self, login_id: str) -> requests.Session | None:
        """获取或创建账号的缓存会话；无有效 Cookie 时返回 None。"""
        with self._lock:
            if self._closed:
                return None
            session = self._sessions.get(login_id)
            if session is None:
                session = SessionFactory(self.config, self.logger).create()
                self._sessions[login_id] = session
        if not self.cookies.load(login_id, session):
            return None
        return session

    def _run_cycle(self) -> None:
        """单次轮询：遍历所有已保存账号，领取可领取任务。"""
        if not self.config.auto_claim_pending_tasks:
            return
        try:
            accounts = self.accounts.list()
        except Exception:
            self.logger.exception("自动领取: 读取保存账号失败")
            return
        for account in accounts:
            if self._stop.is_set():
                return
            login_id = str(account["login_id"])
            if account.get("heartbeat_status") == "expired":
                continue
            try:
                self._claim_for_account(login_id)
            except Exception as exc:
                self.logger.warning("自动领取: 账号 %s 异常: %s", login_id, exc)

    def _claim_for_account(self, login_id: str) -> None:
        """为单个账号执行一次完整的查询+领取流程。"""
        session = self._session_for(login_id)
        if session is None:
            self.logger.info("自动领取: 账号 %s 无可用保存会话，跳过", login_id)
            return
        # 验证会话有效性，顺便续期 Cookie。
        try:
            CasClient(self.config, session, self.logger).check_session(expected_login_id=login_id)
            self.cookies.save(login_id, session)
        except SessionExpired:
            self.logger.info("自动领取: 账号 %s 会话已失效，跳过", login_id)
            return
        except requests.RequestException:
            self.logger.warning("自动领取: 账号 %s 网络不可用，跳过本次", login_id)
            return

        client = PlatformClient(self.config, session, self.logger)
        pending = query_all_todo_tasks(client, login_id, assigned=False, config=self.config)
        claimable = claimable_tasks(pending, self.config.target_title_keywords)
        if not claimable:
            return
        task_ids = [task.task_id for task in claimable if task.task_id]
        if not task_ids:
            return
        try:
            client.assign_tasks(login_id, task_ids)
            self.logger.info(
                "自动领取: 账号 %s 成功领取 %d 条任务（共 %d 条待领取）",
                login_id, len(task_ids), len(pending),
            )
        except PlatformBusinessError as exc:
            self.logger.warning("自动领取: 账号 %s 领取被拒: %s", login_id, exc)
        except (PlatformError, ValueError) as exc:
            self.logger.warning("自动领取: 账号 %s 领取失败: %s", login_id, exc)

    def _run(self) -> None:
        """后台线程主循环。"""
        try:
            while not self._stop.wait(self.interval_seconds):
                self._run_cycle()
        finally:
            self._close_sessions()

    def _close_sessions(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    def start(self) -> None:
        """启动后台轮询线程。"""
        with self._lock:
            if self._closed:
                return
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="auto-claim", daemon=True)
            self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """停止线程并清理会话。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if thread and thread.is_alive():
            return
        self._close_sessions()
        with self._lock:
            self._thread = None