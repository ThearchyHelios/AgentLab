"""发起一次运行：HTTP 发起（api/runs.py 的 start_run）和定时任务到点发起共用的那套规则。

以前这些规则全写在 start_run 里、只认 HTTP 请求：定时任务要发起正式运行，要么另抄一份（两份规则迟早
走样，正式运行「只能从当前发布版本发起」这条最要紧的规则就可能在一边被绕过），要么假装成一次 HTTP 请求。
现在两边都调 launch_run。拒绝时抛 LaunchRefused：start_run 把它换回以前的 HTTP 响应（状态码、detail、
code 逐字不变），调度器据此记一次未能发起。别的异常（库出错之类）也说明运行没有开始执行：发起时定下的事件
（受管级别、目录变化、口径升版处置）和运行记录同一个事务提交，执行任务是最后一步才建的。

发起前检查绑定的工具（missing_tools），以及它和 runner 固定数据版本共用的取绑定的办法（tool_bindings、
node_setting）也放在这里：runner 以前要从接口层导入它们。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import artifact_store
from app.core.events import EventType
from app.data.catalog_impact import catalog_drift
from app.apps.versions import is_published
from app.db.models import Run, Workflow, WorkflowVersion
from app.engine.governance import governance_note, unresolved_caliber_upgrades
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, NodeType, type_label

logger = logging.getLogger(__name__)

#: 发起时发现绑定的工具在本机不存在（见 missing_tools）
TOOL_MISSING = "run_tool_missing"
#: 正式运行引用的版本没有发布过（草稿快照）、已经退役、和调用方带来的 graph_hash 对不上（应用平台 A1）
VERSION_UNPUBLISHED = "run_version_unpublished"
VERSION_RETIRED = "run_version_retired"
VERSION_HASH_MISMATCH = "run_version_hash_mismatch"
#: 发布之后这一版用到的能力的治理版本变了：只提醒、不拦（同数据目录漂移）
CAPABILITY_DRIFT = "capability_drift"


class LaunchRefused(Exception):
    """不发起这次运行。status_code 和 message 就是 HTTP 发起时回的状态码和 detail；code 是给程序认的机器码，
    没有的是 None（HTTP 响应里也就没有 code 字段）。"""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


async def launch_run(
    session: AsyncSession,
    *,
    workflow_id: str | None = None,
    graph: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
    memory_scope: str | None = None,
    collection: str | None = None,
    run_class: str = "exploratory",
    version: int | None = None,
    actor: str | None,
    schedule_id: str | None = None,
    options: dict[str, Any] | None = None,
    origin_kind: str | None = None,
    origin: dict[str, Any] | None = None,
    app_id: str | None = None,
    app_version: int | None = None,
    expect_hash: str | None = None,
) -> Run:
    """发起一次运行，返回刚建好的运行记录（执行已经交给 run_manager）。不发起就抛 LaunchRefused；抛别的异常时
    运行也没有开始执行。

    应用平台（docs/design/app-platform-and-look.md §2.1、§2.10 X3）：
    - origin_kind / origin：运行来源（engine/origin.py 的 ORIGIN_KINDS）。记在运行上，同时把 run.origin 事件放进
      notes（在封存范围内）。调用方：POST /api/runs（客户端只能声明 CLIENT_ORIGINS）、定时任务（schedule）、
      应用入口和视图（app_*）、入站钩子（hook）。
    - app_id / app_version：应用发起的运行属于哪个应用版本。
    - expect_hash：应用发起正式运行时必须带上钉住版本的 graph_hash，和快照对不上就拒绝（run_version_hash_mismatch）。
      正式运行接受「已发布且未退役」的版本（app/apps/versions.py 的 is_published，F1）：没发布过的拒绝
      （run_version_unpublished），退役的拒绝（run_version_retired）。
    - 用到的能力的治理快照写进 options["capabilities"]；能力版本相对发布时有变化只记一条提醒（capability_drift）。

    参数和 POST /api/runs 的请求体一一对应（input 叫 input_payload）；actor 是记在运行上的发起人。
    schedule_id 是发起它的定时任务，options 是整次运行沿用的选项快照，都原样交给 run_manager.start；图里有双盲复算、
    反方审稿节点时 options 另加核查设置的快照（_with_verify_snapshot）。
    """
    # 没挂在工作流上（画布没保存就发起、问数据当场搭的）。老数据里是「临时图」，前端两种都认
    name = "未保存的工作流"
    run_version: int | None = None
    version_hash: str | None = None
    governed = False
    formal = run_class == "formal"
    # 发布时记下的能力版本（正式运行才比，见 _with_capability_snapshot）
    published_capabilities: dict[str, Any] | None = None

    if formal:
        # 正式运行的全部语义就这一条规则：必须引用一个不可变的已发布版本。
        # 传裸 graph、或工作流还没发布过，都进不了 formal。
        if not workflow_id:
            raise LaunchRefused(400, "正式运行需要从已保存的工作流发起：请求中没有指定工作流")
        if graph is not None:
            raise LaunchRefused(400, "正式运行只能使用已发布的版本，画布上未发布的修改不会生效。请先保存并发布")
        workflow = await session.get(Workflow, workflow_id)
        if not workflow:
            raise LaunchRefused(404, "工作流不存在，可能已被删除")
        # 只看 published_version：status 反映的是当前画布（改过图就退回 draft），
        # 而 formal 用的是已发布的那一版，两者本来就可以不一致
        if not workflow.published_version:
            raise LaunchRefused(409, f"「{workflow.name}」还没有发布版本，请先在编排页发布")
        run_version = version or workflow.published_version
        snapshot = (
            await session.execute(
                select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == workflow.id,
                    WorkflowVersion.version == run_version,
                )
            )
        ).scalar_one_or_none()
        if not snapshot:
            raise LaunchRefused(404, f"版本 v{run_version} 不存在")
        # "不可变"和"过过闸"是两件事。快照确实不可变，但 PATCH 保存出来的
        # 草稿版本同样有快照——只认 version 存不存在的话，工作流只要发布过
        # 任意一个版本，之后所有草稿都能以 formal 身份启动，发布期的
        # validate_graph + lint_for_publish 全部绕过。
        # 以前只认当前发布版（F1），应用就固定不住旧的发布版；现在认「已发布且未退役」，说法只有 versions.is_published 一处
        if snapshot.retired_at is not None:
            raise LaunchRefused(
                409, f"v{run_version} 已退役，不再接受新的正式运行。请改用当前发布版本（v{workflow.published_version}）",
                VERSION_RETIRED)
        if not is_published(snapshot, current=workflow.published_version):
            raise LaunchRefused(
                409,
                f"v{run_version} 没有发布过（当前发布版本为 v{workflow.published_version}）。"
                "正式运行只能使用已发布的版本；如需运行这一版，请先发布它。",
                VERSION_UNPUBLISHED,
            )
        graph = snapshot.graph
        version_hash = snapshot.graph_hash or artifact_store.graph_hash(graph)
        published_capabilities = snapshot.capability_versions
        # 应用发起时带着固定版本的指纹：快照和应用发布时看到的不是同一张图就不发起（按 graph_hash 钉死）
        if expect_hash and expect_hash != version_hash:
            raise LaunchRefused(
                409, f"v{run_version} 的内容指纹和发起方记录的不一致，不发起这次正式运行", VERSION_HASH_MISMATCH)
        name = workflow.name
        # 按这一版发布时记下的级别；记级别之前发布的老版本没有，照旧看工作流现在的 status
        governed = (snapshot.level == "governed") if snapshot.level else workflow.status == "governed"

        try:
            spec = GraphSpec.model_validate(graph)
        except ValueError as e:
            # 快照里的图不成形。以前这里没接，由 main.py 的 ValueError 兜底回 400、detail 是校验原话；
            # 原样换成拒绝，HTTP 响应不变，调度器也拿得到一个明确的拒绝
            raise LaunchRefused(400, str(e)) from e
        # 口径升版是被迫处置的事件：引用的方法卡有新版本而未声明策略，拒绝启动
        errors, upgrade_events = await unresolved_caliber_upgrades(session, spec)
        if errors:
            raise LaunchRefused(409, "；".join(errors))
        # 数据目录和口径卡不同：目录是给人和助手看的说明，不是运行的输入。发布之后变了只提醒、不拦——
        # 拦下来等于让一次文字修订卡住正式出具
        catalog_changes = await catalog_drift(session, snapshot.catalog_versions)
    else:
        upgrade_events = []
        catalog_changes = []
        if workflow_id:
            workflow = await session.get(Workflow, workflow_id)
            if not workflow:
                raise LaunchRefused(404, "工作流不存在，可能已被删除")
            graph = graph or workflow.graph
            name = workflow.name
    if not graph:
        raise LaunchRefused(400, "没有要运行的内容：请求中既没有工作流 ID，也没有工作流内容")
    await _refuse_missing_tools(session, graph)
    options = await _with_verify_snapshot(session, graph, options)
    options, capability_changes = await _with_capability_snapshot(
        session, graph, options, published=published_capabilities)

    from app.api.settings import resolve_run_scope

    memory_scope, collection = await resolve_run_scope(session, memory_scope, collection)
    # 发起时就定下的事件交给 run_manager.start，和运行记录同一个事务写进事件流（在封存范围内、排在 run.started
    # 前面）。以前运行建好、开始执行之后才另外写：写失败时运行已经在执行，这里却抛出异常，定时任务会把一次
    # 正在执行的正式运行记成未能发起
    notes: list[tuple[EventType | str, str | None, dict[str, Any]]] = []
    if formal:
        # 按不按受管出具在发起这一刻定下来：status 反映的是当前画布，中途改图、审批停下再恢复时再去读它，
        # 报告节点和出口复核可能各读到不同的值
        notes.append(("log", None, governance_note(governed=governed, workflow_name=name, version=run_version)))
    if catalog_changes:
        # 之后回看这次正式出具，能知道当时的目录和发布时不一样、差在哪几张表。每张表带 impact：direct 写死的
        # SQL 用到的，possible Agent 可能查询的（界面分开说）
        notes.append((EventType.CATALOG_DRIFT, None,
                      {"version": run_version, "count": len(catalog_changes), "tables": catalog_changes}))
    if capability_changes:
        # 能力的治理（读写效果、审批、遮罩……）发布之后改过：同目录漂移，只提醒、不拦，之后回看这次运行知道差在哪
        notes.append(("log", None, {
            "level": "warning", "code": CAPABILITY_DRIFT, "version": run_version, "capabilities": capability_changes,
            "message": f"v{run_version} 发布之后，用到的 {len(capability_changes)} 项能力的治理设置有变化："
                       + "、".join(f"「{c['name']}」" for c in capability_changes[:8])
                       + ("等" if len(capability_changes) > 8 else "")}))
    # 已声明策略的升版记入事件流，出具物上能看到"这期换口径了、怎么处置的"
    for ev in upgrade_events:
        payload_ev = dict(ev)
        node = payload_ev.pop("node_id", None)
        notes.append(("caliber.upgrade", node, payload_ev))
    if origin_kind:
        from app.engine.origin import origin_note

        data = {**(origin or {}), "actor": actor, "app_id": app_id, "app_version": app_version}
        data.pop("kind", None)
        notes.insert(0, (EventType.RUN_ORIGIN, None, origin_note(origin_kind, **data)))
    try:
        run = await run_manager.start(
            graph=graph,
            input_payload=input_payload if input_payload is not None else {},
            workflow_id=workflow_id,
            workflow_name=name,
            memory_scope=memory_scope,
            collection=collection,
            run_class="formal" if formal else "exploratory",
            version=run_version,
            version_hash=version_hash,
            started_by=actor,
            options=options,
            schedule_id=schedule_id,
            notes=notes,
            origin_kind=origin_kind,
            origin=origin,
            app_id=app_id,
            app_version=app_version,
        )
    except ValueError as e:
        raise LaunchRefused(400, str(e)) from e
    return run


def graph_tool_names(graph: dict[str, Any]) -> list[str]:
    """图里绑定的工具名（去重，按出现顺序），能力快照和发布时记能力版本都按它取。模板拼出来的名字发起时还不知道是谁，
    不算。图不成形时为空。"""
    try:
        spec = GraphSpec.model_validate(graph)
    except Exception:  # noqa: BLE001 - 图本身不成形交给 run_manager.start 报
        return []
    return list(dict.fromkeys(name for _, name in _bindings(spec)))


async def _with_capability_snapshot(session: AsyncSession, graph: dict[str, Any], options: dict[str, Any] | None, *,
                                    published: dict[str, Any] | None
                                    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """用到的能力的治理快照放进 options["capabilities"]（A6.4：写进 run.started，恢复和重放用同一份），以及相对发布时
    （WorkflowVersion.capability_versions）有变化的能力 [{name, published, current}]。

    没有登记的能力时快照为空，options 原样不动，run.started 和以前逐字相同。published 为空（探索运行、记能力版本之前
    发布的老版本）不比。"""
    from app.capabilities.registry import capability_snapshot, capability_versions_for

    names = graph_tool_names(graph)
    if not names:
        return options, []
    snapshot = await capability_snapshot(session, names)
    if snapshot and not (isinstance(options, dict) and "capabilities" in options):
        options = {**(options or {}), "capabilities": snapshot}
    changes: list[dict[str, Any]] = []
    if published:
        current = await capability_versions_for(session, names)
        for name, was in published.items():
            if current.get(name) != was:
                changes.append({"name": name, "published": was, "current": current.get(name)})
    return options, changes


#: 运行中途要读核查设置的节点：双盲复算、反方审稿
_VERIFY_NODES = (NodeType.DUAL_CHECK, NodeType.CHALLENGE)


async def _with_verify_snapshot(session: AsyncSession, graph: dict[str, Any],
                                options: dict[str, Any] | None) -> dict[str, Any] | None:
    """图里（含子工作流）有双盲复算或反方审稿节点时，把核查设置（api/settings.py 的 verify 一组）快照进
    options["verify"]。

    这两种节点在运行中途读设置（开关、上限、核查模型）。审批停下期间有人改了设置，恢复后的那一段读到的就和前面
    不一样：本该复算的被关掉了、重放时走了另一条路，正式运行也说不清当时按什么规则核的。所以在发起这一刻定下来，
    节点只读运行选项里的这一份（同 _approval_default 的道理；子工作流的节点经运行上下文拿到的是同一份选项）。没有
    这两种节点的图 options 原样不动，run.started 和以前逐字相同；调用方已经给了 options["verify"] 的照它的。
    """
    from app.api.settings import VERIFY_GROUP, verify_settings

    if isinstance(options, dict) and VERIFY_GROUP in options:
        return options
    try:
        spec = GraphSpec.model_validate(graph)
    except Exception:  # noqa: BLE001 - 图本身不成形交给 run_manager.start 报
        return options
    if not await _has_verify_nodes(session, spec):
        return options
    return {**(options or {}), VERIFY_GROUP: await verify_settings(session)}


async def _has_verify_nodes(session: AsyncSession, spec: GraphSpec) -> bool:
    """图里有没有双盲复算、反方审稿节点，子工作流里的也算。

    子工作流照运行时的取法读（同 runner._bound_sources）：固定了版本的读那一版，没固定的读工作流现在的图。读不到、
    图不成形的跳过——那个节点运行时自己会报错。同一个工作流（同一版）只读一次，互相引用也不会转圈。
    """
    seen: set[tuple[str, str]] = set()
    queue: list[GraphSpec] = [spec]
    while queue:
        graph = queue.pop(0)
        if any(node.type in _VERIFY_NODES for node in graph.nodes):
            return True
        for node in graph.nodes:
            if node.type != NodeType.SUBGRAPH:
                continue
            wid, pinned = (node_setting(graph, node, k) for k in ("workflow_id", "workflow_version"))
            key = (str(wid or ""), str(pinned or ""))
            if not wid or key in seen:
                continue
            seen.add(key)
            try:
                # 只取图：Workflow 的 versions 是 selectin，整行读会把所有历史版本一起读出来
                query = (select(WorkflowVersion.graph).where(
                    WorkflowVersion.workflow_id == wid, WorkflowVersion.version == int(pinned))
                    if pinned else select(Workflow.graph).where(Workflow.id == wid))
                sub = (await session.execute(query)).scalar_one_or_none()
                if sub:
                    queue.append(GraphSpec.model_validate(sub))
            except Exception:  # noqa: BLE001 - 读不到、不成形：子工作流节点运行时会说清楚
                continue
    return False


# --------------------------------------------------------------------------
# 发起前检查绑定的工具
# --------------------------------------------------------------------------


def node_setting(spec: GraphSpec, node: Any, key: str) -> Any:
    """节点配置的实际取值，和运行时 NodeContext.cfg 同一个规则：节点上没写（None、空串）就取图级 defaults。"""
    value = node.config.get(key)
    return spec.defaults.get(key) if value in (None, "") else value


def tool_bindings(spec: GraphSpec) -> list[tuple[str, str]]:
    """图里每一处绑定的工具：(谁绑的, 工具名)。调用工具节点的 tool、Agent 的 tools、协作成员的 tools。

    取值和运行时一致（node_setting）：Agent 节点上没写 tools、协作节点上没写成员，运行时用的是
    图级 defaults 里的，这里也算它们——不然发起前检查放过了缺的工具，发起时固定数据版本也漏了源。

    模板拼出来的名字（{{ vars.tool }}）也在内：发起前检查（_bindings）跳过它们，发起时固定数据
    版本（engine/runner.py）要知道有没有这种名字——有的话说不准会用到哪个数据源。
    """
    out: list[tuple[str, str]] = []
    for node in spec.nodes:
        who = f"「{node.title}」（{type_label(node.type)}）"
        if node.type == NodeType.TOOL:
            names = [node_setting(spec, node, "tool")]
            out.extend((who, n) for n in names if isinstance(n, str) and n.strip())
        elif node.type == NodeType.AGENT:
            out.extend((who, n) for n in node_setting(spec, node, "tools") or []
                       if isinstance(n, str) and n.strip())
        elif node.type == NodeType.SUPERVISOR:
            for member in node_setting(spec, node, "agents") or []:
                if not isinstance(member, dict):
                    continue
                label = f"「{node.title}」的成员「{member.get('name') or '?'}」"
                out.extend((label, n) for n in member.get("tools") or [] if isinstance(n, str) and n.strip())
    return [(who, name.strip()) for who, name in out]


def _bindings(spec: GraphSpec) -> list[tuple[str, str]]:
    """发起前要检查的绑定。模板拼出来的工具名（{{ vars.tool }}）发起时还不知道是谁，交给运行时。"""
    return [(who, name) for who, name in tool_bindings(spec) if "{{" not in name]


async def missing_tools(session: AsyncSession, spec: GraphSpec) -> list[tuple[str, str, str]]:
    """本机不存在的绑定：(谁绑的, 工具名, 去哪接入：data / tools)。

    解析和运行时 build_tools 一致：内置（静态注册）、mcp:<服务>/<工具>、db_query__ / db_schema__
    <数据源>、其余按自定义工具。数据源和自定义工具只认启用着的——停用的运行时同样拿不到。
    MCP 只看服务登记着、启用着没有：连不连得上、工具还在不在，要连一次才知道，发起时不去等它，
    上次没连上或者工具清单里没有的只在日志里提一句。
    """
    from app.db.models import CustomTool, DataSource, McpServer
    from app.tools.datasource import QUERY_PREFIX, SCHEMA_PREFIX
    from app.tools.registry import all_specs

    bindings = _bindings(spec)
    if not bindings:
        return []
    builtin = all_specs()
    sources = {n for (n,) in await session.execute(select(DataSource.name).where(DataSource.enabled.is_(True)))}
    custom = {n for (n,) in await session.execute(select(CustomTool.name).where(CustomTool.enabled.is_(True)))}
    servers = {r.name: r for r in (await session.execute(select(McpServer))).scalars()}
    missing: list[tuple[str, str, str]] = []
    for who, name in bindings:
        if name.startswith("mcp:"):
            server, _, tool = name[4:].partition("/")
            row = servers.get(server)
            if row is None or not row.enabled:
                missing.append((who, name, "tools"))
            elif row.status == "error":
                logger.warning("%s 绑的 %s：MCP 服务 %s 上次没连上，运行时可能拿不到这个工具", who, name, server)
            elif tool not in ("", "*") and row.tools_cache and tool not in row.tools_cache:
                logger.warning("%s 绑的 %s：MCP 服务 %s 上次交回的工具清单里没有 %s", who, name, server, tool)
        elif name in builtin:
            continue
        elif name.startswith((QUERY_PREFIX, SCHEMA_PREFIX)):
            source = name[len(QUERY_PREFIX):] if name.startswith(QUERY_PREFIX) else name[len(SCHEMA_PREFIX):]
            if source not in sources:
                missing.append((who, name, "data"))
        elif name not in custom:
            missing.append((who, name, "tools"))
    return missing


async def _refuse_missing_tools(session: AsyncSession, graph: dict[str, Any]) -> None:
    """绑定的工具在本机不存在就不发起：以前要执行到那一步才失败，前面的步骤白做、钱白花。"""
    try:
        spec = GraphSpec.model_validate(graph)
    except Exception:  # noqa: BLE001 - 图本身不成形交给 run_manager.start 报
        return
    missing = await missing_tools(session, spec)
    if not missing:
        return
    lines = "；".join(f"{who}绑定的 {name}" for who, name, _ in missing[:8])
    more = f"等 {len(missing)} 处" if len(missing) > 8 else ""
    places = {where for _, _, where in missing}
    if places == {"data"}:
        fix = "请到「数据」页接入，或在节点中重新选择"
    elif places == {"tools"}:
        fix = "请到「工具」页接入，或在节点中重新选择"
    else:
        fix = "数据源工具请到「数据」页接入，其他工具请到「工具」页接入，或在节点中重新选择"
    raise LaunchRefused(422, f"绑定的工具不存在：{lines}{more}。{fix}", TOOL_MISSING)
