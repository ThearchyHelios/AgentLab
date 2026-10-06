"""可验证报告的构建：把一次已封存运行的报告、证据和运行记录打成一个自包含的 HTML 文件。

拿到文件的人不连服务、不装任何东西，在浏览器里就能核对（verifier.js）：

- 事件：封存范围内（seq ≤ manifest_seq）的每一条都带 {seq, type, node_id, data_sha256}，data_sha256 是
  sha256(canonical_json(data))，和 runner 封存时算 manifest_hash 用的是同一个算法，浏览器按同样的拼法重算
  清单哈希、和封存哈希比。白名单里的事件另带服务端算好的规范字符串 canonical（逐字等于 canonical_json(data)），
  浏览器先核它的哈希，再从里面读出报告文档、口径卡、查询快照的 id。其余事件只给哈希：工具返回、节点产出的
  预览里可能有遮罩列的原值和提示词。
- 工件：每份报告文档（各报告节点最后一次自查记下的 doc_artifact）、它引用到的口径卡和查询快照，按原字节
  base64 嵌入，浏览器重算 sha256 和 id 比。数据源设了遮罩的查询快照，能证明这条查询没碰遮罩列的（_untouched）照常
  嵌入，别的不嵌内容、只给 id。口径卡上记着每个输入的值和代入式，有输入取自遮罩列（cell() 的格、Agent 字段、按位置
  取的 vars.res.rows[0][2]，SQL 里改了名字的遮罩列也算，SQL 认不出结果里哪一列取自哪一列的整份快照的列都算）的整张卡
  同样只给 id，认法和证据面板、推演同一个（recompute.input_origin）；服务端文件被改过的卡照样按卡上记的输入认，解析
  不出一张卡的整张只给 id。只给 id 的工件，服务端的文件和 id 对不上的另标 mismatch、解析不出来的另标 unparsable，
  核对页照实说（对不上的判不通过）。没被报告引用的工件不嵌。
- 封存之后追加的事件（按需裁判、审批质询这类批注）单列：类型、序号、内容摘要，不进清单。

它只证明「内部一致」：数字、文档、运行记录彼此对得上，而且没被改过。真伪要拿页面上的封存哈希和系统里这次
运行的封存核对（GET /api/runs/{id}/verify）对照，页面上醒目地写着这一点。

主体是纯函数：不碰数据库，工件经 read 取原字节，数据源现在设的遮罩由调用方查好传进来（source_masks）。
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.core.artifact_store import canonical_json, content_hash
from app.core.events import EventType
from app.engine.evidence import iter_units, query_entry_fields
from app.engine.merge_query import MERGE_SOURCE
from app.engine.recompute import input_origin
from app.engine.schema import NodeType
from app.engine.toolcalls import QUERY_PREFIX

logger = logging.getLogger(__name__)

SCHEMA = "agentlab.bundle/1"

#: 不论哪个节点都带规范字符串的事件：运行开始（实际生效的运行参数）、报告自查（报告文档的 id）、
#: 口径卡升版处置、出具判定（档位）
_SHOWN_EVENTS = frozenset({EventType.RUN_STARTED, EventType.REPORT_CHECKED, EventType.CALIBER_UPGRADE,
                           EventType.ISSUANCE})
#: node.finished 带规范字符串的节点：报告（预览就是嵌进来的那份文档的正文）、口径卡（指标集的 id 记在台账里）
_SHOWN_NODES = frozenset({NodeType.REPORT, NodeType.METRICS})
#: 目录条目的种类 → 嵌进来的工件种类。运行输入、检索、表和字段的条目不嵌：前者没有工件，后几样不在核对范围里
_CITED_KINDS = {"metric": "metric_set", "query": "query"}

#: 没挂在工作流上的运行（画布、问数据当场搭的）：老数据叫「临时图」，新的叫「未保存的工作流」，也可能是空的。
#: 和前端 lib/terms.ts 的 runName 同一个规则
UNSAVED_NAME = "未保存的工作流"
_UNSAVED_NAMES = frozenset({"", "临时图", UNSAVED_NAME})
FILE_SUFFIX = "-可验证报告.html"
#: 文件名里不能出现的字（Windows 最严，按它来）和控制字符
_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_NAME_MAX = 80

Reader = Callable[[str], "bytes | None"]


# --------------------------------------------------------------------------
# 工件：原字节、解析、遮罩
# --------------------------------------------------------------------------


class _Store:
    """这一次构建里读过的工件：原字节、解析出来的内容、查询快照带不带遮罩。每份只读一次盘。events 是封存范围内的
    事件（认这次运行里查到过哪些遮罩值，seen）。"""

    def __init__(self, read: Reader, source_masks: Mapping[str, Any],
                 events: Sequence[tuple[int, str, str | None, Any]] = ()) -> None:
        self._read = read
        self._source_masks = source_masks
        self._events = events
        self._raw: dict[str, bytes | None] = {}
        self._json: dict[str, Any] = {}
        self._masked: dict[str, bool] = {}
        self._hidden: dict[str, set[str]] = {}
        self._seen: Any = None

    def raw(self, artifact: str) -> bytes | None:
        if artifact not in self._raw:
            try:
                data = self._read(artifact)
            except (OSError, ValueError):
                data = None
            self._raw[artifact] = data if isinstance(data, (bytes, bytearray)) else None
        return self._raw[artifact]

    def content(self, artifact: str) -> Any:
        """解析出来的内容，读不出来、不是 JSON 的是 None。不看哈希：被改过的快照也要知道它记的遮罩。"""
        if artifact not in self._json:
            raw = self.raw(artifact)
            try:
                self._json[artifact] = json.loads(raw) if raw is not None else None
            except ValueError:
                self._json[artifact] = None
        return self._json[artifact]

    def intact(self, artifact: str) -> bool:
        raw = self.raw(artifact)
        return raw is not None and content_hash(bytes(raw)) == artifact

    def masked(self, artifact: str) -> bool:
        """这份查询快照算不算带遮罩（整份不随文件附带、只给 id）：查询当时记下了遮罩列（mask_columns），或者它的数据源
        现在设了遮罩（合并查询看它合并的那几个数据源），而且证明不了这条查询没碰遮罩列（_untouched）。比证据面板粗：
        面板只遮那几列，这里要么原字节整份附带、要么只给 id——拿掉几格哈希就对不上了；文件会离开系统，遮罩只减少暴露，
        宁可多挡。读不出来的快照不知道带没带遮罩，按带遮罩算；服务端的文件和 id 对不上的，里面的 SQL、列名都不可信，
        也按带遮罩算。"""
        if artifact not in self._masked:
            self._masked[artifact] = _snapshot_masked(self.content(artifact), self._source_masks,
                                                      intact=self.intact(artifact), seen=self.seen)
        return self._masked[artifact]

    def hidden(self, artifact: str) -> set[str]:
        """一份读得出来的查询快照里值取自遮罩列的列（小写）：查询当时记下的、数据源现在设的遮罩列，加上 SQL 里给它们
        改了名字的；SQL 认不出结果里哪一列取自哪一列的，整份的列（_derived_columns）。"""
        if artifact not in self._hidden:
            content = self.content(artifact)
            base = _snapshot_hidden(content, self._source_masks) if isinstance(content, dict) else set()
            self._hidden[artifact] = _derived_columns(content, base) if base else set()
        return self._hidden[artifact]

    def seen(self) -> Any:
        """这次运行里查到过的遮罩值（inquiry._Secrets，只用按值认得出的写法：够长的数、两个字以上的文字）：封存范围内
        每份查询快照里值取自遮罩列的那几列（hidden）的格子。模型拿在遮罩查询里看到的号码去查别的表时，SQL 里没写遮罩列，
        字面量却就是号码（_untouched）。认不出哪一列取自哪一列的快照（整行摊开的画像查询）整份的格子都算：同一次运行里
        别的查询的字面量碰上其中哪个值，也只给哈希（宁可多挡）。这次运行里没查到过的（提问人写进问题的、别的运行里看到的）
        认不出。只收一次。"""
        if self._seen is None:
            from app.engine.inquiry import _Secrets   # inquiry 导入了 engine.nodes 里的模块：用到时再导入

            seen = _Secrets()
            for artifact in _run_queries(self._events, self):
                content = self.content(artifact)
                if isinstance(content, dict) and (hidden := self.hidden(artifact)):
                    seen.add(content, hidden)
            self._seen = seen
        return self._seen

    def card_masked(self, artifact: str, events: Sequence[tuple[int, str, str | None, Any]]) -> bool:
        """这张口径卡算不算带遮罩：卡上有一个输入取自遮罩列，或者遮没遮核对不了。卡上记着每个输入的值（cell() 取的格、
        Agent 字段、按位置取的 vars.res.rows[0][2]）和代入式，整张卡不随文件附带——没法只拿掉几个值，拿掉了哈希就对不上。

        认哪个输入取自哪份查询结果的哪几列，和证据面板、推演同一个认法（recompute.input_origin）；按位置取的一格顺着
        取值链从写变量的节点在口径卡算这一次之前的产出起走。那几列算不算遮罩列，除了列名对上的，SQL 里给遮罩列起了别名、
        套了函数、经子查询或 WITH 转了一道的也算（hidden，同质询、双盲复算的认法）；SQL 认不出结果里哪一列取自哪一列的
        （列名表、整行引用这类，快照本身因此只给哈希），整份快照的列都算，两条路的判断一致。核对不了的宁可多挡：快照读不出来、
        取值链上的产出读不出来、那份结果没存快照只带着数据源名、定位里没写列。认得出、可结果既没带快照的 id 也没带数据源名
        的（数据整形改过形状）认不出出处，不算。

        卡本身按解析出来的内容认，不看哈希（同 content）：服务端的文件被改过，嵌进页面的照样是这份原字节，卡上记着的号码
        也照样在里面。读得出字节、解析不出一张卡的（截断的 JSON、不是对象）认不出有哪些输入，整张按带遮罩算，和读不出来的
        快照按带遮罩算（_snapshot_masked）是同一个道理。字节都读不出来的卡本来就不嵌（只给 id、标缺失），这里不管。"""
        key = f"card:{artifact}"
        if key not in self._masked:
            card = self.content(artifact)
            if not isinstance(card, dict):
                self._masked[key] = self.raw(artifact) is not None
                return self._masked[key]
            outputs = _card_outputs(events, artifact)

            def output(node_id: Any) -> Any:
                found = outputs.get(str(node_id or ""))
                return self.content(found) if found and self.intact(found) else None

            items = [item for metric in card.get("metrics") or [] if isinstance(metric, dict)
                     for item in metric.get("inputs") or [] if isinstance(item, dict)]
            self._masked[key] = any(self._input_masked(item, output) for item in items)
        return self._masked[key]

    def _input_masked(self, item: dict[str, Any], output: Callable[[Any], Any]) -> bool:
        origin = input_origin(item, output)
        if origin is None:
            return False
        if origin.unverified is not None:
            return True
        if origin.artifact is None:
            return origin.source is not None     # 没存快照：查询当时记下的遮罩无从核对
        content = self.content(origin.artifact)
        if not self.intact(origin.artifact) or not isinstance(content, dict):
            return True
        if not origin.columns:
            return self.masked(origin.artifact)
        hidden = self.hidden(origin.artifact)
        return any(c.lower() in hidden for c in origin.columns)


def _snapshot_masked(content: Any, source_masks: Mapping[str, Any], *, intact: bool,
                     seen: Callable[[], Any] | None = None) -> bool:
    if not isinstance(content, dict):
        return True
    recorded = content.get("mask_columns")
    sources = content.get("sources") if content.get("source") == MERGE_SOURCE else [content.get("source")]
    if not (isinstance(recorded, list) and any(isinstance(c, str) and c.strip() for c in recorded)) \
            and not any(source_masks.get(str(name or "")) for name in sources or []):
        return False
    return not (intact and _untouched(content, _snapshot_hidden(content, source_masks), seen))


def _snapshot_hidden(content: dict[str, Any], source_masks: Mapping[str, Any]) -> set[str]:
    """一份读得出来的快照要遮的列（小写）：查询当时记下的，加上它的数据源（合并查询看它合并的那几个）现在设的。"""
    recorded = content.get("mask_columns")
    hidden = {c.lower() for c in recorded if isinstance(c, str)} if isinstance(recorded, list) else set()
    sources = content.get("sources") if content.get("source") == MERGE_SOURCE else [content.get("source")]
    for name in sources if isinstance(sources, list) else []:
        now = source_masks.get(str(name or ""))
        hidden |= {str(c).lower() for c in now} if isinstance(now, (list, tuple, set, frozenset)) else set()
    return hidden


# --------------------------------------------------------------------------
# 设了遮罩的数据源上的查询：能不能证明没碰遮罩列
# --------------------------------------------------------------------------

#: 数据源查询工具（tools/datasource.py）落的查询快照里会有的键。有别的键的（比如核查 agent 在查询时就把遮罩列换成
#: 「已遮罩」、另记下了遮了哪些列）不知道它记着什么，照现有规则只给 id
_QUERY_KEYS = frozenset({"columns", "rows", "row_count", "truncated", "elapsed_ms", "sql", "column_types", "source",
                         "checks", "schema_artifact", "mask_columns", "data_version"})
#: 导出时只知道数据源设的遮罩、不知道是哪种库：SQL 按每种支持的库的读法各解析一遍（None 是 sqlglot 的通用读法），
#: 至少一种读得通，读得通的每一种都要过关
_READINGS = (None, "sqlite", "mysql", "postgres", "oracle")
#: 语法树和数据库实际执行的对不上的写法：MySQL、MariaDB 会执行的注释（/*! … */、/*M! … */，解析器当注释扔掉），
#: Postgres 按 Unicode 转义写的名字（U&"…"，原文里看不出是哪一列）
_OPAQUE_SQL = re.compile(r'/\*m?!|u&"', re.I)
#: 不点列名也读得出表里内容的写法。SQL 原文里出现就按碰了遮罩列算，写在字符串里的也算（宁可多挡）。函数要跟着左括号
#: 才算（列名碰巧叫 crosstab 的不算），表、视图、schema 按名字认
_INDIRECT_READS = re.compile(r"(?<![0-9A-Za-z_$])(?:(?:" + "|".join((
    # 表名、SQL 写在字符串里交给函数执行或整表导出：Postgres 的 XML 一族、ts_stat、tablefunc 的 crosstab / connectby、
    # dblink，Oracle 的 XMLTYPE、XQuery（XMLQUERY、XMLTABLE）
    r"(?:query|table|cursor|schema|database)_to_xml\w*", r"ts_stat", r"crosstab\d*", r"connectby", r"dblink\w*",
    r"xmltype", r"xmlquery", r"xmltable",
    # 按页、按文件、按日志读出的原始数据：Postgres 的 pageinspect、服务器文件、逻辑解码，MySQL 的 LOAD_FILE，Oracle 的
    # BFILENAME
    r"get_raw_page", r"pg_read_(?:binary_)?file", r"pg_logical_slot_\w+", r"load_file", r"bfilename",
)) + r")[\"`]?\s*\(|(?:" + "|".join((
    # Oracle 的 DBMS_XMLGEN 这一族（包名后面跟着点），XQuery 里的 ora:view 和 oradb: 表
    r"dbms_xml\w*", r"ora:view", r"oradb",
    # 统计信息里的采样值、全文索引里的词：Postgres 的 pg_stats、pg_statistic 一族，SQLite 的 sqlite_stat*，MySQL 的
    # column_statistics、innodb_ft_*，Oracle 的直方图、列统计和记着最小最大值的 *_tab_columns
    r"pg_stat(?:s|istic)\w*", r"sqlite_stat\d", r"column_statistics", r"innodb_ft_\w+",
    r"(?:all|dba|user|cdb)_(?:\w+_)?(?:histograms|col_statistics|tab_cols|tab_columns)",
    # 别的会话执行过的 SQL 原文（写入语句里就是值）：Postgres 的 pg_stat_activity，MySQL 的 general_log、slow_log，
    # Oracle 的 V$SQL 一族
    r"pg_stat_activity", r"general_log", r"slow_log", r"g?v\$sql\w*", r"(?:dba|cdb)_hist_sql\w*",
    # SQLite 按页读出的原始数据
    r"sqlite_dbpage",
    # 系统 schema：哪几张表采样了列值、记着别的会话的 SQL，一张张数靠不住，整个不认
    r"pg_catalog", r"information_schema", r"performance_schema",
)) + r")(?![0-9A-Za-z_$]))", re.I)
#: Postgres 的函数记法（m.to_jsonb 就是 to_jsonb(m)）下能把一整行交出来的内置函数（_whole_row）。自己建的函数这里不知道
_ROW_FUNCTIONS = frozenset({"to_json", "to_jsonb", "row_to_json", "hstore", "quote_literal", "quote_nullable",
                            "concat", "json_build_array", "jsonb_build_array", "array_agg", "json_agg", "jsonb_agg"})


def _untouched(snapshot: dict[str, Any], hidden: set[str], seen: Callable[[], Any] | None = None) -> bool:
    """数据源设了遮罩的一份查询快照，能不能证明这条查询没碰遮罩列（快照里不会有遮罩值）。全部满足才算，能证明的照常
    随文件附带：
    - 认得这份快照：数据源查询工具落的那种（不是合并查询，没有不认识的键），SQL、列名、行都在；
    - 结果的列名对不上遮罩列（按数据库交回的列名，不论大小写）；
    - SQL 原文里没写到遮罩列，条件里、COUNT 里也不行：SQL 随快照一起附带，WHERE phone = '…' 的字面量就是遮罩值；
      FROM 里的表函数吃进遮罩列时（unnest(ARRAY[phone])），它交出的列取自哪一列也认不出。这一条比「按 SQL 认不出结果列
      取自遮罩列」更严，有意的：COUNT(DISTINCT phone) 这类结果里没有号码的也只给哈希。连字符串、注释里写到的也算（宁可
      多挡）；
    - 没有语法树认不准的写法（_OPAQUE_SQL），也没有不点列名也读得出表里内容的写法（_INDIRECT_READS）；
    - SQL 解析得了；字面量里没写着这次运行里查到过的遮罩值（seen，_quotes）；每种读得通的读法下，没有认不出结果里哪一列
      取自哪一列的写法（_stray_columns：列名表、整行引用、可能展开出遮罩列的 *），按 SQL 认遮罩列的那一套
      （inquiry._derived_hidden，双盲复算、审批质询用的同一个）也认不出哪个结果列取自遮罩列。SQL 里没写到遮罩列时它本来
      就认不出，这一条是同一套认法的第二道关，那一套以后认得更多时跟着收紧。
    判断过程出错按碰了算。和证据面板一样只按列名认遮罩：视图、别的表里换了名字存着的遮罩列，自己建的函数，都认不出
    （遮罩只减少暴露，不是安全边界）。hidden 是要遮的列（小写，_snapshot_hidden）；seen 交回这次运行里查到过的遮罩值
    （_Store.seen）。"""
    names = {name.strip() for name in hidden if name.strip()}
    sql, columns = snapshot.get("sql"), snapshot.get("columns")
    if not names or snapshot.get("source") == MERGE_SOURCE or not set(snapshot) <= _QUERY_KEYS \
            or not isinstance(sql, str) or not sql.strip() or not isinstance(snapshot.get("rows"), list) \
            or not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        return False
    result = {c.strip().lower() for c in columns}
    if result & names or _writes_any(sql, names) or _OPAQUE_SQL.search(sql) or _INDIRECT_READS.search(sql):
        return False
    try:
        from app.data.sql_semantics import _parse, _read
        from app.engine.inquiry import _derived_hidden   # inquiry 导入了 engine.nodes 里的模块：用到时再导入

        readings = [(kind, tree) for kind in _READINGS for tree in [_parse(sql, _read(kind))[0]] if tree is not None]
        if not readings or (seen is not None and _quotes([tree for _, tree in readings], seen())):
            return False
        return not any(
            _stray_columns(tree) or result & {c.lower() for c in _derived_hidden(dict(snapshot), set(names), kind)[0]}
            for kind, tree in readings)
    except Exception:  # noqa: BLE001 - 解析器、认遮罩列的那一套出了意外：按碰了遮罩列算（宁可多挡）
        logger.warning("可验证报告：判断查询快照有没有碰遮罩列时出错，按碰了处理", exc_info=True)
        return False


def _derived_columns(snapshot: dict[str, Any], base: set[str]) -> set[str]:
    """一份快照里值取自遮罩列的结果列（小写）：遮罩列本身（base），加上 SQL 里给它们起别名、套函数、经子查询或 WITH
    转一道得来的（inquiry._derived_hidden，质询、双盲复算同一个认法）。SQL 按每种读得通的读法各认一遍、合起来；都读不通
    的照那一套的办法，写着遮罩列就整份的列都算。认不出结果里哪一列取自哪一列的，整份的列都算：语法树认不准、不点列名也
    读得出表里内容的写法（_OPAQUE_SQL、_INDIRECT_READS），以及哪一种读法下有列名表、整行引用、不照原名展开的 *、吃进
    遮罩列的表函数的（_renamed）——快照本身因此只给哈希，口径卡上取自它的值也一样。合并查询照那一套的办法不看 SQL。
    出错时整份的列都算（宁可多挡）。"""
    columns = snapshot.get("columns")
    every = set(base) | ({str(c).lower() for c in columns} if isinstance(columns, list) else set())
    try:
        from app.data.sql_semantics import _parse, _read
        from app.engine.inquiry import _derived_hidden

        sql = snapshot.get("sql")
        own = isinstance(sql, str) and snapshot.get("source") != MERGE_SOURCE
        if own and (_OPAQUE_SQL.search(sql) or _INDIRECT_READS.search(sql)):
            return every
        readings = [(kind, tree) for kind in _READINGS for tree in [_parse(sql, _read(kind))[0] if own else None]
                    if tree is not None]
        out = set(base)
        for kind, tree in readings or [(None, None)]:
            found, names = _derived_hidden(dict(snapshot), set(base), kind)
            if tree is not None and _renamed(tree, {n.lower() for n in names}):
                return every
            out |= {c.lower() for c in found}
        return out
    except Exception:  # noqa: BLE001 - 认遮罩列的那一套出了意外：整份的列都按遮罩算
        logger.warning("可验证报告：按 SQL 认改了名字的遮罩列时出错，整份快照的列都按遮罩处理", exc_info=True)
        return every


def _writes_any(sql: str, names: Iterable[str]) -> bool:
    """SQL 原文里有没有写到这些名字：不论大小写，前后不是字母、数字、下划线（引号、括号、点都算隔开）。"""
    return any(re.search(rf"(?<![0-9A-Za-z_]){re.escape(n)}(?![0-9A-Za-z_])", sql, re.I) for n in names if n)


def _quotes(trees: Iterable[Any], secrets: Any) -> bool:
    """SQL 的字面量（'…'、数、$$…$$、E'…'、N'…'、U&'…'，MySQL 读法下的 "…"）里有没有写着按值认得出的遮罩值（secrets
    是 inquiry._Secrets）。只看字面量：守卫已经去掉了注释；表名、列名碰巧和哪个值一样不算（整行摊开的画像查询把列名
    当值交了出来，那些列名会出现在别的 SQL 里）。trees 是各种读法解析出来的语法树，字面量合起来看。"""
    from sqlglot import exp

    kinds = (exp.Literal, exp.RawString, exp.National, exp.UnicodeString, exp.ByteString)
    texts = {node.this for tree in trees for node in tree.walk()
             if isinstance(node, kinds) and isinstance(node.this, str)}
    for text in texts:
        pattern = secrets.pattern(text)
        if pattern is not None and pattern.search(text):
            return True
    return False


def _stray_columns(tree: Any) -> bool:
    """语法树里有没有认不出结果里哪一列取自哪一列的写法。有的话，就算 SQL 里没写到遮罩列、结果的列名也对不上，结果里
    照样可能是遮罩列的值：
    - 列名表、整行引用（_listed_or_whole）；
    - 可能展开出遮罩列的 *：展开了数据源里的表（隔着几层派生来源也算，_star_reaches），又不是只按名字往外传的那几种
      （_contained）。COUNT(*) 只数个数，不算。"""
    if _listed_or_whole(tree):
        return True
    memo: dict[int, bool] = {}
    return any(_star_reaches(node, memo) and not _contained(node) for node in _stars(tree))


def _renamed(tree: Any, names: set[str]) -> bool:
    """结果里可能有遮罩列的值、列名却和 SQL 里认得出的名字对不上的写法（口径卡按列名认遮罩列，这几种认不出）：
    - 列名表、整行引用（_listed_or_whole）；
    - 不照原名展开的 *：UNION 这类里按位置对上第一支列名的，套在表达式、标量子查询里装进一列的。最外层直接写的 *、
      只按名字往外传的（_contained）照原名展开，遮罩列在结果里还叫原来的名字；
    - FROM、JOIN 里的表函数（UNNEST、ROWS FROM 这类，_table_function）吃进了遮罩列（names：SQL 里指代遮罩列的名字，
      小写）：它交出的列叫什么、装着什么，SQL 里看不出。"""
    from sqlglot import exp

    if _listed_or_whole(tree) or any(not (_contained(node) or _outermost(node)) for node in _stars(tree)):
        return True
    return any(_table_function(source) and names & {c.name.lower() for c in source.find_all(exp.Column)}
               for select in tree.find_all(exp.Select) for source in _sources(select))


def _listed_or_whole(tree: Any) -> bool:
    """有没有列名表（members AS m(a, b, c)、(…) AS t(a, b)、WITH t(a, b)：按位置给列改了名），或者整行引用
    （Postgres 的 SELECT m、to_json(m)，_whole_row：一整行装进一列）。"""
    from sqlglot import exp

    return any(alias.columns for alias in tree.find_all(exp.TableAlias)) \
        or any(_whole_row(column) for column in tree.find_all(exp.Column))


def _sources(select: Any) -> list[Any]:
    """一条 SELECT 的 FROM、JOIN 里的来源节点。括号括起来的一组连接（(members m JOIN orders o ON …)）摊开，里面每个
    来源也算：组里的表函数看得见组里前面的来源，外层也看得见组里的名字（组写了别名时 Postgres 外层只认别名，这里宁可
    多认）。"""
    from_ = select.args.get("from_")
    pending = ([from_.this] if from_ is not None and from_.this is not None else []) \
        + [join.this for join in select.args.get("joins") or [] if join.this is not None]
    out: list[Any] = []
    while pending:
        source = pending.pop(0)
        out.append(source)
        head = _join_group(source)
        if head is not None:
            if head is not source:
                out.append(head)
            pending[:0] = [join.this for join in head.args["joins"] if join.this is not None]
    return out


def _join_group(source: Any) -> Any:
    """括号括起来的一组连接里打头的那个来源（语法树里是一层括号包着一张带 joins 的表）；别的来源是 None。"""
    from sqlglot import exp

    node = source
    while isinstance(node, exp.Subquery):
        node = node.this
    return node if isinstance(node, exp.Table) and node.args.get("joins") else None


def _derived(source: Any) -> Any:
    """FROM、JOIN 里的一个来源是派生来源（子查询、LATERAL、指向 WITH 里一段的名字）时，它的那块查询；数据源里的表、表函数、
    VALUES 这类是 None（有哪些列、装着什么，SQL 里看不出）。"""
    from sqlglot import exp

    from app.data.sql_semantics import _visible_ctes

    node = source.this if isinstance(source, exp.Lateral) else source
    if isinstance(node, exp.Table):
        if node.args.get("db") or node.args.get("catalog") or not isinstance(node.this, exp.Identifier):
            return None
        cte = _visible_ctes(node).get(node.name.lower())
        if cte is None:
            return None
        node = cte.this
    while isinstance(node, exp.Subquery):
        node = node.this
    return node if isinstance(node, (exp.Select, exp.SetOperation)) else None


def _table_function(source: Any) -> bool:
    """FROM、JOIN 里的一个来源是不是表函数这类（函数调用、UNNEST、ROWS FROM、VALUES，写没写 LATERAL 都一样）：既不是
    数据源里的表，也不是派生来源。"""
    from sqlglot import exp

    if (isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier)) or _join_group(source) is not None:
        return False
    return _derived(source) is None


def _leaves(block: Any) -> list[Any]:
    """一块查询的每一支，按先后：UNION 这类摊开，套在外面的括号去掉。"""
    from sqlglot import exp

    if isinstance(block, exp.Subquery):
        return _leaves(block.this)
    if isinstance(block, exp.SetOperation):
        return _leaves(block.this) + _leaves(block.expression)
    return [block]


def _whole_row(column: Any) -> bool:
    """一个名字是不是整行引用（Postgres 的 SELECT m、to_json(m)，函数记法的 m.to_jsonb：一整行装进一列）。

    没写表名的，Postgres 先按列认：所在 SELECT 的派生来源明写出了叫这个名字的列，就是那一列；不然对上它看得见的某个来源
    的名字（别名、表名、WITH 里的一段）就算。写了表名的，列名是 _ROW_FUNCTIONS 里的、表名对上看得见的来源才算。
    看得见的是所在 SELECT 和外层 SELECT 的 FROM、JOIN 里的来源。FROM、JOIN 里的表函数（UNNEST、ROWS FROM 这类）和
    LATERAL 看得见同一层的来源：Postgres 的函数不写 LATERAL 也看得见（逗号、CROSS JOIN、JOIN … ON TRUE 都一样），整行
    交给 jsonb_each_text 这类就一列列竖着摊开了；括号括起来的一组连接按同一层算（_sources 摊开）。FROM、JOIN 里的子查询
    不写 LATERAL 看不见，WITH 里的一段也看不见。数据源里的表有哪些列这里不知道：碰巧和表、别名同名的列也算（宁可多挡）。"""
    from sqlglot import exp

    select = column.find_ancestor(exp.Select)
    if isinstance(column.this, exp.Star) or not column.name or select is None:
        return False
    if column.table:
        if column.name.lower() not in _ROW_FUNCTIONS:
            return False
        name = column.table.lower()
    else:
        name = column.name.lower()
        for body in filter(None, map(_derived, _sources(select))):
            first = _leaves(body)[0]
            if isinstance(first, exp.Select) and any(
                    isinstance(item, (exp.Alias, exp.Column)) and not isinstance(item.this, exp.Star)
                    and item.alias_or_name.lower() == name for item in first.expressions):
                return False
    child, node = column, column.parent
    while node is not None:
        # FROM、JOIN 里真的子查询（括号里是一条查询，不是一组连接）看不见同一层，WITH 里的一段也看不见
        subquery = isinstance(child.this, exp.Subquery) and _derived(child.this) is not None
        sees = child.arg_key != "with_" and not (child.arg_key in ("from_", "joins") and subquery)
        if isinstance(node, exp.Select) and sees \
                and any((source.alias_or_name or "").lower() == name for source in _sources(node)):
            return True
        child, node = node, node.parent
    return False


def _stars(tree: Any) -> list[Any]:
    """语法树里的 *（t.* 取包着它的 Column），COUNT(*)、COUNT(t.*) 不算：只数个数。"""
    from sqlglot import exp

    nodes = [star.parent if isinstance(star.parent, exp.Column) else star for star in tree.find_all(exp.Star)]
    return [node for node in nodes if not isinstance(node.parent, exp.Count)]


def _star_reaches(node: Any, memo: dict[int, bool]) -> bool:
    """这个 *（t.* 是 Column 包着的 *）展开了数据源里的表没有：t.* 看 FROM、JOIN 里叫 t 的那一个来源，单独的 * 看所在
    SELECT 的全部来源。数据源里的表、表函数、VALUES 这类来源都算展开了；派生来源看它每一支选出项里的 * 展开了没有
    （隔几层都一样）。t 对不上任何来源（外层的别名）、找不到所在的 SELECT，按展开了算。"""
    from sqlglot import exp

    select = node.find_ancestor(exp.Select)
    if select is None:
        return True
    sources = _sources(select)
    if isinstance(node, exp.Column):
        sources = [s for s in sources if (s.alias_or_name or "").lower() == node.table.lower()]
        if not sources:
            return True
    for source in sources:
        block = _derived(source)
        if block is None:
            return True
        if id(block) not in memo:
            # 先记成展开了：递归的 WITH 绕回自己时按展开了算
            memo[id(block)] = True
            memo[id(block)] = any(not isinstance(leaf, exp.Select) or any(
                _star_reaches(star, memo) for item in leaf.expressions for star in _stars(item))
                for leaf in _leaves(block))
        if memo[id(block)]:
            return True
    return False


def _contained(node: Any) -> bool:
    """展开出来的列只按名字往外传的 *：它就是一条 SELECT 的选出项本身（不套在表达式里），这条 SELECT 不在 UNION 这类
    里，而且是 FROM / JOIN 里的派生来源、LATERAL、WITH 里的一段，或者 EXISTS / IN 里的子查询（只决定有没有、在不在）。
    派生来源的列外层只能按名字取（取到遮罩列，名字就写在 SQL 里），或者再用一个 * 展开（那个 * 照样要过这一关），
    或者整行交出去（整行引用另认，_whole_row）。最外层的 *（结果就是它展开的列）、UNION 里的、标量子查询里的都不算。"""
    from sqlglot import exp

    select = node.parent
    if not isinstance(select, exp.Select) or node.arg_key != "expressions":
        return False
    outer = select
    while isinstance(outer.parent, exp.Subquery):
        outer = outer.parent
    parent = outer.parent
    if isinstance(parent, (exp.CTE, exp.Lateral, exp.Exists)):
        return True
    if isinstance(parent, (exp.From, exp.Join)):
        return outer is not select
    return isinstance(parent, exp.In) and outer.arg_key == "query"


def _outermost(node: Any) -> bool:
    """这个 * 是不是最外层那条 SELECT 的选出项本身（不套在表达式里，这条 SELECT 也不在 UNION 这类里）：展开的列照原来
    的名字落进结果。"""
    from sqlglot import exp

    select = node.parent
    if not isinstance(select, exp.Select) or node.arg_key != "expressions":
        return False
    outer = select
    while isinstance(outer.parent, exp.Subquery):
        outer = outer.parent
    return outer.parent is None


def _card_outputs(events: Sequence[tuple[int, str, str | None, Any]], card: str) -> dict[str, str]:
    """算出这份指标集的那一次口径卡执行开始之前，每个节点最后一次的产出工件：卡上 vars.*、nodes.* 取到的就是它们
    （和证据接口 api/evidence._card_outputs 同一个认法）。认不出是哪一次执行时取封存范围内每个节点最后一次的产出。"""
    found = next(((seq, node_id) for seq, etype, node_id, data in events
                  if etype == EventType.NODE_FINISHED and isinstance(data, dict)
                  and any(isinstance(e, dict) and e.get("kind") == "metric_set" and e.get("artifact") == card
                          for e in data.get("evidence") or [])), None)
    bound = None
    if found is not None:
        bound = max((seq for seq, etype, node_id, _ in events
                     if etype == EventType.NODE_STARTED and node_id == found[1] and seq < found[0]), default=found[0])
    out: dict[str, str] = {}
    for seq, etype, node_id, data in events:
        if bound is not None and seq >= bound:
            break
        if etype == EventType.NODE_FINISHED and node_id and isinstance(data, dict) and data.get("artifact"):
            out[node_id] = str(data["artifact"])
    return out


def _query_artifacts(events: Iterable[tuple[int, str, str | None, Any]]) -> list[str]:
    """封存范围内交回过的查询快照：tool.end / merge.end 的 query_artifact，节点台账里的 query 条目。"""
    out: list[str] = []
    for _, etype, _, data in events:
        if not isinstance(data, dict):
            continue
        if etype in (EventType.TOOL_END, EventType.MERGE_END) and isinstance(data.get("query_artifact"), str):
            out.append(data["query_artifact"])
        elif etype == EventType.NODE_FINISHED:
            out += [str(e["artifact"]) for e in data.get("evidence") or []
                    if isinstance(e, dict) and e.get("kind") == "query" and e.get("artifact")]
    return list(dict.fromkeys(a for a in out if a))


_UNSEEN = object()


def _query_returned(data: Mapping[str, Any], store: _Store) -> tuple[str, str | None]:
    """一次数据源查询（db_query__ 工具的 tool.end）交回了什么：("snapshot", 查询快照 id)；("none", None) 没交回数据
    （查询失败、SQL 被拒的那句原话）；认不出来是 ("unknown", None)。

    新运行在 query_artifact 里直接记了快照 id。没记的有三种：查询没成功；升级前的运行（证据台账之前，tool.end 只有
    工具快照，查询快照的 id 嵌在工具快照记下的返回里，照证据接口 _legacy_pool 的办法认）；协作团队成员的查询（连工具
    快照都不落，只能看事件里的预览，长一点的返回被截断，解析不了）。"""
    artifact = data.get("query_artifact")
    if isinstance(artifact, str) and artifact:
        return "snapshot", artifact
    returned: Any = _UNSEEN
    snapshot = data.get("artifact")
    # 工具快照要哈希对得上、记的是同一个工具：被改过的不能拿来说这次查询没有遮罩
    if isinstance(snapshot, str) and snapshot and store.intact(snapshot):
        content = store.content(snapshot)
        if isinstance(content, dict) and content.get("tool") == data.get("tool") and "result" in content:
            returned = content["result"]
    if returned is _UNSEEN:
        preview = data.get("preview")
        if not isinstance(preview, str):
            return "unknown", None
        returned = preview
        if preview.startswith('"'):
            # 调用工具节点的预览是把返回值再 json.dumps 一遍（文本外面套了一层引号），截断了就剥不开
            try:
                returned = json.loads(preview)
            except ValueError:
                return "unknown", None
    fields = query_entry_fields(returned)
    if fields:
        return "snapshot", str(fields["artifact"])
    # 数据源工具成功时交回的是 JSON 对象；不是对象的是一句原话，对象里没有 rows 的是报错
    if isinstance(returned, str) and not returned.lstrip().startswith("{"):
        return "none", None
    if isinstance(returned, dict) and "rows" not in returned:
        return "none", None
    return "unknown", None


def _run_queries(events: Sequence[tuple[int, str, str | None, Any]], store: _Store) -> list[str]:
    """封存范围内查到过的查询快照：_query_artifacts 之外，升级前的运行从工具快照、预览里认出来的（_query_returned）。"""
    out = _query_artifacts(events)
    for _, etype, _, data in events:
        if etype == EventType.TOOL_END and isinstance(data, dict) \
                and str(data.get("tool") or "").startswith(QUERY_PREFIX):
            artifact = _query_returned(data, store)[1]
            if artifact:
                out.append(artifact)
    return list(dict.fromkeys(out))


def _masked_run(events: Sequence[tuple[int, str, str | None, Any]], store: _Store, cited: Iterable[str],
                source_masks: Mapping[str, Any]) -> bool:
    """运行里有没有哪次查询的结果可能带着遮罩列。有的话，成果节点和运行结束事件的预览可能直接带出查询结果，只给哈希。

    宁严勿宽：封存范围内每一次数据源查询都要对得上一份没有遮罩的查询快照，或者确实没交回数据，才算没有。数据源
    现在设了遮罩的，按工具名就知道，不必去认快照；认不出查了什么的，按带遮罩算。"""
    known = [*_query_artifacts(events), *cited]
    for _, etype, _, data in events:
        if etype != EventType.TOOL_END or not isinstance(data, dict):
            continue
        tool = str(data.get("tool") or "")
        if not tool.startswith(QUERY_PREFIX):
            continue
        if source_masks.get(tool[len(QUERY_PREFIX):]):
            return True
        kind, artifact = _query_returned(data, store)
        if kind == "unknown":
            return True
        if artifact:
            known.append(artifact)
    return any(store.masked(a) for a in dict.fromkeys(known))


# --------------------------------------------------------------------------
# 报告文档和它引用到的工件
# --------------------------------------------------------------------------


def _report_events(events: Iterable[tuple[int, str, str | None, Any]]) -> list[dict[str, Any]]:
    """每个报告节点最后一次自查记下的文档，按第一次出现的顺序。和证据接口（api/evidence._reports）同一个认法：
    报告重写过的，认最后一次。"""
    latest: dict[str, dict[str, Any]] = {}
    for seq, etype, node_id, data in events:
        if etype == EventType.REPORT_CHECKED and node_id and isinstance(data, dict) and data.get("doc_artifact"):
            latest[node_id] = {"node_id": node_id, "doc_artifact": str(data["doc_artifact"]), "seq": seq}
    return list(latest.values())


def _cited_artifacts(doc: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """报告正文引用到的口径卡和查询快照：(工件 id, 种类, 来源节点)，按正文顺序、去重。

    看每个解析成功的引用（行内的数字、单元格，句末的 [[see:]]）在文档目录里指向的条目：指标条目指向口径卡，
    查询条目指向快照。整表在组装时已经展开成每格一个单元格引用，不用另看。"""
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    out: list[tuple[str, str, str | None]] = []
    for _, unit in iter_units(doc):
        if not isinstance(unit, dict):
            continue
        cites = [s.get("cite") for s in unit.get("segments") or [] if isinstance(s, dict)]
        for cite in [*cites, *(unit.get("see") or [])]:
            if not isinstance(cite, dict) or cite.get("status") != "resolved":
                continue
            entry = catalog.get(cite.get("alias"))
            kind = _CITED_KINDS.get(entry.get("kind")) if isinstance(entry, dict) else None
            if kind and isinstance(entry.get("artifact"), str) and entry["artifact"]:
                owner = entry.get("node_id")
                out.append((entry["artifact"], kind, str(owner) if owner else None))
    return list(dict.fromkeys(out))


class _Artifacts:
    """嵌进页面的工件清单，按先后：报告文档在前，接着是它引用到的。同一份只列一次。events 是封存范围内的事件
    （认口径卡带不带遮罩时找写变量的节点当时的产出）。"""

    def __init__(self, store: _Store, events: Sequence[tuple[int, str, str | None, Any]] = ()) -> None:
        self.store = store
        self.events = events
        self.items: list[dict[str, Any]] = []
        self._seen: set[str] = set()

    def add(self, artifact: str, kind: str, node_id: str | None) -> None:
        if artifact in self._seen:
            return
        self._seen.add(artifact)
        item: dict[str, Any] = {"id": artifact, "kind": kind}
        if node_id:
            item["node_id"] = node_id
        raw = self.store.raw(artifact)
        if raw is None:
            # 服务端的文件不在了：只给 id，浏览器照实说核对不了
            item["missing"] = True
        elif (kind == "query" and self.store.masked(artifact)) \
                or (kind == "metric_set" and self.store.card_masked(artifact, self.events)):
            item["masked"] = True
            # 不嵌内容，浏览器就算不出这份文件对不对得上：服务端自己看到的照实带上，核对页据此判不通过、说清原因。
            # 以前被改过的卡原样嵌进去，浏览器自己算出对不上；现在不嵌了，这个证据不能跟着丢
            if not self.store.intact(artifact):
                item["mismatch"] = True
            if not isinstance(self.store.content(artifact), dict):
                item["unparsable"] = True
        else:
            # 原样嵌入，不在这里复验哈希：被改过的文件照实交出去，浏览器自己算出对不上
            item["b64"] = base64.b64encode(bytes(raw)).decode("ascii")
        self.items.append(item)


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------


def _shows_content(etype: str, node_id: str | None, data: Any, node_types: Mapping[str, str], store: _Store,
                   masked_run: bool) -> bool:
    """这条封存范围内的事件带不带规范字符串（白名单）。"""
    if etype in _SHOWN_EVENTS:
        return True
    if etype == EventType.RUN_FINISHED:
        # 运行结束事件里是成果：字段可以直接引用查询结果，运行里有带遮罩的快照时只给哈希
        return not masked_run
    if etype == EventType.NODE_FINISHED:
        kind = node_types.get(node_id or "")
        return kind in _SHOWN_NODES or (kind == NodeType.OUTPUT and not masked_run)
    if etype == EventType.TOOL_END:
        # 查询类：把查询快照的 id 和运行记录连起来。预览里是查询结果的前一截，数据源带遮罩的只给哈希
        artifact = data.get("query_artifact") if isinstance(data, dict) else None
        return isinstance(artifact, str) and bool(artifact) and not store.masked(artifact)
    return False


def _clip(text: Any, limit: int = 60) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _summary(etype: str, data: Any) -> str:
    """封存之后追加的事件说一句是什么。只挑不会带出数据的字段：判了几句、问的是什么、日志那一句。"""
    if not isinstance(data, dict):
        return ""
    if etype == EventType.EVIDENCE_JUDGED:
        verdicts = data.get("verdicts")
        count = len(verdicts) if isinstance(verdicts, dict) else 0
        model = data.get("model")
        return f"按需裁判 {count} 句结论" + (f"（模型 {_clip(model, 40)}）" if isinstance(model, str) and model else "")
    if etype == EventType.APPROVAL_INQUIRY:
        question = data.get("question")
        return f"质询：{_clip(question)}" if isinstance(question, str) and question.strip() else "审批质询"
    if etype == EventType.LOG and isinstance(data.get("message"), str):
        return _clip(data["message"])
    return ""


def _tier(events: Sequence[tuple[int, str, str | None, Any]]) -> dict[str, Any] | None:
    """出具档位：封存范围内最后一条出具判定事件里的 tier，带上序号（浏览器拿那条已核对的事件对照）。"""
    for seq, etype, _, data in reversed(events):
        if etype == EventType.ISSUANCE and isinstance(data, dict) and isinstance(data.get("tier"), str):
            return {"value": data["tier"], "seq": seq}
    return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------


def display_name(run: Mapping[str, Any]) -> str:
    """页面标题、附件名用的运行名：工作流名；没保存的工作流一律叫「未保存的工作流」。"""
    name = str(run.get("workflow_name") or "").strip()
    if not run.get("workflow_id") and name in _UNSAVED_NAMES:
        return UNSAVED_NAME
    return name or f"运行 {str(run.get('id') or '')[:8]}".strip()


def attachment_name(run: Mapping[str, Any]) -> str:
    """附件名「<运行名>-可验证报告.html」。文件名里不能有的字换成下划线。"""
    name = _BAD_FILENAME.sub("_", display_name(run)).strip(" .")[:_NAME_MAX].strip(" .")
    return f"{name or UNSAVED_NAME}{FILE_SUFFIX}"


def content_disposition(filename: str, *, run_id: str) -> str:
    """附件头：中文名按 RFC 5987 写在 filename* 里；filename 给不认 filename* 的老客户端一个纯 ASCII 的名字。"""
    ascii_id = re.sub(r"[^A-Za-z0-9]", "", run_id)[:8]
    return f"attachment; filename=\"agentlab-{ascii_id}-bundle.html\"; filename*=UTF-8''{quote(filename, safe='')}"


def build_bundle(
    run: Mapping[str, Any],
    events: Sequence[tuple[int, str, str | None, Any]],
    *,
    manifest_seq: int,
    manifest_hash: str,
    read: Reader,
    exported_at: str,
    legacy: bool = False,
    graph: Mapping[str, Any] | None = None,
    source_masks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """一次已封存运行 → 页面里的数据（bundle-data）。

    - run：运行记录的几样（id、workflow_id、workflow_name、version、version_hash、run_class、status、
      started_at、finished_at）
    - events：这次运行的全部事件 (seq, type, node_id, data)，按数据库里 seq 的顺序。和封存核对
      （runner.verify_manifest）读的是同一个顺序：seq 相同的几条，浏览器按这里的先后排，清单才算得一样
    - manifest_seq / manifest_hash：封存范围（老运行是按结束事件推算的那个，legacy 为真）和封存哈希
    - read：工件 id → 原字节（不复验哈希），没有就是 None
    - source_masks：数据源名 → 它现在设的遮罩列（只列设了的）
    """
    graph = graph if isinstance(graph, Mapping) else {}
    node_types: dict[str, str] = {}
    nodes: dict[str, dict[str, str]] = {}
    for item in graph.get("nodes") or []:
        if isinstance(item, Mapping) and item.get("id"):
            nid = str(item["id"])
            data = item.get("data") if isinstance(item.get("data"), Mapping) else {}
            node_types[nid] = str(item.get("type") or "")
            nodes[nid] = {"label": str(data.get("label") or nid), "type": node_types[nid]}

    sealed = [e for e in events if e[0] <= manifest_seq]
    later = [e for e in events if e[0] > manifest_seq]
    store = _Store(read, source_masks or {}, sealed)

    artifacts = _Artifacts(store, sealed)
    reports = _report_events(sealed)
    cited_queries: list[str] = []
    for report in reports:
        doc_id, node_id = report["doc_artifact"], report["node_id"]
        artifacts.add(doc_id, "report_doc", node_id)
        doc = store.content(doc_id) if store.intact(doc_id) else None
        # 文档要真是这次运行里这个节点写的：内容寻址只保证没被改，不保证没被张冠李戴。对不上的照实嵌进去让
        # 浏览器标出来，但它的目录不可信，不顺着它去找别的工件
        if not isinstance(doc, dict) or doc.get("run_id") != run.get("id") or doc.get("node_id") != node_id:
            continue
        for artifact, kind, owner in _cited_artifacts(doc):
            artifacts.add(artifact, kind, owner)
            if kind == "query":
                cited_queries.append(artifact)

    masked_run = _masked_run(sealed, store, cited_queries, source_masks or {})
    shown_events = []
    for seq, etype, node_id, data in sealed:
        text = canonical_json(data)
        item: dict[str, Any] = {"seq": seq, "type": etype, "node_id": node_id, "data_sha256": content_hash(text)}
        if _shows_content(etype, node_id, data, node_types, store, masked_run):
            item["canonical"] = text
        shown_events.append(item)

    return {
        "schema": SCHEMA,
        "meta": {
            "run_id": run.get("id"), "workflow_id": run.get("workflow_id"), "workflow_name": display_name(run),
            "version": run.get("version"), "version_hash": run.get("version_hash"),
            "run_class": run.get("run_class"), "status": run.get("status"),
            "started_at": _iso(run.get("started_at")), "finished_at": _iso(run.get("finished_at")),
            "manifest_seq": manifest_seq, "manifest_hash": manifest_hash, "legacy": bool(legacy),
            "tier": _tier(sealed), "exported_at": exported_at,
        },
        "nodes": nodes,
        "events": shown_events,
        "appended": [{"seq": seq, "type": etype, "node_id": node_id, "summary": _summary(etype, data)}
                     for seq, etype, node_id, data in later],
        "reports": reports,
        "artifacts": artifacts.items,
    }


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

#: 数据放在 <script type="application/json"> 里：< > & 换成转义，正文里的 </script>、<!-- 截不断数据块；
#: U+2028 / U+2029 在老的 JS 解析器里是换行，一并转义。这几个字只会出现在 JSON 的字符串里，转义后 JSON 不变
_JSON_ESCAPES = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
                               "\u2028": "\\u2028", "\u2029": "\\u2029"})
_PLACEHOLDER = re.compile(r"__BUNDLE_(TITLE|NAME|SEAL|RUN|FAILED|CSP|DATA|SCRIPT)__")


@lru_cache(maxsize=1)
def _assets() -> tuple[str, str]:
    here = Path(__file__).parent
    return (here / "template.html").read_text(encoding="utf-8"), (here / "verifier.js").read_text(encoding="utf-8")


def bundle_json(bundle: Mapping[str, Any]) -> str:
    """页面数据块里的 JSON 文本。"""
    return json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).translate(_JSON_ESCAPES)


def render_html(bundle: Mapping[str, Any]) -> str:
    """把数据和核对脚本填进模板：样式、脚本都内联，零外链。

    内容安全策略按哈希只放行这一段核对脚本，默认什么都不许连：页面打开后不会、也不能向任何地方发请求。
    工作流名、封存哈希、运行 ID 直接写进页面：邮件、即时通讯的附件预览和系统的快速查看常常不执行脚本，也要看得到
    该拿什么去系统里对照。脚本执行后拿数据里的封存哈希核对，再核一遍这里写出的和它一致。失败结束的运行另写一句：
    之后在系统中继续运行会重新封存，这个文件就再也对不上了。
    占位符一次替换完，填进去的内容不会再被当成占位符。"""
    template, script = _assets()
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    meta = bundle.get("meta") or {}
    name = str(meta.get("workflow_name") or UNSAVED_NAME)
    values = {"TITLE": html.escape(f"{name} · 可验证报告"), "NAME": html.escape(name),
              "SEAL": html.escape(str(meta.get("manifest_hash") or "")), "RUN": html.escape(str(meta.get("run_id") or "")),
              "FAILED": "" if meta.get("status") == "failed" else " hidden",
              "CSP": f"'sha256-{digest}'", "DATA": bundle_json(bundle), "SCRIPT": script}
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
