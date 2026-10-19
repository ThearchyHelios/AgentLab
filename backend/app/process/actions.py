"""动作服务：写外部系统（附录 C，BE-2 提供；设计文档 §2.5 A5、§3.8）。

ActionRecord 是权威记录：先写记录再发送，结果未知永不自动重发。调用工具节点（engine/nodes/tools.py）在能力治理后
效果为 write 时走这里：

  1. prepare：先按 action_key 查记录——已生效的交回存下的回执，已发出 / 结果未知的挂起，失败且有人核实为「未生效，重新
     发送」的 attempt 加一；真要发送时才把渲染好的参数按能力的参数定义收成声明的类型（typed_args）→ payload_hash；
  2. （节点）需要确认时在 tool.start 之前中断（kind=action_confirm），恢复后重算哈希，对不上拒绝发送；
  3. send：先写 pending 并提交 → 改成 sent 并提交 → 发请求（幂等键 {action_key}:{attempt}）→ 2xx confirmed（回执落工件）、
     4xx（429 除外）failed、超时 / 中断 / 5xx unknown（抛 ActionPending，运行挂起、不封存）。

pending 和 sent 分开记，是为了重启之后说得清：pending 一定还没发出去，可以照原样再发；sent 可能已经发出去了，只能核实。
环境：正式运行走 prod；探索运行走 test，端点没有 test 环境时演练（不发请求，记 simulated，回执标 simulated: true）。

结果未知之后（on_unknown）：登记了查询能力（lookup）的，调度循环每拍跑 tick 带退避去查，查到就核实为已生效、系统接着跑；
查满 12 次还没结果、或者没登记查询能力的，开一条核实待办派给应用的 reconciler 岗位，选项是已生效 / 未生效重新发送 /
未生效放弃，核实之后系统接着跑。查询能力按幂等键查：调用参数是 {"idempotency_key": 幂等键}，2xx 算查到（回执取响应体），
别的（404 没查到、出错）都算还没结果。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventType
from app.db.base import SessionLocal, utcnow
from app.db.models import ActionRecord, Endpoint, Run

logger = logging.getLogger(__name__)

#: 查询能力最多查几次；第 n 次之后等 2^(n-1) 分钟再查，单次间隔最长 LOOKUP_CAP_MIN 分钟
LOOKUP_MAX = 12
LOOKUP_CAP_MIN = 60
#: 预览、回执摘要里遮罩值的写法
MASK = "••••"
#: 回执摘要最多存多少字（完整响应在回执工件里）
SUMMARY_CHARS = 2000
_ENV_LABEL = {"prod": "生产环境", "test": "测试环境", "simulated": "演练"}


class ActionPending(Exception):
    """写入的结果未知（或已发出、等核实）。runner 把运行收成 interrupted（挂起，不封存），等核实后接着跑。"""

    def __init__(self, action_id: str, message: str = "写入的结果未知，等待核实") -> None:
        super().__init__(message)
        self.action_id = action_id


class ActionFailed(Exception):
    """这一次写入确定没有生效（外部系统拒绝了、核实为未生效并放弃了）。节点据此失败，说明写在 message 里。"""


def action_key(run_id: str, node_id: str, exec_no: int) -> str:
    """sha256(run_id, node_id, exec)。同一次执行第 n 次发送的幂等键是 f"{action_key}:{attempt}"。"""
    return hashlib.sha256(f"{run_id}\x1f{node_id}\x1f{exec_no}".encode()).hexdigest()


def payload_hash(capability: str, args: dict[str, Any]) -> str:
    """确认卡上的内容指纹：能力名 + 渲染后的参数（真值，不是遮罩后的预览）。确认时看过的就是发出去的，靠它核对。"""
    from app.core.artifact_store import canonical_json

    return hashlib.sha256(canonical_json({"capability": capability, "args": args}).encode()).hexdigest()


def masked(doc: Any, pointers: list[str]) -> Any:
    """按 JSON 指针（RFC 6901）把值换成遮罩。指到不存在的路径就跳过。不改原对象。"""
    out = copy.deepcopy(doc)
    for pointer in pointers or []:
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            continue
        parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]
        node = out
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else (
                node[int(part)] if isinstance(node, list) and part.isdigit() and int(part) < len(node) else None)
            if node is None:
                break
        last = parts[-1]
        if isinstance(node, dict) and last in node:
            node[last] = MASK
        elif isinstance(node, list) and last.isdigit() and int(last) < len(node):
            node[int(last)] = MASK
    return out


# --------------------------------------------------------------------------
# 参数按声明的类型收好
# --------------------------------------------------------------------------

#: 参数类型的叫法（收不成声明的类型时说给人听）
_TYPE_TEXT = {"number": "数", "integer": "整数", "boolean": "是或否（true / false）", "object": "对象（JSON）",
              "array": "列表（JSON）"}
_INTEGER = re.compile(r"[+-]?\d+")
#: 文字里的数：和前端表单读数的写法一样（lib/schemaForm），不认 nan、inf、带下划线的
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


class _Untyped(Exception):
    def __init__(self, where: str, kind: str, value: Any) -> None:
        super().__init__(where)
        self.where, self.kind, self.value = where, kind, value


def typed_args(capability: Any, args: dict[str, Any]) -> dict[str, Any]:
    """按能力登记的参数定义（params_schema，JSON Schema）把渲染好的参数收成声明的类型。确认卡的预览、内容指纹、发出去的
    请求都用收好的这一份：看过的就是发出去的。

    模板渲染出来的一律是文字：{"amount": "{{ input.amount }}"} 得到 "1280.5"，照原样发，外部系统收到的金额是字符串。
    这里只收 number / integer / boolean，以及 object / array（是 JSON 文字的先解开，再按里面的定义逐层收）；声明为文字、
    没写类型的原样不动，参数定义里没有的键也原样不动——没登记参数定义的能力发出去的和以前一字不差。
    可选参数渲染出来是空的（模板没取到值）：不发这个键，不拿空串冒充数；必填的空着照实失败。
    收不成（"abc" 填给数）抛 ActionFailed，说清是哪个参数，一次都不发。"""
    schema = getattr(capability, "params_schema", None)
    if not isinstance(args, dict) or not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return args
    try:
        return _typed_object(schema, args, "")
    except _Untyped as e:
        title = getattr(capability, "title", None) or capability.name
        shown = e.value if isinstance(e.value, str) else json.dumps(e.value, ensure_ascii=False, default=str)
        if e.kind == "required":
            raise ActionFailed(f"「{title}」的必填参数 {e.where} 是空的（参数模板没有取到值），没有发送。"
                               "请检查节点里的参数和上游填的值") from e
        raise ActionFailed(f"「{title}」的参数 {e.where} 应为{_TYPE_TEXT.get(e.kind, e.kind)}，实际是「{shown[:80]}」，"
                           "没有发送。请检查节点里的参数和上游填的值") from e


def _kinds(spec: dict[str, Any]) -> list[str]:
    kind = spec.get("type")
    if isinstance(kind, list):
        return [k for k in kind if isinstance(k, str)]
    return [kind] if isinstance(kind, str) else []


def _typed_object(spec: dict[str, Any], obj: dict[str, Any], path: str) -> dict[str, Any]:
    props = spec.get("properties") if isinstance(spec.get("properties"), dict) else {}
    required = set(spec.get("required") or []) if isinstance(spec.get("required"), list) else set()
    out: dict[str, Any] = {}
    for key, value in obj.items():
        sub = props.get(key)
        where = f"{path}.{key}" if path else str(key)
        kinds = _kinds(sub) if isinstance(sub, dict) else []
        blank = value is None or (isinstance(value, str) and not value.strip())
        if blank and kinds and "string" not in kinds:
            if "null" in kinds:
                out[key] = None
            elif key in required:
                raise _Untyped(where, "required", value)
            continue
        out[key] = _typed(value, sub, where) if isinstance(sub, dict) else value
    return out


def _typed(value: Any, spec: dict[str, Any], where: str) -> Any:
    kinds = [k for k in _kinds(spec) if k != "null"]
    if not kinds or ("string" in kinds and isinstance(value, str)):
        return value
    first: _Untyped | None = None
    for kind in kinds:
        try:
            return _as(kind, value, spec, where)
        except _Untyped as e:
            first = first or e
    raise first or _Untyped(where, kinds[0], value)


def _as(kind: str, value: Any, spec: dict[str, Any], where: str) -> Any:
    if kind == "string":
        return value
    if kind in ("number", "integer"):
        number: Any = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = value
        elif isinstance(value, str):
            text = value.strip()
            if _INTEGER.fullmatch(text):
                number = int(text)
            elif _NUMBER.fullmatch(text):
                number = float(text)
        if number is None or isinstance(number, float) and not math.isfinite(number):
            raise _Untyped(where, kind, value)
        if kind == "integer":
            if isinstance(number, float) and not number.is_integer():
                raise _Untyped(where, kind, value)
            return int(number)
        return number
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise _Untyped(where, kind, value)
    if kind in ("object", "array"):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise _Untyped(where, kind, value) from None
        if kind == "object" and isinstance(value, dict):
            return _typed_object(spec, value, where)
        if kind == "array" and isinstance(value, list):
            items = spec.get("items")
            if not isinstance(items, dict):
                return value
            return [_typed(v, items, f"{where}[{i}]") for i, v in enumerate(value)]
        raise _Untyped(where, kind, value)
    return value


# --------------------------------------------------------------------------
# 准备与发送（调用工具节点用）
# --------------------------------------------------------------------------


@dataclass
class Prepared:
    key: str
    attempt: int
    payload_hash: str
    #: 遮罩后的参数：确认卡、事件、记录上给人看的都是这一份
    preview: Any
    environment: str
    endpoint: str | None
    #: 已经生效（或演练过）的：存下的回执，节点直接交回，不再发
    receipt: dict[str, Any] | None = None
    run: dict[str, Any] = field(default_factory=dict)
    #: 按参数定义收好类型的参数（typed_args）：哈希、预览按它算，send 发的也是它
    args: dict[str, Any] = field(default_factory=dict)
    #: 记录上写的节点 id：认出升级前那种键（legacy_node_id）时照它记，同一个键下的记录 node_id 一致，下一次还认得出
    node_id: str | None = None

    @property
    def idem_key(self) -> str:
        return f"{self.key}:{self.attempt}"


#: 按自己的键找到别人的那一笔时换用的键的前缀（prepare）：节点 id 里不会有这个字符
OWN_KEY_MARK = "\x1e"
#: 带 action_id 的那几种事件：认一条记录是哪个节点写的（_owner）
_ACTION_EVENTS = (EventType.ACTION_PREPARED, EventType.ACTION_SENT, EventType.ACTION_CONFIRMED, EventType.ACTION_FAILED,
                  EventType.ACTION_UNKNOWN, EventType.ACTION_SIMULATED)


async def _owner(session: AsyncSession, rec: ActionRecord) -> str | None:
    """这条记录是哪个节点写的（这次运行里的 id，子工作流里的带前缀）：按它的 action.* 事件上记的 node_id。记录上的 node_id
    升级前子工作流里记的是子图里的 id，和父图同名的节点分不开；事件一向按这次运行里的 id 记。认不出（事件没写上）为 None。"""
    from app.db.models import RunEvent

    rows = (await session.execute(select(RunEvent.node_id, RunEvent.data).where(
        RunEvent.run_id == rec.run_id, RunEvent.type.in_([str(t) for t in _ACTION_EVENTS])))).all()
    return next((str(node) for node, data in rows if node and isinstance(data, dict) and data.get("action_id") == rec.id),
                None)


def _same_payload(rec: ActionRecord, capability: Any, args: dict[str, Any]) -> bool | None:
    """这条记录是不是这一次要发的内容：能力相同、内容指纹等于这一次参数收好类型之后的指纹。收不成类型（参数定义改了）的
    比不了，为 None。"""
    name = capability.name
    if rec.capability != name:
        return False
    try:
        typed = typed_args(capability, args)
    except ActionFailed:
        return None
    return rec.payload_hash == payload_hash(name, typed)


async def _belongs(session: AsyncSession, rec: ActionRecord, node_id: str, capability: Any, args: dict[str, Any], *,
                   strict: bool = False) -> bool:
    """这条记录是不是这个节点（node_id，这次运行里的 id）的。事件认得出是谁写的就按事件；认不出的按内容（能力、内容指纹），
    比不了的当是（以前就是这样认的）。strict（认升级前的旧键时）：事件说是这个节点的，内容也要对得上——循环跨了升级时，
    同一个节点第二轮按老算法同样是第 1 次执行，找到的是第一轮那一笔；比不了内容（参数定义改了）的照事件认。"""
    owner = await _owner(session, rec)
    same = _same_payload(rec, capability, args)
    if owner is not None and owner != node_id:
        return False
    if owner is not None and not strict:
        return True
    return same is not False


async def latest(session: AsyncSession, key: str) -> ActionRecord | None:
    return await session.scalar(select(ActionRecord).where(ActionRecord.action_key == key)
                                .order_by(ActionRecord.attempt.desc()).limit(1))


async def prepare(*, run_id: str, node_id: str, exec_no: int, capability: Any, args: dict[str, Any],
                  legacy_node_id: str | None = None) -> Prepared:
    """节点每次执行都先走这里：按 action_key 查记录，决定发不发、第几次发、走哪个环境。node_id 是这次运行里的节点 id
    （子工作流里的带前缀）；legacy_node_id 是升级前子工作流里按子图里的 id 记的那种（tools._run_write 交进来），按它也没查到
    才算头一次。

    sent / unknown 抛 ActionPending（不发）；failed 且没有人核实为「重新发送」的抛 ActionFailed。先查记录再收类型：已生效
    （演练过）的交回存下的回执、已发出 / 结果未知的挂起，都和这一次渲染出来的参数无关——发出之后能力的参数定义改了、手上的
    值收不成声明的类型（升级时停在结果未知的运行），已经生效的写入照样交回回执，不判成失败。只有真要发送（头一次、记下了
    还没发出去、核实为未生效要重发）时才按参数定义收成声明的类型（typed_args），收不成的抛 ActionFailed；之后的哈希、
    预览、发送都用收好的那一份（Prepared.args）。"""
    from app.capabilities.registry import masked_paths

    name = capability.name
    key = action_key(run_id, node_id, exec_no)
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        endpoint = await _endpoint(session, capability)
        prev = await latest(session, key)
        if prev is not None and not await _belongs(session, prev, node_id, capability, args):
            # 按自己的键找到的是别的节点的那一笔：升级前子工作流里同名的节点按子图里的 id 记的键，和父图这个节点的一样。
            # 那一笔不是这个节点的，换一个只属于这个节点的键（同样的输入永远算出同一个）
            key = action_key(run_id, f"{OWN_KEY_MARK}{node_id}", exec_no)
            prev = await latest(session, key)
        if prev is None and legacy_node_id:
            # 升级前子工作流里的写入按子图里的节点 id 记幂等键（node_id 也记成它）：写入节点本身重新执行时（结果未知核实之后、
            # 失败后接着跑）照那个键认出已经记下的那一笔，接着用它的键（不重发、第几次发送接着数）。只认这个节点的、这一次的：
            # 那一笔的事件记着是这个节点写的（父图里同名的节点按同一个键写过），内容指纹也和这一次的一样（循环跨了升级时，
            # 第二轮按老算法同样是第 1 次执行，找到的是第一轮那一笔）
            old_key = action_key(run_id, legacy_node_id, exec_no)
            old = await latest(session, old_key)
            if old is not None and old.node_id == legacy_node_id \
                    and await _belongs(session, old, node_id, capability, args, strict=True):
                key, prev, node_id = old_key, old, legacy_node_id
    facts = {"app_id": run.app_id, "workflow_id": run.workflow_id, "workflow_version": run.version,
             "graph_hash": run.version_hash, "started_by": run.started_by} if run is not None else {}
    endpoint_name = endpoint.name if endpoint is not None else None
    if prev is not None and prev.state in ("confirmed", "simulated"):
        # 交回的是生效的那一次：指纹、预览、环境、第几次都照记录，不按这一次的参数重算
        return Prepared(key=key, attempt=prev.attempt, payload_hash=prev.payload_hash, preview=prev.preview,
                        environment=prev.environment, endpoint=endpoint_name, receipt=_receipt_of(prev), run=facts,
                        node_id=node_id)
    if prev is not None and prev.state in ("sent", "unknown"):
        raise ActionPending(prev.id)
    formal = run is not None and run.run_class == "formal"
    attempt = 1
    environment = "prod" if formal else "test" if endpoint is not None and (endpoint.environments or {}).get("test") \
        else "simulated"
    if prev is not None and prev.state == "pending":
        # 记下了、还没发出去（发之前进程没了）：照原样再发，同一个幂等键
        attempt, environment = prev.attempt, prev.environment
    elif prev is not None:
        outcome = (prev.resolution or {}).get("outcome")
        if outcome == "abandon":
            raise ActionFailed(f"「{name}」这次写入核实为未生效，已选择放弃发送")
        if outcome != "resend":
            raise ActionFailed(f"「{name}」上一次发送被外部系统拒绝（{prev.error or '未生效'}），不会自动重发。"
                               "请检查上游的数据后重新发起运行")
        attempt = prev.attempt + 1
    args = typed_args(capability, args)
    return Prepared(key=key, attempt=attempt, payload_hash=payload_hash(name, args),
                    preview=masked(args, masked_paths(capability)), environment=environment, endpoint=endpoint_name,
                    run=facts, args=args, node_id=node_id)


async def send(prep: Prepared, capability: Any, *, run_id: str, node_id: str, exec_no: int,
               emit: Callable[..., None], on_behalf: str | dict[str, Any] | None = None,
               confirm_node: str | None = None) -> dict[str, Any]:
    """先写记录再发送。返回节点的产出；结果未知抛 ActionPending，确定失败抛 ActionFailed。发的是 prepare 收好类型的
    那一份参数（prep.args），和确认卡上的哈希是同一份。

    on_behalf 原样交给平台执行器写身份断言：{"sub": 运行的发起人, "act": 确认人}（capabilities.endpoints.on_behalf_parts）。
    记录上的节点 id 照 prepare 认定的（prep.node_id）；confirm_node 是动作确认待办上记的节点 id（确认卡上的，子工作流里是
    子图里的 id），不给就按 node_id 找。"""
    from app.capabilities.registry import endpoint_request, masked_paths

    name = capability.name
    async with SessionLocal() as session:
        rec = await session.scalar(select(ActionRecord).where(ActionRecord.idem_key == prep.idem_key))
        if rec is None:
            rec = ActionRecord(
                action_key=prep.key, attempt=prep.attempt, idem_key=prep.idem_key, run_id=run_id,
                node_id=prep.node_id or node_id,
                exec_no=exec_no, capability=name, capability_version=getattr(capability, "version", None),
                endpoint_id=getattr(capability, "endpoint_id", None), environment=prep.environment,
                payload_hash=prep.payload_hash, preview=prep.preview, state="pending",
                confirm_task_id=await _confirm_task(session, run_id, confirm_node or node_id),
                app_id=prep.run.get("app_id"), workflow_id=prep.run.get("workflow_id"),
                workflow_version=prep.run.get("workflow_version"), graph_hash=prep.run.get("graph_hash"))
            session.add(rec)
            await session.commit()
        action_id = rec.id
    emit(EventType.ACTION_PREPARED, action_id=action_id, capability=name, payload_hash=prep.payload_hash,
         environment=prep.environment, preview=prep.preview, attempt=prep.attempt)

    if prep.environment == "simulated":
        from app.capabilities.platform import is_platform

        receipt = {"simulated": True, "environment": "simulated", "endpoint": prep.endpoint}
        await _set(action_id, ("pending",), state="simulated", receipt=receipt)
        emit(EventType.ACTION_SIMULATED, action_id=action_id, capability=name,
             message="演练：探索运行里不写平台的记录表和数据源，没有写入" if is_platform(name)
             else "演练：探索运行里端点没有测试环境，没有发送请求")
        return _output(action_id, prep, "simulated", None, receipt)

    # 本进程续不上租约了（cluster/fence.py）就不发：这次运行可能已经被别的进程接手
    from app.cluster import fence

    fence.check()
    # 先记 sent 再发：发到一半进程没了，重启后看到的是 sent，只能核实，不会当成没发过再发一次
    if not await _set(action_id, ("pending",), state="sent", request_at=utcnow()):
        raise ActionPending(action_id)
    emit(EventType.ACTION_SENT, action_id=action_id, capability=name, idem_key=prep.idem_key,
         environment=prep.environment, attempt=prep.attempt)
    emit(EventType.TOOL_START, tool=name, args=prep.preview, action_key=prep.key, action_id=action_id,
         environment=prep.environment)
    try:
        async with SessionLocal() as session:
            result = await endpoint_request(session, capability, prep.args, environment=prep.environment,
                                            idem_key=prep.idem_key, on_behalf=on_behalf, run_id=run_id,
                                            node_id=node_id)
    except Exception as e:  # noqa: BLE001 - 请求可能已经发出去了：一律按结果未知处理，不能当成失败再发
        logger.exception("动作 %s 发送时出错，按结果未知处理", action_id)
        await _set(action_id, ("sent",), state="unknown", error=f"发送时出错：{type(e).__name__}")
        raise ActionPending(action_id) from e

    status = result.status
    shown = masked(result.body, masked_paths(capability))
    if result.environment == "simulated":
        receipt = {"simulated": True, "environment": "simulated", "endpoint": prep.endpoint}
        await _set(action_id, ("sent",), state="simulated", receipt=receipt, environment="simulated")
        emit(EventType.ACTION_SIMULATED, action_id=action_id, capability=name,
             message="演练：端点没有测试环境，没有发送请求")
        return _output(action_id, prep, "simulated", None, receipt)
    if status is not None and 200 <= status < 300:
        artifact = await _store_receipt(run_id, node_id, status, result.body, prep)
        receipt = {"artifact": artifact, "status": status, "summary": _summary(shown)}
        await _set(action_id, ("sent",), state="confirmed", response_status=status, receipt=receipt)
        emit(EventType.ACTION_CONFIRMED, action_id=action_id, capability=name, status=status,
             receipt=receipt["summary"], artifact=artifact)
        emit(EventType.TOOL_END, tool=name, preview=receipt["summary"], artifact=artifact, action_id=action_id)
        return _output(action_id, prep, "confirmed", status, result.body)
    if status is None and not result.unknown:
        # 连接都没建立、端点配置不对：请求确定没有发出去。平台内置能力（capabilities/platform.py）是平台自己拒收的
        # （记录表的字段对不上、导入要人在数据页确认……），说「没有写入」
        from app.capabilities.platform import is_platform

        await _set(action_id, ("sent",), state="failed", error=result.error or "请求没有发出")
        emit(EventType.ACTION_FAILED, action_id=action_id, capability=name, error=result.error)
        emit(EventType.TOOL_ERROR, tool=name, error=result.error or "请求没有发出", action_id=action_id)
        if is_platform(name):
            raise ActionFailed(f"「{name}」没有写入：{result.error or '平台没有执行'}")
        raise ActionFailed(f"「{name}」没有发送成功：{result.error or '请求没有发出'}")
    if status is not None and 400 <= status < 500 and status != 429:
        await _set(action_id, ("sent",), state="failed", response_status=status,
                   error=f"HTTP {status}：{_summary(shown)[:300]}")
        emit(EventType.ACTION_FAILED, action_id=action_id, capability=name, status=status)
        emit(EventType.TOOL_ERROR, tool=name, error=f"外部系统拒绝了这次写入（HTTP {status}）", action_id=action_id)
        raise ActionFailed(f"「{name}」被外部系统拒绝（HTTP {status}），没有生效。请检查上游的数据后重新发起运行")
    # 超时、连接中断、5xx、429：对方可能已经处理了，只能核实
    await _set(action_id, ("sent",), state="unknown", response_status=status,
               error=result.error or (f"HTTP {status}" if status else "没有拿到确定的结果"))
    emit(EventType.TOOL_ERROR, tool=name, error="写入的结果未知，等待核实", action_id=action_id)
    raise ActionPending(action_id)


def _output(action_id: str, prep: Prepared, state: str, status: int | None, body: Any) -> dict[str, Any]:
    """节点的产出：receipt 是外部系统的回执（响应体），action 是这次动作的记录摘要。"""
    return {"status": status, "receipt": body, "simulated": state == "simulated",
            "action": {"id": action_id, "state": state, "environment": prep.environment,
                       "payload_hash": prep.payload_hash, "attempt": prep.attempt, "idem_key": prep.idem_key}}


def _receipt_of(rec: ActionRecord) -> dict[str, Any]:
    """已生效 / 演练过的动作交回存下的回执：落了工件的取工件里的响应体，人工核实的是填的外部单号。"""
    from app.core.artifact_store import load

    receipt = rec.receipt or {}
    body: Any = None
    if receipt.get("artifact"):
        body = (load(receipt["artifact"]) or {}).get("body")
    elif receipt.get("external_id"):
        body = {"external_id": receipt["external_id"]}
    prep = Prepared(key=rec.action_key, attempt=rec.attempt, payload_hash=rec.payload_hash, preview=rec.preview,
                    environment=rec.environment, endpoint=None)
    return _output(rec.id, prep, rec.state, rec.response_status, body)


async def _store_receipt(run_id: str, node_id: str, status: int | None, body: Any, prep: Prepared) -> str | None:
    from app.core.artifact_store import put_json

    try:
        return await put_json({"status": status, "body": body, "environment": prep.environment,
                               "idem_key": prep.idem_key, "payload_hash": prep.payload_hash},
                              kind="action_receipt", run_id=run_id, node_id=node_id)
    except Exception:  # noqa: BLE001 - 回执存不下不改变已生效这个事实，记录上留摘要
        logger.exception("动作的回执没能落成工件")
        return None


def _summary(body: Any) -> str:
    import json

    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
    return text[:SUMMARY_CHARS]


async def _set(action_id: str, expected: tuple[str, ...], **values: Any) -> bool:
    """条件 UPDATE：状态还是 expected 才改。返回改没改到。"""
    async with SessionLocal() as session:
        done = await session.execute(update(ActionRecord).where(ActionRecord.id == action_id,
                                                                ActionRecord.state.in_(expected)).values(**values))
        await session.commit()
        return done.rowcount == 1


async def _endpoint(session: AsyncSession, capability: Any) -> Endpoint | None:
    ref = getattr(capability, "endpoint_id", None)
    if not ref:
        return None
    return await session.get(Endpoint, ref) or await session.scalar(select(Endpoint).where(Endpoint.name == ref))


async def _confirm_task(session: AsyncSession, run_id: str, node_id: str) -> str | None:
    """这个节点最近一次动作确认的待办（记在生效记录上，查得到是谁确认的）。"""
    from app.db.models import Task

    return await session.scalar(select(Task.id).where(Task.run_id == run_id, Task.node_id == node_id,
                                                      Task.kind == "action_confirm")
                                .order_by(Task.created_at.desc(), Task.id.desc()).limit(1))


# --------------------------------------------------------------------------
# 结果未知之后：查询、核实
# --------------------------------------------------------------------------


async def unresolved_action(session: AsyncSession, run_id: str) -> bool:
    """这次运行有没有已发出、还没核实的动作。有的话不能手动接着跑（核实之后系统自己接着跑）。"""
    return bool(await session.scalar(select(exists().where(ActionRecord.run_id == run_id,
                                                           ActionRecord.state.in_(("sent", "unknown"))))))


async def on_unknown(action_id: str) -> None:
    """运行因为结果未知挂起之后（runner 调）、重启时发现 sent 的记录（startup 调）：记 action.unknown，安排核实。

    幂等：已经排上查询、或已经有核实待办的不再安排，也不再记事件——节点重新执行时撞上同一条未知记录会再抛一次
    ActionPending，那不是一次新的「结果未知」。"""
    from app.capabilities.registry import get_capability
    from app.db.models import Task

    now = utcnow()
    async with SessionLocal() as session:
        rec = await session.get(ActionRecord, action_id)
        if rec is None or rec.state not in ("sent", "unknown"):
            return
        if rec.state == "sent":
            rec.state = "unknown"
            rec.error = rec.error or "服务重启时请求已发出、还没有拿到结果"
        cap = await get_capability(session, rec.capability)
        has_lookup = bool(getattr(cap, "lookup", None))
        reconciling = await session.scalar(select(Task.id).where(
            Task.correlation_key == f"action:{rec.id}", Task.kind == "reconcile", Task.status == "pending"))
        arranged = reconciling is not None or (has_lookup and (rec.next_lookup_at is not None
                                                                or rec.lookup_attempts > 0))
        task_id: str | None = None
        if not arranged:
            if has_lookup and rec.lookup_attempts < LOOKUP_MAX:
                rec.next_lookup_at = now + timedelta(minutes=1)
            else:
                task_id = await _open_reconcile(session, rec)
        await session.commit()
        run_id, node_id, capability, environment = rec.run_id, rec.node_id, rec.capability, rec.environment
    if arranged:
        return
    from app.process.events import record

    await record(run_id, EventType.ACTION_UNKNOWN, node_id=node_id, action_id=action_id, capability=capability,
                 environment=environment, lookup=has_lookup, task_id=task_id,
                 message="写入外部系统的结果未知，" + ("系统会按幂等键去查" if has_lookup else "等待人工核实"))
    await _after_reconcile_opened(task_id, run_id, action_id)


async def _open_reconcile(session: AsyncSession, rec: ActionRecord) -> str:
    from app.process.tasks import open_reconcile

    task = await open_reconcile(session, rec, title=f"核实写入结果：{rec.capability}")
    return task.id


async def _after_reconcile_opened(task_id: str | None, run_id: str, action_id: str) -> None:
    from app.notify.service import notify_action, spawn

    if task_id:
        from app.process.tasks import announce

        await announce(task_id)
    spawn(notify_action(action_id, "action.unknown"))


async def reconcile(action_id: str, principal: Any, *, outcome: str, external_id: str | None = None,
                    note: str | None = None) -> dict[str, Any]:
    """核实：outcome ∈ confirmed | not_applied_resend | not_applied_abandon。核实后系统自动接着跑。

    重新发送不在这里发：记下「核实为未生效、要重发」，接着跑时节点按 attempt+1 的新幂等键（{action_key}:2）发。"""
    from app.process.tasks import TaskRefused, close_reconcile

    if outcome not in ("confirmed", "not_applied_resend", "not_applied_abandon"):
        raise TaskRefused("核实只能选已生效、未生效重新发送、未生效放弃之一", status_code=422)
    now = utcnow()
    username = getattr(principal, "username", None)
    resolution = {"by": username, "verified": bool(getattr(principal, "verified", False)), "how": "manual",
                  "outcome": {"confirmed": "confirmed", "not_applied_resend": "resend",
                              "not_applied_abandon": "abandon"}[outcome],
                  "external_id": external_id, "note": note or "", "at": now.isoformat()}
    values: dict[str, Any] = {"resolution": resolution}
    if outcome == "confirmed":
        values.update(state="confirmed", receipt={"external_id": external_id, "manual": True})
    else:
        values.update(state="failed", error="核实为未生效")
    async with SessionLocal() as session:
        done = await session.execute(update(ActionRecord).where(
            ActionRecord.id == action_id, ActionRecord.state.in_(("sent", "unknown"))).values(**values))
        if done.rowcount != 1:
            await session.rollback()
            raise TaskRefused("这个动作已经核实过，或者不在等待核实，请刷新页面查看最新状态")
        await close_reconcile(session, action_id, principal, outcome, note)
        rec = await session.get(ActionRecord, action_id)
        await session.commit()
        run_id, node_id = rec.run_id, rec.node_id
    from app.process.events import record

    await record(run_id, EventType.ACTION_RECONCILED, node_id=node_id, action_id=action_id, how="manual",
                 outcome=resolution["outcome"], actor=username, verified=resolution["verified"],
                 external_id=external_id)
    return {"action": await describe(action_id), "run": await _continue(run_id, username)}


async def _continue(run_id: str, actor: str | None) -> Any:
    """核实之后接着跑。运行已经结束（被放弃了）、或者还在等别的审批时不动它，记日志。"""
    from app.engine.runner import run_manager

    try:
        return await run_manager.continue_failed(run_id, actor=actor)
    except (ValueError, KeyError) as e:
        logger.warning("动作核实之后没能接着运行 %s：%s", run_id, e)
        return None
    except Exception:  # noqa: BLE001 - 核实本身已经落库：接着跑不成不该让核实报错，记日志，人可以在记录页接着运行
        logger.exception("动作核实之后接着运行 %s 出错", run_id)
        return None


async def tick(now: datetime | None = None) -> list[str]:
    """调度循环每一拍：对登记了 lookup 的结果未知动作带退避地查询。交回这一拍查过的动作记录 id（同 tasks.tick）。"""
    now = now or utcnow()
    async with SessionLocal() as session:
        due = list((await session.execute(select(ActionRecord.id, ActionRecord.next_lookup_at).where(
            ActionRecord.state == "unknown", ActionRecord.next_lookup_at.is_not(None),
            ActionRecord.next_lookup_at <= now))).all())
    done: list[str] = []
    for action_id, claimed_at in due:
        try:
            if await _lookup(action_id, claimed_at, now):
                done.append(action_id)
        except Exception:  # noqa: BLE001 - 一条出错不拖住这一拍的其他动作
            logger.exception("动作 %s 的结果查询失败，下一拍再试", action_id)
    return done


async def _lookup(action_id: str, claimed_at: datetime, now: datetime) -> int:
    from app.capabilities.registry import endpoint_request, get_capability, masked_paths

    async with SessionLocal() as session:
        # 认领：两拍撞在一起只有一拍查
        claimed = await session.execute(update(ActionRecord).where(
            ActionRecord.id == action_id, ActionRecord.state == "unknown", ActionRecord.next_lookup_at == claimed_at)
            .values(next_lookup_at=None, lookup_attempts=ActionRecord.lookup_attempts + 1))
        await session.commit()
        if claimed.rowcount != 1:
            return 0
        rec = await session.get(ActionRecord, action_id)
        cap = await get_capability(session, rec.capability)
        finder = await get_capability(session, cap.lookup) if cap is not None and cap.lookup else None
        if finder is None:
            task_id = await _open_reconcile(session, rec)
            await session.commit()
            await _after_reconcile_opened(task_id, rec.run_id, action_id)
            return 1
        try:
            result = await endpoint_request(session, finder, {"idempotency_key": rec.idem_key},
                                            environment=rec.environment, run_id=rec.run_id, node_id=rec.node_id)
        except Exception:  # noqa: BLE001 - 查询本身出错：按没查到，下次再查
            logger.exception("动作 %s 的结果查询出错", action_id)
            result = None
        attempts, run_id, node_id = rec.lookup_attempts, rec.run_id, rec.node_id

    if result is not None and result.status is not None and 200 <= result.status < 300:
        prep = Prepared(key=rec.action_key, attempt=rec.attempt, payload_hash=rec.payload_hash,
                        preview=rec.preview, environment=rec.environment, endpoint=None)
        artifact = await _store_receipt(run_id, node_id, result.status, result.body, prep)
        resolution = {"by": None, "how": "lookup", "outcome": "confirmed", "at": now.isoformat(),
                      "attempts": attempts}
        receipt = {"artifact": artifact, "status": result.status,
                   "summary": _summary(masked(result.body, masked_paths(cap)))}
        if not await _set(action_id, ("unknown",), state="confirmed", receipt=receipt, resolution=resolution,
                          response_status=result.status):
            return 1
        async with SessionLocal() as session:
            from app.process.tasks import close_reconcile

            await close_reconcile(session, action_id, None, "confirmed", "按幂等键查到外部系统已生效")
            await session.commit()
        from app.process.events import record

        await record(run_id, EventType.ACTION_RECONCILED, node_id=node_id, action_id=action_id, how="lookup",
                     outcome="confirmed", attempts=attempts, artifact=artifact)
        await _continue(run_id, None)
        return 1

    if attempts >= LOOKUP_MAX:
        async with SessionLocal() as session:
            rec = await session.get(ActionRecord, action_id)
            task_id = await _open_reconcile(session, rec)
            await session.commit()
        await _after_reconcile_opened(task_id, run_id, action_id)
        return 1
    wait = min(2 ** attempts, LOOKUP_CAP_MIN)
    await _set(action_id, ("unknown",), next_lookup_at=now + timedelta(minutes=wait))
    return 1


async def startup(*, live: Any = None) -> int:
    """启动时：上次进程没了的时候已经发出（sent）、还没拿到结果的动作收成结果未知，安排核实；结果未知却还没安排核实的
    （挂起之后、安排之前进程没了）补上。返回处理的条数。

    多进程：所属的运行还在别的活着的进程里执行的不碰（那边正在发、正在等结果）。"""
    from app.cluster import state
    from app.db.models import Run
    from app.engine.runner import run_manager

    live = live if live is not None else await state.current(fresh=True)
    async with SessionLocal() as session:
        rows = (await session.execute(select(ActionRecord.id, ActionRecord.run_id, Run.status, Run.owner)
                                      .outerjoin(Run, Run.id == ActionRecord.run_id).where(
            ActionRecord.state.in_(("sent", "unknown")), ActionRecord.resolution.is_(None)))).all()
    ids = [action_id for action_id, run_id, status, owner in rows
           if not (status in ("running", "queued") and live.alive(owner, local=run_manager.is_active(run_id)))]
    for action_id in ids:
        try:
            await on_unknown(action_id)
        except Exception:  # noqa: BLE001
            logger.exception("动作 %s 的启动整理失败", action_id)
    if ids:
        logger.info("启动时整理了 %d 条结果未知的动作", len(ids))
    return len(ids)


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def row_out(rec: ActionRecord) -> dict[str, Any]:
    """生效记录的接口形状（frontend/src/typesApp.ts 的 ActionRecordRow）。预览、回执摘要都是遮罩后的。"""
    def iso(moment: datetime | None) -> str | None:
        from app.process.tasks import _iso

        return _iso(moment)

    return {"id": rec.id, "run_id": rec.run_id, "node_id": rec.node_id, "app_id": rec.app_id,
            "capability": rec.capability, "environment": rec.environment, "state": rec.state, "attempt": rec.attempt,
            "payload_hash": rec.payload_hash, "preview": rec.preview, "receipt": rec.receipt, "error": rec.error,
            "resolution": rec.resolution, "idem_key": rec.idem_key, "endpoint_id": rec.endpoint_id,
            "workflow_id": rec.workflow_id, "workflow_version": rec.workflow_version,
            "capability_version": rec.capability_version, "request_at": iso(rec.request_at),
            "response_status": rec.response_status, "lookup_attempts": rec.lookup_attempts,
            "confirm_task_id": rec.confirm_task_id,
            "created_at": iso(rec.created_at), "updated_at": iso(rec.updated_at)}


async def describe(action_id: str) -> dict[str, Any] | None:
    async with SessionLocal() as session:
        rec = await session.get(ActionRecord, action_id)
        return row_out(rec) if rec is not None else None


def environment_label(env: str) -> str:
    return _ENV_LABEL.get(env, env)
