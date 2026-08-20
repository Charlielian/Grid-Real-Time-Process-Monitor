from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import time
from typing import Any
import uuid

from backend.platform.client import PlatformClient
from backend.storage.database import Database
from backend.sync.service import SyncCancelled, sync_work_orders
from shared.config import AppConfig
from shared.models import SyncSummary, UserInfo


SYNC_BATCH_SIZE = 100


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
    finished_at: float | None = None


@dataclass(frozen=True, slots=True)
class SyncJobSnapshot:
    job_id: str
    context_id: str
    status: str
    progress: int
    message: str
    error: str | None
    summary: SyncSummary | None


class SyncJobManager:
    def __init__(
        self,
        database: Database,
        logger: Any,
        max_workers: int = 2,
        *,
        job_ttl_seconds: float = 3600,
        max_terminal_jobs: int = 1000,
        cleanup_interval_seconds: float = 60,
    ) -> None:
        self.database = database
        self.logger = logger
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="grid-sync")
        self._jobs: dict[str, SyncJob] = {}
        self._context_jobs: dict[str, str] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._job_ttl_seconds = max(0.0, job_ttl_seconds)
        self._max_terminal_jobs = max(1, max_terminal_jobs)
        self._cleanup_stop = threading.Event()
        self._cleanup_interval_seconds = max(0.1, cleanup_interval_seconds)
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, name="sync-job-cleanup", daemon=True)
        self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.wait(self._cleanup_interval_seconds):
            self._cleanup_jobs()

    def _cleanup_jobs(self) -> None:
        now = time.monotonic()
        with self._lock:
            terminal = [job for job in self._jobs.values() if job.finished_at is not None]
            expired = {job.job_id for job in terminal if now - job.finished_at >= self._job_ttl_seconds}
            retained = [job for job in terminal if job.job_id not in expired]
            if len(retained) > self._max_terminal_jobs:
                retained.sort(key=lambda job: job.finished_at or 0, reverse=True)
                expired.update(job.job_id for job in retained[self._max_terminal_jobs:])
            for job_id in expired:
                job = self._jobs.pop(job_id, None)
                if job and self._context_jobs.get(job.context_id) == job_id:
                    self._context_jobs.pop(job.context_id, None)

    def _snapshot(self, job: SyncJob) -> SyncJobSnapshot:
        return SyncJobSnapshot(
            job_id=job.job_id,
            context_id=job.context_id,
            status=job.status,
            progress=job.progress,
            message=job.message,
            error=job.error,
            summary=job.summary,
        )

    def submit(self, context_id: str, client: PlatformClient, user: UserInfo, config: AppConfig) -> SyncJobSnapshot:
        with self._lock:
            if self._closed:
                raise RuntimeError("同步任务管理器已关闭")
            existing_id = self._context_jobs.get(context_id)
            existing = self._jobs.get(existing_id or "")
            if existing and existing.status in {"queued", "running"}:
                return self._snapshot(existing)
            job = SyncJob(job_id=uuid.uuid4().hex, context_id=context_id)
            self._jobs[job.job_id] = job
            self._context_jobs[context_id] = job.job_id
            job.future = self.executor.submit(self._run, job, client, user, config)
            return self._snapshot(job)

    def get(self, job_id: str) -> SyncJobSnapshot | None:
        self._cleanup_jobs()
        with self._lock:
            job = self._jobs.get(job_id)
            return self._snapshot(job) if job else None

    def _mark_cancelled(self, job: SyncJob) -> None:
        with self._lock:
            if job.status not in {"queued", "running"}:
                return
            job.cancel_event.set()
            job.status = "cancelled"
            job.message = "同步已取消"
            job.finished_at = time.monotonic()
            if self._context_jobs.get(job.context_id) == job.job_id:
                self._context_jobs.pop(job.context_id, None)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status not in {"queued", "running"}:
                return False
            job.cancel_event.set()
            future = job.future
            queued = job.status == "queued"
        if queued and future is not None and future.cancel():
            self._mark_cancelled(job)
        return True

    def cancel_context(self, context_id: str, *, wait: bool = True, timeout: float | None = None) -> bool:
        with self._lock:
            job_id = self._context_jobs.get(context_id)
            job = self._jobs.get(job_id or "")
            if job is None or job.status not in {"queued", "running"}:
                return False
            future = job.future
            target_job_id = job.job_id
        self.cancel(target_job_id)
        if wait and future is not None and not future.cancelled():
            future.result(timeout=timeout)
        return True

    def _set_running(self, job: SyncJob) -> bool:
        with self._lock:
            if job.cancel_event.is_set():
                self._mark_cancelled(job)
                return False
            job.status = "running"
            job.message = "开始同步"
            return True

    def _run(self, job: SyncJob, client: PlatformClient, user: UserInfo, config: AppConfig) -> None:
        if not self._set_running(job):
            return

        def update_progress(progress: int, message: str) -> None:
            with self._lock:
                job.progress = progress
                job.message = message

        try:
            summary = sync_work_orders(
                client,
                self.database,
                user,
                config,
                progress=update_progress,
                cancelled=job.cancel_event.is_set,
            )
            if job.cancel_event.is_set():
                with self._lock:
                    job.status = "cancelled"
                    job.message = "同步已取消"
                    job.finished_at = time.monotonic()
                return
            with self._lock:
                job.summary = summary
                job.progress = 100
                job.status = "succeeded"
                job.message = f"同步完成：{summary.total} 条"
                job.finished_at = time.monotonic()
        except SyncCancelled:
            with self._lock:
                job.status = "cancelled"
                job.error = None
                job.message = "同步已取消"
                job.finished_at = time.monotonic()
        except Exception:
            with self._lock:
                job.status = "cancelled" if job.cancel_event.is_set() else "failed"
                job.error = None if job.cancel_event.is_set() else "sync_failed"
                job.message = "同步已取消" if job.cancel_event.is_set() else "同步失败"
                job.finished_at = time.monotonic()
            if not job.cancel_event.is_set():
                self.logger.exception("同步任务失败")
        finally:
            with self._lock:
                if self._context_jobs.get(job.context_id) == job.job_id:
                    self._context_jobs.pop(job.context_id, None)

    def shutdown(self, timeout: float | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = [job for job in self._jobs.values() if job.status in {"queued", "running"}]
        self._cleanup_stop.set()
        if self._cleanup_thread is not threading.current_thread():
            self._cleanup_thread.join(timeout=timeout)
        futures: list[Future[Any]] = []
        for job in jobs:
            self.cancel(job.job_id)
            with self._lock:
                future = job.future
            if future is not None:
                if future.cancel():
                    self._mark_cancelled(job)
                elif not future.done():
                    futures.append(future)
        for future in futures:
            future.result(timeout=timeout)
        self.executor.shutdown(wait=True, cancel_futures=True)
        self._cleanup_jobs()
