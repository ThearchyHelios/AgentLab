"""可验证报告的构建：把一次已封存运行的报告、证据和运行记录打成一个自包含的 HTML 文件。

拿到文件的人不连服务、不装任何东西，在浏览器里就能核对（verifier.js）：

- 事件：封存范围内（seq ≤ manifest_seq）的每一条都带 {seq, type, node_id, data_sha256}，data_sha256 是
  sha256(canonical_json(data))，和 runner 封存时算 manifest_hash 用的是同一个算法，浏览器按同样的拼法重算
  清单哈希、和封存哈希比。白名单里的事件另带服务端算好的规范字符串 canonical（逐字等于 canonical_json(data)），
  浏览器先核它的哈希，再从里面读出报告文档、口径卡、查询快照的 id。其余事件只给哈希：工具返回、节点产出的
  预览里可能有遮罩列的原值和提示词。口径卡、报告的节点完成事件在白名单里，可它们的预览就是卡上每个指标的值、报告正文：
  卡只给 id 的（和报告引不引用无关）、文档只给 id 的，这两条也只给哈希（_card_shown）。
- 工件：每份报告文档（各报告节点最后一次自查记下的 doc_artifact）、它引用到的口径卡和查询快照，按原字节
  base64 嵌入，浏览器重算 sha256 和 id 比。数据源设了遮罩的查询快照，能证明这条查询没碰遮罩列的（_untouched）照常
  嵌入，别的不嵌内容、只给 id。口径卡上记着每个输入的值和代入式，有输入取自遮罩列（cell() 的格、Agent 字段、按位置
  取的 vars.res.rows[0][2]，SQL 里改了名字的遮罩列也算，SQL 认不出结果里哪一列取自哪一列的整份快照的列都算）的整张卡
  同样只给 id，认法和证据面板、推演同一个（recompute.input_origin）；有指标的值不给的（metric_word：值取自遮罩列的
  一格、一行，整行、下标不是字面量的、转抄的上游口径卡在任何一次执行里有这类指标的也算，转抄的在指标依赖图上一次认，
  recompute.masked_closure；遮罩列按这份文件宁可多遮的认法）、卡上记着的转抄来的值带遮罩的（_copies_masked）也一样；服务端
  文件被改过的卡照样按卡上记的输入认，解析不出一张卡的整张只给 id。报告文档的证据目录收着上游口径卡的全部指标，现在的
  报告撰写节点把值不给的写成「已遮罩」；还写着原值的老文档（升级前生成的，或者生成之后数据源才设的遮罩）整份只给 id、
  不附带它引用的工件——哪些指标的值不给和写作目录同一个判断（metric_word 的 sure：只按确定取自遮罩列的认，由调用方按
  证据面板的那一套认好交进来，api/evidence.py 的 certain_masks），SQL 认不准时没确定取自遮罩列的普通金额照常附带。
  文档里写成「已遮罩」的指标所在的口径卡一律只给 id。随文件附带的口径卡，它记着的输入指向的查询快照也一并附上（遮罩
  规则同上）：点开数字时看得到每个输入的 SQL 和被引用的行，引用链（④）按「已核对的口径卡的输入」认它们。只给 id 的
  工件，服务端的文件和 id 对不上的另标 mismatch、解析不出来的另标 unparsable，核对页照实说（对不上的判不通过）。没被
  报告引用、也没被附带的口径卡的输入引用的工件不嵌。
- 分析结论（[[a:A3]]、[[v:A3.r0.列]]、[[see:A3]] 引用的结论工件）照样按原字节嵌入，连同它的输入快照（遮罩规则同上）和
  下钻的上一层结论；任一份输入快照只给 id（证明不了没碰遮罩列）的，结论本身也只给 id——结果表里的维度值取自输入快照，
  这份文件宁可多挡。浏览器只核哈希、核判定语和数是不是结论里写的那些，不在页面里重算统计：结论随文件附带时，服务端在
  导出时按封存的输入复算一遍（engine/evidence.analysis_recheck），结果记在这份工件的条目上（recheck），页面照它展示。
  口径卡指标上的可比性结论（口径卡执行时自动做的，不进台账、只从卡上引用）随附带的卡一起附上，规则同上，引用链（④）
  按「已核对的口径卡记着的可比性」认它。分析工具的 tool.end（预览里是结论的前一截）、分析卡一项的 analysis.end（带着
  判定语）和结论本身同一个认法：结论随文件附带得了才带规范字符串。
- 报告文档带着系统写的「分析范围」（analysis_scope）的，页面在正文后面照它渲染，编号点得开那条结论。
- 封存之后追加的事件（按需裁判、审批质询这类批注）单列：类型、序号、内容摘要，不进清单。

它只证明「内部一致」：数字、文档、运行记录彼此对得上，而且没被改过。真伪要拿页面上的封存哈希和系统里这次
运行的封存核对（GET /api/runs/{id}/verify）对照，页面上醒目地写着这一点。

已知局限，同样以服务端封存的清单哈希为准：
- 遮罩只减少暴露、不是安全边界。哪些列取自遮罩列按 SQL 和列名认，写法的长尾认不全（先给遮罩列起别名、外层再套没起别名的
  表达式，+phone 这类认法本身漏掉的表达式，Postgres 给没起别名的表达式起的列名，视图、别的表里换了名字存着的遮罩列，
  自己建的函数，数据整形取出来再赋给变量的号码）：取自它们的指标、快照在这份文件里照常附带，和证据面板、写作目录认法一致。
  转抄上游口径卡的按整张卡认（转抄的那张卡在任何一次执行里有一个指标不给，转抄出来的就不给），宁可多挡
- 显示文字的核对（verifier.js 的 checkLayout）只看紧挨着数字头尾的字：白名单、易混字的名单只是兜底，排版和易混字的花样
  数不完（「（单位：万元）」这类隔着几个字的说明、x10 这类写法认不出）。核对页总览有一项无法核对就不是绿的，并写明去系统里
  核对封存；全部通过也只说明文件内部一致

主体是纯函数：不碰数据库，工件经 read 取原字节，数据源现在设的遮罩由调用方查好传进来（source_masks），每份查询快照
里确定取自遮罩列的列、这次运行查到过的遮罩值也一样（sure_columns、sure_secrets）。
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

from app.analysis.contract import ARTIFACT_KIND, LEDGER_KIND, NODE_TYPE as ANALYSIS_NODE, is_result
from app.core.artifact_store import canonical_json, content_hash
from app.core.events import EventType
from app.engine.evidence import analysis_recheck, iter_units, query_entry_fields
from app.engine.lineage import in_scope, scoped_id
from app.engine.masking import OPAQUE_SQL, SYSTEM_SCHEMAS, indirect_reads
from app.engine.merge_query import MERGE_SOURCE
from app.engine.recompute import (
    CopyEdge,
    Judged,
    MetricLinks,
    card_input,
    carries_masked,
    input_origin,
    masked_closure,
)
from app.engine.schema import NodeType
from app.engine.toolcalls import QUERY_PREFIX

logger = logging.getLogger(__name__)

SCHEMA = "agentlab.bundle/1"

#: 不论哪个节点都带规范字符串的事件：运行开始（实际生效的运行参数）、报告自查（报告文档的 id）、
#: 口径卡升版处置、出具判定（档位）
_SHOWN_EVENTS = frozenset({EventType.RUN_STARTED, EventType.REPORT_CHECKED, EventType.CALIBER_UPGRADE,
                           EventType.ISSUANCE})
#: node.finished 带规范字符串的节点：报告（预览就是嵌进来的那份文档的正文，文档只给哈希的不带）、口径卡（指标集的 id
#: 记在台账里；卡上有值不给的指标、有输入取自遮罩列的不带，见 _card_shown）。成果节点在运行里没有带遮罩的快照时带
#: 目录条目的种类 → 嵌进来的工件种类。运行输入、检索、表和字段的条目不嵌：前者没有工件，后几样不在核对范围里
_CITED_KINDS = {"metric": "metric_set", "query": "query", LEDGER_KIND: ARTIFACT_KIND}
#: 下钻的上一层结论最多跟几层（同分析设置里下钻层数的上限）
_PARENT_DEPTH = 6
#: 分析卡里一项做完的事件（core/events.EventType.ANALYSIS_END）：带着那一项的判定、判定语和结论工件（analysis_artifact）
_ANALYSIS_END = "analysis.end"

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
    事件（认这次运行里查到过哪些遮罩值，seen）。

    sure_columns、sure_secrets 由调用方按证据面板的那一套认好交进来（api/evidence.py 的 certain_masks）：每份查询快照里
    确定取自遮罩列的结果列，和这次运行查到过的遮罩值（只收确定的那几列）。认老文档时指标的值给不给按它们认（metric_word
    的 sure），和报告撰写节点的写作目录同一个口径；没交的（直接调用 build_bundle 的地方）按这份文件自己宁可多遮的认法。"""

    def __init__(self, read: Reader, source_masks: Mapping[str, Any],
                 events: Sequence[tuple[int, str, str | None, Any]] = (), *,
                 sure_columns: Mapping[str, Iterable[str]] | None = None, sure_secrets: Any = None) -> None:
        self._read = read
        self._source_masks = source_masks
        self._events = events
        self._sure_columns = sure_columns
        self._sure_secrets = sure_secrets
        self._raw: dict[str, bytes | None] = {}
        self._json: dict[str, Any] = {}
        self._masked: dict[str, bool] = {}
        self._hidden: dict[str, set[str]] = {}
        self._seen: Any = None
        self._graphs: dict[bool, _Graph] = {}
        self._card_outputs: dict[str, dict[str, str]] = {}
        self._docs: dict[str, bool] = {}
        self._sources: dict[str, str] | None = None
        self._runs: dict[str, list[str]] | None = None
        self._owners: dict[str, str] = {}

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
        改了名字的；SQL 认不出结果里哪一列取自哪一列的（解析不了的也算），整份的列（_derived_columns）。"""
        if artifact not in self._hidden:
            content = self.content(artifact)
            base = _snapshot_hidden(content, self._source_masks) if isinstance(content, dict) else set()
            self._hidden[artifact] = _derived_columns(content, base) if base else set()
        return self._hidden[artifact]

    def sure(self, artifact: str) -> set[str]:
        """一份读得出来的查询快照里确定取自遮罩列的列（小写），调用方交进来的（sure_columns，和报告撰写节点的写作目录同一个
        口径）。没交、或者这份不在里面的，按 hidden 宁可多遮：认不准就当它取自遮罩列。"""
        found = self._sure_columns.get(artifact) if self._sure_columns is not None else None
        return {str(c).lower() for c in found} if found is not None else self.hidden(artifact)

    def sure_seen(self) -> Any:
        """这次运行查到过的遮罩值，只收确定取自遮罩列的那几列（调用方交进来的 sure_secrets，同证据面板的 run_secrets）。
        没交 sure_columns 的按 seen。"""
        return self._sure_secrets if self._sure_columns is not None else self.seen()

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

    def analysis_masked(self, artifact: str, depth: int = 0) -> bool:
        """这份分析结论算不算带遮罩（只给 id）：结果表、判定语里的维度值取自它的输入快照。遮罩列不能当维度、度量
        （app/analysis 在调用时就挡下），可这份文件按自己宁可多挡的认法：任一份输入快照证明不了没碰遮罩列（masked），
        结论也只给 id；下钻的上一层结论同样认（最多 _PARENT_DEPTH 层）。读得出字节、却认不出是一份分析结论的，按带遮罩算
        （同口径卡）。"""
        key = f"analysis:{artifact}"
        if key not in self._masked:
            result = self.content(artifact)
            if not is_result(result):
                self._masked[key] = self.raw(artifact) is not None
                return self._masked[key]
            self._masked[key] = any(
                (self.analysis_masked(a, depth + 1) if depth < _PARENT_DEPTH else True) if role == "parent"
                else self.masked(a) for a, role in _analysis_inputs(result))
        return self._masked[key]

    def card_masked(self, artifact: str, events: Sequence[tuple[int, str, str | None, Any]]) -> bool:
        """这张口径卡算不算带遮罩：卡上有一个输入取自遮罩列，或者遮没遮核对不了。卡上记着每个输入的值（cell() 取的格、
        Agent 字段、按位置取的 vars.res.rows[0][2]）和代入式，整张卡不随文件附带——没法只拿掉几个值，拿掉了哈希就对不上。

        认哪个输入取自哪份查询结果的哪几列，和证据面板、推演同一个认法（recompute.input_origin）；按位置取的一格顺着
        取值链从写变量的节点在口径卡算这一次之前的产出起走。那几列算不算遮罩列，除了列名对上的，SQL 里给遮罩列起了别名、
        套了函数、经子查询或 WITH 转了一道的也算（hidden，同质询、双盲复算的认法）；SQL 认不出结果里哪一列取自哪一列的
        （解析不了、列名表、整行引用这类，快照本身因此只给哈希），整份快照的列都算，两条路的判断一致。核对不了的宁可多挡：
        快照读不出来、取值链上的产出读不出来、那份结果没存快照只带着数据源名、定位里没写列。认得出、可结果既没带快照的 id
        也没带数据源名的（数据整形改过形状）认不出出处，不算。

        卡本身按解析出来的内容认，不看哈希（同 content）：服务端的文件被改过，嵌进页面的照样是这份原字节，卡上记着的号码
        也照样在里面。读得出字节、解析不出一张卡的（截断的 JSON、不是对象）认不出有哪些输入，整张按带遮罩算，和读不出来的
        快照按带遮罩算（_snapshot_masked）是同一个道理。字节都读不出来的卡本来就不嵌（只给 id、标缺失），这里不管。"""
        key = f"card:{artifact}"
        if key not in self._masked:
            card = self.content(artifact)
            if not isinstance(card, dict):
                self._masked[key] = self.raw(artifact) is not None
                return self._masked[key]
            output = self._outputs_of(artifact, events)
            metrics = [metric for metric in card.get("metrics") or [] if isinstance(metric, dict)]
            items = [item for metric in metrics for item in metric.get("inputs") or [] if isinstance(item, dict)]
            # 另按指标认：值不给的指标（metric_word，遮罩列宁可多遮的那一套）——按位置取的整行、下标不是字面量的一格，整份
            # 结果里带着遮罩列、值里就是号码的，转抄上游口径卡里这类指标的，上面只认字面量下标一格的那一套认不出，卡上记着的
            # 值照样是号码。转抄来的输入另看一遍（_copies_masked）：卡上记着转抄来的值本身，指标的值照给时它也可能是号码
            self._masked[key] = any(self._input_masked(item, output) for item in items) \
                or any(self.metric_word(artifact, metric, events) is not None or self._copies_masked(artifact, metric)
                       for metric in metrics)
        return self._masked[key]

    def _copies_masked(self, card: str, metric: dict[str, Any]) -> bool:
        """卡上这个指标转抄来的输入带不带遮罩（metric_word 认过之后才看得到）：转抄的那张卡有指标不给（哪种写法都一样，
        recompute.CopyEdge.card）、读到的上游指标核对不了，卡上记着的就是转抄来的值（嵌在下标里、指标的值照给时也一样）；
        整张卡、清单文字转抄、读到的产出里有指标核对不了的（table_unverified），记着的值（清单文字逐条写着「指标（名称）
        = 值」）里有这次运行查到过的遮罩值才算，记成「[3 项]」这种大概的不算。"""
        judged = self._graph(False).judged.get(_metric_key(card, metric, self.content(card)))
        if judged is None:
            return False
        values = {str(i.get("path")): i.get("value") for i in metric.get("inputs") or [] if isinstance(i, dict)}
        for path, (kind, _, _) in judged.copied.items():
            if kind in ("masked", "unverified"):
                return True
            if kind is not None and carries_masked(values.get(path), self.seen()):
                return True
        return False

    def metric_word(self, card: str, metric: dict[str, Any], events: Sequence[tuple[int, str, str | None, Any]], *,
                    sure: bool = False) -> str | None:
        """口径卡 card 上一个指标的值给不给：None 照给；"masked" 不给（值取自遮罩列的一格、一行，或者整份结果里带着遮罩列、
        值里就有这次运行查到过的遮罩值）；"unverified" 遮没遮核对不了。和证据面板指标步骤的值同一个判断（api/evidence.py
        的 _metric_masks，by_value=False：输入的出处按 recompute.input_origin 的 wide 认，转抄上游口径卡的按那张卡在依赖图上
        认，recompute.masked_closure，最后交给 recompute.withheld_value）。只按出处认：认不出出处的输入（写死的
        目标、数据整形算出来的数）值碰巧等于遮罩列里的某一格，不因此算——不然普通报告里的这种指标在文件里也核对不了，
        老文档也整份只给哈希。

        遮罩列两种认法：
        - 默认（认口径卡能不能随文件附带，card_masked）：按这份文件自己的认法宁可多遮（hidden：SQL 认不准时整份的列都算；
          快照读不出来、和 id 对不上的，按它的数据源现在设的遮罩），遮罩值用 seen。卡上记着输入的值，附带了就带出去了
        - sure（认老文档，doc_withheld）：只按确定取自遮罩列的认（sure，调用方按写作目录的口径认好交进来），遮罩值用
          sure_seen。和报告撰写节点写作时认的一样：现在的报告撰写节点照给的普通指标，老文档里写着它也照常附带

        依赖图按 (指标集工件, 指标在卡上的位置) 认指标，两种认法各一张、构建里认过的记下来：卡转抄卡、循环里每一次执行都
        转抄上一次的产出时，每个指标只认一次，和追了几层无关。转抄的保守规则按整张卡认（recompute.CopyEdge.card）：转抄的
        那张卡在封存范围内的任何一次执行里有一个指标不给，转抄出来的就不给，同写作目录。"""
        key = _metric_key(card, metric, self.content(card))
        graph = self._graph(sure)
        if key not in graph.judged:
            fresh = self._build(graph, key, card, metric, events, sure)
            graph.judged.update(masked_closure({k: graph.links[k] for k in fresh}, graph.judged,
                                               self.sure_seen if sure else self.seen))
        return graph.judged[key].word

    def _graph(self, sure: bool) -> "_Graph":
        if sure not in self._graphs:
            self._graphs[sure] = _Graph()
        return self._graphs[sure]

    def _build(self, graph: "_Graph", key: Any, card: str, metric: dict[str, Any],
               events: Sequence[tuple[int, str, str | None, Any]], sure: bool) -> list[Any]:
        """认好 key 和顺着转抄的边够得着、还没认过的指标（转抄以外的输入），交回这一批新认的键，同 api/evidence.py 的
        _CopyGraph._build。转抄一张卡时它在封存范围内每一次执行的每个指标都放进来（_executions）；上游的指标按上游那张卡
        那一次执行之前的产出认（_card_outputs）；卡上没记输入的指标不放。"""
        fresh: list[Any] = []
        todo = [(key, card, metric, self._owner(card))]
        while todo:
            key, art, item_metric, owner = todo.pop()
            if key in graph.links:
                continue
            output = self._outputs_of(art, events)
            links = MetricLinks(item_metric, card=owner)
            for item in item_metric.get("inputs") or []:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path"))
                copied = card_input(item, output)
                if copied is not None:
                    upstream: list[Any] = []
                    # 转抄的是哪张卡：按读到的产出上记着的指标集认（经数据整形赋给变量的也认得出），认不出的按取值链上的节点
                    # 取值链上写的是卡所在那一层图里的 id（engine/lineage.scoped_id）
                    owner = scoped_id(self._owner(art), copied.node_id)
                    if copied.card is not None:
                        up_card = str(copied.card.get("artifact") or "")
                        owner = self._owner(up_card) or owner
                        for index in copied.picked:
                            up_metric = copied.card["metrics"][index]
                            if isinstance(up_metric, dict) and up_metric.get("inputs"):
                                up = (up_card or ("node", copied.node_id, id(copied.card)), index)
                                upstream.append(up)
                                todo.append((up, up_card, up_metric, owner))
                    for run_art in self._executions(owner):
                        content = self.content(run_art)
                        metrics = content.get("metrics") if isinstance(content, dict) else None
                        for index, up_metric in enumerate(metrics if isinstance(metrics, list) else []):
                            if isinstance(up_metric, dict) and up_metric.get("inputs"):
                                todo.append(((run_art, index), run_art, up_metric, owner))
                    links.copies.append(CopyEdge(path, tuple(upstream), copied.whole, copied.unreadable, card=owner))
                    continue
                kind = self._input_kind(item, output, sure=sure)
                if kind is not None:
                    links.direct[path] = (kind, ())
            graph.links[key] = links
            fresh.append(key)
        return fresh

    def _executions(self, node_id: str) -> list[str]:
        """口径卡 node_id 在封存范围内每一次执行的指标集工件（node.finished 台账里的 metric_set 条目），按先后。转抄的保守
        规则按整张卡、每一次执行认（recompute.CopyEdge.card）。按解析出来的内容认、不看哈希（同 card_masked）：被改过的文件
        只会多认出不给的指标；读到的那一次产出读不出来另有 CardInput.unreadable 兜着。"""
        self._index_runs()
        return (self._runs or {}).get(str(node_id or ""), [])

    def _owner(self, card: str) -> str | None:
        """指标集工件 card 是哪张口径卡（节点 id）的产出，按封存范围内的台账认；认不出的是 None。"""
        self._index_runs()
        return self._owners.get(card)

    def _index_runs(self) -> None:
        if self._runs is not None:
            return
        runs: dict[str, list[str]] = {}
        for _, etype, node_id, data in self._events:
            if etype != EventType.NODE_FINISHED or not node_id or not isinstance(data, dict):
                continue
            for entry in data.get("evidence") or []:
                if isinstance(entry, dict) and entry.get("kind") == "metric_set" \
                        and isinstance(entry.get("artifact"), str) and entry["artifact"]:
                    runs.setdefault(str(node_id), []).append(entry["artifact"])
                    self._owners.setdefault(entry["artifact"], str(node_id))
        self._runs = {node_id: list(dict.fromkeys(found)) for node_id, found in runs.items()}

    def _outputs_of(self, card: str, events: Sequence[tuple[int, str, str | None, Any]]) -> Callable[[Any], Any]:
        """口径卡 card 那一次执行之前每个节点的产出（_card_outputs）：节点 id → 读出来的内容，读不出来、和 id 对不上的是
        None。同一张卡只找一次。

        卡上的取值链按卡所在那一层图里的 id 写（engine/lineage.in_scope）：子工作流里的卡（台账记在「子工作流节点 id/卡」
        上）只看同一张子图里的节点，按去掉前缀的 id 查，和证据面板 api/evidence._InputMasks._output 同一个认法。以前按这次
        运行里的 id 查，子图里的卡转抄、按位置取的格一律查不到、当成核对不了整张遮住；父图里有同名的节点时还拿它当依据。"""
        if card not in self._card_outputs:
            self._card_outputs[card] = in_scope(_card_outputs(events, card), self._owner(card))
        outputs = self._card_outputs[card]

        def output(node_id: Any) -> Any:
            found = outputs.get(str(node_id or ""))
            return self.content(found) if found and self.intact(found) else None

        return output

    def _input_kind(self, item: dict[str, Any], output: Callable[[Any], Any], *, sure: bool = False) -> str | None:
        """一个输入（转抄上游口径卡的除外）不给值的原因，同 api/evidence.py 的 _InputMasks.check：取自遮罩列的一格、一行是
        "masked"，整份结果是 "table"（它不一定把哪一格带进指标，指标的值另按值认），遮没遮核对不了的是 "unverified"
        （取值链上的产出读不出来、快照读不出来、结果没存快照只带着数据源名），给值的、认不出出处的（常量、运行输入、数据
        整形算出来的一格）是 None。"""
        origin = input_origin(item, output, wide=True)
        if origin is None:
            return None
        if origin.unverified is not None:
            return "unverified"
        used = list(dict.fromkeys(origin.columns))
        if not used:
            return None
        readable = bool(origin.artifact) and self.intact(str(origin.artifact)) \
            and isinstance(self.content(str(origin.artifact)), dict)
        if readable:
            hidden = self.sure(str(origin.artifact)) if sure else self.hidden(str(origin.artifact))
        else:
            # 读不出来、和 id 对不上的快照：查询当时记下的遮罩在快照里，只能按它的数据源现在设的认
            source = origin.source or (self.query_source(origin.artifact) if origin.artifact else None)
            now = self._source_masks.get(str(source or ""))
            hidden = {str(c).lower() for c in now} if isinstance(now, (list, tuple, set, frozenset)) else set()
        if any(c.lower() in hidden for c in used):
            return "table" if origin.table else "masked"
        if (origin.artifact and not readable) or (not origin.artifact and origin.source):
            return "table" if origin.table else "unverified"
        return None

    def query_source(self, artifact: str) -> str | None:
        """一份查询快照的数据源名，按封存范围内的事件认（_query_sources）：快照读不出来时认它的数据源现在设的遮罩。"""
        if self._sources is None:
            self._sources = _query_sources(self._events)
        return self._sources.get(artifact)

    def doc_withheld(self, doc_id: str) -> bool:
        """这份报告文档的证据目录里还带着值不给的指标的原值：升级前的报告撰写节点生成的老文档（或者生成之后数据源才设的
        遮罩），目录收着上游口径卡的全部指标（没被引用的也收），值、渲染、标签原样写着，正文引用了的话正文里也是。整份只给
        哈希、不嵌入，它的正文也不随节点完成事件带出去。现在的报告撰写节点把这些指标写成「已遮罩」、记 masked
        （evidence.withhold_metrics），不算。

        哪些指标的值不给按 metric_word 的 sure 认，只算「不给」（"masked"）的，和写作时报告撰写节点认的一样：SQL 认不准、
        整份遮的快照里没确定取自遮罩列的普通金额，现在的报告撰写节点照给，老文档里写着它也照常附带。遮没遮核对不了的、
        目录里的口径卡读不出来或找不到这个指标的，按值认：值就是这次运行查到过的遮罩值才算（sure_seen）。按解析出来的内容认，
        不看哈希（同 card_masked）：服务端的文件被改过，嵌进页面的照样是这份原字节。"""
        if doc_id not in self._docs:
            self._docs[doc_id] = bool(self.withheld_aliases(self.content(doc_id)))
        return self._docs[doc_id]

    def withheld_aliases(self, doc: Any) -> frozenset[str]:
        """这份文档的证据目录里还带着原值、值却不给的那几个指标（目录里的别名），认法见 doc_withheld。"""
        catalog = doc.get("catalog") if isinstance(doc, dict) else None
        if not isinstance(catalog, dict):
            return frozenset()
        return frozenset(str(alias) for alias, entry in catalog.items()
                         if isinstance(entry, dict) and entry.get("kind") == "metric" and not entry.get("masked")
                         and entry.get("value") is not None and self._carries_withheld(entry))

    def _carries_withheld(self, entry: dict[str, Any]) -> bool:
        artifact = entry.get("artifact")
        card = self.content(artifact) if isinstance(artifact, str) and artifact else None
        wanted = (entry.get("locator") or {}).get("metric") if isinstance(entry.get("locator"), dict) else None
        metric = next((m for m in card.get("metrics") or [] if isinstance(m, dict) and m.get("id") == wanted), None) \
            if isinstance(card, dict) else None
        word = self.metric_word(str(artifact), metric, self._events, sure=True) if metric is not None else "unverified"
        return word == "masked" or (word == "unverified" and carries_masked(entry.get("value"), self.sure_seen()))

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


class _Graph:
    """一种认法下这次构建认过的指标依赖图（_Store.metric_word）：键 → 转抄以外的输入和转抄的边（MetricLinks）、认出来的
    结论（Judged）。"""

    def __init__(self) -> None:
        self.links: dict[Any, MetricLinks] = {}
        self.judged: dict[Any, Judged] = {}


def _metric_key(card: str, metric: dict[str, Any], content: Any) -> Any:
    """一个指标在依赖图上的键：(指标集工件, 指标在卡上的位置)，转抄的边按同样的键指到上游；不是这张卡上的那个指标对象的
    （调用方另取来的）按对象本身认，不和别的指标混在一起，同 api/evidence.py 的 _metric_key。"""
    metrics = content.get("metrics") if isinstance(content, dict) and isinstance(content.get("metrics"), list) else []
    index = next((i for i, m in enumerate(metrics) if m is metric), None)
    return (card, index) if index is not None and card else ("metric", id(metric))


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
#: 「已遮罩」、另记下了遮了哪些列）不知道它记着什么，照现有规则只给 id。no_data 是查询区间里这张表没有数据的那几列
#: （data/coverage.py）：只有日期和表名、列名，遮罩列上的不探查也不记
_QUERY_KEYS = frozenset({"columns", "rows", "row_count", "truncated", "elapsed_ms", "sql", "column_types", "source",
                         "checks", "schema_artifact", "mask_columns", "data_version", "no_data"})
#: 导出时只知道数据源设的遮罩、不知道是哪种库：SQL 按每种支持的库的读法各解析一遍（None 是 sqlglot 的通用读法），
#: 至少一种读得通，读得通的每一种都要过关
_READINGS = (None, "sqlite", "mysql", "postgres", "oracle")
#: 语法树和数据库实际执行的对不上的写法（MySQL 会执行的注释、Postgres 的 U&"…"），不点列名也读得出表里内容的写法（表名、
#: SQL 写在字符串里交给函数，按页按文件读的原始数据，统计信息、会话记录里存着值的）：和核查 agent 的工具边界共用一份名单
#: （engine/masking.py 的 OPAQUE_SQL、indirect_reads），这里另把系统 schema 整个算上（只决定附不附快照，宁可多挡）。SQL
#: 原文里出现就按碰了遮罩列算，写在字符串里的也算
_OPAQUE_SQL = OPAQUE_SQL
_INDIRECT_READS = indirect_reads(*SYSTEM_SCHEMAS)
#: Postgres 的函数记法（m.to_jsonb 就是 to_jsonb(m)）下能把一整行交出来的内置函数（_whole_row）：只收一个参数、收得下
#: 整行（record、anyelement、"any" 这类），交回的值里带着整行。汇总函数也能这么写：any_value、*_agg 交回整行，max、min
#: 交回最大、最小的那一整行。照 PG 18 的 pg_proc 逐个对过：只交回个数、大小、类型、哈希的不算，这么写会报错的（伪类型的
#: 输出函数、要成对参数的 json_build_object、要 WITHIN GROUP 的 mode）也不算；扩展里的只认 hstore，自己建的函数这里不知道
_ROW_FUNCTIONS = frozenset({"to_json", "to_jsonb", "row_to_json", "hstore", "quote_literal", "quote_nullable",
                            "concat", "json_build_array", "jsonb_build_array", "record_out", "record_send",
                            "array_agg", "json_agg", "jsonb_agg", "json_agg_strict", "jsonb_agg_strict", "any_value",
                            "max", "min"})


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
    转一道得来的（inquiry._derived_hidden，质询、双盲复算同一个认法）。SQL 按每种读得通的读法各认一遍、合起来。认不出
    结果里哪一列取自哪一列的，整份的列都算：每种读法都解析不了、没有 SQL 的，语法树认不准、不点列名也读得出表里内容的
    写法（_OPAQUE_SQL、_INDIRECT_READS），以及哪一种读法下有列名表、整行引用、不照原名展开的 *、吃进遮罩列的表函数的
    （_renamed）——快照本身因此只给哈希，口径卡上取自它的值也一样。解析不了的不退回那一套：它解析不了时只看 SQL 里写没
    写着遮罩列，没写就只认遮罩列本身，卡上取自整行的值认不出。合并查询照那一套的办法不看 SQL（它记着自己的来历）。
    出错时整份的列都算（宁可多挡）。"""
    columns = snapshot.get("columns")
    every = set(base) | ({str(c).lower() for c in columns} if isinstance(columns, list) else set())
    try:
        from app.data.sql_semantics import _parse, _read
        from app.engine.inquiry import _derived_hidden

        if snapshot.get("source") == MERGE_SOURCE:
            return set(base) | {c.lower() for c in _derived_hidden(dict(snapshot), set(base), None)[0]}
        sql = snapshot.get("sql")
        if not isinstance(sql, str) or _OPAQUE_SQL.search(sql) or _INDIRECT_READS.search(sql):
            return every
        readings = [(kind, tree) for kind in _READINGS for tree in [_parse(sql, _read(kind))[0]] if tree is not None]
        if not readings:
            return every
        out = set(base)
        for kind, tree in readings:
            found, names = _derived_hidden(dict(snapshot), set(base), kind)
            if _renamed(tree, {n.lower() for n in names}):
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
    的名字（别名、表名、WITH 里的一段）就算。名字写在 FROM、JOIN 里的表函数的参数里时，只有排在这个表函数前面的派生
    来源算数：它看不见后面的来源，(SELECT 1 AS m) s 排在后面，m 还是整行（_holder）。写了表名的，列名是 _ROW_FUNCTIONS
    里的、表名对上看得见的来源才算，同样先按列认：那个来源是明写出了这一列的派生来源，t.max 就是那一列，不是 max(t)。
    看得见的是所在 SELECT 和外层 SELECT 的 FROM、JOIN 里的来源。FROM、JOIN 里的表函数（UNNEST、ROWS FROM 这类）和
    LATERAL 看得见同一层的来源：Postgres 的函数不写 LATERAL 也看得见（逗号、CROSS JOIN、JOIN … ON TRUE 都一样），整行
    交给 jsonb_each_text 这类就一列列竖着摊开了；括号括起来的一组连接按同一层算（_sources 摊开）。FROM、JOIN 里的子查询
    不写 LATERAL 看不见，WITH 里的一段也看不见。数据源里的表有哪些列这里不知道：碰巧和表、别名同名的列，碰巧叫 max、min
    这类名字的列也算（宁可多挡）。"""
    from sqlglot import exp

    select = column.find_ancestor(exp.Select)
    if isinstance(column.this, exp.Star) or not column.name or select is None:
        return False
    if column.table:
        if column.name.lower() not in _ROW_FUNCTIONS:
            return False
        name, field = column.table.lower(), column.name.lower()
    else:
        name, field = column.name.lower(), None
        sources = _sources(select)
        if any(_names_column(_derived(source), name) for source in sources[:_holder(column, select, sources)]):
            return False
    child, node = column, column.parent
    while node is not None:
        # FROM、JOIN 里真的子查询（括号里是一条查询，不是一组连接）看不见同一层，WITH 里的一段也看不见
        subquery = isinstance(child.this, exp.Subquery) and _derived(child.this) is not None
        sees = child.arg_key != "with_" and not (child.arg_key in ("from_", "joins") and subquery)
        found = [source for source in _sources(node) if (source.alias_or_name or "").lower() == name] \
            if isinstance(node, exp.Select) and sees else []
        if found:
            return field is None or not any(_names_column(_derived(source), field) for source in found)
        child, node = node, node.parent
    return False


def _holder(column: Any, select: Any, sources: list[Any]) -> int | None:
    """名字写在所在 SELECT 的哪一个表函数（_table_function）的参数里：交回它在 sources 里的位置；写在选出项、条件、
    ON 里的是 None（同一层的来源都看得见）。"""
    places = {id(source): place for place, source in enumerate(sources) if _table_function(source)}
    node = column.parent
    while node is not None and node is not select:
        if id(node) in places:
            return places[id(node)]
        node = node.parent
    return None


def _names_column(block: Any, name: str) -> bool:
    """一个派生来源（_derived 交回的那块查询，None 是数据源里的表这类）明写出了叫这个名字的列：第一支的选出项里起了
    这个别名、或者直接写着这一列（* 不算，展开出哪些列这里看不出）。"""
    from sqlglot import exp

    first = _leaves(block)[0] if block is not None else None
    return isinstance(first, exp.Select) and any(
        isinstance(item, (exp.Alias, exp.Column)) and not isinstance(item.this, exp.Star)
        and item.alias_or_name.lower() == name for item in first.expressions)


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


def _query_sources(events: Iterable[tuple[int, str, str | None, Any]]) -> dict[str, str]:
    """封存范围内每份查询快照的数据源名：节点台账 query 条目记下的 source，数据源查询工具（db_query__<数据源>）交回的
    快照按工具名认。快照读不出来时按它认数据源现在设的遮罩（同证据面板按台账认的 sealed.queries）。"""
    out: dict[str, str] = {}
    for _, etype, _, data in events:
        if not isinstance(data, dict):
            continue
        if etype == EventType.TOOL_END and isinstance(data.get("query_artifact"), str):
            tool = str(data.get("tool") or "")
            if tool.startswith(QUERY_PREFIX) and tool[len(QUERY_PREFIX):]:
                out.setdefault(data["query_artifact"], tool[len(QUERY_PREFIX):])
        elif etype == EventType.NODE_FINISHED:
            for entry in data.get("evidence") or []:
                if isinstance(entry, dict) and entry.get("kind") == "query" and entry.get("artifact") \
                        and isinstance(entry.get("source"), str) and entry["source"]:
                    out[str(entry["artifact"])] = entry["source"]
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
    """报告正文引用到的口径卡、查询快照和分析结论：(工件 id, 种类, 来源节点)，按正文顺序、去重。

    看每个解析成功的引用（行内的数字、单元格、判定语，句末的 [[see:]]）在文档目录里指向的条目：指标条目指向口径卡，
    查询条目指向快照，分析结论条目指向结论工件。整表在组装时已经展开成每格一个单元格引用，不用另看。报告文档里系统写的
    「分析范围」点名的结论也算（页面上点得开它们）。"""
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
    scope = doc.get("analysis_scope") if isinstance(doc.get("analysis_scope"), dict) else {}
    for group in ("phenomena", "decomposed", "excluded", "refused"):
        for item in scope.get(group) or []:
            entry = catalog.get(item.get("alias")) if isinstance(item, dict) else None
            if isinstance(entry, dict) and entry.get("kind") == LEDGER_KIND and isinstance(entry.get("artifact"), str) \
                    and entry["artifact"]:
                owner = entry.get("node_id")
                out.append((entry["artifact"], ARTIFACT_KIND, str(owner) if owner else None))
    return list(dict.fromkeys(out))


def _analysis_inputs(result: Any) -> list[tuple[str, str]]:
    """一份分析结论记着的输入：(工件 id, 角色)，按先后、按工件去重。角色是 parent 的是下钻的上一层结论，别的是查询快照。"""
    out: dict[str, str] = {}
    for item in (result.get("inputs") or []) if isinstance(result, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("artifact"), str) and item["artifact"]:
            out.setdefault(item["artifact"], "parent" if item.get("role") == "parent" else "data")
    return list(out.items())


def _card_comparability(card: Any) -> list[str]:
    """口径卡指标上记着的可比性结论（contract.MetricComparability 的 artifact），按先后、去重。这份结论不进台账，只从卡上
    引用：卡随文件附带时一并附上，引用链（④）按「已核对的口径卡记着的可比性」认它。"""
    out: list[str] = []
    for metric in (card.get("metrics") or []) if isinstance(card, dict) else []:
        found = metric.get("comparability") if isinstance(metric, dict) else None
        if isinstance(found, dict) and isinstance(found.get("artifact"), str) and found["artifact"]:
            out.append(found["artifact"])
    return list(dict.fromkeys(out))


def _card_input_queries(card: Any) -> list[tuple[str, str | None]]:
    """口径卡记着的输入引用的查询快照：(工件 id, 输入来自的节点)，按先后、按工件去重。

    只认 cell() 取的格、Agent 交来的字段这两类带着定位的输入（nodes.*、input.* 的取值不走查询快照）。
    卡是调用方确认过随文件附带的那份（遮罩的、文件不在的不进来）：出处面板据此展示每个输入的 SQL 和被引用的行。"""
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    if not isinstance(card, dict):
        return out
    for metric in card.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        for item in metric.get("inputs") or []:
            if not isinstance(item, dict) or item.get("via") not in ("tool_cell", "agent_field"):
                continue
            artifact = item.get("artifact")
            if not isinstance(artifact, str) or not artifact or artifact in seen:
                continue
            seen.add(artifact)
            owner = item.get("node_id")
            out.append((artifact, str(owner) if owner else None))
    return out


def withheld_metric_aliases(doc: Any, *, read: Reader, events: Sequence[tuple[int, str, str | None, Any]] = (),
                            source_masks: Mapping[str, Any] | None = None,
                            sure_columns: Mapping[str, Iterable[str]] | None = None,
                            sure_secrets: Any = None) -> frozenset[str]:
    """报告文档的证据目录里还带着原值、值却不该给的指标（目录里的别名）：可验证报告据此把老文档整份只给哈希
    （_Store.doc_withheld），报告导出成 HTML（api/evidence.export_report_html）据此把引用它们的片段写成「已遮罩」——两样
    都要拿给系统外的人，同一个判断。参数同 build_bundle：events 是封存范围内的事件，sure_columns、sure_secrets 由调用方按
    api/evidence.py 的 certain_masks 认好交进来。"""
    store = _Store(read, source_masks or {}, events, sure_columns=sure_columns, sure_secrets=sure_secrets)
    return store.withheld_aliases(doc)


class _Artifacts:
    """嵌进页面的工件清单，按先后：报告文档在前，接着是它引用到的。同一份只列一次。events 是封存范围内的事件
    （认口径卡带不带遮罩时找写变量的节点当时的产出）。"""

    def __init__(self, store: _Store, events: Sequence[tuple[int, str, str | None, Any]] = ()) -> None:
        self.store = store
        self.events = events
        self.items: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._seen: set[str] = set()

    def add(self, artifact: str, kind: str, node_id: str | None, *, masked: bool = False) -> dict[str, Any] | None:
        """列进清单的一份工件，交回它的条目（同一份只列一次，第二次交回第一次的）。masked：调用方另认出不能附带的
        （报告文档的证据目录里带着值不给的指标的原值；报告文档里写成「已遮罩」的指标所在的口径卡），只给 id。"""
        if artifact in self._seen:
            return self._by_id[artifact]
        self._seen.add(artifact)
        item: dict[str, Any] = {"id": artifact, "kind": kind}
        if node_id:
            item["node_id"] = node_id
        raw = self.store.raw(artifact)
        if raw is None:
            # 服务端的文件不在了：只给 id，浏览器照实说核对不了
            item["missing"] = True
        elif masked or (kind == "query" and self.store.masked(artifact)) \
                or (kind == "metric_set" and self.store.card_masked(artifact, self.events)) \
                or (kind == ARTIFACT_KIND and self.store.analysis_masked(artifact)):
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
        self._by_id[artifact] = item
        return item


def _attach_analysis(artifacts: _Artifacts, store: _Store, item: dict[str, Any] | None, artifact: str,
                     owner: str | None, cited_queries: list[str], depth: int = 0) -> None:
    """随文件附带的分析结论：它的输入快照（种类 query，遮罩照常认）、下钻的上一层结论（同样附带它的输入，最多
    _PARENT_DEPTH 层）一并列进清单，再把服务端的复算结论记在这份结论的条目上（recheck）。只给 id 的结论、被改过的
    （哈希对不上）不顺着它去附带别的：被改过的结论说输入是哪几份，不能替它圆。"""
    if item is None or "b64" not in item or not store.intact(artifact) or "recheck" in item:
        return
    result = store.content(artifact)
    if not is_result(result):
        return
    for input_id, role in _analysis_inputs(result):
        if role == "parent":
            if depth < _PARENT_DEPTH:
                parent = artifacts.add(input_id, ARTIFACT_KIND, owner)
                _attach_analysis(artifacts, store, parent, input_id, owner, cited_queries, depth + 1)
            continue
        artifacts.add(input_id, "query", owner)
        cited_queries.append(input_id)
    item["recheck"] = _recheck(result, store)


def _recheck(result: dict[str, Any], store: _Store) -> dict[str, Any]:
    """导出时服务端按封存的输入把这份分析结论复算一遍（engine/evidence.analysis_recheck，和证据面板同一个函数）：输入快照、
    上一层结论只认这份文件里哈希对得上的原字节。页面照这个结果写「导出时服务端的复算」，不在浏览器里重算统计。"""
    def load(artifact: str) -> Any:
        return store.content(artifact) if store.intact(artifact) else None

    def load_result(artifact: str) -> Any:
        content = store.content(artifact) if store.intact(artifact) else None
        return content if is_result(content) else None

    return analysis_recheck(result, load, load_result=load_result)


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------


def _shows_content(etype: str, node_id: str | None, data: Any, node_types: Mapping[str, str], store: _Store,
                   masked_run: bool, events: Sequence[tuple[int, str, str | None, Any]] = (),
                   withheld: frozenset[str] = frozenset(), forced: frozenset[str] = frozenset()) -> bool:
    """这条封存范围内的事件带不带规范字符串（白名单）。events 是封存范围内的事件（认口径卡带不带遮罩时用），withheld
    是最后一份文档只给哈希的报告节点（_Store.doc_withheld），forced 是报告文档里写成「已遮罩」的指标所在的口径卡
    （_masked_cards）。"""
    if etype in _SHOWN_EVENTS:
        return True
    if etype == EventType.RUN_FINISHED:
        # 运行结束事件里是成果：字段可以直接引用查询结果，运行里有带遮罩的快照时只给哈希
        return not masked_run
    if etype == EventType.NODE_FINISHED:
        kind = node_types.get(node_id or "")
        if kind == NodeType.METRICS:
            return _card_shown(data, store, masked_run, events, forced)
        if kind == ANALYSIS_NODE:
            return _analysis_shown(data, store)
        if kind == NodeType.REPORT:
            # 预览就是那份文档正文的前一截：文档只给哈希的，这条也只给哈希
            preview = data.get("preview") if isinstance(data, dict) else None
            doc = preview.get("doc_artifact") if isinstance(preview, dict) else None
            return not (store.doc_withheld(doc) if isinstance(doc, str) and doc else (node_id or "") in withheld)
        return kind == NodeType.OUTPUT and not masked_run
    if etype == EventType.TOOL_END:
        # 查询类：把查询快照的 id 和运行记录连起来。预览里是查询结果的前一截，数据源带遮罩的只给哈希
        artifact = data.get("query_artifact") if isinstance(data, dict) else None
        if isinstance(artifact, str) and artifact:
            return not store.masked(artifact)
        # 分析工具：预览里是结论的前一截（判定语、具名的数、结果表），和结论本身同一个认法
        found = data.get("analysis_artifact") if isinstance(data, dict) else None
        return isinstance(found, str) and bool(found) and _result_shown(found, store)
    if etype == _ANALYSIS_END:
        # 分析卡的一项：带着判定、判定语，结论随文件附带得了（输入没碰遮罩列）才带
        found = data.get("analysis_artifact") if isinstance(data, dict) else None
        return isinstance(found, str) and bool(found) and _result_shown(found, store)
    return False


def _result_shown(artifact: str, store: _Store) -> bool:
    """一份分析结论的内容能不能出现在页面上（事件里带着它的判定语、预览）：文件在、认得出是分析结论、输入都没碰遮罩列
    （_Store.analysis_masked）。和这份结论随文件附带时同一个认法。"""
    return store.raw(artifact) is not None and not store.analysis_masked(artifact)


def _card_shown(data: Any, store: _Store, masked_run: bool, events: Sequence[tuple[int, str, str | None, Any]],
                forced: frozenset[str] = frozenset()) -> bool:
    """口径卡的节点完成事件带不带规范字符串。预览里逐条写着「指标（名称）= 值」（metrics 节点的 text），赋了变量的另有
    变量的预览：卡上有值不给的指标、有输入取自遮罩列（card_masked，卡本身因此只给 id）的，这条也只给哈希——和报告引不
    引用无关，没有报告的运行照样导出。指标集的 id 记在台账里（evidence 的 metric_set），升级前的运行没有台账，认预览里
    记的 artifact，再没有就认节点产出工件（内容就是这张卡加上 text）。一张都读不出来的不知道带没带遮罩：运行里有带遮罩
    的快照时只给哈希（同成果节点）。报告文档里写成「已遮罩」的指标所在的卡（forced）同样只给哈希：卡本身因此只给 id。
    卡被报告引用时，报告文档的证据目录照样记着它的 id，引用链不靠这条事件。"""
    if not isinstance(data, dict):
        return not masked_run
    cards = [str(e["artifact"]) for e in data.get("evidence") or []
             if isinstance(e, dict) and e.get("kind") == "metric_set" and isinstance(e.get("artifact"), str)
             and e["artifact"]]
    preview = data.get("preview")
    if not cards and isinstance(preview, dict) and isinstance(preview.get("artifact"), str) and preview["artifact"]:
        cards = [preview["artifact"]]
    if not cards and isinstance(data.get("artifact"), str) and data["artifact"]:
        cards = [data["artifact"]]
    if any(card in forced for card in cards):
        return False
    known = [card for card in dict.fromkeys(cards) if store.raw(card) is not None]
    if not known:
        return not masked_run
    return not any(store.card_masked(card, events) for card in known)


def _analysis_shown(data: Any, store: _Store) -> bool:
    """分析卡的节点完成事件带不带规范字符串。产出是每一项的判定、具名的数、判定语（results），台账里记着每份结论的 id：
    这几份结论都随文件附带（输入没碰遮罩列、文件在）才带，有一份只给 id、读不出来，或者一份都没记的，这条只给哈希——
    和结论本身同一个认法（_Store.analysis_masked），预览里的维度值不比结论里的多。"""
    if not isinstance(data, dict):
        return False
    results = [str(e["artifact"]) for e in data.get("evidence") or []
               if isinstance(e, dict) and e.get("kind") == LEDGER_KIND and isinstance(e.get("artifact"), str)
               and e["artifact"]]
    return bool(results) and all(_result_shown(a, store) for a in results)


def _masked_cards(doc: dict[str, Any]) -> set[str]:
    """报告文档的证据目录里写成「已遮罩」的指标（withhold_metrics 记了 masked）所在的口径卡。"""
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    return {str(e["artifact"]) for e in catalog.values() if isinstance(e, dict) and e.get("kind") == "metric"
            and e.get("masked") and isinstance(e.get("artifact"), str) and e["artifact"]}


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
    if etype == EventType.LEDGER_MATCHED:
        # 口径账本的比对（next-round 02）：只说各种状态几条，不带绑定和差异
        counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
        parts = [f"{counts[k]} 条{label}" for k, label in (("proven", "等价已证明"), ("related", "有相关口径"),
                                                          ("none", "没有相关口径"), ("skipped", "没有比完"))
                 if isinstance(counts.get(k), int) and counts[k]]
        return "封存后比对已发布口径" + (f"：{'、'.join(parts)}" if parts else "")
    if etype == EventType.LEDGER_RECALLED:
        # 口径召回（next-round 02 做法 7）：只说按哪一版核对、数变没变，不带新旧值和差值
        to = data.get("to_version")
        head = f"已按 v{to} 核对" if isinstance(to, int) else "口径召回核对"
        changed = data.get("changed")
        return head + ("：数变了" if changed is True else "，不影响" if changed is False else "")
    if etype == EventType.LEDGER_BASIS_CHANGED:
        # 答案依据的目录事实、表结构变了：只说哪一类、几项，不带表名、列名、取值
        kind = {"catalog": "目录事实", "structure": "表结构", "values": "取值", "catalog_edit": "目录"}.get(
            str(data.get("kind") or ""), "")
        items = data.get("items")
        count = len(items) if isinstance(items, list) else 0
        return f"依据的{kind or '东西'}有变" + (f"（{count} 项）" if count else "")
    if etype == EventType.LEDGER_FEEDBACK:
        # 对账结果上的反馈（next-round 02 做法 10）：只说是哪一种，不带谁给的
        verdict = {"useful": "说明有用", "unclear": "看不懂"}.get(str(data.get("verdict") or ""))
        return f"反馈：{verdict}" if verdict else "对账说明的反馈"
    if etype == EventType.LOG and isinstance(data.get("message"), str):
        return _clip(data["message"])
    return ""


def _tier(events: Sequence[tuple[int, str, str | None, Any]]) -> dict[str, Any] | None:
    """出具档位：封存范围内最后一条出具判定事件里的 tier，带上序号（浏览器拿那条已核对的事件对照）。子工作流里成果节点
    的出具判定（节点 id「子工作流节点 id/…」）说的是子工作流交出的那份，不是这次运行的，不认（verifier.js 同一个认法）。"""
    for seq, etype, node_id, data in reversed(events):
        if etype == EventType.ISSUANCE and "/" not in str(node_id or "") and isinstance(data, dict) \
                and isinstance(data.get("tier"), str):
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
    sure_columns: Mapping[str, Iterable[str]] | None = None,
    sure_secrets: Any = None,
) -> dict[str, Any]:
    """一次已封存运行 → 页面里的数据（bundle-data）。

    - run：运行记录的几样（id、workflow_id、workflow_name、version、version_hash、run_class、status、end_reason、
      started_at、finished_at）。end_reason 分开同是 cancelled 的被驳回和手动停止（05-已知遗留 (a)），不在封存范围内
    - events：这次运行的全部事件 (seq, type, node_id, data)，按数据库里 seq 的顺序。和封存核对
      （runner.verify_manifest）读的是同一个顺序：seq 相同的几条，浏览器按这里的先后排，清单才算得一样
    - manifest_seq / manifest_hash：封存范围（老运行是按结束事件推算的那个，legacy 为真）和封存哈希
    - read：工件 id → 原字节（不复验哈希），没有就是 None
    - source_masks：数据源名 → 它现在设的遮罩列（只列设了的）
    - sure_columns、sure_secrets：每份查询快照里确定取自遮罩列的结果列、这次运行查到过的遮罩值（只收确定的那几列），
      调用方按写作目录的口径认好交进来（api/evidence.py 的 certain_masks）：认老文档时按它们认，见 _Store.doc_withheld
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
    store = _Store(read, source_masks or {}, sealed, sure_columns=sure_columns, sure_secrets=sure_secrets)

    artifacts = _Artifacts(store, sealed)
    reports = _report_events(sealed)
    cited_queries: list[str] = []
    # 证据目录还带着值不给的指标原值的老文档只给哈希，也不顺着它的目录去附带它引用的口径卡和快照（附带了，浏览器在
    # 已核对的事件和文档里都找不到谁引用了它们）
    withheld = frozenset(r["node_id"] for r in reports if store.doc_withheld(r["doc_artifact"]))
    docs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for report in reports:
        doc = store.content(report["doc_artifact"]) if store.intact(report["doc_artifact"]) else None
        # 文档要真是这次运行里这个节点写的：内容寻址只保证没被改，不保证没被张冠李戴。对不上的照实嵌进去让
        # 浏览器标出来，但它的目录不可信，不顺着它去找别的工件
        if report["node_id"] not in withheld and isinstance(doc, dict) and doc.get("run_id") == run.get("id") \
                and doc.get("node_id") == report["node_id"]:
            docs.append((report, doc))
    # 文档里写成「已遮罩」的指标所在的口径卡只给 id：报告撰写节点写作时认出值不给，这里的认法认不出的（数据源事后撤了
    # 遮罩这类），卡上记着的原值也不该随文件出去
    forced = frozenset().union(*(_masked_cards(doc) for _, doc in docs))
    for report in reports:
        artifacts.add(report["doc_artifact"], "report_doc", report["node_id"], masked=report["node_id"] in withheld)
        for artifact, kind, owner in next((_cited_artifacts(doc) for r, doc in docs if r is report), []):
            item = artifacts.add(artifact, kind, owner, masked=kind == "metric_set" and artifact in forced)
            if kind == "query":
                cited_queries.append(artifact)
                continue
            if kind == ARTIFACT_KIND:
                # 分析结论随文件附带了（输入都没碰遮罩列、文件在、哈希对得上）：它的输入快照、下钻的上一层结论一并附上，
                # 引用链（④）按「已核对的分析结论的输入」认它们；服务端按封存的输入复算一遍，结果记在条目上（recheck）
                _attach_analysis(artifacts, store, item, artifact, owner, cited_queries)
                continue
            # 口径卡随文件附带了（没遮、文件在）：它记着的输入——哪个指标的值取自哪次查询的哪一格——
            # 那几次查询的快照也一并附上（设了遮罩的照旧只给 id）。点开数字看得到输入的 SQL 和被引用的行；
            # 引用链（④）按「已核对的口径卡的输入」认它们，被改过的卡（intact 不过）不带，免得替它圆
            if kind == "metric_set" and item is not None and "b64" in item and store.intact(artifact):
                for query, query_owner in _card_input_queries(store.content(artifact)):
                    artifacts.add(query, "query", query_owner)
                # 指标上的可比性结论（口径卡执行时自动做的，不进台账、只从卡上引用）同样附上：它的输入快照、服务端的复算
                # 跟着它（_attach_analysis），输入带遮罩的照旧只给 id
                for found in _card_comparability(store.content(artifact)):
                    _attach_analysis(artifacts, store, artifacts.add(found, ARTIFACT_KIND, owner), found, owner,
                                     cited_queries)

    masked_run = _masked_run(sealed, store, cited_queries, source_masks or {})
    shown_events = []
    for seq, etype, node_id, data in sealed:
        text = canonical_json(data)
        item: dict[str, Any] = {"seq": seq, "type": etype, "node_id": node_id, "data_sha256": content_hash(text)}
        if _shows_content(etype, node_id, data, node_types, store, masked_run, sealed, withheld, forced):
            item["canonical"] = text
        shown_events.append(item)

    return {
        "schema": SCHEMA,
        "meta": {
            "run_id": run.get("id"), "workflow_id": run.get("workflow_id"), "workflow_name": display_name(run),
            "version": run.get("version"), "version_hash": run.get("version_hash"),
            "run_class": run.get("run_class"), "status": run.get("status"), "end_reason": run.get("end_reason"),
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
