from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class UserInfo:
    login_id: str
    display_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class WorkOrder:
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
        record = asdict(self)
        record["raw"] = self.raw
        return record


@dataclass(frozen=True)
class WorkOrderPage:
    items: list[WorkOrder]
    total: int
    page_index: int
    page_size: int


@dataclass(frozen=True)
class TodoTask:
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
    items: list[TodoTask]
    total: int
    page_index: int
    page_size: int


@dataclass(frozen=True)
class SyncSummary:
    total: int
    added: int
    changed: int
    completed: int
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True)
class WorkOrderFilters:
    keyword: str = ""
    status: str = ""
    node: str = ""
    start_time: str = ""
    end_time: str = ""
    page_index: int = 1
    page_size: int = 50
