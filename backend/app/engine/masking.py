"""遮罩：一份查询结果里哪些列要遮（按 SQL 认出改了名字的遮罩列）、哪些值是遮罩值（按值认）。

数据源在 options.mask_columns 里设了遮罩的列（手机号、姓名这类），证据面板按列名遮（api/evidence.py）。核查 agent
（engine/verify_agent.py：双盲复算、反方审稿、审批质询共用）自己写 SQL，起别名、套函数、经子查询转一道，结果列名就
对不上遮罩列；按值认又有门槛和写法的问题。认法集中在这里，核查 agent 的查询工具（tools/datasource 的遮罩开关）、审批
质询（engine/inquiry.py）、双盲复算（nodes/dual_check.py，经 inquiry 的同名导出）共用：

- 工具边界先遮（mask_query_result）：核查 agent 的查询结果交给模型、落进快照之前，取自遮罩列的格子换成「已遮罩」，
  别的格子按值换：这份结果里的遮罩值，加上这个 agent 前面的查询遮掉的、调用方交进来的（报告快照里的）。模型看不到
  遮罩值，就不会把它写进回答、理由、SQL——事后按值认每一轮复核都找到新的漏法，根治靠这一步；审批质询回答、反方
  审稿理由里的按值遮降为第二道防线。查询失败时报错原话里的值也换（mask_error_text）

- 哪些列（_derived_hidden）：从遮罩列本身起，按 SQL 一层层认出值取自遮罩列的结果列（别名、函数、子查询、WITH、UNION 的
  另一支、和 * 写在同一支里没起别名的表达式、FROM 里拆开遮罩列的表值函数）；COUNT 只数个数、窗口的分区键只决定次序、
  按遮罩列分档又当分组键或套在汇总里数个数的、汇总里的 CASE 只在条件里用到的不算（_value_columns、_band_only）。认不出
  落在哪一列的、SQL 解析不了又写着遮罩列的，整份快照的列都算。双盲复算按同一套认法在派盲算之前挡下取自遮罩列的指标
  （derived_columns）
- 只装着分档标签的列（_label_columns）：标签是 agent 写在 SQL 里的字，不当遮罩值；* 展开数据源里的表时按冻结的表结构
  认它带不带同名的列（_schema_tables）
- 哪些值（_Secrets）：遮罩列里出现过的值，按值认有门槛（SECRET_MIN、SECRET_DIGITS），换的时候先按词、按开头几个字筛出
  正文里真有的那几个值，不现编上万个分支的正则；正文、文字格、SQL 的字符串和注释里换了写法的长号码（199-1474-2785、
  +86 …、全角数字）按纯数字写法在集合里查（loose_spans）；已经摆在读者眼前的值免遮，之后并进来的照样免（protected）
- 快照遮好的样子（_masked_view），回答时才认得出、事后要照位置重放的地方（view_edits、replay_edits）

除 mask_query_result、换了写法的号码和这一轮新加的几样之外，这里的函数原来都在 engine/inquiry.py 里，名字不变，
inquiry 照旧导出（别处按 inquiry 的名字引用）。
"""
from __future__ import annotations

import contextlib
import functools
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.engine.evidence import MARKER_RE, RenderError, render_cell
from app.engine.expressions import numeric_text
from app.engine.merge_query import MERGE_SOURCE

logger = logging.getLogger(__name__)


#: 按值认遮罩值的门槛：文字至少这么多字（一个字的「是」「男」换掉，会把不相干的词也换得面目全非）；纯数字至少这么多位
#: （年龄、等级、小额金额这类短的数和阈值、天数、日期、LIMIT 里的数太容易撞上，按值换会把实际执行的 SQL、提问人自己
#: 的条件改得面目全非。它们只在 SQL 里和遮罩列做等值一类比较时才换，见 _scrub_sql；引用到的格子照旧按列遮）
SECRET_MIN = 2
SECRET_DIGITS = 5
#: 遮罩列的值最多收这么多种写法（一份快照最多上千行）：再多就按已经收到的换
SECRETS_MAX = 20_000


#: 按词边界认值的字符：值的头尾是这样的字时，前后不能紧挨着同类的字（13900000001 不换掉 139000000012 的一截）
_WORD = re.compile(r"[0-9A-Za-z_]")


def _query_shaped(content: Any) -> bool:
    return isinstance(content, dict) and isinstance(content.get("rows"), list)


def _masked_word() -> str:
    from app.api.evidence import MASKED

    return MASKED


# --------------------------------------------------------------------------
# 遮罩：哪些列
# --------------------------------------------------------------------------


def _value_columns(node: Any) -> set[str]:
    """一个选出列的值取自哪些列（列名小写）。COUNT 里的只数个数，窗口的分区键、排序键只决定次序，结果全是写死常量的
    分档（_band_only：当分组键，或者套在汇总里按条件数个数）只决定一行落在哪一档，都带不出值，不算；套在里面的子查询
    只看它选出来的列（它的条件、连接里写的列带不出值）。汇总里的 CASE / IIF 只看各个结果：条件只决定这一行算不算进来，
    同写在 WHERE 里（SUM(CASE WHEN buyer_phone IS NOT NULL THEN amount ELSE 0 END) 的值全部来自金额）；不汇总的逐行
    CASE 照旧连条件一起算（每一行落在哪一支本身就是这一行的信息）。"""
    from sqlglot import exp

    out: set[str] = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, exp.Column):
            if not isinstance(item.this, exp.Star) and item.name:
                out.add(item.name.lower())
        elif isinstance(item, exp.Count) or (isinstance(item, (exp.Case, exp.If)) and _band_only(item)):
            continue
        elif isinstance(item, (exp.Case, exp.If)) and (results := _branch_results(item)) is not None \
                and _aggregated(item):
            stack.extend(r for r in results if r is not None)
        elif isinstance(item, exp.Window):
            stack.append(item.this)
        elif isinstance(item, exp.Select):
            stack.extend(item.expressions)
        else:
            stack.extend(item.iter_expressions())
    return out


def _aggregated(node: Any) -> bool:
    """node 套在同一层 SELECT 的汇总函数里（SUM(…)、AVG(…) OVER (…) 也算）。"""
    from sqlglot import exp

    parent = node.parent
    while parent is not None and not isinstance(parent, (exp.Select, exp.Subquery)):
        if isinstance(parent, exp.AggFunc):
            return True
        parent = parent.parent
    return False


def _branch_results(node: Any) -> list[Any] | None:
    """CASE 的各个结果（每个 THEN，加上 ELSE；没写 ELSE 记 None，就是 NULL）、IIF / IF 的真假两支；别的写法为 None。
    CASE 里的每个 WHEN … THEN 在语法树里也是 If，不单算。"""
    from sqlglot import exp

    if isinstance(node, exp.Case):
        return [branch.args.get("true") for branch in node.args.get("ifs") or []] + [node.args.get("default")]
    if isinstance(node, exp.If) and not isinstance(node.parent, exp.Case):
        return [node.args.get("true"), node.args.get("false")]
    return None


def _fixed(node: Any) -> bool:
    """写死的结果：字面量（几个数算出来的也算）、NULL、TRUE / FALSE、没写的 ELSE。"""
    from sqlglot import exp

    return node is None or isinstance(node, (exp.Null, exp.Boolean)) or _constant(node) is not None


def _band_only(node: Any) -> bool:
    """按遮罩列分档、只带出档位或个数的 CASE / IIF：结果全是写死的常量（标签、档位编号、1 和 0），而且
    - 它本身就是一个选出列（起不起别名都行），又是这个 SELECT 的分组键（GROUP BY 按位置、按别名、照原样再写一遍都算）：
      每一档一行，这一列只说这一行是哪一档；
    - 或者套在汇总里（SUM(CASE WHEN amount <= 20000 THEN 1 ELSE 0 END)、AVG(IIF(…, 1.0, 0))）：按条件数个数、算占比，
      同 COUNT。
    都带不出遮罩列的值，同按阈值数个数（col >= v 的 COUNT）。不分组、不汇总的逐行标签照旧算派生值，那是每一笔各落在
    哪一档；汇总遮罩列的值本身（SUM(amount)、CASE 里有一支取 amount 的）也照旧算。"""
    from sqlglot import exp

    results = _branch_results(node)
    if results is None or not all(_fixed(r) for r in results):
        return False
    if _aggregated(node):
        return True
    item = node
    while isinstance(item.parent, (exp.Paren, exp.Alias)) and item.arg_key == "this":
        item = item.parent
    select = item.parent
    if not isinstance(select, exp.Select) or item.arg_key != "expressions" or select.args.get("group") is None:
        return False
    place = next((str(i + 1) for i, e in enumerate(select.expressions) if e is item), None)
    alias = item.alias.lower() if isinstance(item, exp.Alias) and item.alias else None
    for key in select.args["group"].expressions:
        while isinstance(key, exp.Paren):
            key = key.this
        if (isinstance(key, exp.Literal) and not key.is_string and key.this == place) or key == node or (
                alias and isinstance(key, exp.Column) and not key.table and key.name.lower() == alias):
            return True
    return False


def _label_columns(sql: Any, kind: str | None, columns: list[str], tables: dict[str, set[str]] | None = None
                   ) -> dict[str, set[str]]:
    """结果里只装着分档标签的列：列名（小写）→ 标签在格子里的写法。标签（CASE、IIF 写死的结果，分档的名字、档位编号）
    是 agent 自己写在 SQL 里的字，不是数据源里的值：这几列派生自遮罩列也不收进遮罩值（_Secrets.add），不然 SQL 里的
    标签、回答正文里的档名都会被换成「已遮罩」。
    只装着标签：结果的这一列在 UNION 这类的每一支里，都是结果全为写死常量的 CASE / IIF，或者取自派生来源（子查询、
    CTE、LATERAL）里只装着标签的一列（_item_label）。一项取自哪个来源按作用域认，不按名字猜：按名字认时，遮罩列原样
    选出（SELECT vip_level）的名字就是它自己，永远算只装着标签，UNION 另一支、子查询里的遮罩列流进标签列也认不出，
    等级 1 撞上 THEN 1 就不收。取自数据源里的表的（哪怕和标签同名）、有一支装着别的、认不准的（说不清取自哪个来源、
    递归的 CTE、几支的 * 展开在哪说不清）都不算，照收（宁可多遮）；同名的几列有一列不算就都不算。解析不了的一个都
    不算。tables 是查询当时冻结的表结构里每张表的列名（_schema_tables）：没写表名的 * 展开的表查得到、又没有同名的列
    时，这张表带不出这一列（_table_lacks），不再因为它不算。"""
    from app.data.sql_semantics import _parse, _read

    try:
        root, _ = _parse(sql, _read(kind))
    except Exception:  # noqa: BLE001 - 解析器的意外按解析不了处理（标签照旧当遮罩值，宁可多遮）
        root = None
    if root is None:
        return {}
    try:
        result = next((top for top, role, _listed in _blocks(root) if role == "result"), None)
        if result is None:
            return {}
        memo = _LabelMemo()
        memo.tables = tables
        found: dict[str, list[set[str] | None]] = {}
        for place, column in enumerate(columns):
            name = column.lower()
            found.setdefault(name, []).append(_result_label(result, place, name, len(columns), memo))
        return {name: texts for name, parts in found.items() if (texts := _all_labels(parts)) is not None}
    except Exception:  # noqa: BLE001 - 分析本身的缺陷不能让回答出错：一个都不算（宁可多遮）
        logger.warning("质询的 SQL 标签列分析失败", exc_info=True)
        return {}


#: 标签列分析里「这个来源不出这一列」（_block_label、_source_label）：和 None（出这一列，但装着别的、认不准）分开
_ABSENT: Any = object()


class _LabelMemo(dict):  # type: ignore[type-arg]
    """标签列分析记下的结果：(id(那块查询), 列名) → 标签的写法、None 或 _ABSENT。tables 是冻结的表结构里每张表的列名
    （小写，_schema_tables），没有为 None。"""

    tables: dict[str, set[str]] | None = None


def _schema_tables(schema: Any) -> dict[str, set[str]] | None:
    """冻结的表结构（数据源的 schema_cache、表结构快照的内容：{"tables": {表名: {"columns": [{"name": …}]}}}）→
    {表名（小写）: 列名（小写）}，按登记的名字和不带库名的表名各记一份（几个库里同名的表合起来算，宁可多遮）。表结构
    里没列出的表不记（截断了的表结构也一样），查不到就当说不清；整份拿不到的为 None。"""
    tables = schema.get("tables") if isinstance(schema, dict) else None
    if not isinstance(tables, dict):
        return None
    out: dict[str, set[str]] = {}
    for key, meta in tables.items():
        columns = meta.get("columns") if isinstance(meta, dict) else None
        if not isinstance(columns, list):
            continue
        names = {str(c["name"]).lower() for c in columns if isinstance(c, dict) and c.get("name") is not None}
        for name in {str(key).lower(), str(key).lower().rsplit(".", 1)[-1],
                     str(meta.get("qualified") or key).lower().rsplit(".", 1)[-1]}:
            out.setdefault(name, set()).update(names)
    return out


def _table_lacks(source: Any, name: str, memo: _LabelMemo) -> bool:
    """FROM 里的这个来源是数据源里的一张表，冻结的表结构里查得到它、又没有 name 这一列：* 展开它带不出 name。"""
    from sqlglot import exp

    if not memo.tables or not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
        return False
    columns = memo.tables.get(source.name.lower())
    return columns is not None and name not in columns


def _result_label(top: Any, place: int, name: str, width: int, memo: _LabelMemo) -> set[str] | None:
    """结果（width 列）的第 place 列（列名 name）只装着标签时标签的写法，不然为 None：UNION 这类的每一支，那一项都要
    只装着标签。落在 * 里的（认不出是哪一项）按列名经 * 往下找，只有一支时才找（几支的 * 各展开成什么说不清）。"""
    from sqlglot import exp

    leaves = _leaves(top)
    if not all(isinstance(leaf, exp.Select) for leaf in leaves):
        return None
    out: set[str] = set()
    for select in leaves:
        items = list(select.expressions)
        item = next((i for i, p in zip(items, _places(items, width)) if p == place), None)
        if item is not None:
            texts = _item_label(item, select, memo)
        elif len(leaves) == 1:
            texts = _star_label(select, name, memo)
        else:
            return None
        if texts is None or texts is _ABSENT:
            return None
        out |= texts
    return out


def _item_label(item: Any, select: Any, memo: _LabelMemo) -> set[str] | None:
    """select 的一个选出项只装着标签时，标签在格子里的写法：结果全为写死常量的 CASE / IIF 取它的各个结果（数按两种
    写法：25、25.0）；单独一列取它在 FROM 里取自的那一列的（_source_label）；别的为 None。"""
    from sqlglot import exp

    node = item
    while isinstance(node, (exp.Alias, exp.Paren)):
        node = node.this
    results = _branch_results(node)
    if results is not None:
        if not all(_fixed(r) for r in results):
            return None
        out: set[str] = set()
        for result in results:
            value = _constant(result) if result is not None else None
            if isinstance(value, Decimal):
                out |= {_point_text(value), format(value, "f")}
            elif value is not None:
                out.add(_plain_text(value))
        return out
    if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star) and node.name:
        texts = _source_label(select, node.table.lower() or None, node.name.lower(), memo)
        return None if texts is _ABSENT else texts
    return None


def _star_label(select: Any, name: str, memo: _LabelMemo, *, before: int | None = None) -> Any:
    """select 经 *（t.* 只看 t）带出的 name 这一列只装着标签时标签的写法；没有 *、* 带不出这一列为 _ABSENT，带出来的
    装着别的、认不准为 None。before 给了只看排在它前面的那几项（_block_label_of 看同名项前面有没有 *）。
    没写表名的 * 展开了数据源里的表（或 VALUES、函数这类说不清出哪些列的来源）为 None：表里可能就有这一列，* 照样把它
    带出来，不像直接写列名那样会报列名有歧义，_source_label 那条「派生来源出这一列的就是它」在这里不成立。"""
    from sqlglot import exp

    out: set[str] = set()
    seen = False
    for item in select.expressions[:before]:
        if not _is_star(item):
            continue
        qualifier = (item.table.lower() or None) if isinstance(item, exp.Column) else None
        if qualifier is None and any(_derived_block(source) is None and not _table_lacks(source, name, memo)
                                     for _, source in _from_sources(select)):
            return None
        texts = _source_label(select, qualifier, name, memo)
        if texts is None:
            return None
        if texts is not _ABSENT:
            out |= texts
            seen = True
    return out if seen else _ABSENT


def _source_label(select: Any, qualifier: str | None, name: str, memo: _LabelMemo) -> Any:
    """select 的 FROM、JOIN 里读到的那一列（qualifier 是写的表名或别名，没写为 None）只装着标签时标签的写法；没有哪个
    来源出这一列为 _ABSENT，出这一列但装着别的、认不准为 None。
    - 写了表名的只看那个来源（对不上的是外层的表，认不准）；
    - 没写的看所有来源：派生来源出这一列的就是它——SQL 执行过，别的来源就没有同名的列，不然会报列名有歧义。按这一列
      连起来的（USING 里写着它、NATURAL）例外：几个来源的这一列合成一列，外连接时取的可能是另一边的值；
    - 数据源里的表一律不算（遮罩列本身、别的列原样选出来，哪怕和标签同名），VALUES、函数这类来源也认不准。"""
    sources = _from_sources(select)
    if qualifier is not None:
        sources = [(alias, source) for alias, source in sources if alias == qualifier]
        if len(sources) != 1:
            return None
    found: list[set[str]] = []
    opaque = False
    for _alias, source in sources:
        derived = _derived_block(source)
        if derived is None:
            # 冻结的表结构里这张表没有这一列：它不出这一列，不算认不准
            opaque = opaque or not _table_lacks(source, name, memo)
            continue
        texts = _block_label(derived[0], derived[1], name, memo)
        if texts is None:
            return None
        if texts is not _ABSENT:
            found.append(texts)
    if not found:
        return None if opaque else _ABSENT
    if opaque and _merging(select, name):
        return None
    return set().union(*found)


def _block_label(block: Any, listed: list[str] | None, name: str, memo: _LabelMemo) -> Any:
    """派生来源的那块查询（listed 是写的列名表）出的 name 这一列只装着标签时标签的写法；不出这一列为 _ABSENT，出这一列
    但装着别的、认不准为 None。一条 SELECT 按选出项的名字找，没有的经 * 往下找；UNION 这类按位置看每一支，列名取写的
    列名表，没写的取第一支的。"""
    key = (id(block), name)
    if key not in memo:
        # 先记成 None：递归的 CTE 绕回自己时按认不准算
        memo[key] = None
        memo[key] = _block_label_of(block, listed, name, memo)
    return memo[key]


def _block_label_of(block: Any, listed: list[str] | None, name: str, memo: _LabelMemo) -> Any:
    """_block_label 真去找的那一步（不看记下的结果）。"""
    from sqlglot import exp

    leaves = _leaves(block)
    if not all(isinstance(leaf, exp.Select) for leaf in leaves):
        return None
    if listed is None and len(leaves) == 1:
        items = list(leaves[0].expressions)
        named = [i for i, item in enumerate(items) if _same_name(item, name)]
        if not named:
            return _star_label(leaves[0], name, memo)
        # SQLite 让同名的几列都留着，外层按名字取的是排在最前面的那一列：第一个同名项前面的 * 带得出这一列（或说不清带
        # 不带），取到的就是 * 里的那一列（SELECT *, CASE … END AS vip_level 交回的是真实等级）
        if _star_label(leaves[0], name, memo, before=named[0]) is not _ABSENT:
            return None
        return _all_labels([_item_label(items[i], leaves[0], memo) for i in named])
    width: int | None = None
    if listed is not None:
        if name not in listed:
            return _ABSENT
        place, width = (listed.index(name) if listed.count(name) == 1 else None), len(listed)
    else:
        first = list(leaves[0].expressions)
        places = [p for item, p in zip(first, _places(first, None)) if _same_name(item, name)]
        if not places:
            # 第一支没明写这一列：有 * 时说不清它在不在、在哪个位置
            return None if any(_is_star(item) for item in first) else _ABSENT
        place = places[0] if len(places) == 1 else None
    if place is None:
        return None
    texts: list[set[str] | None] = []
    for select in leaves:
        items = list(select.expressions)
        item = next((i for i, p in zip(items, _places(items, width)) if p == place), None)
        texts.append(_item_label(item, select, memo) if item is not None else None)
    return _all_labels(texts)


def _all_labels(texts: list[set[str] | None]) -> set[str] | None:
    """几项都只装着标签时，标签的写法合起来；有一项不是为 None。"""
    return set().union(*texts) if all(t is not None for t in texts) else None


def _from_sources(select: Any) -> list[tuple[str, Any]]:
    """一条 SELECT 的 FROM、JOIN 里的来源：(名字, 来源节点)，名字是别名，没起别名的是表名（小写）。"""
    from_ = select.args.get("from_")
    nodes = [from_.this] if from_ is not None and from_.this is not None else []
    nodes += [join.this for join in select.args.get("joins") or [] if join.this is not None]
    return [((node.alias_or_name or "").lower(), node) for node in nodes]


def _derived_block(source: Any) -> tuple[Any, list[str] | None] | None:
    """FROM、JOIN 里的一个来源 → (它的那块查询, 写的列名表)：FROM 里的子查询、LATERAL、指向 WITH 里一段的表名。数据源里
    的表、VALUES、函数这类为 None（出哪些列、装着什么，SQL 里看不出）。"""
    from sqlglot import exp

    from app.data.sql_semantics import _visible_ctes

    node, alias = source, None
    if isinstance(node, exp.Lateral):
        node, alias = node.this, node.args.get("alias")
    if isinstance(node, exp.Table):
        if node.args.get("db") or node.args.get("catalog") or not isinstance(node.this, exp.Identifier):
            return None
        cte = _visible_ctes(node).get(node.name.lower())
        if cte is None:
            return None
        node, alias = cte.this, cte.args.get("alias")
    while isinstance(node, exp.Subquery):
        alias = alias or node.args.get("alias")
        node = node.this
    if not isinstance(node, (exp.Select, exp.SetOperation)):
        return None
    return node, ([c.name.lower() for c in alias.columns] if alias is not None and alias.columns else None)


#: 表值函数常见的出列名：SQLite 的 json_each / json_tree（key、value、atom、fullkey、path），Postgres 的 jsonb_each
#: （key、value），Spark 的 explode / posexplode（col、pos），Snowflake 的 FLATTEN（key、value、path、index、this）。
#: 结构性的几列（id、parent、type）带不出值，不算——算上的话，同一条 SQL 里别的 id 列都会被遮
_FUNCTION_COLUMNS = frozenset({"key", "value", "atom", "fullkey", "path", "col", "pos", "index", "this"})
#: 出列名认得全的表值函数：出的是上面那几列，或者一列、名字就是别名（没起别名的是函数名）
_TABLE_FUNCTIONS = frozenset({
    "json_each", "json_tree", "jsonb_each", "jsonb_each_text", "json_each_text", "json_array_elements",
    "json_array_elements_text", "jsonb_array_elements", "jsonb_array_elements_text", "json_object_keys",
    "jsonb_object_keys", "unnest", "explode", "explode_outer", "posexplode", "regexp_split_to_table",
    "string_to_table", "string_split", "openjson", "flatten",
})


def _function_sources(select: Any) -> list[Any]:
    """一条 SELECT 的 FROM、JOIN、LATERAL VIEW 里说不清出哪些列、装着什么的来源：不是数据源里的表（含指向 WITH 的名字），
    也不是子查询这类派生来源——表值函数（json_each、unnest、JSON_TABLE）、LATERAL 函数、VALUES。"""
    from sqlglot import exp

    sources = [source for _, source in _from_sources(select)] + list(select.args.get("laterals") or [])
    out: list[Any] = []
    for source in sources:
        if _derived_block(source) is not None:
            continue
        node = source.this if isinstance(source, exp.Lateral) else source
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
            continue
        out.append(source)
    return out


def _function_columns(source: Any) -> set[str] | None:
    """表值函数这类来源出的列名（小写）：写的列名表、别名（Postgres 里一列的函数出的列就叫别名，没起别名的叫函数名）、
    函数名、常见的出列名（_FUNCTION_COLUMNS）；JSON_TABLE 按 COLUMNS 里写的，VALUES 按 column1、column2…。不认得的函数
    又没写列名表的，说不清出哪几列，为 None。"""
    from sqlglot import exp

    node = source.this if isinstance(source, exp.Lateral) else source
    alias = source.args.get("alias") or (node.args.get("alias") if node is not source else None)
    names: set[str] = set(_FUNCTION_COLUMNS)
    listed: list[str] = []
    if alias is not None:
        if alias.name:
            names.add(alias.name.lower())
        listed = [c.name.lower() for c in alias.args.get("columns") or [] if c.name]
        names |= set(listed)
    func = node.this if isinstance(node, exp.Table) else node
    while isinstance(func, exp.Subquery):
        func = func.this
    if isinstance(func, exp.JSONTable):
        schema = func.args.get("schema")
        defined = [str(c.name).lower() for c in (schema.expressions if schema is not None else []) if c.name]
        return names | set(defined) if defined or listed else None
    if isinstance(func, exp.Values):
        width = max((len(row.expressions) for row in func.expressions if isinstance(row, exp.Tuple)), default=1)
        return names | {f"column{i}" for i in range(1, width + 1)}
    name = (func.name if isinstance(func, exp.Anonymous) else func.sql_name() if isinstance(func, exp.Func)
            else "").lower()
    if name:
        names.add(name)
    return names if listed or name in _TABLE_FUNCTIONS else None


def _merging(select: Any, name: str) -> bool:
    """FROM 里按同名的 name 这一列连起来（USING 里写着它，或者 NATURAL）：几个来源的这一列合成一列。"""
    return any(str(join.args.get("method") or "").upper() == "NATURAL"
               or any(str(getattr(u, "name", "")).lower() == name for u in join.args.get("using") or [])
               for join in select.args.get("joins") or [])


def _is_star(item: Any) -> bool:
    from sqlglot import exp

    return isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star))


def _leaves(root: Any) -> list[Any]:
    """一块查询的每一支：UNION 这类连起来的按先后摊开，套在外面的括号去掉。一般是 SELECT，也可能是 VALUES 这类。"""
    from sqlglot import exp

    out: list[Any] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Subquery):
            stack.append(node.this)
        elif isinstance(node, exp.SetOperation):
            stack.extend((node.expression, node.this))
        else:
            out.append(node)
    return out


def _branches(root: Any) -> list[list[Any]]:
    """一块查询选出的列：一条 SELECT 一支，UNION 这类的每一支各一支（结果的第 i 列来自每一支的第 i 项）。"""
    from sqlglot import exp

    return [list(node.expressions) for node in _leaves(root) if isinstance(node, exp.Select)]


def _same_name(item: Any, name: str) -> bool:
    """一个选出项在派生来源的结果里是不是叫 name（小写）：起了别名的、直接选一列的按 _output_name；没起别名、套了括号
    或 COLLATE 的一列（(vip_level)、vip_level COLLATE NOCASE）也算——SQLite 给子查询的结果列起名时去掉括号和 COLLATE，
    外层按 vip_level 取得到它。只在认标签列时用：_output_name 别处照旧认不准这种写法（那边认不准就整份遮）。"""
    from sqlglot import exp

    if _output_name(item) == name:
        return True
    node = item
    while isinstance(node, (exp.Paren, exp.Collate)):
        node = node.this
    return node is not item and isinstance(node, exp.Column) and not isinstance(node.this, exp.Star) \
        and node.name.lower() == name


def _output_name(item: Any) -> str | None:
    """一个选出项在结果里的列名（小写）：起了别名的是别名，直接选一列的是列名。别的表达式（函数、运算、CAST、括号、
    子查询）的列名各数据库起法不同（SQLite 照写法原样记「SUBSTR(buyer_phone, 4)」，Postgres 记函数名或 ?column?），
    认不准，返回 None。"""
    from sqlglot import exp

    if isinstance(item, exp.Alias):
        return item.alias.lower() or None
    if isinstance(item, exp.Column) and not isinstance(item.this, exp.Star):
        return item.name.lower() or None
    return None


def _places(branch: list[Any], width: int | None) -> list[int | None]:
    """一支里每一项落在结果的第几列。没有 * 的一一对上；有 * 的，第一个 * 前面的从头数，最后一个 * 后面的从结果末尾
    倒着数（width 是结果的列数，不知道就数不了）；两个 * 之间的、* 本身认不出位置（None）。"""
    stars = [i for i, item in enumerate(branch) if _is_star(item)]
    if not stars:
        return list(range(len(branch)))
    first, last = stars[0], stars[-1]
    tail = len(branch) - 1 - last
    # * 至少展开成 0 列：倒着数要是数到了 * 前面那几项的位置上，列数对不上，后面的一项都不认
    countable = width is not None and width - tail >= first
    return [i if i < first else width - (len(branch) - i) if i > last and countable else None
            for i in range(len(branch))]


def _blocks(root: Any) -> list[tuple[Any, str, list[str] | None]]:
    """SQL 里的每一块查询（一条 SELECT，或者 UNION 这类连起来的一串）和它的结果流到哪：
    - "result"：最外层，结果就是快照的列；
    - "source"：当成一张表用的——WITH 里的一段、FROM / JOIN 里的子查询、LATERAL，外层按列名取它的列，* 会把它的列
      全带出去。写了列名表的（t(p)）带上列名表；
    - "other"：标量子查询、IN / EXISTS 里的，值经外层的表达式带出（_value_columns 已经算上），列名无关紧要。
    套在外面的括号（可能好几层）不改变结果流到哪：先越过它们再看。UNION 里的一支不另算，整串按一块。"""
    from sqlglot import exp

    out: list[tuple[Any, str, list[str] | None]] = []
    for node in root.find_all(exp.Select, exp.SetOperation):
        wrappers: list[Any] = []
        outer = node
        while isinstance(outer.parent, exp.Subquery):
            outer = outer.parent
            wrappers.append(outer)
        parent = outer.parent
        if isinstance(parent, exp.SetOperation):
            continue
        if parent is None:
            out.append((node, "result", None))
        elif isinstance(parent, (exp.CTE, exp.Lateral)) or (wrappers and isinstance(parent, (exp.From, exp.Join))):
            # 别名（和列名表）记在 WITH 的那一段、LATERAL 上，FROM 里的子查询记在带别名的那层括号上
            alias = next((h.args["alias"] for h in [parent, *reversed(wrappers)] if h.args.get("alias")), None)
            listed = [c.name.lower() for c in alias.columns] if alias is not None and alias.columns else None
            out.append((node, "source", listed))
        else:
            out.append((node, "other", None))
    return out


def _source_names(top: Any, listed: list[str] | None, names: set[str]) -> tuple[set[str], bool]:
    """派生来源的结果里值取自遮罩列的那几列的名字，以及有没有认不出名字的（没起别名的表达式、两个 * 之间的项）。
    写了列名表的按位置对上列名表；没写的，单独一条 SELECT 取各项自己的列名，UNION 这类按位置对上第一支的列名。"""
    branches = _branches(top)
    found: set[str] = set()
    unknown = False

    def take(name: str | None) -> None:
        nonlocal unknown
        if name is None:
            unknown = True
        else:
            found.add(name)

    if listed is not None:
        for branch in branches:
            for item, place in zip(branch, _places(branch, len(listed))):
                if names & _value_columns(item):
                    take(listed[place] if place is not None and place < len(listed) else None)
        return found, unknown
    first = branches[0] if branches else []
    for item in first:
        if names & _value_columns(item):
            take(_output_name(item))
    if len(branches) > 1:
        # 每一支一样宽：拿一支没有 * 的定宽度，* 后面的项才数得出位置
        width = next((len(b) for b in branches if not any(_is_star(i) for i in b)), None)
        heads = {place: _output_name(item) for item, place in zip(first, _places(first, width)) if place is not None}
        for branch in branches[1:]:
            for item, place in zip(branch, _places(branch, width)):
                if names & _value_columns(item):
                    take(heads.get(place) if place is not None else None)
    return found, unknown


def _tainted(sql: str, kind: str | None, masked: set[str], width: int) -> tuple[set[str], set[int]] | None:
    """SQL 里指代遮罩列的名字（小写），和结果（width 列）里值取自遮罩列的那几列的位置。解析不了返回 None。

    名字从遮罩列本身起，一层层传下去：选出列的值取自这些名字的（buyer_phone AS 手机号、UPPER(buyer_phone) AS x），它的
    名字也算；派生来源（子查询、WITH 里的一段）按位置对上列名表、UNION 第一支的列名，也算，直到不再多出名字。同名
    不同层的也一并算上（宁可多遮）。
    位置是给没起别名的表达式用的（UPPER(buyer_phone) 的结果列名对不上任何名字）：和 * 写在同一支里的，* 前面的从头数、
    后面的从末尾倒着数。认不出的——两个 * 之间的项、派生来源里没起别名的表达式（外层的 * 会把它原样带出来，列名
    又认不准）——整份快照的列都算（同解析不了）。
    FROM、JOIN、LATERAL 里的表值函数（json_each、unnest、JSON_TABLE、LATERAL VALUES）参数里写着这些名字的，它出的列
    都算（_function_columns）：遮罩列是 JSON 时，json_each 拆开是最自然的写法。出哪几列说不清的整份快照都算。"""
    from sqlglot import exp

    from app.data.sql_semantics import _parse, _read

    try:
        root, _ = _parse(sql, _read(kind))
    except Exception:  # noqa: BLE001 - 解析器的意外按解析不了处理
        root = None
    if root is None:
        return None
    try:
        names = set(masked)
        selects = list(root.find_all(exp.Select))
        blocks = _blocks(root)
        sources = [(top, listed) for top, role, listed in blocks if role == "source"]
        functions = [source for select_ in selects for source in _function_sources(select_)]
        unreadable = False
        for _ in range(len(selects) + len(sources) + len(functions) + 1):
            before = len(names)
            for select_ in selects:
                for item in select_.expressions:
                    name = item.alias_or_name.lower()
                    if name and name not in names and names & _value_columns(item):
                        names.add(name)
            for top, listed in sources:
                names |= _source_names(top, listed, names)[0]
            for source in functions:
                if names & _value_columns(source):
                    columns = _function_columns(source)
                    unreadable = unreadable or columns is None
                    names |= columns or set()
            if len(names) == before:
                break
        everything = unreadable or any(_source_names(top, listed, names)[1] for top, listed in sources)
        positions: set[int] = set()
        for top, role, _listed in blocks:
            if role != "result":
                continue
            for branch in _branches(top):
                for item, place in zip(branch, _places(branch, width)):
                    if names & _value_columns(item):
                        if place is None or place >= width:
                            everything = True
                        else:
                            positions.add(place)
        return names, set(range(width)) if everything else positions
    except Exception:  # noqa: BLE001 - 分析本身的缺陷不能让回答出错：按解析不了处理（宁可多遮）
        logger.warning("质询的 SQL 遮罩分析失败", exc_info=True)
        return None


def _mentions(sql: str, names: Iterable[str]) -> bool:
    return any(re.search(rf"(?<![0-9A-Za-z_]){re.escape(n)}(?![0-9A-Za-z_])", sql, re.I) for n in names if n)


def _derived_hidden(snapshot: dict[str, Any], hidden: set[str], kind: str | None) -> tuple[set[str], set[str]]:
    """(这份快照要遮的列, SQL 里指代遮罩列的名字)，都是小写。

    证据面板只按列名遮（hidden）；agent 的查询起了别名、套了函数，结果列名就对不上。本仓库的查询惯例是起中文别名
    （region AS 区域），所以按 SQL 认出改了名字的遮罩列（_tainted）。SQL 解析不了、里面又写着遮罩列的，整份快照的
    列都遮掉；合并查询的快照不看 SQL，它记着自己的来历（_hidden_columns 已按 _merge_masks 遮好）。"""
    base = set(hidden)
    sql = snapshot.get("sql")
    if not base or not isinstance(sql, str) or not sql.strip() or snapshot.get("source") == MERGE_SOURCE:
        return base, base
    columns = [str(c) for c in snapshot.get("columns") or []]
    found = _tainted(sql, kind, base, len(columns))
    if found is None:
        return (base | {c.lower() for c in columns}, base) if _mentions(sql, base) else (base, base)
    names, positions = found
    return names | {columns[i].lower() for i in positions if i < len(columns)}, names


def derived_columns(snapshot: dict[str, Any], hidden: set[str], kind: str | None) -> set[str] | None:
    """按 SQL 认出值取自遮罩列（hidden，小写）的结果列（小写，列名直接对上的也算）。SQL 解析不了、合并查询的快照（不看
    SQL）为 None：认不准，宽严由调用方定（_derived_hidden 整份遮；双盲复算照常派盲算，由盲算自己的查询去认）。"""
    sql = snapshot.get("sql")
    if not hidden or not isinstance(sql, str) or not sql.strip() or snapshot.get("source") == MERGE_SOURCE:
        return None
    columns = [str(c) for c in snapshot.get("columns") or []]
    found = _tainted(sql, kind, set(hidden), len(columns))
    if found is None:
        return None
    names, positions = found
    return {c.lower() for i, c in enumerate(columns) if c.lower() in names or i in positions}


def _snapshot_labels(snapshot: dict[str, Any], kind: str | None, tables: dict[str, set[str]] | None = None
                     ) -> dict[str, set[str]]:
    """一份快照里只装着分档标签的列（_label_columns）。没有 SQL 的（合并查询）一个都不算。tables 没给的，按快照记着
    的表结构快照（schema_artifact，查询当时冻结的）认。"""
    sql = snapshot.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return {}
    if tables is None and isinstance(snapshot.get("schema_artifact"), str):
        tables = _frozen_tables(snapshot["schema_artifact"])
    return _label_columns(sql, kind, [str(c) for c in snapshot.get("columns") or []], tables)


@functools.lru_cache(maxsize=64)
def _frozen_tables(artifact: str) -> dict[str, set[str]] | None:
    """表结构快照工件里每张表的列名（_schema_tables）。工件按内容寻址、不会变，记下来；取不到、被改过的为 None。"""
    from app.core import artifact_store

    try:
        return _schema_tables(artifact_store.load(artifact))
    except Exception:  # noqa: BLE001 - 取不到就当不知道表结构（标签照旧当遮罩值，宁可多遮）
        return None


# --------------------------------------------------------------------------
# SQL 里写死的值
# --------------------------------------------------------------------------


#: 一个纯粹的数（可带正负号、小数）：范围边界按数比大小，写成字符串的也一样
_PLAIN_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _arithmetic() -> tuple[Any, ...]:
    """加减乘除、取模（%、MOD）、整除（MySQL 的 DIV、DuckDB 的 //）：几个写死的数算出来的整个算式按算出来的那一个值认
    （13900000000 + 1 就是 13900000001，125 % 100 就是 25）。"""
    from sqlglot import exp

    return exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv


#: 数的写法里指数最大多少（1e999999 这样的写法不按数认，免得展开成上百万位）
_EXPONENT_MAX = 40


def _decimal(text: str) -> Decimal | None:
    """一段字按数读（025、2.5e1、13900000001.0 都认）；读不了、不是有限的数、指数太大为 None。"""
    try:
        value = Decimal(text.strip())
    except (ArithmeticError, ValueError):
        return None
    return value if value.is_finite() and abs(value.adjusted()) <= _EXPONENT_MAX else None


def _canonical(text: str) -> str | None:
    """一个数的规范写法（_point_text）：25.0、025、2.5e1 都是 25，13900000001.0 是 13900000001。SQL 里换个写法写同一个
    数，照样认得出是不是遮罩值（_swap_literal）。不是数为 None。"""
    value = _decimal(text)
    return _point_text(value) if value is not None else None


def _constant(node: Any) -> Any:
    """写死的值：数按数（几个数加减乘除算出来的也算，13900000002 - 1；025、2.5e1 这样的写法也按数），字符串按原文
    （纯粹的数照样按数）；别的为 None。"""
    return _constants(node).get(id(node)) if node is not None else None


def _constants(root: Any) -> dict[int, Any]:
    """一棵语法树里每个写死的值（_constant）：id(节点) → 值。从下往上一层层算（广度优先的倒序，孩子总在父亲前面），
    不递归：上千项的连加（1+1+…）会超过递归深度，以前整条 SQL 因此退回按词认，同一句里夹住的值就认不出了。"""
    from sqlglot import exp

    out: dict[int, Any] = {}
    for node in reversed(list(root.walk())):
        value: Any = None
        if isinstance(node, exp.Neg):
            inner = out.get(id(node.this))
            value = -inner if isinstance(inner, Decimal) else None
        elif isinstance(node, (exp.Paren, exp.Cast, exp.Collate)):
            value = out.get(id(node.this))
        elif isinstance(node, exp.Introducer):
            value = out.get(id(node.expression))
        elif isinstance(node, _literal_types()):
            text = str(node.this).strip()
            number = Decimal(text) if _PLAIN_NUMBER.fullmatch(text) else (
                _decimal(text) if isinstance(node, exp.Literal) and not node.is_string else None)
            value = number if number is not None else text
        elif isinstance(node, _arithmetic()):
            value = _worked_out(node, out.get(id(node.this)), out.get(id(node.expression)))
        if value is not None:
            out[id(node)] = value
    return out


def _worked_out(node: Any, a: Any, b: Any) -> Decimal | None:
    """一步加减乘除、取模、整除的结果；不是两个数、除以零、结果太大为 None。取模、整除照 SQL 往零取整（-7 % 2 是 -1，
    -7 DIV 2 是 -3，同 Decimal 的 % 和 //）。/ 在 SQLite、Postgres 这类方言里两个整数相除只留整数部分（解析时记在
    typed 上：53 / 2 是 26），写成小数的（53.0 / 2）、别的方言照小数算。以前一律按小数算，SQLite 里 age >= 25 AND
    age < 53 / 2 夹住了 25 却认不出。已知局限：SQLite 的取模先把两边取整再算（25.9 % 26 是 25），这里照小数算
    （25.9），这样写的值认不出。"""
    from sqlglot import exp

    if not (isinstance(a, Decimal) and isinstance(b, Decimal)):
        return None
    try:
        if isinstance(node, (exp.Div, exp.IntDiv, exp.Mod)):
            if not b:
                return None
            # 写成整数的数（和它们加减乘出来的）指数正好是 0；53.0、1e1 这样写的在 SQL 里不是整数
            whole = isinstance(node, exp.IntDiv) or (
                node.args.get("typed") and a.as_tuple().exponent == 0 and b.as_tuple().exponent == 0)
            value = a % b if isinstance(node, exp.Mod) else a // b if whole else a / b
        else:
            value = a + b if isinstance(node, exp.Add) else a - b if isinstance(node, exp.Sub) else a * b
    except ArithmeticError:
        return None
    return value if value.is_finite() and abs(value.adjusted()) <= _EXPONENT_MAX else None


def _literal_types() -> tuple[Any, ...]:
    """语法树里的字面量：数字、字符串，以及字符串带前缀的几种变体（N'…'、$$…$$、U&'…'、E'…'、X'…'、B'…'）。"""
    from sqlglot import exp

    return (exp.Literal, exp.National, exp.RawString, exp.UnicodeString, exp.ByteString, exp.HexString, exp.BitString)


def _point_text(point: Any) -> str:
    """夹住的值写成格子里的样子（25、13900000001、12.5），拿去和遮罩列里的值比（_swap_literal）。"""
    if isinstance(point, Decimal):
        return format(point.to_integral_value() if point == point.to_integral_value() else point.normalize(), "f")
    return str(point)


# --------------------------------------------------------------------------
# 遮罩：哪些值
# --------------------------------------------------------------------------


#: 一个数的写法：可带正负号、千分位、小数、百分号
_NUMERIC = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
#: 正文里一个个「词」：字母数字下划线连成的一串。头尾都是这种字的遮罩值，只能整个对上这样一串
_WORDS = re.compile(r"[0-9A-Za-z_]+")
#: 正文里的一串数（可带千分位、小数）：带千分位的遮罩值按去掉逗号的样子比
_DIGITS = re.compile(r"\d[\d,]*(?:\.\d+)?")
#: 中文这类遮罩值按开头几个字分桶找（_Secrets._partition）
_PREFIX = 8
#: 正文里一串允许夹分隔符的数字（换了写法的号码，见 _Secrets.loose_spans）：数字（含全角）之间夹着一到四个分隔符，开头
#: 可以带 + 和括号。分隔符是同一行里的各种空白（空格、制表符、全角空格、不换行空格，以及窄空格、细空格这类 U+2000 至
#: U+200B、U+202F、U+205F）、连字符和减号（含全角、U+2010 至 U+2015、U+2212）、点（含全角、间隔号）、括号（含全角）。
#: 逗号不算分隔符（千分位按 _DIGITS 认），冒号、斜杠也不算（时间、日期），不跨行
_LOOSE_SEPARATORS = (" \t\u00a0\u2000-\u200b\u202f\u205f\u3000"
                     "\\-\u2010-\u2015\u2212\uff0d.\uff0e\u00b7\u30fb()\uff08\uff09")
_LOOSE_RUN = re.compile(rf"[(（]?[+＋]?[(（]?[0-9０-９]+(?:[{_LOOSE_SEPARATORS}]{{1,4}}[0-9０-９]+)*[)）]?")
_LOOSE_DIGITS = re.compile(r"[0-9０-９]+")
#: 两截数字之间只隔着这样一个字（不带空格）：是更长的编号、小数、日期的两截（13900000001-2、13900000001.5），号码到这里
#: 不算完——同 _pattern 里「前后不能是 [.:/-] 加数字」。减号（U+2212）、全角连字符同连字符
_JOINERS = frozenset("-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d.\uff0e")
_POINTS = frozenset(".．")
#: 去掉国家码（0086、86）之后至少还要有这么多位才算号码（固话号码最短 7 位）：86 开头的短数不拆
_LOCAL_MIN = 7
_OPENS, _CLOSES = frozenset("(（"), frozenset(")）")
_HALF_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _plain_text(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)
    return re.sub(r"\s+", " ", text).strip()


def _numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float)) or (isinstance(value, str) and numeric_text(value) is not None)


def _forms(value: Any) -> set[str]:
    """一格的值在正文里可能的写法：原样（压成一行），是数的再加上千分位的写法（13,900,000,001）。"""
    text = _plain_text(value)
    forms = {text} if text else set()
    if _numeric(value):
        with contextlib.suppress(RenderError, ValueError, ArithmeticError):
            forms.add(render_cell(value))
    return forms


def _strong(form: str) -> bool:
    """按值认得出的写法（见 SECRET_MIN、SECRET_DIGITS）。"""
    if _NUMERIC.fullmatch(form):
        return sum(c.isdigit() for c in form) >= SECRET_DIGITS
    return len(form) >= SECRET_MIN


def _digits_of(form: str) -> str:
    return form.replace(",", "").lstrip("+-").rstrip("%")


def _loose_candidates(digits: str) -> list[str]:
    """一串数字（半角）可能是哪个号码的纯数字写法：原样；以国家码（0086、86）开头、去掉之后还有 _LOCAL_MIN 位以上的，
    去掉国家码的也算。"""
    out = [digits]
    for code in ("0086", "86"):
        if digits.startswith(code) and len(digits) - len(code) >= _LOCAL_MIN:
            out.append(digits[len(code):])
    return out


def _loose_keys(form: str) -> list[str]:
    """一个按值认得出的遮罩值的纯数字写法（换了写法的号码按它认，见 _Secrets.loose_spans）：整个是一个整数（可带千分位、
    正负号、全角数字）的取它的数字；整个是夹着分隔符的数字串（存的就是 199-1474-2785、+86 199 1474 2785）的取规范化
    之后的数字；以国家码开头的另加去掉国家码的。带小数的、不到 SECRET_DIGITS 位的、别的写法没有。"""
    if _NUMERIC.fullmatch(form):
        digits = _digits_of(form).translate(_HALF_DIGITS)
    elif _LOOSE_RUN.fullmatch(form):
        digits = "".join(_LOOSE_DIGITS.findall(form)).translate(_HALF_DIGITS)
    else:
        return []
    if not (digits.isascii() and digits.isdigit()) or len(digits) < SECRET_DIGITS:
        return []
    return _loose_candidates(digits)


def _loose_whole(text: str, run: re.Match[str], groups: list[tuple[int, int, str]], i: int, j: int) -> bool:
    """一串数字里第 i 到第 j 截拼起来是一个完整的号码，不是更长的编号、小数、日期的一截：两头不和别的数字只隔着一个
    连字符或点（13900000001-2、13900000001.5、2026-10-06），只有两截、中间是一个点的按小数算（19914.74）；这一串
    前后不紧挨着字母数字（ID13900000001），同 _pattern 的词边界。"""
    def gap(a: int, b: int) -> str:
        return text[groups[a][1]:groups[b][0]]

    if (i > 0 and gap(i - 1, i) in _JOINERS) or (j < len(groups) - 1 and gap(j, j + 1) in _JOINERS):
        return False
    if j == i + 1 and gap(i, j) in _POINTS:
        return False
    before = text[run.start() - 1] if i == 0 and run.start() > 0 else ""
    after = text[run.end()] if j == len(groups) - 1 and run.end() < len(text) else ""
    return not (_WORD.match(before) or _WORD.match(after))


def _loose_span(text: str, run: re.Match[str], groups: list[tuple[int, int, str]], i: int, j: int) -> tuple[int, int]:
    """第 i 到第 j 截这个号码在 text 里换掉的那一段：从第一截到最后一截，紧挨在前面的 + 一起换。括号只换号码里的：号码
    里有落单的右括号（(199)1474-2785、(+86)199…），把前面配对的左括号一起换掉；落单的左括号，把后面紧跟着的右括号一起
    换掉。整个号码外面套着的一对括号是标点（「来自（13900000001）」），留着不换。"""
    start, end = groups[i][0], groups[j][1]
    if i == 0 and start > run.start() and text[start - 1] in "+＋":
        start -= 1
    depth = sum((c in _OPENS) - (c in _CLOSES) for c in text[start:end])
    while depth < 0 and start > 0 and text[start - 1] in _OPENS:
        start, depth = start - 1, depth + 1
    while depth > 0 and end < len(text) and text[end] in _CLOSES:
        end, depth = end + 1, depth - 1
    return start, end


def _pattern(value: str) -> str:
    """一个值的正则：头尾是字母数字的按词边界认；数字还不能是更长的数、日期、时间的一截（12.5 里的 2.5、2026-01-25 里的
    25、12:30 里的 30），也不能是千分位写法里的一组（1,390 里的 390）。逗号隔开的一串长号码（13900000001,13900000002）
    各自照认：千分位一组只有三位。"""
    head = tail = ""
    if _WORD.match(value[0]):
        head = r"(?<![0-9A-Za-z_])"
        if value[0].isdigit():
            head += r"(?<!\d[.:/-])" + (r"(?<!\d,)" if len(re.match(r"\d*", value).group()) == 3 else "")
    if _WORD.match(value[-1]):
        tail = r"(?![0-9A-Za-z_])"
        if value[-1].isdigit():
            tail += r"(?![.:/-]\d)(?!,\d{3}(?!\d))"
    return head + re.escape(value) + tail


@dataclass
class _Secrets:
    """遮罩列里出现过的值。模型照着报告正文、提问、别处的文字格里看到的值复述、或者把值写进查询条件，都会把它带进
    回答正文和查询清单；按值找出来换成「已遮罩」。核查 agent 自己的查询结果在工具边界就遮好了（mask_query_result），
    这里是第二道防线。这只减少暴露，不是安全边界（同证据面板的遮罩）。

    strong 是按值认得出的写法（够长的数、两个字以上的文字），正文、SQL 里的字面量、名字和注释、别的列里的格子、列名都按
    它换；weak 是遮罩列里的全部写法（含短的数），只用在 SQL 里和遮罩列做等值比较的字面量上；shown 是 SQL 里照原样
    留着的阈值、行数（去掉千分位、正负号的数字），只在回答正文里不换（scrub 的 prose）。换的时候先按词、按开头两个
    字筛出正文里真有的那几个值，再拿它们现编正则：遮罩值上万个时，现编一个上万分支的正则要一秒上下，每次都卡住整个进程。
    正文、文字格里另认换了写法的长号码（199-1474-2785、+86 199 1474 2785、全角数字，见 loose_spans）：按纯数字写法
    在集合里查，不进正则。protected 是 protect 收下的、已经摆在读者眼前的文字：之后并进来的值（核查 agent 后面的
    查询遮掉的）照样按它们免遮。"""

    strong: set[str] = field(default_factory=set)
    weak: set[str] = field(default_factory=set)
    shown: set[str] = field(default_factory=set)
    protected: tuple[str, ...] = ()
    _index: Any = field(default=None, repr=False, compare=False)
    _compiled: dict[frozenset[str], re.Pattern[str]] = field(default_factory=dict, repr=False, compare=False)

    def add(self, snapshot: Any, hidden: set[str], *, labels: dict[str, set[str]] | None = None,
            base: Iterable[str] = ()) -> None:
        """收一份快照里要遮的列（hidden，小写）的值。labels 是这份快照里只装着分档标签的列（_label_columns）：这几列的
        格子等于标签的不收；改了名字的遮罩列、别的派生列照收，base（数据源设了遮罩的列，小写）自己选出来的值也照收。"""
        if not _query_shaped(snapshot) or not hidden:
            return
        own = set(base)
        written = {name: texts for name, texts in (labels or {}).items() if name not in own}
        columns = [str(c) for c in snapshot.get("columns") or []]
        index = [i for i, c in enumerate(columns) if c.lower() in hidden]
        # 核查 agent 的查询快照在工具边界就遮好了（mask_query_result）：遮罩列的格子是「已遮罩」，不是遮罩值
        word = _masked_word()
        for row in snapshot["rows"]:
            if len(self.weak) >= SECRETS_MAX:
                break
            if isinstance(row, list):
                cells = [(columns[i].lower(), row[i]) for i in index if i < len(row)]
            elif isinstance(row, dict):
                cells = [(str(k).lower(), v) for k, v in row.items() if str(k).lower() in hidden]
            else:
                continue
            for name, value in cells:
                if value is None or isinstance(value, bool) or value == word:
                    continue
                forms = _forms(value)
                if forms & written.get(name, set()):
                    continue
                for form in forms:
                    self.weak.add(form)
                    if _strong(form):
                        self.strong.add(form)
        self._index = None

    def merge(self, other: _Secrets | None) -> None:
        """并进另一份收好的遮罩值（核查 agent 查询时在工具边界遮掉的，见 mask_query_result）。合起来最多 SECRETS_MAX 种。
        两边免遮的文字合起来，并进来的值照样按它们免遮：报告的指标正好等于某一笔金额时，agent 查出了那一笔，别的查询里
        正好是这个数的格子也不遮。"""
        if other is None:
            return
        for form in other.weak:
            if len(self.weak) >= SECRETS_MAX:
                break
            self.weak.add(form)
            if form in other.strong:
                self.strong.add(form)
        self.protected = tuple(dict.fromkeys((*self.protected, *other.protected)))
        self._index, self._compiled = None, {}
        if self.protected:
            self._spare(" ".join(self.protected))

    def protect(self, texts: Iterable[str | None]) -> None:
        """已经摆在读者眼前的值不换：提问人自己写进问题、选中原文里的（「13900000001 这位客户」「排除 300 以下的」），
        报告里列出的指标、运行输入的值——换了只会让回答、查询对不上问题和报告。正文、格子、SQL 里都不换。文字记下来，
        之后并进来的值照样免遮（merge）。"""
        given = " ".join(t for t in texts if t)
        if not given:
            return
        self.protected = tuple(dict.fromkeys((*self.protected, given)))
        self._spare(given)

    def _spare(self, given: str) -> None:
        """去掉 given（几段免遮的文字）里写着的值。"""
        numbers = {_digits_of(n) for n in _DIGITS.findall(given)}
        # 换了写法写进来的号码（「199-1474-2785 这位客户」）按纯数字写法认
        reformatted = {key for _, _, key in self.loose_spans(given)}

        def asked(form: str) -> bool:
            if _digits_of(form) in numbers if _NUMERIC.fullmatch(form) else form in given:
                return True
            return bool(reformatted) and any(key in reformatted for key in _loose_keys(form))

        self.strong = {f for f in self.strong if not asked(f)}
        self.weak = {f for f in self.weak if not asked(f)}
        self._index, self._compiled = None, {}

    def show(self, numbers: Iterable[str]) -> None:
        """SQL 里照原样留着的阈值、行数（_shown_literals）：回答正文里复述它们不换（「金额不低于 30000 元」），换了只会
        让回答和 SQL 对不上。只免正文：格子、列名、SQL 里照旧按值换——格子里的值正好是这个数，可能就是那个人
        （col >= v 再按 col 排序取第一行，查出来的就是 v）。"""
        self.shown = set(numbers)

    def is_shown(self, form: str) -> bool:
        return bool(_NUMERIC.fullmatch(form)) and _digits_of(form) in self.shown

    def hit(self, value: Any) -> bool:
        """一格（或 SQL 里一个字面量）的值是不是按值认得出的遮罩值。短的数不算（见 SECRET_DIGITS）。整格就是一个换了
        写法的号码的（没设遮罩的联系方式列里写着 199-1474-2785）也算。"""
        if value is None or isinstance(value, bool) or not self.strong:
            return False
        text = _plain_text(value)
        if text in self.strong:
            return True
        if _numeric(value) and sum(c.isdigit() for c in text) >= SECRET_DIGITS:
            with contextlib.suppress(RenderError, ValueError, ArithmeticError):
                if render_cell(value) in self.strong:
                    return True
        return isinstance(value, str) and any(start == 0 and end == len(text) for start, end, _ in self.loose_spans(text))

    def weak_hit(self, text: str) -> bool:
        """SQL 里一个字面量是不是遮罩列里出现过的值（短的数也算）。"""
        return _plain_text(text) in self.weak

    def scrub(self, text: str, *, prose: bool = False, loose: bool | None = None) -> str:
        """text 里的遮罩值换成「已遮罩」。prose：这是回答正文，SQL 里照原样留着的阈值、行数不换（show）。loose：换了写法
        的长号码也换（loose_spans），缺省同 prose——正文、文字格、SQL 的字符串和注释里这样认，SQL 里不带引号的
        199-1474-2785 是一个算式。"""
        return self.swap(text, prose=prose, loose=loose)[0]

    def swap(self, text: str, *, prose: bool = False, loose: bool | None = None) -> tuple[str, list[str]]:
        """scrub，另交回换掉了哪些值：按值认出的照原文的写法，换了写法的号码交它的纯数字写法（SQL 里同一个数写成阈值的
        据此不留，见 inquiry._swap_literal）。"""
        hits = self._hits(text, prose=prose, loose=loose)
        word = _masked_word()
        for start, end, _ in reversed(hits):
            text = text[:start] + word + text[end:]
        return text, [value for _, _, value in hits]

    def spans(self, text: str, *, prose: bool = False, loose: bool | None = None) -> list[tuple[int, int]]:
        """scrub 会换掉的那几段（起, 止），按先后、不重叠。质询工件按位置重放回答时换掉的地方（view_edits）。"""
        return [(start, end) for start, end, _ in self._hits(text, prose=prose, loose=loose)]

    def _hits(self, text: str, *, prose: bool, loose: bool | None) -> list[tuple[int, int, str]]:
        """text 里要换掉的遮罩值：(起, 止, 值)，按先后、不重叠。换了写法的号码先认（+86 19914742785 整个换掉，不留下
        一截「+86」），按值认的落在它们里面的不再算。"""
        if not text or not self.strong:
            return []
        hits = [(start, end, key) for start, end, key in self.loose_spans(text, prose=prose)] \
            if (prose if loose is None else loose) else []
        pattern = self.pattern(text, prose=prose)
        if pattern is not None:
            taken = list(hits)
            hits += [(m.start(), m.end(), m.group()) for m in pattern.finditer(text)
                     if not any(m.start() < end and start < m.end() for start, end, _ in taken)]
        return sorted(hits)

    def copy(self) -> _Secrets:
        """另一份一样的遮罩值（改它不影响这一份）：交给线程、按句另加免遮的值时用。"""
        return _Secrets(strong=set(self.strong), weak=set(self.weak), shown=set(self.shown), protected=self.protected)

    def loose_spans(self, text: str, *, prose: bool = False) -> list[tuple[int, int, str]]:
        """text 里换了写法的长号码（199-1474-2785、(199)1474-2785、+86 199 1474 2785、全角数字……）：(起, 止, 纯数字
        写法)，按先后、不重叠。在正文里找允许夹分隔符的数字串（_LOOSE_RUN），从每一截起往后拼，规范化（全角转半角、去
        分隔符、去国家码）之后在遮罩值的纯数字写法里查（集合查找，不现编正则），取拼得最长的那个；拼出来的不是完整的
        号码（更长的编号、小数、日期的一截，见 _loose_whole）不算。prose 同 scrub：SQL 里照原样留着的阈值不算。"""
        if not text or not self.strong:
            return []
        keys, longest = self._partition()[3]
        if not keys:
            return []
        out: list[tuple[int, int, str]] = []
        for run in _LOOSE_RUN.finditer(text):
            groups = [(run.start() + g.start(), run.start() + g.end(), g.group().translate(_HALF_DIGITS))
                      for g in _LOOSE_DIGITS.finditer(run.group())]
            i = 0
            while i < len(groups):
                best: tuple[int, str] | None = None
                digits = ""
                for j in range(i, len(groups)):
                    digits += groups[j][2]
                    if len(digits) > longest + 4:     # 再拼也对不上任何一个了（4 是国家码 0086 的长度）
                        break
                    key = next((c for c in _loose_candidates(digits) if c in keys), None)
                    if key is not None and _loose_whole(text, run, groups, i, j):
                        best = (j, key)
                if best is None:
                    i += 1
                    continue
                j, key = best
                if not (prose and key in self.shown):
                    out.append((*_loose_span(text, run, groups, i, j), key))
                i = j + 1
        return out

    def pattern(self, text: str, *, prose: bool = False) -> re.Pattern[str] | None:
        """认 text 里出现的遮罩值的正则；一个都没有时为 None。prose 同 scrub。"""
        found = self._found(text) if text and self.strong else set()
        if prose and self.shown:
            found = {v for v in found if not self.is_shown(v)}
        if not found:
            return None
        key = frozenset(found)
        if key not in self._compiled:
            if len(self._compiled) > 64:
                self._compiled = {}
            # 长的在前：13900000001 先于它的一截被认出来
            self._compiled[key] = re.compile("|".join(_pattern(v) for v in sorted(found, key=lambda v: (-len(v), v))))
        return self._compiled[key]

    def _found(self, text: str) -> set[str]:
        words, numbers, others, _ = self._partition()
        found = {w for w in _WORDS.findall(text) if w in words}
        if numbers:
            for run in _DIGITS.findall(text):
                found |= numbers.get(_digits_of(run), set())
        for i in range(len(text) - 1) if others else ():
            for size, buckets in others.items():
                for length, values in buckets.get(text[i:i + size], {}).items():
                    if (piece := text[i:i + length]) in values:
                        found.add(piece)
        return found

    def prepare(self) -> None:
        """先把索引建好（收完值、去掉提问人写的之后）：上万个值建一次要几十毫秒，放在线程里建，别等到事件循环里第一次换。"""
        self._partition()

    def _partition(self) -> tuple[set[str], dict[str, set[str]], dict[int, dict[str, dict[int, set[str]]]],
                                  tuple[frozenset[str], int]]:
        """strong 分成三类：只有字母数字的（按词比）、带千分位小数的数（按去掉逗号的数比）、其余的（中文、日期、
        邮箱）。其余的按开头至多 _PREFIX 个字分桶、桶里再按长短分：同一个市区的上万个地址开头七八个字都一样，只按开头
        分的话，正文里每碰到一次那几个字都要逐个比；再按长短分，碰到一次只是按几种长短各查一下。三类各自只在正文里
        真出现时才进正则。另记长号码的纯数字写法和其中最长的有几位（_loose_keys，loose_spans 按它查）。"""
        if self._index is None:
            words: set[str] = set()
            numbers: dict[str, set[str]] = {}
            others: dict[int, dict[str, dict[int, set[str]]]] = {}
            loose: set[str] = set()
            for value in self.strong:
                if _WORDS.fullmatch(value):
                    words.add(value)
                elif _NUMERIC.fullmatch(value):
                    numbers.setdefault(_digits_of(value), set()).add(value)
                else:
                    size = min(len(value), _PREFIX)
                    others.setdefault(size, {}).setdefault(value[:size], {}).setdefault(len(value), set()).add(value)
                loose.update(_loose_keys(value))
            self._index = (words, numbers, others, (frozenset(loose), max(map(len, loose), default=0)))
        return self._index


#: 单元格引用的前一截（Q1.r0.）：后面才是列名
_CELL_HEAD = re.compile(r"^\s*[QK]\d+\.r\d+\.")


def _scrubbed_source(text: str, secrets: _Secrets) -> str:
    """回答原文里的遮罩值换成「已遮罩」：标记之外按正文的办法换（SQL 里照原样留着的阈值不换）；标记里的按段换（| 隔开
    的几段各自换），单元格引用只换列名那一截——编号、行号换不出东西，整段换的话「r0.」会让列名开头的长号码看着像小数
    的一截，反而认不出来。列名照快照、SQL 里的名字那样换（阈值也换，换了写法的号码也认），换得一样才对得上。
    标记里写着遮罩值的引用换完多半解析不了（列名里写着遮罩值的除外：快照的列名也换过了，正好对上），宁可弄坏一个
    引用，也不让值留在存下的原文、片段里，或者从解析不了的占位里露出来。"""
    if not text or not secrets.strong:
        return text
    out: list[str] = []
    last = 0
    for m in MARKER_RE.finditer(text):
        out.append(secrets.scrub(text[last:m.start()], prose=True))
        start, end = m.span("body")
        parts = text[start:end].split("|")
        head = _CELL_HEAD.match(parts[0]) if m.group("kind").strip() == "v" else None
        first = parts[0][:head.end()] + secrets.scrub(parts[0][head.end():], loose=True) if head \
            else secrets.scrub(parts[0], prose=True)
        parts = [first, *(secrets.scrub(p, prose=True) for p in parts[1:])]
        out.append(text[m.start():start] + "|".join(parts) + text[end:m.end()])
        last = m.end()
    out.append(secrets.scrub(text[last:], prose=True))
    return "".join(out)


def _masked_view(snapshot: dict[str, Any], hidden: set[str], secrets: _Secrets, *, embedded: bool = False,
                 rename: bool = True) -> tuple[dict[str, Any], list[str]]:
    """一份查询快照遮好的样子和遮掉的列：要遮的列（hidden，小写）每一格都换成「已遮罩」、类型记成文本（渲染时不当数）；
    别的列里写着按值认得出的遮罩值的格子也换（另一张表里没设遮罩的同一个手机号、改了名字没认出来的遮罩列）。
    列名里写着遮罩值的（agent 把遮罩值写进了别名，「13900000001 的单」），列名也换（同 SQL 里的名字，_scrub_sql）。
    原快照不动；没有要遮的返回原快照。

    embedded 只给质询工件用：别的列里一格文字夹着遮罩值（备注里写着手机号，换了写法的也算），把那一截换成「已遮罩」，
    这一格别的字照原样，门槛同整格（_Secrets.scrub）；这一列不记进遮掉的列——界面把记进去的列整列画成「已遮罩」，这一列在数据源
    里也没设遮罩。回答一路不这样换：渲染出来露了就整份不交（_leaked），不然回答被问的那一格看着像查到了别的东西。
    rename 为假时列名不换（工具边界先遮时：列名是模型自己写的，换了它引用的列名就对不上）。列名按文字的办法换（换了写法的
    号码也认，同 SQL 里带引号的名字）。
    工具边界遮过的快照（记着 masked_at_query）里，整格就是「已遮罩」的是查询时按值换掉的：这一列照样记进遮掉的列，和回答时
    按值换掉的一样画。"""
    columns = [str(c) for c in snapshot.get("columns") or []]
    index = {i for i, c in enumerate(columns) if c.lower() in hidden}
    by_value = bool(secrets.strong)
    premasked = "masked_at_query" in snapshot
    if not index and not by_value and not premasked:
        return snapshot, []
    word = _masked_word()
    touched = set(index)
    edited = False
    position = {c: i for i, c in enumerate(columns)}
    names = [secrets.scrub(c, loose=True) for c in columns] if by_value and rename else columns

    def cell(i: int | None, value: Any) -> Any:
        nonlocal edited
        if i in index:
            return word
        if premasked and value == word:
            if i is not None:
                touched.add(i)
            return value
        if by_value and secrets.hit(value):
            if i is not None:
                touched.add(i)
            return word
        if embedded and by_value and isinstance(value, (str, dict, list)):
            # JSON 列（对象、数组）按界面画出来的样子换，换过的照文字给
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            if (scrubbed := secrets.scrub(text, loose=True)) != text:
                edited = True
                return scrubbed
        return value

    def key(k: Any) -> Any:
        i = position.get(str(k))
        return names[i] if i is not None else k

    rows: list[Any] = []
    for row in snapshot["rows"]:
        if isinstance(row, list):
            rows.append([cell(i, v) for i, v in enumerate(row)])
        elif isinstance(row, dict):
            rows.append({key(k): word if str(k).lower() in hidden else cell(position.get(str(k)), v)
                         for k, v in row.items()})
        else:
            rows.append(row)
    renamed = names != columns
    if not touched and not renamed and not edited:
        return snapshot, []
    types = {key(k): v for k, v in snapshot["column_types"].items()} \
        if isinstance(snapshot.get("column_types"), dict) else {}
    types.update({names[i]: "text" for i in index})
    view = {**snapshot, "rows": rows, "column_types": types, **({"columns": names} if renamed else {})}
    return view, [names[i] for i in sorted(touched)]


def _cell_text(value: Any) -> str | None:
    """一格按文字换的样子（_masked_view 的 embedded）：文字照原样，JSON（对象、数组）按界面画出来的样子；别的为 None。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return None


def view_edits(snapshot: dict[str, Any], hidden: set[str], secrets: _Secrets, baseline: _Secrets
               ) -> dict[str, Any] | None:
    """质询工件按回答时的办法遮（_masked_view 的 embedded）时，secrets 比 baseline 多换掉的地方，相对原快照记成位置，
    不含值：{"cells": [[行, 列], …]（整格换成「已遮罩」）, "spans": [[行, 列, 起, 止], …]（文字格里换掉的那一截，起止按
    这一格的文字算）, "names": [[列, 起, 止], …]（列名里换掉的那一截）, "masked": [列名, …]（因此画成遮罩列的）}；没有
    多换的为 None。
    agent 先查了备注（那时还不知道备注里的号码是遮罩值，原样交回、原样存下），后来又查出了手机号（查询时遮掉，值只在
    内存里）：回答时按内存里的值（secrets）认得出，事后取质询工件时只剩 baseline 认得出的。这份位置记进质询事件，
    事后照着再换一遍（replay_edits）。行是字典的快照不记（核查的查询快照都是行列表）。"""
    full, full_masked = _masked_view(snapshot, hidden, secrets, embedded=True)
    base, base_masked = _masked_view(snapshot, hidden, baseline, embedded=True)
    if full is base:
        return None
    word = _masked_word()
    cells: list[list[int]] = []
    spans: list[list[int]] = []
    for r, (row, shown, plain) in enumerate(zip(snapshot.get("rows") or [], full["rows"], base["rows"])):
        if not (isinstance(row, list) and isinstance(shown, list) and isinstance(plain, list)):
            continue
        for c, (value, now, before) in enumerate(zip(row, shown, plain)):
            if now == before:
                continue
            if now == word:
                cells.append([r, c])
            elif (text := _cell_text(value)) is not None:
                spans += [[r, c, start, end] for start, end in secrets.spans(text, loose=True)]
    columns = [str(c) for c in snapshot.get("columns") or []]
    full_names, base_names = full.get("columns", columns), base.get("columns", columns)
    names = [[i, start, end] for i, (column, now, before) in enumerate(zip(columns, full_names, base_names))
             if now != before for start, end in secrets.spans(column, loose=True)]
    # 按位置比：同一列在两边可能叫不同的名字（列名里的值只有一边换了）
    already = {i for i, name in enumerate(base_names) if name in base_masked}
    masked = [name for i, name in enumerate(full_names) if name in full_masked and i not in already]
    if not (cells or spans or names or masked):
        return None
    return {"cells": cells, "spans": spans, "names": names, "masked": masked}


def replay_edits(snapshot: dict[str, Any], edits: Iterable[Any]) -> dict[str, Any]:
    """照记下的位置（view_edits，可能有好几份：同一份快照被几条质询引用）把回答时换掉的地方再换一遍。位置对不上的
    （行、列越界，起止不合理，形状不对）跳过：记录是封存的事件，坏了也不能让取工件出错。没有可换的返回原快照。"""
    rows = snapshot.get("rows")
    columns = [str(c) for c in snapshot.get("columns") or []]
    if not isinstance(rows, list):
        return snapshot

    def ints(entry: Any, size: int) -> list[int] | None:
        ok = isinstance(entry, list) and len(entry) == size and all(
            isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in entry)
        return entry if ok else None

    whole: set[tuple[int, int]] = set()
    parts: dict[tuple[int, int], list[tuple[int, int]]] = {}
    heads: dict[int, list[tuple[int, int]]] = {}
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        for entry in edit.get("cells") or []:
            if (at := ints(entry, 2)) is not None:
                whole.add((at[0], at[1]))
        for entry in edit.get("spans") or []:
            if (at := ints(entry, 4)) is not None and at[2] < at[3]:
                parts.setdefault((at[0], at[1]), []).append((at[2], at[3]))
        for entry in edit.get("names") or []:
            if (at := ints(entry, 3)) is not None and at[1] < at[2]:
                heads.setdefault(at[0], []).append((at[1], at[2]))
    word = _masked_word()

    def swapped(text: str, cuts: list[tuple[int, int]]) -> str:
        merged: list[list[int]] = []
        for start, end in sorted(cuts):
            if end > len(text):
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in reversed(merged):
            text = text[:start] + word + text[end:]
        return text

    out_rows: list[Any] = []
    changed = False
    for r, row in enumerate(rows):
        if not isinstance(row, list):
            out_rows.append(row)
            continue
        new_row = list(row)
        for c, value in enumerate(row):
            if (r, c) in whole:
                new_row[c] = word
            elif (r, c) in parts and (text := _cell_text(value)) is not None:
                new_row[c] = swapped(text, parts[(r, c)])
        changed = changed or new_row != row
        out_rows.append(new_row)
    names = [swapped(name, heads[i]) if i in heads else name for i, name in enumerate(columns)]
    if not changed and names == columns:
        return snapshot
    view = {**snapshot, "rows": out_rows}
    if names != columns:
        view["columns"] = names
        if isinstance(snapshot.get("column_types"), dict):
            rename = dict(zip(columns, names))
            view["column_types"] = {rename.get(str(k), k): v for k, v in snapshot["column_types"].items()}
    return view


# --------------------------------------------------------------------------
# 工具边界：查询结果交给核查 agent 之前先遮
# --------------------------------------------------------------------------


@dataclass
class QueryMask:
    """一份查询结果在工具边界遮好的样子（mask_query_result）。

    payload：遮好的结果，交给模型的、落进快照的都是它。columns：查询时就整列遮掉的结果列（结果里的列名，按先后）。
    secrets：这份结果里遮掉的值，只在内存里交给调用方（审批质询按值遮的第二道防线：模型照着别处看到的号码写进回答、
    查询条件时认得出），不进快照、不交给模型。"""

    payload: dict[str, Any]
    columns: list[str]
    secrets: _Secrets


def mask_query_result(payload: dict[str, Any], masked: Iterable[str], kind: str | None,
                      known: _Secrets | None = None, *, schema: Any = None) -> QueryMask:
    """一份查询结果（tools/datasource 的查询工具交回的样子：columns / rows / column_types / sql）在交给核查 agent、落进
    快照之前先遮。masked 是数据源设了遮罩的列，kind 是数据源的类型（SQL 按它的方言解析）。known 是此前已经认出的遮罩值：
    这个 agent 前面的查询遮掉的，加上调用方交进来的（审批质询、反方审稿收的报告快照里的值）。schema 是数据源的表结构
    （schema_cache，和随快照冻结的是同一份），认分档标签时用（_label_columns 的 tables）。

    - 整列遮：列名直接对上遮罩列的，加上按 SQL 认出值取自遮罩列的（_derived_hidden：别名、函数、子查询、WITH、
      SELECT *、表值函数；COUNT、只在条件里用到、分档当分组键的不算）；SQL 解析不了、又写着遮罩列的，整份结果的列都遮。
      这几列的格子换成「已遮罩」，类型记成文本
    - 别的列按值换：整格就是遮罩值的换掉，文字格里夹着的那一截换掉（换了写法的号码也认，_Secrets.loose_spans）。遮罩值
      是这份结果里的加上 known：先查号码、再查写着号码的备注、没设遮罩的联系方式表（别的库里的也一样）时，后一份同样
      遮好。门槛同按值遮（SECRET_DIGITS、SECRET_MIN）；只装着分档标签的列里的标签不算遮罩值，这份结果自己写的标签也不
      按 known 换（前面的查询认不准、当遮罩值收了的同名标签，不能把这一份的档位列换掉）
    - 列名不换（模型自己写的，换了它引用的列名就对不上）；结果里一列都没取自遮罩列、也没有可按值换的原样交回：数值的
      汇总、只数个数的查询不受影响

    交回的 secrets 只有这份结果里的遮罩值（known 调用方自己有）。分析本身出了意外，整份结果的列都遮：这一步挡在模型
    前面，分析的缺陷不能把原值放过去（宁可多遮）。"""
    base = {str(c).strip().lower() for c in masked if str(c).strip()}
    columns = [str(c) for c in payload.get("columns") or []]
    given = known is not None and bool(known.strong)
    if not (base or given) or not columns or not _query_shaped(payload):
        return QueryMask(payload, [], _Secrets())
    try:
        hidden = _derived_hidden(payload, base, kind)[0] if base else set()
        own = _Secrets()
        labels = _snapshot_labels(payload, kind, _schema_tables(schema))
        if any(c.lower() in hidden for c in columns):
            own.add(payload, hidden, labels=labels, base=base)
        pool = own
        if given:
            pool = own.copy()
            pool.merge(known)
            # 这份结果自己写的标签（CASE 写死的档名）不当遮罩值，哪怕前面的查询把同名的字收了
            if written := [text for name, texts in labels.items() if name not in base for text in texts]:
                pool.protect(written)
        if not pool.strong and not any(c.lower() in hidden for c in columns):
            return QueryMask(payload, [], own)
        pool.prepare()
        view, _ = _masked_view(payload, hidden, pool, embedded=True, rename=False)
    except Exception:  # noqa: BLE001 - 认不准就整份遮掉，原值不交给模型
        logger.warning("核查查询的遮罩分析出错，整份结果按遮罩处理", exc_info=True)
        hidden, own = {c.lower() for c in columns}, _Secrets()
        view, _ = _masked_view(payload, hidden, own, rename=False)
    return QueryMask(view, [c for c in columns if c.lower() in hidden], own)


#: 查询报错里引号括起来的一段（Postgres、DuckDB、MySQL 把转换失败的值写在引号里：invalid input syntax for type
#: bigint: "139-0000-0001"）。引号前后紧挨着字母数字的不算（can't、isn't 里的撇号）
_QUOTED_ERROR = re.compile(r"(?<![0-9A-Za-z_])(\"[^\"\n]*\"|'[^'\n]*')(?![0-9A-Za-z_])")
#: 报错里不带引号的长数（DuckDB：Type INT64 with value 13900000001 can't be cast）
_ERROR_NUMBER = re.compile(r"(?<![0-9A-Za-z_.])\d{%d,}(?![0-9A-Za-z_])" % SECRET_DIGITS)


def mask_error_text(message: str, sql: str, known: _Secrets | None = None) -> str:
    """查询失败时交给核查 agent、写进 tool.end 的报错原话。驱动的原话是模型改写 SQL 的线索，可数据库会在报错里带上
    数据里的值（类型转换失败、越界），数据源设了遮罩时交出去之前先换：引号里的一段、不带引号的长数，SQL 里没写着的
    换成「已遮罩」——SQL 里写着的是模型自己写的表名、列名、字面量，照留，它要靠这些改写；此前查询遮掉的值（known）
    按值换。不认得是不是数据的也换（多遮）：报错只是线索，说不全也不耽误改写。"""
    if not message:
        return message
    word = _masked_word()
    lowered = sql.lower()

    def quoted(m: re.Match[str]) -> str:
        inner = m.group(1)[1:-1]
        if not inner.strip() or inner.lower() in lowered:
            return m.group()
        return m.group(1)[0] + word + m.group(1)[-1]

    out = _QUOTED_ERROR.sub(quoted, message)
    out = _ERROR_NUMBER.sub(lambda m: m.group() if m.group() in sql else word, out)
    return known.scrub(out, loose=True) if known is not None and known.strong else out
