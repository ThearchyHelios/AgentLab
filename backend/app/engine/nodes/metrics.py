from __future__ import annotations

import ast
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
    TruncatedUse,
    cell_parts,
    cell_value,
    eval_expression,
    leaf_refs,
    locate_cell,
    parse_expression,
    same_value,
    substitute,
)
from app.engine.labels import field_label
from app.engine.lineage import parsed_tool, row_cell
from app.engine.sql_problems import CHECK_FIELDS, QueryProblem, merge_problems, node_of, query_problems, reason_text
from app.engine.state import GraphState, template_context


async def run_metrics(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """口径卡：把数值计算收进固定层，产出受控的指标集（MetricSet）。

    表达式走 AST 白名单求值器——确定性、无模型参与、可复算。
    下游叙述节点只许引用这里登记过的数，出具校验器按这份清单抽数字回指。
    这就是"叙述层无算术权限"的机制载体：不是求模型别算，而是它算了也过不了校验。

    每个指标还记下它是怎么来的：用到了哪些输入、各取自哪个节点、代入后的算式、
    拿代入式复算是否一致。整张卡落成 metric_set 工件，id 进证据台账——报告里的
    数字点开，一路能追到这里。

    表达式把截断的查询结果当成整组用了（len、sum、max……，见 expressions.TruncatedUse）的指标，
    值照算，但标 incomplete 并写明原因：它算的只是取回的那一截。出具契约据此降档（issuance.incomplete_gaps）。

    取数的那条 SQL 对照数据目录查出 error 级问题的（查询快照的 checks，见 data/sqlcheck.py：一对多关联后重复计算、
    存量跨期加总），同样值照算，标 sql_check_failed 并写明原因，出具契约据此降档（issuance.sql_check_gaps）。
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
        raise NodeError(ctx.node.id, f"「缺输入时」只能是「整个节点失败」或「记为空值」，当前为「{on_missing}」")

    metrics: list[dict[str, Any]] = []
    errors: list[str] = []
    #: 查询快照工件 id → 它（含合并查询的输入）没通过 SQL 检查的那几条查询。一张卡里多个指标常常取自同一次查询，
    #: 快照只读一次
    checked: dict[str, list[QueryProblem]] = {}
    #: 指标 id → 它没通过 SQL 检查的那几条来源查询。运行日志按查询归并（_emit_sql_check_logs）
    failed_by: dict[str, list[QueryProblem]] = {}
    #: 查询快照工件 id → 快照本身（读输入的格落在哪一列「查询区间里这张表没有数据」，_no_data_inputs）
    snapshots: dict[str, Any] = {}
    for definition in definitions:
        metric_id = str(definition.get("id") or "").strip()
        expr = str(definition.get("expression") or "").strip()
        if not metric_id or not expr:
            errors.append(f"指标定义不完整（缺少 ID 或计算表达式）：{definition}")
            continue
        display, problem = _display_of(definition)
        if problem:
            errors.append(f"指标「{metric_id}」：{problem}")
            continue
        try:
            tree, _, _ = parse_expression(expr)
        except ExpressionError as e:
            errors.append(f"指标「{metric_id}」：表达式有误（{e}）")
            continue
        paths = leaf_refs(tree)
        values = {path: _value_of(path, tctx) for path in paths}
        missing = [path for path in paths if values[path] is None]

        value: Any = None
        use = TruncatedUse(edge=lambda path: _truncated_field(path, state, ctx))
        try:
            value = eval_expression(expr, tctx, track=use)
        except ExpressionError as e:
            errors.append(f"指标「{metric_id}」：表达式有误（{e}）")
            continue
        except ZeroDivisionError:
            errors.append(f"指标「{metric_id}」：除以零")
            continue
        except Exception as e:  # noqa: BLE001 - 求值器之外的运算错误（None 参与算术、类型不对）要说人话
            if missing or "NoneType" in str(e):
                if on_missing != "null":
                    errors.append(f"指标「{metric_id}」：" + _missing_reason(missing))
                    continue
                value = None
            elif isinstance(e, TypeError):
                errors.append(f"指标「{metric_id}」：输入的类型不正确，无法计算——" + _type_hint(paths, values))
                continue
            else:
                errors.append(f"指标「{metric_id}」：无法计算（{describe_exception(e)}）")
                continue

        if display["bad_decimals"] and isinstance(value, float):
            # 旧写法里 int() 认不得的 decimals：以前走到 round(value, int(decimals)) 就崩，
            # 现在照样算失败，只是说人话。值不是小数时以前根本用不到它，照旧忽略
            errors.append(f"指标「{metric_id}」：小数位（decimals）需要填写整数，当前为「{definition.get('decimals')}」")
            continue
        decimals = display["decimals"]
        value = round_metric(value, decimals)
        substituted = substitute(tree, values)
        inputs = [_input_of(path, values[path], state, ctx, tctx) for path in paths]
        # 转抄另一张口径卡的指标：那个指标不完整、所依据的查询没通过 SQL 检查的，这一个照样不完整、存疑（出处是跨卡追下去
        # 的，判档也按追到底的结果，见 engine/lineage.metric_copy）
        carried = [m for item in inputs if (m := _copied_metric(item, tctx)) is not None]
        truncated = list(use.sources.values())
        for source in (s for m in carried if m.get("incomplete") for s in m.get("truncated_sources") or []):
            if isinstance(source, dict) and source not in truncated:
                truncated.append(source)
        incomplete: dict[str, Any] = {}
        if truncated:
            incomplete = {"incomplete": True, "incomplete_reason": _incomplete_reason(truncated),
                          "truncated_sources": truncated}
        for path, item in zip(paths, inputs):
            # 容器型的 vars.* 输入（列表、字典）另存一份完整值：卡里只记了「[12 项]」这样的大概，事后推演
            # （engine/recompute.py）要拿完整的值重算。nodes.*、input.* 的完整值在那个节点封存的产出工件里，
            # agent 字段能按出处从查询快照还原，都不用另存；vars 是谁写的、写进去的是不是节点产出本身说不准，
            # 只能在这一刻存下。只给这种输入多一个键：没有容器型 vars 输入的卡和以前逐字节相同
            if _container_var(path, values[path]) and (stored := await _store_value(path, values[path], ctx)):
                item["value_artifact"] = stored
        sql_check: dict[str, Any] = {}
        copied_checks = [str(c.get("artifact")) for m in carried for c in m.get("sql_check_sources") or []
                         if isinstance(c, dict) and c.get("artifact")]
        if failed := _failed_queries(inputs, truncated, paths, tctx, checked, extra=copied_checks):
            sql_check = {"sql_check_failed": True, "sql_check_reason": _sql_check_reason(failed),
                         "sql_check_sources": [{"artifact": artifact, "codes": list(dict.fromkeys(
                             code for p in found for code in p.codes))} for artifact, found in failed]}
            failed_by[metric_id] = [p for _, found in failed for p in found]
        # 输入取自「查询区间里这张表没有数据」的列（查询快照的 no_data，data/coverage.py）：值照算，标 no_data 并写明原因，
        # 出具契约据此降档（issuance.no_data_gaps）。按输入的快照 id + 列认：同一条查询里有数的列不受影响。转抄的指标
        # 那张卡标了的，这一个照样标
        no_data: dict[str, Any] = {}
        if lacking := _no_data_inputs(inputs, carried, snapshots):
            no_data = {"no_data": True, "no_data_reason": _no_data_reason(lacking), "no_data_sources": lacking}
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
                "inputs": inputs,
                "substituted": substituted,
                "recompute_ok": _recompute(substituted, tctx, value, decimals),
                # 值是空的就是缺输入，不论 on_missing 是哪一种：fail 模式下直接取到空值的
                # 指标（原式就是 vars.x.y 而 y 不存在）以前也是照常产出一个 None
                "status": "ok" if value is not None else "missing_input",
                # 输入都齐、是计算式自己交回的空值（分母为 0 时写的 … if 分母 else None）：算不出来，不是缺输入。状态照旧是
                # missing_input（证据面板、推演、运行对比按状态认的地方不受影响），只多这一个键；有值的指标和以前一字不差
                **({"undefined": True} if value is None and not missing else {}),
                # 只在不完整时出现：完整的指标和以前一字不差，老运行复核时目录也对得上
                **incomplete,
                # 只在来源查询没通过 SQL 检查时出现，同上
                **sql_check,
                # 只在输入取自「查询区间里这张表没有数据」的列时出现，同上
                **no_data,
            }
        )

    if errors:
        # 口径卡是固定层：任何一个指标算不出来都是模板缺陷，不能带病出具
        raise NodeError(ctx.node.id, "口径卡计算失败：" + "；".join(errors))

    # text 是给叙述节点 prompt 用的清单——它是叙述层唯一的数字来源
    lines = [
        f"- {m['id']}（{m['name']}）= {_listed(m)}"
        # 不写行数：叙述节点照抄进正文就是一个没有出处的数
        + ("（不完整：基于被截断的查询结果计算）" if m.get("incomplete") else "")
        + ("（存疑：所依据的查询未通过 SQL 检查）" if m.get("sql_check_failed") else "")
        + (LISTED_NO_DATA if m.get("no_data") else "")
        for m in metrics
    ]
    for m in metrics:
        if m.get("incomplete"):
            # 结构化字段（metric_name、reason、rows）给界面读，不让它从 message 里解析中文
            ctx.emit(EventType.LOG, level="warn", code="metric_incomplete", metric=m["id"], metric_name=m["name"],
                     reason=m["incomplete_reason"], rows=[s.get("rows") for s in m.get("truncated_sources") or []],
                     message=f"指标「{m['name']}」{m['incomplete_reason']}")
    _emit_sql_check_logs(metrics, failed_by, state, ctx)
    if lacking := [m for m in metrics if m.get("no_data")]:
        # 结构化字段给界面读：metrics / metric_names（受影响的指标）、reason（第一个指标的那句）、sources
        ctx.emit(EventType.LOG, level="warn", code="metric_no_data", metric=lacking[0]["id"],
                 metrics=[m["id"] for m in lacking], metric_names=[m["name"] for m in lacking],
                 reason=lacking[0]["no_data_reason"],
                 message="指标" + "".join(f"「{m['name']}」" for m in lacking) + lacking[0]["no_data_reason"])
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
        raise NodeError(ctx.node.id, f"「{field_label('caliber_from')}」格式有误：需要指定工作流、版本和其中的口径卡，"
                                     f"当前为「{ref}」")
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, wf_id)
        if workflow is None:
            raise NodeError(ctx.node.id, f"「{field_label('caliber_from')}」固定的工作流（{wf_id}）不存在，可能已被删除，"
                                         "请在节点中重新选择口径卡")
        snapshot = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf_id, WorkflowVersion.version == version))).scalar_one_or_none()
        name = workflow.name
    if snapshot is None:
        raise NodeError(ctx.node.id, f"「{field_label('caliber_from')}」固定的「{name}」不存在 v{version} 版本，请在节点中重新选择版本")
    found = next((n for n in (snapshot.graph or {}).get("nodes") or []
                  if isinstance(n, dict) and n.get("id") == node_id), None)
    if found is None:
        raise NodeError(ctx.node.id, f"「{name}」v{version} 中没有节点 {node_id}，请在节点中重新选择口径卡")
    data = found.get("data") if isinstance(found.get("data"), dict) else {}
    title = data.get("label") or node_id
    if found.get("type") != "metrics":
        raise NodeError(ctx.node.id, f"「{field_label('caliber_from')}」指向的「{name}」v{version} 中的「{title}」不是口径卡，"
                                     f"而是「{type_label(found.get('type'))}」")
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    origin = cfg.get("caliber_from")
    if origin not in (None, "", {}):
        root = origin.get("workflow_id") if isinstance(origin, dict) else None
        async with SessionLocal() as session:
            upstream = await session.get(Workflow, str(root)) if root else None
        where = (f"「{upstream.name}」v{origin.get('workflow_version')} 的 {origin.get('node_id')}"
                 if upstream is not None else str(origin))
        raise NodeError(ctx.node.id, f"「{name}」v{version} 中的「{title}」本身也引用自其他工作流的口径卡（源头是 {where}）。"
                                     "请直接固定到源头的口径卡，否则升版时无法正确比对版本")
    source = {"workflow_id": wf_id, "workflow_version": version, "node_id": node_id}
    metrics = cfg.get("metrics") or []
    rename = ref.get("nodes") if isinstance(ref, dict) else None
    if rename not in (None, {}):
        # 节点改名映射（next-round 02 做法 4）：那一版的式子读 nodes.fetch，这张图里拷过来的取数节点叫 c1_fetch。式子按
        # 语法树把 nodes.<原 id> 的根换成新 id 再算；来源里照记映射，证据、对比、升版处置据此认得是同一份口径
        if problem := rename_problem(rename):
            raise NodeError(ctx.node.id, f"「{field_label('caliber_from')}」的节点改名映射有误：{problem}")
        metrics = [rename_definition(m, rename) for m in metrics]
        source["nodes"] = dict(rename)
    return {"caliber": cfg.get("caliber") or title, "caliber_version": str(cfg.get("caliber_version") or "v1"),
            "metrics": metrics}, source


#: 改名映射里的节点 id：要能写进式子的取值链（nodes.<id>），和画布上的节点 id 同一种写法
_NODE_ID = re.compile(r"^[A-Za-z_][\w-]*$")


def rename_problem(rename: Any) -> str | None:
    """caliber_from.nodes 的形状对不对：{原 id: 新 id}，两边都是非空的节点 id，新 id 不重复。对的是 None。
    画布校验（engine/schema._caliber_from_issues）和运行时同一个判断；新 id 在不在这张图里由校验另查。"""
    if not isinstance(rename, dict) or not rename:
        return "需要写成「原节点 id → 这张图里的节点 id」的对照"
    seen: set[str] = set()
    for old, new in rename.items():
        if not isinstance(old, str) or not _NODE_ID.match(old) or not isinstance(new, str) or not _NODE_ID.match(new):
            return f"「{old}」→「{new}」不是节点 id 的写法"
        if new in seen:
            return f"有两个节点都改名成了「{new}」"
        seen.add(new)
    return None


class _RenameNodes(ast.NodeTransformer):
    """nodes.<原 id>（含 cell(nodes.<原 id>, …) 的第一个参数、nodes['原 id']）的根换成新 id。别的根（vars、input）不动。"""

    def __init__(self, rename: dict[str, str]) -> None:
        self.rename = rename
        self.hits = 0

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if isinstance(node.value, ast.Name) and node.value.id == "nodes" and node.attr in self.rename:
            self.hits += 1
            return ast.copy_location(ast.Attribute(value=node.value, attr=self.rename[node.attr], ctx=node.ctx), node)
        return self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        key = node.slice
        if isinstance(node.value, ast.Name) and node.value.id == "nodes" and isinstance(key, ast.Constant) \
                and isinstance(key.value, str) and key.value in self.rename:
            self.hits += 1
            return ast.copy_location(ast.Subscript(value=node.value, slice=ast.Constant(self.rename[key.value]),
                                                   ctx=node.ctx), node)
        return self.generic_visit(node)


def rename_expression(expression: str, rename: dict[str, str]) -> str:
    """把式子里 nodes.<原 id> 的根按映射换成新 id（语法树上换，字符串字面量里的字不动）。一处都没换到、解析不了的原样交回：
    解析不了的式子后面求值时照常报「表达式有误」。"""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return expression
    renamer = _RenameNodes(rename)
    tree = ast.fix_missing_locations(renamer.visit(tree))
    return ast.unparse(tree) if renamer.hits else expression


def rename_definition(definition: Any, rename: dict[str, str]) -> Any:
    """一个指标定义按映射改写式子（别的项原样）。不是对象的原样交回，后面照常报「指标定义不完整」。"""
    if not isinstance(definition, dict) or not isinstance(definition.get("expression"), str):
        return definition
    return {**definition, "expression": rename_expression(definition["expression"], rename)}


def source_expression(expression: str, source: Any) -> str:
    """卡上记的式子换回那一版里的写法（来源里有改名映射时按映射反着换）：对比两次运行的口径（engine/compare）按它比，
    同一份口径拷进图里时取数节点改了名，不算口径变了。没有映射的原样交回。"""
    rename = source.get("nodes") if isinstance(source, dict) else None
    if not isinstance(rename, dict) or not rename or rename_problem(rename) is not None:
        return expression
    return rename_expression(expression, {new: old for old, new in rename.items()})


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
        return {}, f"显示格式（format）只能是 {' / '.join(FORMATS)}，当前为「{fmt}」"
    display: dict[str, Any] = {"decimals": None, "format": fmt, "bad_decimals": False}
    if raw in (None, ""):
        return display, None
    if strict:
        decimals = _integer(raw)
        if decimals is None or not -_DECIMALS_RANGE <= decimals <= _DECIMALS_RANGE:
            return {}, (f"小数位（decimals）需要填写 -{_DECIMALS_RANGE} 到 {_DECIMALS_RANGE} 的整数"
                        f"（负数表示取整到十位、百位），当前为「{raw}」")
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


def round_metric(value: Any, decimals: Any) -> Any:
    """口径卡的取整：只取整浮点，按小数位 round（Python 的 round，负数取整到十位、百位）；整数、文本、空值原样，
    小数位没写或不是整数（老卡上记着的 true、2.0 这类怪值）时也原样。

    推演重算（engine/recompute.py）、双盲复算的盲值都按卡重算，取整规则只能有这一处：两边各写一行，口径卡哪天换了
    取整方式，推演就会对着同一张卡算出另一个数。"""
    if isinstance(value, float) and isinstance(decimals, int) and not isinstance(decimals, bool):
        return round(value, decimals)
    return value


def _rendered(value: Any, definition: dict[str, Any], display: dict[str, Any]) -> str:
    try:
        return render_number(value, unit=str(definition.get("unit") or ""),
                             decimals=display["decimals"], fmt=display["format"])
    except RenderError:
        # 按规则渲染不出来的值显示「—」：inf / nan、不是 0 却会显示成 0 的（小数位不够）、
        # 位数大到不像话的。值本身照旧记着，报告里引用它会被判成解析不了，原因写清楚
        return MISSING


#: 清单里输入取自「查询区间里这张表没有数据」的列的指标后面那句：直接读清单的模型节点不能把 0 当成真的是 0
LISTED_NO_DATA = "（这段时间表里没有数据：不要当成 0 解读）"
#: 指标的 no_data_reason 打头那半句（后面接查询记下的说明）
NO_DATA_REASON = "所依据的查询区间里这张表没有数据"


def _no_data_inputs(inputs: list[dict[str, Any]], carried: list[dict[str, Any]],
                    snapshots: dict[str, Any]) -> list[dict[str, Any]]:
    """这个指标的输入里，取自「查询区间里这张表没有数据」的列的：[{artifact, column, table, window, message}]。

    认两处：输入的出处带着查询快照 id 和列（cell() 取的格、按位置取的格、Agent 经 cite_fields 交来的字段），快照里的
    no_data 记着这一列；转抄的指标那张卡标了 no_data 的（no_data_sources 照抄）。读不出来的快照不算。"""
    from app.data.coverage import no_data_of

    out: list[dict[str, Any]] = []
    for item in inputs:
        artifact = item.get("artifact")
        locator = item.get("locator") if isinstance(item.get("locator"), dict) else {}
        columns = [locator.get("column")] if locator.get("column") is not None \
            else list((locator.get("columns") or {}).values()) if isinstance(locator.get("columns"), dict) else []
        if not isinstance(artifact, str) or not artifact or not columns:
            continue
        if artifact not in snapshots:
            try:
                snapshots[artifact] = artifact_store.load(artifact)
            except (ValueError, OSError):
                snapshots[artifact] = None
        for column in columns:
            found = no_data_of(snapshots[artifact], column)
            if found is None:
                continue
            entry = {"artifact": artifact, "column": str(column), "table": found.get("table"),
                     "window": found.get("window"), "message": found.get("message")}
            if entry not in out:
                out.append(entry)
    for metric in carried:
        for entry in metric.get("no_data_sources") or []:
            if isinstance(entry, dict) and entry not in out:
                out.append(dict(entry))
    return out


def _no_data_reason(lacking: list[dict[str, Any]]) -> str:
    """「所依据的查询区间里这张表没有数据：「上周到访人数」查的是 … 」：出具缺口、运行日志、证据面板、写作目录都用这句。"""
    said = list(dict.fromkeys(str(e.get("message") or "") for e in lacking if e.get("message")))
    return NO_DATA_REASON + (f"：{'；'.join(said[:2])}" if said else "")


#: 清单里没有值的两种说法：缺输入，和输入都齐、计算式自己算不出来（分母为 0）
LISTED_MISSING = "（缺少输入，无值）"
LISTED_UNDEFINED = "（算不出来：这组输入下计算式没有值，比如分母为 0）"
#: 有值、按口径卡的小数位和格式显示不出来（太小、太大、不是有限的数）：不把原值拼出来，照实说
LISTED_UNSHOWABLE = "（有值，但按设定的小数位和格式显示不出来）"


def _listed(metric: dict[str, Any]) -> str:
    """清单里这个指标的值：卡上按单位、小数位、格式渲染好的字（rendered，和报告里经 [[m:]] 渲染出来的一样）。以前拼原值
    （{value}{unit}），比率 0.0007 配 % 写成「0.0007%」，直接读清单的模型节点差 100 倍。"""
    if metric["value"] is None:
        return MISSING + (LISTED_UNDEFINED if metric.get("undefined") else LISTED_MISSING)
    rendered = str(metric.get("rendered") or "")
    if not rendered or rendered == MISSING:
        return MISSING + LISTED_UNSHOWABLE
    return rendered


def _value_of(path: str, tctx: dict[str, Any]) -> Any:
    """单独取一个引用的值。取不到（或取值本身出错）一律当作没有值。"""
    try:
        return eval_expression(path, tctx)
    except Exception:  # noqa: BLE001
        return None


def _incomplete_reason(sources: list[dict[str, Any]]) -> str:
    """「基于被截断的查询结果计算（只取回了前 1000 行），结果不完整」：出具声明、运行日志、证据面板都用这句。"""
    fetched = [f"前 {s['rows']} 行" for s in sources]
    if len(fetched) == 1:
        return f"基于被截断的查询结果计算（只取回了{fetched[0]}），结果不完整"
    return f"基于 {len(fetched)} 份被截断的查询结果计算（分别只取回了{'、'.join(fetched)}），结果不完整"


#: 取值链的最后一段：.字段 或 [下标]
_LAST_STEP = re.compile(r"(?:\.[^.\[\]]+|\[[^\[\]]*\])$")


def _failed_queries(inputs: list[dict[str, Any]], truncated: list[dict[str, Any]], paths: list[str],
                    tctx: dict[str, Any], checked: dict[str, list[QueryProblem]], *,
                    extra: list[str] | tuple[str, ...] = ()) -> list[tuple[str, list[QueryProblem]]]:
    """这个指标用到的查询里，SQL 检查有 error 的：[(查询快照工件 id, 没通过检查的查询)]。合并查询的快照对应它出问题的
    那几个输入（sql_problems.query_problems）。

    用到了哪些查询，四处来历合起来认：输入的出处（cell() 取的格、Agent 经 cite_fields 交来的字段都带着快照 id）、
    截断跟踪记下的整组、取值链上的查询结果本身（数据整形解析过的 vars.res.rows[0][0]，往上找到 vars.res 那份
    带 artifact 的结果），以及 extra：转抄的指标所依据、没通过检查的那几条（跨卡追下去的）。检查结果从快照里读，不信内存里那份：快照按哈希寻址，就是查询当时存下的那份。
    读不出来的快照不算（另有别的检查报快照的问题）。Agent 没开 cite_fields 时说不清数出自哪次查询，不算。
    """
    artifacts = [item.get("artifact") for item in inputs] + [s.get("artifact") for s in truncated]
    artifacts += [_result_artifact(path, tctx) for path in paths if cell_parts(path) is None]
    # 转抄的指标所依据、没通过检查的查询（extra，跨卡追下去的那几条）
    artifacts += list(extra)
    out = []
    for artifact in dict.fromkeys(a for a in artifacts if isinstance(a, str) and a):
        if artifact not in checked:
            checked[artifact] = query_problems(artifact)
        if checked[artifact]:
            out.append((artifact, checked[artifact]))
    return out


def _result_artifact(path: str, tctx: dict[str, Any]) -> str | None:
    """顺着取值链往上找（vars.res.rows[0][0] → vars.res.rows[0] → vars.res.rows → vars.res），第一个查询结果的快照 id。
    数据源工具交回的是 JSON 文本（调用工具节点的输出），解析过的是对象（数据整形之后），两种都认。"""
    current = path
    for _ in range(8):
        value = _value_of(current, tctx)
        if isinstance(value, str) and value.lstrip().startswith("{") and '"artifact"' in value:
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        if isinstance(value, dict) and isinstance(value.get("artifact"), str) and isinstance(value.get("rows"), list):
            return value["artifact"]
        shorter = _LAST_STEP.sub("", current)
        if not shorter or shorter == current:
            return None
        current = shorter
    return None


def _error_checks(artifact: str) -> list[dict[str, Any]]:
    """这份查询快照对照数据目录查出的 error 级问题（合并查询顺着 inputs 往下找，见 sql_problems.query_problems）。"""
    return [c for p in query_problems(artifact) for c in p.checks]


def _sql_check_reason(failed: list[tuple[str, list[QueryProblem]]]) -> str:
    """「所依据的查询未通过 SQL 检查（「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算），
    结果不可靠」：出具声明、运行日志、证据面板都用这句。只摘问题那半句，改法留在查询步骤里看。"""
    return reason_text(list(dict.fromkeys(text for _, found in failed for p in found for text in p.problems)))


def _emit_sql_check_logs(metrics: list[dict[str, Any]], failed_by: dict[str, list[QueryProblem]],
                         state: GraphState, ctx: NodeContext) -> None:
    """没通过 SQL 检查的来源查询，一条一行运行日志（metric_sql_check），列出受它影响的指标。

    以前一个指标一行：同一条查询算出三个指标，时间线上同一句话重复三遍。现在按出问题的那条查询归并。
    结构化字段给界面读，不让它从 message 里解析中文：metrics / metric_names（受影响的指标）、reason、problems、
    checks（不带 for_model）、query_artifact（出问题的查询快照）、source_node（跑这条查询的节点）、
    source_field（要改的那一栏：调用工具节点写死的 SQL 在 args.sql；Agent 自己写的 SQL 没有可定位的栏，不给）。
    界面据 source_node / source_field 把「打开设置」落到来源查询节点的 SQL 上，而不是口径卡。
    """
    names = {m["id"]: m["name"] for m in metrics}
    groups: dict[str, tuple[QueryProblem, list[str]]] = {}
    for metric_id, found in failed_by.items():
        for problem in merge_problems(found):
            held = groups.setdefault(problem.artifact, (problem, []))
            if metric_id not in held[1]:
                held[1].append(metric_id)
    nodes = ctx.run.spec.node_map()
    for artifact, (problem, ids) in groups.items():
        source = problem.node_id or node_of(artifact, state.get("evidence"))
        producer = nodes.get(source) if source else None
        sql_in_args = producer is not None and str(producer.type) == "tool" \
            and isinstance((producer.config.get("args") or {}), dict) and "sql" in (producer.config.get("args") or {})
        reason = reason_text(problem.problems)
        ctx.emit(EventType.LOG, level="warn", code="metric_sql_check",
                 metric=ids[0], metrics=ids, metric_names=[names.get(i, i) for i in ids],
                 reason=reason, problems=problem.problems,
                 checks=[{k: c[k] for k in CHECK_FIELDS if k in c} for c in problem.checks],
                 query_artifact=artifact,
                 **({"source_node": source} if source else {}),
                 **({"source_field": "args.sql"} if sql_in_args else {}),
                 message="指标" + "".join(f"「{names.get(i, i)}」" for i in ids) + reason)


def _truncated_field(path: str, state: GraphState, ctx: NodeContext) -> dict[str, Any] | None:
    """agent 经 cite_fields 交来、被截断切开的数组字段：{rows, artifact}。别的取值链返回 None。

    数组字段的出处是快照里的一段行（data_evidence 的 locator.rows = [a, b]）。cite_fields 核对时，这段行
    到了截断快照的末行就记 truncated（llm._verify_rows）：快照后面还有没取回的行，可能也属于这个字段，
    对它求和、计数算的就只是取回的那一截。停在末行之前的不记——引用的每一行都在快照里，截断拿不走其中
    任何一行。标量字段是一格，不是聚合，也不记。取回的行数就是末行的行号加一。

    路径怎么对到字段和 _input_of 一样：vars.X 找最后写它的节点，nodes.<agent> 下多走一层 data。
    求值器只对取到列表的取值链问这里，一个指标问不了几次。
    """
    head = _REF_HEAD.match(path)
    if not head or head.group(1) == "input":
        return None
    root, name, rest = head.groups()
    field = rest.lstrip(".")
    node_id = name if root == "nodes" else _writer_of(name, state, ctx)
    producer = ctx.run.spec.node_map().get(node_id) if node_id and field else None
    if producer is None or str(producer.type) != "agent":
        return None
    output = (state.get("nodes") or {}).get(producer.id)
    evidence = output.get("data_evidence") if isinstance(output, dict) else None
    key = _field_key(field)
    if root == "nodes":
        key = key[len("data."):] if key.startswith("data.") else ""
    cited = evidence.get(key) if isinstance(evidence, dict) and key else None
    rows = (cited.get("locator") or {}).get("rows") if isinstance(cited, dict) and cited.get("truncated") else None
    if not (isinstance(rows, list) and len(rows) == 2 and isinstance(rows[1], int)):
        return None
    return {"rows": rows[1] + 1, "artifact": cited.get("artifact")}


def _missing_reason(missing: list[str]) -> str:
    if not missing:
        return "有输入为空值，无法计算（请检查上游是否产出了这些字段）"
    return f"输入 {'、'.join(missing[:4])} 没有值，无法计算（上游没有产出该字段，或路径有误）"


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
        return "参与运算的值不是数值"
    return "、".join(parts[:4]) + "，不能参与数值运算"


_REF_HEAD = re.compile(r"^(vars|nodes|input)\.([A-Za-z_一-鿿][\w\-一-鿿]*)(.*)$", re.S)
_VIA = {"input": "input", "code": "code", "agent": "agent", "llm": "llm", "tool": "tool",
        "transform": "transform", "metrics": "metric", "retrieve": "retrieval", "loop": "loop",
        "human": "human", "validate": "validate", "supervisor": "team", "subgraph": "subgraph",
        "memory": "memory", "merge": "merge"}


def _input_of(path: str, value: Any, state: GraphState, ctx: NodeContext,
              tctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """一个输入的来历：{path, value, node_id, via, field?, role?, status}。

    vars.X 要反查是谁写的：assign_to 为 X 的节点里，按执行顺序最后跑的那一个——
    和状态里 vars 的覆盖顺序一致。入口节点把输入字段也写进 vars。

    三种来历能追到查询快照里的那一格（engine/lineage.py 说「追到了原始数据」的就是这几种，加上运行输入）：
    - cell(nodes.fetch, 0, 'gmv')：via tool_cell，带 locator、artifact、eid，按快照核对
    - vars.q.rows[0][0]，q 由「只是把调用工具节点的结果原样按 JSON 解析」的整形节点写：同 cell()，via tool_cell，
      另记 through（穿过的整形节点）
    - agent 开了 cite_fields 的字段：via agent_field，接上它的 data_evidence（eid、核对状态）

    读子工作流出口的一个字段（nodes.<子图>.output.<字段>、vars.<子图的 assign_to>.<字段>）：接上子图记下的那个字段的出处
    （_subgraph_field），via 等照子图里认出的写，另记 subgraph、field；认不出的照旧记 via subgraph，停在子工作流。
    """
    if (parts := cell_parts(path)) is not None:
        return _cell_input(path, value, parts, state, ctx, tctx or template_context(state))
    if (parsed := _parsed_cell(path, value, state, ctx, tctx or template_context(state))) is not None:
        return parsed
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
    if producer is not None and str(producer.type) == "subgraph" and head \
            and (linked := _subgraph_field(path, value, root, field, producer.id, state)) is not None:
        return linked
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
    if producer is not None and str(producer.type) == "metrics":
        # 转抄另一张口径卡的指标：跨卡往下追（engine/lineage.metric_copy）
        if (up := _upstream(path, tctx or template_context(state))) is not None:
            entry["upstream"] = up
    if producer is not None and str(producer.type) == "agent" and field:
        cited = _agent_field(state, producer.id, root if head else "", field)
        if cited is not None:
            entry.update(cited)
    return entry


def _subgraph_field(path: str, value: Any, root: str, field: str, node_id: str,
                    state: GraphState) -> dict[str, Any] | None:
    """读子工作流出口的一个字段：接上子图记下的那个字段的出处（multi.run_subgraph 的 provenance，节点 id 已经是这一层图里
    的「子工作流节点 id/…」）。只认整个字段（nodes.<子图>.output.<字段>、vars.<assign_to>.<字段>，不再往里取），而且这次
    读到的值就是子图交出的那个；别的为 None，照一般的来历记（via subgraph，停在子工作流）。

    接上的出处照子图里认出的写（via、node_id、artifact、locator、eid、status、upstream），path、value 换成这里读的，另记
    subgraph（经过的子工作流节点）、field（出口字段）：engine/lineage.traced 照常认，证据面板照常一路给到子图里的那一格。"""
    result = (state.get("nodes") or {}).get(node_id)
    if not isinstance(result, dict) or not isinstance(result.get("provenance"), dict):
        return None
    key = _field_key(field)
    if root == "nodes":
        if not key.startswith("output."):
            return None
        key = key[len("output."):]
    elif root != "vars":
        return None
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    item = result["provenance"].get(key)
    if not key or not isinstance(item, dict) or key not in output or output[key] != value:
        return None
    return {**item, "path": path, "value": value if value is None or isinstance(value, (int, float, str, bool))
            else _brief(value), "subgraph": node_id, "field": key}


def _copied_card(path: str, tctx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """转抄另一张口径卡一个指标的输入（engine/lineage.metric_copy 认的写法）：(那张卡这一次的产出, 那个指标)。不是这种
    写法、产出不是指标集、下标越界的为 None。"""
    from app.engine.lineage import metric_copy

    found = metric_copy(path)
    if found is None:
        return None
    root, name, index = found
    card = _value_of(f"{root}.{name}", tctx)
    metrics = card.get("metrics") if isinstance(card, dict) and card.get("kind") == "metric_set" else None
    if not isinstance(metrics, list) or index >= len(metrics) or not isinstance(metrics[index], dict):
        return None
    return card, metrics[index]


def _upstream(path: str, tctx: dict[str, Any]) -> dict[str, Any] | None:
    """转抄另一张口径卡的指标时，跨卡往下追的出处：{node_id?, metric, artifact, depth, traced}。那张卡的指标集落了工件、
    那个指标的输入都追到了原始数据（engine/lineage.untraced 为空；它自己转抄的再往下一层层追过了，记在它的输入上）、
    层数没超过 MAX_COPY_DEPTH，才算追到。不是转抄指标的为 None（照旧停在那张卡）。"""
    from app.engine.lineage import MAX_COPY_DEPTH, untraced

    found = _copied_card(path, tctx)
    if found is None:
        return None
    card, metric = found
    inputs = [i for i in metric.get("inputs") or [] if isinstance(i, dict)]
    depth = 1 + max([int((i.get("upstream") or {}).get("depth") or 0) for i in inputs
                     if isinstance(i.get("upstream"), dict)] or [0])
    artifact = card.get("artifact") if isinstance(card.get("artifact"), str) else None
    return {"metric": metric.get("id"), "artifact": artifact, "depth": depth,
            "traced": bool(artifact) and depth <= MAX_COPY_DEPTH and not untraced(inputs)}


def _copied_metric(item: dict[str, Any], tctx: dict[str, Any]) -> dict[str, Any] | None:
    """卡上的一个输入转抄了另一张口径卡的哪个指标（这一次的产出里那一项）；不是转抄的为 None。

    经子工作流出口转抄的（subgraph，子图出口字段就是子图里一张卡的指标）：那张卡以「子工作流节点 id/卡」进了这一层的
    nodes（multi.run_subgraph），按 node_id 和 upstream 记的指标 id 找。"""
    if item.get("via") != "metric" or not isinstance(item.get("upstream"), dict):
        return None
    if item.get("subgraph"):
        card = (tctx.get("nodes") or {}).get(str(item.get("node_id") or ""))
        metrics = card.get("metrics") if isinstance(card, dict) and card.get("kind") == "metric_set" else None
        wanted = item["upstream"].get("metric")
        return next((m for m in metrics or [] if isinstance(m, dict) and m.get("id") == wanted), None)
    found = _copied_card(str(item.get("path") or ""), tctx)
    return found[1] if found is not None else None


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
    for k in ("ref", "artifact", "locator", "eid", "model_value", "reason", "truncated"):
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
        out.update(status="unresolved", reason=f"查询快照无法读取或与哈希不一致：{e}")
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


def _parsed_cell(path: str, value: Any, state: GraphState, ctx: NodeContext,
                 tctx: dict[str, Any]) -> dict[str, Any] | None:
    """穿过「只是把调用工具节点的结果原样按 JSON 解析」的整形节点按位置取的一格（vars.q.rows[0][0]、
    nodes.parse.rows[0]['gmv']）：整形节点不改动任何一格，第 i 行第 j 列就是那份查询快照的第 i 行第 j 列，和 cell() 一样
    按快照核对（_cell_input），through 记下穿过的整形节点。

    不是这种取法（不是 .rows[行][列] 的样子、写它的不是这种整形节点）返回 None，按一般的来历记：表达式、模板拼出来的
    结构即使带着 rows 和快照 id，行也可能被筛过、换过顺序，对不到快照的同一行（见 engine/lineage.parsed_tool）。"""
    at = row_cell(path)
    if at is None:
        return None
    nodes = ctx.run.spec.node_map()
    transform = at.name if at.root == "nodes" else _writer_of(at.name, state, ctx)
    node = nodes.get(transform) if transform else None
    tool = parsed_tool(node.type, node.config, lambda i: nodes[i].type if i in nodes else None) if node else None
    if node is None or tool is None:
        return None
    return _cell_input(path, value, (at.table, repr(at.row), repr(at.column)), state, ctx, tctx,
                       through=(node.id, tool))


def _cell_input(path: str, value: Any, parts: tuple[str, str, str], state: GraphState, ctx: NodeContext,
                tctx: dict[str, Any], *, through: tuple[str, str] | None = None) -> dict[str, Any]:
    """cell() 取的那一格：它来自哪次查询（台账里的 query 条目）、第几行哪一列、快照里是不是这个值。

    查询结果经 transform 解析过也认得出来：数据源工具交回的 JSON 里带着快照的工件 id。
    取不到快照、或者快照里那一格和算的时候用的值不一样，status 如实写，不替它圆。

    through 是 (整形节点, 调用工具节点)：穿过原样按 JSON 解析的整形节点按位置取的格（_parsed_cell）。台账按工件认不出时
    按那个调用工具节点找它最后一次查询，卡上记下穿过的整形节点。
    """
    source, row_src, column_src = parts
    entry: dict[str, Any] = {"path": path, "value": value if value is None or isinstance(value, (int, float, str, bool))
                             else _brief(value), "node_id": None, "via": "tool_cell"}
    if through is not None:
        entry["through"] = through[0]
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
    producer = through[1] if through is not None else head.group(2) if head and head.group(1) == "nodes" else None
    if ledger is None and producer:
        # 台账是按工件认的；工具交回的不是带工件 id 的 JSON 时，按取值链上的节点找它最后一次查询
        ledger = next((e for e in reversed(queries) if e.get("node_id") == producer), None)
    if ledger is None:
        entry.update(node_id=producer, status="unresolved",
                     reason="找不到这份数据对应的查询快照：cell() 需要读取查询工具返回的结果" if through is None
                     else "找不到这份数据对应的查询快照：调用工具节点交回的不是数据源查询的结果")
        return entry
    entry["node_id"] = ledger.get("node_id")
    artifact = ledger.get("artifact")
    try:
        raw, name = locate_cell(data if isinstance(data, dict) else table, row, column)
    except CellError as e:
        entry.update(status="unresolved", reason=f"cell() {e}" if through is None else f"{path}：{e}", artifact=artifact)
        return entry
    locator = {"row": row, "column": name}
    entry.update(locator=locator, artifact=artifact, eid=cell_eid(artifact, row, name))
    try:
        snapshot = artifact_store.load(artifact)
        truth, _ = locate_cell(snapshot, row, name)
    except (ValueError, CellError, OSError) as e:
        entry.update(status="unresolved", reason=f"查询快照无法读取或与哈希不一致：{e}")
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
    again = round_metric(again, decimals)
    if is_number(again) and is_number(value):
        return math.isclose(again, value, rel_tol=1e-12, abs_tol=0.0) or again == value
    return again == value


#: 另存的完整值最大多少字节（规范 JSON）。查询结果整份赋给变量时可能很大，每张卡都再存一份不值当：大过这个数就
#: 不存，推演照实说当时没有保存完整值
VALUE_ARTIFACT_MAX = 4 * 1024 * 1024


def _container_var(path: str, value: Any) -> bool:
    head = _REF_HEAD.match(path)
    return isinstance(value, (list, dict)) and head is not None and head.group(1) == "vars"


async def _store_value(path: str, value: Any, ctx: NodeContext) -> str | None:
    """容器型 vars 输入的完整值落成 metric_input 工件：{path, value}。它的 id 记在指标集里，指标集内容寻址、
    在封存范围内，完整值也就随运行封存，不另起台账条目。存不下（太大、写失败）就不存：口径卡照常算出来，
    只是这个输入以后推演不了。"""
    from app.core.artifact_store import canonical_json, put_json

    content = {"path": path, "value": value}
    try:
        if len(canonical_json(content).encode()) > VALUE_ARTIFACT_MAX:
            return None
        return await put_json(content, kind="metric_input", run_id=ctx.run.run_id, node_id=ctx.node.id,
                              meta={"path": path})
    except Exception:  # noqa: BLE001 - 完整值存不下不该毁掉口径卡
        return None


async def _store(card: dict[str, Any], caliber: str, version: str, ctx: NodeContext) -> str | None:
    """整张卡落成 metric_set 工件。写失败不毁掉运行，只是这张卡没法被引用。"""
    from app.core.artifact_store import put_json

    try:
        return await put_json(card, kind="metric_set", run_id=ctx.run.run_id, node_id=ctx.node.id,
                              meta={"caliber": caliber, "version": version})
    except Exception as e:  # noqa: BLE001
        ctx.emit(EventType.LOG, level="warn", code="evidence_store_failed",
                 message=f"口径卡的指标保存失败（{describe_exception(e)}），报告将无法引用这些指标")
        return None
