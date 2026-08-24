"""工单同步编排。

同步按平台分页读取指定时间窗口内的工单，再按较小批次写入 SQLite，以缩短
单次事务持锁时间。同步运行记录在开始时创建，并在成功、取消或异常时分别
写入终态；取消检查既发生在翻页循环，也发生在批次写入之间，因此调用方可
在不改变已提交数据的前提下尽快停止后续工作。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from backend.platform.client import PlatformClient
from backend.storage.database import Database
from shared.config import AppConfig
from shared.models import SyncSummary, UserInfo, WorkOrder


SYNC_BATCH_SIZE = 100


class SyncCancelled(RuntimeError):
    """调用方请求停止同步时抛出的可识别异常。"""



def _sync_batch(
    orders: list[WorkOrder],
    database: Database,
) -> tuple[int, int, int]:
    """写入一个批次并返回总数、新增数和变化数；空批次不打开事务。"""
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
    """同步用户时间窗口内全部工单并记录运行结果。

    平台结果按页读取，再按 ``SYNC_BATCH_SIZE`` 分批提交数据库；每页结束
    依据平台总数和当前页条数判断是否继续。``progress`` 接收百分比与中文
    状态，``cancelled`` 返回真值时停止后续请求并以 ``cancelled`` 标记运行。
    数据库异常或平台异常会先记录 ``sync_failed``，再原样向上抛出。
    """
    started = datetime.now(timezone.utc)
    run_id = database.start_sync_run(started.isoformat())
    total_seen = added = changed = 0
    try:
        start = started - timedelta(hours=config.lookback_hours)
        page_index = 1
        # 每轮先检查取消，避免取消后仍发起下一页网络请求。
        while not (cancelled and cancelled()):
            page = client.query_work_orders(
                user.login_id,
                page_index=page_index,
                page_size=config.page_size,
                start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=started.strftime("%Y-%m-%d %H:%M:%S"),
            )
            batch = list(page.items)
            for offset in range(0, len(batch), SYNC_BATCH_SIZE):
                # 批次之间再次检查，避免长事务阻塞取消响应。
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
            # 以服务端 total 为准，但空页也必须终止，防止异常接口造成死循环。
            if page_index * page.page_size >= page.total or not page.items:
                break
            page_index += 1
        if cancelled and cancelled():
            # 已写入批次保持提交态，同时让调用方明确知道同步未完成。
            raise SyncCancelled("同步已取消")
        finished = datetime.now(timezone.utc)
        database.finish_sync_run(run_id, total=total_seen, added=added, changed=changed)
        if progress:
            progress(100, f"同步完成：{total_seen} 条")
        return SyncSummary(total_seen, added, changed, 0, started, finished)
    except SyncCancelled:
        # 取消是受控终止，保留已处理统计但不伪装为成功完成。
        database.finish_sync_run(
            run_id, total=total_seen, added=added, changed=changed, error="cancelled"
        )
        raise
    except Exception:
        # 先落库失败状态，再重新抛出原异常供上层告警或重试。
        database.finish_sync_run(run_id, total=total_seen, added=added, changed=changed, error="sync_failed")
        raise
