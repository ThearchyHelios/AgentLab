"""差异卡（期 2，WP-5b；P2-SPEC 7.6）：上传新一期时，拿本期的导入回执和上一期的比，列出哪里和上次不一样。

**为什么要有差异卡。** 配方能通过试运行，只说明这一期的文件「对得上结构」；标签写法、块的先后、合计由
公式变成写死的数、区域外多了一句含数字的说明，这些都不会让试运行失败，却可能改变数据的口径（E3、E7、
R3）。差异卡把它们逐条摆出来：只是信息的（统计期、行数）标成 info，可能改变口径的标成「需确认」，
对应的确认项（`diff:<kind>:<键>`）由 recipe_confirm 收进必勾清单。

**输入形状。** prev / cur 都是「导入回执」``{"receipt": 回执, "checks": [核对摘要], "period": PeriodOut}``，
JSON 形状的 dict（dataclass 也收，先转成 dict）。回执就是 ``dataclasses.asdict(Extraction)`` 去掉
problems 之后的样子；逐项比较时用到其中的 RECEIPT_KEYS（tables、labels、block_order、derived_form、axes、
outside_text、ignored_columns、hidden、placeholders、canonicalized、sheets、lineage、full_calc_on_load；
列表的列顺序只能靠溯源里各列首格的列号推出来），外加顶层的 checks。

**缺键就报错（ReceiptIncomplete），不按空处理。** 早先的写法是「缺的键按空处理、比不了的项不出」，结果
调用方少带一个键（清单里没存 axes、cur 用了只有前 50 条规范写法对照的 TrialOut.receipt），需确认的
axis_form、label_writing、canon_values 就静默消失了（H2）。所以：
- prev、cur 都必须有 ``receipt``，回执里必须有 tables；
- 逐项比较时（上一期也是按配方导入的、配方没变），两边的回执都必须带齐 RECEIPT_KEYS、顶层带 checks；
  规范写法对照被截断（canonicalized_total 大于条数）、或是部分干跑（partial）的结果，同样报错；
- 判断不了配方变没变（没传 recipe_changed，两边又没都带配方哈希）时报错，不默认「没变」去跨配方逐项比；
- 逐项比较时必须给本期配方：列表表头写法的确认项按**块**出，表 → 块只能从配方里查。

cur 取自本次试运行；prev 取自**现行导入的导入清单**（7.7），不取 TableBuild.report：构建可能被复用，
回执里没有 C1、C2，文件名也不是那一次导入的（评审甲-16）。上一期是简单导入（kind=switch，prev 是期 1 的
TableBuild.report，没有 ledger 键）或配方变了（redraft、改配方）时，两边的分段、块、表不一定对得上，
只给 info 类的 rows、period；破坏性变更由 recipe_confirm 按新旧 derive_tables 另算（breaking:*）。

**数字遮盖后的模板**（digit_template）：canon 之后把每一段连续的 ASCII 数字换成 ``{}``。文件名、统计期格、
区域外文字的「写法变了」都按它比，每月必变的日期因此不算变化。

纯函数，不碰数据库、不读文件。
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any

from app.data.names import canon
from app.data.recipe_parsers import match_key, period_residue
from app.data.recipe_types import DiffItem, ListBlock, Recipe

_DIGITS = re.compile(r"[0-9]+")

#: 差异卡的全部 kind，按界面上的先后（7.6 的表；outside_moved 是表里「只是位置变了的算 info」那一项）
DIFF_KINDS: tuple[str, ...] = (
    "period", "rows", "placeholders", "checks", "file_name", "full_calc", "context_text",
    "context_source_added", "column_order", "label_writing", "header_writing", "canon_values", "row_order",
    "block_order", "derived_form", "axis_form", "outside_added", "outside_removed", "outside_changed",
    "outside_moved", "ignored_columns", "hidden",
)

#: 需确认的 kind → 确认项 id 的前缀（id = 前缀 + 键，键的格式见契约 recipe_types 的模块 docstring）
CONFIRM_PREFIX: dict[str, str] = {
    "label_writing": "diff:label_writing:",
    "header_writing": "diff:header_writing:",
    "canon_values": "diff:canon:",
    "row_order": "diff:row_order:",
    "block_order": "diff:block_order:",
    "derived_form": "diff:derived_form:",
    "axis_form": "diff:axis_form:",
    "outside_added": "diff:outside:",
    "outside_removed": "diff:outside:",
    "outside_changed": "diff:outside:",
    "ignored_columns": "diff:ignored:",
    "hidden": "diff:hidden:",
}

_STATUS = {"passed": "通过", "mismatch": "不一致", "unverifiable": "无法核对", "info": "提示"}
_DERIVED_FORM = {"formula": "公式", "literal": "写死的数", "mixed": "公式与写死的数混用"}
_AXIS_FORM = {"text": "文字", "date": "日期格", "mixed": "文字与日期格混用"}
_SHORT = 40
_A1 = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})$")
_WS = re.compile(r"\s+")


#: 逐项比较时两边回执都必须有的键（7.7 的导入清单 receipt 照 asdict(Extraction) 去掉 problems 写就都有）
RECEIPT_KEYS: tuple[str, ...] = (
    "tables", "labels", "block_order", "derived_form", "axes", "outside_text", "ignored_columns", "hidden",
    "placeholders", "canonicalized", "sheets", "lineage", "full_calc_on_load",
)


class ReceiptIncomplete(ValueError):
    """导入回执缺了比较要用的键、被截断或只是部分干跑：调用方的错，报出来，不按空处理（见模块 docstring）。

    side 是 prev / cur / extraction，missing 是缺的键（截断、部分干跑时写成 canonicalized_total、partial）。
    """

    def __init__(self, side: str, missing: list[str]) -> None:
        self.side = side
        self.missing = list(missing)
        super().__init__(f"{side} 不完整，缺少比较要用的内容：{'、'.join(self.missing)}")


# --------------------------------------------------------------------------
# 小工具（recipe_confirm 也用）
# --------------------------------------------------------------------------


def digit_template(text: Any) -> str:
    """数字遮盖后的模板：「统计时间范围：2026年9月1日至2026年9月30日」→「统计时间范围：{}年{}月{}日至{}年{}月{}日」。

    先 canon（全角数字经 NFKC 变成 ASCII、横线统一、空白折叠），再把每一段连续的 ASCII 数字换成 {}。
    """
    return _DIGITS.sub("{}", canon("" if text is None else str(text)))


def same_period_sentence(old: Any, new: Any) -> bool:
    """两期的统计期格是不是「每月重复的同一句」：数字遮盖后的模板相同，且只去掉统计期那一段之后剩下的文字
    （period_residue：其余数字照留）相同。任一期认不出统计期（None）就不算同一句。

    只比遮盖模板不够（AU-3）：「注：2026年9月数据只含3个分区」与「注：2026年10月数据只含2个分区」模板相同，
    但统计期以外的口径数字变了。确认项（outside_digits 免确认的条件 (b)）与差异卡（outside_changed）共用这一个判定。
    """
    if digit_template(old) != digit_template(new):
        return False
    ra, rb = period_residue(old), period_residue(new)
    return ra is not None and rb is not None and match_key(ra) == match_key(rb)


def plain(x: Any) -> Any:
    """dataclass（含嵌套）转成 JSON 形状；dict、list 逐层处理，其余原样。调用方传 Extraction 或 asdict 都行。"""
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return dataclasses.asdict(x)
    if isinstance(x, dict):
        return {k: plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [plain(v) for v in x]
    return x


def coord(sheet: Any, cell: Any) -> str:
    """「工作表!A1」。cell 已经带工作表名的原样返回；不知道工作表时只给 A1。"""
    cell = str(cell or "")
    if "!" in cell or not sheet:
        return cell
    return f"{sheet}!{cell}"


def split_coord(text: Any) -> tuple[str | None, int | None, int | None]:
    """「工作表!C5」→ (工作表, 行, 列号)；认不出的部分为 None。"""
    s = str(text or "")
    sheet, _, a1 = s.rpartition("!")
    m = _A1.match(a1.strip())
    if m is None:
        return (sheet or None), None, None
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + (ord(ch) - 64)
    return (sheet or None), int(m.group(2)), col


def col_letter(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def short(text: Any, limit: int = _SHORT) -> str:
    """给标题用的短写法：空白折叠，超长的截断加「…」，全文放 detail。不做 canon：界面上照原文显示，
    canon 的 NFKC 会把全角标点变成半角。"""
    s = _WS.sub(" ", "" if text is None else str(text)).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def rows_text(rows: Any) -> str:
    """[28, 29, 30] →「第 28–30 行」；不连续的用顿号；空的给空串。"""
    nums = sorted({int(r) for r in rows or [] if isinstance(r, int) or str(r).isdigit()})
    if not nums:
        return ""
    if len(nums) > 1 and nums[-1] - nums[0] == len(nums) - 1:
        return f"第 {nums[0]}–{nums[-1]} 行"
    return "第 " + "、".join(str(n) for n in nums) + " 行"


def receipt_of(doc: Any) -> dict[str, Any]:
    doc = plain(doc) or {}
    rec = doc.get("receipt") if isinstance(doc, dict) else None
    return rec if isinstance(rec, dict) else {}


def require_receipt(doc: Any, side: str, keys: tuple[str, ...] = RECEIPT_KEYS, *,
                    top: tuple[str, ...] = ("checks",)) -> None:
    """检查导入回执带齐了 keys（回执里）和 top（顶层），没有被截断、不是部分干跑；不满足就抛 ReceiptIncomplete。"""
    doc = plain(doc)
    if not isinstance(doc, dict) or not isinstance(doc.get("receipt"), dict):
        raise ReceiptIncomplete(side, ["receipt"])
    rec = doc["receipt"]
    missing = [k for k in top if k not in doc] + [k for k in keys if k not in rec]
    if "canonicalized" in keys and "canonicalized" in rec and "canonicalized_total" in rec:
        # TrialOut.receipt 里的 canonicalized 只有前 50 条；拿它比会漏掉新写法
        if int(rec.get("canonicalized_total") or 0) > len(rec.get("canonicalized") or []):
            missing.append("canonicalized_total")
    if rec.get("partial"):
        missing.append("partial")
    if missing:
        raise ReceiptIncomplete(side, missing)


def recipe_sha_of(doc: Any) -> str | None:
    """导入回执上的配方哈希：顶层 recipe_sha256，或导入清单的 recipe.sha256（7.7）。"""
    doc = plain(doc) or {}
    if not isinstance(doc, dict):
        return None
    sha = doc.get("recipe_sha256")
    if not sha and isinstance(doc.get("recipe"), dict):
        sha = doc["recipe"].get("sha256")
    return str(sha) if sha else None


def period_of(doc: Any) -> dict[str, Any] | None:
    """导入回执的统计期：顶层 period 优先（人工录入只记在那里），没有再看回执里的。"""
    doc = plain(doc) or {}
    p = doc.get("period") if isinstance(doc, dict) else None
    if not isinstance(p, dict):
        p = receipt_of(doc).get("period")
    return p if isinstance(p, dict) else None


def checks_of(doc: Any) -> list[dict[str, Any]]:
    doc = plain(doc) or {}
    rows = doc.get("checks") if isinstance(doc, dict) else None
    return [c for c in rows or [] if isinstance(c, dict) and c.get("id")]


def check_status(doc: Any, check_id: str) -> str | None:
    for c in checks_of(doc):
        if c.get("id") == check_id:
            return str(c.get("status") or "")
    return None


def default_sheet(rec: dict[str, Any]) -> str | None:
    """只有一张配方工作表时它的实际名字：统计期格的坐标万一没带工作表名，靠它补上。"""
    matched = (rec.get("sheets") or {}).get("matched") or {}
    names = {str(v) for v in matched.values() if v}
    if len(names) == 1:
        return next(iter(names))
    sheets = {str(o.get("sheet")) for o in rec.get("outside_text") or [] if isinstance(o, dict) and o.get("sheet")}
    return next(iter(sheets)) if len(sheets) == 1 else None


def outside_of(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """区域外文字（含带附加文字的统计期格），坐标统一成「工作表!A1」。"""
    out = []
    for o in rec.get("outside_text") or []:
        if not isinstance(o, dict):
            continue
        out.append({"coord": coord(o.get("sheet"), o.get("cell")), "text": str(o.get("text") or ""),
                    "kind": str(o.get("kind") or "text"), "period_source": bool(o.get("period_source"))})
    return out


def period_texts(doc: Any, rec: dict[str, Any]) -> dict[str, str]:
    """统计期来源格：坐标 → 全文。"""
    p = period_of(doc) or {}
    sheet = default_sheet(rec)
    return {coord(sheet, k): str(v or "") for k, v in (p.get("texts") or {}).items()}


def period_annotated(doc: Any, rec: dict[str, Any]) -> dict[str, str]:
    """带附加文字的统计期格：坐标 → period_residue。"""
    p = period_of(doc) or {}
    sheet = default_sheet(rec)
    return {coord(sheet, k): str(v or "") for k, v in (p.get("annotated") or {}).items()}


# --------------------------------------------------------------------------
# 差异卡
# --------------------------------------------------------------------------


def diff_reports(prev: dict[str, Any] | None, cur: dict[str, Any], *, prev_file_name: str | None,
                 cur_file_name: str, recipe: Recipe | dict[str, Any] | None = None,
                 recipe_changed: bool | None = None) -> list[DiffItem]:
    """上一期 → 本期的差异卡。prev 为 None（首次）返回 []。需确认的排在前面，同类按 DIFF_KINDS 的顺序。

    上一期的回执没有 ledger 键（简单导入，kind=switch）或配方变了（redraft、改配方）时只给 rows、period；
    否则逐项比较。规格之外多了两个参数：
    - recipe：本期的配方，逐项比较时必须给。列表的表头写法变化要按**块**出确认项
      （``diff:header_writing:<块>``），回执里只有表，「表 → 块」只能从配方里查。
    - recipe_changed：配方与上一期是否不同。不传时看 prev、cur 两边的配方哈希（顶层 ``recipe_sha256``，
      或导入清单的 ``recipe.sha256``）；两边没都带、又没传时报错，不默认「没变」。传了、两边也都带了哈希
      而两者矛盾时同样报错。
    """
    if prev is None:
        return []
    prev, cur = plain(prev) or {}, plain(cur) or {}
    for side, doc in (("prev", prev), ("cur", cur)):
        require_receipt(doc, side, ("tables",), top=())
    p_rec, c_rec = receipt_of(prev), receipt_of(cur)
    items: list[DiffItem] = []
    items += _diff_period(prev, cur)
    items += _diff_rows(p_rec, c_rec)
    if "ledger" in p_rec and not _recipe_changed(prev, cur, recipe_changed):
        require_receipt(prev, "prev")
        require_receipt(cur, "cur")
        if recipe is None:
            raise ValueError("逐项比较需要本期配方：列表表头写法的确认项按块出，表与块的对应只能从配方里查")
        blocks = _list_blocks(recipe)
        items += _diff_placeholders(p_rec, c_rec)
        items += _diff_checks(prev, cur)
        items += _diff_file_name(prev_file_name, cur_file_name)
        items += _diff_full_calc(p_rec, c_rec)
        items += _diff_context_text(prev, cur, p_rec, c_rec)
        items += _diff_column_order(p_rec, c_rec)
        items += _diff_labels(p_rec, c_rec)
        items += _diff_header_writing(p_rec, c_rec, blocks)
        items += _diff_canon(p_rec, c_rec)
        items += _diff_block_order(p_rec, c_rec)
        items += _diff_forms(p_rec, c_rec)
        items += _diff_outside(prev, cur, p_rec, c_rec)
        items += _diff_ignored(p_rec, c_rec)
        items += _diff_hidden(p_rec, c_rec)
    order = {k: i for i, k in enumerate(DIFF_KINDS)}
    items.sort(key=lambda d: (not d.requires_confirm, order.get(d.kind, len(order))))
    return items


def _recipe_changed(prev: dict[str, Any], cur: dict[str, Any], flag: bool | None) -> bool:
    """配方与上一期是否不同。传了 flag 就用它（两边都带哈希时还要对得上）；没传就比两边的配方哈希。"""
    a, b = recipe_sha_of(prev), recipe_sha_of(cur)
    if a and b:
        if flag is not None and flag != (a != b):
            raise ValueError("recipe_changed 与 prev、cur 上的配方哈希不一致")
        return a != b
    if flag is None:
        raise ValueError("无法判断配方是否与上一期相同：请传 recipe_changed，或在 prev、cur 上都带配方哈希")
    return flag


def _item(kind: str, label: str, detail: str = "", key: str | None = None) -> DiffItem:
    prefix = CONFIRM_PREFIX.get(kind)
    if prefix is None:
        return DiffItem(kind=kind, label=label, detail=detail)
    return DiffItem(kind=kind, label=label, detail=detail, requires_confirm=True, confirm_id=f"{prefix}{key}")


def _period_span(p: dict[str, Any] | None) -> tuple[str, str] | None:
    if not p or not p.get("start") or not p.get("end"):
        return None
    return str(p["start"]), str(p["end"])


def _diff_period(prev: dict[str, Any], cur: dict[str, Any]) -> list[DiffItem]:
    a, b = _period_span(period_of(prev)), _period_span(period_of(cur))
    if a == b:
        return []
    if a is None:
        return [_item("period", f"统计期：本期 {b[0]} 至 {b[1]}", "上一期没有记录统计期")]
    if b is None:
        return [_item("period", f"统计期：上一期 {a[0]} 至 {a[1]}，本期未能确定")]
    return [_item("period", f"统计期 {a[0]} 至 {a[1]} → {b[0]} 至 {b[1]}")]


def _table_rows(rec: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in rec.get("tables") or []:
        if isinstance(t, dict) and t.get("name") is not None:
            out[str(t["name"])] = int(t.get("rows") or 0)
    return out


def _placeholder_counts(rec: dict[str, Any]) -> dict[str, int]:
    return {str(k): int(v or 0) for k, v in (rec.get("placeholders") or {}).items()}


def _diff_rows(p_rec: dict[str, Any], c_rec: dict[str, Any]) -> list[DiffItem]:
    a, b = _table_rows(p_rec), _table_rows(c_rec)
    if a == b:
        return []
    parts = []
    for name in list(dict.fromkeys([*b, *a])):
        if name in a and name in b:
            if a[name] != b[name]:
                parts.append(f"「{name}」{a[name]} → {b[name]}")
        elif name in b:
            parts.append(f"「{name}」本期新增，{b[name]} 行")
        else:
            parts.append(f"「{name}」本期没有，上一期 {a[name]} 行")
    pa, pb = _placeholder_counts(p_rec), _placeholder_counts(c_rec)
    notes = [f"占位符「{t}」{pa.get(t, 0)} → {pb.get(t, 0)} 格"
             for t in dict.fromkeys([*pa, *pb]) if pa.get(t, 0) != pb.get(t, 0)]
    return [_item("rows", "行数：" + "；".join(parts), "；".join(notes))]


def _diff_placeholders(p_rec: dict[str, Any], c_rec: dict[str, Any]) -> list[DiffItem]:
    # 只比原文集合：每月个数都会变（31 天 → 30 天），个数写在 rows 的细节里
    a = {t for t, n in _placeholder_counts(p_rec).items() if n > 0}
    b = {t for t, n in _placeholder_counts(c_rec).items() if n > 0}
    if a == b:
        return []
    parts = []
    if b - a:
        parts.append("新出现" + "".join(f"「{t}」" for t in sorted(b - a)))
    if a - b:
        parts.append("不再出现" + "".join(f"「{t}」" for t in sorted(a - b)))
    return [_item("placeholders", "占位符：" + "；".join(parts))]


def _diff_checks(prev: dict[str, Any], cur: dict[str, Any]) -> list[DiffItem]:
    # 比 (id, 状态) 的集合：核对数每期都变，不算
    a = {c["id"]: str(c.get("status") or "") for c in checks_of(prev)}
    b = {c["id"]: str(c.get("status") or "") for c in checks_of(cur)}
    if set(a.items()) == set(b.items()):
        return []
    parts = []
    for cid in dict.fromkeys([*b, *a]):
        if cid in a and cid in b:
            if a[cid] != b[cid]:
                parts.append(f"{cid} {_STATUS.get(a[cid], a[cid])} → {_STATUS.get(b[cid], b[cid])}")
        elif cid in b:
            parts.append(f"{cid} 本期新出现（{_STATUS.get(b[cid], b[cid])}）")
        else:
            parts.append(f"{cid} 本期不再出现")
    return [_item("checks", "核对结果：" + "；".join(parts))]


def _base_name(name: str | None) -> str:
    return str(name or "").replace("\\", "/").rsplit("/", 1)[-1]


def _diff_file_name(prev_name: str | None, cur_name: str) -> list[DiffItem]:
    if prev_name is None:
        return []
    a, b = _base_name(prev_name), _base_name(cur_name)
    if digit_template(a) == digit_template(b):
        return []
    return [_item("file_name", f"文件名的写法变了：「{short(a)}」→「{short(b)}」")]


def _diff_full_calc(p_rec: dict[str, Any], c_rec: dict[str, Any]) -> list[DiffItem]:
    """本期设了打开时重算就出（规格 7.6「这一期设了」，不论上一期设没设）：导入用的是保存值，每期都提醒。"""
    if not c_rec.get("full_calc_on_load"):
        return []
    before = "上一期也设置了" if p_rec.get("full_calc_on_load") else "上一期没有设置"
    return [_item("full_calc", "本期文件设置了打开时重新计算公式", f"导入使用文件里保存的值；{before}")]


def _diff_context_text(prev: dict[str, Any], cur: dict[str, Any], p_rec: dict[str, Any],
                       c_rec: dict[str, Any]) -> list[DiffItem]:
    """两期都是统计期来源、而且都只是在说统计期的格（不在区域外文字里）：写法变了、C1 不是 mismatch 时出 info（D12）。

    带附加文字的统计期格同时是区域外文字，写法变化归 outside_*（要确认），不在这里重复。
    """
    if check_status(cur, "C1") == "mismatch":
        return []
    a, b = period_texts(prev, p_rec), period_texts(cur, c_rec)
    in_outside = {o["coord"] for o in outside_of(p_rec)} | {o["coord"] for o in outside_of(c_rec)}
    out = []
    for cell in b:
        if cell in a and cell not in in_outside and digit_template(a[cell]) != digit_template(b[cell]):
            out.append(_item("context_text", f"统计期格 {cell} 的写法变了：「{short(a[cell])}」→「{short(b[cell])}」",
                             f"上一期：{a[cell]}；本期：{b[cell]}"))
    return out


def _is_list_table(t: dict[str, Any]) -> bool:
    # 交叉表产出的表一定有轴列（宽表、长表、表内合计都以它开头）；列表合计表是 reported_total
    cols = [c for c in t.get("columns") or [] if isinstance(c, dict)]
    return str(t.get("kind") or "data") == "data" and not any(c.get("role") == "axis" for c in cols)


def _tables(rec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(t["name"]): t for t in rec.get("tables") or [] if isinstance(t, dict) and t.get("name") is not None}


def _column_positions(rec: dict[str, Any], table: str) -> dict[str, int]:
    """溯源里各列首格的列号：列表一列一段（向下），首格的列号就是这一列在工作表里的位置。"""
    out: dict[str, int] = {}
    for col, runs in ((rec.get("lineage") or {}).get(table) or {}).items():
        for run in runs or []:
            if isinstance(run, (list, tuple)) and len(run) >= 3:
                _, _, c = split_coord(run[2])
                if c is not None:
                    out[str(col)] = min(out.get(str(col), c), c)
    return out


def _diff_column_order(p_rec: dict[str, Any], c_rec: dict[str, Any]) -> list[DiffItem]:
    pa, pb = _tables(p_rec), _tables(c_rec)
    out = []
    for name, t in pb.items():
        if name not in pa or not _is_list_table(t) or not _is_list_table(pa[name]):
            continue
        a, b = _column_positions(p_rec, name), _column_positions(c_rec, name)
        common = set(a) & set(b)
        if len(common) < 2:
            continue
        oa = sorted(common, key=lambda c: a[c])
        ob = sorted(common, key=lambda c: b[c])
        if oa != ob:
            out.append(_item("column_order", f"表「{name}」的列顺序变了",
                             f"上一期：{'、'.join(oa)}；本期：{'、'.join(ob)}"))
    return out


def _segment_labels(rec: dict[str, Any]) -> dict[str, tuple[list[str], list[str]]]:
    out: dict[str, tuple[list[str], list[str]]] = {}
    for s in rec.get("labels") or []:
        if isinstance(s, dict) and s.get("segment") is not None:
            out[str(s["segment"])] = ([str(x) for x in s.get("raw") or []],
                                      [str(x) for x in s.get("canonical") or []])
    return out


def _diff_labels(p_rec: dict[str, Any], c_rec: dict[str, Any]) -> list[DiffItem]:
    """label_writing（同一规范写法的原文变了，D11、D22、D24）与 row_order（分段内顺序变了，D19）。

    原文逐字比：en dash 的「7–8」和「7-8」canon 之后相同，正是要报的那种写法变化（E3）。
    """
    a, b = _segment_labels(p_rec), _segment_labels(c_rec)
    writing, order = [], []
    for seg, (raw_b, can_b) in b.items():
        if seg not in a:
            continue
        raw_a, can_a = a[seg]
        map_a = dict(zip(reversed(can_a), reversed(raw_a)))
        map_b = dict(zip(reversed(can_b), reversed(raw_b)))
        pairs = [(map_a[c], map_b[c]) for c in dict.fromkeys(can_b) if c in map_a and map_a[c] != map_b[c]]
        if pairs:
            shown = "；".join(f"「{x}」→「{y}」" for x, y in pairs[:3]) + ("等" if len(pairs) > 3 else "")
            writing.append(_item("label_writing", f"分段「{seg}」的标签写法变了：{shown}",
                                 f"共 {len(pairs)} 个标签；按规范写法存储的值不变", key=seg))
        common = set(can_a) & set(can_b)
        seq_a = [c for c in dict.fromkeys(can_a) if c in common]
        seq_b = [c for c in dict.fromkeys(can_b) if c in common]
        if seq_a != seq_b:
            order.append(_item("row_order", f"分段「{seg}」的行顺序变了",
                               f"上一期：{'、'.join(seq_a)}；本期：{'、'.join(seq_b)}", key=seg))
    return writing + order


def _list_blocks(recipe: Recipe | dict[str, Any]) -> dict[str, str]:
    """列表的表名 → 块 id（只看本期配方）。"""
    r = recipe if isinstance(recipe, Recipe) else Recipe.model_validate(recipe)
    out: dict[str, str] = {}
    for sheet in r.sheets:
        for block in sheet.blocks:
            if isinstance(block, ListBlock):
                out.setdefault(block.table, block.id)
    return out


def _diff_header_writing(p_rec: dict[str, Any], c_rec: dict[str, Any], blocks: dict[str, str]) -> list[DiffItem]:
    pa, pb = _tables(p_rec), _tables(c_rec)
    out = []
    for name, t in pb.items():
        if name not in pa or not _is_list_table(t):
            continue
        if name not in blocks:
            # 回执里的列表表在配方里找不到块：传进来的配方不是产出这份回执的那份
            raise ValueError(f"本期配方里没有写入表「{name}」的列表块，配方与回执对不上")
        ha = {str(c.get("name")): c.get("header") for c in pa[name].get("columns") or [] if isinstance(c, dict)}
        pairs = []
        for c in t.get("columns") or []:
            if not isinstance(c, dict):
                continue
            old, new = ha.get(str(c.get("name"))), c.get("header")
            if old is not None and new is not None and str(old) != str(new):
                pairs.append((str(old), str(new)))
        if pairs:
            shown = "；".join(f"「{short(x)}」→「{short(y)}」" for x, y in pairs[:3]) + ("等" if len(pairs) > 3 else "")
            out.append(_item("header_writing", f"表「{name}」的表头写法变了：{shown}",
                             f"共 {len(pairs)} 列；列名和导入的值不变", key=blocks[name]))
    return out


def _diff_canon(p_rec: dict[str, Any], c_rec: dict[str, Any]) -> list[DiffItem]:
    """列表的主键列、canonical 存储的 TEXT 列出现新的「原文 ≠ 规范写法」对（来自标签的值不在 canonicalized 里）。"""
    def pairs(rec: dict[str, Any]) -> list[tuple[str, str, str, str]]:
        return [(str(e.get("table")), str(e.get("column")), str(e.get("raw")), str(e.get("canonical")))
                for e in rec.get("canonicalized") or [] if isinstance(e, dict)]

    seen = set(pairs(p_rec))
    grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for t, c, raw, can in pairs(c_rec):
        if (t, c, raw, can) not in seen:
            grouped.setdefault((t, c), []).append((raw, can))
    out = []
    for (t, c), new in grouped.items():
        shown = "；".join(f"「{short(r)}」按「{short(k)}」保存" for r, k in new[:3]) + ("等" if len(new) > 3 else "")
        out.append(_item("canon_values", f"表「{t}」列「{c}」出现新的写法：{shown}",
                         f"共 {len(new)} 种", key=f"{t}.{c}"))
    return out


def _sheet_ids(rec: dict[str, Any]) -> dict[str, str]:
    """实际工作表名 → 配方工作表 id：工作表改了名，块的先后照样能和上一期对上。"""
    return {str(v): str(k) for k, v in ((rec.get("sheets") or {}).get("matched") or {}).items()}


def _diff_block_order(p_rec: dict[str, Any], c_rec: dict[str, Any]) -> list[DiffItem]:
    a = {str(k): [str(x) for x in v or []] for k, v in (p_rec.get("block_order") or {}).items()}
    b = {str(k): [str(x) for x in v or []] for k, v in (c_rec.get("block_order") or {}).items()}
    ida, idb = _sheet_ids(p_rec), _sheet_ids(c_rec)
    by_id_a = {ida[k]: v for k, v in a.items() if k in ida}
    out = []
    for sheet, seq_b in b.items():
        seq_a = a.get(sheet)
        if seq_a is None and sheet in idb:
            seq_a = by_id_a.get(idb[sheet])
        if seq_a is None:
            continue
        common = set(seq_a) & set(seq_b)
        fa = [k for k in seq_a if k in common]
        fb = [k for k in seq_b if k in common]
        if fa != fb:
            out.append(_item("block_order", f"工作表「{sheet}」上各部分的先后变了",
                             f"上一期：{'、'.join(fa)}；本期：{'、'.join(fb)}", key=sheet))
    return out


def _diff_forms(p_rec: dict[str, Any], c_rec: dict[str, Any]) -> list[DiffItem]:
    out = []
    a = {str(k): str(v) for k, v in (p_rec.get("derived_form") or {}).items()}
    for seg, form in ((c_rec.get("derived_form") or {}).items()):
        seg, form = str(seg), str(form)
        if seg in a and a[seg] != form:
            out.append(_item("derived_form",
                             f"分段「{seg}」的合计由{_DERIVED_FORM.get(a[seg], a[seg])}改为"
                             f"{_DERIVED_FORM.get(form, form)}",
                             "合计仍按明细核对", key=seg))

    def axes(rec: dict[str, Any]) -> dict[str, str]:
        return {str(x.get("block")): str(x.get("form") or "") for x in rec.get("axes") or []
                if isinstance(x, dict) and x.get("block") is not None}

    aa, ab = axes(p_rec), axes(c_rec)
    for blk, form in ab.items():
        if blk in aa and aa[blk] != form:
            out.append(_item("axis_form",
                             f"「{blk}」的日期表头由{_AXIS_FORM.get(aa[blk], aa[blk])}改为{_AXIS_FORM.get(form, form)}",
                             key=blk))
    return out


def _diff_outside(prev: dict[str, Any], cur: dict[str, Any], p_rec: dict[str, Any],
                  c_rec: dict[str, Any]) -> list[DiffItem]:
    """区域外文字（含带附加文字的统计期格）的增、删、改，以及「区域外纯文字变成统计期来源」（D17、D27）。

    顺序：先按全文配对（同格的先配；只是位置变了的出 info）；再看同一格从纯文字变成统计期来源、满足条件的
    归入 context_source_added；剩下同一格的按数字遮盖后的模板比（只是日期变了不算改）；其余算增、删。
    """
    p_out, c_out = outside_of(p_rec), outside_of(c_rec)
    p_per, c_per = period_texts(prev, p_rec), period_texts(cur, c_rec)
    c_ann = period_annotated(cur, c_rec)
    c1_ok = check_status(cur, "C1") == "passed"
    info: list[DiffItem] = []
    confirm: list[DiffItem] = []

    def take(pool: list[dict[str, Any]], pred: Any) -> dict[str, Any] | None:
        for i, x in enumerate(pool):
            if pred(x):
                return pool.pop(i)
        return None

    # 1. 全文配对
    p_left, c_left = list(p_out), []
    for c in c_out:
        if take(p_left, lambda p, c=c: p["coord"] == c["coord"] and canon(p["text"]) == canon(c["text"])) is None:
            c_left.append(c)
    rest = []
    for c in c_left:
        p = take(p_left, lambda p, c=c: canon(p["text"]) == canon(c["text"]))
        if p is None:
            rest.append(c)
        else:
            info.append(_item("outside_moved", f"区域外文字「{short(c['text'])}」从 {p['coord']} 移到 {c['coord']}"))
    c_left = rest

    # 2. 纯文字变成统计期来源
    for p in list(p_left):
        if p["kind"] != "text" or p["coord"] not in c_per:
            continue
        c = next((x for x in c_left if x["coord"] == p["coord"]), None)
        # 本期这一格还在区域外文字里，按契约就是带附加文字的统计期格（period_source 漏标也一样）
        annotated = p["coord"] in c_ann or c is not None
        residue = c_ann.get(p["coord"])
        if annotated and residue is None and c is not None:
            residue = period_residue(c["text"])
        if not c1_ok or (annotated and (residue is None or match_key(residue) != match_key(p["text"]))):
            continue
        p_left.remove(p)
        if c is not None:
            c_left.remove(c)
        new_text = c_per[p["coord"]]
        info.append(_item("context_source_added",
                          f"{p['coord']} 现在也写明了统计期：「{short(p['text'])}」→「{short(new_text)}」",
                          "统计期与其他来源一致；附加的文字与上一期这一格相同"))

    # 3. 同一格：写法（数字遮盖后的模板）变了才算改。两期都是统计期来源的格，还要比去掉统计期之后的文字
    #    （其余数字照留）：附加文字里的口径数字变了也算改（AU-3）
    def changed(cell: str, old: str, new: str) -> None:
        if cell in p_per and cell in c_per:
            differ = not same_period_sentence(old, new)
        else:
            differ = digit_template(old) != digit_template(new)
        if differ:
            confirm.append(_item("outside_changed", f"{cell} 的区域外文字改了：「{short(old)}」→「{short(new)}」",
                                 f"上一期：{old}；本期：{new}", key=cell))

    for p in list(p_left):
        c = take(c_left, lambda x, p=p: x["coord"] == p["coord"])
        if c is not None:
            p_left.remove(p)
            changed(p["coord"], p["text"], c["text"])
        elif p["coord"] in c_per:
            # 这一格本期只是在说统计期了，但不满足 context_source_added 的条件
            p_left.remove(p)
            changed(p["coord"], p["text"], c_per[p["coord"]])
    for c in list(c_left):
        if c["coord"] in p_per:
            # 上一期只是统计期格，本期多了附加文字
            c_left.remove(c)
            changed(c["coord"], p_per[c["coord"]], c["text"])

    # 4. 其余是增、删
    for p in p_left:
        confirm.append(_item("outside_removed", f"区域外文字「{short(p['text'])}」本期没有了（上一期在 {p['coord']}）",
                             p["text"], key=p["coord"]))
    for c in c_left:
        confirm.append(_item("outside_added", f"区域外新增文字「{short(c['text'])}」（{c['coord']}）",
                             c["text"], key=c["coord"]))
    return confirm + info


def _diff_ignored(p_rec: dict[str, Any], c_rec: dict[str, Any]) -> list[DiffItem]:
    a = {str(k): [str(x) for x in v or []] for k, v in (p_rec.get("ignored_columns") or {}).items()}
    b = {str(k): [str(x) for x in v or []] for k, v in (c_rec.get("ignored_columns") or {}).items()}
    out = []
    for blk in dict.fromkeys([*b, *a]):
        ka = {match_key(x) for x in a.get(blk, [])}
        kb = {match_key(x) for x in b.get(blk, [])}
        if ka != kb:
            now = "".join(f"「{short(x, 20)}」" for x in b.get(blk, [])) or "无"
            before = "".join(f"「{short(x, 20)}」" for x in a.get(blk, [])) or "无"
            out.append(_item("ignored_columns", f"「{blk}」忽略的列变了：本期忽略{now}",
                             f"上一期忽略{before}", key=blk))
    return out


def _hidden_parts(h: dict[str, Any]) -> tuple[set[int] | None, set[str] | None]:
    rows = cols = None
    if str(h.get("policy_rows") or "") in ("include", "exclude"):
        rows = {int(r) for r in h.get("rows") or [] if isinstance(r, int) or str(r).isdigit()}
    if str(h.get("policy_cols") or "") == "include":
        cols = {col_letter(c) if isinstance(c, int) else str(c).upper() for c in h.get("cols") or []}
    return rows, cols


def _diff_hidden(p_rec: dict[str, Any], c_rec: dict[str, Any]) -> list[DiffItem]:
    """照常导入（include）或排除（exclude）隐藏行列时，隐藏的是哪些行列变了。

    规格只写了 include；exclude 下被排除的行变了同样会静默改变导入的数据，一并报（见交付说明）。
    reject_if_any 时有隐藏行列就拒收，不会走到这里。
    """
    a = {str(k): v for k, v in (p_rec.get("hidden") or {}).items() if isinstance(v, dict)}
    b = {str(k): v for k, v in (c_rec.get("hidden") or {}).items() if isinstance(v, dict)}
    out = []
    for sheet in dict.fromkeys([*b, *a]):
        rb, cb = _hidden_parts(b.get(sheet, {}))
        ra, ca = _hidden_parts(a.get(sheet, {}))
        if rb is None and cb is None and ra is None and ca is None:
            continue
        parts = []
        if (ra or set()) != (rb or set()) and (ra is not None or rb is not None):
            parts.append("隐藏行：" + (rows_text(rb) or "本期没有"))
        if (ca or set()) != (cb or set()) and (ca is not None or cb is not None):
            parts.append("隐藏列：" + ("、".join(sorted(cb or ())) + " 列" if cb else "本期没有"))
        if parts:
            out.append(_item("hidden", f"工作表「{sheet}」的隐藏行列变了：" + "；".join(parts), key=sheet))
    return out
