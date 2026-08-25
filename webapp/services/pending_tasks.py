"""待领取任务查询服务，供 API 路由与后台自动领取服务共享。

封装上游分页拉取、城市筛选和关键词可领取过滤，避免 API 路由与后台线程
重复实现同一套逻辑。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.platform.client import PlatformClient
from shared.config import AppConfig
from shared.models import TodoTask

_logger = logging.getLogger(__name__)


def query_all_todo_tasks(
    client: PlatformClient,
    login_id: str,
    *,
    assigned: bool,
    config: AppConfig,
    cities: tuple[str, ...] = (),
) -> list[TodoTask]:
    """分页拉取上游全部待领取/已领取任务，城市筛选在内存中完成。"""
    page_index = 1
    page_size = 100
    tasks: list[TodoTask] = []
    effective_page_size = 0
    while True:
        result = client.query_todo_tasks(
            login_id,
            assigned=assigned,
            page_index=page_index,
            page_size=page_size,
        )
        count = len(result.items)
        if count == 0:
            _logger.info(
                "待领取分页结束: page_index=%d total=%d collected=%d",
                page_index, result.total, len(tasks),
            )
            break
        effective_page_size = max(effective_page_size, count)
        _logger.info(
            "待领取分页: page_index=%d page_size=%d effective=%d total=%d count=%d collected=%d",
            page_index, page_size, effective_page_size, result.total, count, len(tasks),
        )
        tasks.extend(
            task for task in result.items
            if not cities or any(city in task.title for city in cities)
        )
        if page_index * effective_page_size >= result.total:
            break
        page_index += 1
    return tasks


def claimable_tasks(pending: list[TodoTask], keywords: tuple[str, ...]) -> list[TodoTask]:
    """返回标题命中任一目标关键词（默认阳江）的可领取任务。

    领取链路（无论自动还是手动触发）统一以这里允许的任务为准，防止越权领取
    与业务无关的任务。
    """
    return [task for task in pending if any(keyword in task.title for keyword in keywords)]