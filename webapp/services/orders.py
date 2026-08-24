"""工单实时查询服务。

应用不再把上游工单快照持久化到本地数据库，一切以平台当前数据为准：按用户
和配置的时间窗口分页拉取工单，再在内存中应用关键字、状态、节点和地市过滤，
供页面首屏与 JSON 局部刷新共用同一套查询逻辑。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from backend.platform.client import PlatformClient
from shared.config import AppConfig
from shared.models import WorkOrder

# 平台没有办结状态的统一枚举，这里按已知取值归一化判断。
_COMPLETED_STATUSES = ("completed", "done", "已办结", "办结", "finish", "finished")
_ACTIVE_STATUSES = ("active", "running", "未办结", "处理中", "待处理", "open", "unfinished")


def _window(config: AppConfig) -> tuple[str, str]:
    """返回查询时间窗口（本地时间字符串），未配置时按最近 lookback_hours 小时。"""
    end = datetime.now()
    start = end - timedelta(hours=config.lookback_hours)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def _matches(order: WorkOrder, *, keyword: str, status: str, node: str, cities: tuple[str, ...]) -> bool:
    """按关键字、状态、节点和地市对单条工单做内存过滤。"""
    if keyword and not any(
        keyword in value
        for value in (order.number, order.title, order.assignee, order.order_id)
        if value
    ):
        return False
    if status:
        current = order.status
        if status == "completed" and current not in _COMPLETED_STATUSES:
            return False
        if status == "active" and current in _COMPLETED_STATUSES:
            return False
        if status not in ("active", "completed"):
            # 其余状态值按子串匹配，保留平台自有状态文本的筛选能力。
            if status not in current:
                return False
    if node and node not in order.current_node:
        return False
    if cities and not any(city in order.title for city in cities):
        return False
    return True


def fetch_work_orders(
    client: PlatformClient,
    login_id: str,
    config: AppConfig,
    *,
    keyword: str = "",
    status: str = "",
    node: str = "",
    cities: tuple[str, ...] = (),
    start_time: str = "",
    end_time: str = "",
) -> list[WorkOrder]:
    """实时拉取时间窗口内全部工单并应用筛选，返回筛选后的完整列表。

    ``start_time``/``end_time`` 缺省时使用配置的 lookback_hours 窗口。分页拉取
    直到平台返回不足一页或总数耗尽为止；单页网络或格式错误向上抛出，由路由层
    映射成统一错误码。
    """
    start_time = start_time or _window(config)[0]
    end_time = end_time or _window(config)[1]
    collected: list[WorkOrder] = []
    page_index = 1
    effective_page_size = 0  # actual max items per page from the platform
    while True:
        page = client.query_work_orders(
            login_id,
            page_index=page_index,
            page_size=config.page_size,
            start_time=start_time,
            end_time=end_time,
        )
        count = len(page.items)
        if count == 0:
            break
        effective_page_size = max(effective_page_size, count)
        collected.extend(
            order for order in page.items
            if _matches(order, keyword=keyword, status=status, node=node, cities=cities)
        )
        if page_index * effective_page_size >= page.total:
            break
        page_index += 1
    return collected