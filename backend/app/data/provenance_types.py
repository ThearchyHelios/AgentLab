"""证据下钻（期 4「推断的来源」）的契约：常量、取值、服务端原文、响应形状和各工作包之间传递的中间结构。

由编排者在第 0 步（WP-0）提交，各工作包只通过这里的类型和 P4-SPEC.md 第 7 节写明的函数签名交互，**不改签名**；
需要改的写进交付说明，由编排者改。这里只有声明，业务逻辑只有三个小函数（refuse、alert、contract_problems）：
接口、测试、前端夹具三处要用同一份原文和同一套不变式。

**为什么只用标准库**：识别器（engine/direct_select.py）只许导入守卫、names 和这个模块（P4-SPEC 7.2），这里一旦
导入 recipe_types、pydantic，判据就被拖进整套配方模块。和 recipe_types 重名的取值（核对状态）在契约测试里钉住一致。

**响应形状**（P4-SPEC 2.8.1）：`dataclasses.asdict(ProvenanceOut(...))` 就是接口返回的 JSON，键名、顺序和规格逐字
一致。响应用的 dataclass 一律 kw_only：字段多、并行的几个包各自构造，按位置传参错一位不会报错，按名字传错了立刻报。
中间结构（Chain、DirectSelect 等）照规格的写法可以按位置构造。

**唯一的改名**：CellSource 的「from」是 Python 关键字，不能当属性名。Python 里写 `from_`（构造、读写都用它），
`asdict` / `fields` / `dataclasses.replace` / `typing.get_type_hints`、pydantic 和 JSON 里一律是「from」（见 _json_keys）。
构造时也认 `**{"from": …}`，从 JSON 读回来可以直接 `CellSource(**d)`。

**路由怎么返回**（WP-C）：照现有证据路由的写法，`return dataclasses.asdict(out)`，返回注解写 `dict[str, Any]`。
把 ProvenanceOut 写成返回注解或 `response_model` 也得到同样的 JSON（pydantic 按 `__annotations__` 收集 dataclass
字段，_json_keys 把注解里的名字也改成了「from」，契约测试钉住 TypeAdapter 和 FastAPI 带类型路由的输出等于 asdict），
但不推荐：那样每次响应都要按类型再校验、再序列化一遍，与其余证据路由不一致。

**文档标记**（P4-SPEC 1.4）：compose_doc 组装的新文档顶层写 `"provenance": DOC_PROVENANCE`。识别器的文法、
`tables_in` 的读法都绑在这个版本号上；以后放宽文法要升版本号，已有 `provenance: 1` 的文档仍按 1 的文法判。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

_log = logging.getLogger(__name__)

# ==========================================================================
# 版本与常量
# ==========================================================================

#: 文档标记的版本号：带这个标记的文档才走期 4 的行为（查询步骤的提示字段、推断来源接口、裁判摘录的新行）
DOC_PROVENANCE = 1
#: 推断来源接口响应的 schema
SCHEMA = "agentlab.provenance/1"

#: 裁判摘录（P4-SPEC 3.2）：来源的分期行最多几期（取最近的几期，其余写「另有 n 期」）
PROV_PARTS = 3
#: 已接受的理由最多几条
PROV_ACCEPT = 4
#: 区域外文字最多几格（每条查询）
PROV_OUTSIDE = 4
#: 区域外文字每格最多几个字，超出写「…（已截断）」
PROV_OUTSIDE_CHARS = 200
#: 列的说明最多几行
PROV_COLUMN_NOTES = 6
#: 整块来历（来源到区域外文字，含口径）最多几个字，超出按 3.2 的五步顺序删
PROV_CHARS = 1200

# ==========================================================================
# 取值
# ==========================================================================

#: inferred：给出格子；table_only：只给表级来历（version 非空），附原因；none：什么都不画，或只画一句原因 / 红色提示
Status = Literal["inferred", "table_only", "none"]

#: 不下钻的原因（P4-SPEC 2.3 的规则表、2.4 的识别器分组）。同一个 code 可能出现在不同 status 下：
#: chain_mismatch 在 R6（清单链本身对不上）是 none，在 R13、R17 和异常映射里是 table_only
ReasonCode = Literal[
    # R0：文档没有标记（期 4 之前组装的）
    "legacy_doc",
    # R1：不是直接引用的查询单元格（指标、实体、代码节点和 Agent 字段的取数）
    "not_cell",
    # R2 / R4：查询快照或表结构快照不在本次运行的事件里、取回复验不通过
    "not_sealed",
    # R3：查询快照没有 data_version（手工源）
    "not_upload",
    # R4：表结构快照不是按配方导入的（简单导入、v0 迁移出的版本）
    "simple_upload",
    # R5（红）：清单取不回来或哈希不符
    "manifest_unreadable",
    # R6 / R13 / R17 / 异常映射第 1 条（红）
    "chain_mismatch",
    # R7 / R8 / R9 / R15：SQL 判据（2.4 的分组，细分写在 detail，取值见 DETAILS）
    "expression", "alias", "multi_table", "unparsed",
    # R9
    "no_pk", "pk_missing",
    # R10
    "masked",
    # R11
    "null_value", "null_pk",
    # R12 / 异常映射第 3 条
    "snapshot_gone",
    # 异常映射第 2 条、R16 强制重算后哈希不等（红）
    "db_tampered",
    # R16
    "recheck_missing", "recheck_multiple", "recheck_mismatch",
    # R17
    "no_lineage",
]

#: 标红的提示。出现时 reason.code 与它相同，界面只画提示、不再画 reason.text（P4-SPEC 4.1、4.2 末尾）
AlertCode = Literal["db_tampered", "chain_mismatch", "manifest_unreadable"]

#: SQL 判据的拒绝分组（DirectSelect / SelectRefusal，P4-SPEC 2.4）
RefusalCode = Literal["expression", "alias", "multi_table", "unparsed"]

#: 每个分组允许的细分（Reason.detail / SelectRefusal.detail）。duplicate_pk（R9）、authorizer、compound（R15）
#: 由接口一侧补上，同样归 multi_table。界面只显示分组的原文，detail 给测试和日志用。
#:
#: 识别器（WP-A）的每个拒绝分支都必须落在这里，测试对每个拒绝用例断言 `detail in DETAILS[code]`。原型
#: p4-probe/probe_recognizer.py 返回的 expression_or_alias、subquery_or_compound、table_shape 之类是原型自己的名字，
#: 移植时要逐个映射，不能照抄。2.4 没逐条写的几种按文法归：
#:   INDEXED BY（表名后面的词被当成省略 AS 的表别名，再往后不是 WHERE / ORDER / LIMIT）→ unparsed · tail_shape；
#:   表函数（表名紧跟「(」）→ unparsed · table_shape；FROM 后面直接是「(」（子查询）→ multi_table · subquery；
#:   带库名的表（main."日客流"）→ unparsed · qualified_table；聚合、函数调用、运算 → expression · expression。
#: 漏了映射时 refuse 不抛异常（照样给分组的原文，细分原样留下、记一条警告），contract_problems 会报出来
DETAILS: dict[str, tuple[str, ...]] = {
    "expression": ("expression", "string_literal", "literal_keyword", "window", "collate", "distinct"),
    "alias": ("alias",),
    "multi_table": ("join", "comma_join", "subquery", "cte", "compound", "authorizer", "duplicate_pk"),
    "unparsed": ("not_select", "multi_statement", "table_shape", "qualified_table", "qualifier", "star_mismatch",
                 "unknown_table", "unknown_column", "ambiguous_column", "tail_shape"),
}

#: 单期是配方的 mode，并集是快照清单的 mode
Mode = Literal["replace", "accumulate"]
#: 表的种类：冻结表结构里 kind 缺省（None）就是 data，reported_total 是原表写明的合计
TableKind = Literal["data", "reported_total"]
#: 回执里列的角色（recipe_types.ColumnOut.role）
ColumnRole = Literal["axis", "dim", "derive", "const", "measure", "value", "text"]
#: 附带的格（FromCell.role）：日期表头格、行标签、分段标题、列表头、合计标签（含回执里的 derived_label）
FromRole = Literal["axis_header", "row_label", "section_title", "col_header", "total_label"]
#: 原件状态（table_imports.raw_state，当前状态）
RawState = Literal["kept", "purged", "absent"]
#: 接受的种类：overrides（数据质量类不成立）→ override；waivers（无法核对）→ waiver
AcceptanceKind = Literal["override", "waiver"]
#: 本期结论：照搬清单里 CheckResult.status（与 recipe_types.CheckStatus 相同，契约测试钉住）
PartStatus = Literal["passed", "mismatch", "unverifiable", "info"]
#: 这一行的关系核对结果（只给关系核对）
RowStatus = Literal["passed", "mismatch", "unverifiable"]
#: 这一格的合计核对结果（只给 K、G、T）
CellStatus = Literal["ok", "unverifiable", "unknown", "not_formula"]
#: 年份取自哪里：统计期从格子解析（period）、人工录入（human）
YearFrom = Literal["period", "human"]

#: loader 的语义同 artifact_store.load：取不到返回 None，哈希不符抛 ValueError
Loader = Callable[[str], Any]

# ==========================================================================
# 服务端原文（P4-SPEC 4.2）：界面原样显示 reason.text、alert.text，不另写
# ==========================================================================

_RECHECK_TEXT = "按主键回查数据文件，结果与查询结果不一致，不推断来源"

#: code → 原文。pk_missing 里的「{列}」由 refuse 用 fmt 填（没带齐的主键列，多列用「、」连）
REASON_TEXT: dict[str, str] = {
    "legacy_doc": "这份报告生成于系统升级前，不推断原表格子",
    # not_cell、not_upload 界面不画（P4-SPEC 4.1 渲染矩阵），原文只给日志和测试，规格的原文表里没有，由 WP-0 补
    "not_cell": "这一处不是直接引用的查询单元格，不推断原表格子",
    "not_sealed": "查询快照或表结构快照不在本次运行的记录里，不推断来源",
    "not_upload": "这次查询的数据源不是上传的表格，不推断原表格子",
    "simple_upload": "这份表格按表头行直接导入，没有记录原表格子的溯源",
    "manifest_unreadable": "导入清单无法读取或与哈希不一致，无法给出来历",
    # 规格原文是「…可能被修改过：只显示导入清单记录的来历」。chain_mismatch 也用于 R6（清单链本身对不上，
    # version 为 null，什么来历都不显示），那句后半句在 R6 时不属实，所以去掉；table_only 时数据版本一节照样画出来，
    # 不需要再说（交付说明里写明）
    "chain_mismatch": "数据文件的登记与导入清单不一致，可能被修改过",
    "db_tampered": "数据文件与登记的版本不一致，可能被修改过：只显示导入清单记录的来历",
    "snapshot_gone": "这一版的数据文件已回收或不存在，无法按主键回查，只显示导入清单记录的来历",
    "expression": "这一格是计算、聚合或改写的结果，只给出表级来历",
    "alias": "查询给列改了名，无法确认这一格对应哪一列，只给出表级来历",
    "multi_table": "查询涉及多张表、同一张表多次，或含子查询、公用表表达式、集合运算，只给出表级来历",
    "unparsed": "查询的写法超出可追溯的范围，只给出表级来历",
    "no_pk": "这张表没有主键，无法按主键回查，只给出表级来历",
    "pk_missing": "查询结果没有带齐主键列（{列}），无法按主键回查，只给出表级来历",
    "masked": "这一格涉及设置了遮罩的列，不推断来源",
    "null_value": "这一格是空值，不推断来源",
    "null_pk": "这一行的主键是空值，无法按主键回查",
    "recheck_missing": _RECHECK_TEXT,
    "recheck_multiple": _RECHECK_TEXT,
    "recheck_mismatch": _RECHECK_TEXT,
    "no_lineage": "导入清单里没有这一格的溯源记录，只给出表级来历",
}

#: 标红提示的原文：与同名 reason 的原文相同（两处同时出现时界面只画提示）
ALERT_TEXT: dict[str, str] = {code: REASON_TEXT[code] for code in get_args(AlertCode)}

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
#: 回查 SQL 里双引号括起的标识符（内部的双引号写两个）：数 `?` 占位符之前先去掉，列名里的问号不算
_QUOTED = re.compile(r'"(?:[^"]|"")*"')


# ==========================================================================
# 响应（GET /api/runs/{run_id}/evidence/segments/{segment_id}/provenance，P4-SPEC 2.8.1）
# ==========================================================================


def _stored_as(py: str, js: str) -> property:
    """Python 名字 `py` 的属性：读写实例字典里的 `js` 键（_json_keys 用）。没有这个键时抛 AttributeError，hasattr 照常。"""
    def get(self: Any) -> Any:
        try:
            return self.__dict__[js]
        except KeyError:
            raise AttributeError(py) from None

    def set_(self: Any, value: Any) -> None:
        self.__dict__[js] = value

    return property(get, set_)


def _json_keys(**aliases: str) -> Callable[[type], type]:
    """dataclass 的字段在 Python 里叫 `py`，在 asdict / fields / replace / get_type_hints / pydantic / JSON 里叫 `js`
    （只给 CellSource.from 用）。

    做法：dataclass 照常按 `py` 生成 __init__、__repr__、__eq__；生成之后
    - 字段表 `__dataclass_fields__` 和注解 `__annotations__` 里的名字都改成 `js`（顺序不变）。只改字段表的话，
      pydantic 按注解收集字段，对不上的直接跳过：FastAPI 带类型的路由、TypeAdapter 的输出会悄悄少掉「from」；
    - 值存在实例字典的 `js` 键下，`py` 是读写它的属性。pydantic 从 JSON 校验出实例时直接按字段名填实例字典，
      不经过 __init__，存在 `py` 下的话这样得到的实例读 `from_` 会抛 AttributeError；
    - __init__ 也认 `js` 这个关键字（dataclasses.replace、`Cls(**json)` 传的是它）。两个都传时以 `py` 为准：
      `replace(obj, from_=新值)` 会同时带上旧的 `from`。
    """
    def wrap(cls: type) -> type:
        fields_: dict[str, Any] = cls.__dataclass_fields__  # type: ignore[attr-defined]
        renamed: dict[str, Any] = {}
        for name, f in fields_.items():
            if name in aliases:
                f.name = aliases[name]
            renamed[f.name] = f
        cls.__dataclass_fields__ = renamed  # type: ignore[attr-defined]
        cls.__annotations__ = {aliases.get(k, k): v for k, v in cls.__dict__["__annotations__"].items()}
        for py, js in aliases.items():
            setattr(cls, py, _stored_as(py, js))
        generated = cls.__init__

        def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
            for py, js in aliases.items():
                if js in kwargs:
                    value = kwargs.pop(js)
                    kwargs.setdefault(py, value)
            generated(self, *args, **kwargs)

        __init__.__doc__ = generated.__doc__
        __init__.__qualname__ = f"{cls.__qualname__}.__init__"
        cls.__init__ = __init__  # type: ignore[method-assign]
        return cls
    return wrap


@dataclass(kw_only=True)
class ReportRef:
    #: 报告节点 id
    node_id: str
    #: 报告文档的工件 id
    doc_artifact: str


@dataclass(kw_only=True)
class CellRef:
    """片段本身是单元格引用（cite.kind == "cell"，有 alias 和 locator）就给，与 status 无关（legacy_doc 时也给）。"""

    #: 目录里的查询编号（Q3）
    alias: str
    #: 快照里的行号，从 0 数（locator.row）
    row: int
    #: 被引用的结果列名（locator.column）
    column: str
    #: 目录条目的 artifact（查询快照 id）；条目没记时 None
    artifact: str | None = None


@dataclass(kw_only=True)
class Reason:
    """status 不是 inferred 时必有。用 refuse() 构造，原文取自 REASON_TEXT。"""

    code: ReasonCode
    #: 细分（DETAILS 里的值、ChainProblem / LocateProblem 的 detail）；没有细分为空串
    detail: str = ""
    text: str


@dataclass(kw_only=True)
class Alert:
    """标红的提示。用 alert() 构造，原文取自 ALERT_TEXT。"""

    code: AlertCode
    text: str


@dataclass(kw_only=True)
class TableRef:
    """SQL 用到的表：direct_select.tables_in 与冻结表结构的交集，按第一次出现的顺序。"""

    name: str
    kind: TableKind = "data"


@dataclass(kw_only=True)
class PeriodView:
    """一期的统计期（导入清单的 period）。"""

    start: str
    end: str
    source: Literal["cells", "human"]
    #: 解析出统计期的格子，带工作表名（「客流汇总!B2」）；人工录入时为空
    cells: list[str] = field(default_factory=list)
    #: 人工录入时的署名（未认证）
    signed_by: str | None = None


@dataclass(kw_only=True)
class AcceptanceView:
    """一条接受理由（导入清单 acceptances 的一项，内容寻址，是当时的事实）。

    不叫 Acceptance：recipe_types.Acceptance 已有同名类，字段不同。
    """

    check_id: str
    #: 同一份清单 checks 里 id 相同的那条的 title；找不到时 None
    title: str | None
    kind: AcceptanceKind
    reason: str
    signed_by: str | None = None
    #: 清单里这条接受的 at；老清单没有时 None
    at: str | None = None


@dataclass(kw_only=True)
class StateNote:
    """导入记录上的当前状态（清除原件、作废接受）：table_imports.purged / revoked 里的 at、signed_by、reason。"""

    at: str | None = None
    signed_by: str | None = None
    reason: str | None = None


@dataclass(kw_only=True)
class PartView:
    """数据版本里的一期（按统计期升序）。前半来自导入清单（内容寻址），raw_state 起的三项来自导入记录（当前状态）。

    version_view（WP-B）只填清单里的那部分：recipe_seq、raw_state、purged、revoked 留 None，has_row 全 False，
    由接口一侧（WP-C）从数据库补、按格子所在的一期标 has_row。
    """

    import_id: str
    #: 导入记录的 seq（同 ChainPart.seq）
    seq: int
    #: 导入清单的工件 id（「查看导入清单」按它经工件接口取）
    manifest: str
    #: 清单没有统计期时 None（界面写「统计期未记录」）
    period: PeriodView | None
    file_name: str | None
    raw_sha256: str | None
    #: S9：回执 regions 里块内各角色区域的外接矩形，按工作表（「客流汇总!B4:AG30」），多张表用「、」连；取不到为 None
    region: str | None = None
    #: S9：回执 rows_excluded 的行数之和；期 3 之前的回执没有这个键时为 None
    excluded_rows: int | None = None
    recipe_sha256: str | None = None
    #: 配方是第几版：取自配方记录（当前状态），取不到为 None
    recipe_seq: int | None = None
    #: 清单的 created_at
    committed_at: str | None = None
    #: 清单的 signed_by.name（署名，未认证）
    signed_by: str | None = None
    #: 这一期的全部接受理由（不只是相关的）：先 overrides，再 waivers，各按清单里的顺序
    acceptances: list[AcceptanceView] = field(default_factory=list)
    raw_state: RawState | None = None
    purged: StateNote | None = None
    revoked: StateNote | None = None
    #: 这一格所在的一期：inferred 时恰好一项为 True，其余 False
    has_row: bool = False


@dataclass(kw_only=True)
class VersionView:
    """数据版本（表级来历）：status 为 none 时整节为 null。"""

    #: 查询快照记的数据源名
    source: str
    #: 查询快照的 data_version
    snapshot_id: str
    mode: Mode
    #: 累积并集（表结构快照有 snapshot_manifest）
    union: bool
    tables: list[TableRef] = field(default_factory=list)
    #: 能不能在面板里查看导入清单：数据源设有任何遮罩（当前设置 ∪ 快照记录）时 False。
    #: version_view（WP-B）不知道遮罩，一律给 True，由接口一侧（WP-C）改
    manifest_view: bool = True
    parts: list[PartView] = field(default_factory=list)


@dataclass(kw_only=True)
class FromCell:
    """这一行其余键列各取自哪一格（日期表头格、行标签格、分段标题格、列表头、合计标签）。"""

    role: FromRole
    #: 这一格给出的是哪一列的值；宽表指标列的行标签（指标名取自哪一格）为 None
    column: str | None
    sheet: str
    #: 不带工作表名的 A1 坐标（「G4」）
    cell: str
    #: 只放格子原文：行标签取 labels[].raw，合计标签取 DerivedItem.label_raw；拿不到为 None，不拿规范写法冒充
    text: str | None = None
    #: 只给 section_title：配方里的定位文字 locate.title（原表里的字可以不同）
    locate_title: str | None = None


@dataclass(kw_only=True)
class YearSource:
    """年份取自哪里：交叉表的轴是文本日期（text / mixed）且配方 axis.year_from 有值时才有，其余为 null。"""

    source: YearFrom
    #: period：统计期的格子（带工作表名）；human：空
    cells: list[str] = field(default_factory=list)
    #: human：录入统计期时的署名（未认证）；period：None
    signed_by: str | None = None
    #: 这一块的日期表头有的是日期格、有的是文本（AxisOut.form == "mixed"），逐格判断不了
    mixed: bool = False


@dataclass(kw_only=True)
class CanonicalView:
    """这一行的标签原文和数据库里的值（规范写法）不同时给出（P4-SPEC 2.5 dim 一行、6.2 A8、A18），否则为 null。

    比的是这一行的**标签列**：交叉表逆透视出的 dim 列（原文取自 labels[].raw）、合计表的合计项列（原文取自
    DerivedItem.label_raw）。raw 是那一格的原文，canonical 是这一行该列在数据库里的值（即 pk 里那一列的值）。
    **被引用的是值列也给**：A8 合计表那一格是 {"raw": "18-22 时合计", "canonical": "18-22时合计"}，累积并集里全角
    标签那一期的值格是 {"raw": "８－９", "canonical": "8-9"}；被引用的就是标签列时同样给（A18）。
    宽表指标列的行标签是指标名，不是哪一列的值，不给；拿不到逐行原文的（列表形态没有 SegmentLabels）不给。
    """

    raw: str
    canonical: str


@dataclass(kw_only=True)
class Recheck:
    """按主键参数化回查数据文件的那一句（推断出格子时 ok 恒为 True，不一致的走 table_only）。

    **占位符用 `?`（位置参数），不用 `:p0`**（规格 2.3 R16 写的 `:p0` 以这里和 2.8.1 为准）：
    `SELECT rowid, "列" FROM "表" WHERE "主键1" = ? AND "主键2" = ?`。标识符一律双引号、内部的双引号写两个；
    WHERE 按冻结表结构 primary_key 的顺序逐列 `= ?` 用 AND 连；params 与占位符一一对应，同样按 primary_key 的顺序
    （等于 CellSource.pk 的值的顺序）。面板「技术细节」原样显示 sql 和 params，两者必须对得上；provenance_db.recheck
    返回的 (行, sql, params) 就是这里的 sql、params。
    """

    sql: str
    #: 与 sql 里的 `?` 按位置一一对应（primary_key 的顺序）
    params: list[Any] = field(default_factory=list)
    ok: bool


@_json_keys(from_="from")
@dataclass(kw_only=True)
class CellSource:
    """推断出的格子：只在 inferred 时有，否则 null。

    locate（WP-B）填格子、附带的格、年份、规范写法、merged_fill、part_seq、part_rowid；pk、rowid、raw_purged、recheck
    由接口一侧（WP-C）回查后填。Python 里「from」写成 from_，JSON 里是「from」（见模块说明）。
    """

    table: str
    column: str
    column_role: ColumnRole
    kind: TableKind = "data"
    #: 主键列 → 值（查询快照里那一行的值），键按冻结表结构 primary_key 的顺序（Recheck.params 同序）
    pk: dict[str, Any] = field(default_factory=dict)
    #: 快照库的 rowid（并集时是并集 rowid，不是 part_rowid）
    rowid: int | None = None
    #: 格子所在的那一期的 seq（导入记录的 seq，同 ChainPart.seq）和该期构建库里的 rowid（单期时 part_rowid == rowid）
    part_seq: int
    part_rowid: int
    sheet: str
    #: 不带工作表名的 A1 坐标（「G5」）
    cell: str
    #: 取自 receipt.tables 里这一列的 header、unit；没有为 None
    header: str | None = None
    unit: str | None = None
    from_: list[FromCell] = field(default_factory=list)
    year: YearSource | None = None
    canonical: CanonicalView | None = None
    #: 这一期的原件已清除（导入记录的 raw_state == "purged"，当前状态）
    raw_purged: bool = False
    #: 列表这一块的配方写了 merged_data == "fill"：值可能取自合并区域的左上格
    merged_fill: bool = False
    recheck: Recheck | None = None


@dataclass(kw_only=True)
class RelatedCheck:
    """相关核对的一条（进响应，不含行级规则：规则只在服务端内部流转，见 RelatedCheckPlan）。"""

    #: 核对 id（R1、K1、G1、T1、C1…）
    id: str
    #: CheckResult.kind（relation_sum_eq、relation_not_comparable、derived_sum、formula_refs、column_sum、context_agree…）
    kind: str
    title: str
    part_status: PartStatus
    #: 只给关系核对（relation_sum_eq）；列在当前目标表结构里不存在时 None
    row_status: RowStatus | None = None
    #: 只给 K、G、T
    cell_status: CellStatus | None = None
    #: 挂在这条核对上的接受理由（同一期清单 acceptances 里 check_id 相同的那条）
    acceptance: AcceptanceView | None = None
    #: K 无法核对时的原因摘要：CheckResult.details 的第一条，可能带数字（系统呈现，不经模型转述）
    detail: str | None = None


@dataclass(kw_only=True)
class ProvenanceOut:
    """推断来源接口的响应。asdict 之后就是 P4-SPEC 2.8.1 的 JSON；不变式见 contract_problems。"""

    schema: str = SCHEMA
    report: ReportRef
    #: 片段 id
    segment: str
    cell: CellRef | None = None
    status: Status
    reason: Reason | None = None
    alert: Alert | None = None
    #: 运行已封存且封存核对通过（同 sealed.trusted）。False 时照常给结论，界面加一句未封存
    sealed: bool
    version: VersionView | None = None
    cell_source: CellSource | None = None
    checks: list[RelatedCheck] = field(default_factory=list)


# ==========================================================================
# 跨包的中间结构（P4-SPEC 7.2–7.4）
# ==========================================================================


@dataclass
class DirectSelect:
    """识别器认可的直接选取（WP-A recognize 的结果）。"""

    #: 冻结表结构里的表名（原样）
    table: str
    alias: str | None
    #: 第 i 个结果列对应的表列名（原样，已按 name_key 解析）；* 已展开
    columns: list[str]


@dataclass
class SelectRefusal:
    """识别器拒绝：code 是 2.4 的分组，detail 必须是 DETAILS[code] 里的细分值（映射见 DETAILS 上面的说明）。"""

    code: RefusalCode
    detail: str


@dataclass
class ChainPart:
    #: 导入记录的 seq（清单里的 seq、快照清单 parts[].seq），不是在 parts 里的位置：每期替换的第二期只有一项，seq 是 2。
    #: 界面和摘录里的「第 k 次导入」、CellSource.part_seq、PartView.seq 都是它
    seq: int
    import_id: str
    manifest_id: str
    #: 已取回的导入清单
    manifest: dict[str, Any]
    #: 表 → [union 起, 止, part 起, part 止]（快照清单 parts[].rows 摊平）；单期为 None
    union_rows: dict[str, list[int]] | None


@dataclass
class Chain:
    """查询快照 → 表结构快照 → 清单（resolve_chain 的结果，P4-SPEC 2.2）。"""

    #: 查询快照记的数据源名
    source: str
    #: 清单里的 source_id（与 source_snapshots.source_id 交叉核对，R13）
    source_id: str
    #: 查询快照的 data_version
    snapshot_id: str
    mode: Mode
    union: bool
    #: 单期：导入清单的 db_sha256；并集：快照清单的 union.db_sha256
    expected_db_sha256: str
    #: 按统计期升序
    parts: list[ChainPart]
    #: 冻结的表结构快照（已取回）
    schema: dict[str, Any]
    #: 快照清单（并集才有）
    snapshot_manifest: dict[str, Any] | None


@dataclass
class ChainProblem:
    """resolve_chain 失败：取不回或哈希不符 → manifest_unreadable（R5）；链本身对不上 → chain_mismatch（R6）。"""

    code: Literal["manifest_unreadable", "chain_mismatch"]
    #: 给测试和日志的细分；规格里写的是 ChainProblem("manifest_unreadable") 这种只给 code 的构造，所以有缺省
    detail: str = ""


@dataclass
class Located:
    """locate 的结果：格子所在的一期、该期的 rowid、推断出的格子（rowid、pk、recheck 留给 WP-C 填）。"""

    part: ChainPart
    part_rowid: int
    cell: CellSource


@dataclass
class LocateProblem:
    """locate 失败：找不到溯源段、锚格所在的块、轴或分段标题 → no_lineage；并集 rowid 越界 → chain_mismatch（红）。"""

    code: Literal["no_lineage", "chain_mismatch"]
    detail: str = ""


@dataclass
class SumEqRule:
    """关系核对的行级规则：只在服务端内部流转，不进响应。WP-C 据此用 sum_eq_conditions 拼 SQL、按快照库的 rowid 跑。"""

    table: str
    total: str
    parts: list[str]
    #: 列 → INTEGER / REAL / TEXT（取自格子所在那一期的 receipt.tables）
    types: dict[str, str]


@dataclass
class RelatedCheckPlan:
    """related_checks 的一项：check 进响应（row_status 由 WP-C 填），rule 是关系核对的行级规则（其余核对为 None）。"""

    check: RelatedCheck
    rule: SumEqRule | None


@dataclass
class CompileFacts:
    """R15 的第二道核对（EXPLAIN 编译、不执行）：授权回调读到的表、字节码里 ResultRow 和 Yield 的个数。"""

    tables: set[str]
    result_rows: int
    yields: int


# ==========================================================================
# 小函数
# ==========================================================================


def refuse(code: ReasonCode, detail: str = "", **fmt: Any) -> Reason:
    """按 REASON_TEXT 生成 Reason。

    pk_missing 的原文里有「{列}」，用 fmt 填：`refuse("pk_missing", 列=["日期", "时段"])` →「…主键列（日期、时段）…」，
    列表、元组按「、」连。建议填没带齐的那几列。

    code 不认识、原文里有占位符却没给对应的 fmt，抛 ValueError：两者都是调用处写死的常量，那个分支的任何一条测试都会
    暴露，不该带着半截原文上界面。

    SQL 判据的四个分组带了 DETAILS 之外的细分**不抛**：细分来自识别器对这条 SQL 的判断，取决于数据，没被测试覆盖
    的写法（INDEXED BY、表函数之类）漏了映射时，抛异常会让推断来源接口回 500，而不是只给表级来历。这时照样按分组给
    原文，细分原样留下，记一条警告；contract_problems 会把它报出来，接口和验收测试断言 contract_problems 为空就能抓到。
    """
    if code not in REASON_TEXT:
        raise ValueError(f"不认识的原因代码：{code!r}")
    if code in DETAILS and detail and detail not in DETAILS[code]:
        _log.warning("provenance: %s 不认识细分 %r，应为 %s 之一（照样按分组给原文）", code, detail, DETAILS[code])
    template = REASON_TEXT[code]
    values = {k: "、".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v) for k, v in fmt.items()}
    missing = [m for m in _PLACEHOLDER.findall(template) if m not in values]
    if missing:
        raise ValueError(f"{code} 的原文需要 {missing} 填值")
    text = template.format_map(values) if _PLACEHOLDER.search(template) else template
    return Reason(code=code, detail=detail, text=text)


def alert(code: AlertCode) -> Alert:
    """按 ALERT_TEXT 生成标红的提示。"""
    if code not in ALERT_TEXT:
        raise ValueError(f"不认识的提示代码：{code!r}")
    return Alert(code=code, text=ALERT_TEXT[code])


def contract_problems(out: ProvenanceOut) -> list[str]:
    """响应的不变式（P4-SPEC 2.3、2.8.1、4.1 渲染矩阵），给接口和验收测试断言用；空列表表示通过。

    不在构造时强制：违反不变式是实现的错，应该在测试里暴露，不该在线上把整个请求变成 500。
    """
    out_problems: list[str] = []
    bad = out_problems.append
    if out.schema != SCHEMA:
        bad(f"schema 应为 {SCHEMA}")
    if out.status not in get_args(Status):
        bad(f"status 不认识：{out.status!r}")
    if out.reason is not None and out.reason.code not in get_args(ReasonCode):
        bad(f"reason.code 不认识：{out.reason.code!r}")
    if (out.reason is not None and out.reason.code in DETAILS and out.reason.detail
            and out.reason.detail not in DETAILS[out.reason.code]):
        # 空细分不报：R8 的防御分支规格没给细分，refuse 的缺省也是空串
        bad(f"{out.reason.code} 的细分 {out.reason.detail!r} 不在 DETAILS 里（识别器漏了映射）")
    if out.alert is not None:
        if out.alert.code not in get_args(AlertCode):
            bad(f"alert.code 不认识：{out.alert.code!r}")
        if out.reason is None or out.reason.code != out.alert.code:
            bad("有 alert 时 reason.code 必须与 alert.code 相同")
    if out.reason is not None and out.reason.code in get_args(AlertCode) and out.alert is None:
        bad(f"{out.reason.code} 必须同时给 alert（标红）")
    if out.status == "inferred":
        if out.reason is not None or out.alert is not None:
            bad("inferred 时 reason、alert 都应为 null")
        if out.version is None or out.cell_source is None:
            bad("inferred 时 version、cell_source 都必须有")
        else:
            cs = out.cell_source
            if cs.rowid is None or not cs.pk or cs.recheck is None or not cs.recheck.ok:
                bad("inferred 时 cell_source 的 rowid、pk、recheck 必须填好，且 recheck.ok 为真")
            elif cs.recheck.params != list(cs.pk.values()) or _QUOTED.sub("", cs.recheck.sql).count("?") != len(cs.pk):
                bad("recheck 的 sql 用 ? 占位，个数和 params 都与 pk 一一对应（按 primary_key 的顺序）")
            hits = [p for p in out.version.parts if p.has_row]
            if len(hits) != 1:
                bad(f"inferred 时 version.parts 恰好一项 has_row，现在是 {len(hits)} 项")
            elif hits[0].seq != cs.part_seq:
                bad("has_row 的那一期与 cell_source.part_seq 不一致")
    else:
        if out.reason is None:
            bad(f"{out.status} 时必须给 reason")
        if out.cell_source is not None or out.checks:
            bad(f"{out.status} 时 cell_source 为 null、checks 为空")
        if out.version is not None and any(p.has_row for p in out.version.parts):
            bad("没有推断出格子时 has_row 全为 False")
    if out.status == "table_only" and out.version is None:
        bad("table_only 时 version 必须有")
    if out.status == "none" and out.version is not None:
        bad("none 时 version 为 null")
    return out_problems
