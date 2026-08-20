from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
from typing import Any

import requests

from backend.auth.cas_client import AuthError, CasClient, SessionExpired, SessionFactory
from backend.storage.database import Database
from shared.config import AppConfig
from webapp.services.auth import PersistentCookieStore


class SessionMonitor:
    """Periodically validate saved account sessions without using browser contexts."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        logger: logging.Logger | None = None,
        *,
        interval_seconds: int | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.interval_seconds = interval_seconds or config.heartbeat_interval_seconds
        self.cookies = PersistentCookieStore(config, self.logger)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._account_locks: dict[str, threading.Lock] = {}
        self._sessions: dict[str, requests.Session] = {}
        self._thread: threading.Thread | None = None

    def update_config(self, config: AppConfig) -> None:
        with self._lock:
            self.config = config
            self.interval_seconds = config.heartbeat_interval_seconds
            self.cookies = PersistentCookieStore(config, self.logger)
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def _lock_for(self, login_id: str) -> threading.Lock:
        with self._lock:
            return self._account_locks.setdefault(login_id, threading.Lock())

    def _session_for(self, login_id: str) -> requests.Session:
        with self._lock:
            session = self._sessions.get(login_id)
            if session is None:
                session = SessionFactory(self.config, self.logger).create()
                self._sessions[login_id] = session
            return session

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _update(self, login_id: str, *, status: str, error: str | None, failures: int) -> None:
        self.database.update_heartbeat(
            login_id,
            status=status,
            heartbeat_at=self._now(),
            error=error,
            consecutive_failures=failures,
        )

    def check_now(self, login_id: str) -> dict[str, Any] | None:
        """Validate one saved account and return its redacted database record."""
        if not login_id:
            return None
        account = self.database.get_saved_account(login_id)
        if account is None:
            return None
        lock = self._lock_for(login_id)
        with lock:
            current = self.database.get_saved_account(login_id)
            if current is None:
                return None
            failures = int(current["consecutive_failures"] or 0)
            session = self._session_for(login_id)
            if not self.cookies.load(login_id, session):
                self._update(login_id, status="expired", error="没有可用的保存会话", failures=failures + 1)
                return dict(self.database.get_saved_account(login_id))
            try:
                CasClient(self.config, session, self.logger).check_session(expected_login_id=login_id)
                # CAS may rotate a service cookie in Set-Cookie; persist only the cookie jar.
                self.cookies.save(login_id, session)
            except SessionExpired:
                self._update(login_id, status="expired", error="上游会话已失效", failures=failures + 1)
            except AuthError:
                self._update(login_id, status="expired", error="账号会话校验失败", failures=failures + 1)
            except requests.RequestException:
                self._update(login_id, status="error", error="网络暂时不可用", failures=failures + 1)
            except Exception as exc:
                self.logger.warning("账号心跳检查异常: %s", type(exc).__name__)
                self._update(login_id, status="error", error="会话检查暂时失败", failures=failures + 1)
            else:
                self._update(login_id, status="healthy", error=None, failures=0)
            return dict(self.database.get_saved_account(login_id))

    def _run_cycle(self) -> None:
        for account in self.database.list_saved_accounts():
            if self._stop.is_set():
                return
            if account["heartbeat_status"] == "expired":
                continue
            self.check_now(str(account["login_id"]))

    def _run(self) -> None:
        self._run_cycle()
        while not self._stop.wait(self.interval_seconds):
            self._run_cycle()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="session-heartbeat", daemon=True)
            self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._thread = None
        for session in sessions:
            session.close()
