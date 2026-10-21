"""分析说法核对（docs/design/analysis-round/00-总方案.md §3.5，细案 01 §9）：报告里的判断词要挂对应的分析结论，判定对得上。

「叙述层无算术」往前走一步：「主要来自华东」「超出常规波动」「持续上升」这类判定和数字一样由系统算、由系统渲染（[[a:A3]]），
写作者只引用。写作者自己写了判断词的，这里确定地核对（不调模型，D6）：

1. **说法表**（LEXICONS，一处定义，带版本 LEXICON_VERSION）：判断词分成来源、分散、抵消、结构、异常、新高新低、正常、
   趋势、无趋势、差异、无差异、因果几类，每类要挂一种分析（op），判定要对得上（细案 01 §9.1）。只收强判断词，宁可漏报
   不误报（误报让正常的报告多重写一次、strict 下降档）。这几种写法不核：
   - 说的是构成、不是变化从哪来：来源、集中、分散、主导、拉动带动要这句话到这个分句为止说了变化（涨跌、变化、降幅、持平、
     环比……）才核——「下降主要来自华东」核，「本周订单主要来自线上渠道」「销售额集中在周末两天」「客户分散在各个城市」
     「通过会员活动带动复购」不核；「拖累」「贡献最大」本身就在说变化，照核
   - 建议、计划、预测、假设：分句里有「建议、计划、预计、争取、需要、应当、将要……」的不核（「建议」「下周」「避免」这些
     管到句子后面的分句：「建议加大投放，拉动新客增长」）
   - 业务上的「异常」：前面是订单、物流、支付、门店、营业、库存、设备、账号或数据、系统（「部分订单异常」「系统无异常」），
     后面是名词（「异常订单」「物流异常的订单」）；「无异常」「未见异常」主语得是指标或波动（「销售额未见异常」），
     「整体运行平稳，未见异常」不核
   - 别的：「持续」要跟着涨跌（「持续关注」不算）；「来源于」不是「源于」；「表结构变化」不是结构变化；「相当」只认比的是
     几组、几样的（「两个渠道的转化率相当」「规模相当」），比上期的（「订单量与上周相当」）、「相当于」「相当高」不算
2. **否定**：判断词前 4 个字以内（跳过「出现」「明显」这类填充词）有「不、没有、没、并非、并不、未、无」的翻到反面类：
   「销售额并未出现明显异常」是正常，「没有明显的上升趋势」是无趋势，「并非主要来自华东」要拆解、华东不在主要来源里；
   「不但」「不少」「无论」「未来」这类不是否定
3. **怎么挂**：一句话的分析依据 = 句中的 [[a:]]、[[v:A…]]、[[table:A…]] 和句末 [[see:A…]]（组装、复核时按重新解析的
   引用算，见 evidence.verify_doc）。类要的 op 必须在依据里出现，几个里任一个对得上就算对；挂了算不了的结论算对不上
4. **点名的段**：结论结果表里的维度值、乘积拆解的因子（全半角归一，两个字以上、不是纯数字）。来源类判断看判断词说的那一边：
   「主要来自」「集中在」「以…为主」看后面、判断词里（到分句末尾），「主导」「贡献最大」看前面（同一分句没点名的往前找
   最近点了名的分句）。判定要说了主要来源（driver、concentrated，比率拆解的 mix_driven、rate_driven），点名的段都在
   subjects 里，而且得点名：「下降主要来自线上渠道」「下降主要来自新客流失」挂了按地区的拆解，说的来源不是拆解里的段，
   对不上（同一分句引用了判定语 [[a:A1]] 的、比率拆解说「结构变化」「自身变化」的不用点名）。拖累要那一段变化为负，
   拉动、带动为正（看整个分句，没点名的按 subjects；不在 subjects 里、但那一段的变化占总变化过半的也算拖累）
5. **来源和因果的分界**：原因在因果词的哪一边：「由于、因为、原因是、源于、得益于」后面，「导致、造成、引起、所致、
   是…的主要原因」前面（「由华东造成」是「由」和「造成」之间），「受…影响」中间。句子挂了拆解、原因点名的是这份结论的
   段或因子（「下降主要由华东造成」「华东是下降的主要原因」）→ 按来源核，这是记账意义上的「来自」；点名的不是（「由于
   促销结束导致下降」「下降主要由促销结束造成」）→ 按因果核：同一句要有知识库原话（[[q:]]）或运行输入（[[i:]]）作
   依据，或者写成推测（可能、或许、推测、待核实、不排除、疑似）。句子没挂拆解、同一分句里有来源判断的，原因不是拆解
   的段时另报（目录里有拆解可比的时候），认不出时并进来源那一处（它报没挂拆解）。原因说的是数据本身（「由于数据只到
   10 月 16 日」「样本少」）、只是指代（「以上是下降的主要原因」，列出原因的那几句各自核）的不算
6. **连续**：「连续 N 期上升」「三连降」按趋势结论记的末尾连续同向（run_direction、run_length）核：方向一致、期数不少于
   N（没写 N 的至少 3 期），不看整体的趋势判定（判定语在方向相反时写「但最近已连续 N 期上升」）
7. **不扫的地方**：知识库原话（[[q:]] 片段）、系统渲染的判定语（[[a:]] 片段）、别的引用渲染出来的字、标题、代码块、
   表格（表头和正文的格都不扫：一格是一个值不是一句话，状态列的「异常」、说明列的「主要来自线上」不是判断；裁判的预筛
   judge.claim_priority 也不送表格的格）；分析范围是文档的结构化字段（analysis_scope），本来就不在正文里

违规三种（CODES）：analysis_claim_uncited（有判断词，依据里没有对应种类的分析结论）、analysis_claim_mismatch（挂了，判定、
点名的段、方向对不上，含挂了算不了的结论）、causal_unsupported（因果词没有依据、也不是推测）。每条带：代码、判断词
（text）、所在句子（context）、类（claim）、要的 op、这句挂的分析（analyses）、给人看的一句（message）和交回写作者的
一句（for_model）。

报告文档记下按哪种处置、哪一版说法表核（DOC_FIELD：{"mode": check / strict, "lexicon": 1}，参与内容哈希）：只有带这一项
的文档在复核时核这一项（verify），老文档复核结果不变；复核按文档记下的那一版说法表，说法表升了版老文档照旧。

整个模块是纯函数，不碰数据库、不调模型。
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from app.analysis.contract import LEDGER_KIND, OP_LABELS, catalog_result, method_label
from app.engine.evidence import _Reason, _violation, iter_units

#: 说法表的版本：改了判断词、类、绑定规则就升版本，老版本留在 LEXICONS 里给老文档复核用
LEXICON_VERSION = 1
#: 报告节点 analysis_claims 的取值里要核的两种（off 不核、文档里也不记）
MODES = ("check", "strict")
#: 报告文档里记下核对处置和说法表版本的字段
DOC_FIELD = "analysis_claims"
UNCITED = "analysis_claim_uncited"
MISMATCH = "analysis_claim_mismatch"
CAUSAL = "causal_unsupported"
CODES = frozenset({UNCITED, MISMATCH, CAUSAL})

# --------------------------------------------------------------------------
# 说法表 v1
# --------------------------------------------------------------------------

#: 涨跌的说法。「扩大」「缩小」「收窄」不收：亏损扩大、差距缩小说不清是哪个方向
_UP = "上升|增长|增加|提升|提高|上涨|攀升|走高|上扬|走强|回升|反弹|增多|上行|抬升|拉升"
_DOWN = "下降|下滑|减少|降低|下跌|走低|回落|走弱|萎缩|减弱|下行|下挫|走软|缩水"
_DIR = f"(?:{_UP}|{_DOWN})"
_UP_RE = re.compile(_UP)
_DOWN_RE = re.compile(_DOWN)
#: 说的是变化：来源、集中、分散、主导、拉动带动要这句话到这个分句为止有它才核（「下降主要来自华东」说的是变化从哪来，
#: 「本周订单主要来自线上渠道」说的是构成）。「环比」「同比」本身不是涨跌，可跟着的是变化（「环比 −5%，主要来自华东」）
_CHANGE = f"{_UP}|{_DOWN}|变化|变动|涨跌|升降|增减|涨幅|降幅|增幅|跌幅|减幅|增量|减量|增速|持平|波动|环比|同比"
#: 判断词后面紧跟着的涨跌、波动（「订单异常下降」「出现异常的增长」说的是指标，不是订单状态）
_MOVES = re.compile(f"(?:的|地)?(?:{_DIR}|波动|变化|变动)")
#: 分句的边界（顿号、冒号不算：「集中在华东、华北」「主要来自：华东」还是一句里的事）
_STOPS = "，,；;。！？!?\n"
_NOT_STOP = f"[^{_STOPS}]"
#: 「相当」比的是几组、几样（「两个渠道的转化率相当」「规模相当」）才是差异判断
_PEERS = re.compile(r"水平|规模|表现|幅度|程度|体量|量级|两(?:个|组|类|种|者|地|边|家|区|店)|二者|双方|几个|几组|各(?:个|组|类)")
#: 「相当」比的是上一期、往常：说的是持平，不是两组差异（「订单量与上周相当」）
_PERIODS = re.compile(r"上(?:一)?(?:个)?(?:周|月|季度|季|年|期)|前(?:一)?(?:周|月|天|日|期)|去年|同期|往年|以往|往常|昨天|昨日"
                      r"|此前|之前")


@dataclass(frozen=True)
class Claim:
    """说法表的一类：判断词（words）、要挂的分析（op，结构变化另要 mode ratio）、判断词前有否定时翻到哪一类（flip，None：
    否定了就不核，宁可漏报）。negative：这一类的说法本身就是否定的写法（「并不异常」「没有明显趋势」），不再看前面的否定。
    同一类可以有几条（key 相同）：翻到这一类时用第一条。"""

    key: str
    label: str
    op: str | None
    words: re.Pattern[str]
    flip: str | None = None
    mode: str | None = None
    negative: bool = False
    #: 「连续…上升」「三连降」：按末尾连续同向的期数核（_run_miss）
    run: bool = False
    #: 这句话到这个分句为止要说了变化（Lexicon.change）才核：来源、集中、分散、主导、拉动带动
    gated: bool = False
    #: 判断词前面（同一分句里）要有这些字才核（「两个渠道的转化率相当」）
    needs: re.Pattern[str] | None = None
    #: 分句里有这些字就不核（「订单量与上周相当」）
    bars: re.Pattern[str] | None = None
    #: 主语要是指标或波动（Lexicon.subject）才核：「销售额未见异常」核，「整体运行平稳，未见异常」不核
    subject: bool = False
    #: 点名的段（来源类）、原因（因果类）在判断词的哪一边：after 后面到分句末尾（主要来自华东、由于促销结束）、before 前面
    #: （华东主导、华东是…的主要原因、促销结束导致）、inside 判断词里（以线上为主、受促销影响）、pull 拖累拉动带动（看整个
    #: 分句，见 _check）；None 整个分句
    side: str | None = None


@dataclass(frozen=True)
class Lexicon:
    version: int
    claims: tuple[Claim, ...]
    #: 判断词本身带的方向（不看上下文）：骤降是跌、激增是涨、拖累要那一段是跌
    intrinsic: tuple[tuple[re.Pattern[str], str], ...]
    #: 判断词前面的否定（_negated）
    negation: re.Pattern[str]
    #: 否定词和判断词之间可以隔着的填充词（「并未出现明显异常」）
    filler: re.Pattern[str]
    #: 推测的说法：因果句有它就不算没依据
    guess: re.Pattern[str]
    #: 因果说的是数据本身（数据只到哪天、口径、样本）：不当原因判断核
    method: re.Pattern[str]
    #: 「异常」「正常」这一类的判断词前面是这些的，说的是数据质量（数据异常、系统无异常），不是指标的波动
    quality: re.Pattern[str]
    #: 「异常」「异动」前面是这些的，说的是业务上的状态（部分订单异常、门店营业异常）；后面跟着涨跌的说的是指标（订单
    #: 异常下降、出现异常的增长），「订单大幅减少」也照核
    status: re.Pattern[str]
    #: 说的是变化（Claim.gated 的类要有它）
    change: re.Pattern[str]
    #: 建议、计划、预测、假设：分句里有它的判断词不核（「下周将持续提升转化率」不是对数据下的判断）
    plan: re.Pattern[str]
    #: 同上，管到句子后面的分句（「建议加大投放，拉动新客增长」「避免缺货导致客诉」）
    plan_ahead: re.Pattern[str]
    #: 指标或波动（Claim.subject 的类要主语是它）
    subject: re.Pattern[str]

    def claim(self, key: str) -> Claim:
        return next(c for c in self.claims if c.key == key)


_V1 = Lexicon(
    version=1,
    claims=(
        Claim("source", "来源", "decompose", re.compile(
            r"主要(?:来自|源自|出自|源于|由(?!于)|集中(?:在|于)|依靠|依赖|靠|体现在|表现在|发生在|在于|在)"
            r"|(?:绝)?大(?:部分|多数|多)(?:来自|源自|出自|由(?!于)|集中(?:在|于))"
            r"|(?:将近|接近|超过|近|超|逾|过|约|多达|高达)?(?:[一二三四五六七八九两几]成|一半|半数|过半|大半|大部分|大多数)"
            rf"{_NOT_STOP}{{0,8}}?(?:来自|源自|出自|集中(?:在|于))"
            r"|集中(?:在|于)"), flip="source_not", gated=True, side="after"),
        Claim("source", "来源", "decompose", re.compile(
            rf"主要是(?!因为|由于|受|指|看|说|想|希望){_NOT_STOP}{{0,12}}?(?:{_DIR}|贡献|拖累|带动|拉动|驱动)"
            rf"|以{_NOT_STOP}{{1,8}}?为主(?![题线要体流打])"), flip="source_not", gated=True, side="inside"),
        Claim("source", "来源", "decompose", re.compile(r"主导(?!地位|产品|品类|品牌|产业)|贡献了(?:绝)?大部分"),
              flip="source_not", gated=True, side="before"),
        Claim("source", "来源", "decompose", re.compile(r"贡献(?:最大|最多|最高)|最大(?:的)?贡献"),
              flip="source_not", side="before"),
        Claim("source", "来源", "decompose", re.compile(r"拉动|带动"), flip="source_not", gated=True, side="pull"),
        Claim("source", "来源", "decompose", re.compile(r"拖累"), flip="source_not", side="pull"),
        Claim("source_not", "来源（否定）", "decompose", re.compile(r"(?!)"), negative=True),
        Claim("dispersed", "分散", "decompose", re.compile(r"分散(?!注意|精力|风险|投资|采购)"), flip="source",
              gated=True),
        Claim("dispersed", "分散", "decompose", re.compile(
            rf"普遍(?:有所|出现|呈现)?{_DIR}"
            rf"|全面{_DIR}"
            rf"|各(?:个)?{_NOT_STOP}{{1,6}}?(?:都|均)(?:有所|出现|在)?{_DIR}"), flip="source"),
        Claim("offset", "抵消", "decompose", re.compile(
            r"抵消|抵冲|此消彼长|此增彼减|此涨彼跌|涨跌互现|有增有减|有涨有跌|有升有降"
            r"|一增一减|一减一增|一升一降|一降一升|一涨一跌|一跌一涨")),
        Claim("mix", "结构", "decompose", re.compile(
            r"结构性|(?<![表据段])结构(?:变化|变动|效应)|占比(?:的)?变化(?:带来|造成|导致|引起)"), mode="ratio"),
        Claim("anomaly", "异常", "baseline", re.compile(
            r"异常(?!订单|单|值|数据|记录|交易|码|状态|类|退款|商品|门店|账户|账号|用户|日志|处理|检测"
            rf"|的(?!{_DIR}|波动|变化|变动|高|低))"
            r"|异动|反常|罕见"
            r"|(?:明显|显著|大幅|严重)偏离|偏离(?:了)?(?:正常|常规|往常|以往|历史|过往)"
            rf"|(?:明显|显著)?(?:超出|超过|跳出|突破|高于|低于)(?:了)?{_NOT_STOP}{{0,14}}?(?:正常|常规)(?:的)?(?:波动)?(?:范围|水平|区间)"
            r"|超出(?:了)?(?:正常|常规)"
            r"|(?:远远|远|明显|显著|大幅)(?:地)?(?:高于|低于|超过|超出)(?:了)?"
            r"(?:往常|以往|平时|平常|往期|过往|历史(?:同期)?(?:水平|均值)?|平均水平)"
            rf"|大幅(?:度)?(?:地)?(?:{_DIR}|波动|变化|变动)|显著(?:地)?{_DIR}"
            r"|骤降|骤减|暴跌|暴降|锐减|激增|飙升|暴涨|猛增|骤增|陡增|陡降"
            r"|剧烈(?:地)?(?:波动|变化|变动|震荡|起伏)|(?:波动|变化|变动)(?:十分|非常|较为|比较)?剧烈"
            r"|波动(?:幅度)?(?:较大|很大|偏大|加大|加剧)"),
              flip="normal"),
        Claim("record", "新高新低", "baseline", re.compile(
            r"历史(?:新高|新低|最高|最低)"
            rf"|创(?!新(?!高|低))(?:下|出|造)?(?:了)?{_NOT_STOP}{{0,4}}?(?:新高(?!地)|新低|最高|最低)"
            r"|前所未有")),
        Claim("normal", "正常", "baseline", re.compile(
            r"(?:正常|常规)(?:的)?波动(?:范围)?(?:内|之内)?"
            r"|(?:正常|常规)(?:的)?(?:范围|区间)(?:内|之内)?"
            r"|(?:属于|处于|在)(?:正常|常规)(?:水平|区间|范围)"), flip="anomaly"),
        Claim("normal", "正常", "baseline", re.compile(
            r"(?:并不|并非|并没有|没有|未见|并无|不算|不属于|不是)(?:明显)?(?:的)?异常|无异常"), negative=True,
              subject=True),
        Claim("trend", "趋势", "trend", re.compile(
            rf"{_DIR}(?:的)?(?:趋势|态势|势头)"
            rf"|(?:趋势|态势|势头)(?:性)?(?:地)?{_DIR}"
            rf"|(?:呈|呈现|保持|维持|显示)(?:出)?{_DIR}?(?:的)?(?:趋势|态势)"
            r"|(?:趋势|态势|走势|势头)(?:持续|继续|整体|总体)?(?:向好|向上|向下|好转|转好|走好|转差|走坏|恶化|转弱|转强)"
            rf"|持续(?:地|性)?{_DIR}"
            rf"|一路(?:{_DIR}|走高|走低|上扬)"
            rf"|逐步{_DIR}|稳步{_DIR}"
            r"|节节(?:攀升|升高|走高|上涨|上升|下滑|下降|败退|走低)"), flip="no_trend"),
        Claim("trend", "趋势", "trend", re.compile(
            rf"连续(?:[\x00\s0-9０-９一二三四五六七八九十两几多数个周月天日季度年期]|都|在|有所|出现)*?{_DIR}"
            r"|(?:[0-9０-９]+|[二三四五六七八九十两几多数]+|\x00+)连(?:降|跌|减|涨|升|增)"),
              flip="no_trend", run=True),
        Claim("no_trend", "无趋势", "trend", re.compile(
            r"(?:没有|并没有|没|无|未见|并无|未呈现|未出现)(?:明显|显著)?(?:的)?"
            r"(?:(?:上升|下降|增长|下滑|向上|向下)(?:或|和|、|与|/)?)*(?:的)?(?:趋势|态势)"
            rf"|{_DIR}?(?:的)?(?:趋势|态势)(?:并)?(?:不明显|不显著|不清晰|不突出)"
            r"|基本平稳|走势平稳"), negative=True),
        Claim("differ", "差异", "differ", re.compile(
            r"(?:显著|明显)(?:地)?(?:高于|低于|大于|小于|多于|少于|好于|差于|优于|劣于)"
            r"|确实(?:更高|更低|更多|更少|高于|低于)"
            r"|差(?:异|别)(?:十分|非常|很|较为)?(?:明显|显著)"
            r"|(?:明显|显著)(?:的)?(?:差异|差别)"), flip="no_diff"),
        Claim("no_diff", "无差异", "differ", re.compile(
            r"差(?:异|别)(?:不大|很小|较小|不明显|并不明显|不显著|有限)"
            r"|(?:没有|并没有|没|无|并无)(?:明显|显著)?(?:的)?(?:差异|差别|区别)"
            r"|不相上下|旗鼓相当"), negative=True),
        Claim("no_diff", "无差异", "differ", re.compile(rf"(?:基本|大致|大体)?相当(?=[{_STOPS}\s\x00]|$)"),
              negative=True, needs=_PEERS, bars=_PERIODS),
        Claim("causal", "因果", None, re.compile(
            r"因为|由于|归因(?:于|为)|(?:主要)?原因(?:主要)?(?:是|在于|为|：|:)|(?<!来)源于|得益于"), side="after"),
        Claim("causal", "因果", None, re.compile(
            rf"导致|造成|引起(?!关注|重视|注意|警惕|讨论|兴趣)|所致|是{_NOT_STOP}{{0,12}}?的主要原因"), side="before"),
        Claim("causal", "因果", None, re.compile(rf"受(?!到?影响)(?:到)?{_NOT_STOP}{{1,12}}?(?:的)?影响"),
              side="inside"),
    ),
    intrinsic=(
        (re.compile(r"拖累|骤降|骤减|暴跌|暴降|锐减|陡降|新低|最低|低于|小于|少于|更低|更少|连(?:降|跌|减)|向下"), "down"),
        (re.compile(r"拉动|带动|激增|飙升|暴涨|猛增|骤增|陡增|新高|最高|高于|大于|多于|更高|更多|连(?:涨|升|增)|向上"), "up"),
    ),
    negation=re.compile(r"不(?![但仅只光论少同断久管过如然错禁免妨要])|没有|没|并非|并不|未(?![来必])|无(?![论疑关需])"),
    filler=re.compile(r"(?:出现|发现|看到|见到|存在|呈现|表现出|明显|显著|什么|任何|多大|太|很|特别|有|过|的|了|[\x00\s])$"),
    guess=re.compile(r"可能|或许|也许|推测|猜测|待核实|有待核实|需(?:要)?(?:进一步)?(?:核实|确认|验证)|待确认|待验证"
                     r"|不排除|疑似|大概率"),
    method=re.compile(r"数据|口径|统计|查询|目录|样本|取数|截断|截止|只到|不完整|未覆盖|篇幅|省略|证据"),
    quality=re.compile(r"(?:数据|系统|接口|取数|口径|同步|查询|日志|字段|格式|编码)(?:出现|存在|有|无|没有|并无|未见)?$"),
    status=re.compile(r"(?:订单|物流|支付|门店|营业|库存|设备|账号|账户|人事)(?:出现|存在|有|无|没有|并无|未见)?$"),
    change=re.compile(_CHANGE),
    plan=re.compile(
        r"(?<![即])将(?=会|要|继续|持续|进一步|全面|大幅|逐步|稳步|保持|维持|迎来|出现|有望|达到|提升|提高|降低|下降|上升|增长"
        r"|增加|减少|加大|加强|推进|推出|开展|启动|上线|恢复|回升|回落)"
        r"|即将|需要|(?<![必所])需(?!求)|应当|应该"
        r"|(?<![相对供反响适效感呼])应(?=继续|持续|进一步|加强|尽快|及时|重点|做好|关注|提升|提高|降低|减少|加大)"
        r"|(?<![主重只首必纲概摘想需不])要(?=继续|持续|进一步|加强|尽快|及时|重点|做好|关注|保持|确保|提升|提高|降低|减少"
        r"|加大)"
        r"|目标(?=是|为|：|:)|如果|假如|一旦|若(?!干)"),
    plan_ahead=re.compile(
        r"建议|计划(?!完成|达成|内|外|数|值|量)|预计|争取|力争|打算|希望|下一步|下阶段|下一阶段|接下来|下周|下个?月|下季度"
        r"|明年|避免|防止|以免|以防|防范|谨防"),
    subject=re.compile(
        rf"{_DIR}|波动|变化|变动|走势|涨跌|涨幅|降幅|增幅|跌幅|增速|幅度|水平|指标|销售额|销量|订单量|订单数|单量|客单价"
        r"|转化率|退款率|退款额|退货率|毛利|利润|收入|营收|客流|流量|访客|GMV", re.I),
)

LEXICONS: dict[int, Lexicon] = {1: _V1}

#: 能挂「主要来自」这类来源判断的拆解判定（判定语就是这么说的）：一段或一个因子为主、集中在前几段，比率拆解里结构为主、
#: 各段自身为主
_SOURCE_VERDICTS = frozenset({"driver", "concentrated", "mix_driven", "rate_driven"})
#: 句子没挂拆解时，有这几类判断的分句里的因果词并进它们核（「下降主要由华东造成」：说的是记账意义上的来源，那一处报没挂
#: 拆解）；挂了拆解的按原因点名的是不是拆解的段分开核（check_sentence）
_SOURCE_KEYS = frozenset({"source", "source_not", "dispersed", "offset", "mix"})
#: 来源类判断要点名是哪一段的那几种（拖累、拉动、带动另按方向核，翻过来的否定另有规矩）
_NAMING_SIDES = frozenset({"after", "inside", "before", "cause"})
#: 比率拆解的结构为主、各段自身为主：判定语说的是「主要来自结构变化」「主要来自各段自身的变化」，句子这么说也算点了名
_EFFECTS = {"mix_driven": re.compile(r"结构|占比"), "rate_driven": re.compile(r"自身")}
#: 判定语引用（[[a:A1]]，不含具名标量 [[a:A1.键]]）的片段 ref
_PHRASE_REF = re.compile(r"^a:\s*([A-Za-z]+\d+)\s*$")
#: 拆解结果表里不是维度的列（params.by 对不上时按「不是度量列」认维度，跳过这些）
_MEASURE_COLUMNS = frozenset({
    "base", "target", "delta", "pct", "share", "flag", "n_base", "n_target", "d_base", "d_target", "w_base",
    "w_target", "r_base", "r_target", "mix", "rate", "total", "contrib",
})
#: 一段的变化记在哪一列：可加拆解的 delta、比率拆解的 total（结构 + 自身）、乘积拆解的 contrib
_DELTA_COLUMNS = ("delta", "total", "contrib")


def lexicon(version: int | None = None) -> Lexicon:
    """某一版说法表；不认识的版本按当前版。"""
    return LEXICONS.get(version if isinstance(version, int) else LEXICON_VERSION, LEXICONS[LEXICON_VERSION])


# --------------------------------------------------------------------------
# 扫判断词
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    """句子里的一处判断词：类（否定翻过之后的）、在句子里的起止、原文、方向（up / down / None）。side 是点名的段、原因在
    判断词的哪一边（Claim.side，否定翻过来的为 None），count 是「连续 N 期」「三连降」写明的期数（没写为 None）。"""

    claim: str
    start: int
    end: int
    word: str
    direction: str | None
    negated: bool = False
    run: bool = False
    side: str | None = None
    count: int | None = None


def _clause(text: str, start: int, end: int) -> tuple[int, int]:
    """[start, end) 所在的分句：往前往后各找到最近的句读。"""
    left = max((text.rfind(ch, 0, start) for ch in _STOPS), default=-1) + 1
    rights = [i for i in (text.find(ch, end) for ch in _STOPS) if i >= 0]
    return left, min(rights) if rights else len(text)


def _negated(lex: Lexicon, before: str) -> bool:
    """判断词前 4 个字以内有否定（先去掉紧挨着的填充词：「并未出现明显异常」的否定隔着「出现明显」）。"""
    while (m := lex.filler.search(before)) and m.group(0):
        before = before[:m.start()]
    return lex.negation.search(before[-4:]) is not None


def _direction(lex: Lexicon, word: str, after: str, clause: str) -> str | None:
    """判断词说的方向：判断词本身带的（骤降、拉动），再看判断词里、紧跟着的几个字、整个分句里的涨跌说法。一个分句里
    涨跌都有的不判方向。"""
    for pattern, direction in lex.intrinsic:
        if pattern.search(word):
            return direction
    for text in (word, after[:4], clause):
        up, down = bool(_UP_RE.search(text)), bool(_DOWN_RE.search(text))
        if up != down:
            return "up" if up else "down"
        if up and down:
            return None
    return None


def _counts(lex: Lexicon, claim: Claim, text: str, start: int, end: int, left: int, right: int) -> bool:
    """这处判断词核不核：说的是数据质量、业务状态的「异常」，没说变化的来源、集中、分散（构成），建议、计划、预测、假设，
    不合条件的「相当」，都不核。"""
    before = text[left:start]
    if claim.key in ("anomaly", "normal"):
        head = re.sub(r"(?:不|没有|没|并不|并非|未|无)$", "", before.rstrip("\x00 "))
        if lex.quality.search(head) or ("异" in text[start:end] and lex.status.search(head)
                                         and not _MOVES.match(text, end)):
            return False
    if claim.gated and not lex.change.search(text[:right]):
        return False
    if lex.plan.search(text[left:right]) or lex.plan_ahead.search(text[:right]):
        return False
    if claim.needs is not None and not claim.needs.search(before):
        return False
    return claim.bars is None or not claim.bars.search(text[left:right])


#: 「连续 N 期」「N 连降」里的期数：阿拉伯数字（全角也认）或一到九十九的中文数字
_COUNT = re.compile(r"[0-9０-９]+|[一二三四五六七八九十两]+")
_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _count(word: str) -> int | None:
    """判断词里写明的期数：「连续三周」3、「连续 12 周」12、「十二连涨」12；「连续多周」「连续下滑」None。"""
    m = _COUNT.search(word)
    if m is None:
        return None
    raw = unicodedata.normalize("NFKC", m.group(0))
    if raw.isdigit():
        return int(raw)
    if "十" not in raw:
        return _CN_DIGITS.get(raw) if len(raw) == 1 else None
    tens, _, ones = raw.partition("十")
    if len(tens) > 1 or len(ones) > 1:
        return None
    return (_CN_DIGITS.get(tens, 0) if tens else 1) * 10 + (_CN_DIGITS.get(ones, 0) if ones else 0)


def scan(text: str, *, version: int | None = None) -> list[Hit]:
    """一句话里的判断词（text 是写作者自己写的字，别的片段已经涂成 \\x00）。同一处有几种读法时取最靠前、最长的：
    「超出正常范围」是异常不是正常，「没有明显的上升或下降趋势」是无趋势不是趋势。不核的写法（_counts）连同和它重叠的
    读法一起跳过。"""
    lex = lexicon(version)
    found: list[tuple[int, int, int, Claim]] = []
    for order, claim in enumerate(lex.claims):
        for m in claim.words.finditer(text):
            if m.end() > m.start():
                found.append((m.start(), -(m.end() - m.start()), order, claim))
    hits: list[Hit] = []
    taken = 0
    for start, minus, _, claim in sorted(found, key=lambda f: (f[0], f[1], f[2])):
        end = start - minus
        if start < taken:
            continue
        taken = end
        left, right = _clause(text, start, end)
        if not _counts(lex, claim, text, start, end, left, right):
            continue
        before = text[left:start]
        key, negated = claim.key, False
        if not claim.negative and _negated(lex, before):
            negated = True
            if claim.flip is None:
                continue
            key = claim.flip
        word = text[start:end]
        if key == "normal" and (claim.subject or (negated and "异" in word)) and not lex.subject.search(text[:right]):
            continue            # 「未见异常」「没有出现异动」：主语不是指标、波动的（运行平稳、系统正常）不核
        direction = _direction(lex, word, text[end:right], text[left:right])
        run = claim.run and not negated
        hits.append(Hit(claim=key, start=start, end=end, word=word, direction=direction, negated=negated, run=run,
                        side=None if negated else claim.side, count=_count(word) if run else None))
    return hits


# --------------------------------------------------------------------------
# 一句话
# --------------------------------------------------------------------------


@dataclass
class Sentence:
    """交给核对的一句话（和标记解析解耦：文档里怎么切、怎么解析引用由 evidence 管，这里只要组装好的单元）。

    text、names、shown 等长，位置一一对应：text 只留写作者自己写的字（判断词在这里找），names 另留引用渲染出来的值（点名
    的段在这里认），两者都把判定语、知识库原话涂成 \\x00；shown 是给人看的整句（排版符号涂成 \\x00，显示时去掉）。没给
    names、shown 的按 text。analyses 是这句的分析依据（A 编号），quoted / inputs：同一句里有知识库原话、运行输入。base 是
    text[0] 在正文里的位置（违规带 span 要用），pieces 是各片段在 text 里的起止和编号（违规带所在片段），phrases 是判定语
    引用 [[a:A1]] 在 text 里的起止和编号（同一分句引用了判定语的来源判断不用再点名）。"""

    text: str
    names: str = ""
    shown: str = ""
    analyses: list[str] = field(default_factory=list)
    quoted: bool = False
    inputs: bool = False
    base: int | None = None
    unit: str | None = None
    pieces: list[tuple[int, int, str | None]] = field(default_factory=list)
    phrases: list[tuple[int, int, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.names) != len(self.text):
            self.names = self.text
        if len(self.shown) != len(self.text):
            self.shown = self.names


def _same_length_norm(text: str) -> str:
    """逐字归一（NFKC 会把个别字展开成几个字，逐字做、展开的取第一个，位置就和原文对得上）。"""
    out = []
    for ch in text:
        n = unicodedata.normalize("NFKC", ch).casefold()
        out.append(n[:1] if n else ch)
    return "".join(out)


_NUMERIC = re.compile(r"^[\d.,+\-%‰\s]+$")


def _named_value(value: Any) -> str | None:
    """结果表里一个维度值能不能拿来认点名：两个字以上、不是纯数字。"""
    if value is None or isinstance(value, bool):
        return None
    text = _same_length_norm(str(value).strip())
    if len(text) < 2 or _NUMERIC.match(text):
        return None
    return text


def _dims(result: Mapping[str, Any]) -> list[int]:
    """结果表里各段名字所在的列：拆解按 params.by（照输入的列名），乘积拆解是 factor 列（因子的叫法），差异检验是 a、b 两组；
    拆解的 by 对不上表头时按「不是度量列」认。别的算子没有段。"""
    table = result.get("table") or {}
    columns = [str(c) for c in table.get("columns") or []]
    if result.get("op") == "differ":
        return [columns.index(c) for c in ("a", "b") if c in columns]
    if result.get("op") != "decompose":
        return []
    if result.get("mode") == "factor" and "factor" in columns:
        return [columns.index("factor")]
    by = (result.get("params") or {}).get("by")
    wanted = by if isinstance(by, list) else [by] if isinstance(by, str) else []
    picked = [columns.index(str(b)) for b in wanted if str(b) in columns]
    return picked or [i for i, c in enumerate(columns) if c not in _MEASURE_COLUMNS]


def _segment_names(result: Mapping[str, Any]) -> dict[str, str]:
    """{归一后的维度值或因子名: 原样}：结果表里各段的维度值，加上判定点名的 subjects。"""
    out: dict[str, str] = {}
    rows = (result.get("table") or {}).get("rows") or []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list):
            continue
        for i in _dims(result):
            if i < len(row) and (named := _named_value(row[i])):
                out.setdefault(named, str(row[i]).strip())
    for subject in _subjects(result):
        if named := _named_value(subject):
            out.setdefault(named, subject)
    return out


def _verdict(result: Mapping[str, Any]) -> Mapping[str, Any]:
    verdict = result.get("verdict")
    return verdict if isinstance(verdict, Mapping) else {}


def _subjects(result: Mapping[str, Any]) -> list[str]:
    verdict = _verdict(result)
    out: list[str] = []
    for subject in verdict.get("subjects") or []:
        values = subject if isinstance(subject, list) else [subject]
        out.extend(str(v).strip() for v in values if v is not None)
    return out


def _named(result: Mapping[str, Any], names: str) -> list[str]:
    """分句里点名的段（原样，按出现的先后）。ASCII 的名字要整词出现（「A」不算点名了「AB」）。"""
    flat = _same_length_norm(names)
    hits: list[tuple[int, str]] = []
    for key, original in _segment_names(result).items():
        at = flat.find(key)
        while at >= 0:
            before, after = flat[at - 1:at], flat[at + len(key):at + len(key) + 1]
            if not (key.isascii() and any(ch.isascii() and ch.isalnum() for ch in (before, after) if ch)):
                hits.append((at, original))
                break
            at = flat.find(key, at + 1)
    return [name for _, name in sorted(hits)]


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _segment_change(result: Mapping[str, Any], name: str) -> tuple[Decimal | None, Decimal | None]:
    """一段（维度值或因子名）的变化和占总变化的比重：结果表里这一段所在各行的变化相加；表里没有的为 None。"""
    table = result.get("table") or {}
    columns = [str(c) for c in table.get("columns") or []]
    col = next((columns.index(c) for c in _DELTA_COLUMNS if c in columns), None)
    if col is None:
        return None, None
    share_col = columns.index("share") if "share" in columns else None
    want = _named_value(name)
    total: Decimal | None = None
    share: Decimal | None = None
    for row in table.get("rows") or []:
        if not isinstance(row, list) or not any(i < len(row) and _named_value(row[i]) == want for i in _dims(result)):
            continue
        if (d := _decimal(row[col] if col < len(row) else None)) is not None:
            total = d if total is None else total + d
        if share_col is not None and (s := _decimal(row[share_col] if share_col < len(row) else None)) is not None:
            share = s if share is None else share + s
    return total, share


def _both_ways(result: Mapping[str, Any]) -> bool:
    """结果表里各段的变化有涨有跌。"""
    table = result.get("table") or {}
    columns = [str(c) for c in table.get("columns") or []]
    col = next((columns.index(c) for c in _DELTA_COLUMNS if c in columns), None)
    if col is None:
        return False
    signs = {d > 0 for row in table.get("rows") or [] if isinstance(row, list) and col < len(row)
             and (d := _decimal(row[col])) is not None and d != 0}
    return signs == {True, False}


def _caveats(result: Mapping[str, Any]) -> set[str]:
    return {str(c.get("code")) for c in result.get("caveats") or [] if isinstance(c, dict)}


def _value(result: Mapping[str, Any], key: str) -> Decimal | None:
    return _decimal((result.get("values") or {}).get(key))


def _dir_word(direction: str | None) -> str:
    return {"up": "上升", "down": "下降"}.get(direction or "", "")


def _run_direction(raw: Any) -> str | None:
    text = str(raw or "").strip().lower()
    if text in ("up", "rising", "上升"):
        return "up"
    if text in ("down", "falling", "下降"):
        return "down"
    return None


# --------------------------------------------------------------------------
# 判定对不对得上
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Miss:
    """一份结论对不上的原因：给写作者的、给人看的各一句（不带结论编号，外面接上）。"""

    model: str
    human: str


def _phrase(result: Mapping[str, Any]) -> str:
    return str(result.get("phrase") or "").strip()


def _verdict_miss(result: Mapping[str, Any]) -> _Miss:
    said = f"判定是「{_phrase(result)}」" if _phrase(result) else "判定不是这一类"
    return _Miss(said, said)


def _check(lex: Lexicon, hit: Hit, result: Mapping[str, Any], names: str, *, spans: tuple[str, ...] = (),
           phrased: bool = False) -> _Miss | None:
    """这处判断词能不能挂这份结论：对得上返回 None，对不上返回原因。names 是判断词所在分句（点名用的样子）；spans 是来源类
    判断说的那一边（_object，近的在前，取第一个点了名的），没给按分句；phrased：同一分句引用了这份结论的判定语。"""
    if result.get("status") != "ok":
        reason = str(result.get("reason") or "算不了")
        return _Miss(f"没有做成（{reason}）", f"这份结论没有做成（{reason}）")
    verdict = _verdict(result)
    code = verdict.get("code")
    key = hit.claim
    if key in ("source", "source_not"):
        subjects = _subjects(result)
        named = next((found for span in spans or (names,) if (found := _named(result, span))), [])
        # 判定语说「主要来自」的几种：一段（一个因子）为主、集中在前几段；比率拆解的结构为主、各段自身为主
        top = code in _SOURCE_VERDICTS
        if key == "source_not":
            inside = [n for n in named if _named_value(n) in {_named_value(s) for s in subjects}]
            if top and (inside or not named):
                who = "、".join(inside or subjects)
                return _Miss(f"判的主要来源正是{who}" if who else _verdict_miss(result).model,
                             f"判定的主要来源正是{who}" if who else _verdict_miss(result).human)
            return None
        if not top:
            return _verdict_miss(result)
        wanted = {_named_value(s) for s in subjects}
        stray = [n for n in named if _named_value(n) not in wanted]
        pull = hit.side == "pull"
        if stray and pull:
            # 拖累、拉动、带动：点名的段不在主要来源里，变化方向对、占总变化过半的也算（整体上涨、华东下跌拖累了涨幅）
            main = _decimal((result.get("settings") or {}).get("main_share")) or Decimal("0.5")
            stray = [n for n in stray if not ((share := _segment_change(result, n)[1]) is not None
                                              and abs(share) >= main)]
        subject = "、".join(subjects)
        if stray:
            return _Miss(f"判的主要来源是{subject}，不是{'、'.join(stray)}",
                         f"判定的主要来源是{subject}，不是{'、'.join(stray)}")
        if not named and hit.side in _NAMING_SIDES and not phrased \
                and not ((effect := _EFFECTS.get(str(code))) and effect.search((spans or (names,))[0])):
            # 说的来源不是拆解里的段（「主要来自线上渠道」挂了按地区的拆解、「主要来自新客流失」），当没点名
            part = "因子" if result.get("mode") == "factor" else "段"
            return _Miss(f"判的主要来源是{subject}，这句说的来源不是这份拆解里的{part}",
                         f"判定的主要来源是{subject}，句子里说的来源不是这份拆解里的{part}")
        if pull and hit.direction:
            for name in named or subjects:
                change, _ = _segment_change(result, name)
                if change is None or change == 0:
                    continue
                if (change > 0) != (hit.direction == "up"):
                    moved = "上升" if change > 0 else "下降"
                    return _Miss(f"里{name}的变化是{moved}，不能说「{hit.word}」",
                                 f"其中{name}的变化是{moved}，与「{hit.word}」的方向相反")
        return None
    if key == "dispersed":
        return None if code == "dispersed" else _verdict_miss(result)
    if key == "offset":
        # 抵消记在 offset 里；判定为基本持平的，判定语本身就说了此消彼长（不一定再接 offset），各段有涨有跌就算
        if "offset" in _caveats(result) or (code == "flat" and _both_ways(result)):
            return None
        return _Miss("没有记下此消彼长（各段的变化方向大多一致）", "这份结论没有记下各段此消彼长")
    if key == "mix":
        if result.get("mode") != "ratio":
            done, wanted = method_label("decompose", result.get("mode")), method_label("decompose", "ratio")
            return _Miss(f"是{done}，说明不了结构变化：结构变化要挂{wanted}",
                         f"这是{done}，说明不了结构变化：结构变化要挂{wanted}")
        return None if code in ("mix_driven", "both") else _verdict_miss(result)
    if key == "anomaly":
        if code not in ("outside_band", "far_outside_band"):
            return _verdict_miss(result)
        return _direction_miss(hit, verdict.get("direction"))
    if key == "record":
        caveats = _caveats(result)
        wanted = {"up": {"record_high"}, "down": {"record_low"}}.get(hit.direction or "", {"record_high", "record_low"})
        if caveats & wanted:
            return None
        return _Miss("没有记下本期是历史最高或最低", "这份结论没有记下本期是历史最高或最低")
    if key == "normal":
        return None if code == "within_band" else _verdict_miss(result)
    if key == "trend":
        if hit.run:
            return _run_miss(hit, result)
        if code not in ("rising", "falling"):
            return _verdict_miss(result)
        return _direction_miss(hit, "up" if code == "rising" else "down")
    if key == "no_trend":
        return None if code == "no_trend" else _verdict_miss(result)
    if key == "differ":
        if code != "differs":
            return _verdict_miss(result)
        subjects = _subjects(result)
        direction = verdict.get("direction")
        if len(subjects) == 2 and hit.direction and direction in ("up", "down"):
            # 判定语的写法是「A 的…高于 B」：A 是 subjects 的第一个。句子里两组都点名了才核方向，后点名的先写就反过来
            flat = _same_length_norm(names)
            spots = [flat.find(_named_value(s) or "\uffff") for s in subjects]
            if all(at >= 0 for at in spots):
                expected = direction if spots[0] < spots[1] else ("down" if direction == "up" else "up")
                if expected != hit.direction:
                    higher, lower = (subjects if direction == "up" else subjects[::-1])
                    return _Miss(f"判的是{higher}高于{lower}，方向写反了", f"判定是{higher}高于{lower}，方向写反了")
        return None
    if key == "no_diff":
        return None if code == "no_evidence" else _verdict_miss(result)
    return None


def _run_miss(hit: Hit, result: Mapping[str, Any]) -> _Miss | None:
    """「连续 N 期上升」「三连降」：按趋势结论记的末尾连续同向核（values 的 run_direction、run_length），不看整体的趋势
    判定——判定语在方向相反时写「但最近已连续 N 期上升」。期数不少于句子写的 N（没写的至少 3 期），方向一致。没记
    run_direction 的老结论按整体的趋势方向。"""
    values = result.get("values") or {}
    run = _value(result, "run_length")
    need = hit.count if hit.count is not None and hit.count >= 2 else 3
    if run is None or run < need:
        shown = "没有记下" if run is None else f"只有 {run} 期"
        return _Miss(f"末尾连续同向的期数{shown}，不够说连续 {need} 期", f"末尾连续同向的期数{shown}，不够说连续 {need} 期")
    ran = _run_direction(values.get("run_direction"))
    if ran is None and "run_direction" not in values:
        ran = {"rising": "up", "falling": "down"}.get(str(_verdict(result).get("code")))
    if hit.direction and ran != hit.direction:
        if ran is None:
            return _Miss("没有记下末尾连续变化的方向", "这份结论没有记下末尾连续变化的方向")
        return _Miss(f"末尾连续{_dir_word(ran)}，不是连续{_dir_word(hit.direction)}",
                     f"末尾连续{_dir_word(ran)}，与「{hit.word.replace(chr(0), '')}」的方向相反")
    return None


def _direction_miss(hit: Hit, direction: Any) -> _Miss | None:
    if hit.direction and direction in ("up", "down") and hit.direction != direction:
        return _Miss(f"判的方向是{_dir_word(direction)}，不是{_dir_word(hit.direction)}",
                     f"判定的方向是{_dir_word(direction)}，与「{hit.word}」相反")
    return None


# --------------------------------------------------------------------------
# 违规
# --------------------------------------------------------------------------

#: 没挂对分析结论时交回写作者的「那就怎么写」
_FALLBACK = {
    "decompose": "只写各段各自的变化，不下「主要」「集中」「拖累」「分散」这类判断",
    "mix": "只写比率和各段的变化，不说结构变化",
    "baseline": "只写本期的数和变化，不说「异常」「大幅」「显著下降」「激增」「历史新高」「正常波动」这类判断",
    "trend": "只写各期的数，不说「持续」「趋势」「连续」这类判断",
    "differ": "只写各组各自的数，不说「显著高于」「明显高于」「差异不大」这类判断",
}


def _op_text(claim: Claim) -> str:
    """这一类要挂的分析怎么叫：算子、模式的叫法从契约取（结构变化是「变化拆解（比率）」）。"""
    return method_label(claim.op, claim.mode) if claim.mode else OP_LABELS.get(claim.op or "", claim.op or "")


def _snippet(shown: str, start: int, end: int, limit: int = 16) -> str:
    """判断词连同它后面半句（给人看的样子），最多 limit 个字。"""
    right = _clause(shown, start, end)[1]
    piece = shown[start:max(end, min(right, start + limit))].replace("\x00", "").strip()
    return piece or shown[start:end]


def _uncited(claim: Claim, word: str, available: Mapping[str, list[str]]) -> _Reason:
    op_text = _op_text(claim)
    pool = available.get(f"{claim.op}:{claim.mode}" if claim.mode else claim.op or "", [])
    fallback = _FALLBACK["mix" if claim.mode == "ratio" else claim.op or "decompose"]
    if pool:
        model = (f"「{word}」要挂{op_text}的结论：照它的判定写、句末挂 [[see:{pool[0]}]]，或者直接引用判定 [[a:{pool[0]}]]；"
                 f"判定和你想写的不一样就照判定写，否则{fallback}")
    else:
        model = f"这次没有{op_text}的结论，不能写「{word}」：{fallback}"
    return _Reason(f"「{word}」是分析判断，这句没有挂{op_text}的分析结论，系统无法核对", model)


def _mismatch(alias: str, snippet: str, miss: _Miss) -> _Reason:
    return _Reason(f"「{snippet}」与所挂的分析结论 {alias} 对不上：{miss.human}",
                   f"{alias} {miss.model}，不能写「{snippet}」：照判定改写（或者直接引用判定 [[a:{alias}]]），"
                   "或者删掉这个判断")


def _causal(snippet: str) -> _Reason:
    return _Reason(f"「{snippet}」是原因判断，这句没有知识库原话或运行输入作依据，也没有写成推测",
                   f"「{snippet}」没有依据：数据只能说明变化来自哪里；要写原因，引用知识库原话，或写成「可能与…有关，待核实」")


def _object(sentence: Sentence, hit: Hit, left: int, right: int) -> tuple[str, ...]:
    """来源类判断说的那一边（点名用的样子 names），近的在前：「主要来自」「集中在」后面到分句末尾，「以…为主」判断词里，
    「主导」「贡献最大」前面——同一分句里，再往前一个个分句（「线上渠道表现突出，贡献最大」）；拖累、拉动、带动和别的类看
    整个分句。"""
    names = sentence.names
    if hit.side == "after":
        return (names[hit.start:right],)
    if hit.side == "inside":
        return (names[hit.start:hit.end],)
    if hit.side != "before":
        return (names[left:right],)
    spans = [names[left:hit.end]]
    cut = left - 1
    while cut > 0:
        prev, _ = _clause(sentence.text, cut, cut)
        spans.append(names[prev:cut])
        cut = prev - 1
    return tuple(spans)


def _cause(sentence: Sentence, hit: Hit, left: int, right: int) -> str:
    """因果词说的原因（点名用的样子 names）：「由于、因为、原因是、源于、得益于」后面到分句末尾，「受…影响」中间，「导致、
    造成、引起、所致、是…的主要原因」前面到分句开头——前面有「由」的（「由华东造成」）从「由」后面起。"""
    names = sentence.names
    if hit.side == "after":
        return names[hit.end:right]
    if hit.side == "inside":
        return names[hit.start:hit.end]
    by = [m.end() for m in re.finditer(r"由(?!于)", sentence.text[left:hit.start])]
    return names[left + by[-1]:hit.start] if by else names[left:hit.start]


#: 原因只是指代（「以上是下降的主要原因」「原因如下：」后面的列表各自核）：这一句不核
_POINTER = re.compile(r"(?:这|那|此|其|以上|上述|前述|如下|以下|下列)(?:些|个|几个|几点|两点|方面|原因|因素)?")
_FILLING = re.compile(r"[\x00\s\W_]|的|都|也|就|主要|正是|便是|即")


def _said_cause(cause: str) -> bool:
    """原因说了实在的东西：去掉标点、空白、虚词之后不是空的，也不只是指代。"""
    rest = _FILLING.sub("", cause)
    return bool(rest) and _POINTER.fullmatch(rest) is None


def check_sentence(sentence: Sentence, results: Mapping[str, Mapping[str, Any]], *,
                   available: Mapping[str, list[str]] | None = None, version: int | None = None) -> list[dict[str, Any]]:
    """一句话的说法违规。results 是写作目录里的分析结论 {A 编号: 结论}，available 是 {op: [这次有的 A 编号]}（没挂时
    告诉写作者可以挂哪一份），不给就按 results 算。"""
    lex = lexicon(version)
    if available is None:
        available = _available(results)
    hits = scan(sentence.text, version=lex.version)
    if not hits:
        return []
    mine = [(a, results[a]) for a in dict.fromkeys(sentence.analyses) if a in results]
    #: 这句挂的拆解（原因点名的是它的段就按来源核），和目录里所有的拆解（没挂时认原因是不是哪份拆解的段）
    decomposes = [r for _, r in mine if r.get("op") == "decompose"]
    known = [r for r in results.values() if r.get("op") == "decompose"]
    out: list[dict[str, Any]] = []
    #: 同一分句里同一类只报一处（「由于促销结束导致下降」是一处没依据的原因，不是两处）
    seen: set[tuple[str, str, int]] = set()
    guessed = lex.guess.search(sentence.text) is not None
    for hit in hits:
        claim = lex.claim(hit.claim)
        left, right = _clause(sentence.text, hit.start, hit.end)
        names = sentence.names[left:right]
        snippet = _snippet(sentence.shown, hit.start, hit.end)
        spans = _object(sentence, hit, left, right)
        if claim.key == "causal":
            clause = sentence.text[left:right]
            if sentence.quoted or sentence.inputs or guessed or lex.method.search(clause):
                continue
            cause = _cause(sentence, hit, left, right)
            if not _said_cause(cause):
                continue            # 「以上是下降的主要原因」「主要原因：」：原因在别处，各自核
            if any(_named(r, cause) for r in decomposes):
                # 原因点名的是所挂拆解的段或因子（「下降主要由华东造成」）：按来源核，这是记账意义上的「来自」
                hit, claim, spans = replace(hit, claim="source", side="cause"), lex.claim("source"), (cause,)
            elif not decomposes and (not known or any(_named(r, cause) for r in known)) \
                    and any(h is not hit and h.claim in _SOURCE_KEYS and left <= h.start < right for h in hits):
                continue            # 没挂拆解：并进同一分句里的来源判断，那一处报没挂拆解
            else:
                key = (CAUSAL, claim.key, left)
                if key not in seen:
                    seen.add(key)
                    out.append(_record(CAUSAL, _causal(snippet), sentence, hit, claim, mine))
                continue
        candidates = [(a, r) for a, r in mine if r.get("op") == claim.op]
        if not candidates:
            key = (UNCITED, claim.key, left)
            if key not in seen:
                seen.add(key)
                out.append(_record(UNCITED, _uncited(claim, hit.word, available), sentence, hit, claim, mine))
            continue
        phrased = {alias for at, _, alias in sentence.phrases if left <= at < right}
        misses = [(a, _check(lex, hit, r, names, spans=spans, phrased=a in phrased)) for a, r in candidates]
        if any(miss is None for _, miss in misses):
            continue
        alias, miss = misses[0]
        key = (MISMATCH, claim.key, left)
        if key not in seen:
            seen.add(key)
            out.append(_record(MISMATCH, _mismatch(alias, snippet, miss), sentence, hit, claim, mine))  # type: ignore[arg-type]
    return out


def _record(code: str, message: _Reason, sentence: Sentence, hit: Hit, claim: Claim,
            mine: list[tuple[str, Mapping[str, Any]]]) -> dict[str, Any]:
    span = [sentence.base + hit.start, sentence.base + hit.end] if sentence.base is not None else None
    segment = next((sid for s, e, sid in sentence.pieces if s <= hit.start < e), None)
    shown = sentence.shown.replace("\x00", "").strip()
    return _violation(code, message, text=hit.word, span=span, context=shown[:200] or None,
                      unit=sentence.unit, segment=segment, claim=claim.key, op=claim.op,
                      **({"mode": claim.mode} if claim.mode else {}), analyses=[a for a, _ in mine])


def _available(results: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """{op: [这次有的 A 编号]}，另按「op:mode」再记一份（结构变化要比率拆解）。只收做成了的：没挂时不教写作者去挂一份
    算不了的结论。"""
    out: dict[str, list[str]] = {}
    for alias, result in results.items():
        if result.get("status") != "ok":
            continue
        op = str(result.get("op") or "")
        out.setdefault(op, []).append(alias)
        if result.get("mode"):
            out.setdefault(f"{op}:{result['mode']}", []).append(alias)
    return out


# --------------------------------------------------------------------------
# 报告文档
# --------------------------------------------------------------------------


def results_of(catalog: Mapping[str, Any], load: Any = None) -> dict[str, dict[str, Any]]:
    """写作目录里的分析结论：{A 编号: 结论}（contract.catalog_result 读）。条目只带摘要、不带整份结论的（存进报告文档的
    目录就是这样）按 load(编号, 条目) 取回（evidence 的 _Snapshots.analysis：按工件取、复验哈希）；没给 load、取不回的
    不在里面。"""
    out: dict[str, dict[str, Any]] = {}
    for alias, entry in catalog.items():
        if not isinstance(entry, dict) or entry.get("kind") != LEDGER_KIND:
            continue
        result = catalog_result(entry)
        if result is None and load is not None:
            result = load(str(alias), entry)
        if isinstance(result, dict):
            out[str(alias)] = dict(result)
    return out


def _sentence(unit: dict[str, Any], aliases: Iterable[str], results: Mapping[str, Any]) -> Sentence | None:
    """文档里的一个句子单元 → Sentence。写作者自己写的字（文字片段、没有出处的数字）留在 text；引用渲染出来的值、表名
    字段名留在 names（点名用）；判定语（a:）、知识库原话（q:）、排版符号两边都涂掉，判定语另记下位置（phrases）。"""
    segments = [s for s in unit.get("segments") or [] if isinstance(s, dict)]
    if not segments:
        return None
    scan_parts: list[str] = []
    name_parts: list[str] = []
    shown_parts: list[str] = []
    pieces: list[tuple[int, int, str | None]] = []
    phrases: list[tuple[int, int, str]] = []
    quoted = False
    pos = 0
    for seg in segments:
        text = str(seg.get("text") or "")
        kind, ref = seg.get("kind"), str(seg.get("ref") or "")
        head = ref.partition(":")[0].strip() if ref else ""
        blank = "\x00" * len(text)
        if kind == "structural":
            scan_parts.append(blank)
            name_parts.append(blank)
        elif head in ("a", "q"):
            scan_parts.append(blank)
            name_parts.append(blank)
            quoted = quoted or (head == "q" and seg.get("state") == "deterministic")
            if head == "a" and (phrase := _PHRASE_REF.match(ref)):
                phrases.append((pos, pos + len(text), phrase.group(1)))
        elif ref or kind == "entity":
            scan_parts.append(blank)
            name_parts.append(text)
        else:
            scan_parts.append(text)
            name_parts.append(text)
        shown_parts.append(blank if kind == "structural" else text)
        pieces.append((pos, pos + len(text), seg.get("id")))
        pos += len(text)
    span = segments[0].get("span")
    base = span[0] if isinstance(span, list) and span and isinstance(span[0], int) else None
    cited = list(dict.fromkeys(str(a) for a in aliases))
    return Sentence(text="".join(scan_parts), names="".join(name_parts), shown="".join(shown_parts),
                    analyses=[a for a in cited if a in results], quoted=quoted,
                    inputs=any(a.startswith("i:") for a in cited), base=base, unit=unit.get("id"), pieces=pieces,
                    phrases=phrases)


def check_doc(doc: dict[str, Any], catalog: Mapping[str, Any], *, cited: Mapping[int, list[str]] | None = None,
              version: int | None = None, load: Any = None) -> list[dict[str, Any]]:
    """一份报告文档的说法违规。cited 是 {id(unit): 这句按重新解析的引用算的依据}（verify_doc 交进来），不给就用文档里
    记的 cites；load 取回只带摘要的条目的整份结论（results_of）。标题、代码块、表格（表头和正文的格）不核。"""
    results = results_of(catalog, load)
    available = _available(results)
    out: list[dict[str, Any]] = []
    for block, unit in iter_units(doc):
        if block.get("type") in ("heading", "code", "table") or unit.get("kind") in ("heading", "code") \
                or unit.get("loc") is not None:
            continue
        aliases = cited.get(id(unit), []) if cited is not None else unit.get("cites") or []
        sentence = _sentence(unit, aliases, results)
        if sentence is not None:
            out.extend(check_sentence(sentence, results, available=available, version=version))
    return out


def record(mode: str) -> dict[str, Any]:
    """报告文档里记下的那一项：按哪种处置、哪一版说法表核。"""
    return {"mode": mode, "lexicon": LEXICON_VERSION}


def recorded(doc: Any) -> tuple[str | None, int | None] | None:
    """文档记下的 (处置, 说法表版本)：没有这一项（老文档、报告节点关掉了核对）返回 None；记了认不出的值，认不出的那一样
    为 None。"""
    if not isinstance(doc, dict) or DOC_FIELD not in doc:
        return None
    raw = doc.get(DOC_FIELD)
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode") if raw.get("mode") in MODES else None
    version = raw.get("lexicon") if type(raw.get("lexicon")) is int and raw.get("lexicon") in LEXICONS else None
    return mode, version


def verify(doc: Any, catalog: Mapping[str, Any], *, cited: Mapping[int, list[str]] | None = None,
           load: Any = None) -> list[dict[str, Any]] | None:
    """复核一份文档的说法（evidence.verify_doc 调）：没记这一项的文档返回 None（不核，老文档复核结果不变）；记了的按记下
    的那一版说法表核。记了认不出的处置、版本的另记一条 bad_schema（文档可能被改过），照当前版核。"""
    found = recorded(doc)
    if found is None:
        return None
    mode, version = found
    out: list[dict[str, Any]] = []
    if mode is None or version is None:
        out.append(_violation("bad_schema", f"报告文档记录的分析说法核对规则「{doc.get(DOC_FIELD)}」无法识别"))
    return out + check_doc(doc, catalog, cited=cited, version=version, load=load)


def counts(violations: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """三种说法违规各几处（只数有的）。"""
    out: dict[str, int] = {}
    for v in violations:
        if v.get("code") in CODES:
            out[str(v["code"])] = out.get(str(v["code"]), 0) + 1
    return out


def summary(violations: list[Mapping[str, Any]]) -> str:
    """几处说法违规怎么说：「1 处判断没挂分析结论、2 处与所挂结论的判定对不上、1 处原因没有依据」。"""
    said = {UNCITED: "判断没挂分析结论", MISMATCH: "与所挂结论的判定对不上", CAUSAL: "原因没有依据"}
    found = counts(violations)
    return "、".join(f"{found[code]} 处{said[code]}" for code in (UNCITED, MISMATCH, CAUSAL) if code in found)


# --------------------------------------------------------------------------
# 写作规则（给模型看）
# --------------------------------------------------------------------------

#: 写作目录里有分析结论时，拼在写作规则后面
ANALYSIS_RULES_PROMPT = (
    "分析结论（系统会逐句核对判断词）：\n"
    "- 「主要来自」「集中在」「拖累」「带动」「分散」「异常」「大幅」「激增」「历史新高」「正常波动」「持续上升」「趋势」"
    "「显著高于」「差异不大」这类判断，只能照证据目录里分析结论的判定写，句末挂对应的依据，比如 [[see:A1]]；最好直接引用"
    "判定语 [[a:A1]]，它由系统按判定渲染。来源、集中、拖累、分散挂变化拆解，结构变化挂比率拆解，异常、大幅、显著上升或下降、"
    "新高新低、正常波动挂波动基线，持续、趋势、连续挂趋势，显著高于、明显高于、差异不大挂差异检验。\n"
    "- 判定和你想写的不一样时照判定写；写来源要点名是哪一段，点名的地区、渠道要在判定点名的段里；没有做成的分析照实写"
    "没做成的原因，不下这类判断。"
    "没有对应的分析结论，就只陈述数字和变化。\n"
    "- 拆解只说明变化来自哪里，不说明为什么。写原因（因为、由于、导致、受…影响）要有知识库原话或运行输入作依据，"
    "否则写成待核实的推测，比如「可能与促销结束有关，待核实」。"
)
#: 写作目录里没有分析结论时，拼在写作规则后面
ANALYSIS_NONE_PROMPT = (
    "判断词（系统会逐句核对）：这次没有分析结论，不要写「主要来自」「集中在」「拖累」「带动」「分散」「异常」「大幅」"
    "「激增」「历史新高」「正常波动」「持续上升」「趋势」「显著高于」「差异不大」这类判断，只陈述数字和变化（各段各自是多少、"
    "变了多少）。写原因（因为、由于、导致、受…影响）要有知识库原话或运行输入作依据，否则写成待核实的推测，"
    "比如「可能与促销结束有关，待核实」。"
)
#: 重写请求里有说法违规时补的一句
ANALYSIS_REPAIR_PROMPT = ("判断词照分析结论的判定写并挂上依据，没有对应结论的就删掉判断、只陈述数字和变化；"
                          "原因没有依据的写成待核实的推测。")


def rules_prompt(catalog: Mapping[str, Any]) -> str:
    """写作规则里说判断词的那一段：目录里有分析结论和没有时各一种说法。"""
    return ANALYSIS_RULES_PROMPT if results_of(catalog) else ANALYSIS_NONE_PROMPT


__all__ = [
    "ANALYSIS_NONE_PROMPT", "ANALYSIS_REPAIR_PROMPT", "ANALYSIS_RULES_PROMPT", "CAUSAL", "CODES", "DOC_FIELD",
    "LEXICONS", "LEXICON_VERSION", "MISMATCH", "MODES", "UNCITED", "Claim", "Hit", "Lexicon", "Sentence",
    "check_doc", "check_sentence", "counts", "lexicon", "record", "recorded", "results_of", "rules_prompt", "scan",
    "summary", "verify",
]
