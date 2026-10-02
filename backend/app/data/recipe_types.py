"""配方导入的契约（期 2，波次 0 由编排者提交；期 3 的改动见 P3-SPEC 9.2，同样由波次 0 提交）：配方 JSON 的模型、
执行结果和各模块之间传递的数据结构。

各工作包之间只通过这里的类型和 P2-SPEC.md、P3-SPEC.md 里写明的函数签名交互。这里**只有声明**，没有业务逻辑
（prose_problems / render_prose / accumulate_blockers 三个小函数除外：静态校验、说明生成、AI 起草、累积计划
几处共用，必须是同一份）。

**哈希前向兼容**（P2-SPEC 2.3、P3-SPEC 9.6）：配方的新字段默认值一律等于「没有这个字段时的行为」，canonical 形式
去掉默认值，所以老配方的哈希和构建 id 不变；不改已有字段的默认值。执行结果、说明里的新字段也都有默认值：旧回执、
旧清单里没有它们，读的一方按默认值处理。

改这个文件要编排者拍板：字段的增删会同时影响执行器、核对、起草、接口和前端。

配方 JSON 的封闭性靠结构本身保证：
- 所有模型 extra="forbid"，没有正则字段，取值能枚举的一律 Literal；
- 常量只有 pick（从确认过的分段标题的候选词里选，recipe_parsers.candidate_words）；
- 占位符只能映射成空值（Placeholder 没有「映射成什么」这个字段）；
- 单位只能取自 recipe_parsers.UNITS（静态校验查）；
- 没有任何字段能写年份、统计期的值：统计期只能从格子解析，解析不出来时由人录入、记在导入记录上。

**键的格式**（执行器产出、确认项和差异卡消费，各包必须一致；P2-SPEC 第 7.5 节的 id 都按这张表拼）：
- 分段键 = 分段 id（Segment.id）；块键 = 块 id（Block.id）。两者都**在整份配方内唯一**（静态校验
  segment_id_duplicate / block_id_duplicate），所以不用带工作表前缀。
- 工作表键 = **实际的工作表名**（与坐标「工作表!A1」里的一致）；只有 SheetsOut.matched / renamed 和确认项
  sheet_renamed:<id> 用配方里的工作表 id。
- 坐标 = 「实际工作表名!A1」；区域 = 「实际工作表名!A1:B2」或 RegionMark 的 (sheet, ref)。
- 表名、列名 = derive_tables 推出的名字，配方内部的一切引用（segment.table、verify、keep_as、relations、
  tables[].units 的键、grain、TotalRow.label_column）都必须与它**逐字相等**，不用 name_key、不用 match_key。
- TableOut.sources 里的「s1/交叉表/日间」只给人看，不作键。
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 配方格式版本。改结构就要升，静态校验只认这一个
RECIPE_FORMAT = "agentlab-recipe/2"
#: 配方执行器版本：进构建 id 和快照 id（table_versions.recipe_build_id / snapshot_id）。期 3 不升：没用新字段的配方，
#: 执行器写出的库与期 2 逐字节相同（P3-SPEC 2.1），期 2 的构建照样复用
RECIPE_ENGINE_VER = "recipe/1"
#: 按期累积的并集库的物化规则版本：进并集构建（TableBuild.engine_ver）、并集 id 和组合表哈希（P3-SPEC 2.1、2.6）
UNION_VER = "union/1"

# ==========================================================================
# 配方 JSON（agentlab-recipe/2）
# ==========================================================================


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SheetMatch(_M):
    #: 工作表名，按 recipe_parsers.match_key 比对
    name: str = Field(min_length=1, max_length=31)
    #: 找不到同名工作表时：only_visible_sheet = 恰好只有一个有内容的可见工作表就用它（差异卡报「工作表改名」，需确认）
    fallback: Literal["only_visible_sheet", "none"] = "only_visible_sheet"


class HiddenPolicy(_M):
    #: 认领区域里有隐藏行时：reject_if_any 拒收（默认）；include 照常导入；exclude 跳过（这些格记作 hidden_excluded）
    rows: Literal["reject_if_any", "include", "exclude"] = "reject_if_any"
    #: 认领区域里有隐藏列时：reject_if_any（默认）或 include。期 2 不支持排除列（要排除请用 extra_columns=ignore）
    cols: Literal["reject_if_any", "include"] = "reject_if_any"


class ContextSpec(_M):
    """统计期。固定行为（不可配）：所有能解析出区间的区域外文字格都列出，一致即可、冲突算数据质量；
    一处都没有时请人为本期录入（记在导入记录上，不写进配方）；文件名区间作旁证，冲突算数据质量，没有只记一笔。"""

    id: Literal["统计期"] = "统计期"
    kind: Literal["period"] = "period"
    parser: Literal["cn_date_range"] = "cn_date_range"
    #: 多处一致时，回执里优先展示以这个词开头的格子（如「统计时间范围」）。只作展示优先，不作必需；不能含数字
    prefer_prefix: str | None = Field(default=None, max_length=12)
    #: 旁证：filename = 和文件名里的区间核对；none = 不核对
    cross_check: Literal["filename", "none"] = "filename"


class AxisFind(_M):
    parser: Literal["month_day_or_date"] = "month_day_or_date"
    #: 轴行至少要有几格可解析的日期（还要占该行标签列右侧非空格的多数）
    min: int = Field(default=2, ge=2, le=400)


class Axis(_M):
    find: AxisFind = Field(default_factory=AxisFind)
    #: 日期列的列名
    name: str = "日期"
    #: 存成 TEXT，写法 YYYY-MM-DD
    type: Literal["DATE"] = "DATE"
    #: 年份来源：上下文 id（只写月日的表头靠它补年份，自带年份的也要落在统计期内）；None = 表头格必须自带年份
    year_from: Literal["统计期"] | None = "统计期"
    #: 断言：contiguous 逐日连续；covers_context 恰好覆盖统计期（不多、不少）。不重复是固定断言。
    #: 少了任何一项都是放宽，要进确认清单（P2-SPEC 7.5 的 checks_relaxed）
    checks: list[Literal["contiguous", "covers_context"]] = Field(
        default_factory=lambda: ["contiguous", "covers_context"])

    @field_validator("checks")
    @classmethod
    def _checks_as_set(cls, v: list[str]) -> list[str]:
        # 按集合处理、固定顺序：["covers_context", "contiguous"] 和默认值是同一个意思，哈希也要一样
        return [x for x in ("contiguous", "covers_context") if x in v]


class Placeholder(_M):
    #: 占位符原文，按 canon 比对。不能像数（「0」「-1」静态校验拒）
    text: str = Field(min_length=1, max_length=8)
    #: 只用于说明文字；入库一律是空值
    meaning: Literal["无数据", "不适用"] = "无数据"


class CrossValues(_M):
    type: Literal["INTEGER", "REAL"] = "INTEGER"
    placeholders: list[Placeholder] = Field(default_factory=list, max_length=8)
    #: 数据区真正的空格：reject 拒收（默认）；null 存空值（要进确认清单）
    blank: Literal["reject", "null"] = "reject"
    #: 文本写的数字（「1,234」）：reject（默认）；parse_thousands 千分位按数字
    text_number: Literal["reject", "parse_thousands"] = "reject"
    #: 数据区出现公式：reject（默认，数据区的公式多半是没认出来的合计）；accept_cached 按保存值导入
    formula: Literal["reject", "accept_cached"] = "reject"


class Labels(_M):
    #: 期望的标签集合（原文，按解析器的规范写法或 match_key 比对）。按集合比：多了少了是结构类问题，顺序变化进差异卡
    expect: list[str] = Field(min_length=1, max_length=200)


class Locate(_M):
    """分段怎么定位。labels：按标签集合；section_title：按分段标题（标签列有字、值格全空的行，不要求合并）；
    after：紧跟在另一个分段之后（只给 derived 用）。"""

    by: Literal["labels", "section_title", "after"]
    title: str | None = Field(default=None, max_length=40)
    segment: str | None = Field(default=None, max_length=32)


class Const(_M):
    #: 从本分段的分段标题的候选词里选（recipe_parsers.candidate_words）
    pick: str = Field(min_length=1, max_length=12)


class DimSpec(_M):
    #: 维度列的列名（如「时段」）。一律存规范写法
    name: str
    parser: Literal["hour_range", "text"]
    #: 从解析结果派生的列（只对 hour_range）：{"起始小时": "start", "结束小时": "end"}，INTEGER。
    #: 建表顺序固定为 start 在前、end 在后（与键序无关：配方哈希按键排序，键序不能有语义）
    derive: dict[str, Literal["start", "end"]] = Field(default_factory=dict)


class MeasuresSegment(_M):
    """每一行是一个指标：落成宽表，一个指标一列。"""

    id: str = Field(min_length=1, max_length=32)
    role: Literal["measures"]
    table: str
    locate: Locate
    labels: Labels
    #: 标签原文 → 列名。键必须和 labels.expect 的元素**逐字相同**、一一对应（全角半角括号不同也不行：
    #: derive_tables 按原文查，查不到报 measures_keys_mismatch，绝不产出空列名）。文件里标签的写法变化由
    #: 执行器按 match_key 认，与这里无关
    measures: dict[str, str]


class DimensionSegment(_M):
    """每一行是维度的一个取值：落成长表（轴 × 维度 → 一个值列）。"""

    id: str = Field(min_length=1, max_length=32)
    role: Literal["dimension"]
    table: str
    locate: Locate
    labels: Labels
    dim: DimSpec
    #: 值列的列名（如「客流」）
    value: str
    #: 常量列：列名 → 取自分段标题的词。建表顺序按列名排序（同上：键序不能有语义）
    const: dict[str, Const] = Field(default_factory=dict)
    #: 遇到能被这个解析器解析的标签就结束本段（后面通常是 derived 段）
    stop_parser: Literal["hour_range_total"] | None = None


class Verify(_M):
    """按标签语义区间重算核对：「18-22时合计」= 基表里 起始小时>=18 且 结束小时<=22 的明细之和（逐个轴日期）。
    固定行为：明细时段必须恰好铺满区间，否则「无法核对」；是公式的，公式引用经溯源后必须正好是这些明细格；
    公式没有缓存值是「无法核对」（默认阻断，可写理由接受）；公式和写死之间的变化进差异卡。"""

    kind: Literal["label_range_sum"] = "label_range_sum"
    against_table: str
    #: 基表的值列
    value: str


class KeepAs(_M):
    """表内合计另存成一张表（kind=reported_total）。"""

    table: str
    #: 合计项列（存规范写法「18-22时合计」）
    dim: str
    derive: dict[str, Literal["start", "end"]] = Field(default_factory=dict)
    value: str


class DerivedSegment(_M):
    """合计 / 小计行：不作数据，按 verify 核对；keep_as 给了就另存一张表。"""

    id: str = Field(min_length=1, max_length=32)
    role: Literal["derived"]
    locate: Locate
    labels_parser: Literal["hour_range_total"] = "hour_range_total"
    labels: Labels
    verify: Verify
    keep_as: KeepAs | None = None


Segment = Annotated[Union[MeasuresSegment, DimensionSegment, DerivedSegment], Field(discriminator="role")]


# ---- 忽略规则（期 3，修复按钮 ⑥ 和框选写入，P3-SPEC 3.2、9.2）。都按文件里的文字认，不记坐标；锚点在某一期
# 没出现，就什么也不忽略、也不报错。锚点文字含数字时静态校验报 period_literal（origin 不是 replay 时）。
# 注意：Recipe.model_json_schema() 会进 AI 起草的提示词（recipe_ai._schema_text），类的 docstring 是 schema 的
# description，所以 docstring 只写给读配方的人看的语义，实现细节写在注释里。
#
# 执行语义（WP-1 实现）：
# - IgnoreRow：轴行以下的行，标签 match_key 在集合里、又没有被分段认领的，归类为 ignored：标签格和轴列格的去向是
#   ignored，不进分段，不报 row_unclaimed；行记进 Extraction.rows_excluded（reason=ignored_rows，anchor=命中的
#   标签原文）。同一文字同时在本块某个分段的 expect 里是静态校验的 ignore_conflict。
# - IgnoreColumn（交叉表）：轴行最后一个日期右侧、表头 match_key 在集合里的列，从轴行到本块数据区底部的格，去向是
#   ignored_column，不报 axis_extra_cells；表头记进 Extraction.ignored_columns[块]。表头能被 month_day_or_date
#   解析的是 ignore_conflict。
# - IgnoreColumn（列表）：表头命中的列按 extra_columns=ignore 的方式处理（去向 ignored_column，表头记进
#   ignored_columns），只针对这些列；其余多出的列照 extra_columns 处理。与 columns 的表头重复是 ignore_conflict。
#   列不是行，不进 rows_excluded。
# - IgnoreOutside：区域外的数字格（含区域外的公式格）所在行，如果有区域外文字格的 match_key 等于锚点，这些数字格的
#   去向是 ignored；锚点格本身仍是区域外文字（照常记录、比对）。行记进 rows_excluded（reason=ignored_outside，
#   anchor=锚点原文）。
# - 同一列表里 match_key 相同是静态校验的 ignore_duplicate。理由（reason）只作说明、不参与匹配，但进配方哈希：
#   改理由就是改配方。


class IgnoreRow(_M):
    """按行标签忽略交叉表里的一行：标签与 label 相同、又没有被任何分段认领的行不导入，记进回执的「排除的行」。"""

    #: 交叉表标签列上要忽略的行的标签原文（按 match_key 比；1–40 字）
    label: str = Field(min_length=1, max_length=40)
    #: 为什么忽略（说明用，不参与匹配；1–200 字）
    reason: str = Field(min_length=1, max_length=200)


class IgnoreColumn(_M):
    """按表头忽略一列。交叉表只认日期表头右侧的列；列表只针对这些表头，其余多出的列照 extra_columns 处理。"""

    #: 交叉表：轴行最后一个日期右侧的表头原文；列表：表头原文（按 match_key 比；1–80 字）
    header: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=200)


class IgnoreOutside(_M):
    """导入区域之外、同一行有这段文字时，这一行区域外的数字格不导入；这段文字本身照常作为区域外文字记录。"""

    #: 区域外同一行里的文字（按 match_key 比；1–40 字）。这一行区域外的数字格都忽略
    anchor: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=200)


class CrosstabBlock(_M):
    id: str = Field(min_length=1, max_length=32)
    layout: Literal["crosstab"]
    axis: Axis = Field(default_factory=Axis)
    #: 标签列相对第一个轴格的列偏移。期 2 只支持紧挨着（-1），字段留作以后放宽
    label_offset: int = Field(default=-1, ge=-1, le=-1)
    values: CrossValues = Field(default_factory=CrossValues)
    segments: list[Segment] = Field(min_length=1, max_length=16)
    #: 期 3：按行标签忽略的行（IgnoreRow）。默认空 = 期 2 的行为
    ignore_rows: list[IgnoreRow] = Field(default_factory=list, max_length=50)
    #: 期 3：轴行最后一个日期右侧、按表头忽略的列（IgnoreColumn）。默认空 = 期 2 的行为
    ignore_columns: list[IgnoreColumn] = Field(default_factory=list, max_length=50)


class ListColumn(_M):
    #: 表头原文（多行表头在合并跨度内用「_」拼接）。按 match_key 比对
    header: str = Field(min_length=1, max_length=80)
    name: str
    type: Literal["TEXT", "INTEGER", "REAL", "DATE"]
    #: 只对 TEXT：canonical 存规范写法、raw 存原文。缺省时主键列 canonical、其余 raw
    store: Literal["canonical", "raw"] | None = None


class TotalRow(_M):
    #: 合计标签所在的列（列名）
    label_column: str
    #: 合计行的标签以这个词开头（match_key 比较）。取自实际格子文字的候选词
    pick: str = Field(min_length=1, max_length=12)
    #: 原表合计另存成哪张表（kind=reported_total）；None 只核对不另存
    keep_as: str | None = None


class ListRows(_M):
    #: 数据中间的空行：stop 到此为止（默认）；skip 跳过继续（回执记个数）
    blank_rows: Literal["stop", "skip"] = "stop"
    #: 合计行：到合计行为止，合计行按列求和核对
    total_row: TotalRow | None = None


class ListValues(_M):
    placeholders: list[Placeholder] = Field(default_factory=list, max_length=8)
    #: 数字列、日期列的空格：null 存空值（默认，列表里常见）；reject 拒收。主键列的空格一律拒收
    blank: Literal["null", "reject"] = "null"
    text_number: Literal["reject", "parse_thousands"] = "reject"
    #: 列表里的公式（金额 = 单价 × 数量）：accept_cached 按保存值（默认）；reject 拒收。
    #: 固定行为：工作簿设了打开时重算（fullCalcOnLoad）时，未经核对的公式格一律拒收
    formula: Literal["accept_cached", "reject"] = "accept_cached"


class ListBlock(_M):
    id: str = Field(min_length=1, max_length=32)
    layout: Literal["list"]
    table: str
    header_rows: int = Field(default=1, ge=1, le=3)
    #: 只在这行文字之后找表头（同一工作表上下两张表头相同的时候用）。按 match_key 比，必须恰好一处
    after_title: str | None = Field(default=None, max_length=40)
    columns: list[ListColumn] = Field(min_length=1, max_length=64)
    #: 表头里多出的列：reject 拒收（默认）；ignore 忽略（按表头文字记进回执）
    extra_columns: Literal["reject", "ignore"] = "reject"
    rows: ListRows = Field(default_factory=ListRows)
    values: ListValues = Field(default_factory=ListValues)
    #: 数据区里的合并单元格：reject（默认）；fill 在合并范围内用左上格的值填充（只对 TEXT 列）
    merged_data: Literal["reject", "fill"] = "reject"
    #: 期 3：按表头忽略的列（IgnoreColumn），只针对这些列；其余多出的列照 extra_columns。默认空 = 期 2 的行为
    ignore_columns: list[IgnoreColumn] = Field(default_factory=list, max_length=50)


Block = Annotated[Union[CrosstabBlock, ListBlock], Field(discriminator="layout")]


class SheetRecipe(_M):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    match: SheetMatch
    hidden: HiddenPolicy = Field(default_factory=HiddenPolicy)
    context: list[ContextSpec] = Field(default_factory=list, max_length=1)
    blocks: list[Block] = Field(min_length=1, max_length=8)
    #: 期 3：按同一行的文字忽略导入区域之外的数字格（IgnoreOutside）。默认空 = 期 2 的行为
    ignore_outside: list[IgnoreOutside] = Field(default_factory=list, max_length=20)


class TableSpec(_M):
    name: str
    #: 粒度 = 主键列（按 canon 唯一）。交叉表产出的表必须含轴列；列表可以为空（不建主键）
    grain: list[str] = Field(default_factory=list, max_length=4)
    kind: Literal["data", "reported_total"] = "data"
    #: 列名 → 单位（取自 recipe_parsers.UNITS）
    units: dict[str, str] = Field(default_factory=dict)
    #: 用户补充的口径说明（散文）：不许有数字和坐标，列名表名用 {列:名} {表:名} 引用。AI 起草的配方这里必须为空
    note: str = Field(default="", max_length=300)


class SumEq(_M):
    """恒等式：total = parts 之和，逐行用 SQL 核对。固定为数据质量类（不成立时可写理由接受）。"""

    id: str = Field(pattern=r"^R[0-9]{1,2}$")
    kind: Literal["sum_eq"]
    table: str
    total: str
    parts: list[str] = Field(min_length=2, max_length=8)
    #: 认领的系统发现（DraftFacts 里的 F 编号）；人自己加的为 None
    claims: str | None = Field(default=None, pattern=r"^F[0-9]{1,3}$")


class Side(_M):
    table: str
    value: str


class NotComparable(_M):
    """口径不同：a 按 by 分组求和 ≠ b（只作说明，导入时记一次观察到的相等个数，不阻断）。"""

    id: str = Field(pattern=r"^R[0-9]{1,2}$")
    kind: Literal["not_comparable"]
    a: Side
    b: Side
    #: 两表共有的分组列（如「日期」）
    by: str
    claims: str | None = Field(default=None, pattern=r"^F[0-9]{1,3}$")


class Dismissed(_M):
    """系统发现了一条关系，但用户判定不登记（如巧合）。也算认领。"""

    id: str = Field(pattern=r"^R[0-9]{1,2}$")
    kind: Literal["dismissed"]
    claims: str = Field(pattern=r"^F[0-9]{1,3}$")
    reason: str = Field(min_length=1, max_length=200)


Relation = Annotated[Union[SumEq, NotComparable, Dismissed], Field(discriminator="kind")]


class Recipe(_M):
    recipe_format: Literal["agentlab-recipe/2"]
    #: replace 每期替换；accumulate 按期累积（期 3 起可用：静态校验按 accumulate_blockers 判资格，P3-SPEC 2.2）
    mode: Literal["replace", "accumulate"] = "replace"
    #: 配方没列到的、有内容的可见工作表：confirm 记进回执且需确认（默认）；reject 拒收
    other_visible_sheets: Literal["confirm", "reject"] = "confirm"
    sheets: list[SheetRecipe] = Field(min_length=1, max_length=8)
    tables: list[TableSpec] = Field(min_length=1, max_length=32)
    relations: list[Relation] = Field(default_factory=list, max_length=32)


# ==========================================================================
# 问题、核对、确认、差异
# ==========================================================================

#: 问题类别：structure 结构类（不能接受，只能改配方或改文件）；data_quality 数据质量类（可写理由接受）；
#: confirm 需确认（勾选后可启用）；input 需要人录入（统计期）；recipe 配方本身不合法（不能试运行）
Category = Literal["structure", "data_quality", "confirm", "input", "recipe"]


@dataclass
class Problem:
    code: str
    category: Category
    #: 给人看的中文，可以含格子里的文字和数
    message: str
    #: 坐标「工作表!A1」，最多 20 个
    cells: list[str] = field(default_factory=list)
    #: 给 AI 修订看的版本：可以有坐标和标签文字，不能有数据格的数值。执行器产出的每个 code 都必须填
    #: （WP-2 的契约测试逐个 code 断言非空）；万一为空，回灌时只发 code 和 cells，**绝不退回 message**
    model_message: str = ""
    #: 修复按钮的种类，取值见 FIX_KINDS（执行器按 recipe_engine.PROBLEM_FIX 填；edit_members 只来自静态校验，
    #: rename_sheet 另有不是问题的触发：Extraction.sheets.renamed）。没有修复按钮时 None
    fix: str | None = None
    #: 期 3：构造修复补丁要的参数，形状按 fix 的种类见 P3-SPEC 3.2（如 label_missing 的
    #: {"segment": 分段 id, "labels": [原文…]}）。由执行器填写。**只含标签、表头、分段标题、锚点的原文和坐标，
    #: 不得含数据格的数值**（declare_placeholder 的 texts 是数据格里的占位符文字，不是数值，允许）。
    #: 没有修复按钮时 None
    fix_args: dict[str, Any] | None = None


#: 修复按钮的九种（P3-SPEC 3.2 ①–⑨）。Problem.fix、FixProposal.kind、确认项 fix:<目标键> 的 kind 都取自这里
FIX_KINDS = ("remove_label", "add_label", "edit_members", "rename_title", "declare_total", "ignore_cells",
             "declare_placeholder", "declare_hidden", "rename_sheet")


@dataclass
class RecipeProblem:
    #: JSON Pointer，如 /sheets/0/blocks/0/segments/2/const/时段类别/pick
    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


CheckStatus = Literal["passed", "mismatch", "unverifiable", "info"]


@dataclass
class CheckResult:
    #: 稳定 id：K1… 合计核对、G1… 公式引用、T1… 列表合计、R1… 关系（沿用配方里的 id）、C1 统计期、C2 文件名、
    #: N1… 行数、P1… 主键；期 3 的并集整体核对 U1 行数、U2 主键、U3 统计期（物化时跑，都是结构类，P3-SPEC 2.7）。
    #: F 编号留给系统发现的事实（Fact.id），两者不混用
    id: str
    kind: Literal["derived_sum", "formula_refs", "column_sum", "relation_sum_eq", "relation_not_comparable",
                  "context_agree", "filename_period", "row_count", "pk_unique",
                  "union_rows", "union_pk", "union_period"]
    title: str
    status: CheckStatus
    #: 不通过时的类别：structure / data_quality / info
    category: Literal["structure", "data_quality", "info"]
    checked: int = 0
    failed: int = 0
    unverifiable: int = 0
    #: 跑过的 SQL 原文（一条代表性的，参数写在 params 里）
    sql: str | None = None
    params: list[Any] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    cells: list[str] = field(default_factory=list)
    #: 可以写理由接受（数据质量类不成立、合计无法核对）
    acceptable: bool = False
    #: 无法核对的原因 → 格数（K / T / G）：tiling 明细没铺满、uncached 公式无缓存、blank 合计格为空、
    #: null_detail 明细含空值、hidden 隐藏行或分类汇总、unrecognized 公式形状认不出。说明按它选模板
    reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class Acceptance:
    """对一条核对结果写理由接受。data_quality 不成立 → overrides；合计无法核对 → waivers。"""

    check_id: str
    reason: str
    signed_by: str | None = None


@dataclass
class ConfirmItem:
    #: 稳定 id，见 P2-SPEC 第 7.5 节、P3-SPEC 9.5 的取值表
    id: str
    label: str
    detail: str = ""
    required: bool = True
    #: 界面按它分组（P3-SPEC 9.5）。期 3 新增 edit（fix:*、select:*、redraft_adopted）和 accumulate
    #: （period_replace、retire、mode_switch、accumulate_restart、accumulate_unit_risk）；默认值仍是 recipe
    source: Literal["recipe", "diff", "outside", "sheet", "context", "switch", "edit", "accumulate"] = "recipe"


@dataclass
class DiffItem:
    kind: str
    label: str
    detail: str = ""
    requires_confirm: bool = False
    #: requires_confirm 时对应的 ConfirmItem.id
    confirm_id: str | None = None


# ==========================================================================
# 网格（读取层 → 执行器、起草器、压缩表示）
# ==========================================================================


@dataclass
class GridCell:
    #: 保存值（openpyxl data_only）：int / float / str / bool / datetime / date / time / None
    value: Any
    #: 公式原文（带「=」），不是公式时 None
    formula: str | None = None

    @property
    def has_cache(self) -> bool:
        return self.formula is None or self.value is not None


@dataclass
class Grid:
    """一张可见工作表的物化网格（只给小表：交叉表、预览、起草；大列表走 xlsx_cells.iter_rows）。"""

    sheet: str
    #: (min_row, min_col, max_row, max_col)；空表 None
    bounds: tuple[int, int, int, int] | None
    #: 非空格（口径和 xlsx_scan 一致：有公式的格不算空；只有空白字符的文本算空）
    cells: dict[tuple[int, int], GridCell] = field(default_factory=dict)
    #: 合并区 (r1, c1, r2, c2)
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    hidden_rows: set[int] = field(default_factory=set)
    hidden_cols: set[int] = field(default_factory=set)
    #: 读到 max_rows 就停了（大表的预览、起草）
    truncated: bool = False

    def get(self, r: int, c: int) -> GridCell | None:
        return self.cells.get((r, c))

    def _index(self) -> dict[int, list[int]]:
        # 行号 → 该行非空格的列号（升序）。第一次按行访问时建一次：逐行扫的调用方（执行器、起草器）
        # 总代价是 O(格数)，而不是每次 row() 都把整张表过一遍。**构造完成后不要再改 cells**
        # （改了要 del grid.__dict__["_rows"]）。存在实例字典里而不是 dataclass 字段：不进 asdict、不进比较
        idx = self.__dict__.get("_rows")
        if idx is None:
            idx = {}
            for (rr, c) in self.cells:
                idx.setdefault(rr, []).append(c)
            for cols in idx.values():
                cols.sort()
            self.__dict__["_rows"] = idx
        return idx

    def row(self, r: int) -> list[tuple[int, GridCell]]:
        """第 r 行的非空格 [(列号, 格)]，按列升序。O(该行格数)（首次调用时建索引，O(格数)）。"""
        return [(c, self.cells[(r, c)]) for c in self._index().get(r, ())]

    def row_numbers(self) -> list[int]:
        """有非空格的行号，升序。"""
        return sorted(self._index())


# ==========================================================================
# 执行结果（执行器 → 核对、说明、差异、回执）
# ==========================================================================

#: 格子账的去处（每个非空格恰好一个）。期 3 新增 ignored：按配方的 ignore_rows / ignore_outside 忽略的格
#: （按表头忽略的列沿用 ignored_column）
Role = Literal["value", "derived_value", "derived_label", "col_header", "row_label", "section_title",
               "context", "outside_text", "ignored_column", "hidden_excluded", "total_label", "total_value",
               "ignored"]


@dataclass
class RegionMark:
    sheet: str
    role: Role
    #: A1 区域（「C5:AG7」或单格）
    ref: str
    #: 期 3：块 id（Block.id）。区域外文字、统计期这类不属于任何块的为 None。框选的重放比对按它过滤出新块的区域，
    #: 网格按块描边。执行器按 (role, block, 列段) 合矩形，不同块的格不合进同一个矩形。
    #: JSON（asdict）里一律带 block 键，None 也带：界面和 replay_compare 按键取，不判断有没有这个键。
    #: 已知后果：期 2 的 test_recipe_imports.py 的 test_stage_first_does_not_create_the_source_and_keeps_the_raw
    #: 逐字比 marks（不含 block），WP-0 合并后它会失败。那个文件归 WP-5（P3-SPEC 12.0），改法是期望值补
    #: "block": None 或只比 sheet / role / ref。不要为了让它通过去掉这个字段或改默认值
    block: str | None = None


@dataclass
class LedgerSheet:
    sheet: str
    #: 第一遍（xlsx_scan）数到的非空格
    nonempty_scan: int
    #: 第二遍（读取层）数到的非空格
    nonempty_read: int
    roles: dict[str, int] = field(default_factory=dict)
    unclaimed: int = 0


@dataclass
class ColumnOut:
    name: str
    #: INTEGER / REAL / TEXT（DATE 存 TEXT）
    type: str
    #: 原表头或原标签（只进清单和界面，不进模型可见文本）
    header: str | None = None
    unit: str | None = None
    #: axis / dim / derive / const / measure / value / text
    role: str = "value"


@dataclass
class TableOut:
    name: str
    sheet: str
    kind: Literal["data", "reported_total"]
    columns: list[ColumnOut]
    grain: list[str]
    rows: int
    #: 写入这张表的分段 / 块（「s1/交叉表/日间」，只给人看，不作键）
    sources: list[str] = field(default_factory=list)


@dataclass
class DerivedItem:
    """一个要核对的合计格（交叉表的合计行或列表的合计行）。核对模块用 SQL 重算，执行器不做算术。"""

    kind: Literal["label_range_sum", "column_sum"]
    #: 分段键（derived 段的 id）或块键（列表块的 id）
    segment: str
    sheet: str
    cell: str
    label_raw: str
    label: str
    #: 保存值（写死的数或公式的缓存值）；公式没有缓存值时 None
    value: int | float | None
    is_formula: bool
    formula: str | None
    base_table: str
    base_value: str
    #: label_range_sum：区间与分组键。key = {轴列名: 日期, 前一分段（locate.segment）的每个常量列: pick}：
    #: 核对 SQL 按 key 的全部列过滤，同一张表里别的分段（别的类别）写入的行不会被加进来
    start: int | None = None
    end: int | None = None
    key: dict[str, Any] = field(default_factory=dict)
    start_col: str | None = None
    end_col: str | None = None
    #: column_sum：基表里这一块数据行的 rowid 闭区间
    rowid_first: int | None = None
    rowid_last: int | None = None
    #: 公式引用经溯源后对应的基表 rowid；不是公式、或认不出公式形状时 None
    ref_rowids: list[int] | None = None
    #: unrecognized（公式形状认不出）/ outside（引用了不在基表里的格）/ hidden（引用里有隐藏行、
    #: 或是 SUBTOTAL / AGGREGATE：合计口径无法确定）/ None
    ref_problem: str | None = None
    #: 合计公式的函数：SUM / SUBTOTAL / AGGREGATE / plus（单格相加）/ None（写死的数）
    func: str | None = None
    #: 本合计覆盖的明细行里有隐藏行（include 或 exclude）：不一致时只能判「无法核对」（critic C6）
    hidden_in_range: bool = False
    keep_table: str | None = None


@dataclass
class CanonEntry:
    table: str
    column: str
    raw: str
    canonical: str
    count: int
    first_cell: str


@dataclass
class SegmentLabels:
    segment: str
    #: 按工作表里的顺序
    raw: list[str]
    canonical: list[str]
    rows: list[int]


@dataclass
class OutsideText:
    sheet: str
    #: 带工作表名的坐标「工作表!A1」（与 Problem.cells、差异卡的 diff:outside:<工作表>!<格> 同一写法），不是纯 A1
    cell: str
    #: 全文（含数字也照录：只给回执、面板、裁判看，不进模型可见文本）
    text: str
    kind: Literal["text", "text_digits"]
    #: 这格同时是统计期来源（带附加文字的统计期格，PeriodOut.annotated）
    period_source: bool = False


@dataclass
class AxisOut:
    block: str
    row: int
    first: str
    last: str
    count: int
    #: text（「8月1日」之类）/ date（日期格）/ mixed
    form: str


@dataclass
class PeriodOut:
    start: str
    end: str
    #: cells 从格子解析；human 本期人工录入
    source: Literal["cells", "human"]
    #: 解析出统计期的格子（含只写到月的旁证）
    cells: list[str] = field(default_factory=list)
    #: 人工录入时的署名（未认证）
    signed_by: str | None = None
    #: 坐标 → 这格的全文（差异卡按「数字遮盖后的模板」比较统计期格的写法变化，D12）
    texts: dict[str, str] = field(default_factory=dict)
    #: 带附加文字的统计期格：坐标 → recipe_parsers.period_residue。这些格同时记进 outside_text
    #: （kind=text_digits），按含数字的区域外文字处理（D15、E7）；只是在说统计期的格不在这里
    annotated: dict[str, str] = field(default_factory=dict)


@dataclass
class SheetsOut:
    #: 配方工作表 id → 实际用的工作表名
    matched: dict[str, str] = field(default_factory=dict)
    #: 按 fallback 认的（工作表改了名）：配方里的名字 → 实际名字
    renamed: dict[str, str] = field(default_factory=dict)
    #: 配方没列到的、有内容的可见工作表
    other_visible: list[str] = field(default_factory=list)
    #: 没读的隐藏工作表 [{sheet, state}]
    skipped_hidden: list[dict[str, str]] = field(default_factory=list)


#: 排除的行的原因（P3-SPEC 第 8 节遗留项 4）：
#: hidden_excluded = hidden.rows=exclude 排除的隐藏行；blank_skipped = 列表 blank_rows=skip 跳过的空行；
#: ignored_rows = 交叉表 ignore_rows 忽略的行；ignored_outside = ignore_outside 忽略了区域外数字格的行；
#: after_stop = 列表停止之后没导入的文字行；total_not_kept = 参与核对、但不另存的合计行（交叉表 keep_as 为空的
#: derived 段、列表 total_row.keep_as 为空的合计行）
ExcludedReason = Literal["hidden_excluded", "blank_skipped", "ignored_rows", "ignored_outside", "after_stop",
                         "total_not_kept"]


@dataclass
class ExcludedRows:
    """回执里的「排除的行」（H2：排除的行要写进回执）。同一工作表、同一原因、同一块（和同一锚点）合成一项，
    行号写成闭区间列表。按表头忽略的列不是行，不在这里（记在 Extraction.ignored_columns）。"""

    #: 实际工作表名
    sheet: str
    reason: ExcludedReason
    #: [[起, 止], …]，1 起的行号闭区间，升序、不重叠
    rows: list[list[int]]
    #: 这些行里被排除的非空格个数（与格子账同一口径）
    cells: int
    #: ignored_rows / ignored_outside：命中的锚点原文（标签或区域外文字）；after_stop 且是跳过空行的列表按「表下说明」
    #: 收尾时（WP-8）：说明那一格的文字（去首尾空白），列表停在哪一行、因为什么由它看出；其余 None
    anchor: str | None = None
    #: 所在块的 id；ignored_outside、hidden_excluded 这类按工作表算的为 None
    block: str | None = None


@dataclass
class Extraction:
    #: 没有 structure / input 类问题，且库已写好
    ok: bool
    problems: list[Problem] = field(default_factory=list)
    #: 只在每张工作表的前若干行上干跑（execute(max_rows=…)，交互时用）：依赖整张表的检查（两遍对账、
    #: 窗口之外的未认领、轴覆盖、稀疏、主键全表去重）没做。partial 的结果只能给界面着色和起草判断，
    #: 不能当试运行用
    partial: bool = False
    tables: list[TableOut] = field(default_factory=list)
    ledger: list[LedgerSheet] = field(default_factory=list)
    regions: list[RegionMark] = field(default_factory=list)
    #: 表 → 列 → [[起始 rowid, 工作表, 起始格, 个数, "right" | "down"]]
    lineage: dict[str, dict[str, list[list[Any]]]] = field(default_factory=dict)
    period: PeriodOut | None = None
    #: 统计期多处一致、文件名旁证的核对结果（C1、C2）。C2 取决于文件名、而构建 id 不含文件名：
    #: 构建回执（TableBuild.report）不存这一项，它跟着导入记录走（table_imports.checks、导入清单）
    context_checks: list[CheckResult] = field(default_factory=list)
    derived: list[DerivedItem] = field(default_factory=list)
    canonicalized: list[CanonEntry] = field(default_factory=list)
    labels: list[SegmentLabels] = field(default_factory=list)
    outside_text: list[OutsideText] = field(default_factory=list)
    #: 占位符原文 → 个数
    placeholders: dict[str, int] = field(default_factory=dict)
    #: 认领区域里的隐藏行列：{sheet: {"rows": [...], "cols": [...], "policy_rows": ..., "policy_cols": ...}}
    hidden: dict[str, dict[str, Any]] = field(default_factory=dict)
    sheets: SheetsOut = field(default_factory=SheetsOut)
    axes: list[AxisOut] = field(default_factory=list)
    #: 工作表键（实际工作表名）→ 该表上各分段键（交叉表）和块键（列表）按首行从上到下的先后
    block_order: dict[str, list[str]] = field(default_factory=dict)
    #: derived 分段键 → formula / literal / mixed
    derived_form: dict[str, str] = field(default_factory=dict)
    full_calc_on_load: bool = False
    #: 按保存值导入的公式格个数（accept_cached）
    formula_cells_accepted: int = 0
    #: 列表里跳过的空行个数（blank_rows=skip）
    blank_rows_skipped: int = 0
    #: 块键 → 按 extra_columns=ignore 忽略的列的表头原文（确认项 ignored_columns:<块>、差异卡 ignored_columns）
    ignored_columns: dict[str, list[str]] = field(default_factory=dict)
    #: 表 → 期望行数（= 格子账推出来的），核对模块和 SELECT COUNT(*) 比
    expected_rows: dict[str, int] = field(default_factory=dict)
    #: 表 → 按主键排序后规范化行的 sha256（写库之后算）
    table_hashes: dict[str, str] = field(default_factory=dict)
    #: 各阶段耗时（秒）
    timings: dict[str, float] = field(default_factory=dict)
    #: 期 3：排除的行（ExcludedRows）。只取本次试运行的 Extraction 写进导入清单的 receipt；复用的期 2 构建回执
    #: （TableBuild.report）里没有这个字段，读的一方按空列表处理。差异卡不把它加进 RECEIPT_KEYS：上一期没有这个
    #: 字段时出 info「上一期未记录」，不当作空列表比较（P3-SPEC 第 8 节遗留项 4）。
    #: 要显示它的地方有两处，WP-5 两处都要接上：①试运行回执 TrialOut.receipt——recipe_imports._RECEIPT_VIEW
    #: 只抄白名单里的键，要把 "rows_excluded" 加进去，否则向导回执一律显示「这次导入未记录排除的行」（界面把缺这个
    #: 键当作期 3 之前的回执）；②导入清单的 receipt。extraction_from_dict 读回时还要在 _EXTRACTION_PARTS 里把
    #: 每项转回 ExcludedRows
    rows_excluded: list[ExcludedRows] = field(default_factory=list)


@dataclass
class PeriodInput:
    """本期人工录入的统计期（只在解析不出来时）。记在导入记录上，计入构建哈希，不写进配方。"""

    start: _dt.date
    end: _dt.date
    signed_by: str | None = None


# ==========================================================================
# 起草
# ==========================================================================


@dataclass
class Fact:
    """系统发现的一条事实（只有关系，不含数值）。首次确认时每条都要被配方认领。"""

    id: str
    kind: Literal["sum_eq", "not_equal_sum"]
    sheet: str
    #: 人能看懂的一句话：「第 5 行 = 第 6 行 + 第 7 行（31/31 列成立）」
    text: str
    #: sum_eq：{"segment": measures 分段 id, "total": 标签原文, "parts": [...], "rows": [...]}；
    #: not_equal_sum：{"a": [dimension 分段 id...], "b_segment": measures 分段 id, "b": 标签原文, "equal": n, "checked": m}。
    #: 静态校验据此做认领的语义检查（fact_claim_mismatch）：认领它的 relation 经 measures 映射后必须正好是这些标签
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class DraftFacts:
    facts: list[Fact] = field(default_factory=list)
    #: 分段标题（原文）→ 候选词，界面上给常量做选择
    candidates: dict[str, list[str]] = field(default_factory=dict)
    #: 数值区里出现的非数字文字 → 个数（占位符候选）
    nonnumeric: dict[str, int] = field(default_factory=dict)


@dataclass
class Card:
    """建议卡片：一条建议加一句理由，指向网格上的格子。"""

    id: str
    title: str
    reason: str
    cells: list[str] = field(default_factory=list)
    #: 对应的待确认问题（Question.id），没有就是 None
    question: str | None = None


@dataclass
class Question:
    id: str
    text: str
    #: [{"value": str, "label": str, "needs_reason": bool}]：只给封闭的选项。needs_reason 为真的选项
    #: （如「不登记」）提交回答时必须带理由（1–200 字）
    options: list[dict[str, Any]] = field(default_factory=list)
    default: str | None = None
    #: 选项值 → 对配方的修改（[{"op": "replace" | "add" | "remove", "path": JSON Pointer, "value": ...}]），由服务端应用。
    #: value 里的字符串 REASON_SLOT 由服务端换成用户写的理由（只换整个字符串等于它的值，不做子串替换）
    effects: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


#: Question.effects 里给理由占位的字符串
REASON_SLOT = "{reason}"


@dataclass
class Draft:
    recipe: dict[str, Any] | None
    #: 起草得出了能通过静态校验、并能认领全部非空格（干跑）的配方
    complete: bool
    origin: Literal["rules", "ai"]
    cards: list[Card] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    facts: DraftFacts = field(default_factory=DraftFacts)
    #: 起草不完整的原因（给人看，可以引用格子原文）
    failures: list[str] = field(default_factory=list)
    #: 同一组原因给模型看的版本：只有坐标、标签和表头文字，不含任何数字格的值、像数的文字
    #: （AI 起草的提示词只用这一份，不用 failures）
    failures_for_model: list[str] = field(default_factory=list)


@dataclass
class AiUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ok: bool
    at: str
    #: 第几次调用（1 起草，2、3 修订）
    attempt: int = 1


@dataclass
class AiAvailability:
    available: bool
    #: 不可用时给人看的原因（「未配置模型接入」）
    reason: str = ""
    #: 可用时的模型名（界面告知会发给谁）
    model: str = ""
    provider: str = ""


# ==========================================================================
# 说明（按核对结果生成）
# ==========================================================================


@dataclass
class TableNote:
    #: 已渲染的表说明（注入 schema_cache 的 comment）
    comment: str
    #: 列名 → 已渲染的列说明（单位、空值含义、日期格式）
    columns: dict[str, str] = field(default_factory=dict)


@dataclass
class NoteFragment:
    """表说明里的一个片段（期 3）：build_notes 按片段族拼说明时顺带记下，build_union_notes 按 (key, subject)
    逐期合并、取最弱（P3-SPEC 2.8）。只是多返回结构，渲染结果不变。"""

    #: NOTE_TEXT 的键（片段族，如 sum_eq_passed、total_k_passed）
    key: str
    #: 关系 id（sum_eq / not_comparable 的 Rn）或表名（合计表、本表）；与 subject 无关的片段为 None
    subject: str | None
    #: 带 {列:} {表:} {单位:} 引用的模板（未渲染）
    template: str


@dataclass
class SchemaNotes:
    tables: dict[str, TableNote] = field(default_factory=dict)
    #: 渲染前的模板（带 {列:名} 标记），进清单，复核时可以重查。键：表名或「表名.列名」
    templates: dict[str, str] = field(default_factory=dict)
    #: 数字检查没过的（非空就不能保存）
    problems: list[str] = field(default_factory=list)
    #: 表名 → 表的种类，只记 reported_total（原表写明的合计）。apply_notes 记进表结构，工具描述、db_schema 的
    #: 表清单、写作目录据此标出「不要彼此相加、不要与明细相加」（AU-5）
    kinds: dict[str, str] = field(default_factory=dict)
    #: 期 3：表名 → 这张表说明的片段（NoteFragment），按拼接顺序。旧清单里没有，按空处理
    fragments: dict[str, list[NoteFragment]] = field(default_factory=dict)


_TOKEN = re.compile(r"\{(列|表|单位):([^{}]{1,48})\}")
_COORD = re.compile(r"(?<![A-Za-z0-9_])\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}(?![A-Za-z0-9_])")


def render_prose(template: str) -> str:
    """把 {列:名} {表:名} {单位:名} 换成名字本身。"""
    return _TOKEN.sub(lambda m: m.group(2), template)


#: prose_problems 的 known 参数里三类引用的键
TOKEN_KINDS = ("列", "表", "单位")


def prose_problems(template: str, *, known: dict[str, set[str]] | None = None) -> list[str]:
    """说明散文的检查：遮盖结构化引用（列名、表名、单位）之后，不许抽出数字，也不许出现单元格坐标。

    数字用出具校验同一套规则抽（app.engine.issuance.extract_numbers，app.engine.evidence 里的同名函数就是它）。
    单位、列名由系统按结构渲染，检查时遮盖：「单位：万元」抽出「万」、「3号门客流」抽出 3，都不是说明在报数。

    **遮盖的前提是引用真的指向结构化的名字。** 给了 known（{"列": 列名集合, "表": 表名集合, "单位": 单位集合}，
    逐字比较）时，不认识的引用不遮盖、并报一条问题：用户写的 note 不能靠「{列:增长30%}」把数字藏过检查。
    静态校验（用户的 note）和说明生成都必须传 known；只有契约测试里检查模板本身时才不传。
    返回给人看的问题列表；空列表表示通过。
    """
    from app.engine.issuance import extract_numbers

    out: list[str] = []

    def _mask(m: re.Match[str]) -> str:
        kind, name = m.group(1), m.group(2)
        if known is not None and name not in known.get(kind, set()):
            label = {"列": "列", "表": "表", "单位": "单位"}[kind]
            out.append(f"说明引用了不存在的{label}「{name}」")
            return m.group(0)
        return " " * len(m.group(0))

    masked = _TOKEN.sub(_mask, template)
    out += [f"说明中含有数字「{t.raw}」" for t in extract_numbers(masked)]
    out += [f"说明中含有单元格坐标「{m.group(0)}」" for m in _COORD.finditer(masked)]
    return out


# ==========================================================================
# 由配方推出每张表的列（执行器建表、静态校验、说明、破坏性变更比对共用同一份）
# ==========================================================================

#: 列表合计行另存成表时，合计项列的固定列名
LIST_TOTAL_DIM = "合计项"
_SQL_TYPE = {"INTEGER": "INTEGER", "REAL": "REAL", "TEXT": "TEXT", "DATE": "TEXT"}


def derive_tables(recipe: Recipe) -> tuple[dict[str, list[ColumnOut]], list[RecipeProblem]]:
    """配方 → {表名: 列（按建表顺序）}，以及形状冲突（同一张表被两个分段写成不同的列）。

    列的顺序：交叉表的宽表 = 轴、各指标（labels.expect 的顺序）；长表 = 轴、维度、常量（按列名排序）、
    派生（start 在前、end 在后）、值；表内合计 = 轴、合计项、派生、值；列表 = columns 的顺序；列表合计表 =
    合计项、各数字列。常量和派生不按 dict 的键序：配方哈希用 sort_keys，键序若决定列序，两份列序不同的配方
    会得到同一个构建 id。单位取 tables[].units（逐字查列名）。

    measures 的键按原文逐字查（不按 match_key）：expect 里某个标签在 measures 里没有、或 measures 里多出
    expect 没有的键，报 measures_keys_mismatch——列名照样产出（空字符串），但带着问题，调用方必须拦下，
    绝不能拿空列名去建表。不查名字合法性、不查 tables 里有没有声明（那是静态校验的事）。
    """
    units = {t.name: t.units for t in recipe.tables}
    out: dict[str, list[ColumnOut]] = {}
    where: dict[str, str] = {}
    problems: list[RecipeProblem] = []

    def _derive_cols(derive: dict[str, str]) -> list[ColumnOut]:
        ordered = sorted(derive.items(), key=lambda kv: (kv[1] != "start", kv[0]))
        return [ColumnOut(name, "INTEGER", role="derive") for name, _ in ordered]

    def put(table: str, cols: list[ColumnOut], path: str) -> None:
        for c in cols:
            c.unit = units.get(table, {}).get(c.name)
        if table not in out:
            out[table], where[table] = cols, path
            return
        old = [(c.name, c.type, c.role) for c in out[table]]
        new = [(c.name, c.type, c.role) for c in cols]
        if old != new:
            problems.append(RecipeProblem(path, "table_shape_conflict",
                                          f"表「{table}」在两处写入的列不一致（另一处：{where[table]}）"))

    for si, sheet in enumerate(recipe.sheets):
        for bi, block in enumerate(sheet.blocks):
            base = f"/sheets/{si}/blocks/{bi}"
            if isinstance(block, CrosstabBlock):
                vtype = block.values.type
                axis_name = block.axis.name
                for gi, seg in enumerate(block.segments):
                    path = f"{base}/segments/{gi}"
                    if isinstance(seg, MeasuresSegment):
                        missing = [label for label in seg.labels.expect if label not in seg.measures]
                        extra = [key for key in seg.measures if key not in seg.labels.expect]
                        if missing or extra:
                            parts = []
                            if missing:
                                parts.append("标签" + "、".join(f"「{x}」" for x in missing) + "没有对应的列名")
                            if extra:
                                parts.append("列名对照里的" + "、".join(f"「{x}」" for x in extra)
                                             + "不在期望的标签中（必须与标签逐字相同）")
                            problems.append(RecipeProblem(f"{path}/measures", "measures_keys_mismatch",
                                                          f"分段「{seg.id}」：" + "；".join(parts)))
                        cols = [ColumnOut(axis_name, "TEXT", role="axis")]
                        cols += [ColumnOut(seg.measures.get(label, ""), vtype, header=label, role="measure")
                                 for label in seg.labels.expect]
                        put(seg.table, cols, path)
                    elif isinstance(seg, DimensionSegment):
                        cols = [ColumnOut(axis_name, "TEXT", role="axis"), ColumnOut(seg.dim.name, "TEXT", role="dim")]
                        cols += [ColumnOut(name, "TEXT", role="const") for name in sorted(seg.const)]
                        cols += _derive_cols(seg.dim.derive)
                        cols.append(ColumnOut(seg.value, vtype, role="value"))
                        put(seg.table, cols, path)
                    elif isinstance(seg, DerivedSegment) and seg.keep_as is not None:
                        k = seg.keep_as
                        cols = [ColumnOut(axis_name, "TEXT", role="axis"), ColumnOut(k.dim, "TEXT", role="dim")]
                        cols += _derive_cols(k.derive)
                        cols.append(ColumnOut(k.value, vtype, role="value"))
                        put(k.table, cols, path)
            else:
                cols = [ColumnOut(c.name, _SQL_TYPE[c.type], header=c.header,
                                  role="measure" if c.type in ("INTEGER", "REAL") else "text")
                        for c in block.columns]
                put(block.table, cols, base)
                total = block.rows.total_row
                if total is not None and total.keep_as:
                    tcols = [ColumnOut(LIST_TOTAL_DIM, "TEXT", role="dim")]
                    tcols += [ColumnOut(c.name, c.type, header=c.header, role="value")
                              for c in cols if c.type in ("INTEGER", "REAL")]
                    put(total.keep_as, tcols, f"{base}/rows/total_row")
    return out, problems


# ==========================================================================
# 期 3：按期累积的资格（静态校验、起草器、累积计划共用同一份，P3-SPEC 2.2）
# ==========================================================================


def accumulate_blockers(recipe: Recipe) -> list[RecipeProblem]:
    """这份配方为什么不能按期累积；空列表 = 符合资格。不看 recipe.mode：静态校验只在 mode=accumulate 时报它们，
    起草器在为空时才写 mode=accumulate，累积计划对每一期的配方都跑一遍（P3-SPEC 2.2、2.4）。

    按期累积要求同时满足：
    1. 至少一个工作表配置了统计期上下文（SheetRecipe.context 非空），否则报 accumulate_needs_period（path=/mode）；
    2. derive_tables 推出的每张表都「按统计期分得开」，否则逐表报 accumulate_unkeyed（path=/tables/{ti}，表没在
       tables 里声明时指向第一个写入它的块），消息写明哪一条不满足：
       - 只由交叉表块写入（measures、dimension 段，或者 derived 段的 keep_as）——列表写入的表一律不满足；
       - 写入它的每个交叉表块，axis.checks 都含 covers_context（日期恰好落在统计期内）；
       - 这张表的 grain 含那个块的 axis.name。
    两条都成立时，各期的日期集合互不相交（统计期不重叠由累积计划保证），主键里不需要另加统计期列。
    """
    problems: list[RecipeProblem] = []
    if not any(sheet.context for sheet in recipe.sheets):
        problems.append(RecipeProblem("/mode", "accumulate_needs_period",
                                      "配方没有配置统计期，不能按期累积：各期要按统计期区分"))
    tables, _ = derive_tables(recipe)
    # 表名 → 写入它的块（去重，按配方顺序）和第一个块的路径
    writers: dict[str, list[CrosstabBlock | ListBlock]] = {}
    first_path: dict[str, str] = {}

    def _add(table: str, block: CrosstabBlock | ListBlock, path: str) -> None:
        got = writers.setdefault(table, [])
        if all(b is not block for b in got):
            got.append(block)
        first_path.setdefault(table, path)

    for si, sheet in enumerate(recipe.sheets):
        for bi, block in enumerate(sheet.blocks):
            path = f"/sheets/{si}/blocks/{bi}"
            if isinstance(block, CrosstabBlock):
                for seg in block.segments:
                    if isinstance(seg, (MeasuresSegment, DimensionSegment)):
                        _add(seg.table, block, path)
                    elif isinstance(seg, DerivedSegment) and seg.keep_as is not None:
                        _add(seg.keep_as.table, block, path)
            else:
                _add(block.table, block, path)
                total = block.rows.total_row
                if total is not None and total.keep_as:
                    _add(total.keep_as, block, path)

    index = {t.name: ti for ti, t in enumerate(recipe.tables)}
    grains = {t.name: list(t.grain) for t in recipe.tables}
    for name in tables:
        blocks = writers.get(name, [])
        if any(isinstance(b, ListBlock) for b in blocks):
            reasons = ["列表形态的表目前只支持每期替换"]
        else:
            reasons = []
            uncovered = [b.id for b in blocks if isinstance(b, CrosstabBlock) and "covers_context" not in b.axis.checks]
            if uncovered:
                reasons.append("、".join(f"交叉表「{x}」" for x in uncovered)
                               + "的日期检查没有选「恰好覆盖统计期」，各期的日期可能超出统计期")
            grain = grains.get(name, [])
            axes = sorted({b.axis.name for b in blocks if isinstance(b, CrosstabBlock) and b.axis.name not in grain})
            if axes:
                reasons.append("主键不含日期列" + "、".join(f"「{x}」" for x in axes) + "，各期的行无法按日期区分")
        if reasons:
            path = f"/tables/{index[name]}" if name in index else first_path.get(name, "/tables")
            problems.append(RecipeProblem(path, "accumulate_unkeyed", f"表「{name}」不能按期累积：" + "；".join(reasons)))
    return problems


# ==========================================================================
# 期 3：修复、框选、累积、并集的数据结构（P3-SPEC 9.2；JSON 形状见 9.4，dataclass 字段与 JSON 同名，
# Selection.as_ 除外）。字段名由 test_recipe_contract.py 钉住，实现者不改名
# ==========================================================================


@dataclass
class FixOption:
    """修复提议的一个封闭选项。效果由服务端算：客户端只发 fix_id、value 和理由，不发补丁。"""

    #: 选项值（remove / add / update / dismiss / keep / check_only / ignore / no_data / not_applicable /
    #: include / exclude / "<候选格>|keep" / "<候选格>|pick:<词>" / 新工作表名……，P3-SPEC 3.2）
    value: str
    #: 给人看的一句话（「去掉标签「7-8」」）
    label: str
    #: 后果说明（「今后各期如果又出现「7-8」，会再次拒收」）
    detail: str = ""
    #: 选它必须写理由（1–200 字）。理由进配方哈希：改理由要重新预览
    needs_reason: bool = False
    #: 这是破坏性变更（如常量换值）
    breaking: bool = False


@dataclass
class FixAnchor:
    """提议对应哪一条问题（P3-SPEC 3.1）。staging_out 按它把 fix_ids 写到对应的问题上，界面只按 fix_ids 放按钮。"""

    kind: Literal["problem", "recipe_problem", "sheet_renamed"]
    #: problem / recipe_problem：在传给 propose_fixes 的 problems / recipe_problems 列表里的下标；sheet_renamed 为 None
    index: int | None = None
    #: sheet_renamed：本期的工作表名；其余 None
    sheet: str | None = None


@dataclass
class FixProposal:
    #: "fx-" + sha1(kind + "|" + 目标键)[:12]：同样的问题得到同样的 id
    id: str
    #: FIX_KINDS 之一
    kind: str
    #: 触发它的问题 code（sheet_renamed 触发时为 None）
    problem_code: str | None
    title: str
    #: 网格上要标出的格（「工作表!A1」）
    cells: list[str]
    #: 目标（{"segment", "labels"}、{"relation", "fact", "total", "parts"}……，P3-SPEC 3.2）
    target: dict[str, Any]
    options: list[FixOption]
    anchor: FixAnchor


#: 框选「选它是什么」的封闭取值（P3-SPEC 4.2）
SELECTION_AS = ("list", "crosstab", "segment", "derived", "section_title", "ignore_rows", "ignore_columns",
                "ignore_outside")
#: 框选的 ref：不带工作表名的 A1 区域，列字母大写，行号从 1 起（界面按网格坐标生成，不会有 $ 和小写）
_SELECTION_REF = re.compile(r"[A-Z]{1,3}[1-9][0-9]{0,6}(?::[A-Z]{1,3}[1-9][0-9]{0,6})?")


@dataclass
class Selection:
    """网格上的一次框选。**JSON 里的键是 as**（Python 关键字），dataclass 字段叫 as_：接口层一律经
    from_json / to_json 改名，不要直接 asdict。"""

    sheet: str
    #: A1 区域（「C5:F8」），不带工作表名
    ref: str
    #: SELECTION_AS 之一
    as_: str
    #: list：header_rows、table、bottom（box / auto）；segment：role、table；derived：keep；section_title：segment；
    #: ignore_*：reason
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: Any) -> "Selection":
        """请求体里的 {"sheet", "ref", "as", "options"?} → Selection，同时把形状校验做完。

        **任何形状不对都只抛 ValueError**（不抛 KeyError、TypeError），消息是可以直接给人看的中文：接口层只要
        ``except ValueError`` 就能映射成 422 edit_invalid（P3-SPEC 9.1），不会因为请求体写得怪而落到 500。
        这里查的是「请求本身对不对」：body 是对象；sheet 是非空文字；ref 是不带工作表名的 A1 区域（大写列字母，
        「C5」或「C5:F8」）；as 是 SELECTION_AS 之一；options 缺省、为 null 或是对象。框和网格、配方的关系
        （超出网格、重叠、表头为空……）是 selection_edit 的事，报成 EditResult.problems，不在这里。"""
        if not isinstance(data, dict):
            raise ValueError("框选的请求格式不对：应为一个对象")
        sheet, ref, as_, options = data.get("sheet"), data.get("ref"), data.get("as"), data.get("options")
        if not isinstance(sheet, str) or not sheet.strip():
            raise ValueError("框选没有指明工作表")
        if not isinstance(ref, str) or not _SELECTION_REF.fullmatch(ref):
            raise ValueError("框选的区域写法不对：应为「C5」或「C5:F8」这样的单元格区域")
        if not isinstance(as_, str) or as_ not in SELECTION_AS:
            raise ValueError("框选没有指明选中的是什么，或取值不在可选范围内")
        if options is not None and not isinstance(options, dict):
            raise ValueError("框选的选项格式不对：应为一个对象")
        return cls(sheet=sheet, ref=ref, as_=as_, options=dict(options or {}))

    def to_json(self) -> dict[str, Any]:
        return {"sheet": self.sheet, "ref": self.ref, "as": self.as_, "options": dict(self.options)}


@dataclass
class Anchor:
    """框选换算出的锚点，给界面显示「按文字定位」用。"""

    kind: Literal["header", "row_label", "section_title", "total_word", "axis", "after_title", "outside_text"]
    text: str
    cell: str | None = None


@dataclass
class EditResult:
    """一次修复（fix_ops）或框选换算（selection_edit）的结果。ok=False 时 problems 写原因（category=recipe），
    ops 为空，界面不能应用。"""

    ok: bool
    kind: Literal["fix", "selection"]
    #: 修复：目标键（remove_label:日间:7-8）；框选：<as>:<块或分段 id>。确认项 fix:<key> / select:<key> 用它
    key: str
    title: str
    #: 人话摘要，确认项和修改记录用
    summary: list[str]
    #: 按完整形式写的 JSON Patch（add / replace / remove，不给用户看）
    ops: list[dict[str, Any]]
    anchors: list[Anchor]
    notes: list[str]
    problems: list[Problem]
    #: apply_patch → validate 解析 → recipe_sha256，与暂存区的哈希同一口径；ok=False 时 None
    recipe_sha256_after: str | None = None
    #: 框选：换算出的期望区域（列表 {"header", "data", "total"}；交叉表 {"axis", "labels", "values"}）
    expected: dict[str, str | None] | None = None
    #: 框选：新增或替换的块 id（重放比对按它过滤 RegionMark）
    block: str | None = None
    #: 只在服务端内部用：这次修改是否带破坏性变化（WP-2 按所选 FixOption.breaking 或换算结果填）。
    #: **注意与 EditPreview JSON 的 breaking 不是同一个值**：预览里的 breaking 是「表名 → 人话列表」（WP-5 用
    #: recipe_confirm.table_changes 拿修改后的配方对现行配方算，P3-SPEC 9.4），没有破坏性变化时是 {}。WP-5 组装
    #: 预览时如果从 asdict(EditResult) 出发，必须总是用那份字典覆盖 breaking：漏了的话界面收到 false，
    #: Object.entries(false) 是空数组，破坏性变化会不声不响地从预览里消失
    breaking: bool = False


@dataclass
class ReplayCompare:
    """框选的期望区域与干跑后执行器认出的区域比对（P3-SPEC 4.5）。match=False 不阻止应用。"""

    expected: dict[str, str | None]
    actual: dict[str, str | None]
    match: bool
    #: 不一致时逐条写人话（「重放的数据区到第 10 行，比框多 2 行（第 9–10 行）」）
    diffs: list[str]
    #: 部分干跑时只比较前多少行；完整比较为 None
    window_rows: int | None = None


#: 累积计划的 action（P3-SPEC 2.4）
ACCUMULATE_ACTIONS = ("replace", "first", "append", "replace_period", "restart", "rejected")


@dataclass
class ChangeClass:
    """classify_changes 的结果（P3-SPEC 2.5）。"""

    change: Literal["same", "compatible", "retire", "semantic"]
    #: [{"table", "column" | None}]
    added: list[dict[str, Any]]
    #: 这次才退役的，出必勾项 retire:*
    retired_new: list[dict[str, Any]]
    #: 早已退役、仍保留在并集里的，不出确认项
    retired_existing: list[dict[str, Any]]
    #: 不兼容变化的人话
    semantic: list[str]
    #: 各期维度取值的差异：[{"table", "column", "segment", "periods": [{"start", "end", "missing", "extra"}]}]
    label_sets: list[dict[str, Any]]


@dataclass
class AccumulatePlan:
    """累积计划（WP-4 plan_accumulate 产出，JSON 形状见 P3-SPEC 9.4，TrialOut.accumulate 原样给界面）。"""

    mode: str
    #: ACCUMULATE_ACTIONS 之一
    action: str
    #: 本期 {"start", "end", "source"}：source 是 "cells"（取自表内统计期）或 "human"（人工录入）；
    #: 每期替换且没有统计期时 start、end 为 None
    period: dict[str, Any]
    #: 结果快照的各期，按统计期排序：[{"import_id", "seq", "start", "end", "file_name", "new", "rows", "blockers"}]，
    #: 本期 import_id、seq 为 None、new 为 True
    parts: list[dict[str, Any]]
    #: 被替换的那一期（replace_period），没有为 None
    replaces: dict[str, Any] | None
    #: restart、模式切换时移出当前版本的各期
    dropped: list[dict[str, Any]]
    #: 部分重叠的各期（rejected；restart 时只作提示）
    overlaps: list[dict[str, Any]]
    #: 相邻两期之间的空缺 [{"start", "end"}]
    gaps: list[dict[str, str]]
    #: 本期早于已有各期
    backfill: bool
    #: ChangeClass.change
    change: str
    added: list[dict[str, Any]]
    retired_new: list[dict[str, Any]]
    retired_existing: list[dict[str, Any]]
    semantic: list[str]
    label_sets: list[dict[str, Any]]
    #: "replace->accumulate" / "accumulate->replace" / None
    mode_switch: str | None
    #: first / restart 的原因（blockers、没有统计期），其余 None
    reason: str | None
    #: 物化了的并集 {"union_id", "db_sha256", "rows"}；没有物化为 None
    union: dict[str, Any] | None


@dataclass
class PartInfo:
    """当前快照里的一期（WP-5 组装，交给 plan_accumulate / materialize_union）。"""

    import_id: str
    seq: int
    #: 统计期取自该期导入清单（内容寻址），再与 table_imports 的库列比对（P3-SPEC 2.3）
    start: str
    end: str
    build_id: str
    db_path: str
    #: 取自 TableBuild
    db_sha256: str
    #: 该期导入清单里记的库哈希（物化第 0 步比对用）
    manifest_db_sha256: str | None
    recipe_id: str | None
    recipe: Recipe | None
    recipe_sha256: str | None
    raw_state: str
    file_name: str
    manifest_artifact: str | None
    #: 表 → 该期行数
    rows: dict[str, int] = field(default_factory=dict)
    overrides: int = 0
    waivers: int = 0


@dataclass
class UnionPart:
    """物化的一期。"""

    #: None = 本期（试运行库）
    import_id: str | None
    start: str
    end: str
    db_path: str
    recipe: Recipe
    #: 期望的库哈希：已有的期取 TableBuild，本期取试运行回执
    db_sha256: str
    manifest_db_sha256: str | None = None
    #: 该期登记的表哈希（组合定义要用，P3-SPEC 2.6 第 6 步）
    table_hashes: dict[str, str] = field(default_factory=dict)


@dataclass
class UnionReport:
    """materialize_union 的结果。UnionBuild.report = asdict(UnionReport) 的 JSON，登记进 TableBuild.report。"""

    #: 表 → 列（目标表结构加退役列）
    tables: dict[str, list[ColumnOut]]
    grains: dict[str, list[str]]
    rows: dict[str, int]
    #: 与 parts 同序：表 → {"union": [a, b], "part": [1, n]}
    part_rows: list[dict[str, dict[str, list[int]]]]
    table_hashes: dict[str, str]
    null_counts: dict[str, dict[str, int]]
    #: 与 parts 同序（说明的空值只按含这一列的期统计）
    part_null_counts: list[dict[str, dict[str, int]]]
    #: [{"table", "column" | None, "periods": [...]}]
    added: list[dict[str, Any]]
    retired: list[dict[str, Any]]
    #: U1–U3
    checks: list[CheckResult]
    ok: bool
    db_sha256: str


@dataclass
class UnionNotePart:
    """build_union_notes 的一期输入。"""

    recipe: Recipe
    extraction: Extraction
    checks: list[CheckResult]
    acceptances: list[Acceptance]
    null_counts: dict[str, dict[str, int]] | None
    start: str
    end: str


# ==========================================================================
# 期 3：版本页（P3-SPEC 7.1）
# ==========================================================================

#: 快照不能启用的原因（SnapshotOut.reason_code，P3-SPEC 7.1 的补充）。7.1 只给了人话 reason，而 available=false
#: 同时覆盖「已回收」和「数据文件已丢失」，界面分不出该用哪句文案，只能去匹配服务端的原话；WP-5 和 WP-7 并行，
#: 各写一套文字必然对不上。所以加一个机器可读的 code：
#: - WP-5 列快照时 reason_code 取这几个值之一（能启用时为 None），reason 一律取 SNAPSHOT_NOT_ACTIVATABLE[code]，
#:   不自己另写文字。几种同时成立时按字典顺序取第一个：已是当前版本 > 已回收 > 数据文件已丢失 > 含作废的导入；
#: - WP-7 按 VERSIONS_TEXT.notActivatable[reason_code] 显示（键与这里相同），没有 reason_code 时退回显示 reason。
#: 文字与附录 C 的 notActivatable 逐字相同，所以无论界面走哪条路，显示都一样。
#: 遮罩列丢失（mask_lost）不在这里：它不让快照变成不能启用，只是启用时要确认。哈希校验（snapshot_tampered）
#: 只在启用时算，列表不算，所以也不在这里
SnapshotReasonCode = Literal["current", "retired", "file_lost", "contains_revoked"]
SNAPSHOT_NOT_ACTIVATABLE: dict[str, str] = {
    "current": "已是当前版本",
    "retired": "已回收，无法启用",
    "file_lost": "数据文件已丢失，无法启用",
    "contains_revoked": "包含已作废接受的导入，无法启用",
}
