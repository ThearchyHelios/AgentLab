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
from app.tools.registry import ToolBuildError, ToolContext, prepare_args

_TYPES = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}

#: 手写参数定义时常见的类型写法（Python 的、其他语言的、缩写）。只用来在报错里
#: 猜「是不是想写 X」，不自动放行：悄悄按猜的类型建出来，试跑和运行对不上还没人知道
_TYPE_ALIASES = {
    "int": "integer", "long": "integer", "float": "number", "double": "number",
    "decimal": "number", "str": "string", "text": "string", "bool": "boolean",
    "list": "array", "dict": "object", "map": "object", "json": "object",
}


def _type_guess(value: Any) -> str | None:
    """value 像是哪个合法类型；认不出来返回 None。"""
    if not isinstance(value, str):
        return None
    low = value.strip().lower()
    return low if low in _TYPES else _TYPE_ALIASES.get(low)


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


def schema_problem(schema: Any) -> str | None:
    """参数定义哪里写得不对；没问题返回 None。

    参数定义是用户在编辑器里手写的 JSON。schema_to_model 只认 {"type": "integer"}
    这样的写法，写成 "int" 或 {"type": "int"} 时要么炸在 .get 上、要么悄悄当成
    字符串——前者会被说成「后端内部出错了」，后者试跑通过、到了模型那边类型不对。
    """
    if not isinstance(schema, dict):
        return "参数定义要是一个 JSON 对象"
    props = schema.get("properties")
    if props is not None and not isinstance(props, dict):
        return '参数定义格式不对：properties 要是一个对象，每个参数一项，比如 {"n": {"type": "integer"}}'
    for key, spec in (props or {}).items():
        if not str(key).strip():
            return "参数定义格式不对：有一个参数名是空的，给它起个名字"
        if str(key).startswith("_"):
            return (f"参数定义格式不对：参数名 {key} 不能以下划线开头，"
                    f"换成 {str(key).lstrip('_') or 'value'} 这样的名字")
        if str(key).startswith("model_") and hasattr(BaseModel, str(key)):
            return f"参数定义格式不对：参数名 {key} 和内部保留的名字冲突，换一个名字"
        if not isinstance(spec, dict):
            # 示例照着他写的那个类型给，照抄就能改好：写了 "string" 却让人改成 integer 只会添乱
            shown = json.dumps(spec, ensure_ascii=False)
            meant = _type_guess(spec) or "string"
            return (f'参数定义格式不对：参数 {key} 要写成 {{"type": "{meant}"}} 这样的对象，'
                    f"不能直接写 {shown}")
        kind = spec.get("type", "string")
        if not isinstance(kind, str) or kind not in _TYPES:
            shown = kind if isinstance(kind, str) else json.dumps(kind, ensure_ascii=False)
            meant = _type_guess(kind)
            guess = f"，是不是想写 {meant}" if meant else ""
            return (f"参数定义格式不对：参数 {key} 的类型「{shown}」认不出来{guess}；"
                    f"只能是 {'、'.join(_TYPES)} 里的一个")
        if not isinstance(spec.get("description", ""), str):
            return f"参数定义格式不对：参数 {key} 的 description 要是一段文字"
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(k, str) for k in required)
    ):
        return '参数定义格式不对：required 要是参数名的列表，比如 ["n"]'
    # 上面没认出来的写法，最后按运行时同一个转换真建一次：放行的一定建得出来
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")   # 和 BaseModel 同名的参数（json、copy）只是警告，照样能用
            schema_to_model("probe", schema)
    except Exception:  # noqa: BLE001
        return "参数定义格式不对：按它建不出工具的参数表，检查参数名和每个参数的 type"
    return None


def broken_tool_message(name: str, problem: str) -> str:
    """库里存着的坏工具被节点绑上时的报错：哪个工具、坏在哪、去哪里改。"""
    return f"自定义工具「{name}」的{problem}。到「工具」页把它的参数定义改好再运行"


class ToolFailure(dict):
    """工具自己报的失败：沙箱里代码出错、地址被拦、请求体不是 JSON。

    运行时照旧当成 {"error": …} 交给模型——它看了会换个参数再试；但试跑时要
    认得出这是失败，不能在界面上标「成功」再贴一段报错。所以是个 dict 的子类：
    序列化出来和原来一模一样，只多了一个能 isinstance 的身份。
    """


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
        return ToolFailure(error=str(e))
    except json.JSONDecodeError as e:
        return ToolFailure(error=f"请求体不是合法 JSON：{e}")

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
        return ToolFailure(error=result.error or result.stderr or "执行失败", exit_code=result.exit_code)
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
    # 保存时已经拒掉写坏的参数定义，但那道关卡之前存进去的还在库里。不交给
    # schema_to_model 去炸——那样节点只能报一句「'str' object has no attribute 'get'」，
    # 看不出是哪个工具。也不悄悄跳过它接着跑：少了一个它指望的工具，模型只会
    # 假装调过、编出一个答案。绑了它的节点失败，协作成员这一步记为失败交给调度者
    broken = [broken_tool_message(row.name, problem) for row in rows
              if (problem := schema_problem(row.parameters or {}))]
    if broken:
        raise ToolBuildError("；".join(broken))
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


async def trial(row: CustomTool, args: dict[str, Any], ctx: ToolContext) -> tuple[Any, str | None]:
    """试跑一次，参数的处理和运行时一样：先按参数 schema 校验、纠正，再执行。

    row 可以是还没保存的草稿。以前试跑直接把参数原样塞进去，运行时却要过
    schema——试跑通过的参数，到了工作流里被拒。参数不对抛 ToolArgsError。

    返回 (结果, 纠正说明或 None)。参数名写错被替换时，运行时会把说明报出去；
    试跑也得报，否则用户照着一份「跑通了」的错参数去配工作流。
    """
    payload, note = prepare_args(schema_to_model(row.name or "draft", row.parameters or {}), args)
    if row.kind == "python":
        return await _run_python(row, payload, ctx), note
    return await _run_http(row, payload), note
