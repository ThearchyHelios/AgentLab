"""蓝图和关节的生成（N2、N3、N11）：接到 api/copilot_gen.Generation 上的两对落地与自查。

- BlueprintApplier / BlueprintChecker：蓝图助手只收 bp_* 操作，落地走 blueprints/ops.apply_bp_op（来源记 assistant，
  只提议：助手写的项都是「待确认」）。复用口径卡的操作按这一轮读好的口径核对并补上它读的列（look/calibers）；
  越界被拒的照实交回模型一次、交付时列给人看（同 AppApplier 的做法）。
- TemplateApplier / TemplateChecker：填关节只收 fill_joint {joint, value}，而且只收这一组里模板声明过的生成关节；
  改别的位置、别的组的关节、或者用别的操作，一律拒绝并照实交回（照 apps/ops.polish_problem 的写法）。自查跑 GraphChecker
  的 SQL 检查（只读守卫、猜列、数据源范围、目录检查）、指标式的白名单（只许 cell() 取格加算术）和 engine/lineage.static_stops
  （静态追到原始数据），再和骨架逐节点比对（tpl.skeleton_changed）。
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from app.blueprints.ops import BpRefused, apply_bp_op
from app.blueprints.spec import BP_OPS
from app.look.base import Applied

#: 越界被拒的编号（交回模型、列给人看）
BP_REFUSED = "bp.refused"
JOINT_REFUSED = "tpl.joint_refused"
JOINT_UNFILLED = "tpl.joint_unfilled"
METRIC_EXPR = "tpl.metric_expr"
METRIC_COLUMN = "tpl.metric_column"
SQL_REJECTED = "tpl.sql_rejected"
#: 指标式里能用的函数：取格和算术（叙述层不做算术，算术只在口径卡里，P7）
METRIC_FUNCS = frozenset({"cell", "round", "abs", "min", "max"})

BLUEPRINT_REPAIR_PROMPT = (
    "这是你刚改好的蓝图：\n{spec}\n\n检查发现下面这些问题：\n{issues}\n\n"
    "只修这些问题，别的不要动。用 bp_* 操作输出修正；拿不准、要用户拿主意的用 bp_ask 问。最后一行输出 done。{looks}")
TEMPLATE_REPAIR_PROMPT = (
    "这是你刚填的关节：\n{values}\n\n检查（和发布同一套规则）发现下面这些问题：\n{issues}\n\n"
    "只修这些问题：用 fill_joint 重新填有问题的关节（整段重写），别的不要动。最后一行输出 done。{looks}")
REPAIR_LOOKS_PROMPT = "\n\n这一轮查看过的内容（和起草时看到的是同一份）：\n{text}"
ISSUE_LINE_PROMPT = "- {where}{message}"


def _issue_lines(issues: list[dict[str, Any]]) -> str:
    return "\n".join(ISSUE_LINE_PROMPT.format(where=f"{i.get('path') or i.get('joint') or i.get('node_id') or ''} ",
                                              message=i.get("for_model") or i.get("message") or "").replace("  ", " ")
                     for i in issues)


# ==========================================================================
# 蓝图
# ==========================================================================


class BlueprintApplier:
    """蓝图的落地：bp_* 操作按 ops.apply_bp_op 落到 spec 上（每落一个换一份新的），来源记 assistant。

    plan、done 记下说明；reply 记下（这句话不需要改蓝图）；bp_ask 记进反问（Generation 读到它就结束这一轮）；别的操作一概
    不收。calibers 是这一轮读好的口径（look/calibers.CaliberKind）：复用口径卡的操作按它核对、补上读的列。"""

    def __init__(self, spec: dict[str, Any], *, calibers: Any = None) -> None:
        self.spec = spec
        self.calibers = calibers
        self.effective: list[dict[str, Any]] = []
        self.plan: dict[str, Any] | None = None
        self.replied: dict[str, Any] | None = None
        self.explanation = ""
        #: 越界被拒的操作：[{op, path, message, code}]
        self.refused: list[dict[str, Any]] = []
        self._reported = 0
        self._landed: list[str] = []
        self._continuing = False
        self._repairing = False

    def apply(self, op: dict[str, Any]) -> Applied:
        kind = str(op.get("op") or "")
        if kind == "plan":
            if self.plan is not None and self._continuing:
                return Applied(False, False)
            self.plan = self.plan or op
            return Applied(False, True)
        if kind == "done":
            self.explanation = str(op.get("explanation", ""))
            return Applied(False, True)
        if kind == "reply":
            if self.replied is None and not self._repairing:
                self.replied = op
            return Applied(False, False)
        if kind not in BP_OPS:
            return Applied(False, False)
        if self._continuing and op in self.effective:
            return Applied(False, False)
        op = {k: v for k, v in op.items() if k in ("op", "path", "value", "question", "options", "template")}
        try:
            op = self._with_reuse(op)
            self.spec = apply_bp_op(self.spec, op, source="assistant")
        except BpRefused as e:
            self.refused.append({"op": kind, "path": op.get("path"), "message": e.message, "code": e.code})
            return Applied(False, False, e.message)
        self.effective.append(op)
        where = str(op.get("path") or op.get("op"))
        if where not in self._landed:
            self._landed.append(where)
        return Applied(True, True)

    def _with_reuse(self, op: dict[str, Any]) -> dict[str, Any]:
        """复用口径卡：按这一轮读好的口径核对，补上那张卡读的取数节点和列。没读口径（calibers 为空）时原样交给存的那一步核。"""
        if self.calibers is None:
            return op
        path = str(op.get("path") or "")
        if op.get("op") == "bp_set" and path.endswith("/reuse") and isinstance(op.get("value"), dict):
            return {**op, "value": self._reuse(op["value"])}
        if op.get("op") == "bp_add" and isinstance(op.get("value"), dict) and isinstance(op["value"].get("reuse"), dict):
            return {**op, "value": {**op["value"], "reuse": self._reuse(op["value"]["reuse"])}}
        return op

    def _reuse(self, ref: dict[str, Any]) -> dict[str, Any]:
        reuse, reason = self.calibers.reuse_for(ref)
        if reuse is None:
            raise BpRefused(f"这张口径卡不能复用：{reason}", code="bp.data_unmapped")
        return reuse

    def landed(self) -> list[str]:
        return list(self._landed)

    def continuing(self) -> None:
        self._continuing = True

    def repairing(self) -> None:
        self._repairing = True

    def unreported(self) -> list[dict[str, Any]]:
        out = self.refused[self._reported:]
        self._reported = len(self.refused)
        return out

    def refusals(self) -> list[dict[str, Any]]:
        """交付时列给人看的：助手想改什么、为什么没改（警告级）。"""
        return [{"code": r["code"], "level": "warning", "path": r["path"],
                 "message": f"助手提的这一处没有采纳：{r['message']}"} for r in self.refused]


class BlueprintChecker:
    """蓝图的自查：越界被拒、还没交回过的操作，加上卡片内容超出模板能力的地方（bp.template_mismatch）——这些是助手提错了，
    交回它改。缺的项（bp.missing、bp.data_unmapped……）不交回：缺的往往是只有用户知道的事，逼它补只会让它编，由它在
    这一轮里 bp_ask 问，或者留给用户在卡片上填。"""

    def __init__(self, applier: BlueprintApplier, *, template_id: str | None = None) -> None:
        self.applier = applier
        self.template_id = template_id

    def lint(self) -> list[dict[str, Any]]:
        from app.blueprints.ops import blueprint_issues

        return blueprint_issues(self.applier.spec, for_="trial")

    async def sources(self) -> list[dict[str, Any]]:
        """对到的数据源和表在不在（要查库，用自己的会话：流式回答时接口的会话可能已经关了）。对到一个不存在的源是助手
        提错了，交回它改，不留给用户到试运行时才碰上。"""
        from app.blueprints.store import data_source_problems
        from app.db.base import SessionLocal

        spec = self.applier.spec
        template_id = (spec.get("template") or {}).get("id") or self.template_id
        async with SessionLocal() as session:
            return await data_source_problems(session, spec, template_id=template_id)

    async def issues(self) -> list[dict[str, Any]]:
        refused = [{"code": r["code"], "level": "error", "path": r["path"], "message": r["message"]}
                   for r in self.applier.unreported()]
        return [*refused, *(i for i in self.lint() if i["code"] == "bp.template_mismatch" and i["level"] == "error"),
                *await self.sources()]

    def repair_request(self, issues: list[dict[str, Any]], looks_text: str) -> str:
        return BLUEPRINT_REPAIR_PROMPT.format(spec=json.dumps(self.applier.spec, ensure_ascii=False),
                                              issues=_issue_lines(issues),
                                              looks=REPAIR_LOOKS_PROMPT.format(text=looks_text) if looks_text else "")


# ==========================================================================
# 模板的生成关节
# ==========================================================================


def joint_problem(joints: dict[str, Any], group: list[Any], op: dict[str, Any]) -> str | None:
    """填关节时这个操作有没有越界：越界返回交回模型的一句话，没越界返回 None（同 apps/ops.polish_problem 的写法）。

    joints 是模板声明的全部关节（id → Joint），group 是这一组要填的生成关节。只收 fill_joint、只收这一组的关节、值是一段
    非空的文字。骨架是锁住的：节点、连线、确定性关节（按卡片填的）和别的配置都不归模型管。"""
    from app.blueprints.templates import GENERATED_JOINT_KINDS

    kind = op.get("op") if isinstance(op, dict) else None
    mine = {j.id for j in group}
    if kind != "fill_joint":
        return (f"填关节只能用 fill_joint 填模板留给你的位置（{'、'.join(sorted(mine))}），不能用 {kind}："
                "模板的节点、连线和别的配置是锁住的")
    joint_id = str(op.get("joint") or "")
    joint = joints.get(joint_id)
    if joint is None:
        return f"模板里没有关节「{joint_id}」：这一轮只能填 {'、'.join(sorted(mine))}"
    if joint.kind not in GENERATED_JOINT_KINDS:
        return f"「{joint.label or joint_id}」按卡片确定地填，不归助手管：要改请回到蓝图卡片"
    if joint_id not in mine:
        return f"「{joint.label or joint_id}」不在这一组里（这一组只填 {'、'.join(sorted(mine))}），由别的那一组填"
    value = op.get("value")
    if not isinstance(value, str) or not value.strip():
        return f"「{joint.label or joint_id}」要写一段非空的文字（{'SQL' if joint.kind == 'sql' else '指标式'}）"
    if len(value) > 20_000:
        return f"「{joint.label or joint_id}」太长了（超过 20000 字）"
    return None


class TemplateApplier:
    """一组生成关节的落地：fill_joint 把值写到图上那个位置（graph 是这一组自己的那一份，原地改），值记在 values 里。

    越界的照实交回（refused），自查时交回模型一次，交付时列给人看。"""

    def __init__(self, graph: dict[str, Any], joints: list[Any], group: list[Any]) -> None:
        self.graph = graph
        self.joints = {j.id: j for j in joints}
        self.group = list(group)
        self.values: dict[str, str] = {}
        self.plan: dict[str, Any] | None = None
        self.replied: dict[str, Any] | None = None
        self.explanation = ""
        self.refused: list[dict[str, Any]] = []
        self.effective: list[dict[str, Any]] = []
        self._reported = 0
        self._continuing = False
        self._repairing = False

    def apply(self, op: dict[str, Any]) -> Applied:
        from app.blueprints.instantiate import node_config, write_at

        kind = str(op.get("op") or "")
        if kind == "plan":
            if self.plan is not None and self._continuing:
                return Applied(False, False)
            self.plan = self.plan or op
            return Applied(False, True)
        if kind == "done":
            self.explanation = str(op.get("explanation", ""))
            return Applied(False, True)
        if kind == "reply":
            if self.replied is None and not self._repairing:
                self.replied = op
            return Applied(False, False)
        if problem := joint_problem(self.joints, self.group, op):
            self.refused.append({"op": kind, "joint": op.get("joint"), "message": problem})
            return Applied(False, False, problem)
        joint = self.joints[str(op["joint"])]
        value = str(op["value"]).strip()
        if self.values.get(joint.id) == value:
            return Applied(False, False)
        config = node_config(self.graph, joint.node_id)
        if config is None:
            return Applied(False, False, f"图上没有节点 {joint.node_id}")
        write_at(config, joint.field, value)
        self.values[joint.id] = value
        self.effective.append(op)
        return Applied(True, True)

    def landed(self) -> list[str]:
        return list(self.values)

    def continuing(self) -> None:
        self._continuing = True

    def repairing(self) -> None:
        self._repairing = True

    def unreported(self) -> list[dict[str, Any]]:
        out = self.refused[self._reported:]
        self._reported = len(self.refused)
        return out

    def refusals(self) -> list[dict[str, Any]]:
        return [{"code": JOINT_REFUSED, "level": "warning", "joint": r.get("joint"),
                 "message": f"助手想改的这一处没有采纳：{r['message']}"} for r in self.refused]


class TemplateChecker:
    """一组生成关节的自查：越界被拒的、还没填的、SQL 检查（GraphChecker：只读守卫、猜列、目录检查）、指标式白名单和
    静态追到原始数据（engine/lineage.static_stops）、骨架比对。只报这一组的节点上的问题：别的组在并行地填，它们的占位
    不归这一组管。"""

    def __init__(self, applier: TemplateApplier, *, skeleton: dict[str, Any], sources: list[Any] | None = None,
                 checkers: dict[str, Any] | None = None) -> None:
        self.applier = applier
        self.skeleton = skeleton
        self.sources = sources or []
        self.checkers = checkers

    def _nodes(self) -> dict[str, dict[str, Any]]:
        return {n["id"]: n for n in self.applier.graph.get("nodes") or [] if isinstance(n, dict) and n.get("id")}

    async def issues(self) -> list[dict[str, Any]]:
        from app.blueprints.instantiate import skeleton_changes

        out = [{"code": JOINT_REFUSED, "level": "error", "joint": r.get("joint"), "message": r["message"]}
               for r in self.applier.unreported()]
        out += [{"code": JOINT_UNFILLED, "level": "error", "joint": j.id, "node_id": j.node_id,
                 "message": f"「{j.label or j.id}」还没填：用 fill_joint 填上"}
                for j in self.applier.group if j.id not in self.applier.values]
        out += self._sql_issues()
        out += self._metric_issues()
        out += skeleton_changes(self.skeleton, self.applier.graph, list(self.applier.joints.values()))
        return out

    def _sql_issues(self) -> list[dict[str, Any]]:
        """这一组 SQL 关节所在的取数节点：只读守卫、再按 GraphChecker（和改图助手同一套：猜列、目录检查、画布校验）。"""
        from app.api.copilot_gen import GraphApplier, GraphChecker
        from app.data.guard import SqlRejected, check

        nodes = self._nodes()
        mine = {j.node_id for j in self.applier.group if j.kind == "sql" and j.id in self.applier.values}
        if not mine:
            return []
        out: list[dict[str, Any]] = []
        kinds = {getattr(s, "name", None): getattr(s, "kind", None) for s in self.sources}
        for node_id in sorted(mine):
            config = (nodes[node_id].get("data") or {}).get("config") or {}
            sql = str((config.get("args") or {}).get("sql") or "")
            source = str(config.get("tool") or "").removeprefix("db_query__")
            try:
                # 模板写法（{{ input.period_start }}）换成一个字面量再查：守卫按 SQL 本身判，不按渲染出来的值判
                check(re.sub(r"\{\{.*?\}\}", "0", sql, flags=re.S), readonly=True, source_name=source,
                      dialect=kinds.get(source))
            except SqlRejected as e:
                out.append({"code": SQL_REJECTED, "level": "error", "node_id": node_id, "field": "args.sql",
                            "message": f"取数的 SQL 过不了只读检查：{e}"})
        graph = GraphApplier(copy.deepcopy(nodes), copy.deepcopy(self.applier.graph.get("edges") or []))
        checker = GraphChecker(graph, sources=self.sources, checkers=self.checkers)
        out += [i for i in checker.blocking() or [] if i.get("node_id") in mine]
        return out

    def _metric_issues(self) -> list[dict[str, Any]]:
        """指标式：只许 cell(nodes.<这一组的取数节点>, 0, '列名') 取格加算术、比较；静态追得到原始数据；取的列在那条 SQL 里。"""
        import ast

        from app.engine.expressions import ExpressionError, cell_parts, leaf_refs, parse_expression
        from app.engine.lineage import static_stops

        nodes = self._nodes()
        fetches = {j.node_id for j in self.applier.group if j.kind == "sql"}
        flat = {nid: {"type": n.get("type"), "title": (n.get("data") or {}).get("label") or nid,
                      "config": (n.get("data") or {}).get("config") or {}} for nid, n in nodes.items()}
        out: list[dict[str, Any]] = []
        for j in self.applier.group:
            if j.kind != "metric_expr" or j.id not in self.applier.values:
                continue
            expr = self.applier.values[j.id]

            def bad(message: str, code: str = METRIC_EXPR) -> None:
                out.append({"code": code, "level": "error", "joint": j.id, "node_id": j.node_id,
                            "message": f"「{j.label or j.id}」{message}"})

            try:
                tree = parse_expression(expr)[0]
            except ExpressionError as e:
                bad(f"写得不对：{e}")
                continue
            calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if extra := sorted(calls - METRIC_FUNCS):
                bad(f"只能用 cell() 取格再做算术（可以用 {'、'.join(sorted(METRIC_FUNCS))}），不能用 {'、'.join(extra)}")
                continue
            refs = leaf_refs(tree)
            if not refs:
                bad("没有取任何数：要用 cell() 从取数的结果里取格")
                continue
            for path in refs:
                parts = cell_parts(path)
                node_id = parts[0][len("nodes."):] if parts and parts[0].startswith("nodes.") else ""
                if parts is None or node_id not in fetches:
                    bad(f"只能用 cell(nodes.{'/'.join(sorted(fetches))}, 0, '列名') 取这一组取数的格，不能写 {path}")
                    break
                if parts[1].strip() != "0":
                    bad("取数只出一行：行号写 0")
                    break
                column = parts[2].strip().strip("'\"")
                sql = str(((flat[node_id]["config"].get("args") or {}).get("sql")) or "")
                if not re.search(rf"(?<![\w]){re.escape(column)}(?![\w])", sql, re.I):
                    bad(f"取的列「{column}」在取数的 SQL 里找不到：SQL 要交出这一列（列名照写）", METRIC_COLUMN)
                    break
            else:
                if stops := static_stops(flat, expr):
                    bad(f"的输入没有追到原始数据（{'、'.join(s.path for s in stops[:3])}）：要用 cell() 取查询结果的格")
        return out

    def repair_request(self, issues: list[dict[str, Any]], looks_text: str) -> str:
        values = "\n".join(f"{jid}：{value}" for jid, value in self.applier.values.items()) or "（还没填）"
        return TEMPLATE_REPAIR_PROMPT.format(values=values, issues=_issue_lines(issues),
                                             looks=REPAIR_LOOKS_PROMPT.format(text=looks_text) if looks_text else "")
