"""框选转锚点（期 3，WP-2，P3-SPEC 第 4 节）：网格上框一块区域、选它是什么，换算成按文字认的配方规则。

**坐标不进配方**（4.1）：配方 schema 里没有任何坐标字段（extra="forbid"），框选的坐标只在换算的那一刻用两次——
从格子里取锚点文字（表头、行标签、分段标题、合计词、轴右侧表头、同一行的文字），以及换算完干跑后把执行器认出的
区域和框比对（replay_compare）。所以下个月行列挪了位置，配方照样按文字找到它（4.6 第 2 条用挪位的文件钉住）。

**换算规则全部复用起草器**（期 2 规格 6.1，评审二-m12）：列表直接用起草器的列表计划和块构造函数
（recipe_suggest.list_block），框只决定表头行和列范围，列名、单位、类型、grain、合计行、跳过空行、占位符、合并
单元格填充都由起草器按框内的数据推断；交叉表直接取规则起草对整张工作表的结果。换算不了就明确说为什么
（EditResult.ok=False，problems 的 code 见 4.3），不退回写死坐标，也不猜。

客户端传来的文字只有两种会进补丁：理由，以及经过名字校验、撞名检查的表名（options.table，评审一-m6）。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from app.data import recipe_suggest as S
from app.data.names import canon, collide_key, name_problem, to_sql_name
from app.data.recipe_fixes import (
    REASON_MAX,
    EditRequestError,
    all_ids,
    finish,
    has_digit,
    keep_ok,
    ptr,
    quoted,
    table_index,
    total_blockers,
    total_ops,
    unique_id,
    unique_name,
)
from app.data.recipe_parsers import (
    LABEL_MAX,
    candidate_words,
    hour_range,
    hour_range_total,
    looks_numeric_text,
    match_key,
    month_day_or_date,
    split_unit_suffix,
    text_label,
)
from app.data.recipe_types import (
    RECIPE_FORMAT,
    Anchor,
    CrosstabBlock,
    Draft,
    EditResult,
    Extraction,
    Grid,
    ListBlock,
    Problem,
    Recipe,
    ReplayCompare,
    Selection,
)
from app.data.xlsx_scan import cell_ref, col_letter, parse_ref

#: 交叉表：框与规则草稿认出的交叉表至少重叠这么多（占框的比例）才算框住了它（4.2）
CROSSTAB_OVERLAP = 0.5
#: 锚点的长度上限（契约 IgnoreRow.label / IgnoreOutside.anchor 40 字、ListBlock.after_title 40 字、IgnoreColumn.header 80 字）
_ANCHOR_MAX, _HEADER_MAX = 40, 80

Rect = tuple[int, int, int, int]


# ==========================================================================
# 小工具
# ==========================================================================


def _rect(ref: str) -> Rect:
    """「C5」或「C5:F8」→ (r1, c1, r2, c2)，左上到右下（反着框也行）。"""
    a, _, b = ref.partition(":")
    r1, c1 = parse_ref(a)
    r2, c2 = parse_ref(b) if b else (r1, c1)
    return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)


def _a1(rect: Rect | None) -> str | None:
    if rect is None:
        return None
    r1, c1, r2, c2 = rect
    return cell_ref(r1, c1) if (r1, c1) == (r2, c2) else f"{cell_ref(r1, c1)}:{cell_ref(r2, c2)}"


def _rows_text(r1: int, r2: int) -> str:
    return f"第 {r1} 行" if r1 == r2 else f"第 {r1}–{r2} 行"


def _intersects(a: Rect, b: Rect) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _area(a: Rect) -> int:
    return (a[2] - a[0] + 1) * (a[3] - a[1] + 1)


def _overlap(a: Rect, b: Rect) -> int:
    if not _intersects(a, b):
        return 0
    return (min(a[2], b[2]) - max(a[0], b[0]) + 1) * (min(a[3], b[3]) - max(a[1], b[1]) + 1)


def _bbox(rects: list[Rect]) -> Rect | None:
    if not rects:
        return None
    return min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)


def _cells_bbox(cells: set[tuple[int, int]] | list[tuple[int, int]]) -> Rect | None:
    cells = list(cells)
    if not cells:
        return None
    return min(r for r, _ in cells), min(c for _, c in cells), max(r for r, _ in cells), max(c for _, c in cells)


def _text_of(grid: Grid, r: int, c: int) -> str | None:
    """格子里的文字（去首尾空白）：不是文字、是公式或只有空白时 None。"""
    cell = grid.get(r, c)
    if cell is None or cell.formula is not None or not isinstance(cell.value, str):
        return None
    t = cell.value.strip()
    return t or None


def _textual(grid: Grid, r: int, c: int) -> bool:
    """这一格能不能作表头文字：是文字、不像数、不是日期写法。"""
    cell = grid.get(r, c)
    if cell is None:
        return True
    if cell.formula is not None or not isinstance(cell.value, str):
        return False
    return not looks_numeric_text(cell.value) and not S._date_kind(cell.value)


def _row_empty(grid: Grid, r: int, c1: int, c2: int) -> bool:
    return all(grid.get(r, c) is None for c in range(c1, c2 + 1))


def _reason(sel: Selection) -> str:
    """忽略类框选的理由（必填，1–200 字）。理由进配方哈希：预览和应用必须用同一个理由。"""
    raw = sel.options.get("reason")
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise EditRequestError("reason_required", "忽略要写明理由")
    if len(text) > REASON_MAX:
        raise EditRequestError("edit_invalid", f"理由不能超过 {REASON_MAX} 字")
    return text


@dataclass
class _Ctx:
    recipe: dict[str, Any] | None
    parsed: Recipe | None
    grid: Grid
    sel: Selection
    rect: Rect
    ext: Extraction | None
    retired: dict[str, list[str]]
    #: 配方里对应这张工作表的下标（没有为 None）
    si: int | None = None
    problems: list[Problem] = field(default_factory=list)

    @property
    def sheet(self) -> str:
        return self.grid.sheet

    def coord(self, r: int, c: int) -> str:
        return f"{self.sheet}!{cell_ref(r, c)}"

    def fail(self, code: str, message: str, cells: list[str] | None = None) -> None:
        box = f"{self.sheet}!{_a1(self.rect)}"
        self.problems.append(Problem(code, "recipe", message, cells=(cells or [box])[:20], model_message=code))


def _failed(ctx: _Ctx) -> EditResult:
    return EditResult(ok=False, kind="selection", key=f"{ctx.sel.as_}:", title=_TITLES.get(ctx.sel.as_, "框选"),
                      summary=[], ops=[], anchors=[], notes=[], problems=list(ctx.problems))


_TITLES = {"list": "框选为列表", "crosstab": "框选为交叉表", "segment": "框选为分段", "derived": "框选为合计行",
           "section_title": "框选为分段标题", "ignore_rows": "忽略框选的行", "ignore_columns": "忽略框选的列",
           "ignore_outside": "忽略框选行里导入区域之外的数字"}


def _sheet_of(recipe: dict[str, Any] | None, grid: Grid, ext: Extraction | None) -> int | None:
    """配方里对应这张网格的工作表：干跑认出的（sheets.matched：工作表 id → 实际名）优先，其次按名字。"""
    if not recipe:
        return None
    sheets = recipe.get("sheets") or []
    if ext is not None:
        for sid, actual in ext.sheets.matched.items():
            if match_key(actual) == match_key(grid.sheet):
                hit = next((i for i, s in enumerate(sheets) if s.get("id") == sid), None)
                if hit is not None:
                    return hit
    key = match_key(grid.sheet)
    return next((i for i, s in enumerate(sheets) if match_key((s.get("match") or {}).get("name")) == key), None)


@dataclass
class _Located:
    bi: int
    block: dict[str, Any]
    rects: list[Rect]
    cross: Any = None          # recipe_suggest._CrossLoc
    lst: Any = None            # recipe_suggest._ListLoc


def _locate(ctx: _Ctx) -> list[_Located]:
    """这张工作表上配方各块认领的区域。干跑结果带块 id（RegionMark.block，期 3）时按它取；否则（期 2 的执行器、
    没有干跑）用起草器的定位函数在网格上近似。交叉表另外总是用起草器定位一遍：标签列、轴行、各分段的行要用到。"""
    if ctx.si is None or ctx.parsed is None:
        return []
    sheet = ctx.recipe["sheets"][ctx.si]          # type: ignore[index]
    model = ctx.parsed.sheets[ctx.si]
    tagged: dict[str, list[Rect]] = {}
    if ctx.ext is not None and any(m.block for m in ctx.ext.regions):
        for m in ctx.ext.regions:
            if m.block and match_key(m.sheet) == match_key(ctx.sheet):
                tagged.setdefault(m.block, []).append(_rect(m.ref))
    out: list[_Located] = []
    # 已用的表头行只对表头相同的块排除（上下两张同表头的表各认各的）；左右并排的两张表表头在同一行，不能互相排除
    taken: dict[frozenset[str], set[int]] = {}
    for bi, (bd, bm) in enumerate(zip(sheet.get("blocks") or [], model.blocks)):
        loc = _Located(bi, bd, list(tagged.get(bm.id, [])))
        if isinstance(bm, CrosstabBlock):
            loc.cross = S._locate_crosstab(bm, ctx.grid)
            if not loc.rects and loc.cross is not None:
                box = _cells_bbox(loc.cross.region)
                loc.rects = [box] if box else []
        elif isinstance(bm, ListBlock):
            keys = frozenset(match_key(c.header) for c in bm.columns)
            loc.lst = S._locate_list(bm, ctx.grid, taken.setdefault(keys, set()))
            if loc.lst is not None:
                taken[keys].update(loc.lst.header_rows)
                if not loc.rects:
                    box = _cells_bbox(loc.lst.region)
                    loc.rects = [box] if box else []
        out.append(loc)
    return out


def _crosstab_at(ctx: _Ctx, located: list[_Located]) -> _Located | None:
    """框落在哪个交叉表块的标签列上：框含标签列、不越过标签列往左、在日期表头之下、不越过最后一个日期往右。"""
    r1, c1, r2, c2 = ctx.rect
    for loc in located:
        x = loc.cross
        if x is None:
            continue
        if c1 == x.label_col and c2 <= x.c2 and r1 > x.axis_row:
            return loc
    return None


def _labels(ctx: _Ctx, col: int) -> list[tuple[int, str]] | None:
    """框内各行标签列的文字；有行没有文字标签时报 selection_label_blank 并返回 None。"""
    r1, _c1, r2, _c2 = ctx.rect
    out: list[tuple[int, str]] = []
    blank: list[int] = []
    for r in range(r1, r2 + 1):
        t = _text_of(ctx.grid, r, col)
        if t is None:
            blank.append(r)
        else:
            out.append((r, t))
    if blank:
        ctx.fail("selection_label_blank",
                 f"{'、'.join(f'第 {r} 行' for r in blank[:8])}的标签列（{col_letter(col)} 列）没有文字，无法按标签认：请调整框的上下范围，"
                 "或改选别的类型", [ctx.coord(r, col) for r in blank])
        return None
    return out


def _new_table_name(ctx: _Ctx, want: str, used: set[str]) -> str | None:
    """新表名：客户端给了 options.table 时过名字校验和撞名检查（含当前版本里退役的表名，评审一-m6），否则按 want
    推出并加 _2 去重。不合法或撞名时报问题、返回 None。"""
    given = ctx.sel.options.get("table")
    if given is None or given == "":
        return unique_name(to_sql_name(want, fallback=f"表{len(used) + 1}") if want else f"表{len(used) + 1}", used)
    if not isinstance(given, str):
        raise EditRequestError("edit_invalid", "表名要是文字")
    why = name_problem(given)
    if why:
        ctx.fail("selection_name_invalid", f"表名不能用：{why}")
        return None
    if collide_key(given) in used:
        ctx.fail("selection_name_taken", f"表名「{given}」与已有的表或已退役的表重名（名字不区分大小写和全角半角），请换一个名字")
        return None
    used.add(collide_key(given))
    return given


def _used_tables(ctx: _Ctx, *, except_: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """已用的表名（collide_key）：工作配方里的表和当前版本里退役的表；except_ 里的（被替换的块自己的表）不算。"""
    used = {collide_key(t.get("name", "")) for t in (ctx.recipe or {}).get("tables") or []
            if t.get("name") not in except_}
    used |= {collide_key(t) for t in ctx.retired if t not in except_}
    return used


# ==========================================================================
# 列表
# ==========================================================================


def _header_problems(ctx: _Ctx, rows: list[int], cols: list[int]) -> dict[int, str] | None:
    """表头：每列按执行器的拼法得到（合并区取左上格、去掉空的和与上一层相同的、用「_」拼接）。任一列为空、
    是数字或日期、两列 match_key 相同，各报一条（4.2 列表第 1 条）。"""
    texts = S._header_texts(ctx.grid, rows, cols)
    # 空：表头那几行这一列一格都没有（是数字、日期的另报 not_text，不算空）
    blank = [c for c in cols if c not in texts and all(ctx.grid.get(r, c) is None for r in rows)]
    if blank:
        ctx.fail("selection_header_blank",
                 f"{_rows_text(rows[0], rows[-1])}的 {'、'.join(col_letter(c) for c in blank)} 列表头为空：列表的每一列都要有表头。"
                 "请调整框的左右范围，或改选「忽略这些列」", [ctx.coord(rows[-1], c) for c in blank])
    bad = sorted({c for c in cols for r in rows if not _textual(ctx.grid, r, c)})
    if bad:
        ctx.fail("selection_header_not_text",
                 f"{'、'.join(col_letter(c) for c in bad)} 列的表头是数字或日期，不像表头：框的第一行应是表头。"
                 "请把框的上边移到表头所在的行", [ctx.coord(rows[0], c) for c in bad])
    seen: dict[str, int] = {}
    dup: list[int] = []
    for c in cols:
        if c in texts:
            k = match_key(texts[c])
            if k in seen:
                dup.append(c)
            seen.setdefault(k, c)
    if dup:
        ctx.fail("selection_header_duplicate",
                 f"表头{quoted([texts[c] for c in dup])}出现了不止一次：同一张列表里表头不能重复。请调整框的左右范围",
                 [ctx.coord(rows[-1], c) for c in dup])
    return None if (blank or bad or dup) else texts


def _header_candidates(grid: Grid, headers: list[str], n: int) -> list[int]:
    """整张网格上像这组表头的行（执行器的候选规则：过半表头命中）。"""
    want = {match_key(h) for h in headers}
    out = []
    for t in grid.row_numbers():
        rows = list(range(t, t + n))
        cols = sorted({c for r in rows for c, _ in grid.row(r)})
        keys = {match_key(x) for x in S._header_texts(grid, rows, cols).values()}
        if len(want & keys) * 2 > len(want):
            out.append(t)
    return out


def _after_title(ctx: _Ctx, top: int, cols: list[int], headers: list[str], n: int) -> tuple[str, str] | None | bool:
    """表头不止一处时找 after_title：表头行往上、块的列范围内最近的、整张表里唯一的、不含数字的文字格（4.2 第 2 条，
    评审一-M11：最近的唯一文字格常常是「2026年8月 销售月报」这种带年月的标题，写进配方等于写死期次）。
    返回 (文字, 坐标)；不需要时 None；需要但找不到时 False。"""
    others = [t for t in _header_candidates(ctx.grid, headers, n) if t != top]
    if not others:
        return None
    above = [t for t in others if t < top]
    floor = 1
    if above:
        # 上面那张表到它下方第一个空行为止：标题只在那之后找，不能拿上一张表的数据格当标题
        floor = max(above) + n
        while floor < top and not _row_empty(ctx.grid, floor, cols[0], cols[-1]):
            floor += 1
    counts: dict[str, int] = {}
    for (r, c), cell in ctx.grid.cells.items():
        if isinstance(cell.value, str) and cell.formula is None:
            counts[match_key(cell.value)] = counts.get(match_key(cell.value), 0) + 1
    for r in range(top - 1, floor - 1, -1):
        for c in cols:
            t = _text_of(ctx.grid, r, c)
            if t and len(t) <= _ANCHOR_MAX and not has_digit(t) and counts.get(match_key(t)) == 1:
                return t, ctx.coord(r, c)
    return False


def _no_extra_columns(ctx: _Ctx, rows: list[int], cols: list[int], ignored: set[str]) -> bool:
    """执行器按表头行里包含锚点的最大连续非空段认列范围（期 2 规格 4.4）：框的左右紧挨着还有非空表头时，重放会把
    它们也算进这张表（多出的列默认拒收），按文字无法表达「到这一列为止」，报 selection_extra_columns（4.2 第 3 条）。
    被替换的块上已经按表头忽略的列不算（ignored：表头的 match_key）。"""
    lo, hi = cols[0], cols[-1]
    wide = S._header_texts(ctx.grid, rows, list(range(max(1, lo - 60), hi + 61)))
    left, right = lo, hi
    while left - 1 in wide:
        left -= 1
    while right + 1 in wide:
        right += 1
    extra = [c for c in range(left, right + 1) if (c < lo or c > hi) and match_key(wide[c]) not in ignored]
    if not extra:
        return True
    r1, _c1, r2, _c2 = ctx.rect
    ctx.fail("selection_extra_columns",
             f"框的旁边紧挨着还有表头{quoted([wide[c] for c in extra])}（{'、'.join(col_letter(c) for c in extra)} 列）："
             f"按文字定位时它们会被算进这张表。请把框扩大到 {_a1((r1, left, r2, right))}，或改选「忽略这些列」",
             [ctx.coord(rows[-1], c) for c in extra])
    return False


def _bottom_box(ctx: _Ctx, plan: Any, first: int) -> bool:
    """bottom="box"：框的下边就是数据的末尾（4.2 第 5 条）。合计行照起草器的规则认（期 2 规格 6.1 第 11 条）。

    - 框内最后一个非空行 last 的第一列以合计词开头、上面还有数据行：它是合计行（rows.total_row），不是数据行。
      框把合计行也框进来是很自然的操作，当成数据行导入的话求和翻倍，而且干跑、核对、重放比对都看不出来（H2）；
    - 合计字样出现在框内别的位置（第一列但不在末尾，或在别的列上）：起草器在这里记起草失败，框选同样报错，
      不把合计当明细导入；
    - 合计行下面紧接着还有同一张表的行：它是夹在中间的小计（起草器的 _more_rows），同样报错；
    - 末尾不是合计行时，看 last 的下一行（只看框的列范围）：全空或到网格底部就「遇到空行停止」，以合计词开头
      就是合计行，都不是就报 selection_bottom_not_anchorable。看的是 last+1、不是框的下一行：框底多带几行空行
      时结果与框到 last 相同，提示里建议的末尾行也从 last+1 往下数，不会让人把下方的备注、制表人行也框进来。"""
    r1, c1, r2, c2 = ctx.rect
    grid = ctx.grid
    filled = [r for r in range(first, r2 + 1) if not _row_empty(grid, r, c1, c2)]
    if not filled:
        ctx.fail("selection_bottom_not_anchorable", "框里只有表头、没有数据行：请把框扩大到数据的实际末尾")
        return False
    last = filled[-1]
    word = S._total_word(S._label_of(grid.get(last, c1)))
    data = filled[:-1] if word is not None else filled
    if not data:
        ctx.fail("selection_bottom_not_anchorable",
                 f"框里只有表头和合计行（第 {last} 行），没有数据行：请把框扩大到数据所在的行")
        return False
    if not _no_stray_totals(ctx, data, c1, c2):
        return False
    bottom = S._bottom(grid)
    acc = S._ColTypes(grid, plan.cols)
    for r in data:
        acc.add(r)
    plan.rows = data
    gaps = [r for r in range(first, last + 1) if r not in filled]
    if gaps:
        # 框内有空行（c08 gap 那种）：跳过空行，执行器跳过空行后照样能碰到末尾的合计行；重放比对把实际的数据区摆出来
        plan.skip_blank, plan.skipped = True, gaps
    if word is not None:
        return _total_at_end(ctx, plan, last, word, bottom, acc.types())
    below = last + 1
    if below > bottom or _row_empty(grid, below, c1, c2):
        return True
    word = S._total_word(S._label_of(grid.get(below, c1)))
    if word is not None:
        return _total_at_end(ctx, plan, below, word, bottom, acc.types())
    end = below
    while end <= bottom and not _row_empty(grid, end, c1, c2) and S._total_word(S._label_of(grid.get(end, c1))) is None:
        end += 1
    ctx.fail("selection_bottom_not_anchorable",
             f"第 {below} 行还有数据：按文字无法表达「到第 {last} 行为止」。请把框扩大到数据的实际末尾（第 {end - 1} 行），"
             "改选「下边界按规则推断」，或删掉文件里多出的行", [ctx.coord(below, c1)])
    return False


def _no_stray_totals(ctx: _Ctx, data: list[int], c1: int, c2: int) -> bool:
    """数据行里不能有合计字样（起草器 _scan_block 的 stray_totals：第一列以合计词开头、或别的列整格以合计词开头）。
    起草器在这里记起草失败；框选照样不收，否则合计会被当成明细导入、求和翻倍（H2）。"""
    grid = ctx.grid
    first_col = [r for r in data if S._total_word(S._label_of(grid.get(r, c1))) is not None]
    other = [(r, c) for r in data for c in range(c1 + 1, c2 + 1)
             if (cell := grid.get(r, c)) is not None and S._total_word(cell.value) is not None]
    if first_col:
        r = first_col[0]
        ctx.fail("selection_bottom_not_anchorable",
                 f"第 {r} 行像合计行（「{S._label_of(grid.get(r, c1))}」），但它不在框的末尾：无法确定它汇总的是哪几行，"
                 f"按数据导入会把合计重复计数。合计行就是表的末尾时，请把框的下边移到第 {r} 行；它是夹在中间的小计时，"
                 "请修改文件", [ctx.coord(x, c1) for x in first_col])
        return False
    if other:
        r, c = other[0]
        ctx.fail("selection_bottom_not_anchorable",
                 f"第 {r} 行 {col_letter(c)} 列是合计字样（「{grid.get(r, c).value}」），但合计字样不在第一列，无法按合计行"
                 "核对，按数据导入会把合计重复计数：请修改文件，把合计字样写在第一列，或把框的下边移到这一行之上",
                 [ctx.coord(x, y) for x, y in other])
        return False
    return True


def _auto_settled(ctx: _Ctx, plan: Any) -> bool:
    """bottom="auto"：起草器收进来、却无法确定的行（紧挨着数据的新表头、不在末尾第一列的合计字样）照起草器记失败
    （recipe_suggest._list_failures，同一套文案）。起草器的 _scan_block 会把这些行照样收进数据行，只是另记失败；
    框选不看失败的话，夹在中间的小计会被当成明细导入、求和翻倍（H2）。"""
    failures = S._list_failures(ctx.sheet, ctx.grid, plan)
    if not failures:
        return True
    cells = [ctx.coord(r, c) for r, c in plan.stray_totals] + [ctx.coord(r, plan.cols[0]) for r, _f in plan.adjacent]
    for f in failures:
        ctx.fail("selection_bottom_not_anchorable",
                 f"{f.human}。按文字无法表达这张表到哪里为止，按数据导入会出错：请修改文件", cells)
    return False


def _total_at_end(ctx: _Ctx, plan: Any, r: int, word: str, bottom: int, types: dict[int, str]) -> bool:
    """第 r 行是表末尾的合计行：下面紧接着还有同一张表的行时，它是夹在中间的小计（起草器的 _more_rows），
    按合计行停在这里会把下面的明细丢掉，报错；否则记为合计行（执行器碰到它就停）。"""
    if S._more_rows(ctx.grid, r, plan, bottom, types):
        ctx.fail("selection_bottom_not_anchorable",
                 f"第 {r} 行像合计行（「{S._label_of(ctx.grid.get(r, plan.cols[0]))}」），但下面紧接着还有同一张表的数据："
                 "它是夹在中间的小计，按文字无法表达这张表到哪里为止，按数据导入又会把小计重复计数。请修改文件",
                 [ctx.coord(r, plan.cols[0])])
        return False
    plan.total_row, plan.total_word = r, word
    return True


def _select_list(ctx: _Ctx, located: list[_Located]) -> EditResult:
    r1, c1, r2, c2 = ctx.rect
    grid = ctx.grid
    n = ctx.sel.options.get("header_rows", 1)
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 3:
        raise EditRequestError("edit_invalid", "表头行数只能是 1 到 3")
    bottom = ctx.sel.options.get("bottom", "box")
    if bottom not in ("box", "auto"):
        raise EditRequestError("edit_invalid", "下边界只能「以框的下边为准」或「按规则推断」")
    rows_h = list(range(r1, r1 + n))
    cols = list(range(c1, c2 + 1))
    if len(cols) < 1 or r2 < r1 + n:
        ctx.fail("selection_bottom_not_anchorable", "框里只有表头、没有数据行：请把框扩大到数据的实际末尾")
        return _failed(ctx)
    # 重叠：恰好一个块时替换它（沿用块 id 和表名，免得关系、单位里的引用断掉），两个以上报错，不相交时新增
    hits = [loc for loc in located if any(_intersects(ctx.rect, r) for r in loc.rects)]
    if len(hits) > 1:
        ctx.fail("selection_overlap", f"框与{quoted([h.block.get('id') for h in hits])}几块都相交：请只框一张表")
        return _failed(ctx)
    old = hits[0] if hits else None
    if old is not None and old.block.get("layout") != "list":
        ctx.fail("selection_overlap", f"框与交叉表「{old.block.get('id')}」相交：列表不能替换交叉表。请调整框的范围")
        return _failed(ctx)
    texts = _header_problems(ctx, rows_h, cols)
    if texts is None:
        return _failed(ctx)
    ignored = {match_key(x.get("header")) for x in (old.block.get("ignore_columns") or [])} if old is not None else set()
    if not _no_extra_columns(ctx, rows_h, cols, ignored):
        return _failed(ctx)
    plan = S._ListPlan(header_rows=rows_h, cols=cols, headers=dict(texts))
    notes: list[str] = []
    if bottom == "box":
        if not _bottom_box(ctx, plan, r1 + n):
            return _failed(ctx)
    else:
        S._scan_block(grid, plan, S._bottom(grid))
        if not plan.rows:
            ctx.fail("selection_bottom_not_anchorable", "表头之下没有认出数据行：请检查框的上边是否在表头所在的行")
            return _failed(ctx)
        if not _auto_settled(ctx, plan):
            return _failed(ctx)
        last = plan.total_row or plan.rows[-1]
        if grid.truncated and last >= S._bottom(grid):
            notes.append("数据延续到已读入范围之外，完整范围在试运行时核对")
    if grid.truncated and plan.rows[-1] >= S._bottom(grid) and not notes:
        notes.append("数据延续到已读入范围之外，完整范围在试运行时核对")
    S._finish_block(grid, plan)
    old_tables = set()
    if old is not None:
        old_tables = {old.block.get("table")}
        tr = (old.block.get("rows") or {}).get("total_row") or {}
        if tr.get("keep_as"):
            old_tables.add(tr["keep_as"])
    used = _used_tables(ctx, except_=old_tables)
    given = ctx.sel.options.get("table")
    table: str | None = None
    if given not in (None, ""):
        table = _new_table_name(ctx, "", used)
        if table is None:
            return _failed(ctx)
    elif old is not None:
        table = old.block.get("table")
    block_id = old.block["id"] if old is not None else unique_id(f"列表{_list_count(ctx.recipe) + 1}",
                                                                  all_ids(ctx.recipe or {}))
    block, main, total_spec = S.list_block(ctx.sheet, plan, block_id=block_id, tables_used=used, table=table)
    # 表头上方的标题：表头不止一处时才要
    anchors = [Anchor("header", texts[c], ctx.coord(r1 + n - 1, c)) for c in cols]
    found = _after_title(ctx, r1, cols, [texts[c] for c in cols], n)
    if found is False:
        ctx.fail("selection_anchor_ambiguous",
                 "这组表头在工作表里不止一处，而上方能区分这张表的文字都含年月或数字，下一期很可能对不上：请修改文件，"
                 "或给这张表加一行不含数字的标题")
        return _failed(ctx)
    if found:
        block["after_title"] = found[0]
        anchors.append(Anchor("after_title", found[0], found[1]))
    if plan.total_row is not None:
        anchors.append(Anchor("total_word", plan.total_word or "", ctx.coord(plan.total_row, c1)))
    for c in cols:
        if S._YEAR.search(texts[c]):
            notes.append(f"表头「{texts[c]}」含年份，下一期年份变化时将无法匹配")
    if old is not None and old.block.get("ignore_columns"):
        # 被替换的块上原有的按表头忽略照旧保留：框选只决定表头行和列范围，不该悄悄丢掉人确认过的规则
        block["ignore_columns"] = list(old.block["ignore_columns"])
    for col in block["columns"]:
        retired = next((x for x in ctx.retired.get(block["table"], []) if collide_key(x) == collide_key(col["name"])
                        and x != col["name"]), None)
        if retired is not None:
            notes.append(f"列「{col['name']}」与已退役的列「{retired}」同名（不区分大小写），沿用原来的写法「{retired}」")
            col["name"] = retired
    ops = _block_ops(ctx, old, block, main, total_spec, old_tables)
    if ops is None:
        return _failed(ctx)
    total_rect = (plan.total_row, c1, plan.total_row, c2) if plan.total_row else None
    expected = {"header": _a1((r1, c1, r1 + n - 1, c2)), "data": _a1((plan.rows[0], c1, plan.rows[-1], c2)),
                "total": _a1(total_rect)}
    verb = "替换现有的" if old is not None else "新增"
    summary = [f"按框选{verb}列表「{block_id}」（表「{block['table']}」）：表头按文字{quoted([texts[c] for c in cols])}定位"]
    if found:
        summary.append(f"只在文字「{found[0]}」之后找表头")
    summary.append("下边界：" + ("到合计行为止" if plan.total_row else "遇到空行停止")
                   + ("，中间的空行跳过" if plan.skip_blank else ""))
    if bottom == "auto":
        summary.append(f"重放的数据区到第 {plan.rows[-1]} 行")
    return finish(ctx.recipe, ops, kind="selection", key=f"list:{block_id}",
                  title=f"按框选{verb}列表「{block_id}」：表头按文字定位", summary=summary, anchors=anchors,
                  notes=notes, expected=expected, block=block_id)


def _list_count(recipe: dict[str, Any] | None) -> int:
    return sum(1 for s in (recipe or {}).get("sheets") or [] for b in s.get("blocks") or [] if b.get("layout") == "list")


def _block_ops(ctx: _Ctx, old: _Located | None, block: dict, main: dict, total_spec: dict | None,
               old_tables: set[str]) -> list[dict[str, Any]] | None:
    """把一个列表块写进配方：替换旧块（连同表定义）、新增块，或者配方为空时用根替换新建配方（4.2 第 7 条）。"""
    recipe = ctx.recipe
    if not recipe:
        tables = [main] + ([total_spec] if total_spec else [])
        return [{"op": "replace", "path": "", "value": {
            "recipe_format": RECIPE_FORMAT, "sheets": [{"id": "s1", "match": {"name": ctx.sheet[:31]}, "blocks": [block]}],
            "tables": tables, "relations": []}}]
    if ctx.si is None:
        sid = _new_sheet_id(recipe)
        ops: list[dict[str, Any]] = [{"op": "add", "path": "/sheets/-", "value": {
            "id": sid, "match": {"name": ctx.sheet[:31]}, "blocks": [block]}}]
        return ops + _add_tables(recipe, [main] + ([total_spec] if total_spec else []))
    if old is None:
        ops = [{"op": "add", "path": ptr("sheets", ctx.si, "blocks", "-"), "value": block}]
        return ops + _add_tables(recipe, [main] + ([total_spec] if total_spec else []))
    # 替换：表定义原位替换（保留用户写的说明），表名变了就是改名；旧块另存的合计表这次不再另存时删掉它的定义
    # （别的块还在写入时不删）。先替换、再删（按下标从大到小）、最后追加，下标不会错位
    ops = [{"op": "replace", "path": ptr("sheets", ctx.si, "blocks", old.bi), "value": block}]
    old_total = ((old.block.get("rows") or {}).get("total_row") or {}).get("keep_as")
    replaced: set[int] = set()
    adds: list[dict] = []
    for spec, was in ((main, old.block.get("table")), (total_spec, old_total)):
        if spec is None:
            continue
        ti = table_index(recipe, was) if was else None
        if ti is None:
            adds.append(spec)
            continue
        replaced.add(ti)
        ops.append({"op": "replace", "path": ptr("tables", ti),
                    "value": {**spec, "note": recipe["tables"][ti].get("note") or ""}})
    removes = sorted({ti for name in old_tables if (ti := table_index(recipe, name)) is not None
                      and ti not in replaced and not _written_elsewhere(recipe, name, ctx.si, old.bi)}, reverse=True)
    ops += [{"op": "remove", "path": ptr("tables", ti)} for ti in removes]
    ops += [{"op": "add", "path": "/tables/-", "value": spec} for spec in adds]
    return ops


def _add_tables(recipe: dict, specs: list[dict]) -> list[dict[str, Any]]:
    return [{"op": "add", "path": "/tables/-", "value": s} for s in specs]


def _new_sheet_id(recipe: dict) -> str:
    have = {s.get("id") for s in recipe.get("sheets") or []}
    n = len(have) + 1
    while f"s{n}" in have:
        n += 1
    return f"s{n}"


def _written_elsewhere(recipe: dict, table: str, si: int, bi: int) -> bool:
    """除了 (si, bi) 这个块，还有别的块写入这张表吗（替换块时不能删掉别人还在用的表定义）。"""
    for sj, sheet in enumerate(recipe.get("sheets") or []):
        for bj, block in enumerate(sheet.get("blocks") or []):
            if (sj, bj) == (si, bi):
                continue
            if block.get("layout") == "list":
                tr = (block.get("rows") or {}).get("total_row") or {}
                if table in (block.get("table"), tr.get("keep_as")):
                    return True
            for seg in block.get("segments") or []:
                if table in (seg.get("table"), (seg.get("keep_as") or {}).get("table")):
                    return True
    return False


# ==========================================================================
# 交叉表（整块）
# ==========================================================================


def _block_tables(block: dict) -> set[str]:
    out: set[str] = set()
    for seg in block.get("segments") or []:
        if seg.get("table"):
            out.add(seg["table"])
        if (seg.get("keep_as") or {}).get("table"):
            out.add(seg["keep_as"]["table"])
    return out


def _rel_tables(rel: dict) -> set[str]:
    if rel.get("kind") == "sum_eq":
        return {rel.get("table")}
    if rel.get("kind") == "not_comparable":
        return {(rel.get("a") or {}).get("table"), (rel.get("b") or {}).get("table")}
    return set()


def _select_crosstab(ctx: _Ctx, located: list[_Located], draft_fn: Callable[[], Draft] | None) -> EditResult:
    """框只用来指认是哪一块：draft_fn() 对整张工作表起草，取认领区域与框重叠最多（且 ≥50%）的交叉表块，连同它写入的表、
    它认领的事实对应的关系、统计期上下文（配方里还没有时）一起并入。关系 id 与已有的重号时，后面的顺延（4.2）。"""
    d = draft_fn() if draft_fn is not None else None
    dr = d.recipe if d is not None else None
    no = ("框住的范围里没有认出交叉表（日期表头加行标签）：请框在交叉表上，或改选「列表」")
    if not dr:
        ctx.fail("selection_no_crosstab", no)
        return _failed(ctx)
    try:
        dmodel = Recipe.model_validate(dr)
    except ValidationError:
        ctx.fail("selection_no_crosstab", no)
        return _failed(ctx)
    best: tuple[int, dict, dict, Any] | None = None
    for dsi, dsheet in enumerate(dmodel.sheets):
        if match_key(dsheet.match.name) != match_key(ctx.sheet):
            continue
        for dbi, dblock in enumerate(dsheet.blocks):
            if not isinstance(dblock, CrosstabBlock):
                continue
            loc = S._locate_crosstab(dblock, ctx.grid)
            box = _cells_bbox(loc.region) if loc is not None else None
            if box is None:
                continue
            ov = _overlap(ctx.rect, box)
            if ov * 1.0 >= CROSSTAB_OVERLAP * _area(ctx.rect) and (best is None or ov > best[0]):
                best = (ov, dr["sheets"][dsi], dr["sheets"][dsi]["blocks"][dbi], loc)
    if best is None:
        ctx.fail("selection_no_crosstab", no)
        return _failed(ctx)
    _ov, dsheet_d, dblock_d, dloc = best
    region = _cells_bbox(dloc.region)
    hits = [loc for loc in located if any(_intersects(region, r) for r in loc.rects)]  # type: ignore[arg-type]
    crosses = [loc for loc in located if loc.block.get("layout") == "crosstab"]
    if len(hits) > 1 or (hits and hits[0].block.get("layout") != "crosstab"):
        ctx.fail("selection_overlap", f"这块交叉表与{quoted([h.block.get('id') for h in hits])}相交：请先调整或删掉那些块")
        return _failed(ctx)
    old = hits[0] if hits else None
    if old is None and crosses:
        ctx.fail("selection_overlap",
                 f"这个工作表已经有交叉表「{crosses[0].block.get('id')}」：一个工作表只能有一个交叉表，请框在它上面以替换它")
        return _failed(ctx)
    recipe = ctx.recipe
    block = copy.deepcopy(dblock_d)
    btables = _block_tables(block)
    rels = [copy.deepcopy(r) for r in dr.get("relations") or [] if _rel_tables(r) and _rel_tables(r) <= btables]
    tables = [copy.deepcopy(t) for t in dr.get("tables") or [] if t.get("name") in btables]
    # 被替换的交叉表只由它自己写入的表，连同引用这些表的关系一起换掉
    drop = ({t for t in _block_tables(old.block) if not _written_elsewhere(recipe, t, ctx.si, old.bi)}
            if recipe and old is not None and ctx.si is not None else set())
    if recipe and old is not None:
        # 替换已有的交叉表：先把起草器的名字按来源对齐到现行配方（6.3 的 align_to_contract），版式没变时得到的
        # 就是原来的配方，表名、列名、分段 id、关系编号都不会因为换了一种定位方式而变
        sheet = recipe["sheets"][ctx.si]  # type: ignore[index]
        cand = {"recipe_format": RECIPE_FORMAT, "mode": recipe.get("mode", "replace"),
                "sheets": [{"id": sheet.get("id"), "match": sheet.get("match"),
                            "context": list(dsheet_d.get("context") or []), "blocks": [block]}],
                "tables": tables, "relations": rels}
        aligned, _report = S.align_to_contract(cand, recipe)
        block = aligned["sheets"][0]["blocks"][0]
        tables, rels = aligned["tables"], aligned["relations"]
        btables = _block_tables(block)
    if recipe:
        old_ids = ({old.block.get("id")} | {s.get("id") for s in old.block.get("segments") or []}) if old else set()
        used_ids = all_ids(recipe) - old_ids
        used = _used_tables(ctx, except_=drop)
        rename = {t: unique_name(t, used) for t in sorted(btables)}
        seg_rename = {seg["id"]: unique_id(seg["id"], used_ids) for seg in block["segments"]}
        block["id"] = old.block["id"] if old is not None else unique_id(block["id"], used_ids)
        _rename_block(block, rename, seg_rename)
        for t in tables:
            t["name"] = rename.get(t["name"], t["name"])
        # 关系 id 与留下来的关系重号时，后面的顺延
        left = {str(r.get("id")) for r in recipe.get("relations") or [] if not (_rel_tables(r) & drop)}
        next_n = max([int(i[1:]) for i in left if i[1:].isdigit()] + [0])
        for r in rels:
            _rename_rel(r, rename)
            if r["id"] in left:
                next_n += 1
                while f"R{next_n}" in left:
                    next_n += 1
                r["id"] = f"R{next_n}"
            left.add(r["id"])
    expected_rows = sorted({r for rows in dloc.seg_rows.values() for r in rows})
    lc = dloc.label_col
    expected = {"axis": _a1((dloc.axis_row, dloc.c1, dloc.axis_row, dloc.c2)),
                "labels": _a1((expected_rows[0], lc, expected_rows[-1], lc)) if expected_rows else None,
                "values": _a1((expected_rows[0], dloc.c1, expected_rows[-1], dloc.c2)) if expected_rows else None}
    anchors = [Anchor("axis", _text_of(ctx.grid, dloc.axis_row, dloc.c1) or str(ctx.grid.get(dloc.axis_row, dloc.c1).value),
                      ctx.coord(dloc.axis_row, dloc.c1))]
    for sid, tr in dloc.title_rows.items():
        anchors.append(Anchor("section_title", _text_of(ctx.grid, tr, lc) or sid, ctx.coord(tr, lc)))
    context = list(dsheet_d.get("context") or [])
    if not recipe:
        root = {"recipe_format": RECIPE_FORMAT, "mode": dr.get("mode", "replace"),
                "sheets": [{"id": "s1", "match": dict(dsheet_d.get("match") or {"name": ctx.sheet[:31]}),
                            "context": context, "blocks": [block]}],
                "tables": tables, "relations": rels}
        ops: list[dict[str, Any]] = [{"op": "replace", "path": "", "value": root}]
    elif ctx.si is None:
        ops = [{"op": "add", "path": "/sheets/-", "value": {
            "id": _new_sheet_id(recipe), "match": {"name": ctx.sheet[:31]}, "context": context, "blocks": [block]}}]
        ops += _add_tables(recipe, tables) + [{"op": "add", "path": "/relations/-", "value": r} for r in rels]
    else:
        if old is not None:
            ops = [{"op": "replace", "path": ptr("sheets", ctx.si, "blocks", old.bi), "value": block}]
        else:
            ops = [{"op": "add", "path": ptr("sheets", ctx.si, "blocks", "-"), "value": block}]
        tis = sorted({ti for t in drop if (ti := table_index(recipe, t)) is not None}, reverse=True)
        ris = sorted({i for i, r in enumerate(recipe.get("relations") or []) if _rel_tables(r) & drop}, reverse=True)
        ops += [{"op": "remove", "path": ptr("tables", ti)} for ti in tis]
        ops += [{"op": "remove", "path": ptr("relations", ri)} for ri in ris]
        ops += _add_tables(recipe, tables) + [{"op": "add", "path": "/relations/-", "value": r} for r in rels]
        if context and not recipe["sheets"][ctx.si].get("context"):
            ops.append({"op": "add", "path": ptr("sheets", ctx.si, "context"), "value": context})
    verb = "替换现有的" if old is not None else "新增"
    summary = [f"按框选{verb}交叉表「{block['id']}」：日期表头、行标签、分段标题按文字定位",
               f"写入表{quoted([t['name'] for t in tables])}"]
    if rels:
        summary.append(f"登记系统发现的关系{quoted([r['id'] for r in rels])}")
    return finish(recipe, ops, kind="selection", key=f"crosstab:{block['id']}",
                  title=f"按框选{verb}交叉表「{block['id']}」：按日期表头和行标签定位", summary=summary,
                  anchors=anchors, expected=expected, block=block["id"])


def _rename_block(block: dict, tables: dict[str, str], segs: dict[str, str]) -> None:
    for seg in block.get("segments") or []:
        seg["id"] = segs.get(seg["id"], seg["id"])
        if seg.get("table"):
            seg["table"] = tables.get(seg["table"], seg["table"])
        loc = seg.get("locate") or {}
        if loc.get("segment"):
            loc["segment"] = segs.get(loc["segment"], loc["segment"])
        if seg.get("verify"):
            seg["verify"]["against_table"] = tables.get(seg["verify"]["against_table"], seg["verify"]["against_table"])
        if seg.get("keep_as"):
            seg["keep_as"]["table"] = tables.get(seg["keep_as"]["table"], seg["keep_as"]["table"])


def _rename_rel(rel: dict, tables: dict[str, str]) -> None:
    if rel.get("table"):
        rel["table"] = tables.get(rel["table"], rel["table"])
    for side in ("a", "b"):
        if isinstance(rel.get(side), dict):
            rel[side]["table"] = tables.get(rel[side]["table"], rel[side]["table"])


# ==========================================================================
# 分段、合计行、分段标题
# ==========================================================================


def _seg_rows(x: Any) -> dict[str, list[int]]:
    return {sid: list(rows) for sid, rows in x.seg_rows.items()}


def _select_segment(ctx: _Ctx, located: list[_Located]) -> EditResult:
    loc = _crosstab_at(ctx, located)
    if loc is None:
        ctx.fail("selection_not_in_block", "框要落在交叉表的标签列上（可以带上右边的数据格），并在日期表头之下")
        return _failed(ctx)
    role = ctx.sel.options.get("role")
    if role not in ("measures", "dimension"):
        raise EditRequestError("edit_invalid", "框选为分段时要选是指标（每行一个指标）还是维度（每行一个取值）")
    x = loc.cross
    r1, _c1, r2, _c2 = ctx.rect
    labels = _labels(ctx, x.label_col)
    if labels is None:
        return _failed(ctx)
    taken = {r: sid for sid, rows in _seg_rows(x).items() for r in rows}
    taken.update({r: sid for sid, r in x.title_rows.items()})
    clash = sorted({taken[r] for r, _ in labels if r in taken})
    if clash:
        ctx.fail("selection_overlap", f"框里的行已经属于分段{quoted(clash)}：请只框还没有分段认领的行")
        return _failed(ctx)
    title_row = r1 - 1
    title = _text_of(ctx.grid, title_row, x.label_col)
    if title is None or not _row_empty(ctx.grid, title_row, x.c1, x.c2) or title_row in taken or len(title) > 40:
        title, title_row = None, None
    texts = [t for _, t in labels]
    block = loc.block
    axis = (block.get("axis") or {}).get("name", "日期")
    used_ids = all_ids(ctx.recipe or {})
    used = _used_tables(ctx)
    notes: list[str] = []
    if role == "measures":
        base = to_sql_name(ctx.sheet, fallback=f"表{len(used) + 1}")[:45]
        table = _new_table_name(ctx, f"{base}_按日", used)
        if table is None:
            return _failed(ctx)
        cols_used = {collide_key(axis)}
        measures: dict[str, str] = {}
        units: dict[str, str] = {}
        for i, lab in enumerate(texts):
            col, unit, _raw = S._measure_column(lab, i + 2)
            col = unique_name(col, cols_used)
            measures[lab] = col
            if unit:
                units[col] = unit
        seg = {"id": unique_id(table, used_ids), "role": "measures", "table": table,
               "locate": {"by": "section_title", "title": title, "segment": None} if title
               else {"by": "labels", "title": None, "segment": None},
               "labels": {"expect": texts}, "measures": measures}
        spec = {"name": table, "grain": [axis], "kind": "data", "units": units, "note": ""}
    else:
        parser = "hour_range" if all(hour_range(t) is not None for t in texts) else "text"
        bad = [t for t in texts if parser == "text" and (text_label(t) is None or len(t) > LABEL_MAX)]
        if bad:
            ctx.fail("selection_label_unparsed", f"标签{quoted(bad)}为空或超过 {LABEL_MAX} 字，无法作维度的取值")
            return _failed(ctx)
        merged, blocked = _merge_target(ctx, block, parser, texts, title)
        if merged is not None:
            return _merged_segment(ctx, loc, merged, texts, title, title_row, labels)
        if blocked:
            notes.append(blocked)
        want = split_unit_suffix(title)[0] if title else ""
        n_fallback = len(used) + 1
        plain = (to_sql_name(want, fallback=f"表{n_fallback}") if want else f"表{n_fallback}")[:48]
        table = _new_table_name(ctx, plain, used)
        if table is None:
            return _failed(ctx)
        dim = S._HOUR_DIM if parser == "hour_range" else S._TEXT_DIM
        value = to_sql_name(plain[-2:], fallback="数值") if len(plain) >= 2 else "数值"
        reserved = {collide_key(n) for n in (axis, dim, *(S._DERIVE if parser == "hour_range" else ()))}
        if collide_key(value) in reserved or len(value) < 2:
            value = "数值"
        unit_raw = split_unit_suffix(title)[1] if title else None
        seg = {"id": unique_id(table, used_ids), "role": "dimension", "table": table,
               "locate": {"by": "section_title", "title": title, "segment": None} if title
               else {"by": "labels", "title": None, "segment": None},
               "labels": {"expect": texts},
               "dim": {"name": dim, "parser": parser, "derive": dict(S._DERIVE) if parser == "hour_range" else {}},
               "value": value, "const": {}, "stop_parser": None}
        units = {value: unit_raw} if unit_raw and S.P.unit_known(unit_raw) else {}
        spec = {"name": table, "grain": [axis, dim], "kind": "data", "units": units, "note": ""}
    ops = [{"op": "add", "path": _segment_path(ctx, loc), "value": seg}, {"op": "add", "path": "/tables/-", "value": spec}]
    kind = "指标" if role == "measures" else "维度"
    return _segment_result(ctx, loc, seg, ops, kind=kind, texts=texts, title=title, title_row=title_row,
                           labels=labels, where=f"写入新表「{table}」", notes=notes)


def _segment_path(ctx: _Ctx, loc: _Located) -> str:
    """新分段插在按行号排在它后面的第一个分段之前；合计分段要紧跟它的基础分段，不能插进两者之间。"""
    x = loc.cross
    r1 = ctx.rect[0]
    segs = loc.block.get("segments") or []
    rows_of = _seg_rows(x)
    idx = len(segs)
    for i, s in enumerate(segs):
        rows = rows_of.get(s.get("id")) or ([x.title_rows[s["id"]]] if s.get("id") in x.title_rows else [])
        if rows and min(rows) > r1:
            idx = i
            break
    while idx < len(segs) and segs[idx].get("role") == "derived":
        idx += 1
    return ptr("sheets", ctx.si, "blocks", loc.bi, "segments", idx)


def _segment_result(ctx: _Ctx, loc: _Located, seg: dict, ops: list[dict[str, Any]], *, kind: str, texts: list[str],
                    title: str | None, title_row: int | None, labels: list[tuple[int, str]], where: str,
                    notes: list[str], extra: list[str] | None = None) -> EditResult:
    x = loc.cross
    r1, _c1, r2, _c2 = ctx.rect
    anchors = [Anchor("row_label", t, ctx.coord(r, x.label_col)) for r, t in labels]
    if title:
        anchors.insert(0, Anchor("section_title", title, ctx.coord(title_row, x.label_col)))  # type: ignore[arg-type]
    expected = {"labels": _a1((r1, x.label_col, r2, x.label_col)), "values": _a1((r1, x.c1, r2, x.c2)),
                "title": _a1((title_row, x.label_col, title_row, x.label_col)) if title else None}
    how = f"分段标题「{title}」" if title else f"标签{quoted(texts[:5])}" + ("等" if len(texts) > 5 else "")
    return finish(ctx.recipe, ops, kind="selection", key=f"segment:{seg['id']}",
                  title=f"按框选新增{kind}分段「{seg['id']}」：按{'分段标题' if title else '标签'}定位",
                  summary=[f"交叉表「{loc.block['id']}」新增{kind}分段「{seg['id']}」，按{how}定位，{where}", *(extra or [])],
                  anchors=anchors, notes=notes, expected=expected, block=loc.block.get("id"))


@dataclass
class _Merge:
    table: str
    template: dict          # 那张表里第一个分段（dim、value 照抄它）
    col: str                # 常量列名
    pick: str               # 新分段的常量取值（候选词的第一个，同起草器第 8b 条）


def _dim_unit(title: str | None) -> str | None:
    return split_unit_suffix(title)[1] if title else None


def _merge_target(ctx: _Ctx, block: dict, parser: str, texts: list[str],
                  title: str | None) -> tuple[_Merge | None, str]:
    """起草器第 5 条（P2-SPEC 6.1，recipe_suggest._group_dimensions）：dim.parser 相同、标签规范写法不相交、单位
    相同的维度分段写进同一张表，各段用常量列区分。框选新的维度分段时照这条找可以并入的表（同一块里、按分段
    顺序的第一张），常量取值照起草器第 8b 条取 candidate_words(新标题, 兄弟标题) 的第一个，并且要与已有的取值
    不同（起草器「选不出互不相同的常量就各成一张表」）。返回 (可以并入的表, 说明)：

    - 能并入：(_Merge, "")；
    - 有 parser、单位、标签都对得上的表，却并不进去（客户端指定了新表名；那张表的分段没有常量列，并入要给现有
      分段加列；分段标题里选不出与已有取值不同的词）：(None, 说明)，说明写进 notes——新建了表、没有并入，
      要让人看见；
    - 没有对得上的表：(None, "")。
    只在同一块里找，并且那张表的写入方必须全在这一块里：跨块、跨工作表合表不是起草器的规则。"""
    keys = {S._dim_key(parser, t) for t in texts}
    unit = _dim_unit(title)
    segs = [s for s in block.get("segments") or [] if s.get("role") == "dimension"]
    order: list[str] = []
    for s in segs:
        if s.get("table") not in order:
            order.append(s.get("table"))
    for table in order:
        group = [s for s in segs if s.get("table") == table]
        if any((s.get("dim") or {}).get("parser") != parser or _dim_unit((s.get("locate") or {}).get("title")) != unit
               for s in group):
            continue
        if any(keys & {S._dim_key(parser, e) for e in (s.get("labels") or {}).get("expect") or []} for s in group):
            continue
        if _written_by_others(ctx.recipe or {}, table, block):
            continue
        given = ctx.sel.options.get("table")
        if given not in (None, ""):
            return None, f"按指定的表名新建了表，没有并入同类的表「{table}」"
        cols = {tuple((s.get("const") or {}).keys()) for s in group}
        if len(cols) != 1 or len(next(iter(cols))) != 1:
            return None, (f"没有并入同类的表「{table}」：它的分段没有用来区分分段的常量列，并入需要给现有分段加列。"
                          "需要并入时请在配方面板中修改")
        col = next(iter(cols))[0]
        picks = {canon(((s.get("const") or {}).get(col) or {}).get("pick") or "") for s in group}
        sibs = [t for s in group if (t := (s.get("locate") or {}).get("title"))]
        words = candidate_words(title, sibs) if title else []
        if not words or canon(words[0]) in picks:
            return None, (f"没有并入同类的表「{table}」：分段标题里没有能与已有分段区分的词作「{col}」的取值。"
                          "需要并入时请在配方面板中修改")
        return _Merge(table, group[0], col, words[0]), ""
    return None, ""


def _written_by_others(recipe: dict, table: str, block: dict) -> bool:
    """这张表还有这一块以外的写入方（别的块、列表、合计另存）。"""
    for sheet in recipe.get("sheets") or []:
        for b in sheet.get("blocks") or []:
            if b is block:
                continue
            if b.get("layout") == "list":
                tr = (b.get("rows") or {}).get("total_row") or {}
                if table in (b.get("table"), tr.get("keep_as")):
                    return True
            for seg in b.get("segments") or []:
                if table in (seg.get("table"), (seg.get("keep_as") or {}).get("table")):
                    return True
    return False


def _merged_segment(ctx: _Ctx, loc: _Located, m: _Merge, texts: list[str], title: str | None,
                    title_row: int | None, labels: list[tuple[int, str]]) -> EditResult:
    """并入已有的表：dim、值列照抄那张表的分段，常量列取 m.pick，不新增表定义。"""
    t = m.template
    seg = {"id": unique_id(m.pick, all_ids(ctx.recipe or {})), "role": "dimension", "table": m.table,
           "locate": {"by": "section_title", "title": title, "segment": None},
           "labels": {"expect": texts}, "dim": copy.deepcopy(t["dim"]), "value": t["value"],
           "const": {m.col: {"pick": m.pick}}, "stop_parser": None}
    ops = [{"op": "add", "path": _segment_path(ctx, loc), "value": seg}]
    return _segment_result(ctx, loc, seg, ops, kind="维度", texts=texts, title=title, title_row=title_row,
                           labels=labels, where=f"并入表「{m.table}」", notes=[],
                           extra=[f"「{m.col}」取「{m.pick}」（取自分段标题，与同表其他分段区分）"])


def _select_derived(ctx: _Ctx, located: list[_Located]) -> EditResult:
    loc = _crosstab_at(ctx, located)
    if loc is None:
        ctx.fail("selection_not_in_block", "框要落在交叉表的标签列上（可以带上右边的数据格），并在日期表头之下")
        return _failed(ctx)
    x = loc.cross
    r1, _c1, r2, _c2 = ctx.rect
    labels = _labels(ctx, x.label_col)
    if labels is None:
        return _failed(ctx)
    bad = [t for _, t in labels if hour_range_total(t) is None]
    if bad:
        ctx.fail("selection_label_unparsed", f"标签{quoted(bad)}无法解析为带区间的合计（如「18-22时合计」）")
        return _failed(ctx)
    keep = ctx.sel.options.get("keep", True)
    if not isinstance(keep, bool):
        raise EditRequestError("edit_invalid", "合计行是否另存只能选是或否")
    box_rows = set(range(r1, r2 + 1))
    rows_of = _seg_rows(x)
    others = {r for rows in rows_of.values() for r in rows}
    segs = loc.block.get("segments") or []
    gi = None
    for i, s in enumerate(segs):
        rows = set(rows_of.get(s.get("id")) or [])
        # 框里的行必须紧跟这个分段的最后一行。起草器定位时会把紧跟的、有数据的合计行也收进分段（它还不知道该停），
        # 所以「收进来的」也算紧跟
        if s.get("role") == "dimension" and (r1 - 1) in rows and all(r in rows or r not in others for r in box_rows):
            gi = i
            break
    if gi is None:
        ctx.fail("selection_not_after_segment", "框里的行要紧跟在一个按时段展开的分段的最后一行之后")
        return _failed(ctx)
    why = total_blockers(ctx.recipe, ctx.si, loc.bi, gi)  # type: ignore[arg-type]
    if why:
        ctx.fail("selection_not_after_segment", why)
        return _failed(ctx)
    texts = [t for _, t in labels]
    ops, summary, _kept = total_ops(ctx.recipe, ctx.si, loc.bi, gi, texts, keep)  # type: ignore[arg-type]
    new_id = ops[1]["value"]["id"]
    expected = {"labels": _a1((r1, x.label_col, r2, x.label_col)), "values": _a1((r1, x.c1, r2, x.c2))}
    return finish(ctx.recipe, ops, kind="selection", key=f"derived:{new_id}",
                  title=f"按框选把{quoted(texts)}改作合计核对", summary=summary,
                  anchors=[Anchor("total_word", t, ctx.coord(r, x.label_col)) for r, t in labels],
                  expected=expected, block=loc.block.get("id"))


def _select_section_title(ctx: _Ctx, located: list[_Located]) -> EditResult:
    r1, c1, r2, c2 = ctx.rect
    loc = _crosstab_at(ctx, located)
    if loc is None or (r1, c1) != (r2, c2):
        ctx.fail("selection_not_in_block", "分段标题要框交叉表标签列上的一格")
        return _failed(ctx)
    seg_id = ctx.sel.options.get("segment")
    segs = loc.block.get("segments") or []
    gi = next((i for i, s in enumerate(segs) if s.get("id") == seg_id), None)
    if gi is None or segs[gi].get("role") == "derived":
        raise EditRequestError("edit_invalid", "要指明是这个交叉表里哪个分段（指标或维度分段）的标题")
    seg = segs[gi]
    text = _text_of(ctx.grid, r1, c1)
    if text is None or len(text) > 40:
        ctx.fail("selection_label_blank", "这一格没有文字（或超过 40 字），不能作分段标题")
        return _failed(ctx)
    sp = ("sheets", ctx.si, "blocks", loc.bi, "segments", gi)
    ops: list[dict[str, Any]] = [{"op": "replace", "path": ptr(*sp, "locate"),
                                  "value": {"by": "section_title", "title": text, "segment": None}}]
    summary = [f"分段「{seg_id}」改按分段标题「{text}」定位"]
    consts = seg.get("const") or {}
    if consts and not keep_ok(seg, text):
        # 常量沿用不了：常量列整列的值要换成新标题里的哪个词，只能由人选（4.1「不猜」）。框选不替人挑第一个
        # 候选词——那是破坏性变更，按期累积时还要重新开始累积。④ 的修复按钮把候选词逐个列成选项，配方面板也能选
        held = "、".join(f"「{k}」现在取「{v.get('pick')}」" for k, v in consts.items())
        ctx.fail("selection_label_unparsed",
                 f"分段「{seg_id}」的常量列{held}，新标题「{text}」里没有这个词，无法沿用；改用哪个词需要人工选择。"
                 "请在配方面板中修改取值；该分段报了找不到分段标题时，也可以用问题旁的修复按钮选词",
                 [ctx.coord(r1, c1)])
        return _failed(ctx)
    if consts:
        summary.append("、".join(f"「{k}」沿用「{v.get('pick')}」" for k, v in consts.items()))
    return finish(ctx.recipe, ops, kind="selection", key=f"section_title:{seg_id}",
                  title=f"按框选把「{text}」作为分段「{seg_id}」的分段标题", summary=summary,
                  anchors=[Anchor("section_title", text, ctx.coord(r1, c1))],
                  expected={"title": _a1(ctx.rect)}, block=loc.block.get("id"))


# ==========================================================================
# 忽略
# ==========================================================================


def _digits_fail(ctx: _Ctx, texts: list[str], cells: list[str]) -> None:
    ctx.fail("selection_anchor_has_digits",
             f"{quoted(texts)}含数字，下一期很可能对不上，无法按文字忽略：请修改文件，或把它框选为新的分段", cells)


def _select_ignore_rows(ctx: _Ctx, located: list[_Located]) -> EditResult:
    reason = _reason(ctx.sel)
    loc = _crosstab_at(ctx, located)
    if loc is None:
        ctx.fail("selection_not_in_block", "要忽略的行要框在交叉表的标签列上（可以带上右边的数据格），并在日期表头之下")
        return _failed(ctx)
    x = loc.cross
    labels = _labels(ctx, x.label_col)
    if labels is None:
        return _failed(ctx)
    digits = [(r, t) for r, t in labels if has_digit(t) or len(t) > _ANCHOR_MAX]
    if digits:
        _digits_fail(ctx, [t for _, t in digits], [ctx.coord(r, x.label_col) for r, _ in digits])
        return _failed(ctx)
    have = {match_key(i.get("label")) for i in loc.block.get("ignore_rows") or []}
    texts: list[str] = []
    for _r, t in labels:
        if match_key(t) not in have:
            have.add(match_key(t))
            texts.append(t)
    bp = ("sheets", ctx.si, "blocks", loc.bi)
    ops = [{"op": "add", "path": ptr(*bp, "ignore_rows", "-"), "value": {"label": t, "reason": reason}} for t in texts]
    r1, _c1, r2, _c2 = ctx.rect
    return finish(ctx.recipe, ops, kind="selection", key=f"ignore_rows:{loc.block['id']}",
                  title=f"按框选按行标签忽略{quoted([t for _, t in labels])}",
                  summary=[f"交叉表「{loc.block['id']}」按行标签忽略{quoted(texts)}（理由：{reason}）"] if texts
                  else ["这些行已经按行标签忽略，配方不变"],
                  anchors=[Anchor("row_label", t, ctx.coord(r, x.label_col)) for r, t in labels],
                  expected={"ignored": _a1((r1, x.label_col, r2, x.label_col))}, block=loc.block.get("id"))


def _select_ignore_columns(ctx: _Ctx, located: list[_Located]) -> EditResult:
    reason = _reason(ctx.sel)
    r1, c1, r2, c2 = ctx.rect
    target: tuple[_Located, int, dict[int, str]] | None = None
    for loc in located:
        if loc.cross is not None and c1 > loc.cross.c2 and loc.cross.axis_row <= r2:
            x = loc.cross
            texts = {c: t for c in range(c1, c2 + 1) if (t := _text_of(ctx.grid, x.axis_row, c))}
            target = (loc, x.axis_row, texts)
            break
        if loc.lst is not None and loc.lst.cols and loc.lst.cols[0] <= c1 and c2 <= loc.lst.cols[-1]:
            hr = loc.lst.header_rows
            texts = S._header_texts(ctx.grid, hr, list(range(c1, c2 + 1)))
            target = (loc, hr[-1], texts)
            break
    if target is None:
        ctx.fail("selection_not_in_block", "要忽略的列要框在交叉表最后一个日期右侧，或列表的表头范围之内")
        return _failed(ctx)
    loc, hrow, texts = target
    cols = list(range(c1, c2 + 1))
    blank = [c for c in cols if c not in texts and ctx.grid.get(hrow, c) is None]
    if blank:
        ctx.fail("selection_header_blank", f"{'、'.join(col_letter(c) for c in blank)} 列没有表头，无法按表头忽略",
                 [ctx.coord(hrow, c) for c in blank])
        return _failed(ctx)
    bad = [c for c in cols if c not in texts or not _textual(ctx.grid, hrow, c)
           or month_day_or_date(texts[c]) is not None]
    if bad:
        ctx.fail("selection_header_not_text", f"{'、'.join(col_letter(c) for c in bad)} 列的表头是数字或日期，无法按表头忽略",
                 [ctx.coord(hrow, c) for c in bad])
        return _failed(ctx)
    digits = [c for c in cols if has_digit(texts[c]) or len(texts[c]) > _HEADER_MAX]
    if digits:
        _digits_fail(ctx, [texts[c] for c in digits], [ctx.coord(hrow, c) for c in digits])
        return _failed(ctx)
    have = {match_key(i.get("header")) for i in loc.block.get("ignore_columns") or []}
    heads: list[str] = []
    for c in cols:
        if match_key(texts[c]) not in have:
            have.add(match_key(texts[c]))
            heads.append(texts[c])
    bp = ("sheets", ctx.si, "blocks", loc.bi)
    ops = [{"op": "add", "path": ptr(*bp, "ignore_columns", "-"), "value": {"header": h, "reason": reason}}
           for h in heads]
    return finish(ctx.recipe, ops, kind="selection", key=f"ignore_columns:{loc.block['id']}",
                  title=f"按框选按表头忽略{quoted([texts[c] for c in cols])}",
                  summary=[f"块「{loc.block['id']}」按表头忽略{quoted(heads)}（理由：{reason}）"] if heads
                  else ["这些列已经按表头忽略，配方不变"],
                  anchors=[Anchor("header", texts[c], ctx.coord(hrow, c)) for c in cols],
                  expected={"ignored": _a1((hrow, c1, hrow, c2))}, block=loc.block.get("id"))


def _select_ignore_outside(ctx: _Ctx, located: list[_Located]) -> EditResult:
    reason = _reason(ctx.sel)
    if ctx.si is None:
        ctx.fail("selection_not_in_block", "这个工作表还没有导入区域：请先框选要导入的表")
        return _failed(ctx)
    r1, c1, r2, c2 = ctx.rect
    rects = [r for loc in located for r in loc.rects]
    inside = lambda r, c: any(a[0] <= r <= a[2] and a[1] <= c <= a[3] for a in rects)  # noqa: E731
    found: list[tuple[int, int, str]] = []
    bare: list[int] = []
    for r in range(r1, r2 + 1):
        hit = next(((c, t) for c in range(c1, c2 + 1) if not inside(r, c) and (t := _text_of(ctx.grid, r, c))
                    and not looks_numeric_text(t)), None)
        if hit is None:
            bare.append(r)
        else:
            found.append((r, hit[0], hit[1]))
    if bare:
        ctx.fail("selection_no_anchor", f"{'、'.join(f'第 {r} 行' for r in bare[:8])}在导入区域之外没有文字格，"
                                        "没有可以作锚点的文字：只能修改文件",
                 [ctx.coord(r, c1) for r in bare])
        return _failed(ctx)
    digits = [(r, c, t) for r, c, t in found if has_digit(t) or len(t) > _ANCHOR_MAX]
    if digits:
        _digits_fail(ctx, [t for *_x, t in digits], [ctx.coord(r, c) for r, c, _t in digits])
        return _failed(ctx)
    have = {match_key(i.get("anchor")) for i in ctx.recipe["sheets"][ctx.si].get("ignore_outside") or []}  # type: ignore[index]
    texts: list[str] = []
    for _r, _c, t in found:
        if match_key(t) not in have:
            have.add(match_key(t))
            texts.append(t)
    ops = [{"op": "add", "path": ptr("sheets", ctx.si, "ignore_outside", "-"), "value": {"anchor": t, "reason": reason}}
           for t in texts]
    sid = ctx.recipe["sheets"][ctx.si]["id"]  # type: ignore[index]
    return finish(ctx.recipe, ops, kind="selection", key=f"ignore_outside:{sid}",
                  title=f"按框选忽略同一行有{quoted([t for *_x, t in found])}的导入区域之外的数字",
                  summary=[f"工作表「{ctx.sheet}」：同一行有{quoted(texts)}时，忽略这一行导入区域之外的数字（理由：{reason}）"]
                  if texts else ["这些行已经按文字忽略，配方不变"],
                  anchors=[Anchor("outside_text", t, ctx.coord(r, c)) for r, c, t in found],
                  expected={"ignored": _a1(ctx.rect)}, block=None)


# ==========================================================================
# 入口
# ==========================================================================


def selection_edit(recipe: dict[str, Any] | None, grid: Grid, sel: Selection, *,
                   extraction: Extraction | None, draft_fn: Callable[[], Draft] | None = None,
                   retired_names: dict[str, list[str]] | None = None) -> EditResult:
    """一次框选 → EditResult（补丁、锚点、期望区域）。

    - recipe：工作配方的**完整形式**（同 fix_ops），配方为空（规则起草失败）时是 None：list、crosstab 用根替换新建配方，
      这样框选也是起草失败时不用 AI 的兜底；
    - grid：该工作表的 read_grid(max_rows=draft_max_rows)，和起草用的同一份；
    - extraction：当前工作配方最近一次干跑的结果（判断选区和已有的块是否重叠；带块 id 时按它取各块的区域）；
    - draft_fn：只在 as=crosstab 时用，接口层注入一个对当前文件跑规则起草的闭包；
    - retired_names：当前版本里退役的表名 → 该表退役的列名（取自快照清单的 columns；整张表退役时键在、值是它的列），
      新表名的撞名检查要算上这些键，新列名与退役列只差大小写时沿用退役列的写法（SQLite 列名不分大小写，并存会撞）。
    选项写错（header_rows 不在 1–3、role 不认识……）抛 EditRequestError（edit_invalid）；忽略类没写理由抛
    EditRequestError（reason_required）。框和网格、配方的关系泛化不了时返回 ok=False，problems 写原因（4.3）。"""
    rect = _rect(sel.ref)
    parsed: Recipe | None = None
    if recipe:
        try:
            parsed = Recipe.model_validate(recipe)
        except ValidationError:
            parsed = None
    ctx = _Ctx(recipe=recipe or None, parsed=parsed, grid=grid, sel=sel, rect=rect, ext=extraction,
               retired={str(k): list(v or []) for k, v in (retired_names or {}).items()})
    rows = grid.row_numbers()
    if not rows or (grid.truncated and rect[2] > rows[-1]):
        ctx.fail("selection_out_of_grid", "框超出了已读入的范围（每张工作表只读入前 500 行）：请框在已显示的范围之内，"
                                          "大表的列表可以改选「下边界按规则推断」")
        return _failed(ctx)
    if recipe and parsed is None and sel.as_ not in ("list", "crosstab"):
        ctx.fail("selection_not_in_block", "当前的配方还有格式问题，无法在它上面框选：请先在配方面板中修改")
        return _failed(ctx)
    ctx.si = _sheet_of(ctx.recipe, grid, extraction)
    located = _locate(ctx)
    if sel.as_ == "list":
        return _select_list(ctx, located)
    if sel.as_ == "crosstab":
        return _select_crosstab(ctx, located, draft_fn)
    if ctx.si is None and sel.as_ != "ignore_outside":
        ctx.fail("selection_not_in_block", "这个工作表还没有交叉表或列表：请先框选要导入的表")
        return _failed(ctx)
    handler = {"segment": _select_segment, "derived": _select_derived, "section_title": _select_section_title,
               "ignore_rows": _select_ignore_rows, "ignore_columns": _select_ignore_columns,
               "ignore_outside": _select_ignore_outside}.get(sel.as_)
    if handler is None:
        raise EditRequestError("edit_invalid", "框选没有指明选中的是什么，或取值不在可选范围内")
    return handler(ctx, located)


# ==========================================================================
# 重放比对（4.5）
# ==========================================================================

#: 期望区域的键 → 干跑的格子角色（RegionMark.role）
_ROLES = {
    "header": ("col_header",), "data": ("value",), "total": ("total_label", "total_value"),
    "axis": ("col_header",), "labels": ("row_label", "derived_label", "section_title"),
    "values": ("value", "derived_value"), "title": ("section_title",), "ignored": ("ignored", "ignored_column"),
}
_NAMES = {"header": "表头", "data": "数据区", "total": "合计行", "axis": "日期表头", "labels": "行标签",
          "values": "数值区", "title": "分段标题", "ignored": "忽略的格"}


def _crop(rect: Rect | None, window: int | None) -> Rect | None:
    if rect is None or window is None:
        return rect
    if rect[0] > window:
        return None
    return rect[0], rect[1], min(rect[2], window), rect[3]


def _covered(rect: Rect, marks: list[Rect]) -> bool:
    """rect 里的每一格都落在某个 mark 里（框是人框的，几十格以内，逐格判）。"""
    return all(any(m[0] <= r <= m[2] and m[1] <= c <= m[3] for m in marks)
               for r in range(rect[0], rect[2] + 1) for c in range(rect[1], rect[3] + 1))


def replay_compare(edit: EditResult, after: Extraction, *, window_rows: int | None = None) -> ReplayCompare:
    """框换算出的期望区域与应用后干跑认出的区域比对（4.5）。match=False 不阻止应用：界面把差异摆出来，按钮文字变成
    「仍然应用（以重放结果为准）」。

    actual 从 after.regions 里按 RegionMark.block 过滤出这个块的格子、按角色合成外接矩形。列表、交叉表按矩形相等判
    （表头矩形相等、数据矩形的行列范围相等、期望有合计行时合计行就是框下一行）；分段、合计行、分段标题、忽略这几种
    框只是一部分，按「框里的格都被认成了期望的角色」判。干跑结果里没有块 id（期 2 的执行器）时退回按列范围取。
    window_rows：大表的交互干跑只跑前若干行时，期望和实际都先裁到这些行再比（评审三-M3）。"""
    exp = {k: v for k, v in (edit.expected or {}).items()}
    tagged = any(m.block for m in after.regions)
    exp_rects = {k: _rect(v) for k, v in exp.items() if v}
    cols = _bbox(list(exp_rects.values()))

    def marks_for(key: str) -> list[Rect]:
        roles = _ROLES.get(key, ())
        out = []
        for m in after.regions:
            if m.role not in roles:
                continue
            if tagged and edit.block is not None and m.block != edit.block:
                continue
            r = _rect(m.ref)
            if (not tagged or edit.block is None) and cols is not None and not (r[1] <= cols[3] and cols[1] <= r[3]):
                continue
            out.append(r)
        return [x for x in (_crop(r, window_rows) for r in out) if x is not None]

    whole = edit.key.split(":", 1)[0] in ("list", "crosstab")
    actual: dict[str, str | None] = {}
    diffs: list[str] = []
    expected: dict[str, str | None] = {}
    match = True
    for key in exp:
        e = _crop(exp_rects.get(key), window_rows)
        expected[key] = _a1(e)
        ms = marks_for(key)
        if whole:
            a = _bbox(ms)
        else:
            a = _bbox([m for m in ms if e is not None and _intersects(m, e)])
        actual[key] = _a1(a)
        name = _NAMES.get(key, key)
        if e is None and a is None:
            continue
        if e is None:
            if whole:
                match = False
                diffs.append(f"重放时多认出了{name}（{_a1(a)}），框里没有")
            continue
        if a is None:
            match = False
            diffs.append(f"重放时没有认出{name}（框里是 {_a1(e)}）")
            continue
        if whole:
            if a != e:
                match = False
                diffs.append(_diff_text(name, e, a))
        elif not _covered(e, ms):
            match = False
            diffs.append(f"框里的{name}（{_a1(e)}）在重放时没有全部认出，认出的是 {_a1(a)}")
    return ReplayCompare(expected=expected, actual=actual, match=match, diffs=diffs, window_rows=window_rows)


def _diff_text(name: str, e: Rect, a: Rect) -> str:
    """人话：「重放的数据区到第 10 行，比框多 2 行（第 9–10 行）」。"""
    parts = []
    if a[2] > e[2]:
        parts.append(f"重放的{name}到第 {a[2]} 行，比框多 {a[2] - e[2]} 行（{_rows_text(e[2] + 1, a[2])}）")
    elif a[2] < e[2]:
        parts.append(f"重放的{name}到第 {a[2]} 行，比框少 {e[2] - a[2]} 行（{_rows_text(a[2] + 1, e[2])}）")
    if a[0] != e[0]:
        parts.append(f"重放的{name}从第 {a[0]} 行开始，框是第 {e[0]} 行")
    if (a[1], a[3]) != (e[1], e[3]):
        parts.append(f"重放的{name}在 {col_letter(a[1])}–{col_letter(a[3])} 列，框是 {col_letter(e[1])}–{col_letter(e[3])} 列")
    return "；".join(parts) or f"重放的{name}是 {_a1(a)}，框是 {_a1(e)}"
