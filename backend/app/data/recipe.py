"""配方语言的静态校验、规范形式与哈希、JSON Patch（期 2，WP-1）。

配方保存时（起草、回答问题、PUT、AI 草稿）都过这里：schema 由契约里的 pydantic 模型保证（recipe_types，
extra="forbid"、取值能枚举的一律 Literal），这里补上 pydantic 表达不了的语义规则（P2-SPEC 2.2 的 13 条）。
problems 为空才能试运行；validate_recipe 不抛异常，问题按「位置说明：原因」的整句给人看，path 是 JSON
Pointer（向导表单据此把问题挂到对应字段旁边）。

**配方内部的一切引用逐字比较**（契约「键的格式」）：segment.table、verify、keep_as、relations、grain、
tables[].units 的键、TotalRow.label_column 都必须与 derive_tables 推出的名字逐字相等，不用 name_key、不用
match_key。文件里写法的变化（全角、空白）由执行器按 match_key 认，与配方内部的引用无关；配方内部若也宽容，
「Sales」和「sales」这种差一点的写法会被静默认成同一张表，下游 SQL 却按另一个名字查。

**哈希的前向兼容**（期 3 要给配方加修复类字段）。canonical 形式去掉等于默认值的字段（pydantic 的
exclude_defaults 对 default_factory 同样生效；axis.checks 在契约里按集合规范化，顺序不同的默认值也会被去掉），
于是以后新增一个带默认值的字段，已存配方的哈希和构建 id 都不变。配套的规矩：
- **不改已有字段的默认值**（改了的话，省略写法的旧配方含义就变了，哈希却不变）；
- 新字段的默认值必须等于「没有这个字段时的行为」；
- 做不到时升 recipe_format，并写旧格式到新格式的迁移，旧版本按旧规则算哈希。
dict 的键序没有语义（derive_tables 不按键序定列序），所以哈希里的 sort_keys 不会让两份不同的配方撞上同一个哈希。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from app.data.names import canon, collide_key, name_key, name_problem
from app.data.recipe_parsers import (
    LABEL_MAX,
    UNITS,
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
    LIST_TOTAL_DIM,
    ColumnOut,
    CrosstabBlock,
    DerivedSegment,
    DimensionSegment,
    Dismissed,
    DraftFacts,
    Fact,
    ListBlock,
    MeasuresSegment,
    NotComparable,
    Recipe,
    RecipeProblem,
    SumEq,
    accumulate_blockers,
    derive_tables,
    prose_problems,
)

Origin = Literal["rules", "ai", "manual", "replay"]

#: pydantic 在 loc 里插在列表下标后面的判别字段标签：转 JSON Pointer 时去掉，否则 path 对不上配方里的位置
_UNION_TAGS = frozenset({"crosstab", "list", "measures", "dimension", "derived", "sum_eq", "not_comparable",
                         "dismissed"})
_NUMERIC = ("INTEGER", "REAL")


class RecipeInvalid(ValueError):
    """配方没过 schema（parse_recipe 抛）。problems 的 code 都是 schema，带 path。"""

    def __init__(self, problems: list[RecipeProblem]):
        self.problems = list(problems)
        super().__init__("；".join(p.message for p in self.problems) or "配方不合法")


# ==========================================================================
# 规范形式、哈希
# ==========================================================================


def canonical_recipe(recipe: Recipe) -> dict[str, Any]:
    """去掉等于默认值的字段的紧凑形式：存库（table_recipes.recipe、import_stagings.recipe）和算哈希都用它。"""
    return recipe.model_dump(mode="json", exclude_defaults=True)


def recipe_sha256(recipe: Recipe) -> str:
    """配方哈希：进构建 id。与 table_versions._sha_json 同一种写法（sort_keys、紧凑分隔符、UTF-8）。"""
    text = json.dumps(canonical_recipe(recipe), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ==========================================================================
# JSON Patch（RFC 6902 的 add / replace / remove）
# ==========================================================================


def _pointer_tokens(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise ValueError(f"路径「{path}」写法不对：应以「/」开头")
    return [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]


def _list_index(token: str, path: str) -> int:
    # RFC 6901：数组下标只能是不带前导零的非负整数
    if not token.isdigit() or (len(token) > 1 and token[0] == "0") or not token.isascii():
        raise ValueError(f"路径「{path}」不存在：「{token}」不是列表下标")
    return int(token)


def _walk(doc: Any, tokens: list[str], path: str) -> Any:
    node = doc
    for t in tokens:
        if isinstance(node, dict):
            if t not in node:
                raise ValueError(f"路径「{path}」不存在")
            node = node[t]
        elif isinstance(node, list):
            i = _list_index(t, path)
            if i >= len(node):
                raise ValueError(f"路径「{path}」不存在：列表只有 {len(node)} 项")
            node = node[i]
        else:
            raise ValueError(f"路径「{path}」不存在")
    return node


def apply_patch(recipe: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """按顺序应用 add / replace / remove，返回新 dict（入参不变）。任何一步出错抛 ValueError，前面的修改也不生效。

    起草问题的 effects、向导表单的修改都走这里；add 到已有的键等于替换（RFC 6902），add 到列表下标是插入、
    「-」是追加；replace、remove 的目标必须存在。
    """
    doc: Any = copy.deepcopy(recipe)
    for n, op in enumerate(ops, start=1):
        if not isinstance(op, dict):
            raise ValueError(f"第 {n} 项修改不是对象")
        kind, path = op.get("op"), op.get("path")
        if kind not in ("add", "replace", "remove"):
            raise ValueError(f"第 {n} 项修改的操作「{kind}」不支持")
        if not isinstance(path, str):
            raise ValueError(f"第 {n} 项修改缺少路径")
        if kind != "remove" and "value" not in op:
            raise ValueError(f"第 {n} 项修改缺少新值")
        tokens = _pointer_tokens(path)
        value = copy.deepcopy(op.get("value"))
        if not tokens:
            if kind == "remove":
                raise ValueError("不能删除整个配方")
            if not isinstance(value, dict):
                raise ValueError("整个配方只能换成一个 JSON 对象")
            doc = value
            continue
        parent = _walk(doc, tokens[:-1], path)
        last = tokens[-1]
        if isinstance(parent, dict):
            if kind != "add" and last not in parent:
                raise ValueError(f"路径「{path}」不存在")
            if kind == "remove":
                del parent[last]
            else:
                parent[last] = value
        elif isinstance(parent, list):
            if kind == "add" and last == "-":
                parent.append(value)
                continue
            i = _list_index(last, path)
            if kind == "add":
                if i > len(parent):
                    raise ValueError(f"路径「{path}」不存在：列表只有 {len(parent)} 项")
                parent.insert(i, value)
            else:
                if i >= len(parent):
                    raise ValueError(f"路径「{path}」不存在：列表只有 {len(parent)} 项")
                if kind == "replace":
                    parent[i] = value
                else:
                    del parent[i]
        else:
            raise ValueError(f"路径「{path}」不存在")
    return doc


# ==========================================================================
# 位置说明（给人看的「第 1 个工作表「客流汇总」的分段「夜间」」）
# ==========================================================================


def _esc(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def _pointer(parts: tuple[Any, ...] | list[Any]) -> str:
    return "".join("/" + _esc(p) for p in parts)


def _get(node: Any, key: Any) -> Any:
    if isinstance(node, dict) and isinstance(key, str):
        return node.get(key)
    if isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
        return node[key]
    return None


def _text(node: Any, *keys: str) -> str | None:
    for k in keys:
        node = _get(node, k)
    return node if isinstance(node, str) and node else None


def _where(parts: tuple[Any, ...], data: Any) -> tuple[str, int]:
    """路径 → (位置说明, 用掉了几段)。说明用人话、不露键名；名字取自配方本身（schema 没过时也尽量取）。"""
    if len(parts) >= 2 and parts[0] == "sheets" and isinstance(parts[1], int):
        sheet = _get(_get(data, "sheets"), parts[1])
        name = _text(sheet, "match", "name")
        desc = f"第 {parts[1] + 1} 个工作表" + (f"「{name}」" if name else "")
        used = 2
        if len(parts) >= 4 and parts[2] == "blocks" and isinstance(parts[3], int):
            block = _get(_get(sheet, "blocks"), parts[3])
            used = 4
            if len(parts) >= 6 and parts[4] == "segments" and isinstance(parts[5], int):
                seg = _get(_get(block, "segments"), parts[5])
                sid = _text(seg, "id")
                desc += f"的分段「{sid}」" if sid else f"的第 {parts[5] + 1} 个分段"
                used = 6
            else:
                bid = _text(block, "id")
                desc += f"的块「{bid}」" if bid else f"的第 {parts[3] + 1} 个块"
                if len(parts) >= 6 and parts[4] == "columns" and isinstance(parts[5], int):
                    col = _get(_get(block, "columns"), parts[5])
                    header = _text(col, "header") or _text(col, "name")
                    desc += f"的列「{header}」" if header else f"的第 {parts[5] + 1} 列"
                    used = 6
        elif len(parts) >= 3 and parts[2] == "context":
            desc += "的统计期"
            used = 4 if len(parts) >= 4 and isinstance(parts[3], int) else 3
        return desc, used
    if len(parts) >= 2 and parts[0] == "tables" and isinstance(parts[1], int):
        name = _text(_get(_get(data, "tables"), parts[1]), "name")
        return (f"表「{name}」" if name else f"第 {parts[1] + 1} 张表"), 2
    if len(parts) >= 2 and parts[0] == "relations" and isinstance(parts[1], int):
        rid = _text(_get(_get(data, "relations"), parts[1]), "id")
        return (f"关系「{rid}」" if rid else f"第 {parts[1] + 1} 条关系"), 2
    return "配方", 0


#: schema 报错时字段的中文名（不在句子里裸写键名）
_FIELD_LABEL = {
    "recipe_format": "配方格式", "mode": "导入模式", "other_visible_sheets": "其他可见工作表的处理",
    "sheets": "工作表", "tables": "表", "relations": "关系", "id": "名称", "match": "工作表匹配",
    "name": "名称", "fallback": "找不到同名工作表时的处理", "hidden": "隐藏行列的处理", "rows": "行",
    "cols": "列", "context": "统计期", "kind": "类型", "parser": "解析方式", "prefer_prefix": "优先展示的开头文字",
    "cross_check": "旁证", "blocks": "块", "layout": "版式", "axis": "日期表头", "find": "日期表头的查找",
    "min": "最少日期格数", "type": "类型", "year_from": "年份来源", "checks": "检查项",
    "label_offset": "标签列位置", "values": "取值规则", "placeholders": "占位符", "text": "原文",
    "meaning": "含义", "blank": "空格的处理", "text_number": "文本数字的处理", "formula": "公式的处理",
    "segments": "分段", "role": "分段类型", "table": "表", "locate": "定位方式", "by": "定位依据",
    "title": "分段标题", "segment": "紧跟的分段", "labels": "标签", "expect": "期望的标签",
    "measures": "指标列名对照", "dim": "维度", "derive": "派生列", "value": "值列", "const": "常量列",
    "pick": "取值", "stop_parser": "结束本段的标签", "labels_parser": "标签解析方式", "verify": "核对方式",
    "against_table": "核对的基表", "keep_as": "另存", "header_rows": "表头行数", "after_title": "表头上方的标题",
    "columns": "列", "header": "表头", "store": "存储写法", "extra_columns": "多出的列",
    "blank_rows": "空行的处理", "total_row": "合计行", "label_column": "合计标签所在的列",
    "merged_data": "合并单元格的处理", "grain": "主键", "units": "单位", "note": "说明", "total": "合计列",
    "parts": "组成列", "claims": "认领", "a": "一方", "b": "另一方", "reason": "理由",
    "ignore_rows": "按行标签忽略的行", "ignore_columns": "按表头忽略的列", "ignore_outside": "按同一行文字忽略的数字",
    "label": "标签", "anchor": "同一行的文字",
}


def _field_path(parts: tuple[Any, ...]) -> str:
    out: list[str] = []
    for p in parts:
        if isinstance(p, int):
            out.append(f"第 {p + 1} 项")
        else:
            out.append(_FIELD_LABEL.get(p, f"「{p}」"))
    return "的".join(out)


def _quoted_options(expected: str) -> str:
    opts = re.findall(r"'([^']*)'", expected)
    return "、".join(f"「{o}」" for o in opts) if opts else expected


def _schema_message(err: dict[str, Any], parts: tuple[Any, ...], data: Any) -> str:
    where, used = _where(parts, data)
    rest = parts[used:]
    typ = err.get("type", "")
    ctx = err.get("ctx") or {}
    raw = err.get("input")
    if typ == "missing" and rest:
        parent = _field_path(rest[:-1])
        return f"{where}：{parent + '中' if parent else ''}缺少必填项「{_FIELD_LABEL.get(rest[-1], rest[-1])}」"
    if typ == "extra_forbidden" and rest:
        parent = _field_path(rest[:-1])
        return f"{where}：{parent + '中' if parent else ''}有配方语言不支持的字段「{rest[-1]}」"
    field = _field_path(rest) or "取值"
    if typ == "literal_error":
        reason = f"取值「{raw}」不在可选范围内（可选：{_quoted_options(str(ctx.get('expected', '')))}）"
    elif typ == "union_tag_invalid":
        disc = str(ctx.get("discriminator", "")).strip("'")
        field = _field_path(rest + (disc,)) if disc else field
        reason = (f"「{ctx.get('tag')}」不在可选范围内"
                  f"（可选：{_quoted_options(str(ctx.get('expected_tags', '')))}）")
    elif typ == "union_tag_not_found":
        disc = str(ctx.get("discriminator", "")).strip("'")
        reason = f"缺少「{_FIELD_LABEL.get(disc, disc)}」"
    elif typ == "string_too_long":
        reason = f"长度不能超过 {ctx.get('max_length')} 字"
    elif typ == "string_too_short":
        reason = "不能为空" if ctx.get("min_length") == 1 else f"长度至少 {ctx.get('min_length')} 字"
    elif typ == "too_short":
        reason = f"至少需要 {ctx.get('min_length')} 项"
    elif typ == "too_long":
        reason = f"最多只能有 {ctx.get('max_length')} 项"
    elif typ in ("greater_than_equal", "greater_than"):
        reason = f"不能小于 {ctx.get('ge', ctx.get('gt'))}"
    elif typ in ("less_than_equal", "less_than"):
        reason = f"不能大于 {ctx.get('le', ctx.get('lt'))}"
    elif typ == "string_pattern_mismatch":
        reason = {
            "id": "写法不对：工作表编号以小写字母开头，只含小写字母、数字和下划线；关系编号写成 R1 到 R99",
            "claims": "写法不对：认领写成 F1、F2 这样的编号",
        }.get(str(rest[-1]) if rest else "", "写法不对")
    elif typ in ("int_type", "int_parsing", "int_from_float"):
        reason = "需要是整数"
    elif typ in ("string_type",):
        reason = "需要是文字"
    elif typ in ("list_type",):
        reason = "需要是列表"
    elif typ in ("dict_type", "model_type", "model_attributes_type"):
        reason = "需要是对象"
    else:
        reason = "取值不合法"
    return f"{where}：{field}{reason}"


def _schema_problems(exc: ValidationError, data: Any) -> list[RecipeProblem]:
    out: list[RecipeProblem] = []
    for err in exc.errors(include_url=False):
        parts: list[Any] = []
        prev: Any = None
        for p in err.get("loc", ()):
            # 只去掉紧跟在列表下标后面的那一个标签：measures 分段缺 measures 字段时 loc 是
            # (…, 0, "measures", "measures")，第二个是真字段
            tag = isinstance(p, str) and p in _UNION_TAGS and isinstance(prev, int)
            prev = p
            if p == "[key]" or tag:
                continue
            parts.append(p)
        tparts = tuple(parts)
        out.append(RecipeProblem(_pointer(tparts), "schema", _schema_message(err, tparts, data)))
    return out


def parse_recipe(data: dict[str, Any]) -> Recipe:
    """pydantic 校验（封闭语言：多出来的键、写死的值都在这里被拒）。失败抛 RecipeInvalid，code 都是 schema。"""
    if not isinstance(data, dict):
        raise RecipeInvalid([RecipeProblem("", "schema", "配方：必须是一个 JSON 对象")])
    try:
        return Recipe.model_validate(data)
    except ValidationError as exc:
        raise RecipeInvalid(_schema_problems(exc, data)) from None


# ==========================================================================
# 语义校验（P2-SPEC 2.2）
# ==========================================================================


@dataclass
class _Writer:
    """写入一张表的一处（分段、keep_as、列表块、列表合计）：建表的列由它决定。"""

    table: str
    #: 表名字段的路径（table_unknown 报在这里）
    table_parts: tuple[Any, ...]
    #: 这一处应写进的表的 kind
    kind: Literal["data", "reported_total"]
    #: (列名, 定义这个名字的字段路径)，顺序与 derive_tables 一致
    columns: list[tuple[str, tuple[Any, ...]]]
    #: 交叉表产出的表：主键必须含轴列
    axis: str | None = None
    #: derive_tables 报问题时用的路径（分段、列表块、列表合计行），据此认出是哪张表的列推不出来
    origin: str = ""


def _measures_segments(r: Recipe) -> list[MeasuresSegment]:
    return [s for sheet in r.sheets for b in sheet.blocks if isinstance(b, CrosstabBlock)
            for s in b.segments if isinstance(s, MeasuresSegment)]


def _find_measures(segs: list[MeasuresSegment], seg_id: Any, labels: list[str]) -> MeasuresSegment | None:
    """按 id 找；找不到时按标签找唯一含这些标签的分段。"""
    hit = next((s for s in segs if s.id == seg_id), None)
    if hit is not None:
        return hit
    keys = {match_key(x) for x in labels}
    found = [s for s in segs if keys <= {match_key(x) for x in s.labels.expect}]
    return found[0] if len(found) == 1 else None


def _most_labels(segs: list[MeasuresSegment], labels: list[str]) -> MeasuresSegment | None:
    """过半标签命中的分段，命中最多且唯一时取它（事实里有标签已被去掉、按全部标签认不出时用）。"""
    keys = {match_key(x) for x in labels}
    if not keys:
        return None
    scored = [(len(keys & {match_key(x) for x in s.labels.expect}), s) for s in segs]
    best = max((n for n, _s in scored), default=0)
    top = [s for n, s in scored if n == best]
    return top[0] if best * 2 > len(keys) and len(top) == 1 else None


def _fact_brief(fact: Fact) -> str:
    # 「第 5 行 = 第 6 行 + 第 7 行（31 列中 31 列成立）」→ 去掉末尾的成立情况，只留关系本身
    return re.sub(r"（[^（）]*）\s*$", "", fact.text).strip() or fact.id


class _Checker:
    def __init__(self, recipe: Recipe, facts: DraftFacts | None, origin: str, base: Recipe | None = None):
        self.r = recipe
        self.facts = facts
        self.origin = origin
        #: 现行配方（上传新一期、修改配方时），3.3 的「沿用已确认的常量」和「认领只查改动过的部分」用。
        #: 首次导入、从简单导入切换时为 None，照期 2 全查
        self.base = base
        self.data = recipe.model_dump(mode="json")
        self.problems: list[RecipeProblem] = []
        self._seen: set[tuple[str, str]] = set()
        #: _near 的缓存：名字 → (name_key, match_key)；名字列表 → 键索引（SE-5：几万个未知键逐个对全表各列
        #: 重算 NFKC 是 O(键×列)，几百 KB 的配方能让静态校验跑几十秒）
        self._kk: dict[str, tuple[str, str]] = {}
        self._near_idx: dict[tuple[str, ...], dict[tuple[str, str], list[tuple[int, str]]]] = {}
        # 分段：(si, bi, gi, 块, 分段)
        self.segs: list[tuple[int, int, int, CrosstabBlock, Any]] = []
        self.lists: list[tuple[int, int, ListBlock]] = []
        for si, sheet in enumerate(recipe.sheets):
            for bi, block in enumerate(sheet.blocks):
                if isinstance(block, CrosstabBlock):
                    for gi, seg in enumerate(block.segments):
                        self.segs.append((si, bi, gi, block, seg))
                else:
                    self.lists.append((si, bi, block))
        # 表名 → 第一次声明的位置（重名的另报 name_collision）
        self.declared = {t.name: i for i, t in reversed(list(enumerate(recipe.tables)))}
        self.cols: dict[str, list[ColumnOut]] = {}
        self.writers: list[_Writer] = []
        #: derive_tables 推不准列的表（形状冲突、measures 对不上标签）：根因已报，不再逐个报「列不存在」，
        #: 免得一处写错连带出一串问题（AI 修订时回灌的也只是根因）
        self.broken: set[str] = set()

    # ---- 小工具 ----

    def add(self, parts: tuple[Any, ...], code: str, reason: str, *, where: bool = True) -> None:
        path = _pointer(parts)
        if (path, code) in self._seen:
            return
        self._seen.add((path, code))
        msg = f"{_where(parts, self.data)[0]}：{reason}" if where else reason
        self.problems.append(RecipeProblem(path, code, msg))

    @staticmethod
    def seg_parts(si: int, bi: int, gi: int) -> tuple[Any, ...]:
        return ("sheets", si, "blocks", bi, "segments", gi)

    def col_names(self, table: str) -> list[str]:
        return [c.name for c in self.cols.get(table, []) if c.name]

    def col(self, table: str, name: str) -> ColumnOut | None:
        return next((c for c in self.cols.get(table, []) if c.name == name), None)

    def _keys(self, name: str) -> tuple[str, str]:
        k = self._kk.get(name)
        if k is None:
            k = self._kk[name] = (name_key(name), match_key(name))
        return k

    def _near(self, wanted: str, names: list[str]) -> str:
        # 只给提示，不放行：配方内部引用一律逐字比较。按名字列表建一次键索引，之后每次查表（结果与逐个比较相同：
        # 取列表里最靠前的、名字不同而 name_key 或 match_key 相同的那个）
        key = tuple(names)
        idx = self._near_idx.get(key)
        if idx is None:
            idx = {}
            for i, n in enumerate(names):
                nk, mk = self._keys(n)
                for k in (("n", nk), ("m", mk)):
                    hits = idx.setdefault(k, [])
                    if len(hits) < 3:
                        hits.append((i, n))
            self._near_idx[key] = idx
        wk, wm = self._keys(wanted)
        for _, n in sorted(idx.get(("n", wk), []) + idx.get(("m", wm), [])):
            if n != wanted:
                return f"（有「{n}」，名字必须逐字相同）"
        return ""

    # ---- 主流程 ----

    def run(self) -> list[RecipeProblem]:
        self.check_mode()
        self.check_ids()
        self.writers = self.build_writers()
        self.cols, shape = derive_tables(self.r)
        for p in shape:
            if (p.path, p.code) not in self._seen:
                self._seen.add((p.path, p.code))
                self.problems.append(p)
            origin = p.path.removesuffix("/measures")
            self.broken |= {w.table for w in self.writers if w.origin == origin}
        self.check_names()
        self.check_tables()
        self.check_context()
        self.check_locate()
        self.check_labels()
        self.check_const()
        self.check_placeholders()
        self.check_units()
        self.check_grain()
        self.check_list_refs()
        self.check_ignores()
        self.check_relations()
        if self.facts is not None:
            self.check_facts()
        self.check_notes()
        return self.problems

    # ---- 7. 格式版本、导入模式 ----

    def check_mode(self) -> None:
        # 期 3：按期累积按资格判（契约 accumulate_blockers，起草器、累积计划共用同一份）；mode_unsupported 不再产生
        if self.r.mode != "accumulate":
            return
        for p in accumulate_blockers(self.r):
            if (p.path, p.code) not in self._seen:
                self._seen.add((p.path, p.code))
                self.problems.append(p)

    # ---- 8. 工作表、块、分段的键唯一 ----

    def check_ids(self) -> None:
        sheet_ids: dict[str, int] = {}
        sheet_names: dict[str, int] = {}
        for si, sheet in enumerate(self.r.sheets):
            if sheet.id in sheet_ids:
                self.add(("sheets", si, "id"), "sheet_id_duplicate",
                         f"工作表编号「{sheet.id}」与第 {sheet_ids[sheet.id] + 1} 个工作表重复")
            sheet_ids.setdefault(sheet.id, si)
            key = match_key(sheet.match.name)
            if key in sheet_names:
                self.add(("sheets", si, "match", "name"), "sheet_name_duplicate",
                         f"与第 {sheet_names[key] + 1} 个工作表对应的是同一张工作表「{sheet.match.name}」")
            sheet_names.setdefault(key, si)
            crosstabs = [bi for bi, b in enumerate(sheet.blocks) if isinstance(b, CrosstabBlock)]
            for bi in crosstabs[1:]:
                self.add(("sheets", si, "blocks", bi), "crosstab_twice", "一个工作表最多只能有一个交叉表块")
        # 分段键和块键都在整份配方内唯一；两者同在 Extraction.block_order 里排先后，所以也不能彼此重名
        seg_ids: dict[str, tuple[Any, ...]] = {}
        for si, bi, gi, _block, seg in self.segs:
            parts = self.seg_parts(si, bi, gi)
            if seg.id in seg_ids:
                self.add(parts + ("id",), "segment_id_duplicate",
                         f"分段名「{seg.id}」在配方里重复：分段名在整份配方内必须唯一")
            seg_ids.setdefault(seg.id, parts)
        block_ids: set[str] = set()
        for si, sheet in enumerate(self.r.sheets):
            for bi, block in enumerate(sheet.blocks):
                parts = ("sheets", si, "blocks", bi, "id")
                if block.id in block_ids:
                    self.add(parts, "block_id_duplicate", f"块名「{block.id}」在配方里重复：块名在整份配方内必须唯一")
                elif block.id in seg_ids:
                    self.add(parts, "block_id_duplicate",
                             f"块名「{block.id}」与一个分段同名：分段名和块名在整份配方内不能重复")
                block_ids.add(block.id)

    # ---- 写入各表的位置与列 ----

    def build_writers(self) -> list[_Writer]:
        out: list[_Writer] = []
        for si, bi, gi, block, seg in self.segs:
            base = ("sheets", si, "blocks", bi)
            sp = self.seg_parts(si, bi, gi)
            axis = (block.axis.name, base + ("axis", "name"))
            if isinstance(seg, MeasuresSegment):
                cols = [axis] + [(seg.measures[label], sp + ("measures", label))
                                 for label in seg.labels.expect if label in seg.measures]
                out.append(_Writer(seg.table, sp + ("table",), "data", cols, block.axis.name, _pointer(sp)))
            elif isinstance(seg, DimensionSegment):
                cols = [axis, (seg.dim.name, sp + ("dim", "name"))]
                cols += [(k, sp + ("const", k)) for k in sorted(seg.const)]
                cols += [(k, sp + ("dim", "derive", k)) for k, _ in _derive_order(seg.dim.derive)]
                cols.append((seg.value, sp + ("value",)))
                out.append(_Writer(seg.table, sp + ("table",), "data", cols, block.axis.name, _pointer(sp)))
            elif isinstance(seg, DerivedSegment) and seg.keep_as is not None:
                k = seg.keep_as
                kp = sp + ("keep_as",)
                cols = [axis, (k.dim, kp + ("dim",))]
                cols += [(n, kp + ("derive", n)) for n, _ in _derive_order(k.derive)]
                cols.append((k.value, kp + ("value",)))
                out.append(_Writer(k.table, kp + ("table",), "reported_total", cols, block.axis.name, _pointer(sp)))
        for si, bi, block in self.lists:
            base = ("sheets", si, "blocks", bi)
            cols = [(c.name, base + ("columns", n, "name")) for n, c in enumerate(block.columns)]
            out.append(_Writer(block.table, base + ("table",), "data", cols, origin=_pointer(base)))
            total = block.rows.total_row
            if total is not None and total.keep_as:
                tp = base + ("rows", "total_row", "keep_as")
                tcols = [(LIST_TOTAL_DIM, tp)]
                tcols += [(c.name, base + ("columns", n, "name"))
                          for n, c in enumerate(block.columns) if c.type in _NUMERIC]
                out.append(_Writer(total.keep_as, tp, "reported_total", tcols,
                                   origin=_pointer(base + ("rows", "total_row"))))
        return out

    # ---- 8. 名字 ----

    def check_names(self) -> None:
        seen: dict[str, str] = {}
        for i, t in enumerate(self.r.tables):
            why = name_problem(t.name)
            if why:
                self.add(("tables", i, "name"), "name_invalid", why)
            key = collide_key(t.name)
            if key in seen:
                self.add(("tables", i, "name"), "name_collision",
                         f"与表「{seen[key]}」重名（名字不区分大小写和全角半角）")
            seen.setdefault(key, t.name)
        for w in self.writers:
            names: dict[str, str] = {}
            for name, parts in w.columns:
                if not name:
                    continue      # measures 对不上标签的空列名：derive_tables 已报 measures_keys_mismatch
                why = name_problem(name)
                if why:
                    self.add(parts, "name_invalid", f"列名有问题：{why}")
                key = collide_key(name)
                if key in names:
                    self.add(parts, "name_collision",
                             f"表「{w.table}」里列名「{name}」与「{names[key]}」重名（名字不区分大小写和全角半角）")
                names.setdefault(key, name)

    # ---- 9. 表的引用、10. 表的种类 ----

    def check_tables(self) -> None:
        declared = list(self.declared)
        written: dict[str, set[str]] = {}
        for w in self.writers:
            if w.table not in self.declared:
                self.add(w.table_parts, "table_unknown",
                         f"表「{w.table}」不在表清单里{self._near(w.table, declared)}")
            written.setdefault(w.table, set()).add(w.kind)
        for i, t in enumerate(self.r.tables):
            kinds = written.get(t.name)
            if kinds is None:
                if self.declared.get(t.name) == i:
                    self.add(("tables", i), "table_unused", "没有任何分段或列表写入这张表")
                continue
            if len(kinds) == 1 and t.kind not in kinds:
                want = "原表合计（另存的合计行）" if "reported_total" in kinds else "数据"
                self.add(("tables", i, "kind"), "table_kind_mismatch",
                         f"这张表写入的是{want}，表的类型要与之一致")
            elif len(kinds) > 1:
                self.add(("tables", i, "kind"), "table_kind_mismatch", "同一张表不能既存数据又存另存的合计行")

    # ---- 统计期：覆盖断言和补年份都要求同一工作表配了统计期 ----

    def check_context(self) -> None:
        for si, sheet in enumerate(self.r.sheets):
            for ci, ctx in enumerate(sheet.context):
                if ctx.prefer_prefix and any(ch.isdigit() for ch in ctx.prefer_prefix):
                    self.add(("sheets", si, "context", ci, "prefer_prefix"), "period_literal",
                             f"优先展示的开头文字「{ctx.prefer_prefix}」不能含数字（配方里不能写死年份或期次）")
            if sheet.context:
                continue
            for bi, block in enumerate(sheet.blocks):
                if not isinstance(block, CrosstabBlock):
                    continue
                ap = ("sheets", si, "blocks", bi, "axis")
                if "covers_context" in block.axis.checks:
                    self.add(ap + ("checks",), "covers_without_context",
                             "要求日期恰好覆盖统计期，但这个工作表没有配置统计期")
                if block.axis.year_from is not None:
                    self.add(ap + ("year_from",), "covers_without_context",
                             "日期的年份取自统计期，但这个工作表没有配置统计期")

    # ---- 10. 定位与合计分段 ----

    def check_locate(self) -> None:
        after_seen: dict[tuple[int, int, str], str] = {}
        by_block: dict[tuple[int, int], dict[str, Any]] = {}
        for si, bi, _gi, _block, seg in self.segs:
            by_block.setdefault((si, bi), {}).setdefault(seg.id, seg)
        for si, bi, gi, _block, seg in self.segs:
            sp = self.seg_parts(si, bi, gi)
            lp = sp + ("locate",)
            loc = seg.locate
            if isinstance(seg, DerivedSegment):
                if loc.by != "after":
                    self.add(lp + ("by",), "derived_after_invalid", "合计分段只能按「紧跟在另一个分段之后」定位")
                    continue
                if not loc.segment:
                    self.add(lp + ("segment",), "derived_after_invalid", "合计分段要写明紧跟在哪个分段之后")
                    continue
                target = by_block[(si, bi)].get(loc.segment)
                if target is None:
                    self.add(lp + ("segment",), "derived_after_invalid",
                             f"紧跟的分段「{loc.segment}」不在同一个交叉表块里")
                    continue
                if not isinstance(target, DimensionSegment):
                    self.add(lp + ("segment",), "derived_after_invalid",
                             f"紧跟的分段「{loc.segment}」不是按维度展开的分段，合计行无法按时段区间核对")
                    continue
                if target.stop_parser != seg.labels_parser:
                    self.add(lp + ("segment",), "derived_after_invalid",
                             f"分段「{target.id}」没有设置遇到合计行就结束（或结束方式与合计分段的标签解析方式不同），"
                             "合计行会被当成时段明细")
                key = (si, bi, loc.segment)
                if key in after_seen:
                    self.add(lp + ("segment",), "derived_after_invalid",
                             f"分段「{after_seen[key]}」已经紧跟在「{loc.segment}」之后")
                after_seen.setdefault(key, seg.id)
                if loc.title is not None:
                    self.add(lp + ("title",), "derived_after_invalid", "合计分段按位置定位，不写分段标题")
                self.check_verify(sp, seg, target)
                continue
            if loc.by == "after":
                self.add(lp + ("by",), "locate_invalid", "只有合计分段才能按「紧跟在另一个分段之后」定位")
            elif loc.by == "section_title" and not loc.title:
                self.add(lp + ("title",), "locate_invalid", "按分段标题定位时必须写分段标题")
            if loc.by != "section_title" and loc.title is not None:
                self.add(lp + ("title",), "locate_invalid", "只有按分段标题定位时才写分段标题")
            if loc.segment is not None:
                self.add(lp + ("segment",), "locate_invalid", "只有合计分段才写紧跟的分段")
            if isinstance(seg, DimensionSegment):
                self.check_derive(sp + ("dim", "derive"), seg.dim.derive)
                if seg.dim.parser != "hour_range" and seg.dim.derive:
                    self.add(sp + ("dim", "derive"), "derive_invalid", "只有按时段解析的维度才能派生起止小时")

    def check_derive(self, parts: tuple[Any, ...], derive: dict[str, str]) -> None:
        roles = list(derive.values())
        for role in ("start", "end"):
            if roles.count(role) > 1:
                names = "、".join(f"「{k}」" for k, v in derive.items() if v == role)
                word = "起始小时" if role == "start" else "结束小时"
                self.add(parts, "derive_role_duplicate", f"派生列{names}都取{word}，同一种只能派生一列")

    def check_verify(self, sp: tuple[Any, ...], seg: DerivedSegment, target: DimensionSegment) -> None:
        v = seg.verify
        vp = sp + ("verify",)
        if seg.keep_as is not None:
            self.check_derive(sp + ("keep_as", "derive"), seg.keep_as.derive)
        if v.against_table not in self.declared and v.against_table not in self.cols:
            self.add(vp + ("against_table",), "table_unknown",
                     f"核对的基表「{v.against_table}」不在表清单里{self._near(v.against_table, list(self.declared))}")
            return
        if v.against_table != target.table:
            self.add(vp + ("against_table",), "verify_against_invalid",
                     f"核对的基表必须是紧邻的分段「{target.id}」写入的表「{target.table}」")
            return
        if target.dim.parser != "hour_range" or sorted(target.dim.derive.values()) != ["end", "start"]:
            self.add(vp + ("against_table",), "verify_against_invalid",
                     f"按标签区间核对要求基表「{v.against_table}」按时段解析、并派生起始小时和结束小时")
        col = self.col(v.against_table, v.value)
        if col is None and v.against_table in self.broken:
            return
        if col is None:
            self.add(vp + ("value",), "column_unknown",
                     f"基表「{v.against_table}」里没有列「{v.value}」{self._near(v.value, self.col_names(v.against_table))}")
        elif col.role != "value":
            self.add(vp + ("value",), "verify_against_invalid",
                     f"核对要加总的是基表的值列「{target.value}」，不是「{v.value}」")

    # ---- 标签：期望的标签按该段的比较方式不能重复、必须解析得出 ----

    def check_labels(self) -> None:
        for si, bi, gi, _block, seg in self.segs:
            sp = self.seg_parts(si, bi, gi)
            seen: dict[str, str] = {}
            for n, label in enumerate(seg.labels.expect):
                key = self.label_key(seg, label)
                lp = sp + ("labels", "expect", n)
                if key is None:
                    how = "时段（如「7-8」）" if isinstance(seg, DimensionSegment) else "带区间的合计（如「18-22时合计」）"
                    if isinstance(seg, DimensionSegment) and seg.dim.parser == "text":
                        self.add(lp, "label_unparsed", f"标签「{label}」为空或超过 {LABEL_MAX} 字")
                    else:
                        self.add(lp, "label_unparsed", f"标签「{label}」无法解析为{how}")
                    continue
                if key in seen:
                    self.add(lp, "label_duplicate", f"标签「{label}」与「{seen[key]}」是同一个标签")
                seen.setdefault(key, label)

    @staticmethod
    def label_key(seg: Any, label: str) -> str | None:
        if isinstance(seg, MeasuresSegment):
            return match_key(label)
        if isinstance(seg, DimensionSegment):
            if seg.dim.parser == "hour_range":
                h = hour_range(label)
                return h.canonical if h else None
            return text_label(label)
        t = hour_range_total(label)
        return t.canonical if t else None

    # ---- 2、6. 常量只能 pick 分段标题的候选词，不能含数字 ----

    def check_const(self) -> None:
        titles: dict[str, list[tuple[str, str]]] = {}
        for _si, _bi, _gi, _block, seg in self.segs:
            if isinstance(seg, DimensionSegment) and seg.locate.by == "section_title" and seg.locate.title:
                titles.setdefault(seg.table, []).append((seg.id, seg.locate.title))
        for si, bi, gi, _block, seg in self.segs:
            if not isinstance(seg, DimensionSegment) or not seg.const:
                continue
            sp = self.seg_parts(si, bi, gi)
            title = seg.locate.title if seg.locate.by == "section_title" else None
            siblings = [t for sid, t in titles.get(seg.table, []) if sid != seg.id]
            cands = candidate_words(title, siblings) if title else []
            for name, c in seg.const.items():
                cp = sp + ("const", name, "pick")
                if any(ch.isdigit() for ch in c.pick):
                    self.add(cp, "period_literal",
                             f"常量「{name}」的取值「{c.pick}」不能含数字（配方里不能写死年份或期次）")
                elif not title:
                    self.add(cp, "const_not_candidate",
                             f"常量「{name}」只能从分段标题里选词，但这个分段没有按分段标题定位")
                elif c.pick not in cands and not self.const_carried(seg.id, name, c.pick, title):
                    self.add(cp, "const_not_candidate",
                             f"常量「{name}」的取值「{c.pick}」不在分段标题「{title}」的候选词中，"
                             f"可选：{'、'.join(cands) if cands else '（无）'}")

    def const_carried(self, seg_id: str, name: str, pick: str, title: str) -> bool:
        """3.3 的 (b)(c)：不在新标题的候选词里，但仍可以沿用。两条都要求 canon(pick) 是 canon(新标题) 的子串。

        (b) 沿用：现行配方里同一分段（同 id）同一常量列的 pick 与它逐字相同。pick 当初是从确认过的标题的候选词里
            选的，现在仍然逐字出现在新标题里，常量列的值不变，不是新编的文字（D09「日间分时段客流」沿用「日间」）；
        (c) 重放：修复后的配方下个月按 replay 校验、又没有 base，执行时 const_missing 本来就只查子串。
        不放宽的话 D09 只能改 pick，常量列整列换值，是破坏性变更，累积模式下还要重新开始累积。"""
        if canon(pick) not in canon(title):
            return False
        if self.origin == "replay":
            return True
        if self.base is None:
            return False
        for sheet in self.base.sheets:
            for block in sheet.blocks:
                if not isinstance(block, CrosstabBlock):
                    continue
                for seg in block.segments:
                    if isinstance(seg, DimensionSegment) and seg.id == seg_id and name in seg.const:
                        return seg.const[name].pick == pick
        return False

    # ---- 3. 占位符只能映射空值，不能像数 ----

    def check_placeholders(self) -> None:
        for si, sheet in enumerate(self.r.sheets):
            for bi, block in enumerate(sheet.blocks):
                for n, ph in enumerate(block.values.placeholders):
                    if looks_numeric_text(ph.text):
                        self.add(("sheets", si, "blocks", bi, "values", "placeholders", n, "text"),
                                 "placeholder_numeric",
                                 f"占位符「{ph.text}」看起来是一个数，不能当作占位符（占位符一律存为空值）")
        for si, bi, block in self.lists:
            total = block.rows.total_row
            if total is not None and any(ch.isdigit() for ch in total.pick):
                self.add(("sheets", si, "blocks", bi, "rows", "total_row", "pick"), "period_literal",
                         f"合计行的开头词「{total.pick}」不能含数字（配方里不能写死年份或期次）")

    # ---- 4、9、13. 单位：键是本表列名，值在词表里，与原表标签、表头里的单位一致 ----

    def check_units(self) -> None:
        for i, t in enumerate(self.r.tables):
            names = self.col_names(t.name)
            for col, unit in t.units.items():
                up = ("tables", i, "units", col)
                # 没人写入的表（已报 table_unused）没有列可比，不再逐个报列不存在
                if names and col not in names and t.name not in self.broken:
                    self.add(up, "column_unknown", f"单位对应的列「{col}」不在这张表里{self._near(col, names)}")
                if unit not in UNITS:
                    self.add(up, "unit_unknown", f"列「{col}」的单位「{unit}」不在单位词表中")
        # 原表里写着单位的地方：标签（指标列）、表头（列表）、分段标题（长表的值列）。
        # 另存的合计表（keep_as）也要查：它的数字列和原表同出一处（列表合计表的列就是那几个表头，交叉表的
        # 表内合计与紧跟的明细同在一个分段标题下），不查的话合计表就成了能把「万元」静默写成「元」的口子
        for si, bi, _gi, _block, seg in self.segs:
            if isinstance(seg, MeasuresSegment):
                for label in seg.labels.expect:
                    if label in seg.measures:
                        self.unit_against(seg.table, seg.measures[label], label, "标签")
            elif isinstance(seg, DimensionSegment) and seg.locate.by == "section_title" and seg.locate.title:
                self.unit_against(seg.table, seg.value, seg.locate.title, "分段标题")
            elif isinstance(seg, DerivedSegment) and seg.keep_as is not None:
                target = self.after_target(si, bi, seg)
                if target is not None and target.locate.by == "section_title" and target.locate.title:
                    self.unit_against(seg.keep_as.table, seg.keep_as.value, target.locate.title, "分段标题")
        for _si, _bi, block in self.lists:
            total = block.rows.total_row
            for c in block.columns:
                self.unit_against(block.table, c.name, c.header, "表头")
                if total is not None and total.keep_as and c.type in _NUMERIC:
                    self.unit_against(total.keep_as, c.name, c.header, "表头")

    def after_target(self, si: int, bi: int, seg: DerivedSegment) -> DimensionSegment | None:
        """合计分段紧跟的那个 dimension 分段（同一块里按 id 找）；找不到或不是 dimension 段时已报 derived_after_invalid。"""
        return next((s for sj, bj, _gi, _block, s in self.segs
                     if (sj, bj) == (si, bi) and isinstance(s, DimensionSegment) and s.id == seg.locate.segment), None)

    def unit_against(self, table: str, column: str, source: str, what: str) -> None:
        i = self.declared.get(table)
        if i is None or not column or (table in self.broken and column not in self.col_names(table)):
            return
        unit = split_unit_suffix(source)[1]
        if unit is None:
            return
        declared = self.r.tables[i].units.get(column)
        parts = ("tables", i, "units", column) if declared is not None else ("tables", i, "units")
        if unit in UNITS:
            if declared is None:
                self.add(parts, "unit_label_conflict",
                         f"原表{what}「{source}」写着单位「{unit}」，列「{column}」的单位也必须写「{unit}」")
            elif declared != unit:
                self.add(parts, "unit_label_conflict",
                         f"列「{column}」的单位写的是「{declared}」，原表{what}「{source}」写的是「{unit}」："
                         f"配方不做单位换算，请改成「{unit}」")
        elif declared is not None:
            self.add(parts, "unit_label_conflict",
                     f"原表{what}「{source}」的单位「{unit}」不在单位词表中，列「{column}」不能写单位")

    # ---- 9、10. 主键 ----

    def check_grain(self) -> None:
        axis_of: dict[str, str] = {}
        for w in self.writers:
            if w.axis is not None:
                axis_of.setdefault(w.table, w.axis)
        dims: dict[str, list[DimensionSegment]] = {}
        for _si, _bi, _gi, _block, seg in self.segs:
            if isinstance(seg, DimensionSegment):
                dims.setdefault(seg.table, []).append(seg)
        for i, t in enumerate(self.r.tables):
            gp = ("tables", i, "grain")
            names = self.col_names(t.name)
            seen: set[str] = set()
            for n, g in enumerate(t.grain):
                if g not in names and names and t.name not in self.broken:
                    self.add(gp + (n,), "column_unknown", f"主键列「{g}」不在这张表里{self._near(g, names)}")
                if g in seen:
                    self.add(gp + (n,), "grain_invalid", f"主键列「{g}」写了两次")
                seen.add(g)
            if self.declared.get(t.name) != i:
                continue
            axis = axis_of.get(t.name)
            if axis is not None and axis not in t.grain:
                self.add(gp, "grain_invalid", f"交叉表产出的表，主键必须含日期列「{axis}」")
            segs = dims.get(t.name, [])
            for a in range(len(segs)):
                for b in range(a + 1, len(segs)):
                    sa, sb = segs[a], segs[b]
                    common = self.dim_keys(sa) & self.dim_keys(sb)
                    if not common:
                        continue
                    if any(g in sa.const and g in sb.const and canon(sa.const[g].pick) != canon(sb.const[g].pick)
                           for g in t.grain):
                        continue
                    ex = "、".join(f"「{x}」" for x in sorted(common)[:3])
                    self.add(gp, "grain_invalid",
                             f"分段「{sa.id}」和「{sb.id}」写进同一张表，标签有重复（{ex}），"
                             "主键必须含能区分它们的常量列")

    @staticmethod
    def dim_keys(seg: DimensionSegment) -> set[str]:
        out: set[str] = set()
        for label in seg.labels.expect:
            if seg.dim.parser == "hour_range":
                h = hour_range(label)
                if h:
                    out.add(h.canonical)
            else:
                k = text_label(label)
                if k:
                    out.add(k)
        return out

    # ---- 9. 列表：合计标签列、表头不重复 ----

    def check_list_refs(self) -> None:
        for si, bi, block in self.lists:
            base = ("sheets", si, "blocks", bi)
            names = [c.name for c in block.columns]
            total = block.rows.total_row
            if total is not None and total.label_column not in names:
                self.add(base + ("rows", "total_row", "label_column"), "column_unknown",
                         f"合计标签所在的列「{total.label_column}」不在这个列表的列里{self._near(total.label_column, names)}")
            seen: dict[str, str] = {}
            for n, c in enumerate(block.columns):
                key = match_key(c.header)
                if key in seen:
                    self.add(base + ("columns", n, "header"), "label_duplicate",
                             f"表头「{c.header}」与「{seen[key]}」是同一个表头")
                seen.setdefault(key, c.header)

    # ---- 期 3：忽略规则、表头上方的标题（P3-SPEC 9.2） ----

    def check_ignores(self) -> None:
        """忽略规则的三条：ignore_conflict（同一行、同一列既导入又忽略；交叉表忽略的表头是日期）、ignore_duplicate
        （同一列表里写了两遍），以及 period_literal 的扩展范围。

        period_literal 只在 origin 不是 replay 时报：期 2 已有的配方按重放校验时不受影响；人改配方、用修复按钮或
        框选时才拦。锚点文字含数字（「注：9月补录」「2026年8月 销售月报」）时，这条规则写进配方以后下一期必然
        对不上，也违背「配方里不写统计期的值」的原则（评审一-M11、二-m11）。"""
        literal = self.origin != "replay"
        for si, sheet in enumerate(self.r.sheets):
            sp = ("sheets", si)
            self.unique_anchors(sp + ("ignore_outside",), [x.anchor for x in sheet.ignore_outside], "anchor")
            if literal:
                for n, x in enumerate(sheet.ignore_outside):
                    if any(ch.isdigit() for ch in x.anchor):
                        self.add(sp + ("ignore_outside", n, "anchor"), "period_literal",
                                 f"按同一行文字忽略数字时用的「{x.anchor}」含数字，下一期很可能对不上"
                                 "（配方里不能写死年份或期次）")
            for bi, block in enumerate(sheet.blocks):
                bp = sp + ("blocks", bi)
                self.unique_anchors(bp + ("ignore_columns",), [x.header for x in block.ignore_columns], "header")
                if literal:
                    for n, x in enumerate(block.ignore_columns):
                        if any(ch.isdigit() for ch in x.header):
                            self.add(bp + ("ignore_columns", n, "header"), "period_literal",
                                     f"按表头忽略的列「{x.header}」含数字，下一期很可能对不上（配方里不能写死年份或期次）")
                if isinstance(block, CrosstabBlock):
                    self.unique_anchors(bp + ("ignore_rows",), [x.label for x in block.ignore_rows], "label")
                    for n, x in enumerate(block.ignore_rows):
                        xp = bp + ("ignore_rows", n, "label")
                        if literal and any(ch.isdigit() for ch in x.label):
                            self.add(xp, "period_literal",
                                     f"按行标签忽略的「{x.label}」含数字，下一期很可能对不上（配方里不能写死年份或期次）")
                        owner = self.expecting(block, x.label)
                        if owner is not None:
                            self.add(xp, "ignore_conflict",
                                     f"按行标签忽略的「{x.label}」同时是分段「{owner}」期望的标签：同一行不能既导入又忽略")
                    for n, x in enumerate(block.ignore_columns):
                        if month_day_or_date(x.header) is not None:
                            self.add(bp + ("ignore_columns", n, "header"), "ignore_conflict",
                                     f"按表头忽略的列「{x.header}」是一个日期：交叉表只能忽略日期表头右侧的列")
                    continue
                if literal and block.after_title and any(ch.isdigit() for ch in block.after_title):
                    self.add(bp + ("after_title",), "period_literal",
                             f"表头上方的标题「{block.after_title}」含数字，下一期很可能对不上：配方里不能写死年份或期次，"
                             "请换一行不含数字的文字，或去掉它")
                heads = {match_key(c.header): c.header for c in block.columns}
                for n, x in enumerate(block.ignore_columns):
                    hit = heads.get(match_key(x.header))
                    if hit is not None:
                        self.add(bp + ("ignore_columns", n, "header"), "ignore_conflict",
                                 f"按表头忽略的列「{x.header}」与要导入的列「{hit}」是同一个表头：同一列不能既导入又忽略")

    def unique_anchors(self, parts: tuple[Any, ...], texts: list[str], field: str) -> None:
        seen: dict[str, str] = {}
        for n, t in enumerate(texts):
            k = match_key(t)
            if k in seen:
                self.add(parts + (n, field), "ignore_duplicate", f"「{t}」与「{seen[k]}」是同一段文字，写了两遍")
            seen.setdefault(k, t)

    def expecting(self, block: CrosstabBlock, label: str) -> str | None:
        """块里期望这个标签的分段 id（按 match_key，或按该段的解析方式得到同一个规范写法）；没有为 None。"""
        key = match_key(label)
        for seg in block.segments:
            own = self.label_key(seg, label)
            for e in seg.labels.expect:
                if match_key(e) == key or (own is not None and self.label_key(seg, e) == own):
                    return seg.id
        return None

    # ---- 9. 关系 ----

    def check_relations(self) -> None:
        ids: set[str] = set()
        for i, rel in enumerate(self.r.relations):
            rp = ("relations", i)
            if rel.id in ids:
                self.add(rp + ("id",), "relation_invalid", f"关系编号「{rel.id}」重复")
            ids.add(rel.id)
            if isinstance(rel, SumEq):
                if not self.table_ref(rp + ("table",), rel.table):
                    continue
                cols = [(rp + ("total",), rel.total)] + [(rp + ("parts", n), p) for n, p in enumerate(rel.parts)]
                for parts, name in cols:
                    self.numeric_col(parts, rel.table, name)
                if rel.total in rel.parts:
                    self.add(rp + ("total",), "relation_invalid", f"合计列「{rel.total}」不能同时是组成列")
                if len(set(rel.parts)) != len(rel.parts):
                    self.add(rp + ("parts",), "relation_invalid", "组成列有重复")
            elif isinstance(rel, NotComparable):
                ok_a = self.table_ref(rp + ("a", "table"), rel.a.table)
                ok_b = self.table_ref(rp + ("b", "table"), rel.b.table)
                if ok_a:
                    self.numeric_col(rp + ("a", "value"), rel.a.table, rel.a.value)
                if ok_b:
                    self.numeric_col(rp + ("b", "value"), rel.b.table, rel.b.value)
                for side, ok in ((rel.a.table, ok_a), (rel.b.table, ok_b)):
                    if ok and self.col(side, rel.by) is None and side not in self.broken:
                        self.add(rp + ("by",), "column_unknown",
                                 f"分组列「{rel.by}」不在表「{side}」里{self._near(rel.by, self.col_names(side))}")
                if ok_a and ok_b and rel.a.table == rel.b.table:
                    self.add(rp + ("b", "table"), "relation_invalid", "口径不同的两方必须是两张不同的表")

    def table_ref(self, parts: tuple[Any, ...], table: str) -> bool:
        if table in self.cols and table in self.declared:
            return True
        self.add(parts, "table_unknown", f"表「{table}」不在配方产出的表里{self._near(table, list(self.declared))}")
        return False

    def numeric_col(self, parts: tuple[Any, ...], table: str, name: str) -> None:
        col = self.col(table, name)
        if col is None and table in self.broken:
            return
        if col is None:
            self.add(parts, "column_unknown", f"表「{table}」里没有列「{name}」{self._near(name, self.col_names(table))}")
        elif col.type not in _NUMERIC:
            self.add(parts, "relation_invalid", f"列「{name}」不是数字列，不能参与求和核对")

    # ---- 11. 认领 ----

    def check_facts(self) -> None:
        assert self.facts is not None
        facts = {f.id: f for f in self.facts.facts}
        scope = self.changed_scope()
        # scope 为 None：首次导入、从简单导入切换，照期 2 全查。否则只查改动过的关系、成员落在改动过的
        # measures 段上的关系，以及落在改动过的 measures 段上的事实（3.3 第 11 条，评审一-M3）
        rels = set(range(len(self.r.relations))) if scope is None else self.relations_in(scope)
        claimers: dict[str, list[int]] = {}
        for i, rel in enumerate(self.r.relations):
            if rel.claims is None:
                continue
            if rel.claims not in facts:
                if i in rels:
                    self.add(("relations", i, "claims"), "fact_claim_mismatch",
                             f"认领的系统发现「{rel.claims}」不存在：请去掉认领，或按本次发现重新登记")
                continue
            claimers.setdefault(rel.claims, []).append(i)
        for fid, fact in facts.items():
            got = claimers.get(fid, [])
            in_scope = scope is None or self.fact_in(fact, scope)
            if not got and in_scope:
                self.add(("relations",), "fact_unclaimed",
                         f"系统发现的关系「{_fact_brief(fact)}」没有被认领：请登记为每期核对，或说明不登记的理由",
                         where=False)
            for j in got[1:]:
                if in_scope or j in rels:
                    self.add(("relations", j, "claims"), "fact_claimed_twice",
                             f"系统发现的关系「{_fact_brief(fact)}」已经由关系「{self.r.relations[got[0]].id}」认领，"
                             "每条只能认领一次")
            for j in got:
                if j not in rels:
                    continue
                why = self.claim_mismatch(self.r.relations[j], fact)
                if why:
                    self.add(("relations", j, "claims"), "fact_claim_mismatch",
                             f"认领了系统发现的关系「{_fact_brief(fact)}」，但{why}")

    def changed_scope(self) -> tuple[set[str], set[str]] | None:
        """与现行配方（base）相比改动过的 (measures 分段 id, 关系 id)；base 为空时 None（全查）。

        measures 段：同 id 的段 labels.expect 集合、measures 映射、locate 任一不同，或者是新段。
        关系：同 id 的关系任一字段不同，或者是新关系。
        理由：用过修复按钮以后工作配方的哈希变了，静态校验改成带本期系统发现。本期 R1 有某天不成立时 _sum_holds
        不出 sum_eq 事实，没改过的 R1 就会报「认领的系统发现不存在」，D4「每期写理由接受」变成永久的配方改动。
        没改过的部分按重放处理：R1 本期成不成立交给试运行的 R 核对（数据质量类，可以写理由接受）。"""
        if self.base is None:
            return None

        def measures(r: Recipe) -> dict[str, MeasuresSegment]:
            return {s.id: s for s in _measures_segments(r)}

        old = measures(self.base)
        segs: set[str] = set()
        for sid, seg in measures(self.r).items():
            o = old.get(sid)
            if (o is None or set(o.labels.expect) != set(seg.labels.expect) or o.measures != seg.measures
                    or o.locate != seg.locate):
                segs.add(sid)
        old_rels = {r.id: r.model_dump(mode="json") for r in self.base.relations}
        rels = {r.id for r in self.r.relations if old_rels.get(r.id) != r.model_dump(mode="json")}
        return segs, rels

    def relations_in(self, scope: tuple[set[str], set[str]]) -> set[int]:
        """要查认领的关系下标：改动过的，以及成员（表 + 列）落在改动过的 measures 段写入的列上的。"""
        segs, rels = scope
        cols = {(s.table, c) for *_rest, s in self.segs if isinstance(s, MeasuresSegment) and s.id in segs
                for c in s.measures.values()}
        out: set[int] = set()
        for i, rel in enumerate(self.r.relations):
            if rel.id in rels:
                out.add(i)
            elif isinstance(rel, SumEq):
                if any((rel.table, c) in cols for c in (rel.total, *rel.parts)):
                    out.add(i)
            elif isinstance(rel, NotComparable):
                if (rel.a.table, rel.a.value) in cols or (rel.b.table, rel.b.value) in cols:
                    out.add(i)
        return out

    def fact_in(self, fact: Fact, scope: tuple[set[str], set[str]]) -> bool:
        """事实是否落在改动过的 measures 段上。

        先在工作配方里按 id、按全部标签认段（measures_seg）。认不出时多半是这一段刚被改过：① 去掉了「分区乙」，
        本期的事实「全日客流 = 分区甲 + 分区乙」还在，分段 id 又是规则起草的写法（remap_facts 要全部标签对上才
        换 id），按 id、按全部标签都认不出。只到这一步就判「不在范围内」的话，这条事实连同认领它的关系被删掉以后，
        既不报未认领、也没有理由，数据质量核对不声不响地消失（3.3 第 11 条要查的正是这种改动）。所以再按两步认：
        1. 在现行配方（base）里按 id、按全部标签认段：认出的段在工作配方里改动过或已删掉，就在范围内；
        2. 在工作配方里按「过半标签命中」认段（命中最多且唯一）。
        两步都只会把事实多算进范围（多查一条认领），不会漏查。"""
        d = fact.detail or {}
        if fact.kind == "sum_eq":
            total, parts = d.get("total"), d.get("parts")
            labels = [total, *parts] if isinstance(total, str) and isinstance(parts, list) else []
            seg_id = d.get("segment")
        else:
            b = d.get("b")
            labels, seg_id = ([b] if isinstance(b, str) else []), d.get("b_segment")
        labels = [x for x in labels if isinstance(x, str)]
        seg = self.measures_seg(seg_id, labels)
        if seg is not None:
            return seg.id in scope[0]
        if self.base is not None:
            old = _find_measures(_measures_segments(self.base), seg_id, labels)
            if old is not None:
                return old.id in scope[0] or old.id not in {s.id for s in self.measures_list()}
        seg = _most_labels(self.measures_list(), labels)
        return seg is not None and seg.id in scope[0]

    def measures_list(self) -> list[MeasuresSegment]:
        return [s for *_rest, s in self.segs if isinstance(s, MeasuresSegment)]

    def measures_seg(self, seg_id: Any, labels: list[str]) -> MeasuresSegment | None:
        """Fact 指向的 measures 分段：按 id 找；改过分段名找不到时，按标签找唯一含这些标签的分段。"""
        return _find_measures(self.measures_list(), seg_id, labels)

    def dimension_segs(self, seg_ids: list[str], near: MeasuresSegment | None) -> list[DimensionSegment]:
        """not_equal_sum 的 a 指向的 dimension 分段：按 id 找；有改过名找不到的，取与 b 那个 measures 段同块的全部 dimension 段。

        起草时 a 就是交叉表里的全部 dimension 段（P2-SPEC 6.1 第 10 条），段 id 取自常量的 pick（8b）；用户在向导
        里改了分段名、或 AI 起草时另起了名字，id 就对不上。Fact 只记 a 的分段 id、不记标签，没法像 measures 段那样
        按标签认，所以按「同块的全部 dimension 段」认，并且个数必须与 a 相同、没改名的那几段也必须在其中：
        多了少了说明内容变了，照样报对不上。
        """
        dims = [(si, bi, s) for si, bi, _gi, _block, s in self.segs if isinstance(s, DimensionSegment)]
        wanted = set(seg_ids)
        hit = [s for *_rest, s in dims if s.id in wanted]
        if {s.id for s in hit} == wanted or near is None:
            return hit
        where = next(((si, bi) for si, bi, _gi, _block, s in self.segs if s is near), None)
        same = [s for si, bi, s in dims if (si, bi) == where]
        if len(same) == len(wanted) and all(any(s is t for t in same) for s in hit):
            return same
        return hit

    def claim_mismatch(self, rel: Any, fact: Fact) -> str | None:
        d = fact.detail or {}
        if isinstance(rel, Dismissed):
            return None
        if fact.kind == "sum_eq":
            if not isinstance(rel, SumEq):
                return "这条发现只能登记为每期核对（合计等于各项之和），或说明不登记的理由"
            total, parts = d.get("total"), d.get("parts")
            if not isinstance(total, str) or not isinstance(parts, list):
                return "系统发现缺少标签信息，无法核对认领的内容"
            seg = self.measures_seg(d.get("segment"), [total, *parts])
            if seg is None:
                return f"配方里找不到这条发现所在的分段「{d.get('segment')}」"
            if rel.table != seg.table:
                return f"它在分段「{seg.id}」写入的表「{seg.table}」里，关系写的是表「{rel.table}」"
            back = {col: label for label, col in seg.measures.items()}
            got_total = back.get(rel.total)
            got_parts = [back.get(p) for p in rel.parts]
            ok = (got_total is not None and match_key(got_total) == match_key(total)
                  and None not in got_parts
                  and sorted(match_key(x) for x in got_parts if x is not None) == sorted(match_key(x) for x in parts))
            if not ok:
                want = f"「{total}」=" + "+".join(f"「{x}」" for x in parts)
                have = f"「{got_total or rel.total}」=" + "+".join(f"「{back.get(p) or p}」" for p in rel.parts)
                return f"内容对不上：应为{want}，关系写的是{have}"
            return None
        # not_equal_sum
        if not isinstance(rel, NotComparable):
            return "这条发现只能登记为口径不同，或说明不登记的理由"
        a_ids, b_label = d.get("a"), d.get("b")
        if not isinstance(a_ids, list) or not isinstance(b_label, str):
            return "系统发现缺少分段信息，无法核对认领的内容"
        seg = self.measures_seg(d.get("b_segment"), [b_label])
        a_segs = self.dimension_segs([x for x in a_ids if isinstance(x, str)], seg)
        a_tables = {s.table for s in a_segs}
        if rel.a.table not in a_tables:
            shown = "、".join(f"「{x}」" for x in sorted(a_tables)) or "（配方里找不到这些分段）"
            return f"一方应是分段{'、'.join(f'「{x}」' for x in a_ids)}写入的表{shown}，关系写的是「{rel.a.table}」"
        # 发现说的是这些分段「各行之和」，加总的是值列；换成派生列（起始小时）或常量列，口径就写错了
        a_values = sorted({s.value for s in a_segs if s.table == rel.a.table})
        if rel.a.value not in a_values:
            shown = "、".join(f"「{x}」" for x in a_values)
            return f"一方应是表「{rel.a.table}」的值列{shown}，关系写的是列「{rel.a.value}」"
        if seg is None:
            return f"配方里找不到这条发现所在的分段「{d.get('b_segment')}」"
        back = {col: label for label, col in seg.measures.items()}
        got = back.get(rel.b.value)
        if rel.b.table != seg.table or got is None or match_key(got) != match_key(b_label):
            return f"另一方应是表「{seg.table}」里标签「{b_label}」对应的列，关系写的是表「{rel.b.table}」的列「{rel.b.value}」"
        return None

    # ---- 12. 说明散文 ----

    def check_notes(self) -> None:
        all_tables = {t.name for t in self.r.tables}
        for i, t in enumerate(self.r.tables):
            if not t.note:
                continue
            np_ = ("tables", i, "note")
            if self.origin == "ai":
                self.add(np_, "ai_note_forbidden", "AI 起草的配方不能写说明：说明由系统按核对结果生成")
            known = {"列": set(self.col_names(t.name)), "表": all_tables, "单位": set(UNITS)}
            for p in prose_problems(t.note, known=known):
                code = "note_token_unknown" if p.startswith("说明引用了不存在的") else "note_numbers"
                self.problems.append(RecipeProblem(_pointer(np_), code, f"表「{t.name}」的说明：{p}"))


def _derive_order(derive: dict[str, str]) -> list[tuple[str, str]]:
    # 与 derive_tables 同一个顺序：start 在前、end 在后，同角色按列名
    return sorted(derive.items(), key=lambda kv: (kv[1] != "start", kv[0]))


def validate_recipe(
    data: dict[str, Any] | Recipe, *,
    facts: DraftFacts | None = None,
    origin: Origin = "manual",
    base: Recipe | dict[str, Any] | None = None,
) -> tuple[Recipe | None, list[RecipeProblem]]:
    """静态校验，不抛异常。schema 不过时返回 (None, problems)；语义问题返回 (recipe, problems)。

    facts 给了（首次导入、改配方）才查认领；重放（origin="replay"）不传。origin="ai" 时 note 必须为空。
    base（期 3）是现行配方（数据源当前的 current_recipe_id 对应的那份，canonical 或完整形式都行），上传新一期、
    修改配方时传：常量沿用现行配方里逐字相同的 pick 时放行（3.3 (b)），认领只查改动过的部分（3.3 第 11 条）。
    base 本身不合 schema 时按没给处理（它是已提交过的配方，正常不会出现）。problems 为空才可试运行。
    """
    if isinstance(data, Recipe):
        recipe = data
    else:
        try:
            recipe = parse_recipe(data)
        except RecipeInvalid as exc:
            return None, exc.problems
    base_recipe: Recipe | None = None
    if isinstance(base, Recipe):
        base_recipe = base
    elif isinstance(base, dict):
        try:
            base_recipe = Recipe.model_validate(base)
        except ValidationError:
            base_recipe = None
    return recipe, _Checker(recipe, facts, origin, base_recipe).run()
