"""Web 查询筛选参数的解析与规范化。

模块把请求中的日期、状态、节点、关键词和地市转换为数据库/平台查询使用
的结构。日期结束边界采用次日零点的半开区间语义，既覆盖结束日全天又避免
依赖具体时间精度；未提供日期时默认查询最近七个自然日。输入错误通过
``ValueError`` 返回给调用方处理，不在此处修改请求对象或执行查询。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from shared.config import normalize_cities


def parse_order_filters(args: Any) -> dict[str, Any]:
    """解析 Web 查询参数并返回可直接用于筛选的字典。

    ``args`` 需提供 Flask 风格的 ``get``/``getlist``；日期按 ISO 格式校验，
    结束时间使用次日零点作为排他上界。未传任何日期时默认最近七个自然日，
    日期倒置或格式错误抛出 ``ValueError``。
    """
    cities = normalize_cities(args.getlist("city"))
    start_date = args.get("start_time", "", type=str).strip()
    end_date = args.get("end_time", "", type=str).strip()
    # 没有日期筛选时限制默认窗口，避免列表页扫描全部历史数据。
    if not start_date and not end_date:
        today = date.today()
        start_date = (today - timedelta(days=6)).isoformat()
        end_date = today.isoformat()
    start_time = end_time = ""
    parsed_start = parsed_end = None
    try:
        if start_date:
            parsed_start = date.fromisoformat(start_date)
            start_date = parsed_start.isoformat()
            start_time = f"{start_date} 00:00:00"
        if end_date:
            parsed_end = date.fromisoformat(end_date)
            end_date = parsed_end.isoformat()
            end_time = f"{(parsed_end + timedelta(days=1)).isoformat()} 00:00:00"
    except ValueError as exc:
        # 对外隐藏 datetime 解析细节，统一为查询参数错误。
        raise ValueError("创建日期格式无效") from exc
    if parsed_start and parsed_end and parsed_start > parsed_end:
        raise ValueError("创建日期起始日期不能晚于结束日期")
    return {
        "keyword": args.get("keyword", "", type=str).strip(),
        "status": args.get("status", "", type=str).strip(),
        "node": args.get("node", "", type=str).strip(),
        "city": cities,
        "start_time": start_time,
        "end_time": end_time,
        "start_date": start_date,
        "end_date": end_date,
    }


def city_title_keywords(cities: tuple[str, ...]) -> tuple[str, ...]:
    """将已规范化的地市元组作为工单标题关键词返回。"""
    return cities
