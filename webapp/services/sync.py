from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import uuid

from backend.platform.client import PlatformClient
from backend.storage.database import Database
from shared.config import AppConfig, matches_title_keywords
from shared.models import SyncSummary, UserInfo


@dataclass
class SyncJob:
    job_id: str
    context_id: str
    status: str = "queued"
    progress: int = 0
    message: str = "等待执行"
    error: str | None = None
    summary: SyncSummary | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: Future[Any] | None = None


class SyncJobManager:
    def __init__(self, database: Database, logger: Any, max_workers: int = 2) -> None:
        self.database = database
        self.logger = logger
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="grid-sync")
        self._jobs: dict[str, SyncJob] = {}
        self._context_jobs: dict[str, str] = {}
        self._lock = threading.RLock()

    def submit(self, context_id: str, client: PlatformClient, user: UserInfo, config: AppConfig) -> SyncJob:
        with self._lock:
            existing_id = self._context_jobs.get(context_id)
            existing = self._jobs.get(existing_id or "")
            if existing and existing.status in {"queued", "running"}:
                return existing
            job = SyncJob(job_id=uuid.uuid4().hex, context_id=context_id)
            self._jobs[job.job_id] = job
            self._context_jobs[context_id] = job.job_id
            job.future = self.executor.submit(self._run, job, client, user, config)
            return job

    def get(self, job_id: str) -> SyncJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status not in {"queued", "running"}:
            return False
        job.cancel_event.set()
        return True

    def _run(self, job: SyncJob, client: PlatformClient, user: UserInfo, config: AppConfig) -> None:
        job.status = "running"
        job.message = "开始同步"
        started = datetime.now(timezone.utc)
        run_id = self.database.start_sync_run(started.isoformat())
        total_seen = added = changed = 0
        try:
            start = started - timedelta(hours=config.lookback_hours)
            page_index = 1
            while not job.cancel_event.is_set():
                page = client.query_work_orders(
                    user.login_id,
                    page_index=page_index,
                    page_size=config.page_size,
                    start_time=start.isoformat(),
                    end_time=started.isoformat(),
                )
                for order in page.items:
                    if not matches_title_keywords(order.title, config.target_title_keywords):
                        continue
                    if job.cancel_event.is_set():
                        break
                    is_new, events = self.database.upsert_work_order(order)
                    added += int(is_new)
                    changed += len(events) - int(is_new)
                    total_seen += 1
                percent = min(99, int(total_seen / max(page.total, 1) * 100))
                job.progress = percent
                job.message = f"已同步 {total_seen} 条"
                if page_index * page.page_size >= page.total or not page.items:
                    break
                page_index += 1
            if job.cancel_event.is_set():
                job.status = "cancelled"
                job.message = "同步已取消"
                self.database.finish_sync_run(run_id, total=total_seen, added=added, changed=changed, error="cancelled")
                return
            finished = datetime.now(timezone.utc)
            job.summary = SyncSummary(total_seen, added, changed, 0, started, finished)
            job.progress = 100
            job.status = "succeeded"
            job.message = f"同步完成：{total_seen} 条"
            self.database.finish_sync_run(run_id, total=total_seen, added=added, changed=changed)
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            job.message = "同步失败"
            self.database.finish_sync_run(run_id, total=total_seen, added=added, changed=changed, error=type(exc).__name__)
            self.logger.warning("同步任务失败: %s", type(exc).__name__)
        finally:
            with self._lock:
                if self._context_jobs.get(job.context_id) == job.job_id:
                    self._context_jobs.pop(job.context_id, None)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
