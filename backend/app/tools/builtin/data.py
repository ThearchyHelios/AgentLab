from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.engine.expressions import ExpressionError, eval_expression, resolve_path
from app.tools.registry import ToolContext, register


class CalculatorArgs(BaseModel):
    expression: str = Field(description="数学表达式，例如 (13*7 + 2) / 3")


@register(
    name="calculator",
    category="数据",
    description="计算一个数学表达式。支持 + - * / // % ** 和 min/max/abs/round/sum 等函数。",
    args_schema=CalculatorArgs,
)
async def calculator(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = CalculatorArgs(**kwargs)
    try:
        value = eval_expression(args.expression, {})
    except ExpressionError as e:
        return {"error": str(e)}
    except ZeroDivisionError:
        return {"error": "除以零"}
    return {"expression": args.expression, "result": value}


class CurrentTimeArgs(BaseModel):
    timezone_name: str = Field(default="Asia/Shanghai", description="IANA 时区名")


@register(
    name="current_time",
    category="数据",
    description="获取当前日期时间。模型不知道此刻是什么时候，需要时间信息时用这个。",
    args_schema=CurrentTimeArgs,
)
async def current_time(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = CurrentTimeArgs(**kwargs)
    try:
        tz = ZoneInfo(args.timezone_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    now = datetime.now(tz)
    return {
        "iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
        "timezone": str(tz),
    }


class JsonQueryArgs(BaseModel):
    data: str = Field(description="JSON 字符串")
    path: str = Field(description="点号路径，例如 results.0.title 或 items[2].name")


@register(
    name="json_query",
    category="数据",
    description="从 JSON 里按路径取值，避免让模型自己在长 JSON 里数括号。",
    args_schema=JsonQueryArgs,
)
async def json_query(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = JsonQueryArgs(**kwargs)
    try:
        parsed = json.loads(args.data)
    except json.JSONDecodeError as e:
        return {"error": f"JSON 解析失败：{e}"}
    return {"path": args.path, "value": resolve_path({"root": parsed, **(parsed if isinstance(parsed, dict) else {})}, args.path)}
