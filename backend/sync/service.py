from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from backend.platform.client import PlatformClient
from backend.storage.database import Database
from shared.config import AppConfig, matches_title_keywords
from shared.models import SyncSummary, UserInfo, WorkOrder


SYNC_BATCH_SIZE = 100


class SyncCancelled(RuntimeError):
    pass


def _sync_batch(
    orders: list[WorkOrder],
    database: Database,
) -> tuple[int, int, int]:
    if not orders:
        return 0, 0, 0
    return database.upsert_orders(orders)


def sync_work_orders(
    client: PlatformClient,
    database: Database,
    user: UserInfo,
    config: AppConfig,
    *,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> SyncSummary:
    started = datetime.now(timezone.utc)
    run_id = database.start_sync_run(started.isoformat())
    total_seen = added = changed = 0
    try:
        start = started - timedelta(hours=config.lookback_hours)
        page_index = 1
        while not (cancelled and cancelled()):
            page = client.query_work_orders(
                user.login_id,
                page_index=page_index,
                page_size=config.page_size,
                start_time=start.isoformat(),
                end_time=started.isoformat(),
            )
            batch = [
                order for order in page.items
                if matches_title_keywords(order.title, config.target_title_keywords)
            ]
            for offset in range(0, len(batch), SYNC_BATCH_SIZE):
                if cancelled and cancelled():
                    break
                batch_total, batch_added, batch_changed = _sync_batch(
                    batch[offset:offset + SYNC_BATCH_SIZE], database
                )
                total_seen += batch_total
                added += batch_added
                changed += batch_changed

            if progress:
                progress(min(99, int(total_seen / max(page.total, 1) * 100)), f"已同步 {total_seen} 条")
            if page_index * page.page_size >= page.total or not page.items:
                break
            page_index += 1
        if cancelled and cancelled():
            raise SyncCancelled("同步已取消")
        finished = datetime.now(timezone.utc)
        database.finish_sync_run(run_id, total=total_seen, added=added, changed=changed)
        if progress:
            progress(100, f"同步完成：{total_seen} 条")
        return SyncSummary(total_seen, added, changed, 0, started, finished)
    except SyncCancelled:
        database.finish_sync_run(
            run_id, total=total_seen, added=added, changed=changed, error="cancelled"
        )
        raise
    except Exception:
        database.finish_sync_run(run_id, total=total_seen, added=added, changed=changed, error="sync_failed")
        raise
