"""双盲复算的选择写进数据目录：审核人在不一致的指标上选了原流程或盲算，而两边只差一个方面、又正好对得上数据目录
里的一项事实时，把被采纳那一边的写法记成人工确认过的目录项。

对得上的事实只有三种（data/catalog.py 的表级项）：

- 两边按不同的时间列筛时间 → 业务日期（business_date）：被采纳的一边按哪一列筛，业务日期就记哪一列。只有一边按时间
  筛的不算：审核人判断的是要不要限定时间，没在两列之间选过。两边的列都得在表结构里声明为带日期的类型（DATE、
  DATETIME、TIMESTAMP、TIMESTAMPTZ，带精度、带时区的写法也算）：差异说明认时间列只看列名和常量像不像日期，
  文本列（SQLite 常把日期存成文字）、整数列（Unix 秒、20260901 这类日期键）、名字带 time 的时长列都可能被说成「按它筛
  时间」，认不准存的是不是日期，不记。只有时刻的 TIME 也不记：一天里的几点几分（MySQL 还常拿它存时长）不是日期，和
  catalog.is_date_column（剖析和 SQL 检查共用的日期列判断）同一个口径——记成业务日期，SQL 检查会对按真实日期列统计的
  查询一直报错误级。上传表格建的数据源，导入时日期一律按文字保存，表结构里总是 TEXT：这样的列看导入时配方里记的逻辑
  类型（upload_dates，导入确认时界面上标的「日期」就是它），是日期的算日期列；配方读不出来（简单导入没有配方、配方
  记录坏了）或者配方里不是日期的，照旧不记，原因照实说是上传的表格按文字保存，不说成用户声明了 TEXT
- 一边多一个排除无效记录的条件 → 有效记录条件（valid_filter）：被采纳的一边有这个条件才记，记的就是这个条件
- 聚合只差去不去重、被采纳的一边去重 → 去重规则（dedup）。采纳了不去重的一边不记：那是这个指标自己的算法（数行），
  写成表的「不去重」会把别的指标带偏

**为什么要这么严。** 目录是给助手、db_schema 工具、SQL 检查和下一次盲算看的数据事实，人工确认过的项还会让 SQL 检查
报错误级、以后的推断也不再覆盖它，写错了就一直错下去。只差一个方面时，人选的那一边就是对这一处的判断；差了好几处、
或者差异说明里有兜底（未能归纳的差异），人选的是「整体上哪边对」，拆不出是哪一处对，写进去就是替人编了一条事实。
两条 SQL 规范化后一致却算出不同的值（两次取数之间数据变了、结果被截断），差在数据不在写法，更没有可记的事实。

**有效记录条件和指标自己的筛选范围分不开。** 「华东销售额」的 region = '华东'、「线上订单数」的 channel = 'online'
在写法上和 status <> 'refunded' 一样是一个筛选条件，审核人选原流程只说明这个指标该这么筛，不说明整张表的有效记录
都得这么筛。写成表的有效记录条件，所有查这张表的工作流（包括下一次盲算）都会被带偏。所以条件要同时满足三条才写：
条件里的取值没有出现在指标名称、口径说明、这一格所在的分组、本次运行的输入里，用到的列也不是分组的列；每一段都是在
排除无效记录（排除的取值是退款、作废、测试这类，或者码值表里写着这类意思；标记列只认留下有效记录的那一边：
is_test = 0、is_valid = 1、deleted_at IS NULL）；只涉及一张表。拿不准就不写，原因照实交回。

**怎么写。** 照对话提案的规矩核对（plan_patch：列在不在表结构里、取值格式），记为人工填写、已确认，备注写明来自哪次
运行、哪个节点、哪个指标。目录里这一项已经是人工确认过的另一种说法时不覆盖，照实交回原因；已经是同样的说法就什么都
不写（再写一遍也不升版本）；这一项被人工驳回过、要写的又带回了驳回的说法（有效记录条件按段比：驳回的那句每一段都在
要写的条件里），两次人工判断对不上，不写。有效记录条件是一整句条件：目录里已有的条件被采纳的一边（几个指标并成一项
时是每一个）也按它筛了的，和新条件合起来写，已有的段只写一次（已确认的不改，照实说多出了哪几段）；有一个没按它筛
的，两者的关系拿不准，不动。推断的去重规则说的未必是同一件事，也不替换。整个写入由节点放在 ask() 之后的一个 @task
里：审批恢复重放、接着运行时不会再写一遍。

**同一次裁决里几个指标落到同一项上。** 说法一样的写一次，备注里列出每个指标。有效记录条件是能用 AND 叠加的排除条件
（这里只收排除无效记录的），几个指标各自采纳的条件都有人的选择撑着，合成一句写——「排除退款」和「排除测试单」并不
矛盾。业务日期一张表只有一个，去重规则说的未必是同一件事，说法不同就一个都不写。合起来之前先按每个指标自己的说法
过一遍驳回检查：自己采纳的说法曾被人工驳回的指标拿出来不写、照实说原因，其余的照写；合起来以后又包含了驳回的整句的，
也不写。

**预告。** 审批卡上要让审核人看得出选了之后会往目录里写什么（preview）：按发起审批时的目录，选原流程、选盲算各会
写什么、不写的为什么。认出了事实、只是被目录挡下（已确认、驳回过、和已有条件合不起来）的那一边也带上写到哪张表的
哪一项、本来要写什么，卡上才认得出它和别的指标是不是落到同一项上。同一次裁决里几个指标落到同一项上时，写进去的是
合起来的说法（上一段），单个指标的预告说不准：preview_joint 把这几个指标各自选哪一边的每种组合都按真写的办法算一遍
（_merged 归拢、_screen 拿掉驳回的、_plan 对目录），卡上按各指标当前的选择取对应的那一种。和真写是同一套判断、同一句
原因，只有审批停下期间目录被人改了、数据源换了版本时才会和最后写的不一样：真写按写入时的目录、表结构和导入时认作
日期的列判断。组合太多的（JOINT_LIMIT）那一项不预告，卡上有条件地说。

catalog.save_patch 只把提案的依据写成「依据：……」的备注，这里的备注是出处（哪次运行、哪个节点），所以用它的同一套原料
（read_entry、apply_patch、write_entry 的乐观锁）写，再把这一项换成带出处备注的人工确认项。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select

from app.data import catalog
from app.data.names import name_key
from app.db.base import SessionLocal
from app.db.models import DataSource

logger = logging.getLogger(__name__)

#: 能写的三项（表级）
FACT_ITEMS = ("business_date", "valid_filter", "dedup")
#: 审核人采纳的一边在差异说明里的位置：a 是原流程、b 是盲算（节点调用 explain_sql_diff 时按这个顺序给两边）
SIDE_LABEL = {"a": "原流程", "b": "盲算"}
#: 差异条目的方面（sql_semantics.FacetDiff.aspect）在说明里的叫法
ASPECT_LABEL = {
    "time": "时间列", "time_range": "时间范围", "filter": "筛选条件", "aggregate": "聚合方式", "expression": "选出的列",
    "join": "关联", "tables": "查询的表", "set_op": "几支查询的合并方式", "group_by": "分组", "having": "分组后的筛选",
    "limit": "取哪几行", "order": "排序", "parse": "能否解析", "other": "未能归纳的地方",
}
#: 聚合函数的说法（去重规则那一句用）
_VERB = {"count": "计数", "sum": "求和", "avg": "求平均"}
#: 写入时版本对不上（别人刚改过这张表）最多重试几次
_RETRIES = 2
#: 同一次裁决里几个指标落到同一项上时，这一项最多预告多少种组合（preview_joint，每一项各算各的，不共用额度）。
#: 每个指标两边都落在这一项上时近于 3 的指标数次方：四个指标 72 种还在里面，再多的那一项不预告，审批卡回到按其余指标
#: 当前的选择有条件地说。组合数先算出来再比（_combos），不把组合列出来比：二十个指标就是几十亿种
JOINT_LIMIT = 128

#: 无效记录的取值：被排除的取值（或码值表里它的意思）里有这些词，才算在排除无效记录
_INVALID_WORDS = frozenset({
    "refund", "refunded", "refunding", "cancel", "canceled", "cancelled", "void", "voided", "delete", "deleted",
    "invalid", "test", "testing", "fraud", "spam", "dummy", "fake", "sandbox", "fail", "failed", "duplicate",
    "duplicated", "obsolete",
})
_INVALID_CJK = ("退款", "作废", "取消", "测试", "无效", "删除", "撤销", "失败", "重复", "虚假", "欺诈", "废弃")
#: 标记列：列名里有这些词的，标记的是无效记录（is_test、is_deleted、del_flag、deleted_at……），留下的是没标记的
_MARKS_INVALID = _INVALID_WORDS | {"del", "removed", "hidden", "archived", "internal", "disabled", "inactive"}
#: 标记列：列名里有这些词的，标记的是有效记录（is_valid、enabled、is_active……），留下的是标记了的
_MARKS_VALID = frozenset({"valid", "enabled", "active", "effective"})
#: 取值和指标名称、口径说明比对时，数字至少这么长才比（0、1 这种标记值到处都是）
_SCOPE_DIGITS = 2
#: 表结构里声明的带日期的类型（业务日期只记这样的列）：DATE、DATETIME、TIMESTAMP、TIMESTAMPTZ，可以带精度
#: （TIMESTAMP(3)、DATETIME(6)）、带时区（TIMESTAMP WITH TIME ZONE、Oracle 的 WITH LOCAL TIME ZONE），大小写不论。整个
#: 类型名都得是这个样子：DATE_KEY、INTERVAL 这类名字里带着 date、time 的不算
_DATED_TYPE = re.compile(r"(?:date|datetime|timestamp|timestamptz)(?:\s*\(\s*\d+\s*\))?"
                         r"(?:\s+with(?:out)?\s+(?:local\s+)?time\s+zone)?", re.IGNORECASE)
#: 只有时刻的类型（TIME、TIMETZ、TIME(3)、TIME WITH TIME ZONE）：不记，原因另说「只有时刻」
_TIME_ONLY_TYPE = re.compile(r"(?:time|timetz)(?:\s*\(\s*\d+\s*\))?(?:\s+with(?:out)?\s+time\s+zone)?",
                             re.IGNORECASE)


@dataclass(frozen=True)
class Fact:
    """要写进目录的一项：哪个数据源的哪张表、哪一项、写什么。metric、choice 只用来写备注和说明；sql、dialect 是被采纳
    那一边的写法和方言，有效记录条件和目录里已有的条件合并时要看它还按哪些条件筛了。同一次裁决里几个指标落到同一项上
    时并成一项：members 是并进来的每个指标各自的那一项（各带自己的说法，审批卡上各自的预告就是按它算的），value 是
    合起来之后的说法；单个指标的 members 为空。metric_id 是指标 ID：几个指标并成一项时，预告按它说清每个指标的结果。"""

    source: str
    table: str
    item: str
    value: Any
    metric: str
    choice: str
    sql: str = ""
    dialect: str | None = None
    members: tuple[Fact, ...] = ()
    metric_id: str = ""


def _members(fact: Fact) -> tuple[Fact, ...]:
    """这一项背后的每个指标的选择：并成一项的是并进来的每一个（各带自己的说法），单个指标的是它自己。"""
    return fact.members or (fact,)


def _names(members: Sequence[Fact]) -> str:
    """说明里的指标叫法：「销售额」，几个指标时「销售额」、「订单金额」。"""
    names = list(dict.fromkeys(m.metric for m in members))
    return "、".join(f"「{n}」" for n in names[:4]) + (f"等 {len(names)} 个" if len(names) > 4 else "")


def derive(candidate: Mapping[str, Any], schema_cache: Mapping[str, Any] | None,
           notes: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[Fact | None, str | None]:
    """一个候选（审核人在某个指标上选了 a 或 b）→ (要写的目录项, None) 或 (None, 为什么不写)。

    candidate：{metric 指标名, choice "a" | "b", source 数据源名, dialect, sql: {a, b}（比较的两条 SQL）, diff:
    {equivalent, fallback, items: [FacetDiff.as_dict()]}, scope: {texts, columns}, uploaded, upload_dates}。scope 是
    这个指标自己的范围：texts 是口径说明、所在分组的取值、本次运行的输入（指标名称总算在内），columns 是分组的列；有效
    记录条件用它排除指标自己的筛选范围。uploaded 为真是上传表格建的数据源，upload_dates 是它导入时认作日期的列
    （{表名: [列名]}，由 upload_dates 从配方读出）：按文字保存的列在这里面的算日期列，不在的业务日期不记、原因照实说
    是上传的表格按文字保存日期。两项都由节点发起审批时备好、放进候选，审批卡上的预告按它；真写时 record_choices 按
    写入时的版本重读 upload_dates，和它现读的表结构出自同一个版本。schema_cache 是数据源的表结构：认表，业务日期还要
    看两边的列声明的类型。notes 是这个数据源的目录（{表名: notes}），认码值的意思用。只看差异说明，不重新比 SQL：
    审核人看到的就是这份说明。
    """
    name = str(candidate.get("metric") or "")
    side = candidate.get("choice")
    diff = candidate.get("diff") if isinstance(candidate.get("diff"), Mapping) else {}
    if side not in SIDE_LABEL:
        return None, f"指标「{name}」没有采用任何一边的结果，不写入数据目录"
    if diff.get("equivalent"):
        return None, f"指标「{name}」两边的 SQL 规范化后一致，差异来自数据而不是写法，不写入数据目录"
    items = [i for i in diff.get("items") or [] if isinstance(i, Mapping)]
    if diff.get("fallback") or len(items) != 1:
        return None, (f"指标「{name}」两边的 SQL 不止差一个方面，或有未能归纳的差异，"
                      "无法确定审核人认可的是哪一处，不写入数据目录")
    item = items[0]
    aspect = str(item.get("aspect") or "")
    if aspect == "time":
        dates = candidate.get("upload_dates")
        fact, why = _business_date(item, side, schema_cache, uploaded=candidate.get("uploaded") is True,
                                   dates=dates if isinstance(dates, Mapping) else None)
    elif aspect == "filter":
        fact, why = _valid_filter(item, side, candidate, schema_cache, notes or {})
    elif aspect == "aggregate":
        fact, why = _dedup(item, side, candidate, schema_cache)
    else:
        return None, (f"指标「{name}」两边差在{ASPECT_LABEL.get(aspect, '其他地方')}，"
                      "对不上数据目录里的业务日期、有效记录条件或去重规则，不写入数据目录")
    if why is not None:
        return None, f"指标「{name}」{why}，不写入数据目录"
    table, key, value = fact
    resolved = catalog.resolve_table_name(schema_cache, table)
    if resolved is None:
        return None, f"指标「{name}」涉及的表「{table}」不在数据源的表结构里，不写入数据目录"
    sql = candidate.get("sql") if isinstance(candidate.get("sql"), Mapping) else {}
    dialect = candidate.get("dialect")
    return Fact(source=str(candidate.get("source") or ""), table=resolved, item=key, value=value, metric=name,
                choice=str(side), sql=str(sql.get(side) or ""),
                dialect=dialect if isinstance(dialect, str) else None,
                metric_id=str(candidate.get("metric_id") or "")), None


def _business_date(item: Mapping[str, Any], side: str, schema_cache: Mapping[str, Any] | None, *,
                   uploaded: bool = False, dates: Mapping[str, Any] | None = None
                   ) -> tuple[tuple[str, str, Any] | None, str | None]:
    """两边按不同的列筛时间、被采纳的一边按哪一列：(表, business_date, {column}) 或 (None, 为什么不记)。两边的列都要在
    表结构里声明为带日期的类型、或者是上传表格导入时认作日期的列（_dated_problem），被采纳的一边先看：它有问题说它；没
    问题再看未被采纳的一边，另说清为什么也要看它（审核人看到的是自己选的那一列，不说就看不懂另一列为什么要紧）。数据源
    还没有可用的表结构时（没探查、探查失败）只说表结构不可用：那时并不知道有没有这一列，说「不在表结构里」是在误导。"""
    other = "b" if side == "a" else "a"
    detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}

    def columns(key: str) -> list[Mapping[str, Any]]:
        return [c for c in detail.get(key) or [] if isinstance(c, Mapping) and c.get("table") and c.get("column")]

    mine, theirs = columns(side), columns(other)
    if not mine:
        return None, f"被采纳的{SIDE_LABEL[side]}没有按时间筛选，没有可记的业务日期"
    if not theirs:
        return None, (f"只有被采纳的{SIDE_LABEL[side]}按时间筛选，{SIDE_LABEL[other]}没有：审核人判断的是要不要限定"
                      "时间，不是按哪一列归属，没有可记的业务日期")
    if len(mine) > 1 or len(theirs) > 1:
        return None, "有一边按好几列筛时间，无法确定业务日期按哪一列"
    if (mine[0]["table"], mine[0]["column"]) == (theirs[0]["table"], theirs[0]["column"]):
        return None, "两边按同一列筛时间，没有可记的业务日期"
    tables = schema_cache.get("tables") if isinstance(schema_cache, Mapping) else None
    if not isinstance(tables, Mapping) or not tables:
        return None, "所在数据源还没有可用的表结构（尚未探查或探查失败），无法确认两边按时间筛的列存的是日期"
    for adopted, ref in ((True, mine[0]), (False, theirs[0])):
        if (problem := _dated_problem(tables, ref, uploaded, dates)) is None:
            continue
        said = f"按时间筛的列「{ref['table']}.{ref['column']}」{problem}"
        if adopted:
            return None, f"被采纳的{SIDE_LABEL[side]}{said}"
        return None, f"未被采纳的{SIDE_LABEL[other]}{said}：两边都是日期列时，审核人选的才是业务日期按哪一列"
    return (str(mine[0]["table"]), "business_date", {"column": str(mine[0]["column"])}), None


def _dated_problem(tables: Mapping[str, Any], ref: Mapping[str, Any], uploaded: bool,
                   dates: Mapping[str, Any] | None = None) -> str | None:
    """按时间筛的这一列在表结构里是不是声明成了带日期的类型（_DATED_TYPE）：是的为 None，不是的交回它怎么了（查不到、没
    声明类型、只有时刻、声明成了别的类型）连同结论，接在「按时间筛的列「表.列」」后面读。

    表按 catalog.resolve_table_name 认，列先按原样、再按 name_key 认（差异说明里的名字是规范化过的，SQLite 不加引号的
    名字一律小写）。按 name_key 认出好几列的说对应好几列：哪一列是它说不准。类型照表结构里记的说（「类型是」，不说
    「声明为」）：introspect 记的是 SQLAlchemy 反射出来的类型，SQLite 里写成 TIMESTAMPTZ 的列反射出来是 NUMERIC；没写
    类型的列反射出来是 NullType，记成「NULL」，按没有声明类型说。

    上传表格建的数据源（uploaded）日期一律存成文字，表结构改不了，类型只能看导入时配方里记的（dates，见 upload_dates）：
    按文字保存的列在里面的就是日期列（按表结构里认出的表名、列名去对，大小写不论）。不在里面的（配方读不出来、配方里
    不是日期）不说「类型是 TEXT」：那不是用户声明的，说清是上传的表格按文字保存。声明成别的类型的列照常说类型，只认
    文字类型：配方里的日期列只会存成文字，别的类型的列存的就不是配方里那一列。"""
    found_table = catalog.resolve_table_name({"tables": tables}, str(ref["table"]))
    meta = tables.get(found_table) if found_table is not None else None
    columns = [c for c in meta.get("columns") or [] if isinstance(c, Mapping) and c.get("name")] \
        if isinstance(meta, Mapping) else []
    column = str(ref["column"])
    found = [c for c in columns if str(c["name"]) == column] \
        or [c for c in columns if name_key(str(c["name"])) == name_key(column)]
    if not found:
        return "不在数据源的表结构里，无法确认存的是日期"
    if len(found) > 1:
        return "在表结构里对应好几列，无法确认存的是日期"
    declared = " ".join(str(found[0].get("type") or "").split())
    if not declared or declared.upper() == "NULL":
        return "在表结构里没有声明类型，无法确认存的是日期"
    if _DATED_TYPE.fullmatch(declared):
        return None
    if _TIME_ONLY_TYPE.fullmatch(declared):
        return f"在表结构里的类型是 {declared}，只有时刻，不含日期"
    if uploaded and any(word in declared.upper() for word in catalog._TEXT_TYPES):
        if _imported_as_date(dates, str(found_table), str(found[0]["name"])):
            return None
        return "来自上传的表格，表格里的日期一律按文字保存，无法确认存的是日期"
    return f"在表结构里的类型是 {declared}，无法确认存的是日期"


def _imported_as_date(dates: Mapping[str, Any] | None, table: str, column: str) -> bool:
    """表结构里的这一列是不是上传表格导入时认作日期的列（dates：{表名: [列名]}）。表、列都先按原样、再按 name_key 对：
    名字出自同一份配方，本来一字不差；按 name_key 对上好几个的不算，说不准是哪一个。"""
    if not isinstance(dates, Mapping):
        return False

    def one(names: Any, wanted: str) -> str | None:
        names = [str(n) for n in names if isinstance(n, str)]
        if wanted in names:
            return wanted
        loose = [n for n in names if name_key(n) == name_key(wanted)]
        return loose[0] if len(loose) == 1 else None

    key = one(list(dates), table)
    columns = dates.get(key) if key is not None else None
    return isinstance(columns, (list, tuple)) and one(columns, column) is not None


# ---- 上传表格导入时认作日期的列：配方里记的逻辑类型

def recipe_dates(recipe: Any) -> dict[str, list[str]]:
    """一份配方（recipe_types.Recipe）里逻辑类型是日期的列：{表名: [列名]}，没有日期列的表不列。

    就是导入确认时界面上标「日期」的那些（recipe_notes 给列写日期格式说明认的也是这两种）：交叉表的日期轴（Axis.type 恒为
    日期），列表块里类型记为 DATE 的列。执行器把它们一律存成 YYYY-MM-DD 的文字，表结构里只剩 TEXT。哪张表有哪几列按
    recipe_types.derive_tables 推（建表用的同一份）：日期轴写进它各分段的表，列表合计行另存的表只有合计项和数字列。
    列表的类型按 _DATED_TYPE 认，配方以后添了日期时间也算；只有时刻的不算。几个列表块写同一张表的，这一列每一块都
    记为日期才算：日期和文字都存成 TEXT，建出的表形状一样，配方校验不拦，可按文字导入的那一块存的是单元格原文。"""
    from app.data.recipe_types import ListBlock, derive_tables

    tables, _ = derive_tables(recipe)
    listed: dict[tuple[str, str], bool] = {}
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, ListBlock):
                for column in block.columns:
                    key = (block.table, column.name)
                    listed[key] = listed.get(key, True) and _DATED_TYPE.fullmatch(column.type) is not None
    out: dict[str, list[str]] = {}
    for table, columns in tables.items():
        names = [c.name for c in columns if c.role == "axis" or listed.get((table, c.name), False)]
        if names:
            out[table] = list(dict.fromkeys(names))
    return out


async def upload_dates(session: Any, row: DataSource | None) -> dict[str, list[str]]:
    """上传表格建的数据源当前版本里，导入时认作日期的列：{表名: [列名]}（recipe_dates）。只读，不碰导入流程。

    配方取现行的那一版（current_recipe_id）：发布、启用旧版本时它和当前快照、表结构（schema_cache）一起换
    （table_versions.activate_snapshot），配方和表结构出自同一个版本。读不出来的一律给空——手工登记的源、简单导入
    （没有配方，构建回执里只有存储类型）、配方记录不在了、配方解析不了：业务日期照旧不记，原因说按文字保存。"""
    from app.data.recipe_types import Recipe
    from app.db.models import TableRecipe

    if row is None or (row.origin or "manual") != "upload" or not row.current_recipe_id:
        return {}
    record = await session.get(TableRecipe, row.current_recipe_id)
    if record is None or record.source_id != row.id:
        return {}
    try:
        return recipe_dates(Recipe.model_validate(record.recipe))
    except ValueError:
        logger.warning("上传表格的配方记录解析不了，业务日期按文字保存处理：source=%s recipe=%s", row.id, record.id)
        return {}


def _valid_filter(item: Mapping[str, Any], side: str, candidate: Mapping[str, Any],
                  schema_cache: Mapping[str, Any] | None,
                  notes: Mapping[str, Mapping[str, Any]]) -> tuple[tuple[str, str, Any] | None, str | None]:
    other = "b" if side == "a" else "a"
    condition, rival = item.get(side), item.get(other)
    if rival is not None:
        if condition is None:
            return None, f"多出筛选条件的是{SIDE_LABEL[other]}，被采纳的{SIDE_LABEL[side]}没有这个条件"
        return None, "两边各有不同的筛选条件，不是一边多出一个条件"
    if not isinstance(condition, str) or not condition.strip():
        return None, "差异说明里没有被采纳一边的筛选条件"
    condition = condition.strip()
    detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}
    refs = [c for c in detail.get("columns") or [] if isinstance(c, Mapping) and c.get("table") and c.get("column")]
    tables = {str(c["table"]) for c in refs}
    if len(tables) != 1:
        return None, "筛选条件不是只涉及一张表的列"
    (table,) = tables
    read = _read(candidate.get("dialect"))
    tree = _parse(condition, read)
    if tree is None:
        return None, f"多出的条件「{condition}」无法解析，无法确定是不是有效记录条件"
    scope = candidate.get("scope") if isinstance(candidate.get("scope"), Mapping) else {}
    grouped = {str(c).lower() for c in scope.get("columns") or [] if isinstance(c, str)}
    if hit := sorted({str(c["column"]) for c in refs if str(c["column"]).lower() in grouped}):
        return None, (f"多出的条件「{condition}」用到的「{'、'.join(hit)}」是原流程分组的列，可能是指标自己的筛选范围，"
                      "不是整张表的有效记录条件")
    texts = [str(candidate.get("metric") or ""), *(str(t) for t in scope.get("texts") or [] if isinstance(t, str))]
    if (word := _scope_hit(tree, texts)) is not None:
        return None, (f"多出的条件「{condition}」里的「{word}」出现在指标名称、口径说明、所在分组或本次运行的输入里，"
                      "可能是指标自己的筛选范围，不是整张表的有效记录条件")
    resolved = catalog.resolve_table_name(schema_cache, table) or table
    if not _drops_invalid(tree, notes.get(resolved) or {}):
        return None, (f"多出的条件「{condition}」不是在排除退款、作废、测试这类无效记录，可能是指标自己的筛选范围"
                      "（例如只看某个地区、某个渠道），不是整张表的有效记录条件")
    return (table, "valid_filter", _unqualified(condition, candidate.get("dialect"))), None


# ---- 有效记录条件：每一段都在排除无效记录

def _read(dialect: Any) -> str | None:
    from app.data.sqlcheck import dialect_of

    return dialect_of(dialect) if isinstance(dialect, str) else None


def _parse(condition: str, read: str | None) -> Any:
    import sqlglot

    try:
        return sqlglot.parse_one(condition, read=read)
    except Exception:  # noqa: BLE001 - 写法特殊、解析不了：调用方当作认不出
        return None


def _unparen(node: Any) -> Any:
    from sqlglot import exp

    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _and_parts(node: Any) -> list[Any]:
    """条件按 AND 拆成几段（括号拆开）。"""
    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, exp.And):
        return _and_parts(node.this) + _and_parts(node.expression)
    return [node]


def _literal(node: Any) -> tuple[bool, Any]:
    """常量的值：(是不是常量, 值)。字符串原样，数字转成 int / float，TRUE / FALSE 是 bool。"""
    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, exp.Boolean):
        return True, bool(node.this)
    negative = isinstance(node, exp.Neg)
    if negative:
        node = _unparen(node.this)
    if not isinstance(node, exp.Literal):
        return False, None
    if node.is_string:
        return (False, None) if negative else (True, str(node.this))
    try:
        number = float(node.this) if any(ch in str(node.this) for ch in ".eE") else int(node.this)
    except ValueError:
        return False, None
    return True, -number if negative else number


def _scope_hit(tree: Any, texts: list[str]) -> str | None:
    """条件里的取值出现在指标自己的范围里（指标名称、口径说明、分组、运行输入）：返回那个取值，没有返回 None。"""
    from sqlglot import exp

    lowered = [t.lower() for t in texts if t.strip()]
    for node in tree.find_all(exp.Literal):
        word = str(node.this).strip()
        if not word:
            continue
        if node.is_string:
            if any(word.lower() in t for t in lowered):
                return word
        elif len(word) >= _SCOPE_DIGITS:
            pattern = re.compile(rf"(?<![0-9]){re.escape(word)}(?![0-9])")
            if any(pattern.search(t) for t in lowered):
                return word
    return None


def _invalid_text(text: str) -> bool:
    lowered = text.lower()
    return any(w in _INVALID_WORDS for w in re.split(r"[^a-z0-9]+", lowered) if w) or \
        any(mark in text for mark in _INVALID_CJK)


def _invalid_value(value: Any, column: str, notes: Mapping[str, Any]) -> bool:
    """被排除的取值是不是无效记录：取值本身（'refunded'、'已作废'），或者码值表里它的意思（9 → 作废）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, str) and _invalid_text(value):
        return True
    columns = notes.get("columns") if isinstance(notes.get("columns"), Mapping) else {}
    entry = next((v for k, v in columns.items() if str(k).lower() == column.lower()), None)
    codes = entry.get("codes") if isinstance(entry, Mapping) else None
    if not isinstance(codes, Mapping) or codes.get("status") == "rejected" \
            or not isinstance(codes.get("value"), Mapping):
        return False
    key = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    meaning = codes["value"].get(key)
    return isinstance(meaning, str) and _invalid_text(meaning)


def _polarity(column: str) -> str | None:
    """标记列标记的是什么：invalid（is_test、deleted_at）、valid（is_valid、enabled），不是标记列为 None。"""
    words = catalog.name_words(column)
    if any(w in _MARKS_INVALID for w in words):
        return "invalid"
    if any(w in _MARKS_VALID for w in words):
        return "valid"
    return None


def _flag_value(value: Any) -> bool | None:
    """标记值：0 / 1 / TRUE / FALSE → bool，别的为 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _drops_invalid(tree: Any, notes: Mapping[str, Any]) -> bool:
    """条件的每一段都在排除无效记录。"""
    return all(_drops_invalid_part(part, notes) for part in _and_parts(tree))


def _drops_invalid_part(node: Any, notes: Mapping[str, Any]) -> bool:
    from sqlglot import exp

    node = _unparen(node)
    negated = False
    if isinstance(node, exp.Not):
        negated, node = True, _unparen(node.this)
    if isinstance(node, exp.NEQ):
        negated, node = not negated, exp.EQ(this=node.this.copy(), expression=node.expression.copy())
    if isinstance(node, exp.EQ):
        left, right = _unparen(node.this), _unparen(node.expression)
        column, other = (left, right) if isinstance(left, exp.Column) else (right, left)
        ok, value = _literal(other)
        return isinstance(column, exp.Column) and ok and _keeps_valid(column.name, [value], negated, notes)
    if isinstance(node, exp.In) and isinstance(_unparen(node.this), exp.Column) and not node.args.get("query"):
        values = [_literal(e) for e in node.expressions]
        return bool(values) and all(ok for ok, _ in values) and \
            _keeps_valid(_unparen(node.this).name, [v for _, v in values], negated, notes)
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null) \
            and isinstance(_unparen(node.this), exp.Column):
        # 列 IS NULL 留下的是没标记的行：标记无效记录的列（deleted_at IS NULL）才是有效记录；IS NOT NULL 反过来
        polarity = _polarity(_unparen(node.this).name)
        return polarity == ("valid" if negated else "invalid")
    if isinstance(node, exp.Column):
        # 布尔标记列直接当条件写：is_valid、NOT is_test
        return _polarity(node.name) == ("invalid" if negated else "valid")
    if isinstance(node, (exp.Like, exp.ILike)) and negated:
        ok, pattern = _literal(node.expression)
        return ok and isinstance(pattern, str) and _invalid_text(pattern)
    return False


def _keeps_valid(column: str, values: list[Any], negated: bool, notes: Mapping[str, Any]) -> bool:
    """列 = 取值（negated 时是 <>、NOT IN）留下的是不是有效记录。"""
    polarity = _polarity(column)
    flags = [_flag_value(v) for v in values]
    if polarity is not None and all(f is not None for f in flags):
        if len(set(flags)) != 1:
            return False
        # is_test = 1 留下的是标记了的行，is_test <> 1 留下的是没标记的；标记有效记录的列反过来
        keeps_marked = flags[0] != negated
        return keeps_marked if polarity == "valid" else not keeps_marked
    # 排除几个取值：被排除的都得是无效记录。只保留几个取值（status = 'paid'）就是指标自己的范围
    return negated and all(_invalid_value(v, column, notes) for v in values)


def _unqualified(condition: str, dialect: Any) -> str:
    """条件里的列去掉表名：有效记录条件记在这张表自己名下，写成 status <> 'refunded' 而不是 orders.status <> …。
    解析不了就原样。"""
    from sqlglot import exp

    read = _read(dialect)
    tree = _parse(condition, read)
    if tree is None or not any(col.table for col in tree.find_all(exp.Column)):
        return condition
    for col in tree.find_all(exp.Column):
        col.set("table", None)
    try:
        return tree.sql(dialect=read)
    except Exception:  # noqa: BLE001
        return condition


def _normalized(node: Any, read: str | None) -> str:
    """一段条件的比较用写法：列去掉表名、列名小写，按方言重新写一遍（<> 和 !=、NOT IN 的两种写法认成一样）。"""
    from sqlglot import exp

    copy = node.copy()
    for col in list(copy.find_all(exp.Column)):
        col.set("table", None)
        col.set("this", exp.to_identifier(col.name.lower()))
    return copy.sql(dialect=read)


def _conditions_of(sql: str, read: str | None) -> set[str] | None:
    """一条 SQL 里所有 WHERE 的每一段（比较用写法）；解析不了返回 None。"""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read=read)
    except Exception:  # noqa: BLE001
        return None
    return {_normalized(part, read) for where in tree.find_all(exp.Where) for part in _and_parts(where.this)}


def _keys(condition: Any, read: str | None) -> set[str] | None:
    """一句条件按 AND 拆开后每一段的比较用写法；不是文字、解析不了返回 None。"""
    tree = _parse(condition, read) if isinstance(condition, str) and condition.strip() else None
    return {_normalized(part, read) for part in _and_parts(tree)} if tree is not None else None


def _and_join(base: str | None, values: list[str], dialect: str | None, *, keep_base: bool = True) -> str | None:
    """几句条件用 AND 接成一句：已经有的段（base 里的、前面接过的，按 _normalized 比）不再接。一句里每一段都是新的，
    原样接上（保留原来的写法）；只有几段是新的，只接那几段（按方言重新写）——整句接上会把已有的段再写一遍。
    keep_base 为假时 base 只用来认哪些段已经有了、不写进结果（说「另外多出哪些条件」用），什么都没多出时为空串。
    不止一段时，最外层是 OR 的加括号，不然后面接的条件只并到 OR 的后一半上。有一句解析不了返回 None。"""
    from sqlglot import exp

    read = _read(dialect)
    pieces: list[tuple[str, bool]] = []  # (写法, 最外层是不是 OR)
    seen: set[str] = set()
    for i, text in enumerate([*([base] if base else []), *values]):
        tree = _parse(str(text), read)
        if tree is None:
            return None
        parts = _and_parts(tree)
        fresh = []
        for part in parts:
            key = _normalized(part, read)
            if key not in seen:
                seen.add(key)
                fresh.append(part)
        if not fresh or (base and i == 0 and not keep_base):
            continue
        if len(fresh) == len(parts):
            pieces.append((str(text).strip(), isinstance(tree, exp.Or)))
        else:
            pieces += [(part.sql(dialect=read), isinstance(part, exp.Or)) for part in fresh]
    if len(pieces) == 1:
        return pieces[0][0]
    return " AND ".join(f"({text})" if is_or else text for text, is_or in pieces)


def _combined_filter(existing: str, fact: Fact) -> tuple[str | None, str | None]:
    """目录里已有的有效记录条件和这次的新条件合起来：(合起来的整句条件, None) 或 (None, 为什么不动)。

    有效记录条件是一整句（copilot 提案也要求「原有条件也要带上」）。只写新条件会把两边都按着筛的原有条件静默替换掉；
    原有条件有一个被采纳的一边没按它筛，审核人的选择说明不了它对不对（合起来写就等于替人确认了它），也不能替人删掉。
    接的是每个指标各自的条件（不是并好的整句）：哪个指标的条件和已有的重了，那一段都只写一次。"""
    label = catalog.TABLE_FIELD_LABEL["valid_filter"]
    read = _read(fact.dialect)
    have = _keys(existing, read)
    combined = _and_join(existing, [str(m.value) for m in _members(fact)], fact.dialect) \
        if have is not None else None
    if have is None or combined is None:
        return None, f"「{fact.table}」已有的{label}「{existing}」无法解析，与这次的条件合不起来，未改动"
    for member in _members(fact):
        if not have <= (_conditions_of(member.sql, read) or set()):
            return None, (f"「{fact.table}」已有的{label}「{existing}」，指标「{member.metric}」被采纳的"
                          f"{SIDE_LABEL.get(member.choice, member.choice)}没有按它筛选，两者的关系无法确定，未改动。"
                          "如需更改，请到数据目录中修改")
    return combined, None


def _dedup(item: Mapping[str, Any], side: str, candidate: Mapping[str, Any],
           schema_cache: Mapping[str, Any] | None) -> tuple[tuple[str, str, Any] | None, str | None]:
    """聚合只差去不去重（同一个函数、同一个参数，一边写了 DISTINCT），被采纳的一边去重。表从被采纳那一边的 SQL 里认：
    参数列属于哪张实表。"""
    from app.data.sql_semantics import facets

    pair = _aggregate_pair(item.get("a"), item.get("b"), candidate.get("dialect"))
    if pair is None:
        return None, "两边的聚合不只是去不去重的差别"
    func, distinct = pair
    adopted = distinct[0] if side == "a" else distinct[1]
    if not adopted:
        return None, (f"被采纳的{SIDE_LABEL[side]}没有去重：不去重是这个指标自己的算法，"
                      "不是表的去重规则")
    sql = (candidate.get("sql") or {}).get(side) if isinstance(candidate.get("sql"), Mapping) else None
    found = facets(str(sql or ""), candidate.get("dialect"), schema=schema_cache)
    hits = [a for a in found.aggregates if a.func == func and a.distinct == adopted and not a.scope]
    columns = {ref for a in hits for ref in a.arg.columns}
    if len(columns) != 1:
        return None, "无法确定去重的是哪一张表的哪一列"
    ((table, column),) = columns
    if table is None:
        # 参数列没认出属于哪张表（没给表结构、列没写表名）：整条语句只查了一张表时就是它
        names = list(dict.fromkeys(t.text for t in found.tables))
        if len(names) != 1:
            return None, "无法确定去重的列属于哪一张表"
        table = names[0]
    return (str(table), "dedup", f"对 {column} {_VERB.get(func, func)}时去重"), None


def _aggregate_pair(a: Any, b: Any, dialect: Any) -> tuple[str, tuple[bool, bool]] | None:
    """两边的聚合写法 → (函数名, (A 去重吗, B 去重吗))；不是「同一个函数、同一个参数、只差 DISTINCT」返回 None。"""
    from sqlglot import exp

    read = _read(dialect)

    def shape(text: Any) -> tuple[str, str, bool] | None:
        if not isinstance(text, str) or not text.strip():
            return None
        node = _parse(text, read)
        if not isinstance(node, exp.AggFunc):
            return None
        arg = node.this
        distinct = isinstance(arg, exp.Distinct)
        if distinct:
            inner = arg.expressions
            if len(inner) != 1:
                return None
            arg = inner[0]
        if arg is None:
            return None
        return node.key, arg.sql(dialect=read), distinct

    left, right = shape(a), shape(b)
    if left is None or right is None or left[:2] != right[:2] or left[2] == right[2]:
        return None
    return left[0], (left[2], right[2])


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------


def note_of(fact: Fact, *, run_id: str, node_title: str) -> str:
    """目录项上的备注：来自哪次运行、哪个节点、哪些指标、各采纳了哪一边。"""
    parts = []
    for side, label in SIDE_LABEL.items():
        names = list(dict.fromkeys(m.metric for m in _members(fact) if m.choice == side))
        if names:
            parts.append(f"在指标{'、'.join(f'「{n}」' for n in names)}上采用了{label}的写法")
    return f"双盲复算（运行 {run_id}，节点「{node_title}」）：审核人" + "，".join(parts)


def _value_text(item: str, value: Any) -> str:
    if item == "business_date" and isinstance(value, Mapping):
        return str(value.get("column") or "")
    return str(value)


def _same_fact(item: str, a: Any, b: Any) -> bool:
    """目录里的一项和要写的是不是同一个说法：业务日期只看哪一列（规则、时区是补充），文字压掉多余的空白比。"""
    if item == "business_date":
        return isinstance(a, Mapping) and isinstance(b, Mapping) and a.get("column") == b.get("column")
    return isinstance(a, str) and isinstance(b, str) and " ".join(a.split()) == " ".join(b.split())


def _differing(facts: list[Fact]) -> str:
    """同一次裁决里对同一项给出的不同说法，说清是谁、各说了什么。"""
    first = facts[0]
    label = catalog.TABLE_FIELD_LABEL.get(first.item, first.item)
    said = "；".join(f"指标「{f.metric}」采纳的是「{_value_text(f.item, f.value)}」" for f in facts[:4])
    return (f"同一次裁决里对「{first.table}」的{label}给出了不同的说法（{said}），无法确定哪一个是数据事实，"
            "都未写入数据目录")


def _merged(members: list[Fact]) -> tuple[Fact | None, str | None]:
    """同一次裁决里落到同一项上的几个说法合成一项：(合起来的那一项, None) 或 (None, 为什么都不写)。只有一个时就是它。

    说法都一样的写一次。有效记录条件用 AND 合成一句（derive 只收排除无效记录的条件，几句叠起来不会互相打架）。
    业务日期一张表只有一个、去重规则说的未必是同一件事，说法不同就都不写。每个指标各自的那一项原样留在 members 里：
    驳回检查（_screen）、和目录里已有条件的合并（_combined_filter）都要按各自的说法算。"""
    first = members[0]
    if len(members) == 1:
        return first, None
    if first.item == "valid_filter":
        value = _and_join(None, [str(m.value) for m in members], first.dialect)
        return (replace(first, value=value, members=tuple(members)), None) if value is not None \
            else (None, _differing(members))
    if all(_same_fact(first.item, m.value, first.value) for m in members[1:]):
        return replace(first, members=tuple(members)), None
    return None, _differing(members)


def _rejected(notes: Mapping[str, Any], item: str) -> tuple[bool, Any]:
    """目录里这一项是不是被人工驳回的：(是不是, 驳回的说法)。"""
    stored = notes.get(item)
    if isinstance(stored, Mapping) and stored.get("status") == "rejected":
        return True, stored.get("value")
    return False, None


def _revives(fact: Fact, rejected: Any) -> bool:
    """要写的说法是不是又带回了曾被人工驳回的说法。业务日期、去重规则：同一个说法。有效记录条件：驳回的那句的每一段
    都在要写的条件里（按 _normalized 比，!= 和 <>、列名大小写、先后不同都认得出；有一边解析不了按原文比）——驳回的
    那句只是要写的一部分也算，写进去就等于替人把驳回过的条件又确认了一遍。"""
    if fact.item != "valid_filter":
        return _same_fact(fact.item, rejected, fact.value)
    read = _read(fact.dialect)
    mine, theirs = _keys(fact.value, read), _keys(rejected, read)
    if mine is None or theirs is None:
        return _same_fact(fact.item, rejected, fact.value)
    return bool(theirs) and theirs <= mine


def _rejected_text(fact: Fact, rejected: Any, members: Sequence[Fact]) -> str:
    """驳回检查拦下时的说法：驳回的是哪一句（照目录里记的原文），哪几个指标的选择又带回了它。审批卡上的预告和真写
    拦下时用的是同一句话。"""
    label = catalog.TABLE_FIELD_LABEL.get(fact.item, fact.item)
    return (f"「{fact.table}」的{label}「{_value_text(fact.item, rejected)}」曾被人工驳回，与这次指标{_names(members)}"
            "的选择相反，未写入。如需更改，请到数据目录中修改")


def _screen(notes: Mapping[str, Any], fact: Fact) -> tuple[Fact | None, list[Fact], list[str]]:
    """几个指标并成的一项，先拿掉自己采纳的说法曾被人工驳回的指标：(剩下的重新并成的一项, 拿掉的指标, 没写的原因)，
    一个都不剩、或者剩下的又合不起来时前者为 None；原因先是拿掉的那几个的（一条），剩下的合不起来时再跟一条。单个指标
    的、这一项没被驳回过的原样返回，由 _plan 照常判断。

    审批卡上每个指标的预告是按它自己的说法单独算的（preview → _plan），在那里被驳回检查拦下、卡上写着「不写」的，
    并进来以后也得拦下。只拿合起来的整句去比，「status <> 'refunded'」和别的条件 AND 在一起就不再等于驳回的那句，
    会被写成人工确认，和卡上说的正相反。拿掉的指标照实说原因（和它的预告同一句话），其余的照写，也和各自的预告一致。"""
    rejected, said = _rejected(notes, fact.item)
    if not rejected or not fact.members:
        return fact, [], []
    out: list[Fact] = []
    kept: list[Fact] = []
    for member in fact.members:
        (out if _revives(member, said) else kept).append(member)
    if not out:
        return fact, [], []
    reasons = [_rejected_text(fact, said, out)]
    if not kept:
        return None, out, reasons
    narrowed, why = _merged(kept)
    return narrowed, out, reasons if why is None else [*reasons, why]


async def record_choices(candidates: list[Mapping[str, Any]], *, run_id: str, node_title: str,
                         actor: str | None) -> dict[str, list[dict[str, Any]]]:
    """把审核人的选择写进数据目录：{written: [{source, table, item, value}], skipped: [{reason}]}。value 是真写进去的
    说法（和已有条件合起来的就是合起来的整句），审批卡上的预告说「会记为」的就是它。永远不抛：数据源没了、目录写不
    进去，都落在 skipped 里照实说。

    先把每个候选认成目录项，再按（数据源、表、项）归拢成一项（_merged：说法一样的、有效记录条件合起来写，说法
    不同的整组不写），逐项写（_write：按读到的目录先拿掉各自带回驳回说法的指标，其余的照写）。认目录项用的表结构、
    目录、上传表格导入时认作日期的列（upload_dates）都按写入时数据源的现状读。"""
    skipped: list[dict[str, Any]] = []
    facts: list[Fact] = []
    for candidate in candidates:
        name = str(candidate.get("source") or "")
        try:
            async with SessionLocal() as session:
                row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
                if row is None:
                    skipped.append({"reason": f"数据源「{name}」已不存在，指标「{candidate.get('metric')}」的选择未写入数据目录"})
                    continue
                notes = {table: entry.notes for table, entry in (await catalog.read_catalog(session, row.id)).items()}
                if "upload_dates" in candidate:
                    # 表结构是这里现读的，导入时认作日期的列也按写入时的当前版本重读：审批停着时换了版本（日期列改成了
                    # 文字），拿发起审批时读的那份去配新的表结构，会写出一条哪个版本单独看都不会给出的人工确认。候选里
                    # 没有这一项的是升级前发起的审批，卡上预告的是不写，照旧不认
                    candidate = {**candidate, "upload_dates": await upload_dates(session, row)}
                fact, why = derive(candidate, row.schema_cache, notes)
        except Exception:  # noqa: BLE001 - 目录读不出来不能毁掉审批的结果：缺口照样按选择记
            logger.exception("双盲复算的选择核对数据目录失败（数据源 %s）", name)
            skipped.append({"reason": f"指标「{candidate.get('metric')}」的选择核对数据目录时出错，未写入"})
            continue
        if fact is None:
            skipped.append({"reason": why})
        else:
            facts.append(fact)
    groups: dict[tuple[str, str, str], list[Fact]] = {}
    for fact in facts:
        groups.setdefault((fact.source, fact.table, fact.item), []).append(fact)
    written: list[dict[str, Any]] = []
    for members in groups.values():
        fact, why = _merged(members)
        if fact is None:
            skipped.append({"reason": why})
            continue
        value, reasons = await _record(fact, run_id=run_id, node_title=node_title, actor=actor)
        if value is not None:
            written.append({"source": fact.source, "table": fact.table, "item": fact.item, "value": value})
        skipped += [{"reason": r} for r in reasons]
    return {"written": written, "skipped": skipped}


async def _record(fact: Fact, *, run_id: str, node_title: str, actor: str | None) -> tuple[Any, list[str]]:
    """写一项：(写进去的说法，没写为 None, 没写的原因)。几个指标并成的一项可能写了、同时有几个指标被拿出来没写，原因
    照样交回。"""
    try:
        async with SessionLocal() as session:
            row = (await session.execute(select(DataSource).where(DataSource.name == fact.source))).scalar_one_or_none()
            if row is None:
                return None, [f"数据源「{fact.source}」已不存在，指标{_names(_members(fact))}的选择未写入数据目录"]
            return await _write(session, row, fact, run_id=run_id, node_title=node_title, actor=actor)
    except Exception:  # noqa: BLE001 - 目录写不进去不能毁掉审批的结果：缺口照样按选择记
        logger.exception("双盲复算的选择写入数据目录失败（数据源 %s）", fact.source)
        return None, [f"指标{_names(_members(fact))}的选择写入数据目录时出错，未写入"]


def _plan(notes: Mapping[str, Any], fact: Fact, tables: Mapping[str, Any] | None) -> tuple[Any, str | None]:
    """按目录里这张表现在的说法，算出这一项要写成什么：(要写的取值, None) 或 (None, 为什么不写)。不读库、不写库：
    真写（_write）和审批卡上的预告（preview）用的是这同一套判断。几个指标并成的一项，先由 _screen 拿掉各自带回驳回
    说法的指标再交到这里。"""
    label = catalog.TABLE_FIELD_LABEL.get(fact.item, fact.item)
    names = _names(_members(fact))
    stored = notes.get(fact.item) if isinstance(notes.get(fact.item), dict) else None
    rejected, said = _rejected(notes, fact.item)
    if rejected and _revives(fact, said):
        # 同一个说法以前被人明确驳回过：两次人工判断对不上，不替人拿主意。并成的一项各自的说法都不是驳回的那句、
        # 合起来却又包含了它，也在这里拦下
        return None, _rejected_text(fact, said, _members(fact))
    live = stored if stored is not None and not rejected else None
    value = fact.value
    if fact.item == "business_date" and live is not None and isinstance(live.get("value"), dict) \
            and live["value"].get("column") == value.get("column"):
        # 业务日期按哪一列是这次选出来的事实；目录里同一列另写了规则、时区的，原样留着
        value = live["value"]
    elif fact.item == "valid_filter" and isinstance(live, dict) and isinstance(live.get("value"), str) \
            and live["value"].strip() and not _same_fact(fact.item, live["value"], value):
        combined, why = _combined_filter(live["value"].strip(), fact)
        if combined is None:
            return None, why
        if live.get("status") == "confirmed" and not _same_fact(fact.item, combined, live["value"]):
            # 只说已确认的说法里没有的那几段：已有的段算成「多出」，审核人会以为连它也被改了
            extra = _and_join(live["value"].strip(), [str(m.value) for m in _members(fact)], fact.dialect,
                              keep_base=False)
            adopted = f"被采纳的{SIDE_LABEL.get(fact.choice, fact.choice)}" if not fact.members \
                else "这次几个指标被采纳的写法"
            return None, (f"「{fact.table}」的{label}已有人工确认过的说法「{live['value'].strip()}」，{adopted}"
                          f"另外多出条件「{extra or fact.value}」，未改动。如需补上，请到数据目录中修改")
        value = combined
    elif fact.item == "dedup" and isinstance(live, dict) and live.get("status") != "confirmed" \
            and not _same_fact(fact.item, live.get("value"), value):
        # 推断的去重规则说的未必是同一件事（按哪一列去重、哪种汇总要去重），替换掉就把它丢了
        return None, (f"「{fact.table}」的{label}已有推断的说法「{_value_text(fact.item, live.get('value'))}」，"
                      f"与这次指标{names}采纳的「{value}」不同，无法确定两者的关系，未覆盖。"
                      "如需更改，请到数据目录中修改")
    plan = catalog.plan_patch(notes, [{"path": fact.item, "value": value}], table=fact.table, tables=tables)
    if plan.problems:
        return None, f"「{fact.table}」的{label}未写入数据目录：{plan.problems[0]}"
    change = plan.changes[0]
    if change.state == "same":
        return None, f"「{fact.table}」的{label}已是人工确认过的「{_value_text(fact.item, value)}」，无需重复写入"
    if change.before_status == "confirmed":
        return None, (f"「{fact.table}」的{label}已有人工确认过的说法「{_value_text(fact.item, change.before)}」，"
                      f"与这次指标{names}采纳的「{_value_text(fact.item, value)}」不同，未覆盖。"
                      "如需更改，请到数据目录中修改")
    return change.after, None


def preview(candidate: Mapping[str, Any], schema_cache: Mapping[str, Any] | None,
            notes: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """审批卡上的预告：审核人在这个指标上选原流程（a）、选盲算（b），按目前的数据目录各会往目录里写什么。

    candidate 同 derive（choice 不用给）；notes 是这个数据源的目录（{表名: notes}）。返回 {a: …, b: …}，每边是
    {write: true, source, table, item, value}（value 是真要写进去的说法：和目录里已有的有效记录条件合起来的，是合起来
    之后的整句）或 {write: false, reason}；认出了事实、只是被目录挡下的不写，照样带上 source、table、item 和本来要写
    的 value（这个指标自己的说法）——卡上据此认出几个指标落到同一项上。这个指标单独落在这一项上时，reason 和真写时
    跳过的原因一字不差；和别的指标落到同一项上时看 preview_joint。永远不抛。"""
    notes = notes or {}
    tables = schema_cache.get("tables") if isinstance(schema_cache, Mapping) else None
    out: dict[str, dict[str, Any]] = {}
    for side in SIDE_LABEL:
        try:
            fact, why = derive({**candidate, "choice": side}, schema_cache, notes)
            value = None
            if fact is not None:
                value, why = _plan(notes.get(fact.table) or {}, fact, tables)
        except Exception:  # noqa: BLE001 - 预告算不出来不能拦住审批：照实说，以写入后的结果为准
            logger.exception("双盲复算预告数据目录的写入失败（指标 %s）", candidate.get("metric"))
            fact, why = None, f"指标「{candidate.get('metric')}」的选择核对数据目录时出错，无法预告是否写入"
        if fact is None:
            out[side] = {"write": False, "reason": why}
            continue
        where = {"source": fact.source, "table": fact.table, "item": fact.item}
        out[side] = {"write": True, **where, "value": value} if why is None else \
            {"write": False, **where, "value": fact.value, "reason": why}
    return out


def _joint_outcome(notes: Mapping[str, Any], members: list[Fact], tables: Mapping[str, Any] | None
                   ) -> dict[str, dict[str, Any]]:
    """几个指标落到同一项上、各按自己的选择并成一项时，每个指标的结果：{指标 ID: {write: true, value} 或 {write: false,
    reason}}。和 record_choices 逐步一样：_merged 归拢（说法不同的整组不写），_screen 拿掉带回驳回说法的（它们说驳回
    那一句，剩下的合不起来时说合不起来的那一句），剩下的按 _plan 对目录（写的是同一个值，不写的是同一句原因）。"""
    fact, why = _merged(members)
    if fact is None:
        return {m.metric_id: {"write": False, "reason": why} for m in members}
    kept, out, reasons = _screen(notes, fact)
    result = {m.metric_id: {"write": False, "reason": reasons[0]} for m in out}
    rest = [m for m in members if m.metric_id not in result]
    if kept is None:
        result.update({m.metric_id: {"write": False, "reason": reasons[-1]} for m in rest})
        return result
    value, why = _plan(notes, kept, tables)
    result.update({m.metric_id: {"write": False, "reason": why} if why is not None else {"write": True, "value": value}
                   for m in rest})
    return result


def _combos(sides: Sequence[int], limit: int) -> int:
    """几个指标落到同一项上时有几种组合（至少两个指标落上来的）：每个指标要么没落上来、要么落上来的那几边之一，
    ∏(1 + k) − 1 − Σk（k 是这个指标落到这一项上的边数，减去一个都没有、只有一个的）。连乘到超过 limit 能确定的地方
    就停，返回 limit + 1：只用来和上限比，不需要真算出几十亿。"""
    total, cap = 1, limit + 1 + sum(sides)
    for k in sides:
        total *= 1 + k
        if total > cap:
            return limit + 1
    return total - 1 - sum(sides)


def preview_joint(candidates: Sequence[Mapping[str, Any]],
                  catalogs: Mapping[str, tuple[Mapping[str, Any] | None, Mapping[str, Mapping[str, Any]]]]
                  ) -> list[dict[str, Any]]:
    """同一次裁决里几个指标落到同一项上时的预告：[{source, table, item, members: [{metric_id, choice}], outcomes:
    {指标 ID: {write: true, value} 或 {write: false, reason}}}]，每条是一种组合（members 是按各自的选择落到这一项上的
    指标，照候选的先后，至少两个）。

    candidates 照节点交给真写的先后（不一致的指标的先后），带 metric_id，不带 choice；catalogs 是 {数据源名: (表结构,
    {表名: notes})}，同 preview 的两个参数。每个指标的两边先各按 derive 认出落到哪一项（同 preview），落到同一项上的
    几个指标，每种「谁选了哪一边」的组合按 _joint_outcome 算一遍：真写时这几个指标就是这样被归拢、写或不写的。

    上限按每一项算：一项的组合数（_combos，先算个数，不先列出来）超过 JOINT_LIMIT 的，这一项整项不预告，别的项照常
    预告，几项之间不共用额度。一项不超过 JOINT_LIMIT 种、每个指标最多落到两项上，总的计算量和指标数成正比。
    永远不抛：算不出来的那一项不预告。"""
    from itertools import product

    landed: dict[tuple[str, str, str], dict[int, list[tuple[str, Fact]]]] = {}
    for index, candidate in enumerate(candidates):
        schema, notes = catalogs.get(str(candidate.get("source") or ""), (None, {}))
        for side in SIDE_LABEL:
            try:
                fact, _ = derive({**candidate, "choice": side}, schema, notes)
            except Exception:  # noqa: BLE001 - 认不出就当这一边落不到任何一项上，同 preview 照实不说
                fact = None
            if fact is not None:
                landed.setdefault((fact.source, fact.table, fact.item), {}).setdefault(index, []).append((side, fact))
    out: list[dict[str, Any]] = []
    for (source, table, item), by_metric in landed.items():
        if len(by_metric) < 2 or _combos([len(sides) for sides in by_metric.values()], JOINT_LIMIT) > JOINT_LIMIT:
            continue
        options = [[None, *sides] for _, sides in sorted(by_metric.items())]
        combos = [[c for c in combo if c is not None] for combo in product(*options)]
        combos = [c for c in combos if len(c) >= 2]
        schema, notes = catalogs.get(source, (None, {}))
        tables = schema.get("tables") if isinstance(schema, Mapping) else None
        try:
            entries = [{"source": source, "table": table, "item": item,
                        "members": [{"metric_id": fact.metric_id, "choice": side} for side, fact in combo],
                        "outcomes": _joint_outcome((notes or {}).get(table) or {}, [fact for _, fact in combo], tables)}
                       for combo in combos]
        except Exception:  # noqa: BLE001 - 预告算不出来不能拦住审批：这一项不预告，卡上有条件地说
            logger.exception("双盲复算预告几个指标落到同一项上的写入失败（%s 的 %s）", table, item)
            continue
        out.extend(entries)
    return out


async def _write(session: Any, row: DataSource, fact: Fact, *, run_id: str, node_title: str,
                 actor: str | None) -> tuple[Any, list[str]]:
    """写一项：(写进去的说法，没写为 None, 没写的原因)。几个指标并成的一项先按读到的目录拿掉各自带回驳回说法的指标
    （_screen），写什么、写不写再由 _plan 定；备注只列真写进去的指标。版本对不上（别人刚改过）重读再来。"""
    label = catalog.TABLE_FIELD_LABEL.get(fact.item, fact.item)
    tables = (row.schema_cache or {}).get("tables") if isinstance(row.schema_cache, dict) else None
    reasons: list[str] = []
    for _ in range(_RETRIES):
        entry = await catalog.read_entry(session, row.id, fact.table)
        notes = entry.notes if entry else {}
        version = entry.version if entry else 0
        kept, _, reasons = _screen(notes, fact)
        if kept is None:
            return None, reasons
        value, why = _plan(notes, kept, tables)
        if why is not None:
            return None, [*reasons, why]
        note = note_of(kept, run_id=run_id, node_title=node_title)
        at = catalog.now_iso()
        try:
            merged = catalog.apply_patch(notes, [{"path": kept.item, "value": value, "reason": note}],
                                         table=kept.table, tables=tables, at=at)
        except catalog.CatalogInvalid as e:
            return None, [*reasons, f"「{kept.table}」的{label}未写入数据目录：{e}"]
        # 不论原来是谁提的：这一项现在出自人的选择，记为人工填写、已确认，备注写明出处
        merged[kept.item] = catalog.make_item(value, "human", "confirmed", note=note, at=at)
        try:
            await catalog.write_entry(session, row.id, kept.table, merged, if_version=version, actor=actor)
        except catalog.CatalogConflict:
            await session.rollback()
            continue
        except catalog.CatalogInvalid as e:
            return None, [*reasons, f"「{kept.table}」的{label}未写入数据目录：{e}"]
        return value, reasons
    return None, [*reasons, f"「{fact.table}」的数据目录正被修改，{label}未能写入，请稍后在数据目录中确认"]


__all__ = ["FACT_ITEMS", "JOINT_LIMIT", "Fact", "derive", "note_of", "preview", "preview_joint", "recipe_dates",
           "record_choices", "upload_dates"]
