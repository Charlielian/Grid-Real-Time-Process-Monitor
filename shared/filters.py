from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from shared.config import normalize_cities


def parse_order_filters(args: Any) -> dict[str, Any]:
    cities = normalize_cities(args.getlist("city"))
    start_date = args.get("start_time", "", type=str).strip()
    end_date = args.get("end_time", "", type=str).strip()
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
    return cities
