"""题目的形状与校验（细案 01 做法 7、8、14；评测-D4、D12）。接口（app/api/evals.py）和导入（app/eval/transfer.py）都经
normalize_case + resolve_case，规矩只有这一份。

**题目**：kind（ask 问数据题 / workflow 钉版本的工作流题）、question（问法）、datasource_ids、workflow 题的
workflow_id@workflow_version 和 input_template、expectations（期望）、gold（期望答案的来源）、tags、enabled。
history 是多轮追问的位置：评测-D12 本轮只做单轮，非空一律拒收（多轮要和问数据页的历史拼法完全一致，前提是轮次由后端落库，
不在本轮）。

**期望**（expectations，每题至少一个，最多 EXPECTATIONS_MAX 个）。每个期望 {id, kind, required, label, …}：
- scalar 一个数：SQL 题写 column（结果里的列）加 row（第几行，缺省 0）或 where（按维度找那一行，如 {"地区": "华南"}）；
  口径题写 metric（口径卡上的指标 id）。tolerance 是允许的相对误差（不写由评分按显示精度推，第 3 波）。
- table 多行：keys（按哪几列配对，必填）、columns（比哪几列，缺省全部）、top_n、ordered（要不要按顺序）。
- set 名单：column、top_n、ordered。
- none 期望答「没有数据」：期望 SQL 返回 0 行。
- ask 期望反问（问法含糊）：不能和要答出数的期望写在一起，不要期望答案的来源。
- text 文字要点（points）：由裁判判，单列「模型判断」，不计入准确率（评测-D4），所以 required 恒为 false。
**数值期望不许人填**（评测-D4）：期望里写了 value、values、expected、answer、rows 这类键的一律拒收——期望值只能由 SQL、
口径或从运行认定的 SQL 算出来（app/eval/gold.py），人填的数会过期、会填错，也说不清是哪个口径。

**期望答案的来源**（gold；scalar、table、set、none 要它，ask、text 不要）：
- sql：{datasource_id, sql}。SQL 可以带 {{ schedule.* }}，按评测的截至时间渲染；保存前过只读守卫。
- caliber：{workflow_id, version, node_id, inputs}，钉住 workflow_id@version 的那张口径卡（只认可复用的卡，
  app/ledger/derive.load_pinned），inputs 是入口字段的模板（同定时任务的输入模板）。只配 scalar（metric）。
  upgrade 是口径升版的处置（服务端写，见 gold.dispose）：客户端带来的不算，保存时按库里的留着。
- from_run：{datasource_id, sql, run_id, query, node_id, confirmed_by, confirmed_at}，从某次运行里的一条查询认定
  （只能经 POST …/cases/from-run 建；之后每次评测按这条 SQL 重算，不冻结数值）。改 SQL 就改成 sql 题。

**题目指纹**（case_hash）：问法、数据源、工作流和输入、期望、期望来源（去掉确认人、出处运行、升版处置这些记录项）。改过
就变，和上一次比时标「题目改过，不可比」；口径改钉新版本（recompute）也就断开了历史。
"""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_eval import CASE_KINDS

#: 题目不合规（422）的机器码，field 是出错的那一项
CASE_INVALID = "eval_case_invalid"
#: 期望 SQL 没过只读守卫（422）
GOLD_GUARD = "eval_gold_guard"
#: 口径题指的卡不能当口径题（422）：不存在、不是口径卡、不能复用、取数节点引用了入口字段以外的值
CALIBER_NOT_REUSABLE = "eval_caliber_not_reusable"

EXPECTATION_KINDS = ("scalar", "table", "set", "none", "ask", "text")
#: 要期望答案来源的期望：数都由 SQL / 口径算
NEEDS_GOLD = ("scalar", "table", "set", "none")
#: 不计入准确率的（评测-D4：文字要点由裁判判，单列）
UNCOUNTED = ("text",)
GOLD_KINDS = ("sql", "caliber", "from_run")
#: 期望里出现这些键就是在人填数（评测-D4）
HUMAN_VALUE_KEYS = ("value", "values", "expected", "answer", "rows", "number")

#: 每种期望认的键（另有 id、kind、required、label）
_EXPECTATION_KEYS: dict[str, tuple[str, ...]] = {
    "scalar": ("column", "metric", "row", "where", "tolerance"),
    "table": ("keys", "columns", "top_n", "ordered", "tolerance"),
    "set": ("column", "top_n", "ordered"),
    "none": (),
    "ask": (),
    "text": ("points",),
}
_COMMON_KEYS = ("id", "kind", "required", "label")
#: 期望答案来源的键（客户端能写的）；from_run 只能由服务端建
_GOLD_KEYS: dict[str, tuple[str, ...]] = {
    "sql": ("datasource_id", "sql"),
    "caliber": ("workflow_id", "version", "node_id", "inputs"),
    "from_run": ("datasource_id", "sql", "run_id", "query", "node_id", "confirmed_by", "confirmed_at"),
}
#: 期望来源里不进指纹的记录项：谁认定的、出自哪次运行、升版怎么处置的（dual 只是多算一版，旧的那一版照旧可比）
_GOLD_RECORDS = ("confirmed_by", "confirmed_at", "run_id", "query", "node_id", "upgrade")
#: 进指纹的题目字段
HASHED_FIELDS = ("kind", "question", "datasource_ids", "workflow_id", "workflow_version", "input_template", "history",
                 "expectations", "gold")

QUESTION_MAX = 2000
SQL_MAX = 20_000
LABEL_MAX = 200
EXPECTATIONS_MAX = 20
POINTS_MAX = 10
POINT_MAX = 300
TAGS_MAX = 20
TAG_MAX = 40
DATASOURCES_MAX = 20
TOP_N_MAX = 1000
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class CaseInvalid(ValueError):
    """题目不合规。message 给人看，field 是出错的那一项（表单据此落到那一格），code 给程序认。"""

    def __init__(self, message: str, field: str | None = None, code: str = CASE_INVALID) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.code = code


# ==========================================================================
# 变量：{{ schedule.* }}，和定时任务同一份（scheduler/service.VARIABLES）
# ==========================================================================


def check_variables(where: str, text: str, field_name: str) -> None:
    """text 里的 {{ }} 只能是定时任务那份变量（可以带过滤器），成对出现。规矩同 scheduler/service.check_template，
    说法换成这里的「哪一项」。"""
    from app.engine.expressions import _FILTERS
    from app.scheduler import service

    for match in service._TEMPLATE_RE.finditer(text):
        expr = match.group(1).strip()
        if not expr:
            raise CaseInvalid(f"{where}里有空的 {{{{ }}}}，请在中间写变量名，例如 schedule.last_week_start", field_name)
        path, *filters = (part.strip() for part in expr.split("|"))
        if path not in service._NAMES:
            raise CaseInvalid(f"{where}里的「{path}」不是可用的变量，可用的变量：{'、'.join(service._NAMES)}", field_name)
        for filt in filters:
            if filt not in _FILTERS:
                raise CaseInvalid(f"{where}里的「{filt}」不是可用的过滤器，可用的过滤器：{'、'.join(_FILTERS)}", field_name)
    rest = service._TEMPLATE_RE.sub("", text)
    if "{{" in rest or "}}" in rest:
        raise CaseInvalid(f"{where}里 {{{{ 和 }}}} 没有成对出现，请检查是否少写了一半", field_name)


def variable_values(as_of: datetime, tz: str, cron_expr: str | None = None) -> dict[str, str]:
    """按截至时间 as_of 在时区 tz 算出的变量（键不带 schedule. 前缀）。定时发起的评测给那条定时的 cron（上一次计划触发
    时间按它往前找）；手动发起的没有 cron，按每天这一刻触发算（同变量清单接口的示例）。"""
    from app.scheduler import cron, service

    if not cron_expr:
        local = as_of.astimezone(cron.zone(tz))
        cron_expr = f"{local.minute} {local.hour} * * *"
    return service.template_values(as_of, tz, cron_expr)


def render(text: str, values: Mapping[str, str]) -> str:
    """按变量渲染一段文字（问法、期望 SQL、口径输入）。用项目通用的渲染器，变量挂在 schedule 下。"""
    from app.engine.expressions import render_template

    return render_template(text or "", {"schedule": dict(values)})


# ==========================================================================
# 逐项校验
# ==========================================================================


def _text(value: Any, where: str, field_name: str, *, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CaseInvalid(f"{where}需要是文字", field_name)
    text = value.strip()
    if required and not text:
        raise CaseInvalid(f"请填写{where}", field_name)
    if len(text) > limit:
        raise CaseInvalid(f"{where}不能超过 {limit} 个字符", field_name)
    return text


def _bool(value: Any, where: str, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CaseInvalid(f"{where}需要是 true 或 false", field_name)
    return value


def _int(value: Any, where: str, field_name: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CaseInvalid(f"{where}需要是 {low} 到 {high} 的整数", field_name)
    return value


def _names(value: Any, where: str, field_name: str, *, limit: int, required: bool = False) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise CaseInvalid(f"{where}需要是一组不为空的文字", field_name)
    out = list(dict.fromkeys(v.strip() for v in value))
    if required and not out:
        raise CaseInvalid(f"请填写{where}", field_name)
    if len(out) > limit:
        raise CaseInvalid(f"{where}最多 {limit} 项", field_name)
    return out


def _expectation(raw: Any, i: int, *, gold_kind: str | None, taken: set[str]) -> dict[str, Any]:
    where = f"第 {i + 1} 个期望"
    path = f"expectations.{i}"
    if not isinstance(raw, Mapping):
        raise CaseInvalid(f"{where}需要是一个对象", path)
    kind = raw.get("kind")
    if kind not in EXPECTATION_KINDS:
        raise CaseInvalid(f"{where}的种类只能是 {'、'.join(EXPECTATION_KINDS)}", f"{path}.kind")
    human = [k for k in HUMAN_VALUE_KEYS if k in raw]
    if human:
        raise CaseInvalid(f"{where}写了「{human[0]}」：数值期望不许人填，期望值只能由期望 SQL、口径或从运行认定的 SQL "
                          "算出来。请写清从期望答案的结果里取哪一格、哪一列或整张表", f"{path}.{human[0]}")
    unknown = sorted(set(raw) - set(_COMMON_KEYS) - set(_EXPECTATION_KEYS[kind]))
    if unknown:
        raise CaseInvalid(f"{where}（{kind}）不认识这些项：{'、'.join(unknown)}", f"{path}.{unknown[0]}")
    eid = raw.get("id")
    if eid in (None, ""):
        n = len(taken) + 1
        while f"e{n}" in taken:
            n += 1
        eid = f"e{n}"
    if not isinstance(eid, str) or not _ID_RE.match(eid):
        raise CaseInvalid(f"{where}的 id 只能是 1 到 40 个字母、数字、下划线或连字符", f"{path}.id")
    if eid in taken:
        raise CaseInvalid(f"{where}的 id「{eid}」和前面的重复了", f"{path}.id")
    out: dict[str, Any] = {"id": eid, "kind": kind}
    if (label := _text(raw.get("label"), f"{where}的说明", f"{path}.label", limit=LABEL_MAX)):
        out["label"] = label
    # 文字要点不计入准确率（评测-D4）：不算「必需」，通过与否不看它
    required = raw.get("required", True)
    out["required"] = False if kind in UNCOUNTED else _bool(required, f"{where}的「必需」", f"{path}.required")

    if kind in NEEDS_GOLD and gold_kind is None:
        raise CaseInvalid(f"{where}要答出数（或答「没有数据」），需要期望答案的来源：写一条期望 SQL、选一个口径，"
                          "或从运行里认定一条查询", "gold")
    if gold_kind == "caliber" and kind not in ("scalar", "ask", "text"):
        raise CaseInvalid(f"{where}（{kind}）不能用口径题算：口径卡给的是一个个指标，只配「一个数」的期望",
                          f"{path}.kind")
    if kind == "scalar":
        if gold_kind == "caliber":
            if raw.get("column") not in (None, "") or "row" in raw or "where" in raw:
                raise CaseInvalid(f"{where}是口径题：写 metric（口径卡上的指标 id），不写列、行", f"{path}.metric")
            out["metric"] = _text(raw.get("metric"), f"{where}的指标", f"{path}.metric", limit=LABEL_MAX,
                                  required=True)
        else:
            if raw.get("metric") not in (None, ""):
                raise CaseInvalid(f"{where}的期望答案来自 SQL：写 column（结果里的列），metric 只用于口径题",
                                  f"{path}.metric")
            out["column"] = _text(raw.get("column"), f"{where}取哪一列", f"{path}.column", limit=LABEL_MAX,
                                  required=True)
            if "where" in raw and raw["where"] not in (None, {}):
                cond = raw["where"]
                if not isinstance(cond, Mapping) or not cond or not all(
                        isinstance(k, str) and k.strip() and isinstance(v, (str, int, float)) and not isinstance(v, bool)
                        for k, v in cond.items()):
                    raise CaseInvalid(f"{where}的 where 需要是「列名：取值」的对象，例如 {{\"地区\": \"华南\"}}",
                                      f"{path}.where")
                if "row" in raw:
                    raise CaseInvalid(f"{where}按 where 找行时不写 row", f"{path}.row")
                out["where"] = {k.strip(): v for k, v in cond.items()}
            else:
                out["row"] = _int(raw.get("row", 0), f"{where}的行号", f"{path}.row", low=0, high=TOP_N_MAX)
        if raw.get("tolerance") is not None:
            out["tolerance"] = _tolerance(raw["tolerance"], where, path)
    elif kind == "table":
        out["keys"] = _names(raw.get("keys"), f"{where}按哪几列配对（keys）", f"{path}.keys", limit=10, required=True)
        if raw.get("columns") is not None:
            out["columns"] = _names(raw["columns"], f"{where}比哪几列", f"{path}.columns", limit=50, required=True)
        if raw.get("top_n") is not None:
            out["top_n"] = _int(raw["top_n"], f"{where}的前几名", f"{path}.top_n", low=1, high=TOP_N_MAX)
        out["ordered"] = _bool(raw.get("ordered", False), f"{where}要不要按顺序", f"{path}.ordered")
        if raw.get("tolerance") is not None:
            out["tolerance"] = _tolerance(raw["tolerance"], where, path)
    elif kind == "set":
        out["column"] = _text(raw.get("column"), f"{where}取哪一列", f"{path}.column", limit=LABEL_MAX, required=True)
        if raw.get("top_n") is not None:
            out["top_n"] = _int(raw["top_n"], f"{where}的前几名", f"{path}.top_n", low=1, high=TOP_N_MAX)
        out["ordered"] = _bool(raw.get("ordered", False), f"{where}要不要按顺序", f"{path}.ordered")
    elif kind == "text":
        points = raw.get("points")
        if not isinstance(points, list) or not points or not all(isinstance(p, str) and p.strip() for p in points):
            raise CaseInvalid(f"{where}是文字要点：points 需要是一组不为空的要点", f"{path}.points")
        if len(points) > POINTS_MAX or any(len(p.strip()) > POINT_MAX for p in points):
            raise CaseInvalid(f"{where}最多 {POINTS_MAX} 条要点，每条不超过 {POINT_MAX} 个字符", f"{path}.points")
        out["points"] = [p.strip() for p in points]
    return out


def _tolerance(value: Any, where: str, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
        raise CaseInvalid(f"{where}允许的相对误差需要是 0 到 1 之间的数（0.01 是 1%）", f"{path}.tolerance")
    return float(value)


def normalize_expectations(raw: Any, *, gold_kind: str | None, case_kind: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise CaseInvalid("每道题至少要有一个期望", "expectations")
    if len(raw) > EXPECTATIONS_MAX:
        raise CaseInvalid(f"每道题最多 {EXPECTATIONS_MAX} 个期望", "expectations")
    out: list[dict[str, Any]] = []
    taken: set[str] = set()
    for i, item in enumerate(raw):
        exp = _expectation(item, i, gold_kind=gold_kind, taken=taken)
        taken.add(exp["id"])
        out.append(exp)
    kinds = {e["kind"] for e in out}
    if "ask" in kinds and kinds & set(NEEDS_GOLD):
        raise CaseInvalid("期望反问的题不能同时期望答出数：问法含糊时，答案就不该给数", "expectations")
    if "ask" in kinds and case_kind == "workflow":
        raise CaseInvalid("工作流题不会反问，期望反问只用于问数据题", "expectations")
    if gold_kind is not None and not kinds & set(NEEDS_GOLD):
        raise CaseInvalid("有期望答案的来源，却没有要它算的期望：请加一个「一个数」「多行」「名单」或「没有数据」的期望，"
                          "或者去掉期望答案的来源", "gold")
    return out


def normalize_gold(raw: Any, *, current: Mapping[str, Any] | None = None, trusted: bool = False) -> dict[str, Any] | None:
    """期望答案的来源 → 规范化的形状。只看形状和变量，库里的引用（数据源、口径卡）由 resolve_case 核对。

    from_run 只能由服务端建（trusted），或者和库里的一样原样带回来；口径题的升版处置（upgrade）只认库里的。"""
    if raw in (None, {}):
        return None
    if not isinstance(raw, Mapping):
        raise CaseInvalid("期望答案的来源需要是一个对象", "gold")
    kind = raw.get("kind")
    if kind not in GOLD_KINDS:
        raise CaseInvalid(f"期望答案的来源只能是 SQL（sql）、口径（caliber）或从运行认定（from_run）", "gold.kind")
    allowed = set(_GOLD_KEYS[kind]) | {"kind", "upgrade"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise CaseInvalid(f"期望答案的来源不认识这些项：{'、'.join(unknown)}", f"gold.{unknown[0]}")
    if kind == "from_run" and not trusted:
        same = current is not None and current.get("kind") == "from_run" and all(
            raw.get(k) == current.get(k) for k in _GOLD_KEYS["from_run"])
        if not same:
            raise CaseInvalid("「从运行认定」的期望只能从一次运行里选一条查询认定（POST …/cases/from-run）。"
                              "要改 SQL，请把期望答案的来源改成 SQL", "gold.kind")
        return copy.deepcopy(dict(current))
    out: dict[str, Any] = {"kind": kind}
    if kind in ("sql", "from_run"):
        out["datasource_id"] = _text(raw.get("datasource_id"), "期望 SQL 查哪个数据源", "gold.datasource_id",
                                     limit=64, required=True)
        out["sql"] = _text(raw.get("sql"), "期望 SQL", "gold.sql", limit=SQL_MAX, required=True)
        check_variables("期望 SQL ", out["sql"], "gold.sql")
        if kind == "from_run":
            for key in ("run_id", "query", "node_id", "confirmed_by", "confirmed_at"):
                if raw.get(key) not in (None, ""):
                    out[key] = str(raw[key])
    else:
        out["workflow_id"] = _text(raw.get("workflow_id"), "口径所在的工作流", "gold.workflow_id", limit=64,
                                   required=True)
        out["version"] = _int(raw.get("version"), "口径固定的版本", "gold.version", low=1, high=10**9)
        out["node_id"] = _text(raw.get("node_id"), "口径卡", "gold.node_id", limit=64, required=True)
        inputs = raw.get("inputs") or {}
        if not isinstance(inputs, Mapping) or not all(isinstance(k, str) and k.strip() and isinstance(v, str)
                                                      for k, v in inputs.items()):
            raise CaseInvalid("口径的输入需要是「入口字段：模板」的对象，模板可以带 {{ schedule.* }} 变量", "gold.inputs")
        out["inputs"] = {k.strip(): v for k, v in inputs.items()}
        for name, text in out["inputs"].items():
            check_variables(f"口径输入「{name}」", text, "gold.inputs")
        # 升版处置是服务端写的（gold.dispose）：钉的还是同一张卡就照库里的留着，换了卡就没有了
        if current is not None and current.get("kind") == "caliber" and all(
                current.get(k) == out[k] for k in ("workflow_id", "version", "node_id")) and current.get("upgrade"):
            out["upgrade"] = copy.deepcopy(current["upgrade"])
    return out


def normalize_case(body: Mapping[str, Any], *, current: Mapping[str, Any] | None = None,
                   trusted_gold: bool = False) -> dict[str, Any]:
    """请求体（改的时候和库里的合起来：没带的项照旧）→ 题目各列的值。不合规抛 CaseInvalid。"""
    if not isinstance(body, Mapping):
        raise CaseInvalid("题目需要是一个对象")
    cur = dict(current or {})

    def pick(key: str, default: Any = None) -> Any:
        return body[key] if key in body else cur.get(key, default)

    kind = pick("kind", "ask")
    if kind not in CASE_KINDS:
        raise CaseInvalid("题目的种类只能是问数据题（ask）或工作流题（workflow）", "kind")
    history = pick("history")
    if history not in (None, []):
        raise CaseInvalid("本轮只做单轮题：多轮追问（history）还不支持，请留空", "history")
    question = _text(pick("question", ""), "问法", "question", limit=QUESTION_MAX, required=kind == "ask")
    check_variables("问法", question, "question")
    datasource_ids = _names(pick("datasource_ids", []), "限定的数据源", "datasource_ids", limit=DATASOURCES_MAX)
    workflow_id, workflow_version, input_template = None, None, {}
    if kind == "workflow":
        workflow_id = _text(pick("workflow_id"), "要评的工作流", "workflow_id", limit=64, required=True)
        workflow_version = _int(pick("workflow_version"), "工作流固定的版本", "workflow_version", low=1, high=10**9)
        from app.scheduler import service

        try:
            input_template = service.check_template(pick("input_template", {}) or {})
        except service.TemplateError as e:
            raise CaseInvalid(str(e), "input_template") from e
    else:
        for key in ("workflow_id", "workflow_version"):
            if body.get(key) not in (None, ""):
                raise CaseInvalid("问数据题不固定工作流：要评某个工作流的某一版，请把题目种类改成工作流题", key)
        if body.get("input_template"):
            raise CaseInvalid("问数据题没有输入模板，问法就是输入", "input_template")
    gold = normalize_gold(pick("gold"), current=cur.get("gold"), trusted=trusted_gold)
    expectations = normalize_expectations(pick("expectations"), gold_kind=gold["kind"] if gold else None,
                                          case_kind=kind)
    tags = _names(pick("tags", []), "标签", "tags", limit=TAGS_MAX)
    if any(len(t) > TAG_MAX for t in tags):
        raise CaseInvalid(f"每个标签不超过 {TAG_MAX} 个字符", "tags")
    enabled = _bool(pick("enabled", True), "是否启用", "enabled")
    return {"kind": kind, "question": question, "datasource_ids": datasource_ids, "workflow_id": workflow_id,
            "workflow_version": workflow_version, "input_template": input_template, "history": None,
            "expectations": expectations, "gold": gold, "tags": tags, "enabled": enabled}


def case_hash(fields: Mapping[str, Any]) -> str:
    """题目内容的指纹：HASHED_FIELDS，期望来源去掉记录项（_GOLD_RECORDS）。"""
    from app.core.artifact_store import canonical_json, content_hash

    body = {k: fields.get(k) for k in HASHED_FIELDS}
    if isinstance(body.get("gold"), Mapping):
        body["gold"] = {k: v for k, v in body["gold"].items() if k not in _GOLD_RECORDS}
    if not body.get("history"):
        body["history"] = None
    return content_hash(canonical_json(body))


def case_fields(row: Any) -> dict[str, Any]:
    """库里的一道题 → normalize_case 给出的那一组字段（再加 id、seq、suite_id）。gold.py 按这个形状读题目。"""
    return {"id": row.id, "suite_id": row.suite_id, "seq": row.seq, "kind": row.kind, "question": row.question,
            "datasource_ids": list(row.datasource_ids or []), "workflow_id": row.workflow_id,
            "workflow_version": row.workflow_version, "input_template": dict(row.input_template or {}),
            "history": row.history, "expectations": copy.deepcopy(list(row.expectations or [])),
            "gold": copy.deepcopy(row.gold) if row.gold else None, "tags": list(row.tags or []),
            "enabled": bool(row.enabled)}


# ==========================================================================
# 核对库里的引用
# ==========================================================================


@dataclass
class CaseRefs:
    """resolve_case 核对出来的引用：数据源 {id: 名字}、口径题读好的那一版（Pinned）。"""

    sources: dict[str, str] = field(default_factory=dict)
    pinned: Any = None


async def resolve_case(session: AsyncSession, fields: Mapping[str, Any], *, tz: str = "Asia/Shanghai",
                       now: datetime | None = None) -> CaseRefs:
    """核对题目引用的数据源、口径卡、工作流版本都在、能用；期望 SQL 按此刻渲染后先过一遍只读守卫（试算时按截至时间
    再过一遍）。不对抛 CaseInvalid。"""
    from app.data.guard import SqlRejected, check
    from app.db.base import utcnow
    from app.db.models import DataSource
    from app.ledger import derive

    refs = CaseRefs()
    ids = list(fields.get("datasource_ids") or [])
    gold = fields.get("gold") or None
    if gold and gold.get("datasource_id"):
        ids.append(gold["datasource_id"])
    for sid in dict.fromkeys(ids):
        source = await session.get(DataSource, sid)
        if source is None:
            where = "gold.datasource_id" if gold and sid == gold.get("datasource_id") else "datasource_ids"
            raise CaseInvalid(f"数据源 {sid} 不存在，可能已被删除", where)
        refs.sources[sid] = source.name
    if gold and gold["kind"] in ("sql", "from_run"):
        source = await session.get(DataSource, gold["datasource_id"])
        sample = render(gold["sql"], variable_values(now or utcnow(), tz))
        try:
            check(sample, readonly=True, source_name=source.name, dialect=(source.kind or "").lower())
        except SqlRejected as e:
            raise CaseInvalid(f"期望 SQL 没有通过只读守卫：{e}", "gold.sql", GOLD_GUARD) from e
    elif gold and gold["kind"] == "caliber":
        try:
            pinned = await derive.load_pinned(session, gold["workflow_id"], gold["version"], gold["node_id"])
        except derive.NotDerivable as e:
            raise CaseInvalid(f"这张口径卡不能当口径题：{e.message}", "gold", CALIBER_NOT_REUSABLE) from e
        if pinned.retired or not pinned.published:
            raise CaseInvalid(f"「{pinned.workflow_name}」v{pinned.version} "
                              f"{'已退役' if pinned.retired else '没有发布过'}：口径题只能固定已发布且未退役的版本",
                              "gold.version", CALIBER_NOT_REUSABLE)
        refs.pinned = pinned
        ids_ = {m["id"] for m in pinned.metrics}
        for i, exp in enumerate(fields.get("expectations") or []):
            if exp.get("kind") == "scalar" and exp.get("metric") not in ids_:
                raise CaseInvalid(f"第 {i + 1} 个期望的指标「{exp.get('metric')}」不在「{pinned.workflow_name}」"
                                  f"v{pinned.version} 的口径卡「{pinned.card_title}」上，这张卡的指标："
                                  f"{'、'.join(sorted(ids_)) or '（没有）'}", f"expectations.{i}.metric")
        _check_inputs(gold.get("inputs") or {}, pinned.entry_fields, pinned.workflow_name)
    if fields.get("kind") == "workflow":
        await _check_workflow(session, fields)
    return refs


def _check_inputs(inputs: Mapping[str, str], fields: Any, workflow_name: str) -> None:
    from app.scheduler import service

    declared = [{"name": f["name"].strip(), "required": bool(f.get("required")), "default": f.get("default")}
                for f in fields or [] if isinstance(f.get("name"), str) and f["name"].strip()]
    try:
        service.check_fields(dict(inputs), declared, workflow_name)
    except service.TemplateError as e:
        raise CaseInvalid(str(e), "gold.inputs") from e


async def _check_workflow(session: AsyncSession, fields: Mapping[str, Any]) -> None:
    from app.apps import versions as app_versions
    from app.db.models import Workflow
    from app.scheduler import service

    workflow = await session.get(Workflow, str(fields.get("workflow_id") or ""))
    if workflow is None:
        raise CaseInvalid("要评的工作流不存在，可能已被删除", "workflow_id")
    row = await app_versions.version_row(session, workflow.id, int(fields["workflow_version"]))
    if row is None:
        raise CaseInvalid(f"「{workflow.name}」没有 v{fields['workflow_version']} 这个版本", "workflow_version")
    if not app_versions.is_published(row, current=workflow.published_version):
        raise CaseInvalid(f"「{workflow.name}」v{row.version} 没有发布过或已退役：工作流题只能固定已发布且未退役的版本",
                          "workflow_version")
    try:
        service.check_fields(dict(fields.get("input_template") or {}), service.input_fields(row.graph), workflow.name)
    except service.TemplateError as e:
        raise CaseInvalid(str(e), "input_template") from e


__all__ = ["CALIBER_NOT_REUSABLE", "CASE_INVALID", "CaseInvalid", "CaseRefs", "EXPECTATION_KINDS", "GOLD_GUARD",
           "GOLD_KINDS", "HASHED_FIELDS", "HUMAN_VALUE_KEYS", "NEEDS_GOLD", "UNCOUNTED", "case_fields", "case_hash",
           "check_variables", "normalize_case", "normalize_expectations", "normalize_gold", "render", "resolve_case",
           "variable_values"]
