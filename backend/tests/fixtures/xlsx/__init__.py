"""合成 xlsx 夹具（期 2，WP-1）：全部在测试里用 openpyxl 现造，标签一律假名，数字随机或手写假数。

- flow.py：「结构仿照客流表」的交叉表（P2-SPEC 9.1），带 variant 参数造 9.2 的漂移用例；
- drift.py：9.2 的 31 例（30 例 + D28b）及各例的期望；
- lab.py：9.3 的实验室难缠用例（结构照 xlsx-lab/case*.py 重写，名字全换成假名）；
- mutate.py：9.4 的数据变异（在 XML 层改 D00 的字节，保留缓存值）。

**用户的真实 Excel 文件不得以任何形式进入这里。** openpyxl 写公式不带缓存值、默认写 fullCalcOnLoad，
「在 Excel 里打开并保存过」的文件由这里的小工具在压缩包层面补上。这些函数不写磁盘，只返回字节。
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Callable
from xml.etree import ElementTree as ET

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


#: 压缩包条目和文档属性里的时间一律写成这个：同样的参数造出同样的字节（原件哈希、构建 id 才可复现）
_FIXED_TIME = (2026, 1, 1, 0, 0, 0)
_STAMP = re.compile(r"(<dcterms:(created|modified)\b[^>]*>)[^<]*(</dcterms:\2>)")


def save(book) -> bytes:
    """openpyxl 工作簿 → 字节（不落盘）。openpyxl 存盘时把「修改时间」写成当前时刻，这里改成固定值。"""
    buf = io.BytesIO()
    book.save(buf)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "docProps/core.xml":
                data = _STAMP.sub(r"\g<1>2026-01-01T00:00:00Z\3", data.decode("utf-8")).encode("utf-8")
            entry = zipfile.ZipInfo(info.filename, date_time=_FIXED_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = info.external_attr
            dst.writestr(entry, data)
    return out.getvalue()


def patch_parts(raw: bytes, edits: dict[str, Callable[[str], str]]) -> bytes:
    """改压缩包里的部件：{部件路径: 文本 → 文本}。其余部件原样拷贝（含压缩参数）。"""
    src = zipfile.ZipFile(io.BytesIO(raw))
    missing = set(edits) - set(src.namelist())
    if missing:
        raise KeyError(f"压缩包里没有这些部件：{sorted(missing)}")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename in edits:
                data = edits[info.filename](data.decode("utf-8")).encode("utf-8")
            dst.writestr(info, data)
    return out.getvalue()


def read_part(raw: bytes, name: str) -> str:
    return zipfile.ZipFile(io.BytesIO(raw)).read(name).decode("utf-8")


def sheet_part(raw: bytes, sheet: str) -> str:
    """工作表名 → 它在压缩包里的 XML 路径（按 workbook.xml 和关系文件配对，不靠 sheetN 的序号猜）。"""
    z = zipfile.ZipFile(io.BytesIO(raw))
    book = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels.iter(f"{{{_NS_PKG}}}Relationship")}
    for el in book.iter(f"{{{_NS_MAIN}}}sheet"):
        if el.get("name") == sheet:
            target = targets[el.get(f"{{{_NS_REL}}}id")]
            return target.lstrip("/") if target.startswith("/") else f"xl/{target}"
    raise KeyError(f"没有工作表「{sheet}」")


def _cell_re(ref: str) -> re.Pattern[str]:
    # openpyxl 写出的格：<c r="C28"><f>SUM(C22:C25)</f><v></v></c>（可能带 s=、t= 属性）
    return re.compile(rf'<c r="{ref}"(?P<attrs>[^>]*)>(?P<f><f>[^<]*</f>)(?P<v><v\s*/>|<v>[^<]*</v>)?</c>')


def fill_cached(xml: str, values: dict[str, object]) -> str:
    """给公式格补上 <v> 缓存值（模拟 Excel 保存过）。每个坐标必须恰好命中一个公式格。"""
    for ref, v in values.items():
        text = repr(v) if isinstance(v, float) else str(v)
        xml, n = _cell_re(ref).subn(lambda m: f'<c r="{ref}"{m.group("attrs")}>{m.group("f")}<v>{text}</v></c>', xml)
        if n != 1:
            raise AssertionError(f"公式格 {ref} 命中 {n} 次")
    return xml


def excel_saved(raw: bytes, sheet: str, values: dict[str, object], *, full_calc: bool = False) -> bytes:
    """补缓存值，并按 full_calc 决定是否保留 workbook.xml 的 fullCalcOnLoad（openpyxl 默认写它）。"""
    edits: dict[str, Callable[[str], str]] = {}
    if values:
        edits[sheet_part(raw, sheet)] = lambda t: fill_cached(t, values)
    if not full_calc:
        edits["xl/workbook.xml"] = lambda t: t.replace(' fullCalcOnLoad="1"', "")
    return patch_parts(raw, edits) if edits else raw


def has_full_calc(raw: bytes) -> bool:
    return 'fullCalcOnLoad="1"' in read_part(raw, "xl/workbook.xml")
