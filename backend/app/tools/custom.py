from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, create_model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CustomTool
from app.engine.expressions import render_deep, render_template
from app.sandbox.base import SandboxLimits
from app.sandbox.manager import sandbox_manager
from app.tools.net import UnsafeUrlError, safe_request
from app.tools.registry import ToolContext

_TYPES = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def schema_to_model(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """把 JSON Schema 转成 pydantic 模型，好让它能绑成 LangChain 工具。"""
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    fields: dict[str, Any] = {}
    for key, spec in props.items():
        py_type = _TYPES.get(spec.get("type", "string"), str)
        description = spec.get("description", "")
        if key in required:
            fields[key] = (py_type, Field(description=description))
        else:
            fields[key] = (py_type | None, Field(default=spec.get("default"), description=description))
    if not fields:
        fields["input"] = (str, Field(default="", description="工具输入"))
    return create_model(f"{name}_Args", **fields)  # type: ignore[call-overload]


async def _run_http(row: CustomTool, args: dict[str, Any]) -> Any:
    """HTTP 模板工具：url / headers / body 里可以用 {{ 参数名 }} 插值。"""
    config = row.config or {}
    ctx = dict(args)
    url = render_template(config.get("url", ""), ctx)
    headers = render_deep(config.get("headers") or {}, ctx)
    body = config.get("body")
    json_body = None
    if body:
        rendered = render_deep(body, ctx) if isinstance(body, dict) else render_template(str(body), ctx)
        json_body = rendered if isinstance(rendered, dict) else json.loads(rendered or "null")
    try:
        result = await safe_request(
            config.get("method", "GET"),
            url,
            headers={k: str(v) for k, v in (headers or {}).items()},
            json_body=json_body,
            timeout=config.get("timeout"),
        )
    except UnsafeUrlError as e:
        return {"error": str(e)}
    except json.JSONDecodeError as e:
        return {"error": f"请求体不是合法 JSON：{e}"}

    text = str(result.get("body", ""))
    if config.get("parse_json"):
        try:
            return {"status": result["status"], "data": json.loads(text)}
        except json.JSONDecodeError:
            return {"status": result["status"], "body": text}
    return {"status": result["status"], "body": text}


async def _run_python(row: CustomTool, args: dict[str, Any], ctx: ToolContext) -> Any:
    """Python 工具：用户代码跑在沙箱里，参数通过 args 变量注入。"""
    config = row.config or {}
    code = config.get("code", "")
    preamble = (
        "import json, sys\n"
        f"args = json.loads({json.dumps(json.dumps(args, ensure_ascii=False))})\n"
    )
    result = await sandbox_manager.run(
        preamble + code,
        language="python",
        limits=SandboxLimits(timeout=int(config.get("timeout") or 30)),
        session_id=ctx.sandbox_session,
    )
    if not result.ok:
        return {"error": result.error or result.stderr or "执行失败", "exit_code": result.exit_code}
    out = result.stdout.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


async def build_custom_tools(
    names: list[str], ctx: ToolContext, session: AsyncSession
) -> list[BaseTool]:
    rows = list(
        (
            await session.execute(
                select(CustomTool).where(
                    CustomTool.name.in_(names), CustomTool.enabled.is_(True)
                )
            )
        ).scalars()
    )
    tools: list[BaseTool] = []
    for row in rows:
        args_model = schema_to_model(row.name, row.parameters or {})

        def _make(bound_row: CustomTool):
            async def _run(**kwargs: Any) -> str:
                if bound_row.kind == "python":
                    value = await _run_python(bound_row, kwargs, ctx)
                else:
                    value = await _run_http(bound_row, kwargs)
                if isinstance(value, str):
                    return value
                return json.dumps(value, ensure_ascii=False, default=str, indent=2)

            return _run

        tools.append(
            StructuredTool(
                name=row.name,
                description=row.description or f"自定义工具 {row.name}",
                args_schema=args_model,
                coroutine=_make(row),
                func=None,
            )
        )
    return tools


async def run_custom_tool(
    session: AsyncSession, name: str, args: dict[str, Any], ctx: ToolContext
) -> Any:
    row = (
        await session.execute(select(CustomTool).where(CustomTool.name == name))
    ).scalar_one_or_none()
    if not row:
        raise KeyError(f"找不到自定义工具 {name!r}")
    if row.kind == "python":
        return await _run_python(row, args, ctx)
    return await _run_http(row, args)
