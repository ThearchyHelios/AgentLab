"""可点击的证据：报告里的每个数字由系统从证据里取、按规则渲染，出处写在数据结构里。

以前叙述是模型写的自由文本，出具时再按数值回头猜每个数来自哪个指标——5 个指标
都是 0.0 时，报告里所有的 0 全算给第一个。这里反过来：模型只写引用标记
`[[m:gmv]]`，数值由系统从口径卡取出、按口径卡的格式渲染。叙述层在结构上就写不出
数字，按数值猜出处这件事不再需要。

整个模块是纯函数（不碰数据库、不调模型），报告节点自查、出口契约复核、证据接口
共用这一份，免得三处各写一套规则、各判各的。

标记语法：

    [[m:<指标 id>]]                口径卡指标，渲染成「值+单位」
    [[m:<指标 id>|万]]              换算显示：万 / 亿 / pct / int / .N（小数位），由渲染器完成
    [[v:Q<n>.r<行>.<列>]]           查询快照里的一格（取回时复验哈希），也能加 |万 这类换算
    [[table:Q<n> cols=a,b rows=0-4]] 整表：系统从快照生成 Markdown 表，每一格都是带出处的片段
    [[i:<输入字段>]]                运行输入
    [[t:<表>]] / [[c:<表>.<列>]]    实体：表 / 字段，显示名字本身（反引号里写的已知名字也会自动链接）
    [[q:K<n>|<逐字引文>]]           引文：必须在那次检索命中的片段里逐字出现，显示引文本身
    [[see:<ref>,<ref>…]]           依据：挂在句末，不渲染

实体只在这次运行冻结了表结构（schema_snapshot）时才核对：目录里没有表和字段条目的，
t / c 仍按一期判为解析不了（「这种引用在后续版本支持」），反引号里的名字也不查；报告节点
写了 entities: off 的，照实说是它关掉的。表结构快照不全（库里的表太多、只存了一部分）时，
找不到的名字只说「核对不了」，不说「可能是编造的」。自动链接的名字只是标注：一句结论挂没挂
依据，只看写作者自己写的 [[…]] 和 [[see:…]]。
表名、字段名怎么写、怎么找，按文档记下的实体语法版本（顶层 entity_syntax，见 ENTITY_SYNTAX）：
2 版起 [[t:]] / [[c:]] 和 SQL 里不加引号的名字认中文（[[t:明细]]、FROM 明细），按 SQLite 的口径找（name_key）；
没有这个字段的老文档按 1 版（只认 ASCII 标识符）复核。反引号里的名字核对、正文裸名自动链接两版都只认 ASCII。
沙箱代码节点的产出不能直接引用（`[[v:N:calc.x]]`），原因写「沙箱算出来的数要进口径卡」。

单元格、整表、引文要读快照：整个模块仍然不碰数据库、不调模型，快照经 loader 取（缺省是
artifact_store.load，只读文件）。测试和接口可以换成自己的 loader。

旧运行没有报告文档时，guess_sources 按数值去已封存的证据里找「可能的来源」：那只是猜测，
不是证据，界面要照实这么说；遮罩的列不当候选。

偏移一律按 Unicode 码点算（Python 的 str 下标）。前端的 JS 字符串按 UTF-16 计，
碰到码点在 BMP 之外的字符（emoji）要自己换算。
"""

from __future__ import annotations

import bisect
import difflib
import json
import math
import re
import unicodedata
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Any, Literal, TypedDict

from app.core.artifact_store import canonical_json, content_hash
from app.core.artifact_store import load as load_artifact
from app.data.names import FILLERS, name_key
from app.data.tabular import UNSHAPED_NOTE
from app.engine.expressions import CellError, cell_value, column_kind, locate_cell
from app.engine.issuance import _tolerance, extract_numbers, number_allowance
from app.engine.labels import option_label

DOC_SCHEMA = "agentlab.report/1"
#: 实体语法（表名、字段名怎么写、怎么找）的版本。新组装的文档记在顶层 entity_syntax，参与内容哈希。
#: - 1：名字只认 ASCII 标识符，按 Python 的 lower() 找。没有 entity_syntax 的老文档按它复核；
#: - 2：[[t:]] / [[c:]] 和 SQL 里不加引号的名字放宽到 Unicode（上传表格的中文表名、列名），按 name_key 找
#:   （只对 ASCII 不区分大小写、不做 NFKC，和 SQLite 一致；导入和证据层共用 names.py 这一个键）。
#: 复核必须按文档生成时的那一版：老文档里的 [[t:明细]] 当年判为「格式无法识别」，拿新规则查就解析得了，
#: 和记下的状态对不上，会被当成文档被改过（state_mismatch）。
ENTITY_SYNTAX = 2
ENTITY_SYNTAXES = frozenset({1, 2})


# --------------------------------------------------------------------------
# 数据结构（方案 1.5）。都是普通 dict，TypedDict 只用来写清形状
# --------------------------------------------------------------------------


class EvidenceEntry(TypedDict, total=False):
    """台账条目：state["evidence"] 里的一行，同时写进 node.finished.evidence。"""

    kind: Literal["query", "metric_set", "retrieval", "tool", "node_output", "schema"]
    node_id: str
    exec: int                 # 这个节点第几次执行（循环里会有多次），从 1 数
    artifact: str             # 被引用的那件工件
    via: str                  # 外层包装的工件（query_snapshot 外面的 tool_snapshot）
    call_id: str
    tool: str
    source: str
    columns: list[str]
    rows: int
    truncated: bool
    caliber: str              # metric_set 专有：口径名、版本、指标 id 清单
    version: str
    metrics: list[str]
    code_sha: str             # node_output（代码节点）专有：实际执行的代码的 sha256、
    role: str                 # evidence_role（source / compute）、语言
    language: str
    schema_artifact: str      # query 专有（有表结构快照时）：查询当时数据源的表结构快照
    tables: Any               # query：SQL 里 FROM / JOIN 的表名（list）；schema：{表: [列…]}
    schema: str | None        # schema 专有：数据库里的 schema 名（全名 = schema.表）、同步时间
    synced_at: str | None


class Citation(TypedDict, total=False):
    ref: str                  # 标记里写的原文：gmv、week、Q4.r0.amount、orders
    alias: str                # 目录里的键：m:gmv、i:week、Q4、t:orders、K1
    locator: dict[str, Any]
    eid: str
    kind: str                 # metric / input / query / cell / table / column / quote
    role: Literal["value", "entity", "quote", "support"]
    status: Literal["resolved", "unresolved", "mismatch"]
    reason: str               # unresolved 时为什么
    unknown: bool             # 实体：本次运行哪里都没有这个名字（可疑实体，不是写法错了）
    conv: str                 # 换算：万 / 亿 / pct / int / .N
    value: Any
    rendered: str
    source: dict[str, Any]    # 引文：原文所在的检索快照、文档、片段


class Segment(TypedDict, total=False):
    id: str
    kind: Literal["text", "number", "value", "entity", "quote", "structural"]
    text: str
    span: list[int]           # [start, end)，在 doc.markdown 里的码点偏移
    ref: str                  # 带前缀的原始引用：m:gmv、i:week、t:orders
    cite: Citation
    state: Literal["deterministic", "probabilistic", "none", "neutral"]
    strong: bool
    code: bool                # 实体写在反引号里：text 带着两个反引号，渲染成行内代码
    auto: bool                # 实体是正文里自动链接出来的，不是 [[t:]] / [[c:]] 标记
    name: str                 # 可疑实体（没有 ref）：反引号里写的那个名字
    issue: str                # 这一段本身的问题：uncited_number / unresolved_ref / unknown_entity


class Unit(TypedDict, total=False):
    id: str
    kind: Literal["claim", "connective", "heading", "code"]
    span: list[int]           # 内容的起止（不含行首的 Markdown 语法）
    cites: list[str]          # 这一句引用过的 alias，行内和 [[see:]] 合在一起、去重
    see: list[Citation]       # [[see:]] 里的每一个引用，单独核对
    segments: list[Segment]
    loc: dict[str, int]       # 表格单元格：{row, col}，表头 row = -1
    depth: int                # 列表项的缩进层级


class Block(TypedDict, total=False):
    id: str
    type: Literal["heading", "paragraph", "list", "table", "quote", "code", "hr"]
    level: int
    ordered: bool
    start: int                # 有序列表的起始序号
    lang: str
    units: list[Unit]


class Violation(TypedDict, total=False):
    code: str
    message: str
    span: list[int]
    text: str
    segment: str
    unit: str
    ref: str
    context: str


# --------------------------------------------------------------------------
# eid：内容派生的全局标识
# --------------------------------------------------------------------------


def make_eid(kind: str, artifact: str | None, locator: dict[str, Any] | None) -> str:
    """ev:<kind>:<16 位 hex>。同样的 (kind, 工件, 定位) 永远得到同一个 eid。

    解析时重算一次，和目录里记的对不上就说明目录被改过。
    """
    digest = content_hash(canonical_json({"kind": kind, "artifact": artifact, "locator": locator or {}}))
    return f"ev:{kind}:{digest[:16]}"


def input_eid(field: str, value: Any) -> str:
    """运行输入的 eid。输入没有工件，工件那一格放值的指纹：同一个字段换了值
    （2026-W37 → 2026-W38）就是另一件证据，跨运行比对才有意义。

    目录条目里记着 value，拿它就能复算：make_eid("input", content_hash(canonical_json(value)), {"field": field})。
    """
    return make_eid("input", content_hash(canonical_json(value)), {"field": field})


def cell_eid(artifact: str | None, row: int, column: str) -> str:
    """查询快照里一格的 eid。报告的 [[v:]]、agent 字段的出处、口径卡的 cell() 都按它算，
    同一格在三处是同一个标识。column 一律是列名（列号先换成列名）。"""
    return make_eid("cell", artifact, {"row": row, "column": column})


# --------------------------------------------------------------------------
# 台账：节点执行器写条目时用的几件小事
# --------------------------------------------------------------------------


def ledger_enabled(run: Any) -> bool:
    """这次运行记不记证据台账（以及二期的其他新行为）。

    判断「升级前发起的运行」沿用护栏那次加的快照：agent_limits 为 None 的运行可能正停在
    某个审批上，节点恢复时整个重放——多一个 task、给模型的消息多一行，checkpoint 里缓存
    的结果就对错号。这类运行一律走旧逻辑，一个字都不加。
    """
    return getattr(run, "agent_limits", None) is not None


def next_exec(state: Any, node_id: str) -> int:
    """这个节点这是第几次成功执行（从 1 数，循环里会有多次），按 trail 算。"""
    done = sum(1 for t in (state.get("trail") or []) if t.get("node_id") == node_id and "error" not in t)
    return done + 1


def query_entry_fields(payload: Any) -> dict[str, Any] | None:
    """数据源查询工具交回的 JSON（文本或已解析的对象）→ 台账 query 条目要的字段。

    不是一次成功的查询（「查询失败：…」这类原话、别的工具的返回、没落成快照的）返回 None。
    rows 是快照里的行数：单元格按快照里的行号引用。
    """
    data = payload
    if isinstance(data, str):
        if not data.lstrip().startswith("{"):
            return None
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    artifact, columns, rows = data.get("artifact"), data.get("columns"), data.get("rows")
    if not (isinstance(artifact, str) and artifact and isinstance(columns, list) and isinstance(rows, list)):
        return None
    fields = {"artifact": artifact, "source": data.get("source"), "columns": [str(c) for c in columns],
              "rows": len(rows), "truncated": bool(data.get("truncated"))}
    schema = data.get("schema_artifact")
    if isinstance(schema, str) and schema:
        # 表结构快照和 SQL 里的表名成对出现：没探查过结构的数据源，只凭 SQL 和结果列核对名字，
        # 反引号里写的真实表名（没进这条 SQL）会被误标成编造的，这种查询不启用实体核对。
        # 表名按当前的实体语法版本取：台账条目在查询当时记一次、存下来，复核老文档时目录按存下的台账重建，
        # 不重新解析 SQL，所以老文档看到的还是当年记的表名
        fields.update(schema_artifact=schema, tables=sql_tables(str(data.get("sql") or "")))
    return fields


# --------------------------------------------------------------------------
# 表结构：冻结的快照、SQL 里用到的表
# --------------------------------------------------------------------------

#: 数据源工具每次查询时把当时的表结构存成这种工件
SCHEMA_SNAPSHOT = "schema_snapshot"

class _SqlSyntax:
    """sql_tables 用的一套正则，按实体语法版本各一套，差在不加引号的名字由哪些字符组成、名字和关键字的边界怎么定。

    1 版：ASCII 字母或下划线开头，字母、数字、_ $ #；空白和词边界按 Python 的 \\s、\\b。原样留着
    （ENTITY_SYNTAXES 里的版本都得能复现）。
    2 版：照数据库的分词来——SQLite、PostgreSQL、MySQL 都把 ASCII 以外的字符一律算作不加引号的标识符的一部分
    （开头也算），所以名字是 ASCII 字母、下划线或任意非 ASCII 字符开头，后面再加数字和 $ #。上传的表格起的是
    中文名，导入时也鼓励不加引号写 SQL：`FROM 明细` 按 1 版认不出来，这张表的条目就记不下是哪次查询用到的，
    写作目录把它归到「其他表」，裁判也拿不到这张表的 SQL。
    不能只认 names.name_problem 的字符集（字母、数字、汉字、下划线）：`FROM 销售数据（2024）` 在库里查的是
    「销售数据（2024）」这一张表（全角括号、间隔号、全角空格都是名字的一部分），只认 \\w 会截成「销售数据」，
    记下一张不存在的表，[[t:销售数据]] 就能静默通过。同理，2 版的空白只认 ASCII 空白，FROM / JOIN 等关键字
    两边不能紧挨非 ASCII 字符（`FROM　明细` 在库里是一个名字，不是 FROM 加表名）。名字切得和数据库一样，
    才谈得上「拿不准往少认一张表偏」。

    判断「# / $ 前面是不是名字的一部分」用的字符集跟着名字走：2 版里「明细#1」和「abc#1」一样是一个名字，
    不能把 # 当注释开头。PostgreSQL 美元引号的标签（$标签$）也按同样的字符集认。
    """

    def __init__(self, start: str, rest: str, tag: str, key: Callable[[str], str], *,
                 ws: str = r"\s", before: str = r"\b", after: str = r"\b") -> None:
        self.ident = rf'(?:"[^"\n]+"|`[^`\n]+`|\[[^\]\n]+\]|{start}{rest}*)'
        self.name = re.compile(rf"{ws}*({self.ident}(?:{ws}*\.{ws}*{self.ident}){{0,2}})")
        self.part = re.compile(self.ident)
        #: FROM / JOIN：后面跟着表名
        self.keyword = re.compile(rf"{before}(?:from|join){after}", re.I)
        #: WITH x AS (…), y AS (…)：公用表表达式的名字不是真表
        self.cte = re.compile(rf"(?:{before}with{after}(?:{ws}+recursive{after})?|,){ws}*({self.ident}){ws}*"
                              rf"(?:\([^()]*\){ws}*)?{before}as{ws}*\(", re.I)
        self.alias = re.compile(rf"{ws}+(?:as{ws}+)?({self.ident})", re.I)
        #: 名字后面紧跟括号：子查询、表函数；紧跟逗号：FROM a, b
        self.call = re.compile(rf"{ws}*\(")
        self.comma = re.compile(rf"{ws}*,")
        #: PostgreSQL 的美元引号字符串：$$…$$、$tag$…$tag$（$1 这种参数不是）
        self.dollar = re.compile(rf"\$(?:{tag})?\$")
        self.word_char = re.compile(rest)
        #: 表名去重、和公用表表达式比对用的键：1 版 lower()，2 版 name_key（「Дата」和「дата」是两张表）
        self.key = key


#: 2 版不加引号的名字里能出现的字符：ASCII 字母、数字、_ $ #，加上任意非 ASCII 字符
_SQL_WORD2 = r"[A-Za-z0-9_$#\x80-\U0010ffff]"
_SQL = {
    1: _SqlSyntax("[A-Za-z_]", "[A-Za-z0-9_$#]", "[A-Za-z_][A-Za-z0-9_]*", str.lower),
    2: _SqlSyntax(r"[A-Za-z_\x80-\U0010ffff]", _SQL_WORD2, r"[A-Za-z_\x80-\U0010ffff][A-Za-z0-9_\x80-\U0010ffff]*",
                  name_key, ws=r"[ \t\n\r\f\v]", before=rf"(?<!{_SQL_WORD2})", after=rf"(?!{_SQL_WORD2})"),
}
#: 跟在 FROM 后面、却不是表名的词
_SQL_NOT_TABLE = frozenset({"select", "lateral", "unnest", "dual", "values", "table"})
#: 表名后面紧跟这些词时，它们不是别名
_SQL_CLAUSE = frozenset({
    "where", "group", "order", "having", "limit", "offset", "fetch", "join", "inner", "left", "right", "full",
    "cross", "outer", "natural", "on", "using", "union", "except", "intersect", "window", "for", "as", "and",
    "or", "not", "when", "then", "else", "end", "set", "returning", "into", "pivot", "unpivot", "qualify"})
#: FROM 出现在这些函数的括号里时是语法的一部分：EXTRACT(YEAR FROM ts)、TRIM(' ' FROM name)
_SQL_FROM_FUNCS = frozenset({"extract", "substring", "trim", "overlay", "position"})
#: SQLite / SQL Server 的方括号标识符：里面有引号的不是（那是 ARRAY['…'] 这样的数组字面量）
_SQL_BRACKET = re.compile(r"\[[^\]'\"\n]*\]")


def _sql_scan(sql: str, rules: _SqlSyntax) -> tuple[str, str]:
    """一趟扫完注释、字符串、带引号的标识符，返回两份和原文等长的文本：

    - code：注释换成空格，字符串字面量的内容换成空格（引号留着），标识符原样——取表名用
    - words：在 code 的基础上，带引号的标识符的内容也换成空格——找 FROM / JOIN 这些关键字用，
      "FROM x" 这样的名字（MySQL 默认还把它当字符串）里的 FROM 不是关键字

    字符串和注释必须一起认：先去注释再抹字符串的话，'--x' 会把后半条语句当注释吞掉，
    '/*' 和 '*/' 两个字符串之间的正文也会被当成注释。拿不准的地方一律往「少认一张表」偏：
    反斜杠当转义（MySQL、E'…'），块注释可以嵌套（PostgreSQL），# 开头到行尾是注释（MySQL）。
    换行原样保留。
    """
    code, words = list(sql), list(sql)
    n = len(sql)

    def blank(a: int, b: int, *targets: list[str]) -> None:
        for k in range(a, min(b, n)):
            if sql[k] != "\n":
                for t in targets:
                    t[k] = " "

    i = 0
    while i < n:
        ch, nxt = sql[i], sql[i + 1] if i + 1 < n else ""
        word_before = i > 0 and bool(rules.word_char.match(sql[i - 1]))
        if (ch == "-" and nxt == "-") or (ch == "#" and not word_before):
            end = sql.find("\n", i)
            end = n if end < 0 else end
            blank(i, end, code, words)
            i = end
        elif ch == "/" and nxt == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j, code, words)
            i = j
        elif ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                elif sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                elif sql[j] == "'":
                    break
                else:
                    j += 1
            blank(i + 1, j, code, words)
            i = j + 1
        elif ch in ('"', "`"):
            j = i + 1
            while j < n and not (sql[j] == ch and not (j + 1 < n and sql[j + 1] == ch)):
                j += 2 if sql[j] == ch else 1
            blank(i + 1, j, words)
            i = j + 1
        elif ch == "[" and (m := _SQL_BRACKET.match(sql, i)):
            blank(i + 1, m.end() - 1, words)
            i = m.end()
        elif ch == "$" and not word_before and (m := rules.dollar.match(sql, i)):
            close = sql.find(m.group(0), m.end())
            end = n if close < 0 else close
            blank(m.end(), end, code, words)
            i = n if close < 0 else close + len(m.group(0))
        else:
            i += 1
    return "".join(code), "".join(words)


def _unquote(name: str, rules: _SqlSyntax) -> str:
    return ".".join(p[1:-1] if p[:1] in ('"', "`", "[") else p for p in rules.part.findall(name))


def _inside_call(code: str, pos: int) -> bool:
    """pos 所在的最内层括号，是不是 EXTRACT( / TRIM( 这类把 FROM 当语法用的函数；IS DISTINCT FROM 同理。"""
    if re.search(r"\bdistinct\s*$", code[:pos], re.I):
        return True
    depth = 0
    for i in range(pos - 1, -1, -1):
        ch = code[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                word = re.search(r"([A-Za-z_]+)\s*$", code[:i])
                return bool(word and word.group(1).lower() in _SQL_FROM_FUNCS)
            depth -= 1
    return False


def sql_tables(sql: str, *, syntax: int | None = None) -> list[str]:
    """SQL 里 FROM / JOIN 后面的表名，按第一次出现的顺序、不分大小写去重。

    注释、字符串字面量一趟扫掉（_sql_scan：`-- FROM ghost`、'JOIN ghost'、'--x' 后面的正文都
    处理对）。双引号、反引号、方括号括起来的标识符照样认，去掉引号；引号里的 FROM 不算关键字。
    WITH 定义的公用表表达式、子查询、表函数（generate_series(…)）不算表。

    syntax 是实体语法版本，缺省当前版（2 版认不加引号的中文表名：FROM 明细、两张中文表 JOIN；名字切得和数据库
    一样，`FROM 销售数据（2024）` 记的是整个名字，见 _SqlSyntax）。
    「不分大小写」按版本的口径：2 版是 name_key，「Дата」和「дата」、「ＯＲＤＥＲＳ」和「orders」算两张表。
    """
    if not sql:
        return []
    rules = _SQL[ENTITY_SYNTAX if syntax is None else syntax]
    code, words = _sql_scan(sql, rules)
    ctes = {rules.key(_unquote(code[m.start(1):m.end(1)], rules)) for m in rules.cte.finditer(words)}
    out: dict[str, str] = {}
    for keyword in rules.keyword.finditer(words):
        if _inside_call(words, keyword.start()):
            continue
        pos = keyword.end()
        while True:
            m = rules.name.match(code, pos)
            if not m or rules.call.match(code, m.end()):
                break                                   # 子查询、表函数
            name = _unquote(m.group(1), rules)
            lowered = name.lower()
            if lowered == "only":                       # FROM ONLY t（PostgreSQL）
                pos = m.end()
                continue
            if lowered in _SQL_NOT_TABLE:
                break
            if rules.key(name) not in ctes:
                out.setdefault(rules.key(name), name)
            pos = m.end()
            alias = rules.alias.match(code[pos:])
            if alias and alias.group(1).lower() not in _SQL_CLAUSE:
                pos += alias.end()
            comma = rules.comma.match(code, pos)
            if not comma:
                break
            pos = comma.end()
    return list(out.values())


def schema_entry_fields(content: Any) -> dict[str, Any] | None:
    """表结构快照的内容 → 台账 schema 条目要的字段：{source, schema, synced_at, tables: {表: [列…]}}，
    快照不全（探查时表太多被截断）时另有 truncated: True 和 total（库里一共几张表）。

    只记名字，不记类型、注释：目录核对名字存不存在就够了，类型要看的时候去取快照本身
    （它在封存范围内，取回时复验哈希）。没有表的快照返回 None。
    """
    if not isinstance(content, dict) or not isinstance(content.get("tables"), dict) or not content["tables"]:
        return None
    tables: dict[str, list[str]] = {}
    for name, meta in content["tables"].items():
        cols = meta.get("columns") if isinstance(meta, dict) else None
        tables[str(name)] = [str(c["name"]) for c in cols or [] if isinstance(c, dict) and c.get("name")]
    fields = {"source": content.get("source"), "schema": content.get("schema"), "synced_at": content.get("synced_at"),
              "tables": tables}
    if content.get("truncated"):
        # 库里的表太多、探查只存了前一部分：没列出的名字可能是真的，实体核对据此只说「核对不了」
        total = content.get("total")
        fields.update(truncated=True, **({"total": total} if isinstance(total, int) else {}))
    return fields


def schema_table(content: Any, name: Any) -> dict[str, Any] | None:
    """表结构快照里的一张表：表名一字不差的优先，其次不分大小写、全名（schema.表）也认。"""
    tables = content.get("tables") if isinstance(content, dict) else None
    if not isinstance(tables, dict) or not name:
        return None
    if isinstance(tables.get(name), dict):
        return tables[name]
    lowered = str(name).lower()
    return next((t for key, t in tables.items() if isinstance(t, dict)
                 and lowered in (str(key).lower(), str(t.get("qualified") or "").lower())), None)


def schema_column(table: dict[str, Any] | None, name: Any) -> dict[str, Any] | None:
    """表结构快照里一张表的一个字段：名字一字不差的优先，其次不分大小写。"""
    columns = [c for c in (table or {}).get("columns") or [] if isinstance(c, dict)]
    return next((c for c in columns if c.get("name") == name), None) \
        or next((c for c in columns if str(c.get("name") or "").lower() == str(name or "").lower()), None)


def table_fields(table: dict[str, Any] | None, hidden: set[str] | frozenset[str] = frozenset(), *,
                 limit: int = 60) -> tuple[list[dict[str, str]], int, bool]:
    """一张表的字段清单：([{name, type}] 最多 limit 个, 另有几个没列, 有没有字段按遮罩没列)。

    hidden 是要遮的列（小写）：遮罩的字段不列，也不计入「另有几个」。证据面板和裁判的摘录同一个口径。
    """
    columns = [c for c in (table or {}).get("columns") or [] if isinstance(c, dict) and c.get("name")]
    shown = [c for c in columns if str(c["name"]).lower() not in hidden]
    fields = [{"name": str(c["name"]), "type": str(c.get("type") or "")} for c in shown[:limit]]
    return fields, max(0, len(shown) - limit), len(shown) < len(columns)


def schema_ledger_entry(artifact: str, *, node_id: str, exec_no: int,
                        loader: Callable[[str], Any] | None = None) -> dict[str, Any] | None:
    """一件表结构快照 → 台账的 schema 条目。快照取不回来（或者哈希对不上）返回 None。

    节点执行器在工具调用之后调它：快照是内容寻址的，重放时读到的是同一份，条目也就是同一条。
    """
    try:
        fields = schema_entry_fields((loader or load_artifact)(artifact))
    except Exception:  # noqa: BLE001 - 取不回来就不记，不能让一次查询因此失败
        return None
    if fields is None:
        return None
    return {"kind": "schema", "node_id": node_id, "exec": exec_no, "artifact": artifact, **fields}


# --------------------------------------------------------------------------
# 渲染：数值 → 报告里显示的那几个字
#
# 规则是固定的，同一个值永远渲染成同一串字：报告节点边写边渲染一次，出口契约
# 复核时再渲染一次，两边逐字比对。所以这里一律用 Decimal 按「四舍五入」算，不走
# float 的 round（2.675 在 float 里是 2.67499…，round 出来是 2.67）。
# --------------------------------------------------------------------------

FORMATS = ("plain", "thousands", "percent_of_ratio")
#: 口径卡没写 format 时按千分位
DEFAULT_FORMAT = "thousands"
#: 值拿不到时显示的字。不插值、不写 0
MISSING = "—"

_CONV_SCALE = {"万": Decimal(10) ** 4, "亿": Decimal(10) ** 8}
_CONV_PLACES = re.compile(r"^\.([0-6])$")
#: 小数位最多写到多少。负数是取整到十位、百位……（round(x, -2) 的意思）
MAX_PLACES = 100
#: 整数部分最多多少位。浮点数到头也就 309 位，再大的整数按规则一位位写出来没有意义
_MAX_DIGITS = 1000


class RenderError(ValueError):
    """这个值按这种写法渲染不出来。报告里算作解析不了的引用，原因就是异常消息。"""


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _decimal(value: int | float) -> Decimal:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RenderError(f"{value} 不是有限的数值")
        return Decimal(repr(value))
    return Decimal(value)


def _ctx(d: Decimal, places: int = 10) -> Context:
    """精度跟着值走。定死一个精度的话，1e55 留两位小数就超出精度（抛 InvalidOperation），
    70 位的整数去尾零时会被悄悄舍入成 60 位有效数字——显示出来的就不是那个数了。"""
    need = max(len(d.as_tuple().digits), d.adjusted() + max(places, 0)) + 10
    return Context(prec=max(60, need), rounding=ROUND_HALF_UP)


def _shown(d: Decimal) -> str:
    text = format(d, "f")
    return text if len(text) <= 24 else format(d, ".6g")


def _digits(d: Decimal, places: int | None, *, group: bool, strip: bool = False,
            zero: str | None = None) -> str:
    """按小数位四舍五入后写出来。

    一个不是 0 的值写出来成了 0（29 单换算成「0万单」、0.004 取整成「0」），就不写：
    抛 RenderError，原因是 zero。系统渲染的数带着「有出处」的标记，一个假的 0 比模型
    自己编的数还难发现。
    """
    # 没写小数位时最多留 10 位再去掉尾零：0.1 + 0.2 显示 0.3，而不是 0.30000000000000004
    exp = -(10 if places is None else places)
    ctx = _ctx(d, -exp)
    q = d.quantize(Decimal(1).scaleb(exp), context=ctx)
    if q.is_zero() and not d.is_zero():
        raise RenderError(zero or f"{_shown(d)} 按 {-exp} 位小数显示为 0，无法看出真实的值")
    if places is None or strip:
        q = q.normalize(context=ctx)
    if q.is_zero():
        q = q.copy_abs()              # 不显示 -0.0
    return format(q, ",f" if group else "f")


def _clean(text: str) -> str:
    """文本值进正文前压成一行：换行会被当成新的段落或列表。"""
    return re.sub(r"\s+", " ", text).strip()


def render_number(
    value: Any,
    *,
    unit: str = "",
    decimals: int | None = None,
    fmt: str | None = None,
    conv: str | None = None,
) -> str:
    """按口径卡的定义把一个值渲染成字。

    - format：thousands（默认，千分位）/ plain（不分组，年份、编号这类）/
      percent_of_ratio（值是比率 0.0235，显示成 2.35%，小数位按 decimals − 2）
    - decimals：固定小数位；不写就按值本来的样子（整数不带小数点）
    - 单位直接接在数字后面：45,678.5元、8.7%
    - conv 是报告里写的换算：万 / 亿（保留两位、去尾零，单位接在后面：4.57万元）、
      pct（比率 ×100 加 %）、int（取整）、.N（N 位小数）

    值是 None 显示「—」。文本值原样（压成一行），不能换算。

    不是 0 的值显示出来成了 0（换算、取整、小数位不够），抛 RenderError——报告里这个
    引用就算解析不了，写作者会被要求换一种写法，而不是给读者一个带出处的假 0。
    """
    fmt = fmt or DEFAULT_FORMAT
    if fmt not in FORMATS:
        raise RenderError(f"显示格式只能是 {' / '.join(FORMATS)}，当前为「{fmt}」")
    if value is None:
        return MISSING
    if not is_number(value):
        if conv:
            raise RenderError(f"「{_clean(str(value))[:40]}」不是数值，不能换算为「{conv}」")
        if isinstance(value, bool):
            return "是" if value else "否"
        return _clean(str(value))
    if decimals is not None and (isinstance(decimals, bool) or not isinstance(decimals, int)
                                 or not -MAX_PLACES <= decimals <= MAX_PLACES):
        raise RenderError(f"小数位需要是 -{MAX_PLACES} 到 {MAX_PLACES} 的整数，当前为「{decimals}」")
    try:
        return _render_numeric(_decimal(value), unit=unit, decimals=decimals, fmt=fmt, conv=conv)
    except RenderError:
        raise
    except (ArithmeticError, ValueError) as e:       # decimal 的 InvalidOperation 也在这里
        raise RenderError(f"「{value}」无法按规则显示") from e


def _render_numeric(d: Decimal, *, unit: str, decimals: int | None, fmt: str, conv: str | None) -> str:
    if d.adjusted() >= _MAX_DIGITS:
        raise RenderError(f"该数值有 {d.adjusted() + 1} 位，超出可显示的范围")
    ratio = fmt == "percent_of_ratio"
    group = fmt != "plain"
    shown = _shown(d)
    if conv in _CONV_SCALE:
        if ratio:
            raise RenderError(f"该指标是比率，不能换算为「{conv}」")
        scaled = _ctx(d).divide(d, _CONV_SCALE[conv])
        return _digits(scaled, 2, group=True, strip=True,
                       zero=f"{shown} 换算为「{conv}」后显示为 0，精度不够：请去掉换算，或换用小一级的单位"
                       ) + conv + unit
    rounded = f"{shown} 按 |{conv} 显示为 0，精度不够：请去掉换算，或改用 |.N 多保留几位小数"
    if conv == "pct":
        if "%" in unit or "％" in unit:
            raise RenderError("该指标本身是百分数（单位为 %），不能再换算为 pct")
        places = max(decimals - 2, 0) if decimals is not None else 1
        return _digits(_ctx(d).multiply(d, 100), places, group=group, zero=rounded) + "%"

    if conv == "int":
        places: int | None = 0
    elif conv and (m := _CONV_PLACES.match(conv)):
        places = int(m.group(1))
    elif conv:
        raise RenderError(f"无法识别的换算 |{conv}，只支持 万 / 亿 / pct / int / .N（N 为 0–6）")
    else:
        rounded = (f"{shown} 按口径卡的小数位（{decimals}）显示为 0，精度不够：请在口径卡中增加小数位"
                   if decimals is not None else
                   f"{shown} 太小：未设置小数位时最多保留 10 位小数，显示为 0。请在口径卡中设置小数位")
        places = (max(decimals - 2, 0) if ratio else decimals) if decimals is not None else None
    if ratio:
        return _digits(_ctx(d).multiply(d, 100), places, group=group, zero=rounded) + "%"
    return _digits(d, places, group=group, zero=rounded) + unit


def render_metric(metric: dict[str, Any], conv: str | None = None) -> str:
    """口径卡里的一个指标按它自己的格式渲染。"""
    return render_number(
        metric.get("value"),
        unit=str(metric.get("unit") or ""),
        decimals=metric.get("decimals"),
        fmt=metric.get("format"),
        conv=conv,
    )


def render_cell(value: Any, conv: str | None = None, *, kind: str | None = None) -> str:
    """查询快照里的一格渲染成字。

    - 数按千分位，小数位照值本来的样子（最多 10 位、去尾零）；DECIMAL 落盘后的数字文本
      （"45678.50"，小数位为 0 的 "45678"，规则见 expressions.cell_value）按数算，用 Decimal
      渲染，不经 float 丢精度
    - kind 是这一列查询时记下的类型：text 列按文本，VARCHAR 的 "2026" 不渲染成「2,026」；
      老快照没记类型就按值猜
    - NULL 显示「—」：这是快照里的事实（那一格就是空的），不是缺值，更不是 0
    - 文本压成一行，竖线换成 ¦：它常常落在表格里，原样的 | 会多切出一格
    """
    if isinstance(value, str) and cell_value(value, kind) is not value:
        try:
            return _render_numeric(Decimal(value.strip()), unit="", decimals=None, fmt=DEFAULT_FORMAT, conv=conv)
        except RenderError:
            raise
        except (ArithmeticError, ValueError) as e:
            raise RenderError(f"「{value}」无法按规则显示") from e
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return render_number(value, fmt=DEFAULT_FORMAT, conv=conv).replace("|", "¦")


# --------------------------------------------------------------------------
# 标记语法
# --------------------------------------------------------------------------

#: 流式渲染时，一个 [[ 之后攒多少字还没闭合，就认定它不是标记、原样放出去
_HOLD_LIMIT = 160
#: 一个标记（从 [[ 到 ]]，含两头）最长多少字。整篇解析和流式渲染用同一个上限：
#: 超长的两边都当普通文字，流里看到的才会和最终正文一模一样。标记本身最长也就几十个字
MARKER_MAX = _HOLD_LIMIT + 1
#: 紧跟在 [[ 后面：MARKER_MAX 以内就能碰到 ]]（中间不许有方括号和换行）
_MARKER_GUARD = rf"(?=[^\[\]\n]{{0,{MARKER_MAX - 4}}}\]\])"
#: [[kind:body]]。body 里不许有方括号和换行——这样一个没闭合的 [[ 最多吞到行尾
MARKER_RE = re.compile(
    r"\[\[" + _MARKER_GUARD + r"[ \t]*(?P<kind>[A-Za-z]+)[ \t]*:(?P<body>[^\[\]\n]*?)\]\]")
SUPPORTED_KINDS = frozenset({"m", "i", "see", "v", "table", "t", "c", "q"})
#: 三期起没有「以后才支持」的标记了；留着这个名字给老代码 import
LATER_KINDS: frozenset[str] = frozenset()
#: 这次运行没冻结表结构（没查过库、升级前的运行）时，t / c 仍按一期判为解析不了，原因是这一句
LATER_REASON = "本次运行没有记录表结构，无法核对表名和字段名"
#: 升级前（二期）组装的文档里存的原因原文。只拿来认出那样的老文档（_phase_two_cite），不再显示给人：
#: 它说的是「以后才支持」，而那份文档真正的情况是生成于升级前
LEGACY_LATER_REASON = "这种引用在后续版本支持"
#: 认出二期文档后显示的原因
PHASE_TWO_REASON = "这份报告生成于系统升级前，当时不解析这类引用"
#: 沙箱代码节点的产出不能直接引用：它能做任意计算，那种数要进口径卡、留下代入式
CODE_REASON = "沙箱代码计算出的数字不能直接引用，需先登记到口径卡"
#: 受管级别的正式出具，契约里没写 "cells": true 时复核按这个原因拒掉单元格引用
CELLS_REASON = "受管级别出具需要在出具契约中开启「单元格引用」"
#: 节点字段引用：给人看的原因，和交回写作者改写时的指令（_Reason 的 fix）
NODE_FIELD_REASON = "不支持引用节点字段：数字需来自查询单元格或口径卡指标"
NODE_FIELD_FIX = "节点字段引用在后续版本支持：数要从查询单元格 [[v:Q1.r0.列]] 或口径卡指标 [[m:…]] 来"


class _Reason(str):
    """解析不了的原因、核对出的问题：字面是给人看的一句话（证据面板、出具横幅、节点报错都显示它），
    fix 是交回写作者（模型）改写时用的指令，照旧写明标记该怎么写（存成 for_model，describe_violations 用它）。

    两者共用一个字段时，写给模型的「单元格要写成 Q<编号>.r<行>.<列>」「别凭空猜」原样显示在界面上。
    用 str 的子类，是为了让原来传字符串的地方（_unresolved、_violation 的 message）不用改签名。
    """

    fix: str

    def __new__(cls, text: str, fix: str) -> "_Reason":
        obj = super().__new__(cls, text)
        obj.fix = fix
        return obj


def _fix_of(reason: Any) -> dict[str, str]:
    """{"for_model": 给模型的指令}；原因本身就是给模型的那句（没有另写）时为空。键不叫 fix：前端的问题条目里
    fix 是修复 id。"""
    fix = getattr(reason, "fix", None)
    return {"for_model": fix} if fix and fix != str(reason) else {}
#: 整表：不写 rows 时取前几行、最多几行、不写 cols 时最多几列
TABLE_DEFAULT_ROWS = 5
TABLE_MAX_ROWS = 20
TABLE_MAX_COLS = 12
_ROLE = {"t": "entity", "c": "entity", "q": "quote"}
_SEG_KIND = {"t": "entity", "c": "entity", "q": "quote"}


def _parse_body(kind: str, body: str) -> dict[str, Any]:
    if kind == "see":
        return {"refs": [r.strip() for r in body.split(",") if r.strip()]}
    if kind == "table":
        head, *rest = body.split() or [""]
        return {"ref": head, "options": dict(p.split("=", 1) for p in rest if "=" in p)}
    ref, _, tail = body.partition("|")
    if kind == "q":
        return {"ref": ref.strip(), "quote": tail.strip()}
    return {"ref": ref.strip(), "conv": tail.strip() or None}


def parse_markers(text: str) -> list[dict[str, Any]]:
    """文本里所有的引用标记，按出现顺序：{kind, body, raw, start, end, ref / conv / refs / …}。"""
    out: list[dict[str, Any]] = []
    for m in MARKER_RE.finditer(text):
        kind, body = m.group("kind"), m.group("body").strip()
        out.append({"kind": kind, "body": body, "raw": m.group(0), "start": m.start(), "end": m.end(),
                    **_parse_body(kind, body)})
    return out


def _parse_ref(ref: str) -> dict[str, Any]:
    """片段上记的 ref（m:gmv|万）还原成标记的解析结果。"""
    kind, _, body = ref.partition(":")
    kind, body = kind.strip(), body.strip()
    return {"kind": kind, "body": body, **_parse_body(kind, body)}


def placeholder(kind: str, body: str) -> str:
    """解析不了的引用在正文里的占位：⟦?m:gmvx⟧。流里和最终文档里是同一个样子。"""
    return f"⟦?{kind}:{body}⟧"


# --------------------------------------------------------------------------
# 证据目录：alias → 条目
# --------------------------------------------------------------------------


def _metric_sources(
    nodes: dict[str, Any], ledger: list[dict[str, Any]], metrics_from: list[str] | None,
    allowed: set[str] | None,
) -> list[tuple[str, dict[str, Any], str | None]]:
    latest: dict[str, str] = {}
    order: list[str] = []
    for entry in ledger:
        if isinstance(entry, dict) and entry.get("kind") == "metric_set" and entry.get("node_id"):
            order.append(entry["node_id"])
            if entry.get("artifact"):
                latest[entry["node_id"]] = entry["artifact"]
    # 台账里没有、节点产出里有的：旧 checkpoint 续跑时台账是空的，口径卡的产出还在
    order.extend(nid for nid, p in nodes.items() if isinstance(p, dict) and p.get("kind") == "metric_set")
    if metrics_from is not None:
        order = list(metrics_from)
    out = []
    for nid in dict.fromkeys(order):
        payload = nodes.get(nid)
        if allowed is not None and nid not in allowed:
            continue
        if isinstance(payload, dict) and payload.get("kind") == "metric_set":
            out.append((nid, payload, payload.get("artifact") or latest.get(nid)))
    return out


def build_catalog(
    *,
    nodes: dict[str, Any],
    ledger: list[dict[str, Any]] | None = None,
    inputs: dict[str, Any] | None = None,
    metrics_from: list[str] | str | None = None,
    allowed: set[str] | frozenset[str] | list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """从证据台账、节点产出、运行输入建目录。

    - nodes：state["nodes"]，口径卡的指标值从这里取（和台账里的工件是同一份内容）
    - ledger：state["evidence"]，决定顺序、提供工件 id；查询、检索按台账顺序编 Q1… / K1…
    - metrics_from：只收这几张口径卡；不传（None、空列表、空字符串）就收全部——和
      validate_graph 的理解一致，那边空列表就是「所有上游口径卡」
    - allowed：只收这些节点的证据（报告节点传它的祖先），不传不限

    同一个指标 id 出现在两张卡里时不登记 m:<id>，只登记 m:<卡的节点 id>.<id>——
    按先来后到默认给一个，正是旧契约错配的那种错。

    沙箱代码节点的台账条目登记成 N:<节点 id>（kind node_output，code: True）：不编号、不进
    写作目录，只为了有人写 [[v:N:calc.x]] 时能说清为什么不行。

    表和字段（t:<表>、c:<表>.<列>、c:<列>）来自三处：表结构快照（schema 条目）、查询 SQL 用到的
    表、查询结果的列，后两样只认带着表结构快照的查询条目（见 query_entry_fields）。二期的台账
    里没有这些，编不出实体条目，实体核对也就不启用。
    """
    if not metrics_from:
        metrics_from = None
    elif isinstance(metrics_from, str):
        metrics_from = [metrics_from]
    allow = set(allowed) if allowed is not None else None
    entries = [e for e in (ledger or []) if isinstance(e, dict)]
    catalog: dict[str, dict[str, Any]] = {}

    sources = _metric_sources(nodes, entries, metrics_from, allow)
    seen: dict[str, int] = {}
    for _, payload, _ in sources:
        for metric in payload.get("metrics") or []:
            mid = str(metric.get("id") or "")
            seen[mid] = seen.get(mid, 0) + 1
    for node_id, payload, artifact in sources:
        for metric in payload.get("metrics") or []:
            mid = str(metric.get("id") or "")
            if not mid:
                continue
            alias = f"m:{mid}" if seen[mid] == 1 else f"m:{node_id}.{mid}"
            if alias in catalog:
                continue
            try:
                rendered = render_metric(metric)
            except RenderError:
                rendered = MISSING
            locator = {"metric": mid}
            catalog[alias] = {
                "alias": alias, "kind": "metric", "eid": make_eid("metric", artifact, locator),
                "node_id": node_id, "artifact": artifact, "locator": locator,
                "caliber": payload.get("caliber") or "", "version": payload.get("caliber_version") or "",
                "name": metric.get("name") or mid, "unit": metric.get("unit") or "",
                "decimals": metric.get("decimals"), "format": metric.get("format") or DEFAULT_FORMAT,
                "value": metric.get("value"),
                "status": metric.get("status") or ("ok" if metric.get("value") is not None else "missing_input"),
                "rendered": rendered, "label": f"{metric.get('name') or mid} = {rendered}",
            }

    queries = retrievals = 0
    schemas: list[dict[str, Any]] = []
    shaped: list[tuple[dict[str, Any], str]] = []      # 带表结构快照的查询条目和它的全局编号
    for entry in entries:
        if allow is not None and entry.get("node_id") not in allow:
            continue
        if entry.get("kind") == "schema":
            schemas.append(entry)
            continue
        rows = entry.get("rows")
        if entry.get("kind") == "node_output" and entry.get("code_sha"):
            # 沙箱代码节点：登记下来只为了引用它时能说清为什么不行，不编号、不进写作目录
            node_id = str(entry.get("node_id") or "")
            catalog[f"N:{node_id}"] = {
                "alias": f"N:{node_id}", "kind": "node_output", "eid": make_eid("node_output", entry.get("artifact"), {}),
                "locator": {}, "code": True,
                **{k: entry[k] for k in ("node_id", "exec", "artifact", "code_sha", "role", "language") if k in entry},
                "label": f"沙箱代码 {node_id}（{option_label('evidence_role', entry.get('role') or 'compute')}）",
            }
            continue
        if entry.get("kind") == "query":
            queries += 1
            alias, kind = f"Q{queries}", "query"
            label = f"{entry.get('tool') or entry.get('source') or kind}" + (f" · {rows} 行" if isinstance(rows, int) else "")
            if entry.get("schema_artifact") and isinstance(entry.get("tables"), list):
                shaped.append((entry, alias))
        elif entry.get("kind") == "retrieval":
            retrievals += 1
            alias, kind = f"K{retrievals}", "retrieval"
            label = (f"知识库「{entry['source']}」" if entry.get("source") else "知识库检索") \
                + (f" · {rows} 条" if isinstance(rows, int) else "")
        else:
            continue
        catalog[alias] = {
            "alias": alias, "kind": kind, "eid": make_eid(kind, entry.get("artifact"), {}),
            "locator": {},
            **{k: entry[k] for k in ("node_id", "exec", "artifact", "via", "call_id", "tool", "source",
                                     "columns", "rows", "truncated") if k in entry},
            "label": label,
        }
    if schemas or shaped:
        catalog.update(_entity_entries(schemas, shaped))

    for key, value in (inputs or {}).items():
        if not isinstance(key, str) or not key or isinstance(value, (dict, list, tuple)):
            continue
        locator = {"field": key}
        rendered = render_number(value, fmt="plain") if value not in (None, "") else MISSING
        catalog[f"i:{key}"] = {
            "alias": f"i:{key}", "kind": "input", "eid": input_eid(key, value),
            "locator": locator, "value": value, "rendered": rendered, "label": f"{key} = {rendered}",
        }
    return catalog


# --------------------------------------------------------------------------
# 实体：表和字段
# --------------------------------------------------------------------------

ENTITY_KINDS = frozenset({"table", "column"})
UNKNOWN_ENTITY_REASON = "疑似不存在的名称：本次运行的表结构快照、查询用到的表和查询结果列中都没有这个名称"
#: 交回写作者（模型）改写时说的那句，保留原来给模型的写法
_UNKNOWN_ENTITY_FOR_MODEL = "本次运行的表结构快照、查询用到的表、查询结果列里都没有这个名字，可能是编造的名字"
#: 表结构快照不全（库里的表太多、只存了一部分）时找不到的名字：可能在没存下来的表里，只能说核对不了
UNVERIFIED_ENTITY_REASON = "表结构快照仅包含部分表，查询用到的表和查询结果列中也没有这个名称，无法核实它是否存在"
#: 报告节点写了 entities: off：作者有意关掉了表名、字段名核对
ENTITIES_OFF_REASON = "该报告节点已关闭表名、字段名核对，相关标记不会解析"
ENTITIES_OFF_FIX = "这个报告节点关掉了表名、字段名核对（entities: off），[[t:]] / [[c:]] 不解析：直接写名字就行"


def _add_origin(entry: dict[str, Any], origin: dict[str, Any]) -> None:
    key = (origin.get("kind"), origin.get("artifact"), origin.get("alias"))
    if all((o.get("kind"), o.get("artifact"), o.get("alias")) != key for o in entry["sources"]):
        entry["sources"].append(origin)
    alias = origin.get("alias")
    if alias and alias not in entry["queries"]:
        entry["queries"].append(alias)


def _entity_entries(schemas: list[dict[str, Any]], queries: list[tuple[dict[str, Any], str]]) -> dict[str, Any]:
    """表条目 t:<表>、列条目 c:<表>.<列>，没有表名的结果列（聚合的别名）是 c:<列>。

    每个条目的 eid 取它第一次出现的那件工件：表结构快照里有的按快照，只在 SQL / 结果列里出现
    的按那份查询快照。sources 记全三种来历（schema / sql / result），queries 是哪几次查询用到。
    同名字段在好几张表里（id、amount）另编一条 c:<列>，tables 写明是哪几张：正文里只写字段名时
    指的是哪张说不准，但这个名字确实存在。

    已知限制：表按 Python 的 lower() 并，比 SQLite 宽（SQLite 只对 ASCII 不区分大小写）。手工接入的库里同时有
    「Дата」和「дата」两张表时，目录里只有先出现的 t:Дата，后一张的字段记成 c:Дата.<列>：[[c:Дата.y]] 能解析
    （y 其实是 дата 的字段，静默放行），[[t:дата]] 判为疑似编造（误报）；SQL 里写 FROM дата 的查询也记在
    t:Дата 名下。上传的表格碰不到：导入时按 names.collide_key（casefold）判撞名，这样的两张表不会并存。
    没改成按 name_key 并，是因为目录没有版本：复核时它按存下的台账重新拼，不知道文档是哪一版语法，改了
    并法，碰上这种库的老文档复核时就对不上当年的目录。以后给目录也加版本时再按 name_key 并。
    """
    tables: dict[str, dict[str, Any]] = {}
    columns: dict[str, dict[str, Any]] = {}
    #: 小写的表名、全名、全名的末段 → 条目里的表名。lower() 并得比 SQLite 宽，见上面的「已知限制」
    by_lower: dict[str, str] = {}
    owners: dict[str, list[str]] = {}              # 小写的列名 → 表结构里有它的表

    def table(name: str, artifact: Any, *, qualified: str | None = None, source: Any = None) -> str:
        key = by_lower.get(name.lower()) or (by_lower.get(qualified.lower()) if qualified else None)
        if key is None and not qualified and "." in name:
            key = by_lower.get(name.rsplit(".", 1)[-1].lower())     # SQL 里写了别的 schema 前缀的同名表
        if key is None:
            key = name
            tables[key] = {"alias": f"t:{key}", "kind": "table", "eid": make_eid("table", artifact, {"table": key}),
                           "artifact": artifact, "locator": {"table": key}, "name": key,
                           **({"qualified": qualified} if qualified and qualified != key else {}),
                           **({"source": source} if source else {}), "sources": [], "queries": [],
                           "label": f"表 {key}"}
            for k in (key, qualified, key.rsplit(".", 1)[-1]):
                if k:
                    by_lower.setdefault(k.lower(), key)
        return key

    def column(alias: str, name: str, artifact: Any, locator: dict[str, str], source: Any) -> dict[str, Any]:
        if alias not in columns:
            columns[alias] = {"alias": alias, "kind": "column", "eid": make_eid("column", artifact, locator),
                              "artifact": artifact, "locator": locator, "name": name,
                              **({"table": locator["table"]} if "table" in locator else {}),
                              **({"source": source} if source else {}), "sources": [], "queries": [],
                              "label": f"字段 {alias[2:]}"}
        return columns[alias]

    for entry in schemas:
        artifact, source, prefix = entry.get("artifact"), entry.get("source"), entry.get("schema")
        origin = {"kind": "schema", "artifact": artifact, **({"source": source} if source else {}),
                  **({"truncated": True} if entry.get("truncated") else {})}
        for name, cols in (entry.get("tables") or {}).items():
            key = table(str(name), artifact, qualified=f"{prefix}.{name}" if prefix else None, source=source)
            _add_origin(tables[key], origin)
            for col in cols or []:
                col = str(col)
                _add_origin(column(f"c:{key}.{col}", col, artifact, {"table": key, "column": col}, source), origin)
                listed = owners.setdefault(col.lower(), [])
                if key not in listed:
                    listed.append(key)
    by_column = {a.lower(): a for a in columns}
    for entry, alias in queries:
        artifact, source = entry.get("artifact"), entry.get("source")
        extra = {"source": source} if source else {}
        used = []
        for written in entry.get("tables") or []:
            key = table(str(written), artifact, source=source)
            _add_origin(tables[key], {"kind": "sql", "artifact": artifact, "alias": alias, **extra})
            used.append(key)
        for col in entry.get("columns") or []:
            col = str(col)
            mine = [a for t in used if (a := by_column.get(f"c:{t}.{col}".lower()))]
            target = columns[mine[0]] if len(mine) == 1 else column(f"c:{col}", col, artifact, {"column": col}, source)
            _add_origin(target, {"kind": "result", "artifact": artifact, "alias": alias, **extra})
    for lowered, keys in owners.items():
        if len(keys) < 2:
            continue
        first = columns[by_column[f"c:{keys[0]}.{lowered}".lower()]]
        target = column(f"c:{first['name']}", first["name"], first["artifact"], {"column": first["name"]},
                        first.get("source"))
        target["tables"] = list(keys)
        for key in keys:
            for origin in columns[by_column[f"c:{key}.{lowered}".lower()]]["sources"]:
                if origin["kind"] == "schema":
                    _add_origin(target, origin)
    return {**{e["alias"]: e for e in tables.values()}, **{e["alias"]: e for e in columns.values()}}


#: 反引号里、正文里像标识符的名字，以及 1 版的 [[t:]] / [[c:]]：ASCII 字母或下划线开头，最多四段用点连起来。
#: 反引号核对（_checked_name）和裸名链接（_BARE_NAME）两版都只认这个：放宽到 Unicode 的话，反引号里的
#: 「华东」这种业务词会被判成可疑实体，中文正文没有词边界，整句会被当成一个名字
_ASCII_NAME = r"[A-Za-z_][A-Za-z0-9_$#]*(?:\.[A-Za-z_][A-Za-z0-9_$#]*){0,3}"
_ASCII_IDENTIFIER = re.compile(rf"^{_ASCII_NAME}$")
#: 2 版的 [[t:]] / [[c:]]：每段字母或汉字开头，只含字母、数字、汉字、下划线（names.name_problem 的字符集），
#: 另认 1 版就认的 $ #；段之间仍用「.」分隔（[[c:明细.金额]]）
_UNICODE_IDENTIFIER = re.compile(r"^[^\W\d][\w$#]*(?:\.[^\W\d][\w$#]*){0,3}$")


def _entity_ref_ok(ref: str, syntax: int) -> bool:
    """[[t:]] / [[c:]] 里的名字写法对不对，按实体语法版本。

    2 版另外挡住 names.name_problem 也挡的两类字符：不等于自身 NFKC 形式的（全角「ＡＢＣ」、带圈数字「①」）——
    正文照原样显示，和 ABC 肉眼难分，SQLite 里却是另一张表（它不做归一，names.name_key 也不做）；以及填充字符
    （\\w 把它们算作字母，肉眼看不出来）。填充字符里 U+3164、U+FFA0 经 NFKC 会变，上一条就挡住了；U+115F、U+1160 是 NFKC 稳定的，只能靠 names.FILLERS 挡。
    长度和 SQL 关键字不管：手工接入的库里有长名字、有叫 order 的表，1 版也认。
    """
    if syntax < 2:
        return bool(_ASCII_IDENTIFIER.match(ref))
    return bool(_UNICODE_IDENTIFIER.match(ref)) and ref == unicodedata.normalize("NFKC", ref) \
        and not any(ch in FILLERS for ch in ref)


#: 反引号里写了这些不算名字：SQL 关键字、字面量、常用函数
_SQL_WORDS = frozenset({
    "null", "true", "false", "none", "nan", "select", "from", "where", "group", "order", "by", "having", "limit",
    "join", "left", "right", "inner", "outer", "on", "as", "and", "or", "not", "in", "is", "like", "between",
    "case", "when", "then", "else", "end", "distinct", "union", "all", "with", "sum", "count", "avg", "min",
    "max", "asc", "desc", "date", "now", "offset", "insert", "update", "delete"})
#: 正文里的裸名字：这些常用英文词就算恰好是表名也不自动链接，免得满屏下划线
_COMMON_WORDS = frozenset({
    "id", "name", "date", "time", "type", "status", "value", "data", "info", "key", "code", "text", "title",
    "count", "sum", "total", "year", "month", "day", "week", "user", "users", "item", "items", "list", "table",
    "log", "note", "notes", "order", "group", "level", "rank", "score", "price", "amount", "region",
    "city", "source", "target", "event", "events", "result", "results", "report", "detail", "details"})


class _EntityIndex:
    """按名字找目录里的表和字段。不分大小写；全名（schema.表）和末段都认。

    表和字段的查法分开（t: 只找表，c: 只找字段）。正文里的名字两样都试：先整名当表，再当
    字段（表.字段、只有字段名），最后才把 schema.表 按末段认成表——「orders.amount」不能因为
    恰好有一张叫 amount 的表就认成表。

    「不分大小写」按实体语法版本的口径（self.key）：2 版用 name_key，只对 ASCII 不区分大小写，和 SQLite 比较
    标识符一致（ABC 和 abc 是一张表，Дата 和 дата、ＯＲＤＥＲＳ 和 orders 都不是）；1 版照旧用 Python 的 lower()，
    老文档按当年的口径复核。
    目录本身怎么并表不分版本（_entity_entries 按 lower() 并），这里只管查：只差非 ASCII 大小写的两张表在目录里
    已经是一条，见那里的说明。
    """

    def __init__(self, catalog: dict[str, Any], *, syntax: int | None = None) -> None:
        self.catalog = catalog
        self.key: Callable[[str], str] = name_key if (ENTITY_SYNTAX if syntax is None else syntax) >= 2 \
            else str.lower
        self.tables: dict[str, str] = {}
        self.columns: dict[str, str] = {}
        self.owners: dict[str, list[str]] = {}
        self.others: set[str] = set()
        self.described: set[str] = set()           # 表结构快照里列出了全部字段的表（目录键）
        self.partial = False                       # 有表结构快照不全（探查时被截断）
        for alias, entry in catalog.items():
            if not isinstance(entry, dict):
                continue
            kind, locator = entry.get("kind"), entry.get("locator") or {}
            if kind == "table":
                frozen = [o for o in entry.get("sources") or [] if isinstance(o, dict) and o.get("kind") == "schema"]
                if frozen:
                    self.described.add(alias)
                    self.partial = self.partial or any(o.get("truncated") for o in frozen)
                name = str(entry.get("name") or alias[2:])
                for key in (name, entry.get("qualified"), name.rsplit(".", 1)[-1]):
                    if key:
                        self.tables.setdefault(self.key(str(key)), alias)
            elif kind == "column":
                col = str(locator.get("column") or entry.get("name") or "")
                if locator.get("table"):
                    self.columns.setdefault(self.key(f"{locator['table']}.{col}"), alias)
                    self.owners.setdefault(self.key(col), []).append(alias)
                else:
                    self.columns.setdefault(self.key(col), alias)
            elif kind == "metric":
                self.others.add(self.key(str(locator.get("metric") or "")))
            elif kind == "input":
                self.others.add(self.key(str(locator.get("field") or "")))
            self.others.add(self.key(alias))
        self.active = bool(self.tables or self.columns)

    def table(self, name: str, *, tail: bool = True) -> str | None:
        lowered = self.key(name)
        hit = self.tables.get(lowered)
        if hit is None and tail and "." in lowered:
            hit = self.tables.get(lowered.rsplit(".", 1)[-1])
        return hit

    def column(self, name: str) -> str | None:
        lowered = self.key(name)
        if lowered in self.columns:
            return self.columns[lowered]
        if "." in lowered:
            head, col = lowered.rsplit(".", 1)
            owner = self.table(head)
            if owner is None:
                return None
            return self.columns.get(self.key(f"{self.catalog[owner]['name']}.{col}"))
        mine = self.owners.get(lowered) or []
        return mine[0] if len(mine) == 1 else None

    def find(self, name: str, kind: str | None = None) -> tuple[str, str] | None:
        """(table | column, alias)，找不到返回 None。"""
        if kind in (None, "table") and (hit := self.table(name, tail=kind == "table")):
            return "table", hit
        if kind in (None, "column") and (hit := self.column(name)):
            return "column", hit
        if kind is None and (hit := self.table(name)):
            return "table", hit
        return None

    def known(self, name: str) -> bool:
        """这个名字在这次运行里有没有出处：表、字段，或者指标 id、运行输入、证据编号。"""
        return self.find(name) is not None or self.key(name) in self.others

    def unsure(self, name: str) -> bool:
        """一个找不到的名字是不是只能说「核对不了」：表结构快照不全时，它可能在没存下来的表里。

        快照里列出的表，字段是全的：「orders.ghost」里的 orders 在快照里，ghost 找不到就是真没有。
        """
        if not self.partial:
            return False
        head = name.rsplit(".", 1)[0] if "." in name else ""
        owner = self.table(head) if head else None
        return owner is None or owner not in self.described


def find_entity(name: str, catalog: dict[str, Any], kind: str | None = None, *,
                syntax: int | None = None) -> str | None:
    """按名字找表或字段的目录键（t:orders、c:orders.amount），找不到返回 None。kind 为 table / column 时只找那一种。
    syntax 是实体语法版本（决定大小写口径），缺省当前版。"""
    hit = _EntityIndex(catalog, syntax=syntax).find(name.strip(), kind)
    return hit[1] if hit else None


def closest_entities(name: str, catalog: dict[str, Any], *, limit: int = 3) -> list[str]:
    """和一个可疑的名字最像的已知表、字段（目录键，最多 limit 个），给「你是不是想写…」用。"""
    names: dict[str, str] = {}
    for alias, entry in catalog.items():
        if isinstance(entry, dict) and entry.get("kind") in ENTITY_KINDS:
            names.setdefault(alias[2:].lower(), alias)
            names.setdefault(str(entry.get("name") or "").lower(), alias)
    names.pop("", None)
    picked = difflib.get_close_matches(name.strip().lower(), list(names), n=limit * 3, cutoff=0.6)
    return list(dict.fromkeys(names[p] for p in picked))[:limit]


# --------------------------------------------------------------------------
# 解析一个引用
# --------------------------------------------------------------------------


def _unresolved(marker: dict[str, Any], alias: str, ev_kind: str, reason: str, *,
                rendered: str | None = None) -> dict[str, Any]:
    """解析不了的引用。rendered 是正文里显示的字：缺值显示「—」，其余是占位 ⟦?m:gmvx⟧。
    reason 是 _Reason 时另记 for_model（给模型的改写指令）；存进文档前去掉（_stored）。"""
    kind, body = marker["kind"], marker.get("body", "")
    return {"ref": marker.get("ref") or "", "alias": alias, "kind": ev_kind,
            "role": _ROLE.get(kind, "value"), "status": "unresolved", "reason": str(reason), **_fix_of(reason),
            "rendered": rendered if rendered is not None else placeholder(kind, body)}


def _metric_alias(ref: str, catalog: dict[str, Any]) -> tuple[str | None, str]:
    alias = f"m:{ref}"
    if alias in catalog:
        return alias, ""
    doubles = sorted(a for a in catalog if a.startswith("m:") and a.endswith(f".{ref}"))
    if doubles:
        return None, _Reason(f"指标「{ref}」同时出现在多张口径卡中，无法确定引用的是哪一个",
                             f"指标 {ref} 同时出现在好几张口径卡里，要写成带卡名的形式："
                             + "、".join(f"[[{a}]]" for a in doubles[:4]))
    return None, _Reason(f"引用的指标「{ref}」不存在", f"目录里没有指标 {ref}（能用的指标见证据目录）")


# --------------------------------------------------------------------------
# 单元格与整表：值一律从查询快照取
# --------------------------------------------------------------------------

#: 取快照的函数：工件 id → 内容。取不到返回 None，哈希对不上抛 ValueError（artifact_store.load 的约定）
Loader = Callable[[str], Any]


class _Snapshots:
    """这一次解析里取过的快照（查询、检索），以及按名字找表和字段的索引。

    一张整表每一格都要读同一份快照，每格都读盘、复验一次哈希不值。缓存只活在这一次
    compose / verify / 流式渲染里：出口复核是另一次调用，照样重新取、重新验。
    """

    def __init__(self, loader: Loader | None = None, *, entities: bool = True, syntax: int | None = None) -> None:
        self.loader = loader or load_artifact
        self.memo: dict[str, tuple[Any, str | None]] = {}
        self._index: tuple[dict[str, Any], _EntityIndex] | None = None
        #: False：报告节点写了 entities: off，目录里就算有表和字段也整层不管
        self.entities_on = entities
        #: 这一次解析按哪一版实体语法：组装、流式渲染用当前版，复核按文档记下的版本（verify_doc）
        self.syntax = ENTITY_SYNTAX if syntax is None else syntax

    def entities(self, catalog: dict[str, Any]) -> _EntityIndex:
        if self._index is None or self._index[0] is not catalog:
            shown = catalog if self.entities_on else \
                {a: e for a, e in catalog.items() if not (isinstance(e, dict) and e.get("kind") in ENTITY_KINDS)}
            self._index = (catalog, _EntityIndex(shown, syntax=self.syntax))
        return self._index[1]

    def with_syntax(self, syntax: int) -> "_Snapshots":
        """换一版实体语法的同一次解析。取过的快照共用：快照内容和语法版本无关，不必再取、再验一遍。"""
        if syntax == self.syntax:
            return self
        twin = _Snapshots(self.loader, entities=self.entities_on, syntax=syntax)
        twin.memo = self.memo
        return twin

    def get(self, alias: str, artifact: Any) -> tuple[Any, str | None]:
        """查询快照：(内容, 取不到的原因)。原因以 alias 开头，能直接当 unresolved 的 reason。"""
        content, why = self._fetch(artifact, "查询快照")
        if why is None and (not isinstance(content, dict) or not isinstance(content.get("rows"), list)):
            why = "的快照里没有表格数据"
        return (None, f"{alias} {why}") if why else (content, None)

    def hits(self, alias: str, artifact: Any) -> tuple[list[Any], str | None]:
        """检索快照里的命中：(命中列表, 取不到的原因)。"""
        content, why = self._fetch(artifact, "检索快照")
        if why is None and (not isinstance(content, dict) or not isinstance(content.get("hits"), list)):
            why = "的快照里没有检索命中"
        return ([], f"{alias} {why}") if why else (content["hits"], None)

    def _fetch(self, artifact: Any, noun: str) -> tuple[Any, str | None]:
        key = str(artifact or "")
        if key not in self.memo:
            self.memo[key] = self._load(key)
        content, problem = self.memo[key]
        if problem is None:
            return content, None
        if problem == "missing":
            return None, f"没有{noun}"
        if problem == "tampered":
            return None, f"的{noun}与哈希不一致，疑似被修改"
        if problem == "absent":
            return None, f"的{noun}已不存在，无法读取"
        return None, f"的{noun}无法读取"

    def _load(self, artifact: str) -> tuple[Any, str | None]:
        if not artifact:
            return None, "missing"
        try:
            content = self.loader(artifact)
        except ValueError:
            return None, "tampered"
        except Exception as e:  # noqa: BLE001 - 取不回来就是解析不了，不能让整篇报告崩掉
            return None, type(e).__name__
        return (None, "absent") if content is None else (content, None)


def _snapshots(loader: Loader | _Snapshots | None, *, entities: bool = True,
               syntax: int | None = None) -> _Snapshots:
    """loader 已经是 _Snapshots 时沿用它（entities 不看）；给了 syntax 就换成那一版。"""
    if isinstance(loader, _Snapshots):
        return loader if syntax is None else loader.with_syntax(syntax)
    return _Snapshots(loader, entities=entities, syntax=syntax)


_CELL_REF = re.compile(r"^(?P<alias>[QK]\d+)\.r(?P<row>\d+)\.(?P<column>.+)$")
_NODE_REF = re.compile(r"^N:(?P<node>[^.\s]+)(?:\.(?P<field>.+))?$")


def parse_cell_ref(ref: str) -> dict[str, Any] | None:
    """Q4.r0.amount → {alias: "Q4", row: 0, column: "amount"}；写法不对返回 None。"""
    m = _CELL_REF.match((ref or "").strip())
    return {"alias": m.group("alias"), "row": int(m.group("row")), "column": m.group("column")} if m else None


def _query_entry(alias: str, catalog: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    entry = catalog.get(alias)
    if entry is None:
        return None, _Reason(f"引用的查询结果 {alias} 不存在", f"目录里没有 {alias}（能引用的查询见证据目录）")
    if entry.get("kind") != "query":
        return None, f"{alias} 不是查询结果，不能按单元格引用"
    if entry.get("code"):
        return None, CODE_REASON
    return entry, ""


def _resolve_cell(marker: dict[str, Any], catalog: dict[str, Any], snaps: _Snapshots,
                  cells_allowed: bool) -> dict[str, Any]:
    ref = marker.get("ref") or ""
    conv = marker.get("conv")
    node = _NODE_REF.match(ref)
    if node:
        alias = f"N:{node.group('node')}"
        entry = catalog.get(alias) or {}
        return _unresolved(marker, alias, "node_field",
                           CODE_REASON if entry.get("code") else _Reason(NODE_FIELD_REASON, NODE_FIELD_FIX))
    parsed = parse_cell_ref(ref)
    if parsed is None:
        return _unresolved(marker, ref.split(".")[0], "cell", _Reason(
            "单元格引用格式无法识别", "单元格要写成 Q<编号>.r<行>.<列>（行号从 0 数），比如 [[v:Q1.r0.amount]]"))
    alias = parsed["alias"]
    if not cells_allowed:
        return _unresolved(marker, alias, "cell", CELLS_REASON)
    entry, why = _query_entry(alias, catalog)
    if entry is None:
        return _unresolved(marker, alias, "cell", why)
    snapshot, why = snaps.get(alias, entry.get("artifact"))
    if why:
        return _unresolved(marker, alias, "cell", why)
    try:
        raw, column = locate_cell(snapshot, parsed["row"], parsed["column"])
        kind = column_kind(snapshot, column)
        rendered = render_cell(raw, conv, kind=kind)
    except CellError as e:
        return _unresolved(marker, alias, "cell", f"{alias} {e}")
    except RenderError as e:
        return _unresolved(marker, alias, "cell", str(e))
    locator = {"row": parsed["row"], "column": column}
    cite = {"ref": ref, "alias": alias, "locator": locator, "eid": cell_eid(entry.get("artifact"), **locator),
            "kind": "cell", "role": "value", "status": "resolved", "value": cell_value(raw, kind),
            "rendered": rendered}
    if conv:
        cite["conv"] = conv
    return cite


_TABLE_OPTIONS = ("cols", "rows")
_TABLE_ROWS = re.compile(r"^(\d+)(?:-(\d+))?$")
#: 列名里有这些字就写不进 [[v:]] 标记，或者会把表格行切错
_UNSAFE_COLUMN = re.compile(r"[\[\]\n|]")


def _table_plan(marker: dict[str, Any], catalog: dict[str, Any], snaps: _Snapshots,
                cells_allowed: bool) -> tuple[str | None, dict[str, Any] | None]:
    """整表标记 → (Markdown 表，每格是一个 [[v:]] 标记, None)，或者 (None, 解析不了的 Citation)。

    表由系统生成，但每一格照样是单元格引用：切块、切片段、复核都走单元格那条路，
    不用为整表另写一套核对规则。
    """
    alias = marker.get("ref") or ""

    def fail(reason: str, fix: str | None = None) -> tuple[None, dict[str, Any]]:
        return None, _unresolved(marker, alias, "query", _Reason(reason, fix) if fix else reason)

    if not cells_allowed:
        return fail(CELLS_REASON)
    _, *rest = (marker.get("body") or "").split() or [""]
    odd = [p for p in rest if p.partition("=")[0] not in _TABLE_OPTIONS or "=" not in p]
    if odd:
        return fail(f"整表引用中有无法识别的选项「{odd[0]}」", f"不认识的选项 {odd[0]}：整表只支持 cols=列a,列b 和 rows=0-4")
    options = marker.get("options") or {}
    entry, why = _query_entry(alias, catalog)
    if entry is None:
        return fail(why)
    snapshot, why = snaps.get(alias, entry.get("artifact"))
    if why:
        return fail(why)
    columns = [str(c) for c in snapshot.get("columns") or []]
    rows = snapshot["rows"]
    if "cols" in options:
        wanted = [c.strip() for c in options["cols"].split(",") if c.strip()]
        if not wanted:
            return fail("整表引用的 cols 没有写列名", "cols= 后面要写列名，用逗号分开")
        missing = [c for c in wanted if c not in columns]
        if missing:
            return fail(f"引用的查询结果 {alias} 中没有列「{missing[0]}」",
                        f"{alias} 没有列「{missing[0]}」，有：{'、'.join(columns[:12])}")
        if len(wanted) > TABLE_MAX_COLS:
            return fail(f"整表最多 {TABLE_MAX_COLS} 列，当前选了 {len(wanted)} 列",
                        f"整表最多 {TABLE_MAX_COLS} 列，cols= 写了 {len(wanted)} 列")
    else:
        wanted = columns
        if len(wanted) > TABLE_MAX_COLS:
            return fail(f"{alias} 有 {len(columns)} 列，超过整表上限 {TABLE_MAX_COLS} 列",
                        f"{alias} 有 {len(columns)} 列，整表最多 {TABLE_MAX_COLS} 列：用 cols=列a,列b 挑出要的列")
    if not wanted:
        return fail(f"{alias} 没有列，无法生成表格")
    if not rows:
        return fail(f"{alias} 为空（0 行），无法生成表格")
    if "rows" in options:
        span = _TABLE_ROWS.match(options["rows"].strip())
        first, last = (int(span.group(1)), int(span.group(2) or span.group(1))) if span else (1, 0)
        if last < first:
            return fail(f"整表引用的行号范围格式有误，当前为「{options['rows']}」",
                        f"rows 要写成 0-4 这样从小到大的行号范围（从 0 数），写的是 {options['rows']}")
        if last - first + 1 > TABLE_MAX_ROWS:
            return fail(f"整表最多 {TABLE_MAX_ROWS} 行，当前选了 {last - first + 1} 行",
                        f"整表最多 {TABLE_MAX_ROWS} 行，rows={options['rows']} 是 {last - first + 1} 行："
                        "挑出要的行，或者分成几张表")
        if last >= len(rows):
            return fail(f"{alias} 只有 {len(rows)} 行，没有第 {last} 行（行号从 0 开始）")
    else:
        first, last = 0, min(TABLE_DEFAULT_ROWS, len(rows)) - 1
    for c in wanted:
        if not c.strip() or c != c.strip() or _UNSAFE_COLUMN.search(c) \
                or len(f"[[v:{alias}.r{last}.{c}]]") > MARKER_MAX:
            return fail(f"列名「{_clean(c)[:30]}」无法放进表格（为空、首尾有空格、含方括号 / 竖线 / 换行，或过长）",
                        f"列名「{_clean(c)[:30]}」写不进表格（空的、首尾有空格、带方括号竖线换行，或者太长）："
                        "在 SQL 里给它起个别名再查")
    lines = ["| " + " | ".join(_clean(c) for c in wanted) + " |",
             "| " + " | ".join("---" for _ in wanted) + " |"]
    lines += ["| " + " | ".join(f"[[v:{alias}.r{r}.{c}]]" for c in wanted) + " |" for r in range(first, last + 1)]
    return "\n".join(lines), None


def _expand_tables(text: str, catalog: dict[str, Any], snaps: _Snapshots, cells_allowed: bool,
                   prev: str = "") -> str:
    """把能展开的整表标记换成 Markdown 表（每格一个 [[v:]]）。展开不了的原样留着，后面按解析不了处理。

    表格要独占几行：标记前面的字不是换行就补一个，后面的字不是换行也补一个。这只取决于
    紧挨着标记的那两个字——prev 是这段文本之前的那个字（流式渲染时在上一块里）。
    """
    out: list[str] = []
    last = 0
    for m in MARKER_RE.finditer(text):
        if m.group("kind") != "table":
            continue
        body = m.group("body").strip()
        table, _ = _table_plan({"kind": "table", "body": body, **_parse_body("table", body)}, catalog, snaps,
                               cells_allowed)
        if table is None:
            continue
        before = text[m.start() - 1] if m.start() > 0 else prev
        after = text[m.end()] if m.end() < len(text) else ""
        out.append(text[last:m.start()])
        out.append(("" if before in ("", "\n") else "\n") + table + ("" if after in ("", "\n") else "\n"))
        last = m.end()
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


# --------------------------------------------------------------------------
# 解析一个行内标记
# --------------------------------------------------------------------------


def resolve_marker(marker: dict[str, Any], catalog: dict[str, Any], *, cells_allowed: bool = True,
                   loader: Loader | _Snapshots | None = None) -> dict[str, Any]:
    """行内标记（m / i / v / t / c / q / table）→ Citation，带 rendered（正文里显示的字）。

    cells_allowed=False 时单元格和整表一律解析不了，原因是 CELLS_REASON（受管出具没在契约里声明 cells）。
    loader 是取查询快照的函数，缺省 artifact_store.load。
    """
    kind = marker["kind"]
    ref = marker.get("ref") or ""
    if kind == "v":
        return _resolve_cell(marker, catalog, _snapshots(loader), cells_allowed)
    if kind == "table":
        snaps = _snapshots(loader)
        table, bad = _table_plan(marker, catalog, snaps, cells_allowed)
        if bad is not None:
            return bad
        # 整篇渲染时整表已经按上下文展开过了，走到这里的是复核一个片段：给出不带上下文的样子
        return {"ref": ref, "alias": ref, "locator": {}, "eid": catalog[ref]["eid"], "kind": "query",
                "role": "value", "status": "resolved", "rendered": _render(table or "", catalog, snaps, cells_allowed)}
    if kind in ("t", "c"):
        return _resolve_entity(marker, catalog, _snapshots(loader))
    if kind == "q":
        return _resolve_quote(marker, catalog, _snapshots(loader))
    if kind not in ("m", "i"):
        return _unresolved(marker, f"{kind}:{ref}", kind, _Reason(
            f"引用类型「{kind}」无法识别",
            f"不认识的引用类型 {kind}：只支持 m（口径卡指标）、v（查询单元格）、table（整表）、"
            "i（运行输入）、t / c（表 / 字段）、q（引文）、see（依据）"))

    conv = marker.get("conv")
    if kind == "m":
        ev_kind = "metric"
        alias, why = _metric_alias(ref, catalog)
        if alias is None:
            return _unresolved(marker, f"m:{ref}", ev_kind, why)
        entry = catalog[alias]
        if entry.get("value") is None:
            return _unresolved(marker, alias, ev_kind, f"指标「{entry.get('name') or ref}」本次没有值（缺少输入），"
                               "无法写入报告", rendered=MISSING)
        try:
            rendered = render_metric(entry, conv)
        except RenderError as e:
            return _unresolved(marker, alias, ev_kind, str(e))
    else:
        ev_kind, alias = "input", f"i:{ref}"
        entry = catalog.get(alias)
        if entry is None:
            return _unresolved(marker, alias, ev_kind, f"运行输入中没有「{ref}」")
        if entry.get("value") in (None, ""):
            return _unresolved(marker, alias, ev_kind, f"运行输入「{ref}」为空", rendered=MISSING)
        try:
            rendered = render_number(entry["value"], fmt="plain", conv=conv)
        except RenderError as e:
            return _unresolved(marker, alias, ev_kind, str(e))
    cite = {"ref": ref, "alias": alias, "locator": dict(entry.get("locator") or {}), "eid": entry["eid"],
            "kind": ev_kind, "role": "value", "status": "resolved", "value": entry.get("value"),
            "rendered": rendered}
    if conv:
        cite["conv"] = conv
    return cite


def _resolve_entity(marker: dict[str, Any], catalog: dict[str, Any], snaps: _Snapshots) -> dict[str, Any]:
    """[[t:orders]] / [[c:orders.amount]]：显示名字本身（照写作者写的大小写），出处是目录里的表或字段。

    报告节点关掉了实体层（entities: off）时照实说关掉了；这次运行没冻结表结构时没有东西可以核对，
    按一期的样子判为解析不了。名字哪里都找不到的是可疑实体（unknown: True）：它不是写法错了，是可能
    编造了一个名字——出口按缺口处理，不拦。表结构快照不全时找不到的名字只是核对不了（unverified: True）。
    """
    kind, ref = marker["kind"], (marker.get("ref") or "").strip()
    ev_kind, other = ("table", "column") if kind == "t" else ("column", "table")
    alias = f"{kind}:{ref}"
    index = snaps.entities(catalog)
    if not snaps.entities_on:
        return _unresolved(marker, alias, ev_kind, _Reason(ENTITIES_OFF_REASON, ENTITIES_OFF_FIX))
    if not index.active:
        return _unresolved(marker, alias, ev_kind, LATER_REASON)
    if not ref or not _entity_ref_ok(ref, snaps.syntax):
        return _unresolved(marker, alias, ev_kind, _Reason(
            "表名、字段名引用格式无法识别",
            "表名、字段名要写成 [[t:表名]]、[[c:表名.字段名]]，名字是字母或下划线开头的标识符" if snaps.syntax < 2
            else "表名、字段名要写成 [[t:表名]]、[[c:表名.字段名]]，名字以字母、汉字或下划线开头，只含字母、数字、"
                 "汉字和下划线，不含空格、括号和全角字符"))
    hit = index.find(ref, ev_kind)
    if hit is None:
        if index.find(ref, other):
            right = "c" if kind == "t" else "t"
            return _unresolved(marker, alias, ev_kind, _Reason(
                f"「{ref}」是{'字段' if kind == 't' else '表'}，不是{'表' if kind == 't' else '字段'}",
                f"{ref} 是{'字段' if kind == 't' else '表'}，不是{'表' if kind == 't' else '字段'}：写成 [[{right}:{ref}]]"))
        if index.unsure(ref):
            cite = _unresolved(marker, alias, ev_kind, UNVERIFIED_ENTITY_REASON, rendered=ref)
            cite["unverified"] = True
            return cite
        cite = _unresolved(marker, alias, ev_kind, UNKNOWN_ENTITY_REASON, rendered=ref)
        cite["unknown"] = True
        return cite
    entry = catalog[hit[1]]
    return {"ref": ref, "alias": hit[1], "locator": dict(entry.get("locator") or {}), "eid": entry["eid"],
            "kind": ev_kind, "role": "entity", "status": "resolved", "rendered": ref}


# --------------------------------------------------------------------------
# 引文：[[q:K1|原话]] 要在检索快照的命中片段里逐字出现
# --------------------------------------------------------------------------

#: 引文至少几个字（空白归一化之后）：「东区」这种两个字的「原话」哪里都找得到，证明不了什么
QUOTE_MIN = 4


def _wide(ch: str) -> bool:
    """中日韩文字和全角标点：它们之间的空白是排版（换行、PDF 断行），不是词的分隔。"""
    code = ord(ch)
    return (0x2E80 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFFEF
            or 0x3000 <= code <= 0x303F)


def _normalized(text: str) -> tuple[str, list[int]]:
    """空白归一化：连续空白并成一个空格、去掉首尾，挨着中文或全角字符的空白直接去掉。

    同时记下归一化后每个字在原文里的位置，命中之后能换算回原文的起止。
    """
    out: list[str] = []
    where: list[int] = []
    gap: int | None = None
    for i, ch in enumerate(text):
        if ch.isspace():
            if out and gap is None:
                gap = i
            continue
        if gap is not None and not (_wide(out[-1]) or _wide(ch)):
            out.append(" ")
            where.append(gap)
        gap = None
        out.append(ch)
        where.append(i)
    return "".join(out), where


def normalize_quote(text: str) -> str:
    """引文比对前的样子：见 _normalized。大小写、标点、全半角都不改——要的是逐字。"""
    return _normalized(text or "")[0]


def find_quote(quote: str, hits: list[Any]) -> tuple[int, int, int] | None:
    """引文落在哪条命中、原文里的 [起, 止)。按命中的顺序找第一处；找不到返回 None。"""
    wanted = normalize_quote(quote)
    if not wanted:
        return None
    for i, hit in enumerate(hits):
        content = hit.get("content") if isinstance(hit, dict) else None
        if not isinstance(content, str):
            continue
        flat, where = _normalized(content)
        at = flat.find(wanted)
        if at >= 0:
            return i, where[at], where[at + len(wanted) - 1] + 1
    return None


def _resolve_quote(marker: dict[str, Any], catalog: dict[str, Any], snaps: _Snapshots) -> dict[str, Any]:
    """引文显示引文本身；原话对不上时照样显示（写作者的话），只是标成没有出处。

    目录里没有这次检索、快照取不回来时没有东西可以对，和别的解析不了的引用一样显示占位。
    """
    alias = (marker.get("ref") or "").strip()
    quote = _clean(str(marker.get("quote") or ""))
    entry = catalog.get(alias)
    if entry is None:
        return _unresolved(marker, alias, "quote", _Reason(
            f"引用的检索结果 {alias} 不存在", f"目录里没有 {alias}（能引原话的知识库检索见证据目录）"))
    if entry.get("kind") != "retrieval":
        return _unresolved(marker, alias, "quote", f"{alias} 不是知识库检索结果，引文只能引用检索命中的原文")
    if not quote:
        return _unresolved(marker, alias, "quote", _Reason("引文为空", "引文是空的：写成 [[q:K1|原话]]"))
    hits, why = snaps.hits(alias, entry.get("artifact"))
    if why:
        return _unresolved(marker, alias, "quote", why)
    if len(normalize_quote(quote)) < QUOTE_MIN:
        return _unresolved(marker, alias, "quote", f"引文太短（至少 {QUOTE_MIN} 个字），无法确认是否为原文",
                           rendered=quote)
    found = find_quote(quote, hits)
    if found is None:
        return _unresolved(marker, alias, "quote", _Reason(
            f"在 {alias} 的检索结果中找不到这段引文的原文",
            f"{alias} 的命中片段里找不到这句原话（空白归一化后逐字比对）：引文要一字不差地抄原文"), rendered=quote)
    at, start, end = found
    hit = hits[at]
    locator = {"hit": at, "start": start, "end": end}
    source = {"artifact": entry.get("artifact"), "document": hit.get("document_id"), "chunk": hit.get("chunk_id"),
              "title": hit.get("title"), "ordinal": hit.get("ordinal")}
    return {"ref": alias, "alias": alias, "locator": locator, "eid": make_eid("quote", entry.get("artifact"), locator),
            "kind": "quote", "role": "quote", "status": "resolved", "rendered": quote,
            "source": {k: v for k, v in source.items() if v is not None}}


_ROW_REF = re.compile(r"^(?P<alias>[QK]\d+)(?:\.r(?P<row>\d+))?$")


def resolve_support(ref: str, catalog: dict[str, Any], *, loader: Loader | _Snapshots | None = None) -> dict[str, Any]:
    """[[see:]] 里的一个依据：m:gmv、i:week、Q4、Q4.r0、K2、t:orders、c:orders.amount。只核对「这件证据存在」。"""
    ref = ref.strip()
    kind, sep, body = ref.partition(":")
    base = {"ref": ref, "role": "support"}
    if sep and kind in ("t", "c"):
        cite = _resolve_entity({"kind": kind, "ref": body, "body": body}, catalog, _snapshots(loader))
        return {**base, **{k: cite[k] for k in ("alias", "kind", "status", "eid", "locator", "reason", "for_model",
                                                "unknown", "unverified") if k in cite}}
    if sep and kind == "q":
        return {**base, "alias": ref, "kind": "quote", "status": "unresolved",
                "reason": "引文不能作为依据，依据应引用检索结果的编号",
                "for_model": "引文不能当依据：依据写那次检索的编号，比如 [[see:K1]]"}
    alias: str | None = None
    locator: dict[str, Any] = {}
    why: str = _Reason(f"引用的依据「{ref}」不存在", f"目录里没有 {ref}")
    if sep and kind == "m":
        alias, why = _metric_alias(body, catalog)
    elif sep and kind == "i":
        alias = ref if ref in catalog else None
        why = f"运行输入中没有「{body}」"
    elif sep:
        why = _Reason(f"依据「{ref}」的写法无法识别", f"不认识的依据写法 {ref}：写 m:指标、i:输入，或者 Q1 / K1 这样的编号")
    elif m := _ROW_REF.match(ref):
        alias = m.group("alias") if m.group("alias") in catalog else None
        if alias and m.group("row") is not None:
            row, total = int(m.group("row")), catalog[alias].get("rows")
            if isinstance(total, int) and row >= total:
                alias, why = None, f"{m.group('alias')} 只有 {total} 行，没有第 {row} 行（行号从 0 开始）"
            else:
                locator = {"row": row}
    elif f"m:{ref}" in catalog:
        alias = f"m:{ref}"          # 漏写了 m: 前缀的指标 id，意思没有歧义
    if alias is None:
        return {**base, "alias": ref, "kind": kind if sep else "unknown", "status": "unresolved",
                "reason": str(why), **_fix_of(why)}
    entry = catalog[alias]
    return {**base, "alias": alias, "kind": entry["kind"], "status": "resolved", "eid": entry["eid"],
            "locator": {**(entry.get("locator") or {}), **locator}}


def resolve_ref(ref: str, catalog: dict[str, Any], *, cells_allowed: bool = True,
                loader: Loader | _Snapshots | None = None) -> dict[str, Any]:
    """片段上记的 ref（m:gmv|万、i:week、v:Q1.r0.amount）→ Citation。证据接口点开片段时用它重新解析。"""
    return resolve_marker(_parse_ref(ref), catalog, cells_allowed=cells_allowed, loader=loader)


#: 数字标记后面重复写的单位：最多隔几个空格、单位最长几个字。流式渲染放出一个数字标记之前，
#: 要等到它后面至少有 UNIT_LOOKAHEAD 个字（或者一个换行）才能判断，多出来的 1 是单位后面那个字
_UNIT_GAP = 2
_UNIT_MAX = 10
UNIT_LOOKAHEAD = _UNIT_GAP + _UNIT_MAX + 1
_UNIT_KINDS = frozenset({"m", "i", "v"})
#: 渲染结果末尾的单位：最后一个数字后面的字（12,345人次 → 人次，4.57万元 → 万元，12.5% → %）
_UNIT_TAIL = re.compile(r"\d([^\d\s][^\d]*)$")
_BLANK = " \t\u3000"
_SEE_RUN = re.compile(r"\[\[" + _MARKER_GUARD + r"[ \t]*see[ \t]*:[^\[\]\n]*?\]\]"
                      r"(?:[" + _BLANK + r"]*\[\[" + _MARKER_GUARD + r"[ \t]*see[ \t]*:[^\[\]\n]*?\]\])*")


def _rendered_unit(marker: dict[str, Any], catalog: dict[str, Any], snaps: _Snapshots,
                   cells_allowed: bool) -> str:
    cite = resolve_marker(marker, catalog, cells_allowed=cells_allowed, loader=snaps)
    if cite["status"] != "resolved" or not is_number(cite.get("value")):
        return ""
    tail = _UNIT_TAIL.search(str(cite.get("rendered") or ""))
    return tail.group(1) if tail and len(tail.group(1)) <= _UNIT_MAX else ""


def _tidy(text: str, catalog: dict[str, Any], snaps: _Snapshots, cells_allowed: bool, prev: str = "") -> str:
    """渲染前收拾两处排版，数和引用都不动：

    - 数字标记后面重复写的单位去掉。[[m:visits]] 渲染出来已经带单位（12,345人次），写作者常常
      还跟着写一遍（[[m:visits]] 人次），读者看到的是「12,345人次 人次」。单字的单位后面紧跟着字时
      不动：写作者写的可能是另一个单位（口径卡是「人」，正文写「人次」），硬删会把不一致藏起来；
    - [[see:…]] 前面的空白去掉。see 渲染成空，「最高月份 [[see:Q2]]。」会剩下「最高月份 。」。
      see 后面紧跟着字、要靠这个空白隔开的（「A [[see:Q1]]B」）留着；行首的缩进不动。

    prev 是这段文字前面那个字（流式渲染分块时用），判断空白是不是在行首。
    """
    cuts: list[tuple[int, int]] = []
    for marker in parse_markers(text):
        if marker["kind"] not in _UNIT_KINDS:
            continue
        unit = _rendered_unit(marker, catalog, snaps, cells_allowed)
        if not unit:
            continue
        hit = re.compile(f"[{_BLANK}]{{0,{_UNIT_GAP}}}" + re.escape(unit)).match(text, marker["end"])
        if hit:
            after = text[hit.end():hit.end() + 1]
            if len(unit) > 1 or not after.isalnum():
                cuts.append((marker["end"], hit.end()))
    for run in _SEE_RUN.finditer(text):
        start = run.start()
        while start > 0 and text[start - 1] in _BLANK:
            start -= 1
        before = text[start - 1] if start > 0 else prev
        after = text[run.end():run.end() + 1]
        # 几个 see 之间的空白也一样渲染成多余的空格
        parts = list(MARKER_RE.finditer(text, run.start(), run.end()))
        cuts.extend((m.end(), n.start()) for m, n in zip(parts, parts[1:]) if m.end() < n.start())
        if start < run.start() and before not in ("", "\n") and not (after.isalnum() or after == "["):
            cuts.append((start, run.start()))
    if not cuts:
        return text
    out, last = [], 0
    for a, b in sorted(cuts):
        if a < last:
            continue
        out.append(text[last:a])
        last = b
    out.append(text[last:])
    return "".join(out)


def _render(text: str, catalog: dict[str, Any], snaps: _Snapshots, cells_allowed: bool, prev: str = "") -> str:
    text = _tidy(_expand_tables(text, catalog, snaps, cells_allowed, prev), catalog, snaps, cells_allowed, prev)
    out: list[str] = []
    last = 0
    for marker in parse_markers(text):
        out.append(text[last:marker["start"]])
        if marker["kind"] != "see":
            out.append(resolve_marker(marker, catalog, cells_allowed=cells_allowed, loader=snaps)["rendered"])
        last = marker["end"]
    out.append(text[last:])
    return "".join(out)


def render_markers(text: str, catalog: dict[str, Any], *, cells_allowed: bool = True,
                   loader: Loader | None = None) -> str:
    """把一段文本里的标记全部换成字：m / i / v 渲染成值，整表展开成表，see 去掉，解析不了的换成占位。"""
    return _render(text, catalog, _snapshots(loader), cells_allowed)


# --------------------------------------------------------------------------
# 流式渲染：让 llm.token 里出现的就是最终的数字
# --------------------------------------------------------------------------

#: 文字末尾的空白和 [[see:…]]：流式渲染先攒着，等后面的字来了再一起交给 _tidy
_TRAILING_SEE = re.compile(r"(?:[" + _BLANK + r"]|\[\[" + _MARKER_GUARD + r"[ \t]*see[ \t]*:[^\[\]\n]*?\]\])+$")


def _safe_cut(text: str, has_unit: Callable[[dict[str, Any]], bool] | None = None) -> int:
    """能放出去的前缀有多长：最后一个还可能长成标记的 [[ 之前都能放。

    一个标记最长 MARKER_MAX 个字，所以没闭合的时候最多攒 MARKER_MAX - 1 = _HOLD_LIMIT 个字；
    超过了、或者碰到换行，它就不可能再是标记。末尾单独一个 [ 也要留着——下一块的
    开头可能是另一个 [。

    整表标记闭合了也先不放：它展开时要看紧跟在后面的那个字（是不是换行），那个字还没到。
    _tidy 要往后看的也先不放：末尾的空白和 [[see:…]]（后面来的字决定空白去不去掉），
    以及后面还没攒够 UNIT_LOOKAHEAD 个字的数字标记（后面可能是重复的单位）。has_unit 判断一个
    标记渲染出来带不带单位，不带的（运行输入里的周次、没有单位的格）不用攒；不给就都攒。
    """
    cut = len(text)
    start = text.rfind("[[")
    if start != -1 and text.find("]]", start) == -1:
        tail = text[start:]
        if len(tail) <= _HOLD_LIMIT and "\n" not in tail:
            cut = start
    if cut == len(text) and text.endswith("["):
        cut = len(text) - 1
    while True:
        held = cut
        start = text.rfind("[[", 0, held)
        m = MARKER_RE.match(text, start) if start != -1 else None
        if m and m.end() == held and m.group("kind") == "table":
            held = start
        if (trail := _TRAILING_SEE.search(text, 0, held)) and trail.start() < held:
            held = trail.start()
        numeric = [m for m in MARKER_RE.finditer(text, 0, held) if m.group("kind") in _UNIT_KINDS
                   and (has_unit is None or has_unit(parse_markers(m.group(0))[0]))]
        if numeric and len(text[numeric[-1].end():held]) < UNIT_LOOKAHEAD \
                and "\n" not in text[numeric[-1].end():held]:
            held = numeric[-1].start()
        if held == cut:
            return cut
        cut = held



class StreamRenderer:
    """边收模型的 token 边渲染：缓冲到没有未闭合的 [[ 为止，再把这一段里的标记换成字。

        renderer = StreamRenderer(catalog)
        async for chunk in model.astream(messages):
            if text := renderer.feed(message_text(chunk)):
                ctx.emit(EventType.LLM_TOKEN, delta=text)
        if tail := renderer.flush():
            ctx.emit(EventType.LLM_TOKEN, delta=tail)

    所有 feed / flush 的输出拼起来，等于 render_markers(全文)，不论模型怎么分块：
    整篇解析也只认 MARKER_MAX 以内的标记，超长的两边都原样当文字；整表展开要看的
    前一个字记在 prev 里，后一个字靠 _safe_cut 等它到了再放。
    """

    def __init__(self, catalog: dict[str, Any], *, cells_allowed: bool = True, loader: Loader | None = None) -> None:
        self.catalog = catalog
        self.cells_allowed = cells_allowed
        self.snaps = _Snapshots(loader)
        self.pending = ""
        self.prev = ""

    def _has_unit(self, marker: dict[str, Any]) -> bool:
        return bool(_rendered_unit(marker, self.catalog, self.snaps, self.cells_allowed))

    def _release(self, ready: str) -> str:
        if not ready:
            return ""
        out = _render(ready, self.catalog, self.snaps, self.cells_allowed, self.prev)
        self.prev = ready[-1]
        return out

    def feed(self, delta: str) -> str:
        self.pending += delta or ""
        cut = _safe_cut(self.pending, self._has_unit)
        ready, self.pending = self.pending[:cut], self.pending[cut:]
        return self._release(ready)

    def flush(self) -> str:
        ready, self.pending = self.pending, ""
        return self._release(ready)


# --------------------------------------------------------------------------
# 切块：和前端 Markdown.tsx 认同一套块语法（标题、段落、列表、表格、引用、代码、分隔线）
#
# 在模型的原文（带标记）上切，而不是在渲染后的正文上切：一个指标值渲染出来恰好是
# 「3. 」开头，不能因此把一句话当成有序列表。
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```([A-Za-z0-9_]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^(\s*>\s?)(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _is_block_start(line: str) -> bool:
    return bool(_FENCE.match(line) or _HR.match(line) or _HEADING.match(line) or _BULLET.match(line)
                or _ORDERED.match(line) or _QUOTE.match(line) or _TABLE_ROW.match(line))


class _Item:
    """一个将来的 unit（或者一段要再切成句子的内容）：原文上的若干片 (s|c, start, end)。"""

    __slots__ = ("pieces", "pos", "split", "kind", "loc", "depth")

    def __init__(self, pieces: list[tuple[str, int, int]], pos: int, *, split: bool = False,
                 kind: str | None = None, loc: dict[str, int] | None = None, depth: int | None = None):
        self.pieces, self.pos, self.split, self.kind = pieces, pos, split, kind
        self.loc, self.depth = loc, depth


class _Block:
    __slots__ = ("type", "meta", "items")

    def __init__(self, type_: str, meta: dict[str, Any], items: list[_Item]):
        self.type, self.meta, self.items = type_, meta, items


def _scan_blocks(text: str, protected: list[tuple[int, int]]) -> list[_Block]:
    lines = text.split("\n")
    starts: list[int] = []
    acc = 0
    for line in lines:
        starts.append(acc)
        acc += len(line) + 1

    def end(i: int) -> int:
        return starts[i] + len(lines[i])

    blocks: list[_Block] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            j = i + 1
            while j < n and not _FENCE.match(lines[j]):
                j += 1
            body = [("c", starts[i + 1], end(j - 1))] if j > i + 1 else []
            last = min(j, n - 1)
            blocks.append(_Block("code", {"lang": fence.group(1)},
                                 [_Item(body, body[0][1] if body else end(last), kind="code")]))
            i = j + 1
            continue

        if _HR.match(line):
            blocks.append(_Block("hr", {}, [_Item([], end(i), kind="connective")]))
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            cs = starts[i] + heading.start(2)
            pieces = [("s", starts[i], cs)] + ([("c", cs, end(i))] if cs < end(i) else [])
            blocks.append(_Block("heading", {"level": len(heading.group(1))},
                                 [_Item(pieces, starts[i], kind="heading")]))
            i += 1
            continue

        if _TABLE_ROW.match(line) and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            items = _table_cells(lines[i], starts[i], -1, protected)
            i += 2
            row = 0
            while i < n and _TABLE_ROW.match(lines[i]):
                items.extend(_table_cells(lines[i], starts[i], row, protected))
                row += 1
                i += 1
            blocks.append(_Block("table", {}, items))
            continue

        if _QUOTE.match(line):
            pieces: list[tuple[str, int, int]] = []
            first = i
            while i < n and (q := _QUOTE.match(lines[i])):
                if i > first:
                    pieces.append(("c", starts[i] - 1, starts[i]))     # 行与行之间的换行属于正文
                cs = starts[i] + len(q.group(1))
                pieces.append(("s", starts[i], cs))
                if cs < end(i):
                    pieces.append(("c", cs, end(i)))
                i += 1
            blocks.append(_Block("quote", {}, [_Item(pieces, starts[first], split=True)]))
            continue

        bullet, ordered = _BULLET.match(line), _ORDERED.match(line)
        if bullet or ordered:
            items = []
            meta: dict[str, Any] = {"ordered": bool(ordered)}
            if ordered:
                meta["start"] = int(ordered.group(2))
            while i < n:
                b = _BULLET.match(lines[i])
                o = None if b else _ORDERED.match(lines[i])
                if b or o:
                    m = b or o
                    cs = starts[i] + m.start(2 if b else 3)
                    pieces = [("s", starts[i], cs)] + ([("c", cs, end(i))] if cs < end(i) else [])
                    items.append(_Item(pieces, starts[i],
                                       depth=len(m.group(1).replace("\t", "  ")) // 2))
                    i += 1
                elif lines[i].strip() and not _is_block_start(lines[i]) and items:
                    # 悬挂缩进的续行接到上一项：换行算正文，行首缩进算结构（Markdown.tsx 会 trim 掉）
                    lead = len(lines[i]) - len(lines[i].lstrip())
                    stop = len(lines[i].rstrip())
                    items[-1].pieces.append(("c", starts[i] - 1, starts[i]))
                    if lead:
                        items[-1].pieces.append(("s", starts[i], starts[i] + lead))
                    items[-1].pieces.append(("c", starts[i] + lead, starts[i] + stop))
                    i += 1
                else:
                    break
            blocks.append(_Block("list", meta, items))
            continue

        first = i
        i += 1
        while i < n and lines[i].strip() and not _is_block_start(lines[i]):
            i += 1
        blocks.append(_Block("paragraph", {}, [_Item([("c", starts[first], end(i - 1))], starts[first],
                                                     split=True)]))
    return blocks


def _table_cells(line: str, base: int, row: int, protected: list[tuple[int, int]]) -> list[_Item]:
    """一行表格拆成格。和 Markdown.tsx 的 splitRow 一样按 | 拆，只是标记里的 |（[[m:x|万]]）不算。"""
    lo = len(line) - len(line.lstrip())
    hi = len(line.rstrip())
    if line[lo:lo + 1] == "|":
        lo += 1
    if hi > lo and line[hi - 1] == "|":
        hi -= 1
    inside = [(s - base, e - base) for s, e in protected if base <= s and e <= base + len(line)]
    cuts = [p for p in range(lo, hi) if line[p] == "|" and not any(s <= p < e for s, e in inside)]
    items = []
    for col, (a, b) in enumerate(zip([lo, *[c + 1 for c in cuts]], [*cuts, hi])):
        ca = a + len(line[a:b]) - len(line[a:b].lstrip())
        cb = a + len(line[a:b].rstrip())
        pieces = [("c", base + ca, base + cb)] if ca < cb else []
        items.append(_Item(pieces, base + (ca if ca < cb else a), loc={"row": row, "col": col}))
    return items


def _attach_gaps(items: list[_Item], length: int) -> bool:
    """没被任何一项认领的原文（空行、块尾换行、表格分隔行、代码围栏）作为结构片挂到后一项上。

    挂好之后，所有项的片按顺序拼起来必须正好铺满原文——这是「片段拼起来等于正文」
    的来源。铺不满说明切块有漏洞，返回 False，调用方退回整篇一段。
    """
    covered = sorted((s, e) for item in items for _, s, e in item.pieces if e > s)
    gaps: list[tuple[int, int]] = []
    pos = 0
    for s, e in covered:
        if s > pos:
            gaps.append((pos, s))
        pos = max(pos, e)
    if pos < length:
        gaps.append((pos, length))
    if not items:
        return not gaps
    positions = [item.pos for item in items]
    for s, e in gaps:
        k = bisect.bisect_left(positions, e)
        (items[k] if k < len(items) else items[-1]).pieces.append(("s", s, e))
    for item in items:
        item.pieces = sorted((p for p in item.pieces if p[2] > p[1]), key=lambda p: p[1])
    pos = 0
    for item in items:
        for _, s, e in item.pieces:
            if s != pos:
                return False
            pos = e
    return pos == length


# --------------------------------------------------------------------------
# 切句：段落和引用按句子切成 unit；[[see:]] 挂在它前面那一句
# --------------------------------------------------------------------------

_TERMINATOR = re.compile(r"(?:[。！？!?]+|\.(?=\s|$)|\n)[”’\"'）)」』】》]*")
_SEE_TAIL = re.compile(r"(?:[ \t\u3000]*\[\[" + _MARKER_GUARD + r"[ \t]*see[ \t]*:[^\[\]\n]*?\]\])*\s*")
#: 和 Markdown.tsx 的 INLINE 同一套：句号落在代码、链接、粗体里面时不切，不然一对 ** 会被拆到两句
_INLINE = re.compile("|".join([
    r"(`+)[^`]+?\1",
    r"\[[^\]]+\]\([^)\s]+[^)]*\)",
    r"\*\*[^*]+?\*\*",
    r"__[^_]+?__",
    r"(?<!\*)\*[^*\n]+?\*(?!\*)",
    r"~~[^~]+?~~",
]))
_ONLY_SEE = re.compile(r"^(?:\s|\[\[" + _MARKER_GUARD + r"[ \t]*see[ \t]*:[^\[\]\n]*?\]\])*$")


def _split_sentences(item: _Item, text: str, markers: list[dict[str, Any]]) -> list[list[tuple[str, int, int]]]:
    content = [(s, e) for kind, s, e in item.pieces if kind == "c"]
    if not item.split or not content:
        return [item.pieces]
    # 把内容片接成一条虚拟文本 V，记下 V 的每个位置对应原文哪里
    v_parts, v_map = [], []
    for s, e in content:
        v_map.append((sum(len(p) for p in v_parts), s, e))
        v_parts.append(text[s:e])
    virtual = "".join(v_parts)

    def to_raw(v: int) -> int:           # V 里第 v 个字符之前的那个边界，落在原文哪里
        for v0, s, e in v_map:
            if v0 < v <= v0 + (e - s):
                return s + (v - v0)
        return content[0][0]

    def to_v(raw: int) -> int | None:
        for v0, s, e in v_map:
            if s <= raw <= e:
                return v0 + (raw - s)
        return None

    protected = [(m.start(), m.end()) for m in _INLINE.finditer(virtual)]
    for marker in markers:
        a, b = to_v(marker["start"]), to_v(marker["end"])
        if a is not None and b is not None:
            protected.append((a, b))

    cuts: list[int] = []
    for m in _TERMINATOR.finditer(virtual):
        if any(s < m.start() < e or s < m.end() < e for s, e in protected):
            continue
        cut = _SEE_TAIL.match(virtual, m.end()).end()
        if 0 < cut < len(virtual) and not any(s < cut < e for s, e in protected):
            cuts.append(cut)
    cuts = sorted(set(cuts))
    # 只剩空白或者只有 [[see:]] 的一截不单独成句，并进相邻的那一句
    kept: list[int] = []
    last = 0
    for k, cut in enumerate(cuts):
        following = cuts[k + 1] if k + 1 < len(cuts) else len(virtual)
        if _ONLY_SEE.match(virtual[last:cut]) or _ONLY_SEE.match(virtual[cut:following]):
            continue
        kept.append(cut)
        last = cut
    raw_cuts = [to_raw(v) for v in kept]

    groups: list[list[tuple[str, int, int]]] = [[]]
    queue = list(raw_cuts)
    for kind, s, e in item.pieces:
        while queue and kind == "c" and s < queue[0] < e:
            groups[-1].append((kind, s, queue[0]))
            groups.append([])
            s = queue.pop(0)
        if queue and s >= queue[0]:
            groups.append([])
            queue.pop(0)
        groups[-1].append((kind, s, e))
    return [g for g in groups if g]


# --------------------------------------------------------------------------
# 裸数字
# --------------------------------------------------------------------------

_STRUCT_BEFORE = re.compile(r"(?:第|前|后|首|末|近|最近|Top|TOP|top|No\.|NO\.)\s*$")
_STRUCT_AFTER = re.compile(
    r"\s*(?:个(?:方面|原因|问题|建议|要点|维度|阶段|步骤|部分|层面|角度|环节|关键|特点|措施)"
    r"|点(?:建议|原因|看法)|条建议|步)")


def _structural_number(text: str, token: Any) -> bool:
    """「前 3 名」「第 2 季度」「从 3 个方面看」「3 月」：序号和结构，不是结论数字。限 20 以内。"""
    value = token.value
    if token.decimals or token.is_percent or not float(value).is_integer() or not 0 < value <= 20:
        return False
    before, after = text[max(0, token.start - 6):token.start], text[token.end:token.end + 8]
    if _STRUCT_BEFORE.search(before) or _STRUCT_AFTER.match(after):
        return True
    # 行文中间的列举：（1）…（2）…、1、…2、…（行首的「1. 」「2、」出具校验本来就放过）
    if (re.search(r"[（(]\s*$", before) and re.match(r"\s*[)）]", after)) or after.startswith("、"):
        return True
    return bool((value <= 12 and re.match(r"\s*月", after))
                or (value <= 31 and re.match(r"\s*[日号]", after))
                or (value <= 4 and re.match(r"\s*季度", after)))


#: 结构片段涂白时只涂 Markdown 符号本身，数字和字母原样留着：「100. 」前端照样显示成
#: <ol start=100>，「```45678」的语言标签显示在代码块头上——这些数字和正文过同一套规则
#: （1–2 位的行首序号照旧放过，py3 这类标识符照旧不算）。表格竖线涂成一个不是空白的字：
#: 表格第一格里的「1.」不在行首，不能沾行首序号免检的光
_SYNTAX_BLANK = {**{c: " " for c in "`>#*+-~=:"}, "|": "\u00a6"}


def _bare_numbers(markdown: str, masks: list[tuple[int, int]], allow: list[Any] | None,
                  syntax: list[tuple[int, int]] = ()) -> list[Any]:
    """正文里没有出处的数字，再用出具校验同一套规则抽数字。

    - masks：引用渲染出来的字，整段涂白（保留换行，行首序号的免检规则要认得行首）
    - syntax：结构片段，只涂 Markdown 符号（见 _SYNTAX_BLANK），里面的数字照样要查
    """
    chars = list(markdown)
    for s, e in masks:
        for k in range(max(s, 0), min(e, len(chars))):
            if chars[k] != "\n":
                chars[k] = " "
    for s, e in syntax:
        for k in range(max(s, 0), min(e, len(chars))):
            chars[k] = _SYNTAX_BLANK.get(chars[k], chars[k])
    masked = "".join(chars)
    allowed = number_allowance(allow)
    return [t for t in extract_numbers(masked) if not allowed(t) and not _structural_number(masked, t)]


def _context(text: str, start: int, end: int) -> str:
    return text[max(0, start - 18):min(len(text), end + 18)].replace("\n", " ")


# --------------------------------------------------------------------------
# 组装文档
# --------------------------------------------------------------------------

#: 粗体：**…** 或 __…__，里面可以有标记（标记里的下划线不算收尾，[[m:refund_rate]] 很常见）
_BOLD = re.compile(r"(\*\*|__)(?P<inner>(?:\[\[" + _MARKER_GUARD + r"[^\[\]\n]*?\]\]|(?!\1)[^\n])+?)\1")
#: 方向词、因果词：没有数字也没有引用的句子，含这些词才算结论。「同比」「环比」不在里面：它们只说明
#: 跟谁比，本身不是涨跌；真正的比较结论还会带「增长」「下降」或者数字
_DIRECTIONAL = re.compile(
    r"增长|增加|上升|提升|提高|上涨|下降|下滑|减少|降低|回落|下跌|反弹|超过|高于|低于|领先|落后|最高|最低"
    r"|最多|最少|来自|导致|因为|由于|带动|拉动|拖累|驱动|原因|归因|贡献|占比|翻倍|持平|波动"
    r"|(?<![A-Za-z])(?:increase|decrease|grow|growth|decline|drop|rise|due to|because|driven)(?![A-Za-z])",
    re.I)
#: 定义句：「同比是将本期与上年同月对比」「增长率是指……」。解释一个概念怎么算，不是对数据下结论
_DEFINITION = re.compile(r"是将|是指|指的是|定义为|的定义是|含义是|计算方[式法][为是]|计算公式[为是]")
#: 定义句里出现这些词就不按定义算：「增长主要是将促销提前带来的」说的是原因
_CAUSAL = re.compile(
    r"导致|因为|由于|带动|拉动|拖累|驱动|原因|归因|贡献|带来|所致|造成|引起|使得"
    r"|(?<![A-Za-z])(?:due to|because|driven)(?![A-Za-z])",
    re.I)


def _normalize_strong(raw: str) -> tuple[str, list[tuple[int, int]]]:
    """包着引用标记的粗体去掉星号，记下哪一段要加粗：**销售额 [[m:gmv]]** → 销售额 [[m:gmv]]。

    星号留在正文里的话，数字一切成单独的片段，两边的文字片段各剩半对 **，前端只能
    原样显示星号。不含标记的粗体原样留着，它整个落在一个文字片段里，前端照常渲染。
    """
    out: list[str] = []
    ranges: list[tuple[int, int]] = []
    last = size = 0
    for m in _BOLD.finditer(raw):
        inner = m.group("inner")
        if not MARKER_RE.search(inner):
            continue
        out.append(raw[last:m.start()])
        size += m.start() - last
        ranges.append((size, size + len(inner)))
        out.append(inner)
        size += len(inner)
        last = m.end()
    out.append(raw[last:])
    return "".join(out), ranges


# --------------------------------------------------------------------------
# 实体的自动链接：反引号里的名字一律核对，正文里的裸名字只链接明显是标识符的
# --------------------------------------------------------------------------

_CODE_NAME = re.compile(r"^`([^`\n]+)`$")
#: 复核时找反引号：粗体、链接里面的也算——组装时它们没切成片段（切开会拆坏粗体），违规照记
_CODE_SPAN = re.compile(r"(?<![`\\])`([^`\n]+?)`(?!`)")
_BARE_NAME = re.compile(rf"(?<![A-Za-z0-9_$#.`/\\@]){_ASCII_NAME}(?![A-Za-z0-9_$#])")


def _checked_name(name: str) -> bool:
    """反引号里的这段是不是要核对的名字：像标识符、不止一个字、不是 SQL 关键字和常用函数。"""
    return len(name) >= 2 and bool(_ASCII_IDENTIFIER.match(name)) and name.lower() not in _SQL_WORDS


def _bare_link(name: str, catalog: dict[str, Any], index: _EntityIndex) -> tuple[str, str] | None:
    """正文里的裸名字要不要自动链接：保守，只链接一眼就是标识符的。

    表.列 这种全名、和表名一字不差的（大小写也一样）、带下划线或数字或大小写混排的（order_id、
    orderId）才链接；name、date、id 这类常用词，哪怕恰好是字段名、表名，也不链接，免得满屏下划线。
    """
    if name.lower() in _COMMON_WORDS:
        return None
    hit = index.find(name)
    if hit is None:
        return None
    entry = catalog[hit[1]]
    if "." in name or (hit[0] == "table" and name in (entry.get("name"), entry.get("qualified"))):
        return hit
    mixed = any(ch.isupper() for ch in name[1:]) and any(ch.islower() for ch in name)
    if "_" in name or any(ch.isdigit() for ch in name) or mixed:
        return hit
    return None


def _stored(cite: dict[str, Any]) -> dict[str, Any]:
    """存进文档的样子：去掉 for_model。那是交回写作者改写时用的指令，复核时按目录重新解析会再得到，
    文档里只留给人看的 reason。"""
    return {k: v for k, v in cite.items() if k != "for_model"} if "for_model" in cite else cite


def _entity_segment(name: str, hit: tuple[str, str], catalog: dict[str, Any], *, code: bool) -> dict[str, Any]:
    kind, alias = hit
    entry = catalog[alias]
    cite = {"ref": name, "alias": alias, "locator": dict(entry.get("locator") or {}), "eid": entry["eid"],
            "kind": kind, "role": "entity", "status": "resolved", "rendered": name}
    seg = {"kind": "entity", "text": f"`{name}`" if code else name, "ref": f"{'t' if kind == 'table' else 'c'}:{name}",
           "cite": cite, "state": "deterministic", "auto": True}
    if code:
        seg["code"] = True
    return seg


def _mentions(text: str, catalog: dict[str, Any], index: _EntityIndex) -> list[tuple[int, int, dict[str, Any]]]:
    """一段文字里要切出来的实体：(起, 止, 片段)。只切顶层的行内代码和粗体、链接外面的裸名字。"""
    found: list[tuple[int, int, dict[str, Any]]] = []
    protected: list[tuple[int, int]] = []
    for m in _INLINE.finditer(text):
        protected.append((m.start(), m.end()))
        code = _CODE_NAME.match(m.group(0))
        name = code.group(1) if code else ""
        if not code or not _checked_name(name):
            continue
        if hit := index.find(name):
            found.append((m.start(), m.end(), _entity_segment(name, hit, catalog, code=True)))
        elif not index.known(name):
            issue = "unverified_entity" if index.unsure(name) else "unknown_entity"
            found.append((m.start(), m.end(), {"kind": "entity", "text": m.group(0), "state": "none",
                                               "issue": issue, "code": True, "name": name}))
    for m in _BARE_NAME.finditer(text):
        if any(a <= m.start() < b for a, b in protected):
            continue
        if hit := _bare_link(m.group(0), catalog, index):
            found.append((m.start(), m.end(), _entity_segment(m.group(0), hit, catalog, code=False)))
    return sorted(found, key=lambda f: f[0])


def _link_entities(segments: list[dict[str, Any]], catalog: dict[str, Any],
                   index: _EntityIndex) -> list[dict[str, Any]]:
    """把文字片段里的表名、字段名切成实体片段。只重新切片，正文一个字不改。"""
    out: list[dict[str, Any]] = []
    for seg in segments:
        found = _mentions(seg["text"], catalog, index) if seg["kind"] == "text" else []
        if not found:
            out.append(seg)
            continue
        s0, text = seg["span"][0], seg["text"]
        extra = {"strong": True} if seg.get("strong") else {}
        cursor = 0
        for a, b, piece in found:
            if cursor < a:
                out.append({"kind": "text", "text": text[cursor:a], "span": [s0 + cursor, s0 + a],
                            "state": seg["state"], **extra})
            out.append({**piece, "span": [s0 + a, s0 + b], **extra})
            cursor = b
        if cursor < len(text):
            out.append({"kind": "text", "text": text[cursor:], "span": [s0 + cursor, s0 + len(text)],
                        "state": seg["state"], **extra})
    return out


def _auto_entity(seg: dict[str, Any]) -> bool:
    """系统在正文里自动切出来的实体片段（自动链接的名字、反引号里的可疑名字），不是写作者写的标记。"""
    return seg.get("kind") == "entity" and (bool(seg.get("auto")) or not seg.get("ref"))


def _classify(kind: str | None, segments: list[dict[str, Any]], see: list[dict[str, Any]]) -> str:
    """unit 的种类。本期没有裁判，按确定的规则分：引用了证据、写了数字、或者有方向词、
    因果词的算结论；其余的是连接性的话。

    自动链接的名字按文字算：它只是把正文重新切了一刀，句子算不算结论和开没开实体层无关。

    没有数字、没有引用的定义句（「环比是将本期与上月对比，反映短期波动」）也算连接性的话：
    它在解释概念，方向词只是定义的一部分。带因果词的不算定义句，照旧是结论。
    """
    if kind:
        return kind
    if see or any(s["kind"] not in ("text", "structural") and not _auto_entity(s) for s in segments):
        return "claim"
    words = "".join(s["text"] for s in segments if s["kind"] == "text" or _auto_entity(s))
    if _DIRECTIONAL.search(words) and not (_DEFINITION.search(words) and not _CAUSAL.search(words)):
        return "claim"
    return "connective"


def compose_doc(
    raw: str,
    catalog: dict[str, Any],
    *,
    node_id: str = "",
    run_id: str | None = None,
    allow_numbers: list[Any] | None = None,
    cells_allowed: bool = True,
    loader: Loader | None = None,
    entities: bool = True,
) -> dict[str, Any]:
    """模型写的原文（带标记）→ 报告文档：渲染后的 markdown、块、句、片段、统计、违规。

    entities=False（报告节点 entities: off）：表名、字段名整层不管——不链接、不核对反引号，
    [[t:]] / [[c:]] 解析不了，原因是 ENTITIES_OFF_REASON。

    stats / violations 由 verify_doc 算出，和出口契约复核用的是同一个函数。
    整表标记先展开成每格一个 [[v:]] 的 Markdown 表，之后和模型自己写的表格走同一条路；
    doc["source"] 仍是模型的原文。
    """
    source = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    snaps = _Snapshots(loader, entities=entities)
    text, strong = _normalize_strong(_tidy(_expand_tables(source, catalog, snaps, cells_allowed),
                                           catalog, snaps, cells_allowed))
    markers = parse_markers(text)
    for marker in markers:
        marker["strong"] = any(a <= marker["start"] and marker["end"] <= b for a, b in strong)

    blocks = _scan_blocks(text, [(m["start"], m["end"]) for m in markers])
    items = [item for block in blocks for item in block.items]
    if not _attach_gaps(items, len(text)):
        # 切块出了漏洞：整篇当一段，宁可版式粗糙，也不能让哪个字落在片段外面没人查
        blocks = [_Block("paragraph", {}, [_Item([("c", 0, len(text))], 0, split=True)])] if text else []
        items = [item for block in blocks for item in block.items]
        _attach_gaps(items, len(text))

    parts: list[str] = []
    size = 0
    out_blocks: list[dict[str, Any]] = []
    all_segments: list[dict[str, Any]] = []

    def push(seg: dict[str, Any], segments: list[dict[str, Any]]) -> None:
        nonlocal size
        prev = segments[-1] if segments else None
        if prev is not None and seg["kind"] in ("text", "structural") and prev["kind"] == seg["kind"] \
                and prev.get("strong") == seg.get("strong") and prev["span"][1] == size:
            prev["text"] += seg["text"]
            prev["span"][1] += len(seg["text"])
        else:
            seg["span"] = [size, size + len(seg["text"])]
            segments.append(seg)
        parts.append(seg["text"])
        size += len(seg["text"])

    for block in blocks:
        units: list[dict[str, Any]] = []
        for item in block.items:
            for pieces in _split_sentences(item, text, markers):
                segments: list[dict[str, Any]] = []
                see: list[dict[str, Any]] = []
                anchor = size
                for kind, s, e in pieces:
                    if kind == "s":
                        push({"kind": "structural", "text": text[s:e], "state": "neutral"}, segments)
                        continue
                    def plain(a: int, b: int) -> None:
                        # 文字按粗体的边界再切一刀：一半加粗一半不加粗的不能并成一段
                        cuts = sorted({a, b, *(x for r in strong for x in r if a < x < b)})
                        for x, y in zip(cuts, cuts[1:]):
                            seg = {"kind": "text", "text": text[x:y], "state": "neutral"}
                            if any(p <= x and y <= q for p, q in strong):
                                seg["strong"] = True
                            push(seg, segments)

                    cursor = s
                    for marker in markers:
                        if marker["start"] < s or marker["end"] > e:
                            continue
                        if cursor < marker["start"]:
                            plain(cursor, marker["start"])
                        cursor = marker["end"]
                        if marker["kind"] == "see":
                            see.extend(_stored(resolve_support(r, catalog, loader=snaps)) for r in marker["refs"])
                            continue
                        cite = _stored(resolve_marker(marker, catalog, cells_allowed=cells_allowed, loader=snaps))
                        ok = cite["status"] == "resolved"
                        seg = {
                            "kind": _SEG_KIND.get(marker["kind"])
                            or ("number" if marker["kind"] == "m" or (ok and is_number(cite.get("value")))
                                else "value"),
                            "text": cite["rendered"], "ref": f"{marker['kind']}:{marker['body']}",
                            "cite": cite, "state": "deterministic" if ok else "none",
                        }
                        if not ok:
                            # 实体名字哪里都找不到：可疑实体，不是写法错了（出口只按缺口处理）；
                            # 表结构快照不全时只是核对不了（出口只标注）
                            seg["issue"] = "unknown_entity" if cite.get("unknown") else \
                                "unverified_entity" if cite.get("unverified") else "unresolved_ref"
                        if marker["strong"]:
                            seg["strong"] = True
                        push(seg, segments)
                    if cursor < e:
                        plain(cursor, e)
                unit: dict[str, Any] = {"kind": item.kind, "segments": segments, "see": see,
                                        "_anchor": anchor}
                if item.loc is not None:
                    unit["loc"] = dict(item.loc)
                if item.depth is not None:
                    unit["depth"] = item.depth
                units.append(unit)
                all_segments.extend(segments)
        out_blocks.append({"type": block.type, **block.meta, "units": units})

    markdown = "".join(parts)
    index = snaps.entities(catalog)
    if index.active:
        # 表名、字段名切成实体片段（代码块里的不算：那是 SQL 原文，不是在报告里提到一个名字）
        for block in out_blocks:
            if block["type"] != "code":
                for unit in block["units"]:
                    unit["segments"] = _link_entities(unit["segments"], catalog, index)
        all_segments = [s for block in out_blocks for unit in block["units"] for s in unit["segments"]]
    # 裸数字单独切成片段：前端要在那几个字底下画「无出处」
    refs = [tuple(s["span"]) for s in all_segments if s.get("ref")] + _header_masks(out_blocks, catalog)
    syntax = [tuple(s["span"]) for s in all_segments if s["kind"] == "structural"]
    bare = _bare_numbers(markdown, refs, allow_numbers, syntax)
    for block in out_blocks:
        for unit in block["units"]:
            unit["segments"] = _split_bare(unit["segments"], bare)

    seg_no = unit_no = 0
    for b_no, block in enumerate(out_blocks):
        block["id"] = f"b{b_no}"
        for unit in block["units"]:
            unit["id"] = f"u{unit_no}"
            unit_no += 1
            for seg in unit["segments"]:
                seg["id"] = f"s{seg_no}"
                seg_no += 1
            body = [s for s in unit["segments"] if s["kind"] != "structural"]
            anchor = unit.pop("_anchor")
            unit["span"] = [body[0]["span"][0], body[-1]["span"][1]] if body else [anchor, anchor]
            unit["kind"] = _classify(unit["kind"], unit["segments"], unit["see"])
            # 这句话挂的依据：只认写作者自己写的标记和 [[see:]]。自动链接的名字是系统加的标注，
            # 提一个字段名不等于给结论挂了出处（claims: require_citation 按这个判）
            unit["cites"] = list(dict.fromkeys(
                c["alias"] for c in [*(s["cite"] for s in unit["segments"] if s.get("cite") and not s.get("auto")),
                                     *unit["see"]]
                if c["status"] == "resolved"))
            # 字段顺序固定，文档内容寻址，同样的输入要得到同样的哈希
            ordered = {k: unit[k] for k in ("id", "kind", "span", "cites", "see", "segments", "loc", "depth")
                       if k in unit}
            unit.clear()
            unit.update(ordered)

    # 表结构可能有几百张表、几千个字段：文档里只留正文用到的实体条目（复核用的是重建的目录，不看这份）。
    # 自动链接的名字不进 cites，但点开它要查这份目录，所以按片段自己的 cite 收
    used = {c["alias"] for block in out_blocks for unit in block["units"]
            for c in [*(s["cite"] for s in unit["segments"] if s.get("cite")), *unit["see"]]
            if c.get("status") == "resolved" and c.get("alias")}
    kept = {a: e for a, e in catalog.items() if e.get("kind") not in ENTITY_KINDS or a in used}
    # entity_syntax：这份文档按哪一版实体语法组装，复核照它走（verify_doc）。在文档顶层，参与内容哈希
    doc: dict[str, Any] = {
        "schema": DOC_SCHEMA, "entity_syntax": snaps.syntax, "run_id": run_id, "node_id": node_id,
        "markdown": markdown, "source": source, "catalog": kept, "blocks": out_blocks,
    }
    checked = verify_doc(doc, catalog, allow_numbers=allow_numbers, cells_allowed=cells_allowed, loader=snaps)
    doc["stats"], doc["violations"] = checked["stats"], checked["violations"]
    return doc


def _header_masks(blocks: list[dict[str, Any]], catalog: dict[str, Any]) -> list[tuple[int, int]]:
    """表头里恰好是某次查询的列名的格：整表的表头是系统照快照列名写的，「销售额2025」这种列名
    里的数字不是结论数字，写作者也改不了它。只放过表头、只放过和列名一字不差的格。"""
    names = {_clean(str(c)) for e in catalog.values() if e.get("kind") == "query" for c in e.get("columns") or []}
    out: list[tuple[int, int]] = []
    for block in blocks if names else []:
        if not isinstance(block, dict) or block.get("type") != "table":
            continue
        for unit in block.get("units") or []:
            if (unit.get("loc") or {}).get("row") != -1:
                continue
            body = [s for s in unit.get("segments") or [] if s.get("kind") != "structural"]
            spans = [s.get("span") for s in body]
            if not body or not all(isinstance(p, list) and len(p) == 2 and all(isinstance(x, int) for x in p)
                                   for p in spans):
                continue        # 片段的偏移坏了：复核会报 segment_mismatch，这里不替它兜
            if all(s.get("kind") == "text" for s in body) \
                    and "".join(str(s.get("text") or "") for s in body).strip() in names:
                out.append((spans[0][0], spans[-1][1]))
    return out


def _split_bare(segments: list[dict[str, Any]], bare: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in segments:
        s0, e0 = seg["span"]
        hits = [t for t in bare if s0 <= t.start < e0] if seg["kind"] == "text" else []
        if not hits:
            out.append(seg)
            continue
        extra = {"strong": True} if seg.get("strong") else {}

        def piece(a: int, b: int, **fields: Any) -> None:
            out.append({"kind": "text", "text": seg["text"][a - s0:b - s0], "span": [a, b],
                        "state": seg["state"], **extra, **fields})

        cursor = s0
        for token in hits:
            end = min(token.end, e0)
            if cursor < token.start:
                piece(cursor, token.start)
            piece(token.start, end, kind="number", state="none", issue="uncited_number")
            cursor = end
        if cursor < e0:
            piece(cursor, e0)
    return out


# --------------------------------------------------------------------------
# 复核：不信任文档自己的说法，逐项重算
# --------------------------------------------------------------------------

_STRUCTURAL_REST = re.compile(r"^[\s|>#*+\-_:~=.]*$")


def _is_structural(text: str) -> bool:
    """结构片段里只能有 Markdown 语法：标题井号、列表符号和序号、引用号、表格竖线、围栏。

    这里只回答「是不是 Markdown 语法」，不回答「里面的数字要不要出处」——后者交给裸数字
    检查（结构片段只涂符号，数字照查，见 _SYNTAX_BLANK）。所以行首序号不限位数，和切块的
    _ORDERED、前端 Markdown.tsx 的 ORDERED 一致：「100. 」在前端就是 <ol start=100>，把它
    判成夹带内容会变成完整性问题（出口记 gap），而它真正的问题是一个没有出处的数字。
    """
    rest = re.sub(r"`{3,}[A-Za-z0-9_]*", "", text)
    rest = re.sub(r"(?m)^[ \t]*\d+[.)](?=\s)", "", rest)
    return bool(_STRUCTURAL_REST.match(rest))


def iter_units(doc: dict[str, Any]):
    for block in doc.get("blocks") or []:
        for unit in block.get("units") or []:
            yield block, unit


def iter_segments(doc: dict[str, Any]):
    """按正文顺序给出全部片段（含结构片段）。"""
    for _, unit in iter_units(doc):
        yield from unit.get("segments") or []


def find_segment(doc: dict[str, Any], segment_id: str) -> tuple[dict, dict, dict] | None:
    """(block, unit, segment)，找不到返回 None。"""
    for block, unit in iter_units(doc):
        for seg in unit.get("segments") or []:
            if seg.get("id") == segment_id:
                return block, unit, seg
    return None


def _violation(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """一处问题。message 给人看；message 是 _Reason 时另记 for_model，交回写作者改写时用（describe_violations）。"""
    return {"code": code, "message": str(message), **_fix_of(message),
            **{k: v for k, v in extra.items() if v is not None}}


def _unresolved_message(label: str, ref: str, cite: dict[str, Any]) -> str:
    """「引用 [[m:gmv]] 无法解析：…」。cite 带 for_model 的，给模型的那句照旧写原来的说法。"""
    text = f"{label} [[{ref}]] 无法解析：{cite['reason']}"
    return _Reason(text, f"{label} [[{ref}]] 解析不了：{cite['for_model']}") if cite.get("for_model") else text


def verify_doc(
    doc: dict[str, Any],
    catalog: dict[str, Any],
    *,
    allow_numbers: list[Any] | None = None,
    cells_allowed: bool = True,
    loader: Loader | _Snapshots | None = None,
    entities: bool = True,
) -> dict[str, Any]:
    """逐项复核一份报告文档：{ok, violations, stats, uncited}。

    cells_allowed=False：单元格引用（含整表里的格）一律判解析不了，原因是 CELLS_REASON——
    受管级别的正式出具，契约没声明 cells 时出口复核这样调。快照经 loader 重新取、复验哈希。
    entities=False：报告节点写了 entities: off，和组装时一样整层不管表名、字段名。

    报告节点自查用它（违规就让写作者重写），出口契约复核也用它（判档）——后者传的
    是自己从状态里重建的目录，不用文档里存的那份。复核什么：

    - 片段按顺序拼起来正好是 markdown（没有字落在片段外面）
    - 结构片段里只有 Markdown 语法
    - 每个带引用的片段：按给定目录重新解析、重新渲染，和片段上的字逐字相等，eid 一致
    - [[see:]] 里的每个依据都能解析
    - 引用涂白、结构片段只涂符号之后，正文里剩下的数字全部算裸数字（结构片段里的
      「100. 」「```45678」也在内）
    - 目录里有表和字段时（这次运行冻结了表结构）：反引号里像标识符、却哪里都找不到的名字
      全部算可疑实体（unknown_entity）——不管文档有没有把它切成片段，粗体、链接里的也算；
      表结构快照不全时，这样的名字只算核对不了（unverified_entity）
    - 引文重新取检索快照、重新逐字比对
    - 片段标的状态和核对结果一致（界面按状态画线，不能标着「有出处」其实没有）

    表名、字段名按文档记下的实体语法版本解析（entity_syntax，没有这个字段的老文档按 1 版）：老文档按
    它生成时的规则复核，不能因为今天认得中文名了，就把当年「格式无法识别」的片段判成被改过。loader 是
    _Snapshots 时也以文档为准。记了认不出的版本的，记 bad_schema，按当前版查。

    另外返回 uncited：没挂依据的结论句 [{unit, span, text}]（按这次核对的引用算，不信文档里的 cites；
    自动链接的名字不算依据）。

    stats 里 entities、quotes、unknown_entities、unverified_entities 四个键只在目录里有表和字段、或者
    有知识库检索时才有（它们才可能不是 0）：升级前的运行统计的键和以前一模一样。
    """
    entity_syntax = doc_entity_syntax(doc)
    snaps = _snapshots(loader, entities=entities, syntax=entity_syntax or ENTITY_SYNTAX)
    index = snaps.entities(catalog)
    violations: list[dict[str, Any]] = []
    uncited: list[dict[str, Any]] = []
    layered = index.active or any(isinstance(e, dict) and e.get("kind") == "retrieval" for e in catalog.values())
    stats = {"units": 0, "segments": 0, "claims": 0, "connective": 0, "headings": 0, "numbers": 0,
             "numbers_cited": 0, "values": 0, **({"entities": 0, "quotes": 0} if layered else {}),
             "uncited_numbers": 0, "unresolved": 0,
             **({"unknown_entities": 0, "unverified_entities": 0} if layered else {}),
             "see": 0, "uncited_claims": 0, "violations": 0}
    if not isinstance(doc, dict):
        violations.append(_violation("bad_schema", "报告文档格式有误"))
        return {"ok": False, "violations": violations, "stats": stats, "uncited": uncited}
    if doc.get("schema") != DOC_SCHEMA:
        violations.append(_violation("bad_schema", f"报告文档格式「{doc.get('schema')}」无法识别"))
    if entity_syntax is None:
        violations.append(_violation("bad_schema",
                                     f"报告文档记录的表名、字段名规则版本「{doc.get('entity_syntax')}」无法识别"))

    markdown = str(doc.get("markdown") or "")
    masks: list[tuple[int, int]] = []                  # 引用渲染出来的字
    syntax: list[tuple[int, int]] = []                 # 结构片段：只涂符号，数字照查
    owners: list[tuple[int, int, str, str]] = []        # (start, end, segment id, unit id)
    pos, tiled = 0, True
    for block, unit in iter_units(doc):
        stats["units"] += 1
        kind = unit.get("kind")
        if kind in ("claim", "connective"):
            stats["claims" if kind == "claim" else "connective"] += 1
        elif kind == "heading":
            stats["headings"] += 1
        cited: list[str] = []
        for seg in unit.get("segments") or []:
            stats["segments"] += 1
            text, span = str(seg.get("text") or ""), seg.get("span")
            intact = (isinstance(span, list) and len(span) == 2 and all(isinstance(x, int) for x in span)
                      and markdown[span[0]:span[1]] == text and span[1] - span[0] == len(text))
            if tiled and (not intact or span[0] != pos):
                tiled = False
                violations.append(_violation("segment_mismatch",
                                             f"报告的第 {stats['segments']} 个片段与正文不一致，文档可能被修改过",
                                             segment=seg.get("id"), unit=unit.get("id")))
            if intact:
                pos = span[1]
                owners.append((span[0], span[1], seg.get("id"), unit.get("id")))
            where = {"span": list(span) if intact else None, "text": text, "segment": seg.get("id"),
                     "unit": unit.get("id")}
            state, expected = seg.get("state"), None
            found = len(violations)
            if seg.get("kind") == "structural":
                expected = "neutral"
                if intact and _is_structural(text):
                    syntax.append((span[0], span[1]))
                else:
                    violations.append(_violation("structural_text",
                                                 f"排版片段中混入了正文内容「{text[:30]}」", **where))
            elif seg.get("ref"):
                ref = str(seg["ref"])
                cite = _phase_two_cite(seg, ref, text) \
                    or resolve_ref(ref, catalog, cells_allowed=cells_allowed, loader=snaps)
                ok = cite["status"] == "resolved"
                # 反引号里的实体：片段的字带着两个反引号
                shown = f"`{cite['rendered']}`" if seg.get("code") and seg.get("kind") == "entity" else cite["rendered"]
                matches = shown == text
                if intact:
                    masks.append((span[0], span[1]))
                if not ok and cite.get("unknown"):
                    violations.append(_violation("unknown_entity", _unknown_message(cite.get("ref") or ref),
                                                 ref=ref, **where))
                elif not ok and cite.get("unverified"):
                    violations.append(_violation("unverified_entity", _unverified_message(cite.get("ref") or ref),
                                                 ref=ref, **where))
                elif not ok:
                    violations.append(_violation("unresolved_ref", _unresolved_message("引用", ref, cite),
                                                 ref=ref, **where))
                elif not matches:
                    violations.append(_violation(
                        "render_mismatch", f"片段「{text}」与引用 [[{ref}]] 重新渲染的结果「{shown}」"
                        "不一致", ref=ref, **where))
                elif (seg.get("cite") or {}).get("eid") not in (None, cite["eid"]):
                    violations.append(_violation("eid_mismatch", f"引用 [[{ref}]] 记录的证据标识与本次运行的证据"
                                                 "不一致，文档可能被修改过", ref=ref, **where))
                good = ok and matches
                expected = "deterministic" if good else "none"
                if seg.get("kind") == "number":
                    stats["numbers"] += 1
                    stats["numbers_cited"] += int(good)
                elif good:
                    key = {"entity": "entities", "quote": "quotes"}.get(seg.get("kind"), "values")
                    stats[key if key in stats else "values"] += 1
                if ok and not seg.get("auto"):
                    # 自动链接的名字是标注，不是写作者给这句话挂的依据
                    cited.append(cite["alias"])
            elif seg.get("kind") == "number":
                expected = "none"
            elif seg.get("kind") == "entity":
                # 可疑实体（没有引用）：反引号里那个名字这次核对下来确实哪里都没有，才该标 none
                code = _CODE_NAME.match(text)
                expected = "none" if code and index.active and not index.known(code.group(1)) else "neutral"
            elif state == "deterministic":
                expected = "neutral"
            if seg.get("kind") in ("text", "entity") and not seg.get("ref") and index.active \
                    and block.get("type") != "code":
                for m in _CODE_SPAN.finditer(text):
                    name = m.group(1)
                    if _checked_name(name) and not index.known(name):
                        at = [span[0] + m.start(1), span[0] + m.end(1)] if intact else None
                        unsure = index.unsure(name)
                        violations.append(_violation("unverified_entity" if unsure else "unknown_entity",
                                                     (_unverified_message if unsure else _unknown_message)(name),
                                                     span=at, text=name, segment=seg.get("id"), unit=unit.get("id"),
                                                     context=_context(markdown, *at) if at else None))
            # 已经为这一段报过别的问题，状态不对是它的推论，不再重复
            if expected is not None and state != expected and len(violations) == found:
                violations.append(_violation("state_mismatch",
                                             f"片段「{text[:30]}」记录的核对状态与重新核对的结果不一致，文档可能被修改过",
                                             **where))
        for support in unit.get("see") or []:
            stats["see"] += 1
            again = resolve_support(str(support.get("ref") or ""), catalog, loader=snaps)
            if again["status"] == "resolved":
                cited.append(again["alias"])
            elif again.get("unknown") or again.get("unverified"):
                unsure = bool(again.get("unverified"))
                violations.append(_violation("unverified_entity" if unsure else "unknown_entity",
                                             (_unverified_message if unsure else _unknown_message)(
                                                 again["ref"].partition(":")[2]),
                                             ref=again["ref"], unit=unit.get("id"),
                                             span=list(unit.get("span") or []) or None))
            else:
                violations.append(_violation("unresolved_ref",
                                             _unresolved_message("依据", f"see:{again['ref']}", again),
                                             ref=again["ref"], unit=unit.get("id"),
                                             span=list(unit.get("span") or []) or None))
        if kind == "claim" and not cited:
            stats["uncited_claims"] += 1
            where = unit.get("span")
            ok_span = isinstance(where, list) and len(where) == 2 and all(isinstance(x, int) for x in where)
            uncited.append({"unit": unit.get("id"), "span": list(where) if ok_span else None,
                            "text": markdown[where[0]:where[1]] if ok_span else ""})
    if tiled and pos != len(markdown):
        violations.append(_violation("segment_mismatch", "正文末尾有一段不属于任何片段，文档可能被修改过"))

    masks += _header_masks(list(doc.get("blocks") or []), catalog)
    for token in _bare_numbers(markdown, masks, allow_numbers, syntax):
        owner = next(((sid, uid) for s, e, sid, uid in owners if s <= token.start < e), (None, None))
        violations.append(_violation(
            "uncited_number", _Reason(f"数字「{token.raw}」没有出处：报告中直接写出了数字，系统无法核对",
                                      f"数字「{token.raw}」没有出处：要写成引用标记（比如 [[m:指标id]]），不能直接写数字"),
            span=[token.start, token.end], text=token.raw, context=_context(markdown, token.start, token.end),
            segment=owner[0], unit=owner[1]))

    order = {"bad_schema": 0, "segment_mismatch": 1}
    violations.sort(key=lambda v: (order.get(v["code"], 2), (v.get("span") or [-1])[0]))
    stats["uncited_numbers"] = sum(1 for v in violations if v["code"] == "uncited_number")
    stats["numbers"] += stats["uncited_numbers"]
    stats["unresolved"] = sum(1 for v in violations if v["code"] == "unresolved_ref")
    if layered:
        stats["unknown_entities"] = sum(1 for v in violations if v["code"] == "unknown_entity")
        stats["unverified_entities"] = sum(1 for v in violations if v["code"] == "unverified_entity")
    stats["violations"] = len(violations)
    return {"ok": not violations, "violations": violations, "stats": stats, "uncited": uncited}


def doc_entity_syntax(doc: Any) -> int | None:
    """文档按哪一版实体语法组装：没有 entity_syntax 字段的是老文档（1 版），记了认不出的值返回 None。"""
    raw = doc.get("entity_syntax", 1) if isinstance(doc, dict) else 1
    return raw if type(raw) is int and raw in ENTITY_SYNTAXES else None


def _phase_two_cite(seg: dict[str, Any], ref: str, text: str) -> dict[str, Any] | None:
    """升级前（二期）组装的文档里的 t / c / q：那时一律判为解析不了，正文是占位，原因是 LEGACY_LATER_REASON。

    这样的文档升级后才被复核（运行停在报告和出口之间）时，按当时的规矩查：正文确实是占位、确实
    没有出处，记解析不了——不能因为今天解析得了，就把它判成「渲染对不上」（完整性问题，出口记缺口）。
    只认「原因是 LEGACY_LATER_REASON（或现在的 LATER_REASON）并且正文恰好是占位」的片段：自称二期却显示了
    别的字的，照现在的规矩查。
    """
    kind, _, body = ref.partition(":")
    stored = seg.get("cite")
    said = stored.get("reason") if isinstance(stored, dict) else None
    if kind not in ("t", "c", "q") or said not in (LATER_REASON, LEGACY_LATER_REASON) \
            or text != placeholder(kind, body):
        return None
    alias = stored.get("alias") if isinstance(stored.get("alias"), str) else ref
    reason = PHASE_TWO_REASON if said == LEGACY_LATER_REASON else LATER_REASON
    return {"ref": body, "alias": alias, "kind": stored.get("kind") or kind, "role": _ROLE.get(kind, "value"),
            "status": "unresolved", "reason": reason, "rendered": text}


def _unknown_message(name: str) -> str:
    return _Reason(f"「{name}」{UNKNOWN_ENTITY_REASON}",
                   f"「{name}」{_UNKNOWN_ENTITY_FOR_MODEL}：只写本次运行的表结构、查询里真实存在的表名和字段名")


def _unverified_message(name: str) -> str:
    return f"「{name}」无法核实：{UNVERIFIED_ENTITY_REASON}"


def uncited_claims(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """文档里没挂依据的结论句：[{unit, span, text}]。按文档自己记的 cites 算，给界面用；
    出口判档要用 verify_doc 返回的 uncited（按重新核对的引用算）。"""
    markdown = str(doc.get("markdown") or "")
    out = []
    for _, unit in iter_units(doc):
        span = unit.get("span")
        if unit.get("kind") == "claim" and not unit.get("cites") and isinstance(span, list) and len(span) == 2:
            out.append({"unit": unit.get("id"), "span": list(span), "text": markdown[span[0]:span[1]]})
    return out


# --------------------------------------------------------------------------
# 给报告节点用的文字：证据目录、写作规则、违规清单
# --------------------------------------------------------------------------

MARKER_RULES = """写作规则（系统会逐字核对）：
1. 任何数字都只能用引用标记写：[[m:指标id]]。要换算显示就写 [[m:指标id|万]]，也可以用 |亿、|pct（比率显示成百分数）、|int（取整）、|.1（一位小数）。不要自己写数字，不要计算，不要改写数值——标记会被换成口径卡里的真实数值。
2. 运行输入用 [[i:字段名]]。
3. 每句陈述数据的结论，句末用 [[see:m:指标id,…]] 标出依据；过渡、连接性的话不用标。
4. 日期、ISO 周（2026-W37）、「前 3 名」「第 2 季度」这类序号可以直接写。
5. 目录里没有的指标不要编造引用；标着「没有值」的指标不要写进报告。"""

#: 引用查询结果的写法。只跟着目录里的查询出现（catalog_prompt）：写作规则本身保持一期那份，升级前
#: 发起的运行跑到报告节点时提示一字不差；只有口径卡的目录也不该教写作者去引用不存在的 Q1
CELL_RULES = (
    "查询结果（里面的数和指标一样只能用引用标记写，系统照快照换成真实数值、逐字核对）：\n"
    "写法：一格写 [[v:Q1.r0.列名]]（行号从 0 数），换算同指标，比如 [[v:Q1.r0.列名|万]]；"
    "要列出多行多列，单独一行写 [[table:Q1 cols=列a,列b rows=0-4]]，系统照快照生成表格"
    f"（不写 rows 就是前 {TABLE_DEFAULT_ROWS} 行，最多 {TABLE_MAX_ROWS} 行）；"
    "结论的依据可以写查询编号，比如 [[see:Q1]]。下面没有的查询、行、列不要编造。"
)

#: 表名、字段名的写法。只在这次运行冻结了表结构（目录里有表和字段）时出现
#: 只用于组装新文档的写作目录（catalog_prompt → 报告节点），新文档一律按当前的实体语法（ENTITY_SYNTAX）
#: 组装和复核，所以这里写的就是当前版的规矩；老文档复核不看提示词。反引号里的名字只核对 ASCII 标识符
#: （_checked_name，中文正文、地名放进反引号不能被判成编造的名字），所以中文名必须用 [[t:]] / [[c:]]：
#: 照「放进反引号」写 `某表`，编造的中文表名就绕开了核对
ENTITY_RULES = (
    "表和字段（系统会核对）：提到表名、字段名时写 [[t:表名]]、[[c:表名.字段名]]；英文的名字也可以放进反引号"
    "（`orders`），中文的表名、字段名一律用 [[t:]] / [[c:]]——反引号里的中文名系统不核对；"
    "只写下面列出的、或者这次查过的数据源里真实存在的名字——本次运行的表结构快照、查询用到的表、"
    "查询结果列里都没有的名字，会被标成「可能是编造的名字」。\n"
    "说明一个数是怎么算出来的（按哪个字段去重、怎么汇总、筛了什么条件）时，句末挂 [[see:Q1]] 指向算出它的那次"
    "查询（下面每张表后面写了哪几次查询用到它）：只挂 [[t:]]、[[c:]] 指不出是哪一次查询算出的这个数。"
)
#: 引原话的写法。只跟着目录里的知识库检索出现
QUOTE_RULES = (
    "知识库检索：引用原话写 [[q:K1|原话]]，原话必须和下面片段里的字一字不差（系统逐字核对，可以只抄其中"
    "连续的一段，至少 4 个字）；只是拿检索当依据时写 [[see:K1]]。下面没有的检索、片段里没有的话不要编造。"
)
#: 报告节点 claims 为 require_citation 时，拼在写作规则后面
CLAIMS_RULE = (
    "这次要求每句结论都挂依据：陈述数据、比较、变化、原因的句子，句末必须写 [[see:…]]（指标、查询编号、"
    "知识库编号都可以）。没挂依据的结论句会被记为缺口，报告因此降档出具；过渡、连接性的话不用挂。"
)

#: 写作目录里每次查询最多列出几行、几列。更多的行照样能按行号引用
_PROMPT_ROWS = 20
_PROMPT_COLS = 12
#: 表和字段：最多列几张查询用到的表、几张别的表、几个结果列；每条检索最多几段、每段多长
_PROMPT_TABLES = 20
_PROMPT_OTHER_TABLES = 40
_PROMPT_RESULT_COLS = 40
_PROMPT_HITS = 8
_PROMPT_HIT_CHARS = 300


def _query_prompt(entry: dict[str, Any], snaps: _Snapshots, cells_allowed: bool) -> list[str]:
    """一次查询在写作目录里的样子：编号、来源、列，以及能引用的行（行号 + 渲染后的值）。

    值按 [[v:]] 的渲染规则写出来：写作者在这里看到的，就是引用之后报告里显示的字。
    """
    alias = entry["alias"]
    cols = entry.get("columns") or []
    lines = [f"- {alias}：{entry.get('label')}" + (f"，列 {', '.join(map(str, cols[:_PROMPT_COLS]))}" if cols else "")]
    snapshot, why = snaps.get(alias, entry.get("artifact"))
    if why:
        return [*lines, f"  （{why}，不要引用）"]
    if not cells_allowed:
        return lines
    columns = [str(c) for c in snapshot.get("columns") or []][:_PROMPT_COLS]
    rows = snapshot["rows"]
    example = None
    for r, record in enumerate(rows[:_PROMPT_ROWS]):
        cells = []
        for c in columns:
            try:
                raw, _ = locate_cell(snapshot, r, c)
                kind = column_kind(snapshot, c)
                shown, numeric = render_cell(raw, kind=kind), is_number(cell_value(raw, kind))
            except (CellError, RenderError):
                shown, numeric = MISSING, False
            cells.append(f"{c}={shown}")
            if example is None and r == 0 and numeric:
                example = c           # 示例挑第一行里第一个数，写作者最常引用的就是它
        lines.append(f"  r{r}：" + "，".join(cells))
    if not rows:
        lines.append("  （0 行，没有可以引用的格）")
    elif len(rows) > _PROMPT_ROWS:
        lines.append(f"  …共 {len(rows)} 行，这里只列出前 {_PROMPT_ROWS} 行；后面的行也能按行号引用")
    if rows and columns:
        lines.append(f"  例：[[v:{alias}.r0.{example or columns[0]}]]")
    return lines


def catalog_prompt(catalog: dict[str, Any], *, budget: int = 12000, cells_allowed: bool = True,
                   loader: Loader | None = None) -> str:
    """证据目录的文字版，放进写作者的 prompt。超过预算就截断，并照实说截断了。

    查询结果会列出能引用的行（要读快照）；cells_allowed=False 时只列编号，说明只能当依据。
    沙箱代码节点不列：它的产出不能引用。
    """
    snaps = _Snapshots(loader)
    lines = ["可引用的证据（写数字只能用下面的引用标记，系统会换成真实数值）："]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for entry in catalog.values():
        if entry.get("kind") == "metric":
            key = (entry.get("caliber") or "", entry.get("version") or "", entry.get("node_id") or "")
            groups.setdefault(key, []).append(entry)
    for (caliber, version, node_id), entries in groups.items():
        title = " @ ".join(v for v in (caliber, version) if v) or node_id
        lines.append(f"\n口径卡「{title}」：")
        for e in entries:
            if e.get("value") is None:
                lines.append(f"- [[{e['alias']}]] {e.get('name')}：这次没有值，不要引用")
            elif e.get("rendered") == MISSING:
                # 有值但按口径卡的格式显示不出来（太小、太大、不是有限的数）：引用了也是解析不了
                lines.append(f"- [[{e['alias']}]] {e.get('name')}：按口径卡的格式显示不出来，不要引用")
            else:
                lines.append(f"- [[{e['alias']}]] {e.get('name')} = {e.get('rendered')}")
    inputs = [e for e in catalog.values() if e.get("kind") == "input"]
    if inputs:
        lines.append("\n运行输入：")
        lines.extend(f"- [[{e['alias']}]] = {e.get('rendered')}" for e in inputs)
    queries = [e for e in catalog.values() if e.get("kind") == "query"]
    if queries:
        lines.append(f"\n{CELL_RULES}" if cells_allowed else
                     "\n查询结果（这次出具不允许直接引用单元格，只能写在 [[see:Q1]] 这样的依据里）：")
        for e in queries:
            lines.extend(_query_prompt(e, snaps, cells_allowed))
    if (index := snaps.entities(catalog)).active:
        lines.extend(_entity_prompt(catalog, partial=index.partial, snaps=snaps))
    retrievals = [e for e in catalog.values() if e.get("kind") == "retrieval"]
    if retrievals:
        lines.append(f"\n{QUOTE_RULES}")
        for e in retrievals:
            lines.extend(_retrieval_prompt(e, snaps))
    text = "\n".join(lines)
    if len(text) > budget:
        text = text[:budget].rsplit("\n", 1)[0] + "\n…（目录太长，后面的省略了）"
    return text


def unshaped_table(entry: dict[str, Any], fetch: Callable[[Any], Any]) -> bool:
    """目录里的这张表是不是按原样导入、未经规整的（冻结的表结构里它的 comment 是 tabular.UNSHAPED_NOTE）。

    看的是这次运行冻结的表结构快照（条目 sources 里 kind 为 schema 的那几份），不看数据源现在的样子。
    fetch 取工件内容，取不到给 None。写作目录和裁判摘录同一个口径。
    """
    name = entry.get("name") or (entry.get("locator") or {}).get("table")
    for origin in entry.get("sources") or []:
        if not (isinstance(origin, dict) and origin.get("kind") == "schema" and origin.get("artifact")):
            continue
        info = schema_table(fetch(origin["artifact"]), name)
        if info is not None:
            return info.get("comment") == UNSHAPED_NOTE
    return False


#: 写作目录里未规整的表名后面跟的那句。不含数字（H11）
_UNSHAPED_MARK = "（按原样导入、未经规整：同一列里混有不同口径的行，不能直接对列求和）"
#: 写作目录里「原表写明的合计」表（按配方导入另存的表内合计，表结构里 kind=reported_total）后面跟的那句（AU-5）
_REPORTED_TOTAL_MARK = "（原表写明的合计：不要彼此相加，也不要与明细相加）"


def reported_total_table(entry: dict[str, Any], fetch: Callable[[Any], Any]) -> bool:
    """目录里的这张表是不是原表写明的合计（冻结的表结构里 kind 为 reported_total）。口径同 unshaped_table。"""
    name = entry.get("name") or (entry.get("locator") or {}).get("table")
    for origin in entry.get("sources") or []:
        if not (isinstance(origin, dict) and origin.get("kind") == "schema" and origin.get("artifact")):
            continue
        info = schema_table(fetch(origin["artifact"]), name)
        if info is not None:
            return info.get("kind") == "reported_total"
    return False


def _entity_prompt(catalog: dict[str, Any], *, partial: bool = False, snaps: _Snapshots | None = None) -> list[str]:
    """表和字段在写作目录里的样子：查询用到的表连同字段，查询结果里的列，别的表只列名字。

    按原样导入、未经规整的表（上传时选了「按原样导入」）在名字后面写明不能直接对列求和：说明本来只在
    db_schema 查单表时看得到，写报告的模型多半没看过（R4、D16）。
    """
    def fetch(artifact: Any) -> Any:
        return snaps._fetch(artifact, "表结构快照")[0] if snaps is not None else None

    def mark(entry: dict[str, Any]) -> str:
        if snaps is None:
            return ""
        if unshaped_table(entry, fetch):
            return _UNSHAPED_MARK
        return _REPORTED_TOTAL_MARK if reported_total_table(entry, fetch) else ""

    tables = [e for e in catalog.values() if e.get("kind") == "table"]
    fields: dict[str, list[str]] = {}
    loose: list[str] = []
    for e in catalog.values():
        if e.get("kind") != "column":
            continue
        table = (e.get("locator") or {}).get("table")
        if table:
            fields.setdefault(table, []).append(str(e.get("name")))
        elif any(o.get("kind") == "result" for o in e.get("sources") or []):
            loose.append(str(e.get("name")))
    lines = [f"\n{ENTITY_RULES}"]
    used = [t for t in tables if t.get("queries")]
    for t in used[:_PROMPT_TABLES]:
        cols = fields.get(t["name"], [])
        more = f" 等 {len(cols)} 个" if len(cols) > _PROMPT_COLS else ""
        lines.append(f"- {t['name']}（{'、'.join(t['queries'])} 用到）{mark(t)}"
                     + (f"：{', '.join(cols[:_PROMPT_COLS])}{more}" if cols else ""))
    if loose:
        more = f" 等 {len(loose)} 个" if len(loose) > _PROMPT_RESULT_COLS else ""
        lines.append(f"- 查询结果里的列：{', '.join(loose[:_PROMPT_RESULT_COLS])}{more}")
    others = [f"{t['name']}{mark(t)}" for t in tables if not t.get("queries")]
    if others:
        more = f" 等 {len(others)} 张" if len(others) > _PROMPT_OTHER_TABLES else ""
        lines.append(f"- 其他表：{'、'.join(others[:_PROMPT_OTHER_TABLES])}{more}")
    if partial:
        lines.append("- 这个数据源的表太多，表结构快照只存了一部分：没列出的名字系统核对不了，只写你确定存在的")
    return lines


def _retrieval_prompt(entry: dict[str, Any], snaps: _Snapshots) -> list[str]:
    """一次检索在写作目录里的样子：编号、来源，以及能引原话的片段（压成一行，太长的截断）。"""
    alias = entry["alias"]
    lines = [f"- {alias}：{entry.get('label')}"]
    hits, why = snaps.hits(alias, entry.get("artifact"))
    if why:
        return [*lines, f"  （{why}，只能写 [[see:{alias}]]，不要引原话）"]
    for i, hit in enumerate(hits[:_PROMPT_HITS]):
        if not isinstance(hit, dict) or not isinstance(hit.get("content"), str):
            continue
        body = _clean(hit["content"])
        body = body if len(body) <= _PROMPT_HIT_CHARS else body[:_PROMPT_HIT_CHARS] + "…"
        title = f"《{_clean(str(hit['title']))}》" if hit.get("title") else ""
        lines.append(f"  片段 {i + 1}{title}：{body}")
    if len(hits) > _PROMPT_HITS:
        lines.append(f"  …共 {len(hits)} 段，这里只列出前 {_PROMPT_HITS} 段")
    return lines


def describe_violations(violations: list[dict[str, Any]], *, limit: int = 20) -> str:
    """违规清单的文字版，交回写作者（模型）改写用：有 for_model（给模型的改写指令）的用它，
    没有的用 message。给人看的报错直接用 message，不经过这里。"""
    lines = []
    for v in violations[:limit]:
        where = f"（…{v['context']}…）" if v.get("context") else ""
        lines.append(f"- {v.get('for_model') or v['message']}{where}")
    if len(violations) > limit:
        lines.append(f"- …另有 {len(violations) - limit} 处")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 旧运行：按数值猜「可能的来源」
#
# 没有契约、没有报告文档的旧答案，界面上仍想让人看看每个数大概是从哪来的。这件事一期就
# 核对过：按数值匹配巧合极多（168 个数里 131 个能在快照里找到相同的值，大量是 0、1、2），
# 所以它只做降级展示：state 记 candidate，界面默认折叠，写明「猜测的来源，不能当证据」。
# --------------------------------------------------------------------------

GUESS_SCHEMA = "agentlab.guess/1"
GUESS_NOTE = "猜测的来源，不能当证据：按数值在本次运行已封存的查询结果和口径卡中查找相同的值，数值相同的巧合很多"
#: 每个数字最多给几个候选
MAX_CANDIDATES = 3


#: 同一个值的格往往成千上万（0、1）：一个值最多看这么多个，够挑出候选、又不必逐个过一遍
_GUESS_GROUP_SCAN = 16
_MASK_SPLIT = re.compile(r"[,，、;；\n]+")


def _mask_names(raw: Any) -> set[str]:
    """遮罩列名 → 小写的集合。列表和「a, b」这样的文字都认（和 data.engine.masked_columns 一样）；
    别的形状当作没遮——但字符串一律当列名处理，宁可多遮，不能因为写法不同露出原值。"""
    items = _MASK_SPLIT.split(raw) if isinstance(raw, str) else raw if isinstance(raw, (list, tuple, set)) else []
    return {str(c).strip().lower() for c in items if isinstance(c, (str, int)) and str(c).strip()}


def _guess_pool(sealed: list[Any], masked: dict[str, Any] | None
                ) -> tuple[list[tuple[float, int, tuple[Any, ...]]], list[dict[str, Any]]]:
    """已封存的条目摊成 (数值, 顺序号, 出处) 和每个条目的上下文。

    只取值、不渲染：一份几千行的快照有几万格，渲染只留给最后挑中的候选（每个数字至多 limit 个）。
    顺序号按传入顺序、行、列递增，距离相同时靠它排。遮罩的列（快照记下的 mask_columns，加上调用方
    按 artifact 交来的数据源现有遮罩）整列不进池子：候选的值、渲染、差值都会把原值带出去。
    """
    pool: list[tuple[float, int, tuple[Any, ...]]] = []
    contexts: list[dict[str, Any]] = []

    def add(value: Any, where: tuple[Any, ...]) -> None:
        if not is_number(value):
            return
        try:
            number = float(value)
        except OverflowError:           # 几百位的整数：换不成浮点数，也不会有人按它写报告
            return
        if math.isfinite(number):
            pool.append((number, len(pool), where))

    for entry in sealed or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("artifact"), str) or not entry["artifact"]:
            continue
        artifact, content = entry["artifact"], entry.get("content")
        if not isinstance(content, dict):
            continue
        at = len(contexts)
        context = {"entry": entry, "artifact": artifact, "content": content,
                   "extra": {k: entry[k] for k in ("node_id", "tool") if entry.get(k)}}
        if entry.get("kind") == "metric_set" and isinstance(content.get("metrics"), list):
            contexts.append(context)
            for metric in content["metrics"]:
                if isinstance(metric, dict) and metric.get("id"):
                    add(metric.get("value"), (at, metric))
        elif entry.get("kind") == "query" and isinstance(content.get("rows"), list):
            contexts.append(context)
            hidden = _mask_names(content.get("mask_columns")) | _mask_names((masked or {}).get(artifact))
            names = [str(c) for c in content.get("columns") or []]
            types = content.get("column_types") if isinstance(content.get("column_types"), dict) else {}
            # 同名的列按第一个算（和 locate_cell 一样），只算一次
            columns = [(name, names.index(name), types.get(name) if isinstance(types.get(name), str) else None)
                       for name in dict.fromkeys(names) if name.lower() not in hidden]
            for r, record in enumerate(content["rows"]):
                for name, index, kind in columns:
                    if isinstance(record, dict):
                        if name not in record:
                            continue
                        raw = record[name]
                    elif isinstance(record, (list, tuple)) and index < len(record):
                        raw = record[index]
                    else:
                        continue
                    add(cell_value(raw, kind), (at, r, name, kind, raw))
    return pool, contexts


def _guess_candidate(where: tuple[Any, ...], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """池子里的一项 → 候选。单元格渲染不出来（非零却显示成 0）的返回 None，名额让给下一个。"""
    context = contexts[where[0]]
    artifact, content, extra = context["artifact"], context["content"], context["extra"]
    if len(where) == 2:
        metric = where[1]
        mid = str(metric["id"])
        try:
            rendered = render_metric(metric)
        except RenderError:
            rendered = MISSING
        return {"kind": "metric", "ref": f"m:{mid}", "artifact": artifact, "locator": {"metric": mid},
                "eid": make_eid("metric", artifact, {"metric": mid}), "value": metric.get("value"),
                "rendered": rendered, "name": metric.get("name") or mid,
                **{k: v for k, v in (("caliber", content.get("caliber")),
                                     ("version", content.get("caliber_version"))) if v},
                **{k: v for k, v in extra.items() if k == "node_id"}}
    _, r, name, kind, raw = where
    try:
        rendered = render_cell(raw, kind=kind)
    except RenderError:
        return None
    alias = context["entry"].get("alias")
    return {"kind": "cell", **({"ref": f"{alias}.r{r}.{name}", "alias": alias} if alias else {}),
            "artifact": artifact, "locator": {"row": r, "column": name}, "eid": cell_eid(artifact, r, name),
            "value": cell_value(raw, kind), "rendered": rendered, **extra}


def _guesses(token: Any, values: list[float], pool: list[tuple[float, int, tuple[Any, ...]]],
             contexts: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """一个数字的候选：书写精度以内相等的值（写成 8.7% 的也和比率 0.087 比），按距离、再按传入顺序。"""
    if limit <= 0:
        return []
    tolerance = _tolerance(token) + 1e-9
    best: dict[int, tuple[float, int]] = {}
    targets = [(token.value, 1.0)] + ([(token.value / 100, 100.0)] if token.is_percent else [])
    for target, scale in targets:
        k = bisect.bisect_left(values, target - tolerance / scale)
        hi = bisect.bisect_right(values, target + tolerance / scale)
        while k < hi:
            # 同一个值的一串（池子按值、顺序号排好了）：差值一样，只有前几个有机会入选
            group_end = bisect.bisect_right(values, values[k], k, hi)
            for j in range(k, min(group_end, k + _GUESS_GROUP_SCAN)):
                value, order, _ = pool[j]
                diff = abs(token.value - value * scale)
                if diff <= tolerance and (order not in best or diff < best[order][0]):
                    best[order] = (diff, j)
            k = group_end
    out: list[dict[str, Any]] = []
    for _, (diff, j) in sorted(best.items(), key=lambda item: (item[1][0], item[0])):
        candidate = _guess_candidate(pool[j][2], contexts)
        if candidate is not None:
            out.append({**candidate, "diff": round(diff, 10)})
            if len(out) >= limit:
                break
    return out


def guess_sources(text: str, sealed: list[Any], *, limit: int = MAX_CANDIDATES,
                  masked: dict[str, Any] | None = None) -> dict[str, Any]:
    """旧答案里的每个数字，去调用方交来的已封存条目里按数值找候选。只做展示，不是证据。

    sealed 的每一项：{kind: "query" | "metric_set", artifact, content, alias?, node_id?, tool?}
    - query：content 是查询快照（columns / rows / column_types / mask_columns），候选是单元格，ref 形如
      Q1.r0.gmv（给了 alias 才有）；文本列里的数字样子的字（编号 "00123"）不按数比
    - metric_set：content 是口径卡的指标集，候选是指标，ref 形如 m:gmv
    这个函数不读库：调用方只把封存范围内的事件引用得到、取回时复验过哈希的工件交进来。

    遮罩：快照自己记下的 mask_columns，加上 masked（{查询快照工件: 数据源现在的遮罩列}，调用方按
    masked_columns(source.options) 给）里的列，一律不当候选——候选的 value、rendered、diff 都会带出
    原值，写成 8,123 的数也会对上 8,123.45。列名不分大小写。

    数字的抽取和容差沿用出具校验（日期、ISO 周、行首序号不算数字；写 12.3 的容差是 0.05）。
    每个数字最多 limit 个候选，按距离从近到远，距离相同的按传入顺序（条目、行、列），同样的输入
    永远同样的输出。返回 {schema, mode: "legacy_text", note, markdown, segments, stats}：
    segments 铺满原文，数字片段有候选的 state 为 candidate（带 candidates），没有的为 none。
    """
    text = text or ""
    pool, contexts = _guess_pool(sealed, masked if isinstance(masked, dict) else None)
    pool.sort(key=lambda item: (item[0], item[1]))
    values = [item[0] for item in pool]
    segments: list[dict[str, Any]] = []
    stats = {"numbers": 0, "guessed": 0, "unguessed": 0, "candidates": 0}
    cursor = 0

    def push(seg: dict[str, Any]) -> None:
        segments.append({"id": f"s{len(segments)}", **seg})

    for token in sorted(extract_numbers(text), key=lambda t: t.start):
        if token.start < cursor:
            continue
        if cursor < token.start:
            push({"kind": "text", "text": text[cursor:token.start], "span": [cursor, token.start], "state": "neutral"})
        found = _guesses(token, values, pool, contexts, limit)
        seg = {"kind": "number", "text": text[token.start:token.end], "span": [token.start, token.end],
               "state": "candidate" if found else "none"}
        if found:
            seg["candidates"] = found
        push(seg)
        stats["numbers"] += 1
        stats["guessed" if found else "unguessed"] += 1
        stats["candidates"] += len(found)
        cursor = token.end
    if cursor < len(text):
        push({"kind": "text", "text": text[cursor:], "span": [cursor, len(text)], "state": "neutral"})
    return {"schema": GUESS_SCHEMA, "mode": "legacy_text", "note": GUESS_NOTE, "markdown": text,
            "segments": segments, "stats": stats}
