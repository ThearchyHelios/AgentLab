"""问题意图：用户问的一句话是不是分析类问题（docs/design/analysis-round/00-总方案.md §3.7）。

助手搭问数据的图时自查（api/copilot_gen.GraphChecker 的 analysis_gaps）用它：问题问的是为什么、怎么变了、哪里拖累、谁贡献
最大、是否异常、正常吗、波动、趋势、持续、是不是真的更高这类，而查库的 Agent 没绑分析工具、图里也没有分析卡时，提一条只挡
一轮的问题，请助手给 Agent 绑上分析工具——变化从哪来、算不算异常、是不是趋势由平台按固定的方法和阈值算，判定语由系统写，
报告引用它。

**确定的词表，带版本**（INTENT_VERSION）：不调模型。只收明确的问法，宁可漏判：漏判只是少一条自查提醒，误判要让助手多改一轮、
给用不上的 Agent 绑分析工具。按句子认（句号、问号、感叹号、分号、换行分开；逗号不分：「销售额下降了，为什么」是一句），
一句里命中一种就算，先认先得（原因 → 来源 → 异常 → 波动 → 趋势 → 差异）。各类收什么、不收什么：

- 原因（why）：「为什么 / 为何 / 为啥 / 怎么会」要和涨跌、变化、这么高、比…低这类说法在同一句（「为什么报错」「为什么查不到
  数据」不算）；「原因」要是变化的原因（「下降的原因」「什么原因导致订单减少」「销售额下滑了，原因是什么」），「退款原因有哪些」
  「按取消原因统计」问的是一个字段，不算
- 来源（source）：「怎么变了 / 有什么变化」「哪里拖累 / 谁拉动 / 什么带动了增长」「贡献最大」「下降主要来自哪」「驱动因素」
  「把变化拆开」「归因分析」。「订单来自哪些渠道」问的是构成，不算（要先说变化）；「表结构有什么变化」「口径有什么变化」问的
  不是数，不算
- 异常（anomaly）：「是否异常 / 异常吗 / 正常吗 / 算不算异常 / 有没有异动 / 是不是暴跌了」这类问句，和「超出正常范围」。
  「异常订单有多少」「有没有异常记录」「门店是否正常营业」问的是业务状态，「系统正常吗」「连接正常吗」问的是服务，都不算
- 波动（fluctuation）：「波动大吗 / 变化大不大 / 是不是正常波动 / 只是波动」。「列出价格波动超过 10% 的商品」是筛选，「网络波动大吗」问的
  是服务，都不算
- 趋势（trend）：「趋势 / 走势」「持续上升 / 下降会持续吗 / 一直在降 / 连续三周下降 / 越来越少 / 是不是在涨 / 稳步增长」。
  「活动会持续几天」「促销还在持续吗」不算（「持续」前后要有涨跌）
- 差异（differ）：「是不是真的更高 / 真的比华南高吗」「差异显著吗 / 有没有明显差别」「是不是偶然」「统计上显著」

英文只收几种明确的问法（why … drop、what drove、is this normal、trend、significantly higher……），同样宁可漏判。

意图判的是用户的问题，不是写作者的句子：说法核对的说法表（engine/analysis_claims）另是一套，两边不共用词表——写作者写
「主要来自华东」是下判断，用户问「主要来自哪」是在问。

整个模块是纯函数，不碰数据库、不调模型。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: 词表的版本：加减词、改规则就升版本（评测比两次的提示词时据此认得出自查规则变了）
INTENT_VERSION = 1

#: 意图的种类 → 给人看的叫法（自查的那条问题里说「问题问的是……」）
KINDS: dict[str, str] = {
    "why": "原因", "source": "变化从哪来", "anomaly": "是否异常", "fluctuation": "波动大不大", "trend": "趋势",
    "differ": "差异站不站得住",
}


@dataclass(frozen=True)
class Intent:
    """一句问题的分析意图：kind 是 KINDS 之一，text 是问题里认出的那几个字（给人看、给助手看）。"""

    kind: str
    text: str
    version: int = INTENT_VERSION

    @property
    def label(self) -> str:
        return KINDS[self.kind]


# --------------------------------------------------------------------------
# 词表 v1（正则都写在 re.compile 里：拼词表的常量只放用「|」连起来的词）
# --------------------------------------------------------------------------

#: 涨跌和变化（用户问题里的说法）。单字的「升」「降」不收：「升级」「降级」不是变化
_CHANGE = ("上升|下降|增长|下滑|减少|增加|提升|提高|降低|上涨|下跌|回落|回升|走低|走高|反弹|萎缩|下行|上行|缩水|"
           "变化|变动|波动|变了|变多|变少|多了|少了|高了|低了|大了|小了|好转|恶化|变好|变差|暴跌|激增|骤降|飙升|大涨|"
           "大跌|突增|突降|翻倍|腰斩|涨|跌")
_WHY = "为什么|为何|为啥|怎么会|咋会|怎么就|怎么突然"
#: 「异常」后面是这些名词的，说的是业务状态（「异常订单」「有没有异常记录」）
_STATUS_NOUN = "订单|记录|单据|单|数据|状态|交易|用户|账户|账号|日志|值|码|件|商品|门店|设备|情况|信息|事件|标记|原因"
#: 「正常」后面是这些的，说的是在不在营业、能不能用（「门店是否正常营业」）
_STATUS_VERB = "营业|运行|运转|工作|使用|登录|发货|开放|上线|启动|显示|访问|打开|连接"

#: 句子的边界：逗号不分（「销售额下降了，为什么」是一句）
_SENTENCE = re.compile(r"[。；;！!？?\n]|\.(?=\s|$)")
#: 一句里有这些就不是在问数：服务、配置、结构、口径本身（「系统正常吗」「表结构有什么变化」「口径有什么变化」）。只管标了
#: data_only 的那几条规则
_NOT_DATA = re.compile(r"系统|服务器|连接|接口|网络|进程|部署|配置|设置|权限|代码|界面|流程|工作流|表结构|字段|列名|"
                       r"口径|定义|报错|出错")

#: 原因一类：问原因的词和变化、高低的说法在同一句（_why）
_WHY_RE = re.compile(_WHY)
_WHY_WHAT = re.compile(rf"{_CHANGE}|(?:这么|那么|如此|这样|偏|特别|格外|明显)(?:高|低|多|少|大|小|差|好)"
                       r"|比.{1,16}?(?:高|低|多|少|大|小|好|差)|差(?:这么|那么)多|差距(?:这么|那么)?大")
#: 「原因是什么」：同一句前面要说过变化（「销售额下滑了，原因是什么」）
_REASON_ASK = re.compile(r"原因(?:是什么|是啥|有哪些|在哪|何在|为何)")
_CHANGE_RE = re.compile(_CHANGE)

#: (种类, 规则, data_only：这一句有 _NOT_DATA 时不认)
_RULES: list[tuple[str, re.Pattern[str], bool]] = [
    ("why", re.compile(rf"(?:{_CHANGE}).{{0,6}}?的?(?:主要)?原因"), False),
    ("why", re.compile(rf"(?:什么|啥|哪些)原因.{{0,12}}?(?:{_CHANGE})"), False),
    ("source", re.compile(r"归因分析|做(?:个|一下|下)?归因|归因一下|(?:怎么|如何)归因|驱动(?:因素|力)"
                          r"|(?:由|靠)(?:什么|哪些|哪个).{0,4}?驱动"), False),
    ("source", re.compile(r"怎么变(?:了|的|化)|如何变化|有(?:什么|啥|哪些)变化|变化(?:在哪|如何|怎么样|情况)"), True),
    ("source", re.compile(r"(?:哪|谁|什么).{0,10}?(?:拖累|拉低|拉高|拉动|带动|推动|贡献)"
                          r"|(?:拖累|拉动|带动|推动|贡献)了?.{0,6}?(?:最大|最多|最明显)"), False),
    ("source", re.compile(rf"对.{{0,12}}?(?:{_CHANGE}|增量|增幅|降幅).{{0,2}}?贡献"), False),
    ("source", re.compile(rf"(?:{_CHANGE}).{{0,12}}?(?:主要)?(?:来自|来源于|出在|集中在|源于|发生在)(?:哪|什么|何)"),
     False),
    ("source", re.compile(rf"(?:哪|什么).{{0,8}}?(?:导致|造成|引起|带来)了?.{{0,12}}?(?:{_CHANGE})"), False),
    ("source", re.compile(rf"(?:拆解|拆分|分解|拆开).{{0,12}}?(?:{_CHANGE})|(?:{_CHANGE}).{{0,12}}?(?:拆解|拆分|分解|拆开)"),
     False),
    ("anomaly", re.compile(rf"(?:是否|是不是|有没有|算不算|算是|属于)(?:出现)?了?(?:异常|反常|不正常)(?!{_STATUS_NOUN})"),
     True),
    ("anomaly", re.compile(rf"(?:异常|反常)(?!{_STATUS_NOUN})(?:吗|么|与否)|异常不异常"
                           rf"|(?:正常|不正常)(?!{_STATUS_VERB})(?:吗|么|与否)|正不正常"
                           rf"|(?:是否|是不是|算不算|属于)正常(?!{_STATUS_VERB}|的?波动)"), True),
    ("anomaly", re.compile(r"(?:超出|超过|偏离)了?(?:正常|常规|历史)的?(?:范围|水平|波动|区间)"
                           r"|(?:有没有|是否有|是不是)异动|异动分析|异动吗"), True),
    ("anomaly", re.compile(r"(?:是否|是不是|算不算|有没有).{0,6}?(?:大幅|骤降|暴跌|激增|飙升|突增|突降|大涨|大跌)"), True),
    ("fluctuation", re.compile(r"(?:波动|变化)(?:大吗|大么|大不大|算大|有多大)|波动(?:正常吗|正不正常)"
                               r"|(?:正常|常规|一般|季节性|随机|自然)的?波动"
                               r"|(?:只是|仅仅是|是不是|是否|算不算|属于|算是)(?:正常|常规)?的?波动"), True),
    ("trend", re.compile(r"趋势|走势"), False),
    ("trend", re.compile(r"持续(?:地)?(?:上升|下降|增长|下滑|走高|走低|上涨|下跌|减少|增加|回落|萎缩|改善|恶化|变好|变差|"
                         r"走强|走弱|攀升)"), False),
    ("trend", re.compile(rf"(?:{_CHANGE}).{{0,12}}?(?:是否|是不是|会不会|还会|还在|一直在?|会)持续"), False),
    ("trend", re.compile(r"一直(?:在)?(?:涨|跌|上升|下降|增长|下滑|减少|增加|走高|走低|攀升)"
                         r"|连续.{0,6}?(?:周|月|天|日|期|季度|年).{0,4}?(?:上升|下降|增长|下滑|涨|跌|减少|增加|走高|走低)"
                         r"|越来越(?:高|低|多|少|好|差|大|小)"
                         r"|(?:是不是|是否|有没有)(?:一直)?在(?:涨|跌|上升|下降|增长|下滑|减少|增加)"
                         r"|(?:逐步|稳步|节节)(?:上升|下降|增长|下滑|攀升|走高|走低|回落)"), False),
    ("differ", re.compile(r"(?:是不是|是否)真的(?:更|比较)?(?:高|低|多|少|好|差|大|小)"
                          r"|真的(?:更)?(?:高|低|多|少|好|差)(?:吗|么)|真的比.{1,16}?(?:高|低|多|少|好|差|大|小)"
                          r"|(?:差异|差别|区别|差距)(?:是否|是不是)?(?:显著|明显|站得住|有意义|真实存在)"
                          r"|有没有(?:显著|明显|统计学?上?)的?(?:差异|差别|区别)"
                          r"|(?:差异|差别|区别)(?:大吗|大不大|明显吗|显著吗)"
                          r"|显著(?:高于|低于|不同|差异|更高|更低)|统计学?上?(?:显著|有意义)"
                          r"|(?:是不是|是否|会不会)(?:只是)?(?:偶然|巧合|随机|样本(?:波动|太少|太小))|置信区间"), False),
]

#: 英文：只收几种明确的问法
_EN_CHANGE = (r"drop|dropped|fall|fell|decline|declined|decrease|decreased|rise|rose|increase|increased|grow|grew|"
              r"change|changed|higher|lower|spike|spiked|dip|dipped|jump|jumped|surge|surged|plunge|plunged|so (?:high|low)")
_EN_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("why", re.compile(rf"\bwhy\b.{{0,60}}?\b(?:{_EN_CHANGE})\b|\b(?:{_EN_CHANGE})\b.{{0,40}}?\bwhy\b", re.I)),
    ("source", re.compile(r"\bwhat (?:drove|drives|is driving|caused|explains)\b|\bdrivers? of\b"
                          r"|\bcontribut(?:ed|es|ion)\b.{0,20}\bmost\b|\bwhere did\b.{0,40}\bcome from\b", re.I)),
    ("anomaly", re.compile(r"\b(?:is|was|are|were) (?:this|it|that|these) (?:normal|abnormal|unusual|an anomaly|anomalous)\b"
                           r"|\banomal(?:y|ies|ous)\b|\bout of the ordinary\b", re.I)),
    ("trend", re.compile(r"\btrend(?:s|ing)?\b|\bkeeps? (?:rising|falling|growing|declining|increasing|decreasing)\b"
                         r"|\bconsecutive (?:weeks|months|days)\b", re.I)),
    ("differ", re.compile(r"\bsignificant(?:ly)? (?:higher|lower|different|difference)\b|\bstatistically\b"
                          r"|\breally (?:higher|lower|better|worse)\b", re.I)),
]

#: 认出的那几个字最多留多长（给人看的问题里引一段）
TEXT_MAX = 24


def _quote(text: str) -> str:
    text = text.strip()
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1] + "…"


def _why(sentence: str) -> str | None:
    """问原因：问原因的词和变化、高低的说法在同一句。交回从先出现的那个到后出现的那个的一段。"""
    ask = _WHY_RE.search(sentence)
    what = _WHY_WHAT.search(sentence)
    if ask is None or what is None:
        return None
    return sentence[min(ask.start(), what.start()):max(ask.end(), what.end())]


def _reason(sentence: str) -> str | None:
    """「…下滑了，原因是什么」：问原因的那几个字前面说过变化。"""
    ask = _REASON_ASK.search(sentence)
    if ask is None:
        return None
    what = _CHANGE_RE.search(sentence, 0, ask.start())
    return sentence[what.start():ask.end()] if what is not None else None


def analysis_intent(question: str | None) -> Intent | None:
    """一句问题的分析意图；不是分析类问题（或认不准）为 None。"""
    text = str(question or "").strip()
    for sentence in (s.strip() for s in _SENTENCE.split(text)):
        if not sentence:
            continue
        if found := _why(sentence) or _reason(sentence):
            return Intent("why", _quote(found))
        not_data = _NOT_DATA.search(sentence) is not None
        for kind, rule, data_only in _RULES:
            if data_only and not_data:
                continue
            if (m := rule.search(sentence)) is not None:
                return Intent(kind, _quote(m.group(0)))
        for kind, rule in _EN_RULES:
            if (m := rule.search(sentence)) is not None:
                return Intent(kind, _quote(m.group(0)))
    return None


def is_analytical(question: str | None) -> bool:
    return analysis_intent(question) is not None


__all__ = ["INTENT_VERSION", "KINDS", "Intent", "analysis_intent", "is_analytical"]
