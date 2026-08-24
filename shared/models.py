"""跨层共享的数据模型。

这些不可变数据类承载认证用户、工单、待办任务、分页结果和同步统计，隔离
平台原始 JSON 与业务层字段。字符串字段保留平台可能缺失的值为空字符串，
``raw`` 保存未裁剪的原始字典供兼容解析和诊断使用；分页模型同时记录总数
及当前页参数，便于调用方判断后续分页。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class UserInfo:
    """认证服务确认的用户身份及平台原始信息。"""
    login_id: str
    display_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class WorkOrder:
    """统一表示平台工单的核心字段和原始响应。"""
    order_id: str
    number: str = ""
    title: str = ""
    status: str = ""
    current_node: str = ""
    assignee: str = ""
    created_at: str = ""
    due_at: str = ""
    process_instance_id: str = ""
    task_id: str = ""
    process_version: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_record(self) -> dict[str, Any]:
        """转换为普通字典；保留 ``raw`` 以供数据库序列化或诊断。"""
        record = asdict(self)
        record["raw"] = self.raw
        return record


@dataclass(frozen=True)
class WorkOrderPage:
    """工单分页结果，``total`` 是服务端总数而非当前页长度。"""
    items: list[WorkOrder]
    total: int
    page_index: int
    page_size: int


@dataclass(frozen=True)
class TodoTask:
    """统一表示待领取/待办任务及其关联工单字段。"""
    task_id: str
    order_id: str = ""
    number: str = ""
    title: str = ""
    current_node: str = ""
    assignee: str = ""
    process_instance_id: str = ""
    process_definition_key: str = ""
    created_at: str = ""
    due_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class TodoTaskPage:
    """待办任务分页结果及服务端分页信息。"""
    items: list[TodoTask]
    total: int
    page_index: int
    page_size: int


@dataclass(frozen=True)
class SyncSummary:
    """一次同步的统计结果和 UTC 起止时间。"""
    total: int
    added: int
    changed: int
    completed: int
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True)
class WorkOrderFilters:
    """工单列表查询条件；分页从 1 开始，默认每页 50 条。"""
    keyword: str = ""
    status: str = ""
    node: str = ""
    start_time: str = ""
    end_time: str = ""
    page_index: int = 1
    page_size: int = 50
