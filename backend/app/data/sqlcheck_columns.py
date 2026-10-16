"""SQL 里的列名对照数据源结构（sql_unknown_columns）：助手自查（api/copilot.py）和表结构变化的影响面
（data/catalog_impact.py：已发布模板写死的 SQL 换上新结构再跑一遍，说得出「这条 SQL 现在会报列不存在」）共用这一份。

以前它在 api/copilot.py 里，影响面要用就得从 data 包反过来导入接口层；挪到这里，copilot 那边只改 import。

只做有把握的判断，宁可漏判不可误判：助手自查会把图打回去让模型改，影响面会把一条变化升为「要处理」。CTE、FROM 里的
子查询、引号括起来的标识符、结构里找不到的表、结构没探查过或者探查被截断的，一律不判。
"""
from __future__ import annotations

import re
from typing import Any

#: @ 开头的是变量（@region、@@session），: 开头的是参数，. 后面的是限定名
_SQL_IDENT = re.compile(r"(?<![\w$#.:@])([A-Za-z_][A-Za-z0-9_$#]*)(?![\w$#])")
_FROM_ITEMS = re.compile(
    r"\b(?:from|join)\s+(.+?)(?=\bwhere\b|\bgroup\b|\border\b|\bhaving\b|\blimit\b|\bfetch\b|\boffset\b"
    r"|\bunion\b|\bintersect\b|\bexcept\b|\bon\b|\busing\b|\b(?:left|right|inner|outer|full|cross|natural)\b"
    r"|\bjoin\b|\bwindow\b|\)|$)", re.I | re.S)
_TABLE_ITEM = re.compile(r"^\s*([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)*)(?:\s+(?:as\s+)?([A-Za-z_][\w$#]*))?\s*$",
                         re.I)
_ALIAS_AS = re.compile(r"\bas\s+([A-Za-z_][\w$#]*)", re.I)
#: 不写 AS 的列别名：「SUM(amount) total,」「amount total FROM」
_ALIAS_BARE = re.compile(r"(\)|\b[A-Za-z_][\w$#]*|\d)\s+([A-Za-z_][\w$#]*)\s*(?=,|\bfrom\b)", re.I)
_QUALIFIED = re.compile(r"(?<![\w$#.])([A-Za-z_][\w$#]*)\s*\.\s*([A-Za-z_][\w$#]*|\*)(?!\s*\()")
_SQL_WORDS = frozenset("""
select distinct all from where and or not in is null like ilike between exists case when then else end as on using
join inner left right outer full cross natural group by order having limit offset fetch first next rows row only
union intersect except asc desc nulls last with over partition range preceding following unbounded current true
false interval year month day hour minute second week quarter dow doy epoch date time timestamp datetime zone
cast integer int bigint smallint tinyint decimal numeric real float double precision varchar char character text
boolean bool signed unsigned binary top percent escape collate any some values current_date current_time
current_timestamp localtime localtimestamp sysdate systimestamp rownum level dual unknown filter within
at nocase rtrim glob regexp rlike similar to div mod xor isnull notnull minus prior connect start nocycle siblings
grouping sets rollup cube ignore respect lateral apply qualify window exclude include ties others no groups
for update of share nowait skip locked lock mode array sounds overlaps language query expansion separator
charset utf8 utf8mb4 latin1 ascii unicode years months weeks days hours minutes seconds microsecond microseconds
millisecond milliseconds year_month day_hour day_minute day_second hour_minute hour_second minute_second
isodow isoyear century decade millennium julian timezone timezone_hour timezone_minute
sql_calc_found_rows sql_no_cache sql_cache sql_small_result sql_big_result sql_buffer_result high_priority
straight_join distinctrow unique recursive tablesample bernoulli sample repeatable pivot unpivot keep ordinality
symmetric asymmetric leading trailing both placing force use index into default without local
user current_user session_user system_user current_schema current_catalog current_role
""".split())

#: 后面跟的一定是操作数、不会是别名的词：「SELECT created_at FROM」里 created_at 是列。
#: END、NULL、TRUE、CURRENT_DATE 这类「一个值到此为止」的词不在里面——「… END flag,」的 flag 是别名
_OPERAND_BEFORE = frozenset("""
select distinct distinctrow all unique by as and or not xor is in like ilike glob regexp rlike similar to between
when then else case on where having exists div mod escape interval collate using any some from join
sql_calc_found_rows sql_no_cache sql_cache sql_small_result sql_big_result sql_buffer_result high_priority
straight_join
""".split())
#: 不是列的名字：排序规则名（COLLATE x）、字符集名（USING x、CHARSET x、CHARACTER SET x）
_NAME_AFTER = re.compile(
    r"\b(?:collate|charset|character\s+set|using(?!\s*\())\s+([A-Za-z_][\w$#]*)", re.I)
#: DATEADD(dd, …)、DATEDIFF(wk, …) 的第一个参数是日期部分的缩写，不是列。只在这个位置放过：
#: 放进关键字表的话，别处打错成 wk、dd 的列名就查不出来了
_DATEPART_ARG = re.compile(
    r"\b(?:dateadd|datediff|datediff_big|datepart|datename|datetrunc|date_bucket|timestampadd|timestampdiff)"
    r"\s*\(\s*([A-Za-z_][\w$#]*)", re.I)
#: 命名窗口：WINDOW w AS (…)、…, w2 AS (…)
_WINDOW_NAME = re.compile(r"(?:\bwindow|,)\s*([A-Za-z_][\w$#]*)\s+as\s*\(", re.I)


def _blank_unicode_identifiers(sql: str) -> str:
    """带引号、名字里有非 ASCII 字符的标识符（中文别名 AS "月份"、AS `曝光量`）连引号一起换成空格。

    这项核对只认 ASCII 的列名，中文名本来就不判；不抹掉的话，SQL 里一出现带引号的标识符就整条放弃核对，照「结果列
    起中文别名」写的 SQL 会把猜错的英文列名一起放过去。英文的带引号标识符（"StatDate"）大小写、空格都可能，照旧不判。"""
    from app.data.guard import _GENERIC, _tokens

    out = []
    for token in _tokens(sql, _GENERIC):
        if token.kind == "ident" and not token.text.isascii():
            out.append(" " * len(token.text))
        else:
            out.append(token.text)
    return "".join(out)


def sql_unknown_columns(sql: str, source: Any) -> tuple[list[str], list[str]] | None:
    """SQL 里用到、数据源结构里查不到的列：(查不到的列, 这几张表的全部列)。判断不了、或者都查得到时返回 None。"""
    from app.data import introspect as _introspect
    from app.data.guard import _blank_quoted, first_verb, strip_comments

    cache = getattr(source, "schema_cache", None) or {}
    if not cache.get("tables") or cache.get("failed") or first_verb(sql) != "select":
        return None
    text = _blank_unicode_identifiers(re.sub(r"\{\{.*?\}\}", " 0 ", strip_comments(sql), flags=re.S))
    if '"' in text.replace("''", "") and _blank_quoted(text) != _blank_quoted(text, identifiers=False):
        return None                                   # 引号括起来的标识符：大小写、空格都可能，不猜
    text = _blank_quoted(text)
    if re.search(r"\bwith\b", text, re.I) or re.search(r"\b(?:from|join)\s*\(", text, re.I) or "`" in text \
            or "[" in text:
        return None                                   # [方括号] 标识符、数组下标：同样不猜
    tables: dict[str, dict[str, Any]] = {}       # 表名、别名（小写）→ 表结构
    spans: list[tuple[int, int]] = []
    for m in _FROM_ITEMS.finditer(text):
        spans.append(m.span(1))
        for item in m.group(1).split(","):
            parsed = _TABLE_ITEM.match(item)
            if not parsed:
                return None
            name, alias = parsed.group(1), parsed.group(2)
            meta = _introspect.find_table(source, name)
            if meta is None:
                return None                           # 结构里找不到的表（视图、同义词、截断了的结构）不判
            tables[name.lower()] = tables[name.rsplit(".", 1)[-1].lower()] = meta
            if alias and alias.lower() not in _SQL_WORDS:
                tables[alias.lower()] = meta
    if not tables:
        return None
    known = list(dict.fromkeys(c["name"] for meta in {id(m): m for m in tables.values()}.values()
                               for c in meta.get("columns") or []))
    columns = {c.lower() for c in known}
    # 「SELECT created_at FROM」里 created_at 前面是关键字，它是列，不是别名
    aliases = {a.lower() for a in _ALIAS_AS.findall(text)} | {
        m.group(2).lower() for m in _ALIAS_BARE.finditer(text) if m.group(1).lower() not in _OPERAND_BEFORE} | {
        a.lower() for a in _WINDOW_NAME.findall(text)}
    names = {m.start(1) for pattern in (_NAME_AFTER, _DATEPART_ARG) for m in pattern.finditer(text)}
    unknown: list[str] = []
    for m in _QUALIFIED.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        meta = tables.get(m.group(1).lower())
        col = m.group(2)
        if meta is not None and col != "*" and col.lower() not in {c["name"].lower() for c in meta.get("columns") or []}:
            unknown.append(f"{m.group(1)}.{col}")
    for m in _SQL_IDENT.finditer(text):
        word = m.group(1)
        low = word.lower()
        if any(a <= m.start() < b for a, b in spans) or low in _SQL_WORDS or low in columns or low in aliases \
                or low in tables or m.start() in names or re.match(r"\s*[.(]", text[m.end():]) \
                or text[m.end():m.end() + 1] == "'":      # N'…'、X'…'、_utf8mb4'…'：字面量的前缀
            continue
        unknown.append(word)
    unknown = list(dict.fromkeys(unknown))
    return (unknown, known) if unknown else None


__all__ = ["sql_unknown_columns"]
