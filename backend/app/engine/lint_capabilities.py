"""发布检查：写入能力（应用平台 §3.13 的 action.* 四条）。导入时登记进 engine/governance.EXTRA_LINTS。

只看图和能力登记的进程内快照（capabilities/registry.cached_view，同步、治理时更新），不查库、不连网络：发布门禁是同步
函数，助手自查、一键升级也调它。登记里没有的工具（没同步过、来源已删）不在这里报——那是别的检查的事。

| 编号 | 条件 | 级别 |
| action.write_in_agent | Agent 或多 Agent 协作的工具清单里有写入能力，或者绑定了端点、由平台执行、视参数而定的能力（MCP 不算） | 各级都报错 |
| action.unattended_write | 调用工具节点用写入能力、审批策略为全部自动放行，而这个能力不允许无人值守写入 | 已发布级警告，受管级报错 |
| action.prod_missing | 写入能力绑定的端点没有生产环境（或者端点已被删除） | 各级都报错 |
| action.reconcile_unplanned | 写入能力没有登记查询能力：结果未知时只能转人工核实 | 警告 |

写入只出在绑定登记端点的能力上（数据源、MCP、没绑定端点的自定义工具同步出来都不是 write，除非管理员治理时改成写入）。
认法和运行时是同一个函数（capabilities.registry.writes_externally）。
视参数而定的数据源查询、有副作用的内置工具不算：它们每次调用按参数问审批，Agent 里照旧能用。
"""
from __future__ import annotations

import inspect
from typing import Any, Callable

from app.capabilities.registry import cached_view, endpoint_has_prod, writes_externally
from app.engine.governance import EXTRA_LINTS
from app.engine.labels import option_label, type_label
from app.engine.schema import GraphSpec, NodeType


def _always_error(flag: Callable[..., None], message: str, **kw: Any) -> None:
    """各级都报错的问题。governance 的 flag 只在受管级把 hard 变成错误；它支持 always 参数时用它，不支持时退回 hard
    （已发布级是警告，运行时 build_tools 另有一道拒绝写入能力进 Agent）。"""
    if "always" in inspect.signature(flag).parameters:
        flag(message, always=True, **kw)
    else:
        flag(message, hard=True, **kw)


def _agent_tools(node: Any) -> list[str]:
    cfg = node.config or {}
    if node.type == NodeType.AGENT:
        return [str(t) for t in cfg.get("tools") or []]
    if node.type == NodeType.SUPERVISOR:
        return [str(t) for agent in cfg.get("agents") or [] if isinstance(agent, dict)
                for t in agent.get("tools") or []]
    return []


def _unattended(node: Any, spec: GraphSpec) -> bool:
    """调用工具节点设成了不需确认：节点的审批策略是全部自动放行，或者节点没写、跟随的全图默认是全部自动放行。"""
    approval = (node.config or {}).get("approval")
    return approval == "never" or (approval in (None, "") and spec.defaults.get("approval") == "never")


def check(spec: GraphSpec, flag: Callable[..., None], *, level: str, strict: bool) -> None:
    for node in spec.nodes:
        who = f"「{node.title}」（{type_label(node.type)}）"
        for name in dict.fromkeys(_agent_tools(node)):
            view = cached_view(name)
            if view is None or not writes_externally(name, view["effect"], view.get("endpoint_id")):
                continue
            _always_error(
                flag, f"{who}的工具清单里有写入能力「{name}」：写入外部系统只能放在「调用工具」节点里，由动作确认后发送，"
                      "Agent 运行时也会拒绝它。请从工具清单中移除，改用「调用工具」节点",
                code="action.write_in_agent", node_id=node.id, field="tools")
        if node.type != NodeType.TOOL:
            continue
        name = str((node.config or {}).get("tool") or "")
        view = cached_view(name) if name else None
        if view is None or not writes_externally(name, view["effect"], view.get("endpoint_id")):
            continue
        if _unattended(node, spec) and not view["unattended_write"]:
            flag(f"{who}使用写入能力「{name}」，审批策略是「{option_label('approval', 'never')}」，而这项能力不允许"
                 "无人值守写入。请改为需要确认，或由管理员在能力登记中允许无人值守写入",
                 code="action.unattended_write", node_id=node.id, hard=True, field="approval")
        if view.get("endpoint_id") and endpoint_has_prod(view["endpoint_id"]) is not True:
            _always_error(flag, f"{who}使用的写入能力「{name}」绑定的端点不存在或没有配置生产环境，正式运行无处发送。"
                                "请到「工具 → 端点」补上生产环境",
                          code="action.prod_missing", node_id=node.id, field="tool")
        if not view.get("lookup"):
            flag(f"{who}使用的写入能力「{name}」没有登记查询能力：写入结果未知时无法自动核实，只能转人工核实。"
                 "可在能力登记中为它指定查询能力",
                 code="action.reconcile_unplanned", node_id=node.id, field="tool")


EXTRA_LINTS.append(check)
