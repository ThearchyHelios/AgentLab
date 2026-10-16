"""bp_* 操作的确定性落地（N2、§3.2）。模型（BlueprintApplier）和用户（PATCH /api/blueprints/{id}）用同一组操作，
只是来源记成 assistant 或 user。每张卡片有白名单（例如 people[].policy.kind 只能是 any / all / quorum），越界的拒绝并照实交回。

路径是 JSON 指针，能写的位置逐项列在 _SETTERS 里：
- 卡片上的一项（Item）：写值（/purpose、/data/0/table、/people/1/due……），来源记成这次操作的来源，用户写的记「已确认」，
  助手写的记「待确认」，写空记「缺」；/…/why 写一句「为什么」；/…/status 只收用户的「确认」（值不变、来源不变）；
- 列表（/data、/people、/fields、/access/use……）：bp_add 追加（/data、/data/- ）或插到某个位置（/data/2），bp_remove 删一项；
  新加的一项里卡片字段写纯值，来源和状态按这次操作记；
- /asks：bp_ask 追加一个反问（只有助手问），/asks/<i>/answer 记用户的回答；
- 选模板只走 bp_template：记下模板和版本，按模板预置还没填的卡片（来源记「模板默认」、状态「待确认」）。

「谁能看」（access）和写回的「结果不明时谁去核实」（/writeback/reconciler）只收用户的操作：授权、派活给谁是人的决定，
助手提的没有「待确认」可挂（P2）。
落地前后都按 agentlab.blueprint/1 的形状核一遍（spec.parse_blueprint），不合形状的一律拒。
"""
from __future__ import annotations

import copy
import keyword
import re
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from app.blueprints.spec import BP_OPS, BpOp, parse_blueprint


class BpRefused(Exception):
    """操作被拒：message 是写给人看的一句话，code 是 bp.* 机器码。"""

    def __init__(self, message: str, code: str = "bp.refused") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


#: 操作的来源：用户在卡片上改的、助手提的
BP_SOURCES = ("user", "assistant")
#: 统计哪一期（入口字段和定时任务的输入模板按它取，blueprints/templates/_common.PERIODS）
PERIODS = ("today", "yesterday", "this_iso_week", "last_iso_week", "this_month", "last_month")
CADENCE_PRESETS = ("daily", "weekly", "monthly")
#: 「谁来做」里一个处理人的种类：人、组、岗位、发起人
WHO_KINDS = ("user", "group", "slot", "initiator")
POLICY_KINDS = ("any", "all", "quorum")
TIMEOUT_ACTIONS = ("remind", "escalate", "reject")
#: 金额线只许「字段 比较 常量」
THRESHOLD_OPS = (">", ">=", "<", "<=", "==", "!=")
#: 「谁能看」的三档和能授权的对象（写回的核实人也只能是人或组：绑到应用的核实岗位）
ACCESS_KINDS = ("use", "observe", "manage")
GRANTEE_KINDS = ("user", "group")
#: 写回卡片上的项
WRITEBACK_KEYS = ("capability", "mapping", "confirm", "reconciler")
#: 只收用户操作的位置（JSON 指针的前几段）和助手动它时交回的话（按位置的最后一段认）
USER_ONLY = (("access",), ("writeback", "reconciler"))
_USER_ONLY_TEXT = {
    "access": "「谁能看」要由你来定：授权是人的决定，助手不替你填",
    "reconciler": "「结果不明时谁去核实」要由你来定：派活给谁是人的决定，助手不替你选",
}
#: 入口字段里模板自己要用的名字（统计期），「填什么」不能占
RESERVED_FIELDS = frozenset({"period", "period_start", "period_end", "mode"})
#: 列表的上限：再多就不是普通人在卡片上能判断对错的颗粒度了
LIST_LIMITS = {"data": 20, "people": 6, "fields": 40, "use": 50, "observe": 50, "manage": 50}

#: 数据项、步骤的 id 进节点 id 和指标 id：只用小写字母、数字、下划线
_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
#: 卡片上的时限说法：「48 小时」「2 天」「1 周」「30 分钟」
_PLAIN_DURATION = re.compile(r"^\s*(\d{1,4})\s*(分钟|小时|个小时|天|日|周|星期)\s*$")
_DURATION_UNIT = {"分钟": ("T", "M"), "小时": ("T", "H"), "个小时": ("T", "H"), "天": ("", "D"), "日": ("", "D"),
                  "周": ("", "W"), "星期": ("", "W")}
#: 写回的参数映射：入口表单字段、指标、核对出来的那几行（核对写回）
_MAP_SOURCE = re.compile(r"^(form|metric)\.([^\s.]{1,64})$")
_MAP_ROWS = "rows"

_ITEM_FIELDS = {
    "data": ("label", "unit", "table", "measure", "date", "filter", "grain"),
    "people": ("label", "who", "policy", "due", "on_timeout", "threshold"),
}
_LABEL = {"purpose": "目的", "template": "场景", "data": "看哪些数", "cadence": "多久更新", "people": "谁来做",
          "fields": "填什么", "writeback": "写回哪里", "access": "谁能看", "asks": "反问"}


# ==========================================================================
# JSON 指针
# ==========================================================================


def parse_pointer(path: Any) -> list[str]:
    """/data/0/table → ["data", "0", "table"]。不是 JSON 指针的抛 BpRefused。"""
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise BpRefused(f"位置要写成 /purpose、/data/0/table 这样的路径，当前为「{path}」")
    return [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]


def _index(token: str, size: int, *, allow_end: bool = False) -> int:
    if allow_end and token == "-":
        return size
    if not token.isdigit():
        raise BpRefused(f"列表的位置要写成数字，当前为「{token}」")
    at = int(token)
    if at > size or (at == size and not allow_end):
        raise BpRefused(f"位置 {at} 超出了列表的范围（现有 {size} 项）")
    return at


# ==========================================================================
# 各项的白名单（值 → 规范化后的值；不合规抛 BpRefused）
# ==========================================================================


def _text(limit: int, what: str) -> Callable[[Any, dict[str, Any]], Any]:
    def check(value: Any, _spec: dict[str, Any]) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise BpRefused(f"「{what}」要写一段文字")
        if len(value.strip()) > limit:
            raise BpRefused(f"「{what}」最多 {limit} 个字")
        return value.strip()

    return check


def _identifier(name: Any) -> bool:
    return isinstance(name, str) and 0 < len(name) <= 40 and name.isidentifier() and not keyword.iskeyword(name)


def _table(value: Any, _spec: dict[str, Any]) -> Any:
    if not isinstance(value, dict) or set(value) - {"source", "table"}:
        raise BpRefused("对到的表要写明数据源和表名两项")
    out = {k: str(value.get(k) or "").strip() for k in ("source", "table")}
    if out["source"] == "@records":
        # 本应用的记录表（填报登记，blueprints/store.SELF_RECORDS）只有一张，表名固定，用户和模型都不用写
        out["table"] = "records"
    if not all(out.values()) or any(len(v) > 200 for v in out.values()):
        raise BpRefused("对到的表要写明数据源和表名")
    return out


def _date(value: Any, _spec: dict[str, Any]) -> Any:
    if not isinstance(value, dict) or set(value) - {"column", "rule"} or not str(value.get("column") or "").strip():
        raise BpRefused("业务日期要写明按哪一列算，可以加一句规则（例如按支付日）")
    out = {"column": str(value["column"]).strip()[:200]}
    if value.get("rule") not in (None, ""):
        out["rule"] = _text(60, "业务日期的规则")(value["rule"], _spec)
    return out


def _cadence(value: Any, _spec: dict[str, Any]) -> Any:
    from app.scheduler.cron import TimezoneError, timezone_name

    if not isinstance(value, dict) or set(value) - {"preset", "weekday", "day", "time", "tz", "period"}:
        raise BpRefused("「多久更新」要写明每天、每周还是每月，几点，哪个时区，统计哪一期")
    preset = value.get("preset")
    if preset not in CADENCE_PRESETS:
        raise BpRefused("「多久更新」只能是每天、每周或每月")
    time = str(value.get("time") or "")
    if not _TIME.match(time):
        raise BpRefused(f"更新的时间要写成 09:00 这样的 24 小时制，当前为「{time}」")
    try:
        tz = timezone_name(str(value.get("tz") or ""))
    except TimezoneError as e:
        raise BpRefused(str(e)) from e
    out: dict[str, Any] = {"preset": preset, "time": time, "tz": tz}
    if preset == "weekly":
        weekday = value.get("weekday")
        if isinstance(weekday, bool) or not isinstance(weekday, int) or not 1 <= weekday <= 7:
            raise BpRefused("每周更新要写明周几（1 是周一，7 是周日）")
        out["weekday"] = weekday
    if preset == "monthly":
        day = value.get("day")
        # 只收 1–28：29 日以后不是每个月都有，那几个月就不出数了
        if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 28:
            raise BpRefused("每月更新要写明几号（1 到 28 号，29 号以后不是每个月都有）")
        out["day"] = day
    period = value.get("period")
    if period not in (None, ""):
        if period not in PERIODS:
            raise BpRefused("统计哪一期只能是今天、昨天、本周、上周、本月或上月")
        out["period"] = period
    return out


def _principal(value: Any, kinds: tuple[str, ...], what: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - {"kind", "name"} or value.get("kind") not in kinds:
        raise BpRefused(f"{what}要写明种类和名字，种类只能是{_kinds_text(kinds)}")
    name = str(value.get("name") or "").strip()
    if value["kind"] != "initiator" and not name:
        raise BpRefused(f"{what}要写明是谁")
    if len(name) > 100:
        raise BpRefused(f"{what}的名字最多 100 个字")
    return {"kind": value["kind"], "name": name}


_KIND_WORDS = {"user": "人", "group": "组", "slot": "岗位", "initiator": "发起人"}


def _kinds_text(kinds: tuple[str, ...]) -> str:
    words = [_KIND_WORDS[k] for k in kinds]
    return "、".join(words[:-1]) + f"或{words[-1]}" if len(words) > 1 else words[0]


def _who(value: Any, _spec: dict[str, Any]) -> Any:
    if not isinstance(value, list) or len(value) > 20:
        raise BpRefused("处理人要写成一组人、组或岗位（最多 20 个）")
    out = [_principal(v, WHO_KINDS, "处理人") for v in value]
    if any(p["kind"] == "initiator" for p in out) and len(out) > 1:
        raise BpRefused("处理人选了发起人时，不能再加别的人")
    # 岗位是一个整体（绑到哪些人在应用里定）：和具体的人、组混在一起，就说不清这一步派给岗位还是派给这几个人
    if any(p["kind"] == "slot" for p in out) and len(out) > 1:
        raise BpRefused("处理人选了岗位时，不能再加别的人、组或岗位")
    return out or None


def _policy(value: Any, _spec: dict[str, Any]) -> Any:
    if isinstance(value, str):
        value = {"kind": value}
    if not isinstance(value, dict) or set(value) - {"kind", "m"} or value.get("kind") not in POLICY_KINDS:
        raise BpRefused("几个人同意算通过，只能是任一人、全体同意或其中若干人同意")
    if value["kind"] != "quorum":
        if "m" in value:
            raise BpRefused("只有「若干人同意」要写人数")
        return {"kind": value["kind"]}
    m = value.get("m")
    if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 50:
        raise BpRefused("「若干人同意」要写明几个人（1 到 50）")
    return {"kind": "quorum", "m": m}


def duration_of(value: Any) -> str | None:
    """卡片上的时限 → ISO 8601 时长：「48 小时」→ PT48H，「2 天」→ P2D，ISO 写法原样（大写）。换算不了返回 None。"""
    from app.engine.lint_process import parse_duration

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if m := _PLAIN_DURATION.match(text):
        prefix, unit = _DURATION_UNIT[m.group(2)]
        text = f"P{prefix}{int(m.group(1))}{unit}"
    text = text.upper()
    return text if parse_duration(text) is not None else None


def _due(value: Any, _spec: dict[str, Any]) -> Any:
    iso = duration_of(value)
    if iso is None:
        raise BpRefused(f"时限要写成「48 小时」「2 天」「1 周」这样的时长，当前为「{value}」（工作日换算不了，请写成天数）")
    return iso


def _on_timeout(value: Any, _spec: dict[str, Any]) -> Any:
    if not isinstance(value, dict) or set(value) - {"action", "to", "every"} or value.get("action") not in TIMEOUT_ACTIONS:
        raise BpRefused("超时以后只能提醒、升级给别人或驳回（不会自动同意）")
    out: dict[str, Any] = {"action": value["action"]}
    if value["action"] == "escalate":
        to = value.get("to")
        if not isinstance(to, list) or not to:
            raise BpRefused("超时升级要写明升级给谁")
        out["to"] = [_principal(v, ("user", "group", "slot"), "升级给的人") for v in to[:20]]
    elif value.get("to"):
        raise BpRefused("只有超时升级要写升级给谁")
    if value.get("every") not in (None, ""):
        if value["action"] != "remind":
            raise BpRefused("只有超时提醒能写提醒间隔")
        every = duration_of(value["every"])
        if every is None:
            raise BpRefused(f"提醒间隔要写成「1 小时」这样的时长，当前为「{value['every']}」")
        out["every"] = every
    return out


def _threshold(value: Any, spec: dict[str, Any]) -> Any:
    if not isinstance(value, dict) or set(value) != {"field", "op", "value"}:
        raise BpRefused("走这一步的条件只能写成「字段 比较 常量」，例如金额大于 5000")
    field, op, const = value["field"], value["op"], value["value"]
    if not _identifier(field):
        raise BpRefused(f"条件里的字段要写字段名（不能写算式），当前为「{field}」")
    if op not in THRESHOLD_OPS:
        raise BpRefused(f"条件只能用这几种比较：{'、'.join(THRESHOLD_OPS)}")
    if isinstance(const, bool) or not isinstance(const, (int, float, str)) or (isinstance(const, str) and len(const) > 100):
        raise BpRefused("条件里比较的只能是一个数或一段文字，不能写算式")
    if isinstance(const, str) and op not in ("==", "!="):
        raise BpRefused("和文字只能比较相等或不相等")
    names = [f.get("name") for f in spec.get("fields") or [] if isinstance(f, dict)]
    if names and field not in names:
        raise BpRefused(f"条件用到的字段「{field}」不在「填什么」里")
    return {"field": field, "op": op, "value": const}


def _field_spec(value: Any, spec: dict[str, Any], name: str = "x") -> Any:
    """「填什么」的一个字段：agentlab.form/1 的一个属性，另可写 required（是否必填）。按表单同一份规则核（P5）。"""
    from app.apps.forms import validate_form_schema

    if not isinstance(value, dict):
        raise BpRefused("字段要写明类型、名称这些表单属性")
    prop = {k: v for k, v in value.items() if k != "required"}
    if "required" in value and not isinstance(value["required"], bool):
        raise BpRefused("「必填」只能是是或否")
    problems = validate_form_schema({"type": "object", "properties": {name: prop}})
    if problems:
        raise BpRefused(f"字段不符合表单的写法：{problems[0]}")
    return copy.deepcopy(value)


def _capability(value: Any, _spec: dict[str, Any]) -> Any:
    """写回的目标：能力登记里效果为写入的能力（写外部系统只经调用工具节点，总方案 A5）。读进程内快照，不查库。"""
    from app.capabilities.registry import cached_view, writes_externally

    name = str(value or "").strip()
    if not name:
        raise BpRefused("写回要选一项能力")
    view = cached_view(name)
    if view is None or view.get("missing"):
        raise BpRefused(f"能力登记里没有「{name}」，请从能力登记里选一项写入能力")
    if not writes_externally(name, view.get("effect"), view.get("endpoint_id")) and view.get("effect") != "write":
        raise BpRefused(f"「{name}」不是写入能力（它是只读的或在登记里没有定为写入），不能用来写回")
    return name


def _mapping(value: Any, _spec: dict[str, Any]) -> Any:
    if not isinstance(value, dict) or not value or len(value) > 40:
        raise BpRefused("字段对应要写成「参数：取值」的一组")
    out: dict[str, Any] = {}
    for param, source in value.items():
        if not isinstance(param, str) or not param.strip() or len(param) > 64:
            raise BpRefused("字段对应里的参数名要写清楚")
        if isinstance(source, bool) or isinstance(source, (int, float)):
            out[param] = source
        elif isinstance(source, str) and (_MAP_SOURCE.match(source) or source == _MAP_ROWS):
            out[param] = source
        elif isinstance(source, str) and len(source) >= 2 and source[0] == source[-1] == "'":
            out[param] = source
        else:
            raise BpRefused(f"参数「{param}」的取值只能是表单字段、指标、核对出来的行或一个常量（文字要加单引号），"
                            f"当前为「{source}」")
    return out


def _confirm(value: Any, _spec: dict[str, Any]) -> Any:
    if not isinstance(value, bool):
        raise BpRefused("要不要人确认只能是是或否")
    return value


def _reuse(value: Any, _spec: dict[str, Any]) -> Any:
    """复用已发布的口径卡：{workflow_id, version, node_id, metric_id}，另可带 inputs（那张卡读哪几个取数节点的哪几列，
    look/calibers 认出来写上，蓝图存的时候按库里那一版再核一遍）。"""
    if value is None:
        return None
    keys = {"workflow_id", "version", "node_id", "metric_id"}
    if not isinstance(value, dict) or not keys <= set(value) or set(value) - keys - {"inputs"}:
        raise BpRefused("复用口径卡要写明工作流、版本、口径卡节点和指标")
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise BpRefused("复用口径卡要写明具体的版本号（1、2…）")
    out = {k: str(value[k]).strip() for k in ("workflow_id", "node_id", "metric_id")}
    if not all(out.values()):
        raise BpRefused("复用口径卡要写明工作流、版本、口径卡节点和指标")
    out["version"] = version
    inputs = value.get("inputs")
    if inputs is not None:
        if not isinstance(inputs, list) or not all(
                isinstance(i, dict) and isinstance(i.get("node"), str) and isinstance(i.get("tool"), str)
                and isinstance(i.get("columns"), list) for i in inputs):
            raise BpRefused("复用口径卡的输入要逐项写明节点、工具和列")
        out["inputs"] = copy.deepcopy(inputs)
    return out


def _grantees(value: Any, _spec: dict[str, Any]) -> Any:
    if not isinstance(value, list):
        raise BpRefused("「谁能看」要写成一组人或组")
    return [_principal(v, GRANTEE_KINDS, "授权的对象") for v in value]


def _reconciler(value: Any, _spec: dict[str, Any]) -> Any:
    """结果不明时谁去核实：一组人或组（绑到应用的核实岗位，核实待办派给他们）。"""
    if not isinstance(value, list) or len(value) > 20:
        raise BpRefused("核实的人要写成一组人或组（最多 20 个）")
    out: list[dict[str, str]] = []
    for v in value:
        p = _principal(v, GRANTEE_KINDS, "核实的人")
        if p not in out:
            out.append(p)
    return out or None


#: 各项的检查。键是 (卡片, 项)；卡片本身就是一项的写 (卡片, None)
_CHECKS: dict[tuple[str, str | None], Callable[[Any, dict[str, Any]], Any]] = {
    ("purpose", None): _text(500, "目的"),
    ("cadence", None): _cadence,
    ("data", "label"): _text(60, "名称"),
    ("data", "unit"): _text(20, "单位"),
    ("data", "table"): _table,
    ("data", "measure"): _text(300, "怎么算"),
    ("data", "date"): _date,
    ("data", "filter"): _text(300, "筛选条件"),
    ("data", "grain"): _text(60, "粒度"),
    ("people", "label"): _text(60, "这一步叫什么"),
    ("people", "who"): _who,
    ("people", "policy"): _policy,
    ("people", "due"): _due,
    ("people", "on_timeout"): _on_timeout,
    ("people", "threshold"): _threshold,
    ("writeback", "capability"): _capability,
    ("writeback", "mapping"): _mapping,
    ("writeback", "confirm"): _confirm,
    ("writeback", "reconciler"): _reconciler,
}


# ==========================================================================
# 落地
# ==========================================================================


def _item(value: Any, source: str, *, why: Any = None) -> dict[str, Any]:
    """卡片上的一项：写空的记「缺」，用户写的记「已确认」，助手写的记「待确认」。"""
    if value is None:
        return {"value": None, "source": source, "status": "missing", "why": why}
    return {"value": value, "source": source, "status": "confirmed" if source == "user" else "proposed", "why": why}


def _checked(card: str, key: str | None, value: Any, spec: dict[str, Any]) -> Any:
    if value is None or value == "" or value == []:
        return None
    return _CHECKS[(card, key)](value, spec)


def _new_entry(card: str, raw: Any, source: str, spec: dict[str, Any]) -> dict[str, Any]:
    """bp_add 加的一项：数据项、步骤、字段。卡片字段写纯值，来源和状态按这次操作记。"""
    if not isinstance(raw, dict):
        raise BpRefused(f"「{_LABEL[card]}」加的一项要写成一个对象")
    existing = spec.get(card) or []
    if card == "fields":
        name = raw.get("name")
        if not _identifier(name):
            raise BpRefused(f"字段名要写成字母、汉字、数字和下划线（不以数字开头），当前为「{name}」")
        if name in RESERVED_FIELDS:
            raise BpRefused(f"字段名「{name}」留给统计期用了，请换一个")
        if any(f.get("name") == name for f in existing):
            raise BpRefused(f"已经有一个叫「{name}」的字段了")
        if set(raw) - {"name", "spec", "sensitive"}:
            raise BpRefused("字段只能写名字、表单属性和是否敏感")
        sensitive = raw.get("sensitive", False)
        if not isinstance(sensitive, bool):
            raise BpRefused("「敏感」只能是是或否")
        prop = None if raw.get("spec") is None else _field_spec(raw["spec"], spec, name)
        return {"name": name, "spec": _item(prop, source), "sensitive": sensitive}
    entry_id = raw.get("id")
    if not isinstance(entry_id, str) or not _ID.match(entry_id):
        raise BpRefused(f"每一项要有一个 id（小写字母开头，只用小写字母、数字、下划线），当前为「{entry_id}」")
    if any(e.get("id") == entry_id for e in existing):
        raise BpRefused(f"「{_LABEL[card]}」里已经有 id 为「{entry_id}」的一项了")
    allowed = {"id", *(_ITEM_FIELDS[card]), *(("reuse",) if card == "data" else ())}
    if extra := sorted(set(raw) - allowed):
        raise BpRefused(f"「{_LABEL[card]}」的一项不能写这些：{'、'.join(extra)}")
    out: dict[str, Any] = {"id": entry_id}
    for key in _ITEM_FIELDS[card]:
        out[key] = _item(_checked(card, key, raw.get(key), spec), source)
    if card == "data":
        out["reuse"] = _reuse(raw.get("reuse"), spec)
    return out


def _writeback(raw: Any, source: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    """整张写回卡片换掉。核实人只由用户定：助手写了要拒；没写的沿用卡片上原来的（助手改目标能力、字段对应时，不能把
    用户选好的人冲掉）。"""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - set(WRITEBACK_KEYS):
        raise BpRefused("「写回哪里」只能写目标能力、字段对应、要不要人确认和结果不明时谁去核实")
    if "reconciler" in raw and source != "user":
        raise BpRefused(_USER_ONLY_TEXT["reconciler"])
    out = {key: _item(_checked("writeback", key, raw.get(key), spec), source)
           for key in ("capability", "mapping", "confirm")}
    if "reconciler" in raw:
        out["reconciler"] = _item(_checked("writeback", "reconciler", raw["reconciler"], spec), source)
    else:
        before = (spec.get("writeback") or {}).get("reconciler")
        out["reconciler"] = copy.deepcopy(before) if isinstance(before, dict) else _item(None, "template")
    return out


def _set_item_part(item: dict[str, Any], part: str, value: Any, source: str) -> None:
    """一项里的 status（只收用户的确认）、why（一句为什么）。"""
    if part == "why":
        if value is not None and (not isinstance(value, str) or len(value) > 300):
            raise BpRefused("「为什么」要写一句话（最多 300 字）")
        item["why"] = value.strip() if isinstance(value, str) and value.strip() else None
        return
    if part != "status":
        raise BpRefused(f"卡片上的一项只能改值、确认或写一句为什么，不能改「{part}」")
    if source != "user":
        raise BpRefused("确认只能由你来做：助手提的项要你看过才算数")
    if value != "confirmed":
        raise BpRefused("只能把一项标成已确认；要改就直接改它的值")
    if item.get("value") is None:
        raise BpRefused("这一项还没有值，没法确认")
    item["status"] = "confirmed"


def _container(spec: dict[str, Any], tokens: list[str]) -> tuple[dict[str, Any], str]:
    """卡片上的一项所在的对象和键：/purpose → (spec, "purpose")，/data/0/table → (spec.data[0], "table")。"""
    card = tokens[0]
    if card in ("purpose", "cadence") and len(tokens) == 1:
        return spec, card
    if card in ("data", "people") and len(tokens) == 3:
        entries = spec.get(card) or []
        at = _index(tokens[1], len(entries))
        if tokens[2] not in _ITEM_FIELDS[card]:
            raise BpRefused(f"「{_LABEL[card]}」的一项没有「{tokens[2]}」")
        return entries[at], tokens[2]
    if card == "fields" and len(tokens) == 3 and tokens[2] == "spec":
        entries = spec.get("fields") or []
        return entries[_index(tokens[1], len(entries))], "spec"
    if card == "writeback" and len(tokens) == 2 and tokens[1] in WRITEBACK_KEYS:
        if spec.get("writeback") is None:
            spec["writeback"] = {k: _item(None, "template") for k in WRITEBACK_KEYS}
        return spec["writeback"], tokens[1]
    raise BpRefused(f"不能改这个位置：/{'/'.join(tokens)}")


def is_freeform(spec: dict[str, Any]) -> bool:
    from app.blueprints.templates._common import is_freeform as imports_graph

    return imports_graph(spec)


#: 自由形的「谁来做」只定由谁做（05-已知遗留 (d)、遗留-Q5）
FREEFORM_PEOPLE_ONLY_WHO = "自由形蓝图的「谁来做」只定每一步由谁做；会签、时限、超时、加减步骤在画布上改"
FREEFORM_PEOPLE_USER = "「谁来做」要由你来定：派活给谁是人的决定，助手不替你选"


def _freeform_who(spec: dict[str, Any], tokens: list[str], value: Any, source: str, graph: Any) -> None:
    """自由形的 /people/<i>/who：i 是按导入的图列出的第几个岗位（freeform.people_rows 的岗位行，界面照那一份的 path 写），
    绑定存进 spec.people[]（id 就是岗位名、只有 who）。只收人和组：岗位、发起人在图上已经写定了。"""
    from app.blueprints.templates import freeform
    from app.blueprints.templates._common import with_graph

    if len(tokens) != 3 or tokens[2] != "who":
        raise BpRefused(FREEFORM_PEOPLE_ONLY_WHO, code="bp.template_mismatch")
    if source != "user":
        raise BpRefused(FREEFORM_PEOPLE_USER)
    if graph is None:
        raise BpRefused("这张蓝图还没有导入的图，没法按图定谁来做")
    rows = [r for r in freeform.people_rows(with_graph(spec, graph)) if r["kind"] == "slot"]
    slot = rows[_index(tokens[1], len(rows))]["slot"]
    checked = _checked("people", "who", value, spec)
    if checked and any(p["kind"] not in ("user", "group") for p in checked):
        raise BpRefused("这里只选人或组：岗位、发起人在图上已经定了")
    entries = spec.setdefault("people", [])
    entry = next((e for e in entries if e.get("id") == slot), None)
    if entry is None:
        entry = {"id": slot}
        entries.append(entry)
    entry["who"] = _item(checked, source)


def _set(spec: dict[str, Any], tokens: list[str], value: Any, source: str, graph: Any = None) -> None:
    card = tokens[0]
    if card == "people" and is_freeform(spec):
        _freeform_who(spec, tokens, value, source, graph)
        return
    if card == "access":
        if len(tokens) != 2 or tokens[1] not in ACCESS_KINDS:
            raise BpRefused("「谁能看」只有使用、查看全部、管理三档")
        grantees = _grantees(value if value is not None else [], spec)
        if len(grantees) > LIST_LIMITS[tokens[1]]:
            raise BpRefused(f"每一档最多授权 {LIST_LIMITS[tokens[1]]} 个人或组")
        spec["access"][tokens[1]] = grantees
        return
    if card == "asks":
        if len(tokens) != 3 or tokens[2] != "answer":
            raise BpRefused("反问只能记下回答")
        asks = spec.get("asks") or []
        ask = asks[_index(tokens[1], len(asks))]
        if source != "user":
            raise BpRefused("反问要由你来回答")
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise BpRefused("回答要写一句话（最多 500 字）")
        ask["answer"] = value.strip()
        return
    if card == "writeback" and len(tokens) == 1:
        spec["writeback"] = _writeback(value, source, spec)
        return
    if card == "data" and len(tokens) == 3 and tokens[2] == "reuse":
        entries = spec.get("data") or []
        entries[_index(tokens[1], len(entries))]["reuse"] = _reuse(value, spec)
        return
    if card == "fields" and len(tokens) == 3 and tokens[2] in ("name", "sensitive"):
        entries = spec.get("fields") or []
        entry = entries[_index(tokens[1], len(entries))]
        if tokens[2] == "sensitive":
            if not isinstance(value, bool):
                raise BpRefused("「敏感」只能是是或否")
            entry["sensitive"] = value
            return
        if not _identifier(value) or value in RESERVED_FIELDS:
            raise BpRefused(f"字段名「{value}」不能用：要写成字母、汉字、数字和下划线，也不能和统计期重名")
        if any(f is not entry and f.get("name") == value for f in entries):
            raise BpRefused(f"已经有一个叫「{value}」的字段了")
        entry["name"] = value
        return
    if tokens[-1] in ("status", "why") and len(tokens) >= 2:
        holder, key = _container(spec, tokens[:-1])
        _set_item_part(holder[key], tokens[-1], value, source)
        return
    holder, key = _container(spec, tokens)
    if card == "fields":
        checked = None if value is None else _field_spec(value, spec, holder.get("name") or "x")
    else:
        checked = _checked(card, None if card in ("purpose", "cadence") else key, value, spec)
    why = (holder.get(key) or {}).get("why") if checked is not None else None
    holder[key] = _item(checked, source, why=why)


def _add(spec: dict[str, Any], tokens: list[str], value: Any, source: str) -> None:
    card = tokens[0]
    if card == "people" and is_freeform(spec):
        raise BpRefused(FREEFORM_PEOPLE_ONLY_WHO, code="bp.template_mismatch")
    if card == "access":
        if len(tokens) not in (2, 3) or tokens[1] not in ACCESS_KINDS:
            raise BpRefused("「谁能看」只有使用、查看全部、管理三档")
        grantees = spec["access"][tokens[1]]
        at = _index(tokens[2], len(grantees), allow_end=True) if len(tokens) == 3 else len(grantees)
        grantee = _principal(value, GRANTEE_KINDS, "授权的对象")
        if grantee in grantees:
            raise BpRefused(f"「{grantee['name']}」已经在这一档里了")
        if len(grantees) >= LIST_LIMITS[tokens[1]]:
            raise BpRefused(f"每一档最多授权 {LIST_LIMITS[tokens[1]]} 个人或组")
        grantees.insert(at, grantee)
        return
    if card not in ("data", "people", "fields") or len(tokens) > 2:
        raise BpRefused(f"只能往「看哪些数」「谁来做」「填什么」「谁能看」里加，不能加到 /{'/'.join(tokens)}")
    entries = spec.setdefault(card, [])
    at = _index(tokens[1], len(entries), allow_end=True) if len(tokens) == 2 else len(entries)
    if len(entries) >= LIST_LIMITS[card]:
        raise BpRefused(f"「{_LABEL[card]}」最多 {LIST_LIMITS[card]} 项")
    entries.insert(at, _new_entry(card, value, source, spec))


def _remove(spec: dict[str, Any], tokens: list[str], source: str, graph: Any = None) -> None:
    card = tokens[0]
    if card == "people" and is_freeform(spec):
        if len(tokens) != 3:
            raise BpRefused(FREEFORM_PEOPLE_ONLY_WHO, code="bp.template_mismatch")
        _freeform_who(spec, tokens, None, source, graph)
        return
    if card == "access":
        if len(tokens) != 3 or tokens[1] not in ACCESS_KINDS:
            raise BpRefused("「谁能看」要写明删哪一档的第几个")
        grantees = spec["access"][tokens[1]]
        del grantees[_index(tokens[2], len(grantees))]
        return
    if card in ("data", "people", "fields") and len(tokens) == 2:
        entries = spec.get(card) or []
        del entries[_index(tokens[1], len(entries))]
        return
    if card == "writeback" and len(tokens) == 1:
        spec["writeback"] = None
        return
    if card == "data" and len(tokens) == 3 and tokens[2] == "reuse":
        entries = spec.get("data") or []
        entries[_index(tokens[1], len(entries))]["reuse"] = None
        return
    # 卡片上的一项：清空等于记成「缺」
    _set(spec, tokens, None, source)


def _ask(spec: dict[str, Any], op: BpOp, source: str) -> None:
    if source != "assistant":
        raise BpRefused("反问是助手问你的，你直接回答就好")
    question = (op.question or "").strip()
    if not question or len(question) > 300:
        raise BpRefused("反问要写一个问题（最多 300 字）")
    options = [o.strip() for o in op.options if isinstance(o, str) and o.strip()]
    if len(options) > 6 or any(len(o) > 100 for o in options):
        raise BpRefused("反问最多给 6 个候选，每个最多 100 字")
    if op.path is not None:
        parse_pointer(op.path)
    asks = spec.setdefault("asks", [])
    asks.append({"id": f"q{len(asks) + 1}", "question": question, "options": options, "path": op.path,
                 "answer": None})


def _template(spec: dict[str, Any], op: BpOp, source: str) -> None:
    from app.blueprints.templates import get_template

    ref = op.template
    if ref is None:
        raise BpRefused("选模板要写明是哪一个")
    template = get_template(ref.id)
    if template is None:
        raise BpRefused(f"没有叫「{ref.id}」的场景模板", code="bp.template_mismatch")
    if ref.version != template.version:
        raise BpRefused(f"场景模板「{template.label}」现在是第 {template.version} 版，请按这一版选",
                        code="bp.template_mismatch")
    spec["template"] = {"id": template.id, "version": template.version}
    from app.blueprints.templates._common import prefill

    prefill(template.id, spec)


def apply_bp_op(spec: dict[str, Any], op: dict[str, Any], *, source: str, graph: Any = None) -> dict[str, Any]:
    """把一个 bp_* 操作落到蓝图上，交回新的蓝图（不改入参）。source 是 user / assistant。不合规时抛 BpRefused。
    graph 是蓝图存的图：自由形的「谁来做」按它列岗位（没给时那张卡片改不了）。"""
    if source not in BP_SOURCES:
        raise BpRefused(f"操作的来源只能是用户或助手，当前为「{source}」")
    if not isinstance(op, dict) or op.get("op") not in BP_OPS:
        raise BpRefused(f"只能用这几种操作：{'、'.join(BP_OPS)}")
    try:
        parsed = BpOp.model_validate(op)
        out = parse_blueprint(copy.deepcopy(spec)).model_dump(mode="json", by_alias=True)
    except ValidationError as e:
        raise BpRefused(f"操作的写法不对：{_first(e)}") from e
    if parsed.op == "bp_ask":
        _ask(out, parsed, source)
    elif parsed.op == "bp_template":
        _template(out, parsed, source)
    else:
        tokens = parse_pointer(parsed.path)
        if tokens[0] not in _LABEL or tokens[0] == "template":
            raise BpRefused(f"没有这张卡片：/{tokens[0]}（选模板用 bp_template）")
        if source != "user" and (only := next((k for k in USER_ONLY if tuple(tokens[:len(k)]) == k), None)):
            raise BpRefused(_USER_ONLY_TEXT[only[-1]])
        if parsed.op == "bp_set":
            _set(out, tokens, parsed.value, source, graph)
        elif parsed.op == "bp_add":
            _add(out, tokens, parsed.value, source)
        else:
            _remove(out, tokens, source, graph)
    try:
        return parse_blueprint(out).model_dump(mode="json", by_alias=True)
    except ValidationError as e:
        raise BpRefused(f"改完的蓝图不合格式：{_first(e)}") from e


def _first(e: ValidationError) -> str:
    err = e.errors()[0] if e.errors() else {}
    where = ".".join(str(x) for x in err.get("loc") or ())
    return f"{where} {err.get('msg', '')}".strip()


# ==========================================================================
# 蓝图检查（§3.13 bp.*）
# ==========================================================================

#: 检查的用途：试运行（缺项挡）、发布（再加待确认项挡）、个人应用（缺项挡，另查模板的个人应用部分）
ISSUE_PURPOSES = ("trial", "publish", "personal")
#: 卡片约束说明以这两个词开头：模板登记里 cards 的写法（blueprints/templates）
REQUIRED_MARK = "必填"


def _value(item: Any) -> Any:
    return item.get("value") if isinstance(item, dict) else None


def blueprint_issues(spec: dict[str, Any], *, for_: str, graph: Any = None) -> list[dict[str, Any]]:
    """蓝图检查（§3.13 bp.*）：for_ = trial（缺项挡试运行）/ publish（待确认项挡发布）/ personal。
    每条 {code, level, path, message}。graph 是蓝图存的图：自由形的「谁来做」按它查（没给时不查这一张）。"""
    from app.blueprints.templates import get_template

    if for_ not in ISSUE_PURPOSES:
        raise ValueError(for_)
    out: list[dict[str, Any]] = []

    def add(code: str, path: str, message: str, level: str = "error") -> None:
        out.append({"code": code, "level": level, "path": path, "message": message})

    ref = spec.get("template") or None
    template = get_template(ref["id"]) if isinstance(ref, dict) else None
    if not isinstance(ref, dict):
        add("bp.missing", "/template", "还没有选场景：先说说要做什么，或者从场景模板开始")
        return out
    if template is None:
        add("bp.template_mismatch", "/template", f"场景模板「{ref.get('id')}」已经不在了，请换一个场景")
        return out
    if ref.get("version") != template.version:
        add("bp.template_mismatch", "/template",
            f"这张蓝图按场景模板「{template.label}」第 {ref.get('version')} 版做的，模板现在是第 {template.version} 版："
            "请重新选一次模板，卡片会按新版本核一遍", level="warning")
    used = set(template.cards)
    required = {c for c, note in template.cards.items() if note.startswith(REQUIRED_MARK)}
    publish = for_ == "publish"

    if "purpose" in required and _value(spec.get("purpose")) is None:
        add("bp.missing", "/purpose", "「目的」还没写：一句话说清这个应用解决什么问题、给谁用")
    # 模板用不到的卡片只报一条 bp.template_mismatch（见下），里面的项不再逐条查
    if "data" in used:
        _data_issues(spec, "data" in required, add)
    if "cadence" in required and _value(spec.get("cadence")) is None:
        add("bp.missing", "/cadence", "「多久更新」还没定：每天、每周还是每月，几点，统计哪一期")
    if "people" in used and template.imports_graph:
        if graph is not None and for_ != "personal":
            out += _freeform_people_issues(spec, graph, publish)
    elif "people" in used:
        _people_issues(spec, "people" in required, publish, add)
    if "fields" in used:
        _field_issues(spec, "fields" in required, add)
    wb = spec.get("writeback")
    if "writeback" in required and (not isinstance(wb, dict) or _value(wb.get("capability")) is None):
        add("bp.missing", "/writeback", "「写回哪里」还没定：要写回哪个系统、哪些字段")
    elif isinstance(wb, dict) and _value(wb.get("capability")) is not None and _value(wb.get("mapping")) is None:
        add("bp.missing", "/writeback/mapping", "写回的字段对应还没写：目标系统的每个参数取哪个字段或指标")
    if "writeback" in used and for_ != "personal" and isinstance(wb, dict) and _value(wb.get("reconciler")) is None:
        from app.blueprints.templates._common import needs_reconciler

        if needs_reconciler(spec):
            # 目标能力没登记查询：写回的结果不明时平台查不了、也不自动重发，要有人去目标系统里核实。试运行时写入走测试
            # 环境或演练、待办派给试运行的人自己，不挡试运行，只挡发布（同 bp.people_unbound）
            add("bp.reconciler_unbound", "/writeback/reconciler",
                f"「{_value(wb.get('capability'))}」没登记查询：写回的结果不明时（请求发出去了、没拿到答复）要有人去目标系统里"
                "核实，还没定是谁", level="error" if publish else "warning")

    for card in ("data", "people", "fields"):
        if spec.get(card) and card not in used:
            add("bp.template_mismatch", f"/{card}",
                f"场景「{template.label}」用不到「{_LABEL[card]}」：这张卡片上写的不会生效。请删掉，或者换一个场景")
    if isinstance(wb, dict) and _value(wb.get("capability")) is not None and "writeback" not in used:
        add("bp.template_mismatch", "/writeback",
            f"场景「{template.label}」不写回别的系统：「写回哪里」不会生效。请删掉，或者换成核对写回、审批流程")
    # 模板自己认的：卡片内容超出模板能力（只能从一个库查、复用的口径卡接不上、写回取不到的字段……）
    from app.blueprints.templates._common import template_problems

    out += template_problems(template.id, spec)
    if for_ == "personal":
        if template.personal is None:
            add("bp.template_mismatch", "/template", f"场景「{template.label}」不能做成个人应用：它要有人审批或写回别的系统")
        if spec.get("people"):
            add("bp.template_mismatch", "/people", "个人应用不能有审批步骤：想给别人用，请提交给搭建者审核发布")
        if isinstance(wb, dict) and _value(wb.get("capability")) is not None:
            add("bp.template_mismatch", "/writeback", "个人应用只读，不能写回别的系统")
    if publish:
        for path, item in _items(spec):
            if item.get("status") == "proposed" and item.get("value") is not None:
                add("bp.unconfirmed", path, f"「{_where(path)}」是{_SOURCE_WORD.get(item.get('source'), '')}提的，"
                                            "还没确认：发布前要你看过")
    return out


_SOURCE_WORD = {"assistant": "助手", "template": "模板默认", "catalog": "按数据目录对到", "caliber": "按口径卡对到"}


def _data_issues(spec: dict[str, Any], required: bool, add: Callable[..., None]) -> None:
    data = spec.get("data") or []
    if required and not data:
        add("bp.missing", "/data", "「看哪些数」还没写：至少要有一项，比如销售额、订单数")
    seen: set[str] = set()
    for i, item in enumerate(data):
        if _value(item.get("label")) is None:
            add("bp.missing", f"/data/{i}/label", f"第 {i + 1} 项数还没有名字")
        reuse = item.get("reuse")
        if _value(item.get("table")) is None and not reuse:
            add("bp.data_unmapped", f"/data/{i}/table",
                f"「{_value(item.get('label')) or item.get('id')}」还没对到表或已发布的口径卡")
        if reuse and reuse.get("metric_id") in seen:
            add("bp.template_mismatch", f"/data/{i}/reuse", f"复用的指标「{reuse['metric_id']}」和别的项重名，请只留一项")
        seen.add(str(reuse.get("metric_id") if reuse else item.get("id")))


def _people_issues(spec: dict[str, Any], required: bool, publish: bool, add: Callable[..., None]) -> None:
    people = spec.get("people") or []
    if required and not people:
        add("bp.missing", "/people", "「谁来做」还没写：至少要有一步，写明谁处理")
    numeric = {f.get("name") for f in spec.get("fields") or []
               if (_value(f.get("spec")) or {}).get("type") in ("number", "integer")}
    names = {f.get("name") for f in spec.get("fields") or []}
    for i, step in enumerate(people):
        label = _value(step.get("label")) or step.get("id")
        if _value(step.get("who")) is None:
            # 试运行时人工步骤派给试运行的人自己，岗位没人不挡试运行，只挡发布
            add("bp.people_unbound", f"/people/{i}/who", f"「{label}」还没定谁来处理",
                level="error" if publish else "warning")
        if _value(step.get("policy")) is None:
            add("bp.missing", f"/people/{i}/policy", f"「{label}」还没定几个人同意算通过")
        timeout = _value(step.get("on_timeout"))
        if timeout and _value(step.get("due")) is None:
            add("bp.missing", f"/people/{i}/due", f"「{label}」写了超时怎么办，还要写时限")
        threshold = _value(step.get("threshold"))
        if threshold:
            if threshold["field"] not in names:
                add("bp.template_mismatch", f"/people/{i}/threshold",
                    f"「{label}」的条件用到的字段「{threshold['field']}」不在「填什么」里")
            elif threshold["op"] not in ("==", "!=") and threshold["field"] not in numeric:
                add("bp.template_mismatch", f"/people/{i}/threshold",
                    f"「{label}」的条件拿「{threshold['field']}」比大小，它要是数字字段")


def _freeform_people_issues(spec: dict[str, Any], graph: Any, publish: bool) -> list[dict[str, Any]]:
    """自由形的「谁来做」（freeform.people_rows）：岗位、核实人没绑的报 bp.people_unbound（试运行 warning：待办派给试运行的
    人自己；发布 error），路径就是那一行的 path，带上 slot（发布单据此去掉同一件事的 app.slot_unbound / app.reconciler_unbound）。
    图里已经没有的岗位、卡片上还留着绑定的，照实提醒（warning），不静默删掉。"""
    from app.blueprints.templates import freeform
    from app.blueprints.templates._common import with_graph

    out: list[dict[str, Any]] = []
    level = "error" if publish else "warning"
    rows = freeform.people_rows(with_graph(spec, graph))
    for row in rows:
        if row["kind"] not in ("slot", "reconciler") or row["who"]:
            continue
        message = (f"{row['label']}还没定：{row['detail']}，写入外部系统的结果不明时要有人去目标系统里核实"
                   if row["kind"] == "reconciler" else f"{row['label']}还没定谁来处理（{row['detail']}）")
        out.append({"code": "bp.people_unbound", "level": level, "path": row["path"], "message": message,
                    "slot": row["slot"]})
    known = {r["slot"] for r in rows if r["kind"] == "slot"}
    for step in spec.get("people") or []:
        slot = str(step.get("id") or "")
        if slot and slot not in known and _value(step.get("who")):
            out.append({"code": "bp.template_mismatch", "level": "warning", "path": "/people",
                        "message": f"岗位「{slot}」在导入的图里已经没有了：卡片上留着的这条绑定不会生效"})
    return out


def _field_issues(spec: dict[str, Any], required: bool, add: Callable[..., None]) -> None:
    fields = spec.get("fields") or []
    if required and not fields:
        add("bp.missing", "/fields", "「填什么」还没写：至少要有一个字段")
    for i, f in enumerate(fields):
        if _value(f.get("spec")) is None:
            add("bp.missing", f"/fields/{i}/spec", f"字段「{f.get('name')}」还没定类型")


def _items(spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """蓝图里所有带状态的项：(路径, 项)。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for card in ("purpose", "cadence"):
        if isinstance(spec.get(card), dict):
            out.append((f"/{card}", spec[card]))
    for card in ("data", "people"):
        for i, entry in enumerate(spec.get(card) or []):
            out += [(f"/{card}/{i}/{k}", entry[k]) for k in _ITEM_FIELDS[card] if isinstance(entry.get(k), dict)]
    for i, f in enumerate(spec.get("fields") or []):
        if isinstance(f.get("spec"), dict):
            out.append((f"/fields/{i}/spec", f["spec"]))
    wb = spec.get("writeback")
    if isinstance(wb, dict):
        out += [(f"/writeback/{k}", wb[k]) for k in WRITEBACK_KEYS if isinstance(wb.get(k), dict)]
    return out


_PART_LABEL = {"label": "名称", "unit": "单位", "table": "对到的表", "measure": "怎么算", "date": "业务日期",
               "filter": "筛选条件", "grain": "粒度", "who": "处理人", "policy": "几个人同意", "due": "时限",
               "on_timeout": "超时怎么办", "threshold": "条件", "spec": "字段", "capability": "目标能力",
               "mapping": "字段对应", "confirm": "要不要人确认", "reconciler": "结果不明时谁去核实"}


def _where(path: str) -> str:
    tokens = parse_pointer(path)
    card = _LABEL.get(tokens[0], tokens[0])
    if len(tokens) == 1:
        return card
    if len(tokens) == 3:
        return f"{card}·第 {int(tokens[1]) + 1} 项·{_PART_LABEL.get(tokens[2], tokens[2])}"
    return f"{card}·{_PART_LABEL.get(tokens[-1], tokens[-1])}"
