"""能力登记的对外接口（附录 C，BE-1 提供）。

capabilities 一行 = 图里一个可调用的工具（主键就是图里写的工具名，tools/registry.build_tools 的命名规则），分两层：
- 系统同步（sync_all）：内置工具、数据源（db_query__ / db_schema__）、自定义工具、MCP 工具。source_hash 比对，只改变了的；
  来源没了记 missing、不删行——运行和发布记录还引用它的版本。启动时同步一次，列表和快照时按需再同步；
- 人工治理（govern）：effect、approval、unattended_write、returns_schema、tabular、fields、masked、credential_mode、lookup、
  endpoint_id。每改一次 version 加一，写审计。

同步得出的效果（effect_system）只说系统知道的：
- 内置工具：有副作用的（ToolSpec.dangerous）为 dynamic，别的为 read；
- 数据源：只读源的查询、查表结构为 read；可写源的查询为 dynamic（SELECT 只读、DML 是写，看这一次的 SQL）；
- 自定义工具：python 和没绑定端点的 http 为 dynamic（平台不知道它做什么）；绑定端点的 http 按方法，GET / HEAD 为 read，
  其余为 write——「写」只出在登记的端点上，老工具一个都不会因为升级被当成写入能力拒掉；
- MCP：声明了 readOnlyHint 的为 read（只当建议，要人确认才免审批：免审批还得治理时把审批设成 none），别的为 dynamic。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class EndpointResult:
    status: int | None
    #: 响应体（解析后的 JSON 或文本）
    body: Any
    #: 按 tabular 落成的查询快照形状的工件 id（声明了 tabular 的读取才有）
    snapshot_artifact: str | None = None
    #: 网络层没拿到确定结果（超时、连接中断）
    unknown: bool = False
    error: str | None = None
    environment: str = "prod"


#: 治理字段：PUT /api/capabilities/{name}/governance 只认这些
GOVERNED_FIELDS = ("effect", "approval", "unattended_write", "returns_schema", "tabular", "fields", "masked",
                   "credential_mode", "lookup", "endpoint_id")
#: 同步时写的字段（source_hash 只按它们算）
_SYNCED_FIELDS = ("system", "title", "description", "params_schema", "effect_system")

#: 登记的进程内快照：同步、治理、端点改动时重算（refresh_cache）。is_platform_query 跑在节点的热路径上、发布检查
#: （engine/lint_capabilities.py）是同步函数，都不能每次查库
#: - 平台执行、能作证的端点读取
_platform_queries: set[str] = set()
#: - 每个能力的治理快照（governance_of 的样子，另加 system）
_view: dict[str, dict[str, Any]] = {}
#: - 每个端点有没有生产环境
_endpoint_prod: dict[str, bool] = {}


def _hash(found: dict[str, Any]) -> str:
    text = json.dumps(found, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _json_schema(model: Any) -> dict[str, Any]:
    """pydantic 模型转 JSON Schema；MCP 工具的参数定义本来就是一份 dict。"""
    if isinstance(model, dict):
        return model
    try:
        return model.model_json_schema() if hasattr(model, "model_json_schema") else {}
    except Exception:  # noqa: BLE001 - 第三方工具的参数定义转不出来，登记里就不放
        return {}


async def _discover(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """系统此刻知道的全部能力：{名字: 同步字段（含 endpoint_id，只对绑定端点的自定义工具、MCP 服务有意义）}。"""
    from app.capabilities.endpoints import bound_endpoint_ref, find_endpoint
    from app.db.models import CustomTool, DataSource, McpServer
    from app.tools.datasource import QUERY_PREFIX, SCHEMA_PREFIX, _QueryArgs, _SchemaArgs
    from app.tools.registry import all_specs

    found: dict[str, dict[str, Any]] = {}
    for name, spec in all_specs().items():
        found[name] = {"system": "builtin", "title": name, "description": spec.description,
                       "params_schema": _json_schema(spec.args_schema),
                       "effect_system": "dynamic" if spec.dangerous else "read"}

    for src in (await session.execute(select(DataSource))).scalars():
        found[f"{QUERY_PREFIX}{src.name}"] = {
            "system": src.name, "title": f"在「{src.name}」上执行 SQL", "description": src.description or "",
            "params_schema": _json_schema(_QueryArgs), "effect_system": "read" if src.readonly else "dynamic"}
        found[f"{SCHEMA_PREFIX}{src.name}"] = {
            "system": src.name, "title": f"查看「{src.name}」的表结构", "description": src.description or "",
            "params_schema": _json_schema(_SchemaArgs), "effect_system": "read"}

    for tool in (await session.execute(select(CustomTool))).scalars():
        ref = bound_endpoint_ref(tool)
        endpoint = await find_endpoint(session, ref) if ref else None
        if ref:
            method = str((tool.config or {}).get("method") or "GET").upper()
            effect = "read" if method in ("GET", "HEAD") else "write"
        else:
            effect = "dynamic"
        found[tool.name] = {"system": endpoint.name if endpoint else "custom", "title": tool.name,
                            "description": tool.description or "", "params_schema": tool.parameters or {},
                            "effect_system": effect, "endpoint_id": endpoint.id if endpoint else None}

    from app.tools.mcp_manager import mcp_manager

    for server in (await session.execute(select(McpServer))).scalars():
        loaded = {t.name: t for t in mcp_manager.cached_tools(server.name)}
        names = list(dict.fromkeys([*(str(n) for n in server.tools_cache or []), *loaded]))
        for tool_name in names:
            tool = loaded.get(tool_name)
            # 适配层把工具的 annotations（readOnlyHint、destructiveHint……）摊在 metadata 顶层
            read_only = bool(((getattr(tool, "metadata", None) or {}) if tool else {}).get("readOnlyHint"))
            found[f"mcp:{server.name}/{tool_name}"] = {
                "system": server.name, "title": tool_name, "description": (tool.description if tool else "") or "",
                "params_schema": _json_schema(getattr(tool, "args_schema", None)) if tool else {},
                "effect_system": "read" if read_only else "dynamic", "endpoint_id": server.endpoint_id}
    return found


async def sync_all(session: AsyncSession) -> int:
    """把内置工具、数据源、自定义工具、MCP 同步进 capabilities。返回变动的行数。启动时和各对象增删改时调。"""
    from sqlalchemy.dialects.sqlite import insert

    from app.db.base import utcnow
    from app.db.models import Capability

    found = await _discover(session)
    existing = {row.name: row for row in (await session.execute(select(Capability))).scalars()}
    changed = 0
    now = utcnow()
    fresh: list[dict[str, Any]] = []
    for name, item in found.items():
        synced = {k: item[k] for k in _SYNCED_FIELDS}
        digest = _hash({**synced, "endpoint_id": item.get("endpoint_id")})
        row = existing.get(name)
        if row is None:
            fresh.append({"name": name, **synced, "source_hash": digest, "synced_at": now,
                          "endpoint_id": item.get("endpoint_id")})
        elif row.source_hash != digest or row.missing:
            for key, value in synced.items():
                setattr(row, key, value)
            # 绑定端点由工具自己的配置定（自定义工具的 config.endpoint、MCP 服务的端点），登记里跟着它走
            if "endpoint_id" in item:
                row.endpoint_id = item["endpoint_id"]
            row.source_hash, row.synced_at, row.missing = digest, now, False
            changed += 1
    for name, row in existing.items():
        if name not in found and not row.missing:
            row.missing = True
            row.synced_at = now
            changed += 1
    if fresh:
        # 两个请求同时同步（列表页和发起运行撞在一起）：后到的那条插入什么都不做，不报主键冲突。不加锁（单进程 + SQLite）
        done = await session.execute(insert(Capability).values(fresh).on_conflict_do_nothing(index_elements=["name"]))
        changed += done.rowcount or 0
    if changed or fresh:
        await session.commit()
    await refresh_cache(session)
    return changed


async def refresh_cache(session: AsyncSession) -> None:
    """重算登记的进程内快照。能作证的端点读取：绑定登记端点的 http 自定义工具，治理后效果为 read，声明了 tabular。
    Python 自定义工具（用户代码在算）和 MCP（没有结构化回执）不算，不管登记里写了什么。"""
    from app.capabilities.endpoints import bound_endpoint_ref
    from app.db.models import Capability, CustomTool, Endpoint

    bound = {t.name for t in (await session.execute(select(CustomTool))).scalars() if bound_endpoint_ref(t)}
    rows = list((await session.execute(select(Capability))).scalars())
    _platform_queries.clear()
    _platform_queries.update(row.name for row in rows if row.name in bound and row.tabular and row.endpoint_id
                             and not row.missing and effective_effect(row) == "read")
    _view.clear()
    _view.update({row.name: {**governance_of(row), "system": row.system, "missing": row.missing} for row in rows})
    _endpoint_prod.clear()
    _endpoint_prod.update({e.id: bool((e.environments or {}).get("prod"))
                           for e in (await session.execute(select(Endpoint))).scalars()})


def cached_view(name: str) -> dict[str, Any] | None:
    """一个能力在进程内快照里的治理视图（发布检查用，不查库）。没同步过、登记里没有为 None。"""
    return _view.get(name)


def endpoint_has_prod(endpoint_id: str) -> bool | None:
    """端点有没有生产环境；快照里没有这个端点为 None。"""
    return _endpoint_prod.get(endpoint_id)


async def ensure_synced(session: AsyncSession, names: list[str] | None = None) -> None:
    """按需轻量同步（能力登记自己的接口用）：names 里有登记还没有的（刚建的数据源、刚加的自定义工具）才同步一次；不给 names
    就同步一次。同步用自己的会话提交，所以调用方的会话此刻不能开着写事务——别的包的发起、发布流程不调它，
    用只读的 capability_snapshot / capability_versions_for。"""
    from app.db.base import SessionLocal
    from app.db.models import Capability

    wanted = [n for n in dict.fromkeys(names or []) if n]
    if names is not None:
        if not wanted:
            return
        have = set((await session.execute(select(Capability.name).where(Capability.name.in_(wanted)))).scalars())
        if not set(wanted) - have:
            return
    async with SessionLocal() as own:
        await sync_all(own)


async def get_capability(session: AsyncSession, name: str) -> Any | None:
    """一行能力登记（db.models_capabilities.Capability），没有就是 None。"""
    from app.db.models import Capability

    return await session.get(Capability, name)


def effective_effect(capability: Any) -> str:
    """治理后的效果：effect 为空时按 effect_system。"""
    return getattr(capability, "effect", None) or getattr(capability, "effect_system", None) or "dynamic"


def writes_externally(name: str, effect: str | None, endpoint_id: str | None) -> bool:
    """这项能力是不是写外部系统、只能经调用工具节点的动作流程发送（留生效记录、按幂等键发、结果未知转核实）。

    治理后效果为 write 的是；绑定登记端点、由平台执行器发请求的（自定义工具）效果为视参数而定的也是：执行器不看参数，
    分不出这一次是读还是写，只能按写入对待——不然管理员把一个 POST 能力改成视参数而定，它就绕过动作流程直接往生产环境
    发了。MCP 绑定端点只管连到哪儿，调用由 MCP 服务执行、每次按参数问审批，不算。运行时（build_tools、调用工具节点、
    自定义工具的执行）和发布检查（engine/lint_capabilities.py）都按这一个判断。"""
    if effect == "write":
        return True
    return effect == "dynamic" and bool(endpoint_id) and not name.startswith("mcp:")


def governance_of(capability: Any) -> dict[str, Any]:
    """一行能力的治理快照（运行里固定下来的那一份）。"""
    return {"effect": effective_effect(capability), "approval": capability.approval or "inherit",
            "unattended_write": bool(capability.unattended_write), "version": capability.version,
            "endpoint_id": capability.endpoint_id, "masked": list(capability.masked or []),
            "lookup": capability.lookup, "credential_mode": capability.credential_mode or "service"}


def _default_governance(item: dict[str, Any]) -> dict[str, Any]:
    """还没进登记表的能力（刚建的数据源，登记还没同步到）按同步得出的样子算：没人治理过，版本是新行的 1。"""
    return {"effect": item["effect_system"], "approval": "inherit", "unattended_write": False, "version": 1,
            "endpoint_id": item.get("endpoint_id"), "masked": [], "lookup": None, "credential_mode": "service"}


async def _governed_rows(session: AsyncSession, tool_names: list[str]) -> dict[str, dict[str, Any]]:
    """{名字: 治理快照}。只读：发起运行、发布时调用方的会话可能正开着写事务，这里再起一个会话去写登记表，SQLite 会锁住
    （单进程、单写者）。登记表里还没有的名字按系统此刻知道的样子补（_default_governance），系统也不认识的不出现。"""
    from app.db.models import Capability

    names = [n for n in dict.fromkeys(tool_names) if n]
    if not names:
        return {}
    out = {row.name: governance_of(row) for row in
           (await session.execute(select(Capability).where(Capability.name.in_(names)))).scalars()}
    if missing := [n for n in names if n not in out]:
        found = await _discover(session)
        out.update({n: _default_governance(found[n]) for n in missing if n in found})
    return out


async def capability_snapshot(session: AsyncSession, tool_names: list[str]) -> dict[str, Any]:
    """运行发起时用到的能力的治理快照：{名字: {effect, approval, unattended_write, version, endpoint_id, masked, lookup,
    credential_mode}}。写进 Run.options["capabilities"]，恢复和重放用同一份。系统不认识的名字不出现。只读，不提交调用方的会话。"""
    return await _governed_rows(session, tool_names)


async def capability_versions_for(session: AsyncSession, tool_names: list[str]) -> dict[str, int]:
    """发布时记下的能力版本：{名字: version}。只读，同 capability_snapshot。"""
    return {name: view["version"] for name, view in (await _governed_rows(session, tool_names)).items()}


def is_platform_query(name: str) -> bool:
    """这个工具的结果能不能当查询快照作证（替换 nodes/tools.py、nodes/llm.py 里按名字前缀的判断，F11）。
    认的是「平台执行」这个身份：数据源查询工具，和登记里声明了 tabular、绑定端点、由平台执行的读取。不看结果长什么样。"""
    from app.tools.datasource import QUERY_PREFIX

    return name.startswith(QUERY_PREFIX) or name in _platform_queries


async def snapshot_owned(artifact_id: str, run_id: str, name: str) -> bool:
    """端点读取交回的快照是不是这次运行落下的 endpoint_snapshot（防伪的第二道：名字对了，工件也得对）。数据源查询工具的
    快照由数据层自己落，照旧不查。"""
    from app.tools.datasource import QUERY_PREFIX

    if name.startswith(QUERY_PREFIX):
        return True
    from app.capabilities.endpoints import SNAPSHOT_KIND
    from app.db.base import SessionLocal
    from app.db.models import Artifact

    async with SessionLocal() as session:
        row = await session.get(Artifact, (artifact_id, run_id or ""))
    return row is not None and row.kind == SNAPSHOT_KIND


async def endpoint_request(session: AsyncSession, capability: Any, args: dict[str, Any], *, environment: str,
                           idem_key: str | None = None, on_behalf: str | None = None,
                           run_id: str | None = None, node_id: str | None = None) -> EndpointResult:
    """平台执行器：按登记的端点发请求（只认登记的 base_url，参数按路径段编码代入），声明了 tabular 的读取落快照。
    实现在 capabilities/endpoints.py。"""
    from app.capabilities.endpoints import endpoint_request as run

    return await run(session, capability, args, environment=environment, idem_key=idem_key, on_behalf=on_behalf,
                     run_id=run_id, node_id=node_id)


def masked_paths(capability: Any) -> list[str]:
    """能力的遮罩字段（JSON 指针）。"""
    return list(getattr(capability, "masked", None) or [])


# ---------------------------------------------------------------- 治理


class GovernanceError(ValueError):
    """治理字段的值不对：给人看的一句话。"""


_EFFECT_TEXT = {"read": "只读", "write": "写入", "dynamic": "视参数而定"}


def validate_governance(changes: dict[str, Any]) -> dict[str, Any]:
    """治理字段的取值检查，交回规整后的值。"""
    from app.db.models_capabilities import APPROVALS, CREDENTIAL_MODES, EFFECTS

    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in GOVERNED_FIELDS:
            continue
        if key == "effect":
            if value is not None and value not in EFFECTS:
                raise GovernanceError(f"效果只能是{'、'.join(_EFFECT_TEXT.values())}之一")
        elif key == "approval":
            if value not in APPROVALS:
                raise GovernanceError("审批只能是跟随信任设置、总要确认、免审批之一")
        elif key == "credential_mode":
            if value not in CREDENTIAL_MODES:
                raise GovernanceError("凭据模式只能是服务凭据，或服务凭据加身份断言")
        elif key == "tabular" and value is not None:
            if not (isinstance(value, dict) and isinstance(value.get("rows"), str)
                    and isinstance(value.get("columns"), dict) and value["columns"]
                    and all(isinstance(v, str) and (v == "" or v.startswith("/")) for v in value["columns"].values())):
                raise GovernanceError("表格需要写明行的位置和各列的位置（JSON 指针，以 / 开头）")
        elif key == "masked":
            if not (isinstance(value, list) and all(isinstance(v, str) and v.startswith("/") for v in value)):
                raise GovernanceError("遮罩字段需要写成 JSON 指针的列表，每一项以 / 开头")
        elif key == "fields" and not isinstance(value, dict):
            # 逐项的取值、状态在 govern 里按数据目录「项」的规矩查（data/capability_review.normalize_fields）
            raise GovernanceError("字段语义需要写成按 JSON 指针分项的对象")
        elif key == "unattended_write":
            value = bool(value)
        out[key] = value
    return out


class VersionConflict(Exception):
    """治理字段在读到之后被别人改过（version 对不上）。"""


async def govern(session: AsyncSession, name: str, changes: dict[str, Any], *, version: int,
                 actor: str | None, verified: bool) -> Any:
    """改治理字段：version 乐观锁（条件 UPDATE，对不上就是别人先改了），version 加一，写审计。交回改后的那一行。"""
    from sqlalchemy import update

    from app.auth.audit import record
    from app.db.models import Capability

    await ensure_synced(session, [name])
    row = await session.get(Capability, name)
    if row is None:
        raise LookupError(name)
    clean = validate_governance(changes)
    if "fields" in clean:
        # 字段语义沿用数据目录「项」的规矩（data/capability_review.py）：人工新填、改过的记为已确认，没动的保持原状态，
        # 推断的项留给审阅队列。不这样的话，界面整份回写时带着的状态、来源都会被原样当真
        from app.data.capability_review import FieldsInvalid, normalize_fields

        try:
            clean["fields"] = normalize_fields(row.fields, clean["fields"])
        except FieldsInvalid as e:
            raise GovernanceError(str(e)) from e
    effect = (clean["effect"] if "effect" in clean else row.effect) or row.effect_system
    endpoint_id = clean["endpoint_id"] if "endpoint_id" in clean else row.endpoint_id
    if effect == "dynamic" and endpoint_id and not name.startswith("mcp:"):
        raise GovernanceError("绑定登记端点的能力由平台按登记发请求，分不出某一次是读还是写：效果只能是只读或写入")
    if clean.get("approval", row.approval) == "none" and effect != "read":
        raise GovernanceError("只有只读能力可以免审批：写入和视参数而定的能力，审批至少要跟随信任设置")
    before = {k: getattr(row, k) for k in clean}
    done = await session.execute(
        update(Capability).where(Capability.name == name, Capability.version == version)
        .values(**clean, version=version + 1, updated_by=actor))
    if done.rowcount == 0:
        await session.rollback()
        raise VersionConflict(name)
    await record(session, "capability.govern", "capability", name, actor=actor, verified=verified,
                 data={"version": version + 1, "before": before, "after": clean})
    await session.commit()
    session.expire_all()
    await refresh_cache(session)
    return await session.get(Capability, name)
