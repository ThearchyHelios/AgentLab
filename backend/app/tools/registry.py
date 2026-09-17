from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

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


async def call_tool(name: str, args: dict[str, Any], ctx: ToolContext, session: Any = None) -> Any:
    """直接调用一个工具（工具节点、以及设置页的"试一下"按钮用）。"""
    spec = get_spec(name)
    if spec:
        validated = spec.args_schema(**args)
        payload = validated.model_dump()
        if spec.needs_context:
            return await spec.func(ctx, **payload)
        return await spec.func(**payload)

    tools = await build_tools([name], ctx, session=session)
    if not tools:
        raise KeyError(f"找不到工具 {name!r}")
    tool = tools[0]
    if tool.coroutine:
        return await tool.coroutine(**args)
    result = tool.invoke(args)
    if inspect.isawaitable(result):
        return await result
    return result
