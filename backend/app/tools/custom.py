from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, create_model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capabilities import secrets as vault
from app.capabilities.endpoints import bound_endpoint_ref, find_endpoint
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
        return "参数定义必须是一个 JSON 对象"
    props = schema.get("properties")
    if props is not None and not isinstance(props, dict):
        return '参数定义格式有误：properties 必须是一个对象，每个参数一项，例如 {"n": {"type": "integer"}}'
    for key, spec in (props or {}).items():
        if not str(key).strip():
            return "参数定义格式有误：存在空的参数名，请为其命名"
        if str(key).startswith("_"):
            return (f"参数定义格式有误：参数名 {key} 不能以下划线开头，"
                    f"请改为 {str(key).lstrip('_') or 'value'} 这样的名称")
        if str(key).startswith("model_") and hasattr(BaseModel, str(key)):
            return f"参数定义格式有误：参数名 {key} 与内部保留的名称冲突，请换一个名称"
        if not isinstance(spec, dict):
            # 示例照着他写的那个类型给，照抄就能改好：写了 "string" 却让人改成 integer 只会添乱
            shown = json.dumps(spec, ensure_ascii=False)
            meant = _type_guess(spec) or "string"
            return (f'参数定义格式有误：参数 {key} 应写成 {{"type": "{meant}"}} 这样的对象，'
                    f"不能直接写 {shown}")
        kind = spec.get("type", "string")
        if not isinstance(kind, str) or kind not in _TYPES:
            shown = kind if isinstance(kind, str) else json.dumps(kind, ensure_ascii=False)
            meant = _type_guess(kind)
            guess = f"，是否应为 {meant}" if meant else ""
            return (f"参数定义格式有误：无法识别参数 {key} 的类型「{shown}」{guess}；"
                    f"只能是 {'、'.join(_TYPES)} 中的一个")
        if not isinstance(spec.get("description", ""), str):
            return f"参数定义格式有误：参数 {key} 的 description 必须是文字"
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(k, str) for k in required)
    ):
        return '参数定义格式有误：required 必须是参数名的列表，例如 ["n"]'
    # 上面没认出来的写法，最后按运行时同一个转换真建一次：放行的一定建得出来
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")   # 和 BaseModel 同名的参数（json、copy）只是警告，照样能用
            schema_to_model("probe", schema)
    except Exception:  # noqa: BLE001
        return "参数定义格式有误：无法据此生成工具的参数表，请检查参数名和每个参数的 type"
    return None


def broken_tool_message(name: str, problem: str) -> str:
    """库里存着的坏工具被节点绑上时的报错：哪个工具、坏在哪、去哪里改。"""
    return f"自定义工具「{name}」的{problem}。到「工具」页修改参数定义后再运行"


class ToolFailure(dict):
    """工具自己报的失败：沙箱里代码出错、地址被拦、请求体不是 JSON。

    运行时照旧当成 {"error": …} 交给模型——它看了会换个参数再试；但试跑时要
    认得出这是失败，不能在界面上标「成功」再贴一段报错。所以是个 dict 的子类：
    序列化出来和原来一模一样，只多了一个能 isinstance 的身份。
    """


async def _run_http(row: CustomTool, args: dict[str, Any]) -> Any:
    """HTTP 模板工具：url / headers / body 里可以用 {{ 参数名 }} 插值。headers 的值存的是密文，这里解开再插值。"""
    config = row.config or {}
    ctx = dict(args)
    url = render_template(config.get("url", ""), ctx)
    headers = render_deep(vault.reveal(config.get("headers")), ctx)
    if problem := vault.header_problem(headers):
        # 发不出去的头（空名字、换行）：照实说是哪一项，不是一句 LocalProtocolError
        return ToolFailure(error=f"自定义工具「{row.name}」的{problem}，请到「工具」页修改请求头")
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


async def _run_info(session: AsyncSession, run_id: str) -> tuple[bool, str | None, bool]:
    """(正式运行？, 发起人, 是不是在一次真实的运行里)。工具库试用、试跑不在运行里：按探索运行算，发起人是当前请求的人。"""
    from app.db.models import Run

    run = await session.get(Run, run_id) if run_id else None
    if run is None:
        from app.api.deps import current_principal

        return False, current_principal().username, False
    return run.run_class == "formal", run.started_by, True


#: 效果从宽到严：只读、视参数而定、写入
_STRICTNESS = {"read": 0, "dynamic": 1, "write": 2}


def _system_effect(row: CustomTool) -> str:
    """按同步的规矩（capabilities.registry.sync_all）算这一行的效果：绑定端点的按方法，GET / HEAD 为只读、其余为写入；
    别的视参数而定。"""
    if bound_endpoint_ref(row):
        return "read" if str((row.config or {}).get("method") or "GET").upper() in ("GET", "HEAD") else "write"
    return "dynamic"


def _effect_of(row: CustomTool, cap: Any) -> str:
    """治理后的效果；登记里还没有（刚建、没保存的草稿）按同步的规矩算。

    治理是对登记时的那个样子做的：手上这一行按同步的规矩算出来的和登记里的（effect_system）对不上——工具页试运行的草稿
    把 GET 改成了 POST——取两者里更严的一个，不然草稿凭已保存那一版的「只读」把写入真发出去。对得上的（保存过的那一行）
    照治理的走：管理员把一个用 POST 的查询接口治理成只读，是他的决定。"""
    from app.capabilities.registry import effective_effect

    own = _system_effect(row)
    if cap is None:
        return own
    governed = effective_effect(cap)
    if own == getattr(cap, "effect_system", own):
        return governed
    return max(governed, own, key=lambda e: _STRICTNESS.get(e, len(_STRICTNESS)))


async def held_write(session: AsyncSession, row: CustomTool) -> bool:
    """这个自定义工具是不是写外部系统的能力（capabilities.registry.writes_externally，和运行时、发布检查同一个判断）。
    是的话不在运行里（工具页试运行、工具库执行）一律演练、不发送：对外写入只经调用工具节点，要确认、带幂等键、留生效
    记录（A5）。绑定的端点已经没了的不算：执行时照实报端点不存在。"""
    from app.capabilities.registry import get_capability

    ref = bound_endpoint_ref(row)
    endpoint = await find_endpoint(session, ref) if ref else None
    if ref and endpoint is None:
        return False
    return _holds(row, await get_capability(session, row.name), endpoint.id if endpoint else None)


def _holds(row: CustomTool, cap: Any, endpoint_id: str | None) -> bool:
    from app.capabilities.registry import writes_externally

    return writes_externally(row.name, _effect_of(row, cap), endpoint_id)


def _write_refused(name: str) -> ToolFailure:
    """写入能力只能经调用工具节点发送：运行里别的路（Agent、直接调用）碰到它时的报错。"""
    return ToolFailure(error=f"「{name}」是写入能力，只能经「调用工具」节点的动作确认后发送")


async def _run_endpoint(row: CustomTool, args: dict[str, Any], ctx: ToolContext) -> Any:
    """绑定登记端点的 HTTP 工具：由平台执行器发请求（capabilities/endpoints.py），模型只给参数、拿不到任意地址。

    写入（held_write：治理后效果为 write，或者视参数而定——执行器分不出读写）不从这里发：运行里它要先留生效记录、按幂等键
    发送、结果未知时转核实，那是调用工具节点的动作流程（app/process/actions.py）；工具页试运行、工具库执行（不在运行里）
    只演练，交回将要发送的请求（endpoints.rehearsal）。读取照常发，按探索运行走测试环境，没有测试环境时走生产环境。

    请求按手上这一行拼（tool=row）：工具页试运行的草稿发的是眼前编辑的方法、路径和请求体，不是按名字找到的已保存的那一版。"""
    from app.capabilities import endpoints as ep
    from app.capabilities.registry import endpoint_request, get_capability
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        endpoint = await ep.find_endpoint(session, ep.bound_endpoint_ref(row))
        if endpoint is None:
            return ToolFailure(error=f"自定义工具「{row.name}」绑定的端点不存在，可能已被删除。请到「工具」页重新选择端点")
        cap = await get_capability(session, row.name)
        effect = _effect_of(row, cap)
        formal, starter, in_run = await _run_info(session, ctx.run_id)
        if _holds(row, cap, endpoint.id):
            if in_run:
                return _write_refused(row.name)
            try:
                return ep.rehearsal(endpoint, row.config or {}, cap, args)
            except ep.EndpointError as e:
                return ToolFailure(error=str(e))
        environment = ep.pick_environment(endpoint, formal=formal, effect=effect)
        result = await endpoint_request(session, cap if cap is not None else row.name, args,
                                        environment=environment, on_behalf=starter,
                                        run_id=ctx.run_id if in_run else None, node_id=ctx.node_id, tool=row)
    out = ep.tool_result(result)
    return ToolFailure(out) if "error" in out else out


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
                value = await _execute(bound_row, kwargs, ctx)
                if isinstance(value, str):
                    return value
                return json.dumps(value, ensure_ascii=False, default=str, indent=2)

            return _run

        meta: dict[str, Any] = {"trust_key": row.name}
        if ref := bound_endpoint_ref(row):
            endpoint = await find_endpoint(session, ref)
            if endpoint is not None:
                # 时限按端点登记的走：界面上写「已超出 N 秒上限」，引擎到点收回控制权（engine/toolcalls.limit_of）
                meta["timeout_s"] = endpoint.timeout_s
        tools.append(
            StructuredTool(
                name=row.name,
                description=row.description or f"自定义工具 {row.name}",
                args_schema=args_model,
                coroutine=_make(row),
                func=None,
                # 信任三档按它认工具（trust.py）；和工具列表里的 id 一致
                metadata=meta,
            )
        )
    return tools


def _rehearse_unbound(row: CustomTool, args: dict[str, Any]) -> dict[str, Any]:
    """没绑定端点、被治理为写入的自定义工具在工具页试运行、工具库执行时的演练回执：HTTP 工具交回将要发送的方法、地址、
    请求体和请求头的名字；Python 工具不执行代码，交回这次的参数。"""
    from app.capabilities.endpoints import REHEARSAL_MESSAGE

    config = row.config or {}
    if row.kind == "python":
        return {"rehearsal": True, "message": "演练：写入能力不在这里真的执行，代码没有运行", "args": args}
    body = config.get("body")
    if body:
        rendered = render_deep(body, args) if isinstance(body, dict) else render_template(str(body), args)
        try:
            body = rendered if isinstance(rendered, dict) else json.loads(rendered or "null")
        except json.JSONDecodeError:
            body = rendered
    return {"rehearsal": True, "message": REHEARSAL_MESSAGE,
            "request": {"method": str(config.get("method") or "GET").upper(),
                        "url": render_template(config.get("url", ""), args), "body": body or None,
                        "headers": sorted(config.get("headers") or {})}}


async def _execute(row: CustomTool, args: dict[str, Any], ctx: ToolContext) -> Any:
    if bound_endpoint_ref(row):
        return await _run_endpoint(row, args, ctx)
    from app.db.base import SessionLocal

    # 没绑定端点、被治理为写入的（effect=write）：和绑定端点的写入一样，运行里不从这里发，试运行只演练
    async with SessionLocal() as session:
        if await held_write(session, row):
            return _write_refused(row.name) if (await _run_info(session, ctx.run_id))[2] else _rehearse_unbound(row, args)
    if row.kind == "python":
        return await _run_python(row, args, ctx)
    return await _run_http(row, args)


def http_failure(row: CustomTool, result: Any) -> dict[str, Any] | None:
    """HTTP 工具（绑定端点的、模板的）交回的结果带着非 2xx 的状态码：试运行、工具库执行照实说失败和状态码，
    交回 {error, hint, detail}；2xx 和别的结果交回 None。运行里照旧原样交给模型，它看得到状态码。结果的形状是平台拼的
    （{status, data | body}），工具库执行经 call_tool 拿到的是它的 JSON 文本，一并认。"""
    from app.capabilities.endpoints import describe_status

    if row.kind != "http":
        return None
    value = result
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    status = value.get("status") if isinstance(value, dict) else None
    if not isinstance(status, int) or isinstance(status, bool) or 200 <= status < 300:
        return None
    hint = ("请检查认证头" if status in (401, 403) else "请检查路径和地址" if status == 404
            else "请对照对方返回的内容（见技术细节）检查参数和请求体" if 400 <= status < 500
            else "通常是对方系统的问题，请稍后重试或联系对方" if status >= 500 else "")
    return {"error": f"请求失败：{describe_status(status)}", "hint": hint,
            "detail": json.dumps(value, ensure_ascii=False, default=str)[:4000]}


async def trial(row: CustomTool, args: dict[str, Any], ctx: ToolContext) -> tuple[Any, str | None]:
    """试跑一次，参数的处理和运行时一样：先按参数 schema 校验、纠正，再执行。

    row 可以是还没保存的草稿。以前试跑直接把参数原样塞进去，运行时却要过
    schema——试跑通过的参数，到了工作流里被拒。参数不对抛 ToolArgsError。

    返回 (结果, 纠正说明或 None)。参数名写错被替换时，运行时会把说明报出去；
    试跑也得报，否则用户照着一份「跑通了」的错参数去配工作流。
    """
    payload, note = prepare_args(schema_to_model(row.name or "draft", row.parameters or {}), args)
    return await _execute(row, payload, ctx), note
