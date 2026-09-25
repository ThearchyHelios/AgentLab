from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ValidationError

ToolFunc = Callable[..., Awaitable[Any]]


@dataclass
class ToolContext:
    """一次调用里工具能用到的运行期依赖。"""

    run_id: str = ""
    node_id: str = ""
    sandbox_session: str = "default"
    memory_scope: str = "default"
    collection: str = "default"
    emit: Callable[..., Awaitable[None]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    category: str
    args_schema: type[BaseModel]
    func: ToolFunc
    # 需要人工确认才执行（写文件、跑代码、发请求这类有副作用的）
    dangerous: bool = False
    # 函数第一个参数是否接收 ToolContext
    needs_context: bool = True

    def json_schema(self) -> dict[str, Any]:
        return self.args_schema.model_json_schema()


_REGISTRY: dict[str, ToolSpec] = {}


def register(
    name: str,
    description: str,
    category: str,
    args_schema: type[BaseModel],
    *,
    dangerous: bool = False,
    needs_context: bool = True,
) -> Callable[[ToolFunc], ToolFunc]:
    def deco(func: ToolFunc) -> ToolFunc:
        _REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            category=category,
            args_schema=args_schema,
            func=func,
            dangerous=dangerous,
            needs_context=needs_context,
        )
        return func

    return deco


def all_specs() -> dict[str, ToolSpec]:
    import app.tools.builtin  # noqa: F401  触发内置工具注册

    return dict(_REGISTRY)


def get_spec(name: str) -> ToolSpec | None:
    return all_specs().get(name)


def call_is_dangerous(tool: BaseTool | None, name: str, args: dict[str, Any]) -> bool:
    """这一次调用要不要人工确认。所有审批关卡都问这一个函数。

    内置工具危不危险是固定的（ToolSpec.dangerous）。动态工具要看这一次传了
    什么：同一个 db_query__<源>，SELECT 不用确认，可写源上的 DELETE 必须确认。
    它们把判定挂在工具的 metadata["dangerous_if"] 上。以前没有这一层，数据源
    工具查不到 ToolSpec，于是在任何审批模式下都被当成安全的。
    """
    spec = get_spec(name)
    if spec is not None:
        return spec.dangerous
    judge = ((tool.metadata or {}) if tool is not None else {}).get("dangerous_if")
    return bool(judge and judge(args))


def _stringify(value: Any) -> str:
    """工具结果最终是喂给模型的，所以统一成字符串；结构化数据转紧凑 JSON。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str, indent=2)


def build_tool(spec: ToolSpec, ctx: ToolContext) -> BaseTool:
    """把注册表里的定义 + 运行期上下文，绑成一个 LangChain 工具。"""

    async def _run(**kwargs: Any) -> str:
        if spec.needs_context:
            result = await spec.func(ctx, **kwargs)
        else:
            result = await spec.func(**kwargs)
        return _stringify(result)

    return StructuredTool(
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_schema,
        coroutine=_run,
        # 只给异步实现；同步路径直接报错，避免在事件循环里被阻塞调用
        func=None,
    )


async def build_tools(
    names: list[str], ctx: ToolContext, *, session: Any = None
) -> list[BaseTool]:
    """按名字列表组装工具。

    四种来源：内置（静态注册）、MCP（mcp:<server>/<tool>）、数据源
    （db_query__<源名> / db_schema__<源名>）、自定义（数据库里的 CustomTool）。
    后两种都是运行时从库里读出来动态构造的，不在静态注册表里。
    """
    from app.tools.datasource import QUERY_PREFIX, SCHEMA_PREFIX

    specs = all_specs()
    tools: list[BaseTool] = []
    mcp_names: list[str] = []
    datasource_names: list[str] = []
    custom_names: list[str] = []

    for name in names:
        if name.startswith("mcp:"):
            mcp_names.append(name)
        elif name in specs:
            tools.append(build_tool(specs[name], ctx))
        elif name.startswith((QUERY_PREFIX, SCHEMA_PREFIX)):
            datasource_names.append(name)
        else:
            custom_names.append(name)

    if datasource_names and session is not None:
        from app.tools.datasource import build_datasource_tools

        tools.extend(await build_datasource_tools(datasource_names, ctx, session))

    if mcp_names:
        from app.tools.mcp_manager import mcp_manager

        tools.extend(await mcp_manager.get_tools(mcp_names))

    if custom_names and session is not None:
        from app.tools.custom import build_custom_tools

        tools.extend(await build_custom_tools(custom_names, ctx, session))

    return tools


class ToolArgsError(ValueError):
    """工具参数对不上 schema。

    message 是直接给人和模型看的：报错里必须说清楚"收到了什么"和"它接受什么"，
    因为看到这句话的下一步动作就是照着改参数名。不要再往外包一层。
    """


_TYPE_LABELS = {
    "string": "字符串", "integer": "整数", "number": "数字",
    "boolean": "布尔", "array": "数组", "object": "对象",
}


def _type_label(prop: dict[str, Any]) -> str:
    """JSON Schema 的类型写成人话。可选字段是 anyOf[T, null]，要摊开看。"""
    names: list[str] = []
    for node in [prop, *(prop.get("anyOf") or [])]:
        kind = node.get("type")
        if isinstance(kind, str) and kind != "null":
            names.append(_TYPE_LABELS.get(kind, kind))
    return "/".join(dict.fromkeys(names)) or "任意"


def args_model_of(tool: BaseTool) -> type[BaseModel] | None:
    """工具的参数模型。

    MCP 这类动态工具的 args_schema 可能直接是一份 JSON Schema dict，拿不到
    pydantic 模型——那种只能原样放行，不能因为校验不了就拒绝调用。
    """
    schema = getattr(tool, "args_schema", None)
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema
    return None


def describe_args(
    schema: type[BaseModel],
    got: dict[str, Any] | None = None,
    error: ValidationError | None = None,
) -> str:
    """把"哪里不对、它到底要什么"写成一句能照着改的话。

    字段说明取 Field(description=...)，各工具定义里本来就写了，这里只是搬到报错里
    ——模型拿到这句话就能在下一步自己改对，不用人工介入。

    返回的永远是一句完整的话（"参数不对：…。它接受：…"），调用方直接拼在
    工具名后面即可，不用猜这次会不会缺个主语。
    """
    js = schema.model_json_schema()
    props: dict[str, Any] = js.get("properties") or {}
    required = [k for k in (js.get("required") or []) if k in props]

    fields: list[str] = []
    for key, prop in props.items():
        tag = "必填" if key in required else "可选"
        desc = str(prop.get("description") or "").strip()
        fields.append(f"{key}（{tag}，{_type_label(prop)}）" + (f" — {desc}" if desc else ""))
    tail = "它接受：" + "；".join(fields) if fields else "它不接受任何参数。"

    got = dict(got or {})
    unknown = [k for k in got if k not in props]
    missing = [k for k in required if k not in got]
    problems: list[str] = []
    if unknown:
        problems.append(f"{'、'.join(unknown)} 不是它的参数")
    if missing:
        problems.append(f"缺少必填参数 {'、'.join(missing)}")
    for err in error.errors() if error else ():
        loc = ".".join(str(p) for p in (err.get("loc") or ()))
        if not loc or loc in unknown or loc in missing:
            continue  # 上面两条已经说过了，不重复
        # 用 schema 里的类型说，不要把 pydantic 那句英文原样甩出去
        problems.append(
            f"{loc} 的值不合法，应为{_type_label(props[loc])}"
            if loc in props
            else f"{loc} 的值不合法（{err.get('msg', '')}）"
        )

    head = "参数不对：" + "；".join(problems) + "。" if problems else "参数不对。"
    return f"{head}{tail}"


def prepare_args(
    schema: type[BaseModel], args: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """校验工具参数，必要时纠正一个明显写错的参数名。

    返回 (可直接展开给函数的 payload, 纠正说明或 None)。纠正说明不为空时调用方
    **必须**把它说出去——参数还错在配置里/还错在模型脑子里，替它跑通一次却不吭声，
    下一次照样踩。

    多余的参数名在这里是硬错误，哪怕校验能过：pydantic 默认把认不出的键悄悄丢掉，
    于是全可选字段的 schema 上 {"tabel": "orders"} 会"成功"执行一次什么都没查的调用。
    这种静默失败比报错难查得多。

    纠正只在候选唯一时发生。多于一个就有排列组合，猜对了也是运气，不如把 schema
    摊开让人或模型自己改。
    """
    args = dict(args or {})
    js = schema.model_json_schema()
    props: dict[str, Any] = js.get("properties") or {}
    required = [k for k in (js.get("required") or []) if k in props]

    unknown = [k for k in args if k not in props]
    if not unknown:
        try:
            return schema(**args).model_dump(), None
        except ValidationError as e:
            raise ToolArgsError(describe_args(schema, args, e)) from e

    # 少了哪个必填的，就是多出来那个想写的；一个必填都不缺时，退而看还空着哪个可选字段
    missing = [k for k in required if k not in args] or [k for k in props if k not in args]
    residual: ValidationError | None = None
    if len(unknown) == 1 and len(missing) == 1:
        fixed = dict(args)
        fixed[missing[0]] = fixed.pop(unknown[0])
        try:
            payload = schema(**fixed).model_dump()
        except ValidationError as e:
            # 改完名还是不合法。把这些毛病一并说出来，省得对方改对了名字
            # 再回来撞一次类型——describe_args 会跳过已经报过的那几个字段
            residual = e
        else:
            return payload, f"参数名 {unknown[0]} 不存在，已按唯一候选 {missing[0]} 执行"

    raise ToolArgsError(describe_args(schema, args, residual))


async def call_tool(
    name: str,
    args: dict[str, Any],
    ctx: ToolContext,
    session: Any = None,
    *,
    on_fix: Callable[[str], None] | None = None,
) -> Any:
    """直接调用一个工具（工具节点、以及设置页的"试一下"按钮用）。

    两条分支都先过 args_schema。动态工具那条以前是裸 `**args` 展开的，参数名写错
    时抛的是 TypeError，报错里只剩内部闭包名——`_make_query_tool.<locals>._run() got
    an unexpected keyword argument 'query'` 既指不到是哪个工具，也说不出该填什么。

    on_fix 在参数名被纠正时回调一次。call_tool 不该知道事件系统长什么样，
    所以只把这件事交出去，由调用方决定说给谁听。
    """
    spec = get_spec(name)
    if spec:
        payload, note = prepare_args(spec.args_schema, args)
        if note and on_fix:
            on_fix(note)
        if spec.needs_context:
            return await spec.func(ctx, **payload)
        return await spec.func(**payload)

    tools = await build_tools([name], ctx, session=session)
    if not tools:
        raise KeyError(f"找不到工具 {name!r}")
    tool = tools[0]

    model = args_model_of(tool)
    if model is None:
        payload = dict(args)
    else:
        payload, note = prepare_args(model, args)
        if note and on_fix:
            on_fix(note)

    if tool.coroutine:
        return await tool.coroutine(**payload)
    result = tool.invoke(payload)
    if inspect.isawaitable(result):
        return await result
    return result
