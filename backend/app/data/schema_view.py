"""数据源的只读表结构视图：库概览、表目录、表详情（字段、数据目录、剖析事实、连接条件、两跳的连接路径）、大库的检索预筛。

助手查看（app/look/tables.py）、运行时 agent 的 db_schema__（tools/datasource.py）、/api/datasources/{id}/schema?table=
三处共用这一份（look 文档 D6：同一张表只有一种说法，同一套遮罩规矩）。以前三处各拼各的：助手的小库摘要每张表只列
前 6 列，db_schema 和助手又都把遮罩列的目录原样交给模型。

**遮罩规矩。** 遮罩列（data/engine.py 的 masked_columns，按列名设在整个数据源上）只给列名和类型：它的注释、目录里
这一列的各项（含码值、带具体取值的说明）都不给。和表助手（data/catalog_assistant.py）是同一条规矩。代价是运行时
agent 看到的目录少了遮罩列那部分（README 写明）。

**剖析事实**（next-round 遗留 e）：剖析记下的行数估算和依据、各日期列的范围（catalog_profile_jobs.last_facts）在表详情里
成一节（profile_lines），后面跟一句固定提示「只供写查询参考……不能作为回答里的数」——剖析数字是会过期的观测，叙述层无算术靠
这句和裁判守住。遮罩按渲染这一刻的设置重滤（剖析之后才加遮罩的列，范围不出现）；上传表格只在剖析时的快照和这次读的快照
一致时给，不一致写「剖析早于当前版本，不给」。look、运行时 db_schema、/schema、表助手四处都是这一段文字。

**大库的检索预筛**（search_tables，next-round 03 A2）：几千张表的库，挑表时不能把整份表目录交给挑选模型。按需求做一次 BM25：
文档是表名、注释、目录里的中文名 / 粒度 / 说明、列名、列的中文名（遮罩列只用列名）；探查范围外的对象只按名字和注释参与，
权重低一档。切词用 memory/embeddings._tokens（英文按词、中文按字补二元组）加 catalog.name_words（下划线、驼峰拆词）。

**限制：范围外、又没有注释的对象，中文需求命不中。** 它只剩英文名可检索，英文名拆出的词没有中文说法：仓库里没有现成的英中
业务词对照（命名推断只拆词、不翻译，catalog.name_words 也一样），这里不临时编一份词表（编出来的覆盖不全、还会把不相干的表
抬上来），所以「上周的退款」找不到只叫 refund_orders、没写注释的表。真库多半有注释，注释照常参与检索，影响小；范围内的表另有
目录里的中文名、列名可配。要用这张表时按英文名点名查看（会按需加入），或者请管理员把它加进探查范围、补上注释或目录的中文名。

**三种「不在」**（absent_detail）：表已不在、在探查范围外、在范围里却读不到结构，说法分开；只有真删除才说「表已不在」。

只读：只看表结构缓存（schema_cache）和调用方读好的数据目录、剖析事实、对象索引，不碰业务库。这里的文字都是写给模型看的，
不上界面。
"""
from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.data import catalog, introspect
from app.data.catalog import CARDINALITY_LABEL, INFERRED_MARK, JoinEdge
from app.data.engine import masked_columns

#: 一个源最多列多少条连接条件、多少条连接路径
JOIN_LINES_LIMIT = 40
PATH_LINES_LIMIT = 12

# ==========================================================================
# 写给模型的文字
# ==========================================================================

OVERVIEW_LINE_PROMPT = "- {name}（{kind}，{access}）：{description}｜{state}"
OVERVIEW_TABLES_PROMPT = "共 {total} 张表"
OVERVIEW_PARTIAL_PROMPT = "共 {total} 张表，只探查了 {explored} 张"
#: 有对象索引的源（next-round 03 A1）：范围外的表有名字，点名查看时按需加入
OVERVIEW_SCOPED_PROMPT = "共 {total} 张表，探查范围内 {explored} 张（范围外的点名查看时按需加入）"
OVERVIEW_CATALOG_ALL_PROMPT = "都有数据目录"
OVERVIEW_CATALOG_SOME_PROMPT = "{n} 张有数据目录"
OVERVIEW_CATALOG_NONE_PROMPT = "还没有数据目录"
OVERVIEW_MASKED_PROMPT = "设了遮罩"
OVERVIEW_SCHEMAS_PROMPT = "该账号名下没有对象，数据可能在这些 schema：{names}"
MASKED_COLUMN_PROMPT = "已遮罩，只给列名和类型"
BRIEF_PROMPT = "{name}[{kind}]：{columns}（全部 {n} 列，字数超了只给字段名，要看字段说明和目录时点名再 look）"
JOIN_HEAD_PROMPT = "表之间的连接条件（写 JOIN … ON … 时照抄）："
JOIN_MORE_PROMPT = "…另有 {n} 条，点名查看相关的表看各表的关联关系"
PATH_HEAD_PROMPT = "连接路径："
ROUTE_PROMPT = "{a} 与 {b} 没有直接关联，经 {mid} 连接：FROM {a} JOIN {mid} ON {first} JOIN {b} ON {second}"

#: 剖析事实那一节（profile_lines）
PROFILE_HEAD_PROMPT = "数据剖析（{day}）：{facts}"
PROFILE_ROWS_PROMPT = {"stats": "约 {n} 行（数据库统计估算）", "count": "{n} 行（逐行数出）",
                       "at_least": "至少 {n} 行（数到上限为止）"}
PROFILE_RANGE_PROMPT = "{column} 从 {low} 到 {high}"
PROFILE_HINT_PROMPT = "只供写查询参考（定时间范围、估表大小），不是查询结果，不能作为回答里的数"
#: 日期范围最早和最晚是同一天：这张表很可能只存最新一天（每天整表重灌），拿它算上周、上月只会查出 0
PROFILE_ONE_DAY_PROMPT = "剖析时这张表的 {column} 只有一天（{day}）的数据，可能每天整表重灌：算历史周期请换有历史的表"
PROFILE_STALE_PROMPT = "数据剖析：剖析早于当前版本，不给"
#: 取值观察的结论（next-round 03 B2）：最新业务日期；行数比剖析的新时用观察的
PROFILE_LATEST_PROMPT = "最新业务日期 {column} 到 {value}（取值观察，{day}）"
OBSERVE_HEAD_PROMPT = "取值观察（{day}）：{facts}"

#: 三种「不在」（absent_detail）。{source} 数据源名，{table} 点名的表
ABSENT_REMOVED_PROMPT = "数据源「{source}」里已经没有 {table}（表已删除或改名）。别拿名字相近的表顶替"
ABSENT_MISSING_PROMPT = "数据源「{source}」里没有 {table}（全库的对象清单里也没有）。别拿名字相近的表顶替，不确定时用 keyword 检索"
ABSENT_OUT_OF_SCOPE_PROMPT = ("{table} 在数据源「{source}」的探查范围外，这次没有它的结构{why}。要用它，请点名查看（会按需加入），"
                              "或者请管理员把它加进探查范围")
ABSENT_REFLECT_FAILED_PROMPT = "{table} 在数据源「{source}」的探查范围里，但读不到它的结构：{error}。别猜它的字段名"
ABSENT_UNKNOWN_PROMPT = ("数据源「{source}」只探查了一部分表，{table} 不在已探查的表里，无法判断是否存在。请直接查询数据字典"
                         "（information_schema.tables / columns，SQLite 用 sqlite_master）确认它的结构")


# ==========================================================================
# 遮罩
# ==========================================================================


def masked_set(source: Any) -> set[str]:
    """这个源的遮罩列（小写）。"""
    return {c.lower() for c in masked_columns(getattr(source, "options", None))}


def _masked_meta(meta: Mapping[str, Any], masked: set[str]) -> dict[str, Any]:
    """表结构里遮罩列的注释抹掉：注释里常有取值说明（「1=男 2=女」），遮罩列只给列名和类型。"""
    out = dict(meta)
    out["columns"] = [{**col, "comment": None} if str(col.get("name") or "").lower() in masked else col
                      for col in meta.get("columns") or [] if isinstance(col, Mapping)]
    return out


def masked_notes(notes: Mapping[str, Any] | None, masked: set[str]) -> dict[str, Any]:
    """目录里遮罩列的各项整列去掉（含码值和说明）。表级的项、关联关系只是列名，照给。"""
    out = copy.deepcopy(dict(notes or {}))
    columns = out.get("columns")
    if masked and isinstance(columns, dict):
        out["columns"] = {name: items for name, items in columns.items() if str(name).lower() not in masked}
    return out


# ==========================================================================
# 库概览、表目录
# ==========================================================================


def overview_line(source: Any, *, catalog_tables: int) -> str:
    """system 里一个库一行：名称、类型、是否只读、说明、表数（没探查全时写明）、有数据目录的表数、有没有设遮罩；
    没探查或探查失败的写原因（introspect.why_empty）。"""
    cache = getattr(source, "schema_cache", None) or {}
    tables = cache.get("tables") or {}
    if not tables:
        state = introspect.why_empty(cache)
        if candidates := cache.get("available_schemas"):
            state += "，" + OVERVIEW_SCHEMAS_PROMPT.format(names="、".join(candidates[:8]))
    else:
        total = cache.get("total")
        total = max(total if isinstance(total, int) and not isinstance(total, bool) else 0, len(tables))
        partial = OVERVIEW_SCOPED_PROMPT if isinstance(cache.get("scope"), dict) else OVERVIEW_PARTIAL_PROMPT
        parts = [partial.format(total=total, explored=len(tables)) if cache.get("truncated")
                 and total > len(tables) else OVERVIEW_TABLES_PROMPT.format(total=total)]
        if catalog_tables >= len(tables):
            parts.append(OVERVIEW_CATALOG_ALL_PROMPT)
        elif catalog_tables:
            parts.append(OVERVIEW_CATALOG_SOME_PROMPT.format(n=catalog_tables))
        else:
            parts.append(OVERVIEW_CATALOG_NONE_PROMPT)
        if masked_set(source):
            parts.append(OVERVIEW_MASKED_PROMPT)
        state = "，".join(parts)
    return OVERVIEW_LINE_PROMPT.format(
        name=source.name, kind=source.kind, access="只读" if source.readonly else "可写",
        description=source.description or "未填说明", state=state)


def table_index(source: Any, notes_by_table: Mapping[str, Mapping[str, Any]], *,
                tables: Iterable[str] | None = None) -> str:
    """紧凑的表目录，每张表一行：表名｜中文名｜粒度（catalog.render_table_index）。挑选失败时交回给模型点名。"""
    return catalog.render_table_index(getattr(source, "schema_cache", None), notes_by_table, tables=tables)


# ==========================================================================
# 表详情
# ==========================================================================


def table_detail(source: Any, table: str, *, catalog_notes: Mapping[str, Mapping[str, Any]] | None = None,
                 profile: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """单表的完整字段说明，有可给模型看的目录时附在后面，有剖析事实时再附一节（profile_lines）；遮罩列只给列名和类型。

    catalog_notes 是这个源的数据目录 {表名: notes}（原样，含驳回项：render_table_notes 自己去掉）。profile 是这个源的剖析
    事实 {表名: facts}（catalog_profile_jobs.last_facts）；不给、这张表没有时输出和以前一字不差。找不到这张表时交回的几种
    说法（结构没探查、只探查了一部分、确实没有）和 introspect.describe_table 一样。
    """
    meta = introspect.find_table(source, table)
    if not meta:
        return introspect.describe_table(source, table)
    masked = masked_set(source)
    shown = _masked_meta(meta, masked)
    head = f"{'视图' if meta.get('is_view') else '表'} {meta.get('qualified', table)}"
    if meta.get("comment"):
        head += f"（{meta['comment']}）"
    lines = [head]
    pk = set(meta.get("primary_key") or [])
    for col in shown["columns"]:
        marks = []
        if str(col.get("name") or "").lower() in masked:
            marks.append(MASKED_COLUMN_PROMPT)
        else:
            if col["name"] in pk:
                marks.append("主键")
            if not col.get("nullable", True):
                marks.append("非空")
            if col.get("comment"):
                marks.append(col["comment"])
        suffix = f"  {'、'.join(marks)}" if marks else ""
        lines.append(f"  {col['name']}  {col.get('type') or ''}{suffix}")
    if catalog_notes:
        key = catalog.resolve_table_name(getattr(source, "schema_cache", None), table)
        block = catalog.render_table_notes(masked_notes(catalog_notes.get(key) if key else None, masked),
                                           meta=shown, system_notes=catalog.system_notes_source(source))
        if block:
            lines.append(block)
    if profile:
        key = catalog.resolve_table_name(getattr(source, "schema_cache", None), table)
        lines += profile_lines(profile.get(key) if key else None, masked, snapshot_of(source),
                               upload=is_upload(source))
    return "\n".join(lines)


# ==========================================================================
# 剖析事实
# ==========================================================================


def is_upload(source: Any) -> bool:
    """上传表格的源（含绑定了快照的视图）：剖析事实要和快照对得上才给。"""
    return (getattr(source, "origin", None) or "manual") == "upload"


def snapshot_of(source: Any) -> str | None:
    """这次读的是上传表格的哪个快照：绑定了快照的视图（table_versions.SourceView）是它绑定的那个，数据源记录是当前启用的
    那个；数据库源为 None。"""
    if not is_upload(source):
        return None
    return getattr(source, "snapshot_id", None) or getattr(source, "current_snapshot_id", None)


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _when(value: Any) -> str:
    """日期范围的一端：日期只写到日，带时刻的写到秒（剖析记的是数据库原样的文字）。"""
    text = str(value or "")
    return text[:10] if len(text) <= 10 or text[10:].strip(" T0:.") == "" else text[:19]


def profile_lines(facts: Mapping[str, Any] | None, masked: set[str], snapshot_id: str | None, *,
                  upload: bool = False, indent: str = "  ") -> list[str]:
    """剖析事实那一节：「数据剖析（YYYY-MM-DD）：约 N 行（数据库统计估算）；下单日从 … 到 …」，后面一句固定提示。

    masked 是渲染这一刻的遮罩列（小写）：剖析之后才加遮罩的列，范围不出现。upload：上传表格源，剖析时的快照
    （facts.snapshot_id）和这次读的快照对不上时只写「剖析早于当前版本，不给」——升级前的剖析记录没有快照，上传表格在重新剖析
    之前一律不给。没有剖析事实（或者滤完什么都不剩）返回空列表：表详情和以前一字不差。"""
    if not facts:
        return []
    observed = facts.get("observed") if isinstance(facts.get("observed"), Mapping) and not upload else {}
    if upload and (not snapshot_id or facts.get("snapshot_id") != snapshot_id):
        return [indent + PROFILE_STALE_PROMPT]
    parts: list[str] = []
    one_day: list[str] = []
    estimate = facts.get("row_estimate") if isinstance(facts.get("row_estimate"), Mapping) else None
    seen_rows = observed.get("rows") if isinstance(observed.get("rows"), Mapping) else None
    if seen_rows and str(seen_rows.get("at") or "") > str(facts.get("at") or ""):
        # 取值观察记的行数比上次剖析的新：用新的（同一种说法，口径照写）
        estimate = seen_rows
    if estimate:
        method, rows, least = estimate.get("method"), estimate.get("rows"), estimate.get("at_least")
        if isinstance(rows, int) and method in ("stats", "count"):
            parts.append(PROFILE_ROWS_PROMPT[method].format(n=f"{rows:,}"))
        elif isinstance(least, int) and method == "count":
            parts.append(PROFILE_ROWS_PROMPT["at_least"].format(n=f"{least:,}"))
    for found in facts.get("date_ranges") or []:
        if not isinstance(found, Mapping):
            continue
        column = str(found.get("column") or "")
        if not column or column.lower() in masked or found.get("min") is None or found.get("max") is None:
            continue
        parts.append(PROFILE_RANGE_PROMPT.format(column=column, low=_when(found["min"]), high=_when(found["max"])))
        if (day := _same_day(found["min"], found["max"])) is not None:
            one_day.append(PROFILE_ONE_DAY_PROMPT.format(column=column, day=day))
    for found in observed.get("freshness") or []:
        column = str((found or {}).get("column") or "")
        if column and column.lower() not in masked and found.get("max"):
            parts.append(PROFILE_LATEST_PROMPT.format(column=column, value=_when(found["max"]), day=_day(found.get("at"))))
    if not parts:
        return []
    if facts.get("at"):
        head = PROFILE_HEAD_PROMPT.format(day=_day(facts.get("at")), facts="；".join(parts))
    else:
        # 只有取值观察、没剖析过：照实说是观察
        latest = max((str(f.get("at") or "") for f in [seen_rows or {}, *(observed.get("freshness") or [])]), default="")
        head = OBSERVE_HEAD_PROMPT.format(day=_day(latest), facts="；".join(parts))
    return [indent + head, *(indent + "  " + line for line in one_day), indent + "  " + PROFILE_HINT_PROMPT]


def _same_day(low: Any, high: Any) -> str | None:
    """日期范围的两头是同一天（DATE、DATETIME、'YYYY-MM-DD'、'YYYYMMDD' 都认，data/coverage 同一个归一）：那一天，不是为 None。"""
    from app.data.coverage import _as_day

    lo, hi = _as_day(low), _as_day(high)
    return lo.day.isoformat() if lo is not None and hi is not None and lo.day == hi.day else None


# ==========================================================================
# 三种「不在」
# ==========================================================================


def absent_detail(source: Any, table: str, reason: str | None, *, error: str | None = None, why: str = "") -> str | None:
    """点名的表不在表结构里时的说法（source_objects.absent_reasons 的原因）：表已不在、在探查范围外（why 是没能按需加入的
    原因，比如上限用完了）、在范围里却读不到结构。原因不明（没有对象索引的老缓存）返回 None，由调用方照旧说。"""
    from app.data import source_objects as so

    name = getattr(source, "name", "")
    if reason == so.ABSENT_REMOVED:
        return ABSENT_REMOVED_PROMPT.format(source=name, table=table)
    if reason == so.ABSENT_MISSING:
        return ABSENT_MISSING_PROMPT.format(source=name, table=table)
    if reason == so.ABSENT_OUT_OF_SCOPE:
        return ABSENT_OUT_OF_SCOPE_PROMPT.format(source=name, table=table, why=f"（{why}）" if why else "")
    if reason == so.ABSENT_REFLECT_FAILED:
        return ABSENT_REFLECT_FAILED_PROMPT.format(source=name, table=table, error=error or "读不到列")
    if reason == so.ABSENT_UNKNOWN:
        return ABSENT_UNKNOWN_PROMPT.format(source=name, table=table)
    return None


# ==========================================================================
# 大库的检索预筛
# ==========================================================================

#: 范围外的对象在检索里的权重（只有名字和注释，低一档）
OUTSIDE_WEIGHT = 0.6


@dataclass(frozen=True)
class SearchHit:
    """检索的一个命中：表名（范围内是 schema_cache 的键，范围外是对象索引里的名字）、得分、在不在探查范围里。"""

    name: str
    score: float
    in_scope: bool = True


def _doc_text(name: str, meta: Mapping[str, Any] | None, notes: Mapping[str, Any] | None, masked: set[str]) -> str:
    """一张范围内的表的检索文档：表名（再拆词）、注释、目录里的中文名 / 粒度 / 说明、列名（再拆词）、列的中文名。遮罩列只用
    列名（注释、目录项不进：里面常写着取值）。"""
    from app.data.catalog import name_words

    parts = [name, " ".join(name_words(name))]
    meta = meta or {}
    if meta.get("comment"):
        parts.append(str(meta["comment"]))
    visible = catalog.visible_notes(notes or {})
    for key in ("label", "grain", "description", "note"):
        item = visible.get(key)
        if isinstance(item, Mapping) and isinstance(item.get("value"), str):
            parts.append(item["value"])
    columns_notes = visible.get("columns") if isinstance(visible.get("columns"), Mapping) else {}
    for col in meta.get("columns") or []:
        if not isinstance(col, Mapping) or not col.get("name"):
            continue
        cname = str(col["name"])
        parts += [cname, " ".join(name_words(cname))]
        if cname.lower() in masked:
            continue
        if col.get("comment"):
            parts.append(str(col["comment"]))
        items = columns_notes.get(cname) if isinstance(columns_notes, Mapping) else None
        label = items.get("label") if isinstance(items, Mapping) else None
        if isinstance(label, Mapping) and isinstance(label.get("value"), str):
            parts.append(label["value"])
    return " ".join(parts)


def _outside_text(name: str, comment: str | None) -> str:
    """一个范围外对象的检索文档：名字、名字拆出的词、注释。没有注释的只有英文（见模块说明的限制）。"""
    from app.data.catalog import name_words

    return " ".join([name, " ".join(name_words(name)), comment or ""])


class _Index:
    """BM25（k1=1.5、b=0.75，和 memory/embeddings.BM25 同一个公式）的倒排实现：几千上万篇文档时只看含这个词的文档，
    不逐篇数。"""

    def __init__(self, docs: list[str]) -> None:
        import math

        from app.memory.embeddings import _tokens

        self._tokens = _tokens
        self.k1, self.b = 1.5, 0.75
        self.lengths: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, doc in enumerate(docs):
            toks = _tokens(doc)
            self.lengths.append(len(toks))
            counts: dict[str, int] = {}
            for t in toks:
                counts[t] = counts.get(t, 0) + 1
            for t, n in counts.items():
                self.postings.setdefault(t, []).append((i, n))
        self.n = len(docs)
        self.avg = (sum(self.lengths) / self.n) if self.n else 0.0
        self._log = math.log

    def score(self, query: str) -> dict[int, float]:
        out: dict[int, float] = {}
        for tok in dict.fromkeys(self._tokens(query)):
            posting = self.postings.get(tok)
            if not posting:
                continue
            idf = self._log(1 + (self.n - len(posting) + 0.5) / (len(posting) + 0.5))
            for i, tf in posting:
                denom = tf + self.k1 * (1 - self.b + self.b * self.lengths[i] / (self.avg or 1))
                out[i] = out.get(i, 0.0) + idf * (tf * (self.k1 + 1)) / denom
        return out


class TableSearch:
    """一个源的检索：范围内的表（schema_cache）和范围外的对象（对象索引里的名字、注释）。建一次（一轮查看里第一次要挑时），
    之后每个需求查一次。"""

    def __init__(self, source: Any, notes_by_table: Mapping[str, Mapping[str, Any]],
                 outside: Iterable[Any] = ()) -> None:
        tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
        masked = masked_set(source)
        self.names: list[str] = []
        self.inside: list[bool] = []
        docs: list[str] = []
        for name, meta in tables.items():
            self.names.append(name)
            self.inside.append(True)
            docs.append(_doc_text(name, meta if isinstance(meta, Mapping) else None, notes_by_table.get(name), masked))
        known = set(self.names)
        for obj in outside:
            name = str(getattr(obj, "name", obj))
            if name in known:
                continue
            self.names.append(name)
            self.inside.append(False)
            docs.append(_outside_text(name, getattr(obj, "comment", None)))
        self._index = _Index(docs)

    def __len__(self) -> int:
        return len(self.names)

    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        """按得分从高到低的前 limit 个（得分为 0 的不算命中）。"""
        scored = self._index.score(query)
        hits = [SearchHit(self.names[i], s * (1.0 if self.inside[i] else OUTSIDE_WEIGHT), self.inside[i])
                for i, s in scored.items() if s > 0]
        hits.sort(key=lambda h: (-h.score, not h.in_scope, h.name))
        return hits[:limit]


def search_tables(source: Any, notes_by_table: Mapping[str, Mapping[str, Any]], query: str, *,
                  outside: Iterable[Any] = (), limit: int = 120) -> list[SearchHit]:
    """按需求检索这个源的表（一次性的写法；同一个源要查多次就建一个 TableSearch 复用）。"""
    return TableSearch(source, notes_by_table, outside).search(query, limit=limit)


def table_brief(source: Any, table: str) -> str:
    """字数超了时的一行：表名和全部字段名（遮罩列的名字照给，名字本身不是值）。"""
    meta = introspect.find_table(source, table) or {}
    columns = [str(c.get("name")) for c in meta.get("columns") or [] if isinstance(c, Mapping) and c.get("name")]
    return BRIEF_PROMPT.format(name=meta.get("qualified", table), kind="视图" if meta.get("is_view") else "表",
                               columns="、".join(columns), n=len(columns))


# ==========================================================================
# 连接条件与连接路径
# ==========================================================================


def relation_graph(source: Any, raw_notes: Mapping[str, Mapping[str, Any]]) -> dict[str, list[JoinEdge]]:
    """这个源的关系图：目录里的关系（驳回的不进，驳回项还要压住同一条关系的现推结果，所以给原样的目录）加上按表结构
    现推的（外键约束、命名推断）。"""
    return catalog.relation_graph(raw_notes, schema_cache=getattr(source, "schema_cache", None))


def complete(picked: list[str], graph: Mapping[str, list[JoinEdge]],
             room: int) -> tuple[list[str], list[tuple[str, str, tuple[JoinEdge, ...]]]]:
    """补连接用的中间表：(给详情的表, [(a, b, 经中间表的两跳路径)])。room 是还能补几张。

    挑中的表两两之间已经直接相连的不补；有一条路径的中间表已经在里面的也不补。
    """
    tables = list(picked)
    routes: list[tuple[str, str, tuple[JoinEdge, ...]]] = []
    for i, a in enumerate(picked):
        for b in picked[i + 1:]:
            if any(e.to_table == b for e in graph.get(a, ())):
                continue
            paths = [p for p in catalog.join_paths(graph, a, b, max_hops=2) if len(p) == 2]
            via = next((p for p in paths if p[0].to_table in tables), None)
            if via is None and paths and len(tables) - len(picked) < room:
                via = paths[0]
                tables.append(via[0].to_table)
            if via is not None:
                routes.append((a, b, via))
    return tables, routes


def qualifier(source: Any):
    """表名（schema_cache 的键）→ 全名：模型照着全名写 SQL，漏了 schema 前缀在 Oracle 上直接报错。"""
    tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
    return lambda key: str((tables.get(key) or {}).get("qualified") or key)


def _condition(edge: JoinEdge, qualified) -> str:
    """连接条件，表名用全名：channel_visits.visit_id = visits.id（多列用 AND 连接）。"""
    left, right = qualified(edge.from_table), qualified(edge.to_table)
    return " AND ".join(f"{left}.{a} = {right}.{b}" for a, b in zip(edge.from_columns, edge.to_columns))


def _edge_line(edge: JoinEdge, qualified) -> str:
    card = f"（{CARDINALITY_LABEL[edge.cardinality]}）" if edge.cardinality in CARDINALITY_LABEL else ""
    return _condition(edge, qualified) + card + (INFERRED_MARK if edge.status == "proposed" else "")


def _edges_within(graph: Mapping[str, list[JoinEdge]], tables: list[str]) -> list[JoinEdge]:
    """两端都在 tables 里的关系，每条一次（按 relation_id）；一对多的翻过来，从「多」的一端写起（外键所在的表在左）。"""
    wanted = set(tables)
    seen: set[str] = set()
    out = []
    for table in tables:
        for edge in graph.get(table, ()):
            if edge.to_table not in wanted or edge.relation_id in seen:
                continue
            seen.add(edge.relation_id)
            out.append(edge.reversed() if edge.cardinality == "one_to_many" else edge)
    return out


def join_lines(source: Any, graph: Mapping[str, list[JoinEdge]], tables: list[str], *, indent: str = "  ") -> list[str]:
    """给出的几张表之间的连接条件，可以照抄进 SQL。没有就是空列表。"""
    edges = _edges_within(graph, tables)
    if not edges:
        return []
    qualified = qualifier(source)
    lines = [indent + JOIN_HEAD_PROMPT]
    lines += [f"{indent}  {_edge_line(e, qualified)}" for e in edges[:JOIN_LINES_LIMIT]]
    if len(edges) > JOIN_LINES_LIMIT:
        lines.append(indent + "  " + JOIN_MORE_PROMPT.format(n=len(edges) - JOIN_LINES_LIMIT))
    return lines


def route_lines(source: Any, routes: list[tuple[str, str, tuple[JoinEdge, ...]]], *, indent: str = "  ") -> list[str]:
    """两张表没有直接关联、要经一张中间表连接的写法。"""
    if not routes:
        return []
    qualified = qualifier(source)
    lines = [indent + PATH_HEAD_PROMPT]
    for a, b, (first, second) in routes[:PATH_LINES_LIMIT]:
        line = ROUTE_PROMPT.format(a=qualified(a), b=qualified(b), mid=qualified(first.to_table),
                                   first=_condition(first, qualified), second=_condition(second, qualified))
        inferred = first.status == "proposed" or second.status == "proposed"
        lines.append(f"{indent}  {line}" + (INFERRED_MARK if inferred else ""))
    return lines


__all__ = [
    "JOIN_LINES_LIMIT", "OUTSIDE_WEIGHT", "PATH_LINES_LIMIT", "PROFILE_HINT_PROMPT", "PROFILE_STALE_PROMPT", "SearchHit",
    "TableSearch", "absent_detail", "complete", "is_upload", "join_lines", "masked_notes", "masked_set",
    "overview_line", "profile_lines", "qualifier", "relation_graph", "route_lines", "search_tables", "snapshot_of",
    "table_brief", "table_detail", "table_index",
]
