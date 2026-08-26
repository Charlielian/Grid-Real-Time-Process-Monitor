"""后台自动领取服务，独立于浏览器页面运行。

使用已保存的 keyring Cookie 为每个账号创建会话，按固定间隔轮询上游待领取
任务，仅领取标题匹配 target_title_keywords（默认阳江）的任务。该服务进
程启动即运行，页面关闭不影响其执行。每次领取成功后记录统计到 JSON 文件，
支持 API 查询统计信息。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from backend.auth.cas_client import CasClient, SessionExpired, SessionFactory
from backend.platform.client import PlatformBusinessError, PlatformClient, PlatformError
from shared.config import AppConfig
from shared.models import TodoTask
from webapp.services.auth import AccountIndexStore, PersistentCookieStore
from webapp.services.pending_tasks import claimable_tasks, query_all_todo_tasks


class AutoClaimService:
    """后台线程，按固定间隔轮询上游并自动领取匹配关键词的任务。

    与页面 JS 自动领取互不依赖；页面关闭后该服务继续运行。仅在所有已保存
    账号上执行，跳过心跳状态为 expired 的账号。每次成功领取后记录统计信息
    到 data/auto_claim_stats.json，同时维护进程内 session_claimed 计数器。
    """

    _STATS_FILENAME = "auto_claim_stats.json"

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger | None = None,
        *,
        interval_seconds: int | None = None,
        data_dir: str | Path | None = None,
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
        # 统计
        self._stats_path = Path(data_dir) / self._STATS_FILENAME if data_dir else None
        self._stats: dict[str, Any] = {
            "total_claimed": 0,
            "per_account": {},
            "last_claim": None,
            "history": [],
            "recent_tasks": [],
        }
        self._session_claimed = 0
        self._stats_lock = threading.Lock()

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

    # ---- 统计记录 ----

    def _record_claim(self, login_id: str, task_ids: list[str], tasks: list[TodoTask] | None = None) -> None:
        """记录一次成功领取：更新累计/分账号计数，追加历史、工单明细并落盘。"""
        count = len(task_ids)
        now = datetime.now().isoformat(timespec="seconds")
        entry = {"time": now, "login_id": login_id, "count": count}
        with self._stats_lock:
            self._stats["total_claimed"] = self._stats.get("total_claimed", 0) + count
            per_account = self._stats.setdefault("per_account", {})
            per_account[login_id] = per_account.get(login_id, 0) + count
            self._stats["last_claim"] = entry
            history = self._stats.setdefault("history", [])
            history.append(entry)
            self._stats["history"] = history[-200:]
            # 记录最近领取工单明细（最多 200 条）
            recent = self._stats.setdefault("recent_tasks", [])
            if tasks:
                for task in tasks:
                    recent.append({
                        "time": now,
                        "login_id": login_id,
                        "number": task.number or "",
                        "title": task.title or "",
                    })
            self._stats["recent_tasks"] = recent[-200:]
            self._session_claimed += count
            self._write_stats_locked()

    def _write_stats_locked(self) -> None:
        """在持有 _stats_lock 时把统计写入 JSON 文件；失败仅记录日志。"""
        if not self._stats_path:
            return
        tmp = self._stats_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._stats, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._stats_path)
        except OSError:
            self.logger.exception("自动领取: 统计文件写入失败: %s", self._stats_path)

    def _load_stats(self) -> None:
        """从 JSON 文件恢复统计；文件损坏或不含历史时退回空统计。"""
        if not self._stats_path or not self._stats_path.exists():
            return
        try:
            with self._stats_lock:
                loaded = json.loads(self._stats_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._stats = {
                        "total_claimed": int(loaded.get("total_claimed", 0)),
                        "per_account": dict(loaded.get("per_account", {})),
                        "last_claim": loaded.get("last_claim"),
                        "history": list(loaded.get("history", []))[-200:],
                        "recent_tasks": list(loaded.get("recent_tasks", []))[-200:],
                    }
        except (OSError, ValueError, TypeError):
            self.logger.exception("自动领取: 统计文件读取失败，使用空统计: %s", self._stats_path)

    def stats(self) -> dict[str, Any]:
        """返回统计快照（累计/分账号/本次启动/上次领取/最近工单），供 API 使用。"""
        with self._stats_lock:
            per_account = dict(self._stats.get("per_account", {}))
            last_claim = self._stats.get("last_claim")
            history = list(self._stats.get("history", []))
            recent_tasks = list(self._stats.get("recent_tasks", []))
        return {
            "total_claimed": self._stats.get("total_claimed", 0),
            "per_account": per_account,
            "session_claimed": self._session_claimed,
            "last_claim": last_claim,
            "history": history[-10:],
            "recent_tasks": recent_tasks[-5:],
        }

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
            self.logger.info("自动领取: 服务未启用，跳过本轮扫描")
            return
        cycle_started = datetime.now()
        self.logger.info("自动领取: 开始扫描")
        try:
            accounts = self.accounts.list()
        except Exception:
            self.logger.exception("自动领取: 读取保存账号失败")
            return
        scanned = 0
        skipped = 0
        for account in accounts:
            if self._stop.is_set():
                return
            login_id = str(account["login_id"])
            if account.get("heartbeat_status") == "expired":
                skipped += 1
                self.logger.info("自动领取: 账号 %s 会话已过期，跳过扫描", login_id)
                continue
            scanned += 1
            try:
                self._claim_for_account(login_id)
            except Exception as exc:
                self.logger.warning("自动领取: 账号 %s 异常: %s", login_id, exc)
        elapsed = (datetime.now() - cycle_started).total_seconds()
        self.logger.info(
            "自动领取: 扫描完成，账号总数=%d，实际扫描=%d，跳过=%d，耗时=%.1f秒",
            len(accounts), scanned, skipped, elapsed,
        )

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
        self.logger.info(
            "自动领取: 账号 %s 扫描完成，待领取=%d，可领取=%d",
            login_id, len(pending), len(claimable),
        )
        if not claimable:
            return
        task_ids = [task.task_id for task in claimable if task.task_id]
        if not task_ids:
            return
        try:
            client.assign_tasks(login_id, task_ids)
            self._record_claim(login_id, task_ids, tasks=claimable)
            self.logger.info(
                "自动领取: 账号 %s 成功领取 %d 条任务（共 %d 条待领取）",
                login_id, len(task_ids), len(pending),
            )
        except PlatformBusinessError as exc:
            self.logger.warning("自动领取: 账号 %s 领取被拒: %s", login_id, exc)
        except (PlatformError, ValueError) as exc:
            self.logger.warning("自动领取: 账号 %s 领取失败: %s", login_id, exc)

    def _run(self) -> None:
        """后台线程主循环；启动后立即执行首轮扫描。"""
        try:
            self._run_cycle()
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
        """启动后台轮询线程前从磁盘恢复统计。"""
        with self._lock:
            if self._closed:
                return
            if self._thread and self._thread.is_alive():
                return
            self._load_stats()
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