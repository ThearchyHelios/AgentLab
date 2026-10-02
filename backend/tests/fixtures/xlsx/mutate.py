"""9.4 的数据变异：在 XML 层改 D00 的字节（flow_workbook(2026-08-01, 31)），保留缓存值。

做法同 xlsx-lab 评审时的 mutate.py，但输入是合成夹具。openpyxl 写不出来的形态（去掉公式留缓存、陈旧的
<dimension>、右侧多出的格子不进 <dimension>）只能这么造。每种变异只改预定的格子，其余字节级不动
（test_fixtures_xlsx 逐格比对）。
"""
from __future__ import annotations

import re

from . import patch_parts, sheet_part
from .flow import SHEET

#: 代号 → (改法, 期望的结果, 期望出现的问题 code 或核对 id)。照 P2-SPEC 9.4 抄录，WP-7 验收时逐项比
MUTATIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "P1": ("第 28–30 行去掉公式、保留缓存值", "passed", ()),
    "P1b": ("第 28–30 行保留公式、去掉缓存值", "needs_decision", ("K1",)),
    "P2": ("D15 改成空", "rejected", ("value_blank",)),
    "P6": ("R4 改成与 Q4 相同的日期", "rejected", ("axis_duplicate",)),
    "P9": ("C28 改成 =SUM(C22:C24)，缓存值自洽", "rejected", ("K1", "G1")),
    "P20": ("第 32 行加「备用分区（人次）」和两个数，<dimension> 不更新", "rejected", ("row_unclaimed",)),
    "P23": ("<dimension> 截到第 27 行", "passed", ()),
    "P24": ("<dimension> 截到倒数第二个日期列", "passed", ()),
    "P25": ("右侧多一列：表头「合计」、各数据行 999，<dimension> 不更新", "rejected", ("axis_extra_cells",)),
    "P26": ("删掉最后一个日期列的所有格，<dimension> 跟着改，B2 仍写到月末", "rejected", ("axis_coverage",)),
    # 9.4「外加」：同一分段里一行写 8-9、另一行写 8－9（全角横线）
    "PKDUP": ("B12（9-10）改成「8－9」，与 B11 的「8-9」规范写法相同", "rejected", ("pk_duplicate", "label_duplicate")),
}

#: D00 的数据行（指标、日间、夜间、合计）
DATA_ROWS = (5, 6, 7, *range(10, 21), *range(22, 31))
_TOTAL_ROWS = "(?:28|29|30)"


def _cell_span(xml: str, ref: str) -> re.Match[str]:
    m = re.search(rf'<c r="{ref}"(?:[^>]*/>|[^>]*>.*?</c>)', xml)
    if m is None:
        raise AssertionError(f"没有格子 {ref}")
    return m


def _replace_cell(xml: str, ref: str, new: str) -> str:
    m = _cell_span(xml, ref)
    return xml[:m.start()] + new + xml[m.end():]


def _number(xml: str, ref: str) -> float:
    m = re.search(r"<v>([^<]*)</v>", _cell_span(xml, ref).group(0))
    if m is None:
        raise AssertionError(f"格子 {ref} 没有值")
    return float(m.group(1))


def _text(xml: str, ref: str) -> str:
    m = re.search(r"<t[^>]*>([^<]*)</t>", _cell_span(xml, ref).group(0))
    if m is None:
        raise AssertionError(f"格子 {ref} 不是文字")
    return m.group(1)


def _inline(ref: str, text: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _num(ref: str, v: float | int) -> str:
    return f'<c r="{ref}" t="n"><v>{v}</v></c>'


def _append_to_row(xml: str, row: int, cell: str) -> str:
    xml, n = re.subn(rf'(<row r="{row}"[^>]*>.*?)(</row>)', lambda m: m.group(1) + cell + m.group(2), xml,
                     count=1, flags=re.S)
    if n != 1:
        raise AssertionError(f"没有第 {row} 行")
    return xml


def _dimension(xml: str) -> tuple[str, int, str, int]:
    m = re.search(r'<dimension ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"/>', xml)
    if m is None:
        raise AssertionError("没有 <dimension>")
    return m.group(1), int(m.group(2)), m.group(3), int(m.group(4))


def _set_dimension(xml: str, ref: str) -> str:
    return re.sub(r'<dimension ref="[^"]*"/>', f'<dimension ref="{ref}"/>', xml, count=1)


def _col_shift(letters: str, delta: int) -> str:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    n += delta
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _mutate_sheet(xml: str, case: str) -> str:
    c1, r1, last, r2 = _dimension(xml)
    if (c1, r1, r2) != ("B", 2, 30):
        raise ValueError(f"mutate 只接受 D00 的版式（<dimension> 是 B2:…30），这份是 {c1}{r1}:{last}{r2}")
    if case == "P1":
        return re.sub(rf'(<c r="[A-Z]+{_TOTAL_ROWS}"[^>]*>)<f>[^<]*</f>', r"\1", xml)
    if case == "P1b":
        return re.sub(rf'(<c r="[A-Z]+{_TOTAL_ROWS}"[^>]*><f>[^<]*</f>)(?:<v>[^<]*</v>|<v\s*/>)', r"\1", xml)
    if case == "P2":
        return _replace_cell(xml, "D15", "")
    if case == "P6":
        return _replace_cell(xml, "R4", _inline("R4", _text(xml, "Q4")))
    if case == "P9":
        v = sum(_number(xml, f"C{r}") for r in (22, 23, 24))
        return _replace_cell(xml, "C28", f'<c r="C28"><f>SUM(C22:C24)</f><v>{int(v)}</v></c>')
    if case == "P20":
        row = ('<row r="32">' + _inline("B32", "备用分区（人次）") + _num("C32", 12) + _num("D32", 15) + "</row>")
        return xml.replace("</sheetData>", row + "</sheetData>", 1)
    if case == "P23":
        return _set_dimension(xml, f"B2:{last}27")
    if case == "P24":
        return _set_dimension(xml, f"B2:{_col_shift(last, -1)}30")
    if case == "P25":
        extra = _col_shift(last, 1)
        xml = _append_to_row(xml, 4, _inline(f"{extra}4", "合计"))
        for r in DATA_ROWS:
            xml = _append_to_row(xml, r, _num(f"{extra}{r}", 999))
        return xml
    if case == "P26":
        xml = re.sub(rf'<c r="{last}\d+"(?:[^>]*/>|[^>]*>.*?</c>)', "", xml)
        return _set_dimension(xml, f"B2:{_col_shift(last, -1)}30")
    if case == "PKDUP":
        return _replace_cell(xml, "B12", _inline("B12", "8－9"))
    raise ValueError(f"不认识的变异：{case}")


def mutate(xlsx: bytes, case: str) -> bytes:
    """在 XML 层改 D00 的字节。case 见 MUTATIONS。"""
    if case not in MUTATIONS:
        raise ValueError(f"不认识的变异：{case}")
    part = sheet_part(xlsx, SHEET)
    return patch_parts(xlsx, {part: lambda t: _mutate_sheet(t, case)})
