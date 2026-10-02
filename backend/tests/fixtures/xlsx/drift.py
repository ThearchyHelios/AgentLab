"""9.2 的 31 例漂移夹具（30 例 + D28b）及各例的期望。

基线是 D00 首次导入并启用（8 月 31 天）；其余各例作为「上传新一期」按参考配方重放，与 D00 比差异。
配方没变，所以 7.5 的「配方类」确认项都不出现，confirms 只列「本期类」和 diff:*（D00 首次导入的确认项见
flow.FLOW_EXPECT["confirms"]）。差异卡的基础集合是 {period, rows}：文件名只变日期不出 file_name，占位符
原文集合不变不出 placeholders，核对的 (id, 状态) 集合不变不出 checks。diff_kinds 是**精确集合**。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable

from .flow import flow_workbook

AUG = dt.date(2026, 8, 1)
SEP = dt.date(2026, 9, 1)
OCT = dt.date(2026, 10, 1)

#: 三张表的行数：日客流 / 时段客流 / 时段客流_表内合计
ROWS_31 = (31, 527, 93)
ROWS_30 = (30, 510, 90)


@dataclass
class DriftCase:
    code: str
    title: str
    #: seed → (字节, 文件名)
    build: Callable[[int], tuple[bytes, str]]
    #: passed / confirm / rejected / decision
    expect: str
    #: 三张表的行数（拒收为 None）
    rows: tuple[int, int, int] | None
    #: 期望出现的问题 code 或核对 id（rejected / decision）
    codes: tuple[str, ...]
    #: 期望出现的「本期类」「diff」确认项 id（精确 id；带 * 的按前缀）
    confirms: tuple[str, ...]
    #: 差异卡里出现的 kind 的精确集合（拒收为 None）
    diff_kinds: frozenset[str] | None
    #: 说明里必须出现的片段
    note_has: tuple[str, ...] = ()
    #: 说明里不得出现的片段
    note_lacks: tuple[str, ...] = ()


def _build(start: dt.date, days: int, variant: str) -> Callable[[int], tuple[bytes, str]]:
    def build(seed: int) -> tuple[bytes, str]:
        return flow_workbook(start, days, seed=seed, variant=variant)
    return build


_BASE = frozenset({"period", "rows"})
_NOTE_E7 = ("本期原表附有说明文字",)


def _case(code: str, title: str, expect: str, *, start: dt.date = SEP, days: int = 30,
          rows: tuple[int, int, int] | None = ROWS_30, codes: tuple[str, ...] = (),
          confirms: tuple[str, ...] = (), diff: frozenset[str] | set[str] | None = _BASE,
          note_has: tuple[str, ...] = (), note_lacks: tuple[str, ...] = ()) -> DriftCase:
    if expect == "rejected":
        rows, diff = None, None
    return DriftCase(code=code, title=title, build=_build(start, days, code), expect=expect, rows=rows,
                     codes=codes, confirms=confirms, diff_kinds=None if diff is None else frozenset(diff),
                     note_has=note_has, note_lacks=note_lacks)


_CASES = [
    # ---- 通过（12） ----
    _case("D00", "基线：8 月 31 天（同一文件再传是 same_as_import，提交 unchanged）", "passed", start=AUG, days=31,
          rows=ROWS_31, diff=frozenset(), note_has=("已逐日核对",), note_lacks=_NOTE_E7),
    _case("D01", "9 月 30 天", "passed"),
    _case("D02", "10 月 31 天（行数与 D00 相同）", "passed", start=OCT, days=31, rows=ROWS_31, diff={"period"}),
    _case("D03", "10 月 1–15 日", "passed", start=OCT, days=15, rows=(15, 255, 45)),
    _case("D03b", "10 月 1–7 日", "passed", start=OCT, days=7, rows=(7, 119, 21)),
    _case("D12", "B2 第二个日期省略年份", "passed", diff=_BASE | {"context_text"}),
    _case("D14", "去掉第 8 行空行", "passed"),
    _case("D15", "分段标题不合并", "passed"),
    _case("D17", "B3 改成「客流汇总表（2026年9月）」：进 annotated，免 outside_digits", "passed",
          diff=_BASE | {"context_source_added"}, note_has=_NOTE_E7),
    _case("D23", "文件名改成「9月客流.xlsx」：C2 passed → info", "passed", diff=_BASE | {"file_name", "checks"}),
    _case("D25", "保留 fullCalcOnLoad、缓存正确：K1 passed 带「保存值与重算一致」", "passed",
          diff=_BASE | {"full_calc"}),
    _case("D27", "B3 写成「客流汇总表（2026年9月1日至2026年9月30日）」：C1 两处一致、进 annotated", "passed",
          diff=_BASE | {"context_source_added"}, note_has=_NOTE_E7),
    # ---- 需要确认（11） ----
    _case("D05", "夜间块（标题、时段、合计）移到日间块之前", "confirm", diff=_BASE | {"block_order"},
          confirms=("diff:block_order:客流汇总",)),
    _case("D06", "日客流三行移到最后，前面加标题「日客流（人次）」", "confirm",
          diff=_BASE | {"block_order", "outside_added"},
          confirms=("diff:block_order:客流汇总", "diff:outside:客流汇总!B*")),
    _case("D07", "合计行改成写死的数（值正确）：K1 passed，没有 G1", "confirm",
          diff=_BASE | {"checks", "derived_form"}, confirms=("diff:derived_form:夜间合计",)),
    _case("D10", "表头改成日期格（显示格式 m\"月\"d\"日\"）", "confirm", diff=_BASE | {"axis_form"},
          confirms=("diff:axis_form:交叉表",)),
    _case("D11", "时段写成 07:00-08:00（时段仍存 7-8，没有 diff:canon:*）", "confirm",
          diff=_BASE | {"label_writing"}, confirms=("diff:label_writing:日间", "diff:label_writing:夜间")),
    _case("D19", "日客流三行顺序改成 分区甲、分区乙、全日客流", "confirm", diff=_BASE | {"row_order"},
          confirms=("diff:row_order:日客流",)),
    _case("D22", "合计标签写成 18:00-22:00合计（合计项仍是 18-22时合计）", "confirm",
          diff=_BASE | {"label_writing"}, confirms=("diff:label_writing:夜间合计",)),
    _case("D24", "时段用 en-dash", "confirm", diff=_BASE | {"label_writing"},
          confirms=("diff:label_writing:日间", "diff:label_writing:夜间")),
    _case("D20", "多一个可见工作表「说明」", "confirm", confirms=("sheet_extra:说明",)),
    _case("D28", "表下新增「注：9月15日闸机故障，当日客流为估算值」", "confirm", diff=_BASE | {"outside_added"},
          confirms=("outside_digits:客流汇总!B32", "diff:outside:客流汇总!B32"), note_has=_NOTE_E7),
    _case("D28b", "表下新增「注：2026年9月数据为初步统计，待修订」：进 annotated、C1 两处一致，仍要确认", "confirm",
          diff=_BASE | {"outside_added"}, confirms=("outside_digits:客流汇总!B32", "diff:outside:客流汇总!B32"),
          note_has=_NOTE_E7),
    # ---- 拒收：结构类（6） ----
    _case("D04", "日间少了 7-8 行", "rejected", codes=("label_missing",)),
    _case("D09", "日间标题改成「日间分时段客流（人次）」", "rejected", codes=("title_not_found",)),
    _case("D13", "日客流加「分区丙（人次）」，全日 = 甲 + 乙 + 丙", "rejected", codes=("label_unexpected",)),
    _case("D16", "右侧多一列「合计」（每行 SUM 公式，有缓存）", "rejected", codes=("axis_extra_cells",)),
    _case("D18", "日间末尾多一行公式小计「7-18 时合计」", "rejected", codes=("label_unparsed",)),
    _case("D21", "表下空一行补录「补录（人次）」「1,234」「2,345」（B32:D32）", "rejected", codes=("row_unclaimed",)),
    # ---- 拒收：合计核对（1）、需要决定（1） ----
    _case("D08", "写死的合计，「18-22 时合计」第 5 个日期 +100：K1 mismatch，定位 G28", "rejected", codes=("K1",)),
    # D26 的说明片段是「写理由接受 R1 之后」重新生成的说明
    _case("D26", "第 10 个日期全日 = 甲 + 乙 + 7：R1 mismatch，写理由接受后可提交", "decision",
          codes=("R1",), diff=_BASE | {"checks"}, note_has=("已由用户确认接受",), note_lacks=("已逐日核对",)),
]

DRIFT_CASES: dict[str, DriftCase] = {c.code: c for c in _CASES}

#: final.md 2.8 的分组（12 / 8 / 2 / 6 / 2；D28b 归「需要确认才能启用」，那一组变成 3 例）。
#: 与 expect 的对应：通过 → passed；版式变化需确认、需要确认才能启用 → confirm；拒收可自助修复、D08 → rejected；
#: D26 → decision（数据质量类，写理由接受后可提交）
GROUPS = {
    "通过": ("D00", "D01", "D02", "D03", "D03b", "D12", "D14", "D15", "D17", "D23", "D25", "D27"),
    "版式变化需确认": ("D05", "D06", "D07", "D10", "D11", "D19", "D22", "D24"),
    "需要确认才能启用": ("D20", "D28", "D28b"),
    "拒收可自助修复": ("D04", "D09", "D13", "D16", "D18", "D21"),
    "正确拒收": ("D08", "D26"),
}
