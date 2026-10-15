"""开登录后 user 角色的问数据运行能用什么（D-c，§3.12「问数据、助手」那一行，Q2）。

管理员开了「使用者可以问数据」之后，user 角色经 POST /api/runs 发起的只能是问数据当场搭的探索运行（api/runs.py 的
start_run 先查来源和运行类别）。图本身再按这里收窄，理由是同一条：对 user 角色来说遮罩就是边界（A7 第 6 条），它能
碰的只有「读数据、整理、撰写」这一类，不能执行代码、不能调外部系统、不能写任何东西：

- 节点只许：输入、模型、Agent、报告、输出、知识库检索、数据整形、条件分支、口径卡、合并查询、调用工具。代码、协作、
  子工作流、循环、人工审批、记忆写入、校验、双盲复算、反方审稿一律不许——后几种不是危险，而是问数据用不到，多放一种
  就多一处要单独核对遮罩的地方；
- 工具（Agent 绑的、调用工具节点调的同一份白名单，engine/launch.tool_bindings 一并列出）只许数据源工具（db_query__ /
  db_schema__，查询结果按 MASK_RESULTS_OPTION 在工具边界遮）和只读的内置工具（计算、当前时间、JSON 取值、知识库检索、
  记忆读取）。自定义工具、MCP、文件、网页、HTTP、代码执行、平台的写入能力（records_append 这类）一律不许；模板拼出来的
  工具名（{{ vars.tool }}）发起时不知道是谁，也不许。调用工具节点放行的理由：它和 Agent 调同一个查询工具等价——同一个
  ToolContext 带着遮罩、只读两个开关（engine/nodes/tools.py 的 _tool_ctx），参数由人写死反而比模型挑的更好核对；
  周期看板、异常处置这些模板的取数节点就是它（个人应用和问数据同一份白名单，blueprints/personal.policy_problem）；
- 数据源查询一律只读：可写源上的写语句由守卫拒掉，不交给审批（api/runs.py、个人应用刷新、个人蓝图试运行给运行打上
  READ_ONLY_OPTION）——Agent、调用工具节点的 approval 是发起人自己写在图里的，写成 never 就不问人了。能力治理里改成
  「写入」的工具在只读运行里同样不执行（调用工具节点在走动作流程之前拒，Agent 建工具时拒）；
- 记忆范围、知识库不由发起人挑：记忆和知识库里的内容不经遮罩，搭建者的 Agent 存进去的查询原文，换个范围就读到了。
  记忆固定为这位使用者自己的范围（chat_memory_scope；问数据里写不了记忆，它读到的只会是空的），知识库固定为设置里的
  运行默认值。节点上、图级 defaults 里、调用工具节点的参数里写了 memory_scope / collection 的图不许发起，请求体里带的
  同样拒（api/runs.py）。这只是发起前的一道：根子在运行时——遮罩、只读的运行里范围由运行定，节点配置改不了，kb_search
  的 collection 参数（参数名纠错改出来的、模型挑的）一律拒（tools/registry.ToolContext.fixed_scope）。

没在这里列出的节点类型、工具，以后加了也默认不许（白名单），要放开得先想清楚遮罩怎么守。
"""
from __future__ import annotations

from typing import Any

#: 问数据运行里 user 角色能用的节点类型（engine/schema.py 的 NodeType 取值）
CHAT_NODE_TYPES = frozenset({"input", "llm", "agent", "report", "output", "retrieve", "transform", "branch",
                             "metrics", "merge", "tool"})
#: Agent 能绑的只读内置工具（tools/builtin/）
CHAT_BUILTIN_TOOLS = frozenset({"calculator", "current_time", "json_query", "kb_search", "memory_recall"})
#: 运行时改记忆范围、知识库的配置键（engine/nodes 的 ctx.cfg("memory_scope") / ctx.cfg("collection")，图级 defaults 同样生效）
SCOPE_KEYS = ("memory_scope", "collection")
SCOPE_REFUSED = "没有权限：使用者在问数据中不能指定记忆范围和知识库，使用的是管理员设好的默认值"


def chat_memory_scope(actor: str | None) -> str:
    """user 角色问数据运行的记忆范围：每人一份，和搭建者的不相通。"""
    return f"chat:{actor or 'anonymous'}"


def _tool_allowed(name: str) -> bool:
    from app.tools.datasource import source_of_tool

    return name in CHAT_BUILTIN_TOOLS or bool(source_of_tool(name))


def _set(value: Any) -> bool:
    """配置里填了值。和运行时 NodeContext.cfg 同一个规则：只有 None、空串算没填——全是空白的串运行时也照用。"""
    return value not in (None, "")


def chat_graph_problem(graph: Any, *, where: str = "问数据") -> str | None:
    """这张图 user 角色能不能在问数据里运行：能用返回 None，不能用交回一句给人看的原因（403 的 detail）。

    图不成形（GraphSpec 校验不过）时也返回 None：交给 launch_run 照常报校验问题——两边用的是同一个 GraphSpec，过不了
    校验的图本来就发起不了。

    where 是说法里的「在哪」：个人应用（blueprints/personal.py）用的是同一份白名单，只把「问数据」换成「个人应用」。"""
    from app.engine.launch import node_setting, tool_bindings
    from app.engine.schema import GraphSpec, type_label

    if not isinstance(graph, dict):
        return None
    try:
        spec = GraphSpec.model_validate(graph)
    except ValueError:
        return None
    scope_refused = SCOPE_REFUSED.replace("问数据", where)
    if any(_set(spec.defaults.get(key)) for key in SCOPE_KEYS):
        return scope_refused
    for node in spec.nodes:
        if str(node.type) not in CHAT_NODE_TYPES:
            return (f"没有权限：使用者在{where}中不能使用「{type_label(node.type)}」节点（「{node.title}」）。"
                    "如需这类处理，请联系搭建者做成应用")
        if any(_set(node.config.get(key)) for key in SCOPE_KEYS):
            return f"{scope_refused}（「{node.title}」）"
        # 调用工具节点的参数直接交给工具：kb_search 的 collection 这类参数写了，同样是自己挑了范围。取值和运行时同一个规则
        # （node_setting：节点上没写就取图级 defaults 里的 args）。参数名写错、运行时被纠成 collection 的，这里认不出——
        # 运行时那一道由工具自己拒（tools/registry.ToolContext.fixed_scope，kb_search 只能检索运行定下的知识库）
        args = node_setting(spec, node, "args") if str(node.type) == "tool" else None
        if isinstance(args, dict) and any(_set(args.get(key)) for key in SCOPE_KEYS):
            return f"{scope_refused}（「{node.title}」）"
    for who, name in tool_bindings(spec):
        if "{{" in name:
            return f"没有权限：{who}的工具名由模板拼成，发起前无法确定是哪个工具，使用者在{where}中不能这样绑定工具"
        if not _tool_allowed(name):
            return (f"没有权限：{who}绑定了工具「{name}」。使用者在{where}中只能使用数据源查询和只读的内置工具"
                    "（计算、当前时间、JSON 取值、知识库检索、记忆读取）")
    return None


__all__ = ["CHAT_BUILTIN_TOOLS", "CHAT_NODE_TYPES", "SCOPE_REFUSED", "chat_graph_problem", "chat_memory_scope"]
