from __future__ import annotations

import json
import math
import re
from typing import Any

from app.core import artifact_store
from app.core.errors import describe_exception
from app.core.events import EventType
from app.engine.context import NodeContext, NodeError
from app.engine.evidence import (
    DEFAULT_FORMAT,
    FORMATS,
    MISSING,
    RenderError,
    cell_eid,
    is_number,
    next_exec,
    render_number,
)
from app.engine.expressions import (
    CellError,
    ExpressionError,
    cell_parts,
    cell_value,
    eval_expression,
    leaf_refs,
    locate_cell,
    parse_expression,
    same_value,
    substitute,
)
from app.engine.state import GraphState, template_context


async def run_metrics(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """口径卡：把数值计算收进固定层，产出受控的指标集（MetricSet）。

    表达式走 AST 白名单求值器——确定性、无模型参与、可复算。
    下游叙述节点只许引用这里登记过的数，出具校验器按这份清单抽数字回指。
    这就是"叙述层无算术权限"的机制载体：不是求模型别算，而是它算了也过不了校验。

    每个指标还记下它是怎么来的：用到了哪些输入、各取自哪个节点、代入后的算式、
    拿代入式复算是否一致。整张卡落成 metric_set 工件，id 进证据台账——报告里的
    数字点开，一路能追到这里。
    """
    definitions = ctx.cfg("metrics", []) or []
    caliber_raw, caliber_version = ctx.cfg("caliber", "") or ctx.node.title, str(ctx.cfg("caliber_version", "") or "v1")
    source: dict[str, Any] | None = None
    if ctx.config.get("caliber_from") not in (None, "", {}):
        # 钉住别的工作流某一版里的口径卡：名字、版本、指标定义都按那一版，本地写的不算
        pinned, source = await _pinned_caliber(ctx)
        definitions = pinned["metrics"]
        caliber_raw, caliber_version = pinned["caliber"], pinned["caliber_version"]
    if not definitions:
        raise NodeError(ctx.node.id, "口径卡没有定义任何指标")

    tctx = template_context(state)
    caliber = ctx.render_str(caliber_raw, state)
    # 缺输入时怎么办。默认 fail：和以前一样整张卡失败；null 则这个指标记为空值、
    # 交给出具契约的 required / expected 去判档
    on_missing = str(ctx.cfg("on_missing", "fail") or "fail")
    if on_missing not in ("fail", "null"):
        raise NodeError(ctx.node.id, f"on_missing 只能是 fail 或 null，写的是 {on_missing!r}")

    metrics: list[dict[str, Any]] = []
    errors: list[str] = []
    for definition in definitions:
        metric_id = str(definition.get("id") or "").strip()
        expr = str(definition.get("expression") or "").strip()
        if not metric_id or not expr:
            errors.append(f"指标定义不完整：{definition}")
            continue
        display, problem = _display_of(definition)
        if problem:
            errors.append(f"{metric_id}: {problem}")
            continue
        try:
            tree, _, _ = parse_expression(expr)
        except ExpressionError as e:
            errors.append(f"{metric_id}: 表达式错误（{e}）")
            continue
        paths = leaf_refs(tree)
        values = {path: _value_of(path, tctx) for path in paths}
        missing = [path for path in paths if values[path] is None]

        value: Any = None
        try:
            value = eval_expression(expr, tctx)
        except ExpressionError as e:
            errors.append(f"{metric_id}: 表达式错误（{e}）")
            continue
        except ZeroDivisionError:
            errors.append(f"{metric_id}: 除以零")
            continue
        except Exception as e:  # noqa: BLE001 - 求值器之外的运算错误（None 参与算术、类型不对）要说人话
            if missing or "NoneType" in str(e):
                if on_missing != "null":
                    errors.append(f"{metric_id}: " + _missing_reason(missing))
                    continue
                value = None
            elif isinstance(e, TypeError):
                errors.append(f"{metric_id}: 输入的类型不对，算不出来——" + _type_hint(paths, values))
                continue
            else:
                errors.append(f"{metric_id}: 算不出来（{describe_exception(e)}）")
                continue

        if display["bad_decimals"] and isinstance(value, float):
            # 旧写法里 int() 认不得的 decimals：以前走到 round(value, int(decimals)) 就崩，
            # 现在照样算失败，只是说人话。值不是小数时以前根本用不到它，照旧忽略
            errors.append(f"{metric_id}: decimals 要写整数，写的是 {definition.get('decimals')!r}")
            continue
        decimals = display["decimals"]
        if isinstance(value, float) and decimals is not None:
            value = round(value, decimals)
        substituted = substitute(tree, values)
        metrics.append(
            {
                "id": metric_id,
                "name": definition.get("name") or metric_id,
                "unit": definition.get("unit") or "",
                "value": value,
                "expression": expr,
                "decimals": decimals,
                "format": display["format"],
                "rendered": _rendered(value, definition, display),
                "inputs": [_input_of(path, values[path], state, ctx, tctx) for path in paths],
                "substituted": substituted,
                "recompute_ok": _recompute(substituted, tctx, value, decimals),
                # 值是空的就是缺输入，不论 on_missing 是哪一种：fail 模式下直接取到空值的
                # 指标（原式就是 vars.x.y 而 y 不存在）以前也是照常产出一个 None
                "status": "ok" if value is not None else "missing_input",
            }
        )

    if errors:
        # 口径卡是固定层：任何一个指标算不出来都是模板缺陷，不能带病出具
        raise NodeError(ctx.node.id, "口径卡计算失败：" + "；".join(errors))

    # text 是给叙述节点 prompt 用的清单——它是叙述层唯一的数字来源
    lines = [
        f"- {m['id']}（{m['name']}）= {m['value']}{m['unit']}" if m["value"] is not None
        else f"- {m['id']}（{m['name']}）= {MISSING}（缺输入，没有值）"
        for m in metrics
    ]
    card = {
        "kind": "metric_set",
        "caliber": caliber,
        "caliber_version": caliber_version,
        "metrics": metrics,
    }
    if source:
        # 指标定义来自哪个工作流的哪一版：证据面板的指标步骤据此写出处，升版处置按它比版本
        card["source"] = source
    artifact = await _store(card, caliber, caliber_version, ctx)
    result = {
        **card,
        "text": f"指标清单（口径 {caliber} @ {caliber_version}）：\n" + "\n".join(lines),
    }
    ctx.emit(
        EventType.LOG,
        level="info",
        message=f"口径卡 {caliber}@{caliber_version}：产出 {len(metrics)} 个受控指标",
    )

    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}}
    if artifact:
        result["artifact"] = artifact
        updates["evidence"] = [{
            "kind": "metric_set",
            "node_id": ctx.node.id,
            "exec": next_exec(state, ctx.node.id),
            "artifact": artifact,
            "caliber": caliber,
            "version": caliber_version,
            "metrics": [m["id"] for m in metrics],
            **({"source": source} if source else {}),
        }]
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: result}
    return updates


async def _pinned_caliber(ctx: NodeContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """caliber_from 指向的那张口径卡：({caliber, caliber_version, metrics}, 来源)。

    从那个工作流的 WorkflowVersion 快照里取——版本快照不可变，同一份引用每次取到的定义都一样，
    审批恢复后节点重放也还是这一份。取不到就让节点失败并说清是哪一环断了：悄悄退回本地
    定义，等于换了口径还照常出具。
    """
    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.db.models import Workflow, WorkflowVersion
    from app.engine.schema import type_label

    ref = ctx.config.get("caliber_from")
    wf_id = str(ref.get("workflow_id") or "").strip() if isinstance(ref, dict) else ""
    node_id = str(ref.get("node_id") or "").strip() if isinstance(ref, dict) else ""
    version = _integer(ref.get("workflow_version")) if isinstance(ref, dict) else None
    if not wf_id or not node_id or version is None or version < 1:
        raise NodeError(ctx.node.id, "caliber_from 要写 {workflow_id, workflow_version, node_id}：钉住哪个工作流"
                                     f"的哪一版里的哪张口径卡，写的是 {ref!r}")
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, wf_id)
        if workflow is None:
            raise NodeError(ctx.node.id, f"caliber_from 钉住的工作流 {wf_id} 不存在（可能已经被删了）："
                                         "在节点里重新选一张口径卡")
        snapshot = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf_id, WorkflowVersion.version == version))).scalar_one_or_none()
        name = workflow.name
    if snapshot is None:
        raise NodeError(ctx.node.id, f"caliber_from 钉住的「{name}」没有 v{version} 这个版本：在节点里重新选版本")
    found = next((n for n in (snapshot.graph or {}).get("nodes") or []
                  if isinstance(n, dict) and n.get("id") == node_id), None)
    if found is None:
        raise NodeError(ctx.node.id, f"「{name}」v{version} 里没有节点 {node_id}：在节点里重新选一张口径卡")
    data = found.get("data") if isinstance(found.get("data"), dict) else {}
    title = data.get("label") or node_id
    if found.get("type") != "metrics":
        raise NodeError(ctx.node.id, f"caliber_from 指向的「{name}」v{version} 里的「{title}」不是口径卡，"
                                     f"是「{type_label(found.get('type'))}」")
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    origin = cfg.get("caliber_from")
    if origin not in (None, "", {}):
        root = origin.get("workflow_id") if isinstance(origin, dict) else None
        async with SessionLocal() as session:
            upstream = await session.get(Workflow, str(root)) if root else None
        where = (f"「{upstream.name}」v{origin.get('workflow_version')} 的 {origin.get('node_id')}"
                 if upstream is not None else f"{origin!r}")
        raise NodeError(ctx.node.id, f"「{name}」v{version} 里的「{title}」自己也是钉住别处的口径卡（源头是 {where}）："
                                     "直接钉住源头那张，升版处置才比得对版本")
    source = {"workflow_id": wf_id, "workflow_version": version, "node_id": node_id}
    return {"caliber": cfg.get("caliber") or title, "caliber_version": str(cfg.get("caliber_version") or "v1"),
            "metrics": cfg.get("metrics") or []}, source


#: 新写法的小数位范围。负数是取整到十位、百位（round(x, -2)）
_DECIMALS_RANGE = 15


def _display_of(definition: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """指标的显示格式：{decimals, format, bad_decimals}，以及写错时的一句话。

    写了 format 的是新写法，decimals 严格校验：-15 到 15 的整数。
    没写 format 的是这一期之前的口径卡，旧代码能接受的照旧接受：以前只在值是小数时
    round(value, int(decimals))，所以 int() 认得的都算数（"2"、-2、12）；int() 认不得的
    记成 bad_decimals，由调用方按值的类型决定——值是小数才报错（以前在这里崩），
    不是小数就忽略（以前根本用不到它）。
    """
    raw = definition.get("decimals")
    strict = definition.get("format") not in (None, "")
    fmt = definition.get("format") or DEFAULT_FORMAT
    if fmt not in FORMATS:
        return {}, f"format 只能是 {' / '.join(FORMATS)}，写的是 {fmt!r}"
    display: dict[str, Any] = {"decimals": None, "format": fmt, "bad_decimals": False}
    if raw in (None, ""):
        return display, None
    if strict:
        decimals = _integer(raw)
        if decimals is None or not -_DECIMALS_RANGE <= decimals <= _DECIMALS_RANGE:
            return {}, (f"decimals 要写 -{_DECIMALS_RANGE} 到 {_DECIMALS_RANGE} 的整数"
                        f"（负数表示取整到十位、百位），写的是 {raw!r}")
        display["decimals"] = decimals
        return display, None
    try:
        display["decimals"] = int(raw)
    except (TypeError, ValueError, OverflowError):
        display["bad_decimals"] = True
    return display, None


def _integer(raw: Any) -> int | None:
    """严格的整数：2、2.0、"2"、"-2" 算；1.5、"两位"、true 不算。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str) and re.fullmatch(r"\s*[+-]?\d+\s*", raw):
        return int(raw)
    return None


def _rendered(value: Any, definition: dict[str, Any], display: dict[str, Any]) -> str:
    try:
        return render_number(value, unit=str(definition.get("unit") or ""),
                             decimals=display["decimals"], fmt=display["format"])
    except RenderError:
        # 按规则渲染不出来的值显示「—」：inf / nan、不是 0 却会显示成 0 的（小数位不够）、
        # 位数大到不像话的。值本身照旧记着，报告里引用它会被判成解析不了，原因写清楚
        return MISSING


def _value_of(path: str, tctx: dict[str, Any]) -> Any:
    """单独取一个引用的值。取不到（或取值本身出错）一律当作没有值。"""
    try:
        return eval_expression(path, tctx)
    except Exception:  # noqa: BLE001
        return None


def _missing_reason(missing: list[str]) -> str:
    if not missing:
        return "有输入是空值，算不出来（检查上游有没有产出这些字段）"
    return f"输入 {'、'.join(missing[:4])} 没有值，算不出来（上游没有产出这个字段，或者路径写错了）"


def _type_hint(paths: list[str], values: dict[str, Any]) -> str:
    kinds = {bool: "布尔值", str: "文本", list: "列表", dict: "对象"}
    parts = []
    for path in paths:
        value = values[path]
        if value is None or is_number(value):
            continue
        label = next((name for t, name in kinds.items() if isinstance(value, t)), type(value).__name__)
        sample = f"「{str(value)[:20]}」" if isinstance(value, str) else ""
        parts.append(f"{path} 是{label}{sample}")
    if not parts:
        return "参与运算的值不是数"
    return "、".join(parts[:4]) + "，不能参与数值运算"


_REF_HEAD = re.compile(r"^(vars|nodes|input)\.([A-Za-z_一-鿿][\w\-一-鿿]*)(.*)$", re.S)
_VIA = {"input": "input", "code": "code", "agent": "agent", "llm": "llm", "tool": "tool",
        "transform": "transform", "metrics": "metric", "retrieve": "retrieval", "loop": "loop",
        "human": "human", "validate": "validate", "supervisor": "team", "subgraph": "subgraph",
        "memory": "memory"}


def _input_of(path: str, value: Any, state: GraphState, ctx: NodeContext,
              tctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """一个输入的来历：{path, value, node_id, via, field?, role?, status}。

    vars.X 要反查是谁写的：assign_to 为 X 的节点里，按执行顺序最后跑的那一个——
    和状态里 vars 的覆盖顺序一致。入口节点把输入字段也写进 vars。

    两种来历能追到查询快照里的那一格：
    - cell(nodes.fetch, 0, 'gmv')：via tool_cell，带 locator、artifact、eid，按快照核对
    - agent 开了 cite_fields 的字段：via agent_field，接上它的 data_evidence（eid、核对状态）
    """
    if (parts := cell_parts(path)) is not None:
        return _cell_input(path, value, parts, state, ctx, tctx or template_context(state))
    spec = ctx.run.spec
    nodes = spec.node_map()
    node_id: str | None = None
    field = ""
    head = _REF_HEAD.match(path)
    if head:
        root, name, rest = head.groups()
        field = rest.lstrip(".")
        if root == "nodes":
            node_id = name if name in nodes else None
        elif root == "input":
            node_id = next((n.id for n in spec.entry_nodes() if str(n.type) == "input"), None)
        else:
            node_id = _writer_of(name, state, ctx)
    producer = nodes.get(node_id) if node_id else None
    entry: dict[str, Any] = {
        "path": path,
        "value": value if value is None or isinstance(value, (int, float, str, bool)) else _brief(value),
        "node_id": node_id,
        "via": _VIA.get(str(producer.type), str(producer.type)) if producer else "state",
    }
    if field:
        entry["field"] = field
    if producer is not None and str(producer.type) == "code":
        # 沙箱算出来的数喂给口径卡：记下它自认是「取数」还是「计算」，治理规则按它判
        entry["role"] = str(producer.config.get("evidence_role") or "compute")
    entry["status"] = "ok" if value is not None else "missing"
    if producer is not None and str(producer.type) == "agent" and field:
        cited = _agent_field(state, producer.id, root if head else "", field)
        if cited is not None:
            entry.update(cited)
    return entry


def _field_key(field: str) -> str:
    """vars.kpi['gmv'] 和 vars.kpi.gmv 是同一个字段：下标写法换成点号。"""
    return re.sub(r"\[\s*['\"]([^'\"\]]+)['\"]\s*\]", r".\1", field).strip(".")


def _agent_field(state: GraphState, node_id: str, root: str, field: str) -> dict[str, Any] | None:
    """agent 字段的出处：{via: agent_field, field, ref, artifact, locator, eid, status, model_value?, reason?}。

    assign_to 写进 vars 的是 data 本身，nodes.<agent> 下要多走一层 data。没开 cite_fields
    （或者是老运行）的 agent 没有 data_evidence，返回 None，来历照旧记成 agent。
    """
    output = (state.get("nodes") or {}).get(node_id)
    evidence = output.get("data_evidence") if isinstance(output, dict) else None
    if not isinstance(evidence, dict):
        return None
    key = _field_key(field)
    if root == "nodes":
        if not key.startswith("data."):
            return None
        key = key[len("data."):]
    cited = evidence.get(key)
    if not isinstance(cited, dict):
        return _agent_element(evidence, key)
    out: dict[str, Any] = {"via": "agent_field", "field": key, "status": cited.get("status") or "unresolved"}
    for k in ("ref", "artifact", "locator", "eid", "model_value", "reason"):
        if k in cited:
            out[k] = cited[k]
    return out


_ELEMENT = re.compile(r"^(?P<base>[^\[\]]+)\[(?P<index>\d+)\](?:\.(?P<sub>[^\[\]]+))?$")


def _agent_element(evidence: dict[str, Any], key: str) -> dict[str, Any] | None:
    """数组字段里的一个元素（vars.kpi.weeks[1].amount）：按行映射落到快照里的一格。

    数组的出处是一段行 rows=[a, b] 加列；第 k 个元素就是第 a+k 行。整组的核对状态说的是整组：
    别的元素对不上，不等于这一个也对不上。所以这一个元素按它自己那一格再核一遍——模型报的是
    model_value 里的第 k 个（整组 verified 时模型报的就是快照里的值），和快照里那一格比。
    """
    m = _ELEMENT.match(key)
    cited = evidence.get(m.group("base")) if m else None
    locator = cited.get("locator") if isinstance(cited, dict) else None
    if not (isinstance(locator, dict) and isinstance(locator.get("rows"), list) and len(locator["rows"]) == 2):
        return None
    first, last = locator["rows"]
    index, sub = int(m.group("index")), m.group("sub")
    row = first + index
    column = (locator.get("columns") or {}).get(sub) if sub else locator.get("column")
    if row > last or not isinstance(column, str):
        return None
    call, artifact = str(cited.get("call") or ""), cited.get("artifact")
    out: dict[str, Any] = {"via": "agent_field", "field": key, "ref": f"{call}.r{row}.{column}", "artifact": artifact,
                           "locator": {"row": row, "column": column}, "eid": cell_eid(artifact, row, column)}
    try:
        truth, _ = locate_cell(artifact_store.load(artifact), row, column)
    except (ValueError, CellError, OSError) as e:
        out.update(status="unresolved", reason=f"查询快照取不回来或对不上：{e}")
        return out
    if cited.get("status") == "verified":
        out["status"] = "verified"
        return out
    told = cited.get("model_value")
    item = told[index] if isinstance(told, list) and index < len(told) else None
    reported = isinstance(told, list) and index < len(told) and (not sub or (isinstance(item, dict) and sub in item))
    element = (item[sub] if sub else item) if reported else None
    if reported and same_value(element, truth):
        out["status"] = "verified"
    else:
        # 模型没报这个元素（给的数组短了）就没有 model_value 可留
        out.update(status="mismatch", **({"model_value": element} if reported else {}))
    return out


def _cell_input(path: str, value: Any, parts: tuple[str, str, str], state: GraphState, ctx: NodeContext,
                tctx: dict[str, Any]) -> dict[str, Any]:
    """cell() 取的那一格：它来自哪次查询（台账里的 query 条目）、第几行哪一列、快照里是不是这个值。

    查询结果经 transform 解析过也认得出来：数据源工具交回的 JSON 里带着快照的工件 id。
    取不到快照、或者快照里那一格和算的时候用的值不一样，status 如实写，不替它圆。
    """
    source, row_src, column_src = parts
    entry: dict[str, Any] = {"path": path, "value": value if value is None or isinstance(value, (int, float, str, bool))
                             else _brief(value), "node_id": None, "via": "tool_cell"}
    table = _value_of(source, tctx)
    row, column = _value_of(row_src, tctx), _value_of(column_src, tctx)
    data = table
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    artifact = data.get("artifact") if isinstance(data, dict) else None
    queries = [e for e in state.get("evidence") or [] if isinstance(e, dict) and e.get("kind") == "query"]
    ledger = next((e for e in reversed(queries) if artifact and e.get("artifact") == artifact), None)
    head = _REF_HEAD.match(source)
    if ledger is None and head and head.group(1) == "nodes":
        # 台账是按工件认的；工具交回的不是带工件 id 的 JSON 时，按取值链上的节点找它最后一次查询
        ledger = next((e for e in reversed(queries) if e.get("node_id") == head.group(2)), None)
    if ledger is None:
        entry.update(node_id=head.group(2) if head and head.group(1) == "nodes" else None,
                     status="unresolved", reason="找不到这份数据对应的查询快照：cell() 要取查询工具交回的结果")
        return entry
    entry["node_id"] = ledger.get("node_id")
    artifact = ledger.get("artifact")
    try:
        raw, name = locate_cell(data if isinstance(data, dict) else table, row, column)
    except CellError as e:
        entry.update(status="unresolved", reason=f"cell() {e}", artifact=artifact)
        return entry
    locator = {"row": row, "column": name}
    entry.update(locator=locator, artifact=artifact, eid=cell_eid(artifact, row, name))
    try:
        snapshot = artifact_store.load(artifact)
        truth, _ = locate_cell(snapshot, row, name)
    except (ValueError, CellError, OSError) as e:
        entry.update(status="unresolved", reason=f"查询快照取不回来或对不上：{e}")
        return entry
    if cell_value(truth) != cell_value(raw):
        # 算的时候用的值和快照不一样（上游改写过这份结果）：照实记下快照里的值
        entry.update(status="mismatch", snapshot_value=cell_value(truth))
        return entry
    entry["status"] = "verified"
    return entry


def _writer_of(var: str, state: GraphState, ctx: NodeContext) -> str | None:
    nodes = ctx.run.spec.node_map()

    def writes(node_id: str) -> bool:
        node = nodes.get(node_id)
        if node is None:
            return False
        cfg = node.config
        if str(cfg.get("assign_to") or "").strip() == var:
            return True
        if str(node.type) == "input":
            return var in (state.get("input") or {})
        if str(node.type) == "loop":
            item = str(cfg.get("item_var") or "item").strip()
            return var in (item, f"{item}_index")
        return False

    for step in reversed(state.get("trail") or []):
        node_id = step.get("node_id")
        if node_id and "error" not in step and writes(node_id):
            return node_id
    # 没有执行记录可查（比如直接调用执行器）：按图里声明的写入者兜底
    return next((n.id for n in ctx.run.spec.nodes if writes(n.id)), None)


def _brief(value: Any) -> str:
    """容器型的输入不整份抄进指标：记个大概，完整的在上游节点的产出工件里。"""
    if isinstance(value, list):
        return f"[{len(value)} 项]"
    if isinstance(value, dict):
        return f"{{{len(value)} 个字段}}"
    return str(value)[:200]


def _recompute(substituted: str, tctx: dict[str, Any], value: Any, decimals: int | None) -> bool | None:
    """拿代入式再算一遍，和结果比对。值是空的就没有可比的，返回 None。"""
    if value is None:
        return None
    try:
        again = eval_expression(substituted, tctx)
    except Exception:  # noqa: BLE001 - 代入式算不出来本身就是「对不上」
        return False
    if isinstance(again, float) and decimals is not None:
        again = round(again, decimals)
    if is_number(again) and is_number(value):
        return math.isclose(again, value, rel_tol=1e-12, abs_tol=0.0) or again == value
    return again == value


async def _store(card: dict[str, Any], caliber: str, version: str, ctx: NodeContext) -> str | None:
    """整张卡落成 metric_set 工件。写失败不毁掉运行，只是这张卡没法被引用。"""
    from app.core.artifact_store import put_json

    try:
        return await put_json(card, kind="metric_set", run_id=ctx.run.run_id, node_id=ctx.node.id,
                              meta={"caliber": caliber, "version": version})
    except Exception as e:  # noqa: BLE001
        ctx.emit(EventType.LOG, level="warn", code="evidence_store_failed",
                 message=f"口径卡的指标集没能落进工件库（{describe_exception(e)}），报告里没法引用它的指标")
        return None
