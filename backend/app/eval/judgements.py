"""分析期望：贡献（contribution）、判定（verdict）的算法和评分（docs/design/analysis-round/00-总方案.md §3.8）。

**期望怎么算。** 期望 SQL 在业务库上查一次（gold.py 的 SQL 探针，和别的期望同一份快照），在这份快照上用平台的算子
（app.analysis.run，和作答里的分析工具、分析卡同一个函数）算一次分析，判定就是期望：

- 贡献：变化拆解（可加、比率、乘积），参数同分析工具（按维度 by、期次 period、基期 base、本期 target、度量 measure、分子
  numerator、分母 denominator、乘积 product、因子 factors、叫法 label），另写 top_n（缺省 1）：期望点名主要来源的前几段。
- 判定：波动基线、趋势判断、差异检验、可比性检查，参数同对应的分析工具。

算子要的输入只有这一份快照（编号 GOLD_REF），不写 query、两份快照、总数、下钻。参数里的文字可以带 {{ schedule.* }}，按评测
的截至时间渲染（同期望 SQL）；期望 SQL 只返回两期时可以不写基期、本期：先的一期是基期、后的一期是本期。阈值用这次评测的
设置快照（和作答的运行同一份，runner.py），不写在期望里；算不了的（历史不够、本期不完整……）这一项不能评分，原因照实写。

**判定类**（JUDGEMENTS）：期望和答案里的分析结论都按判定码归到同一套类再比——贡献：source 主要来源（可加的 driver、
concentrated，乘积的 driver）、dispersed 分散、flat 基本持平、mix 主要来自结构变化、rate 主要来自各段自身的变化、mixed
两方面都有影响（比率、乘积的 both）；判定：normal 常规波动（within_band）、anomaly 超出常规波动（outside_band、
far_outside_band，看方向）、rising / falling / no_trend、differs 差异站得住（看哪两组、方向）、no_evidence 不足以下结论、
comparable 可比（comparable、conditional）/ incomparable 不可比。

**评分**（score；沿用五种结论）：
- 对且可追溯：答案引用了（[[a:]]、[[v:A…]]、[[table:A…]]、[[see:A…]]）判定对得上的分析结论（同一个算子，变化拆解还要同一种
  模式；主要来源要点名期望的那几段，方向一致；差异检验比的是同两组）；引用它的那句话里的判断词却和期望对不上的不算。
- 对但没引用：没引用对得上的结论，但正文按说法表（engine/analysis_claims，同一套词表、同一套绑定规则）写对了：说法表认出的
  这一类判断词，拿期望的那份分析结论核对得上（点名的段、方向、连续几期都核）。可比性不进说法表（说法核对不该打回「本周数据
  不完整，不能直接比」这种谨慎的提醒），评分另用一张只给自己用的小词表（_INCOMPARABLE）：期望是不可比的，正文明确说了两期
  不能直接比（不能直接比、不可比、本期不完整、还没过完、只有几天、天数不同、口径不同……；分句里先有否定的、句子里先有假设
  或疑问的、问句都不算）也算；期望是可比的只能靠引用。
- 错：引用的分析判定对不上、正文写的和期望相反（把常规波动说成异常、把上升说成下降、主要来源说成别的段），或者根本没做也
  没说。
- 不能评分：期望算不了。
"""
from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from app.analysis.contract import OP_LABELS, method_label

#: 两种分析期望
ANALYSIS_KINDS = ("contribution", "verdict")
#: 判定期望能用的分析（贡献恒为变化拆解）
VERDICT_OPS = ("baseline", "trend", "differ", "comparable")
#: 判定类，见模块说明。前六个是贡献的，后九个是判定的
JUDGEMENTS = ("source", "dispersed", "flat", "mix", "rate", "mixed",
              "normal", "anomaly", "rising", "falling", "no_trend", "differs", "no_evidence", "comparable",
              "incomparable")
#: 分析的输入在算子里的名字（报错里照它说：「期望 SQL 里没有『地区』这一列」）
GOLD_REF = "期望 SQL"
#: 贡献期望最多点名前几段
TOP_N_MAX = 10
#: 期望里不写的参数：输入就是期望 SQL 这一份快照，没有两份快照、总数、下钻
_NOT_IN_EXPECTATION = frozenset({"query", "base_query", "target_query", "headline", "parent"})
#: 期望 SQL 只返回两期时，这几种分析可以不写基期、本期
_TWO_PERIODS = frozenset({"decompose", "differ", "comparable"})

#: 判定码 → 判定类（按算子、模式）
_JUDGEMENT_OF: dict[tuple[str, str | None], dict[str, str]] = {
    ("decompose", "additive"): {"driver": "source", "concentrated": "source", "dispersed": "dispersed", "flat": "flat"},
    ("decompose", "ratio"): {"mix_driven": "mix", "rate_driven": "rate", "both": "mixed", "flat": "flat"},
    ("decompose", "factor"): {"driver": "source", "both": "mixed", "flat": "flat"},
    ("baseline", None): {"within_band": "normal", "outside_band": "anomaly", "far_outside_band": "anomaly"},
    ("trend", None): {"rising": "rising", "falling": "falling", "no_trend": "no_trend"},
    ("differ", None): {"differs": "differs", "no_evidence": "no_evidence"},
    ("comparable", None): {"comparable": "comparable", "conditional": "comparable", "incomparable": "incomparable"},
}
#: 判定类要看方向的：主要来源、结构或自身为主、分散（总变化的涨跌）、超出常规波动（本期变化的涨跌）、差异站得住（A 相对 B）
_DIRECTED = frozenset({"source", "mix", "rate", "mixed", "dispersed", "anomaly", "differs"})
#: 说法表的类（engine/analysis_claims 的 Claim.key）→ 它说出的判定类；「连续…」「趋势」看方向，见 _said_judgement
_CLAIM_JUDGEMENT = {"source": "source", "dispersed": "dispersed", "mix": "mix", "anomaly": "anomaly", "normal": "normal",
                    "no_trend": "no_trend", "differ": "differs", "no_diff": "no_evidence"}
#: 期望是这一类时，正文里哪几类判断词算说出了它（别的类只看对不对得上：对不上算说反了）
_AFFIRMS: dict[str, frozenset[str]] = {
    "source": frozenset({"source"}), "dispersed": frozenset({"dispersed"}), "mix": frozenset({"source", "mix"}),
    "rate": frozenset({"source"}), "mixed": frozenset({"mix"}), "flat": frozenset(), "normal": frozenset({"normal"}),
    "anomaly": frozenset({"anomaly"}), "rising": frozenset({"trend"}), "falling": frozenset({"trend"}),
    "no_trend": frozenset({"no_trend"}), "differs": frozenset({"differ"}), "no_evidence": frozenset({"no_diff"}),
    "comparable": frozenset(), "incomparable": frozenset(),
}
#: 判断词所在的句子最多留多少字给人看
_SNIPPET_MAX = 40

#: 数字（阿拉伯、全角、汉字）、期次、分句里的字（不跨句读）、比
_N = r"[0-9０-９一二两三四五六七八九十]+"
_PERIOD = "周|月|季|年|期|天|日"
_IN_CLAUSE = r"[^，,；;。！？!?\n]"
_COMPARE = "比较|对比|相比|比"
#: 正文里明确说两期不能直接比的说法：直说不能比的，和说出不能直接比的原因的（本期不完整、还没过完、只有几天、天数不同、
#: 口径不同）。只给评分用，不进说法表（engine/analysis_claims）：说法核对不该打回「本周数据不完整，不能直接比」这种谨慎的
#: 提醒
_INCOMPARABLE = re.compile(
    # 直说不能比：不能直接比、无法直接比较、不宜简单对比、难以比较……「无法比拟」「比不上」「比得上」不是
    rf"(?:不能|不可以|无法|不宜|不应该?|不便|难以|很难|不好|没办法)(?:直接|简单)?地?(?:拿来|放在一起)?"
    rf"(?:{_COMPARE})(?![拟肩]|得上|不上)"
    # 「没法」要带「直接」「简单」，或者说「比较」「对比」：光说「没法比」多半是说差得远
    rf"|没法(?:直接|简单)地?(?:拿来|放在一起)?(?:{_COMPARE})(?![拟肩]|得上|不上)|没法(?:比较|对比|相比)"
    # 不能直接拿本周和上周比
    rf"|(?:不能|无法|不宜|不好|没法)(?:直接|简单)?(?:拿|把|将){_IN_CLAUSE}{{1,12}}?(?:和|跟|与|同){_IN_CLAUSE}{{1,12}}?"
    rf"(?:{_COMPARE})(?![拟肩]|得上|不上)"
    r"|不可比(?!拟)|不具备?可比性|没有可比性|缺乏可比性|可比性(?:不足|不够|存疑|较差|很差)"
    # 原因：本期不完整、还没过完、只有几天、差几天、不满一周、天数不同、口径不同
    rf"|(?:数据|统计|{_PERIOD}){_IN_CLAUSE}{{0,4}}?(?:不完整|不全(?!是))"
    rf"|(?:{_PERIOD}){_IN_CLAUSE}{{0,4}}?(?:还|尚)?(?:没|未)(?:过完|结束|走完)"
    rf"|(?:只有|仅有|才有|只统计了|只算了|只过了|才过了|才过去|才)\s*{_N}\s*(?:天|日)"
    rf"|(?:还差|少了?|相差|差了?)\s*{_N}\s*(?:天|日)"
    r"|(?:不满|不足|不到)\s*(?:一|1)\s*整?(?:周|个?月|季度?|年)"
    r"|(?:天数|期长|时长|长度)也?(?:不同|不一样|不一致|不等|不相等|对不上|不足|不够)"
    r"|口径也?(?:不同|不一样|不一致|变了|有变|有变化|发生了?变化|调整过?|改过|改了|有调整)|(?:换了|改了|调整了)(?:统计)?口径"
)
#: 可比性的说法前面（同一分句里）有否定的不算说了：「不是不能比」「并非不可比」
_INCOMPARABLE_DENIED = re.compile(r"不是|并非|并不|未必|不一定")
#: 句子里在它前面有假设、疑问的不算说了：「如果口径不同，就不能直接比」「是不是不能直接比」
_INCOMPARABLE_SUPPOSED = re.compile(r"如果|假如|要是|倘若|假设|若是|是否|是不是|能不能|有没有")


class ParamsInvalid(ValueError):
    """期望的分析参数写得不对（保存题目时、算期望时都可能）。message 给人看，key 是出错的那个参数。"""

    def __init__(self, message: str, key: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.key = key


# ==========================================================================
# 参数：校验（cases.py 保存题目时）、组成分析请求（gold.py 算期望时）
# ==========================================================================


def param_names(op: str) -> list[str]:
    """期望里这种分析可以写的参数（分析工具的参数去掉输入、总数、下钻），按工具里的先后。"""
    from app.tools.builtin.analysis import param_names as tool_params

    return [k for k in tool_params(op) if k not in _NOT_IN_EXPECTATION]


def op_text(op: str, mode: str | None = None) -> str:
    """「变化拆解（可加）」「波动基线」：叫法只取契约那一份。"""
    return method_label(op, mode) if mode else OP_LABELS.get(op, op)


def template_texts(params: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    """参数里的文字（含列表、成对列表里的）：(参数名, 文字)。{{ schedule.* }} 的校验、渲染按它走。"""
    for key, value in params.items():
        if isinstance(value, str):
            yield key, value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    yield key, item
                elif isinstance(item, list):
                    yield from ((key, x) for x in item if isinstance(x, str))


def check_params(op: str, mode: str | None, params: Any) -> dict[str, Any]:
    """参数的形状：只能写这种分析的参数，不能写阈值，类型照分析工具的参数定义。交回规范化的参数（去掉空值）。不对抛
    ParamsInvalid。列在不在期望 SQL 的结果里要等试算才知道（算子按快照核）。"""
    from pydantic import ValidationError

    from app.analysis.contract import DEFAULTS, LABELS
    from app.tools.builtin.analysis import ARGS

    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise ParamsInvalid("分析参数需要是一个对象")
    allowed = param_names(op)
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in DEFAULTS:
            label = LABELS[key] if LABELS[key].startswith("「") else f"「{LABELS[key]}」"
            raise ParamsInvalid(f"参数里写了阈值{label}：阈值不写在期望里，评测按这次评测的分析设置算（和作答用同一份）",
                                key)
        if key not in allowed:
            raise ParamsInvalid(f"参数里没有「{key}」，{op_text(op)}可以写：{'、'.join(allowed)}", key)
        if value is None or value == "" or value == []:
            continue
        out[key] = copy.deepcopy(value)
    probe = {**out, "query": GOLD_REF, **({"mode": mode} if mode else {})}
    try:
        ARGS[op].model_validate(probe)
    except ValidationError as e:
        error = e.errors()[0]
        key = str((error.get("loc") or ("",))[0])
        field = ARGS[op].model_fields.get(key)
        said = (field.description or "") if field is not None else ""
        verb = "没写" if error.get("type") == "missing" else "写得不对"
        raise ParamsInvalid(f"参数「{key}」{verb}" + (f"：{said}" if said else ""), key) from None
    return out


def _render(value: Any, variables: Mapping[str, str]) -> Any:
    from app.eval.cases import render

    if isinstance(value, str):
        return render(value, variables)
    if isinstance(value, list):
        return [_render(v, variables) for v in value]
    return value


def _two_periods(snapshot: Mapping[str, Any], period: str) -> tuple[Any, Any]:
    """期望 SQL 只返回两期时的基期、本期：按期次先后（periods.parse_compared 认得出的按它，否则按写法）。"""
    from app.analysis import periods

    columns = [str(c) for c in snapshot.get("columns") or []]
    hits = [i for i, c in enumerate(columns) if c == period] or \
        [i for i, c in enumerate(columns) if c.lower() == period.lower()]
    if not hits:
        raise ParamsInvalid(f"期望 SQL 的结果里没有期次列「{period}」（结果的列：{'、'.join(columns) or '（没有）'}）",
                            "period")
    i = hits[0]
    values = list(dict.fromkeys(row[i] for row in snapshot.get("rows") or []
                                if isinstance(row, (list, tuple)) and i < len(row) and row[i] is not None))
    if len(values) != 2:
        raise ParamsInvalid(f"期望 SQL 的期次列「{period}」里有 {len(values)} 期：不写基期、本期时，期望 SQL 只能返回两期"
                            "（先的一期是基期、后的一期是本期）", "base")
    parsed = periods.parse_compared(values, str(snapshot.get("sql") or ""), columns[i], snapshot.get("dialect"))
    if parsed is not None:
        items, _ = parsed
        order = sorted(range(2), key=lambda k: items[k].ordinal)
    else:
        order = sorted(range(2), key=lambda k: str(values[k]))
    # 写成文字交给分析工具的参数（期次是数的也一样：算子按写出来的样子认期次列里的值）
    return str(values[order[0]]), str(values[order[1]])


def request_of(exp: Mapping[str, Any], variables: Mapping[str, str], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """一项分析期望 → 一次分析的请求（contract.AnalysisRequest）：参数按截至时间渲染，输入是期望 SQL 这一份快照；只返回两期
    又没写基期、本期的按两期补上。不对抛 ParamsInvalid。"""
    from pydantic import ValidationError

    from app.tools.builtin.analysis import ARGS, request_of as tool_request

    op, mode = str(exp.get("op")), exp.get("mode")
    params = {k: _render(v, variables) for k, v in (exp.get("params") or {}).items()}
    if op in _TWO_PERIODS and isinstance(params.get("period"), str) and params.get("base") in (None, "") \
            and params.get("target") in (None, ""):
        params["base"], params["target"] = _two_periods(snapshot, params["period"])
    params["query"] = GOLD_REF
    if mode:
        params["mode"] = mode
    try:
        args = ARGS[op].model_validate(params)
    except ValidationError as e:
        key = str((e.errors()[0].get("loc") or ("",))[0])
        raise ParamsInvalid(f"参数「{key}」写得不对", key) from None
    return tool_request(op, args)  # type: ignore[return-value]


# ==========================================================================
# 判定类
# ==========================================================================


def judgement_code(op: Any, mode: Any, code: Any) -> str | None:
    """一份分析结论的判定码 → 判定类；没有对应的（拆解对不上总数）为 None。"""
    key = (str(op), str(mode) if op == "decompose" else None)
    return _JUDGEMENT_OF.get(key, {}).get(str(code)) if code else None


def _segment(subject: Any) -> str:
    """判定点名的一段（维度键是一个列表，两列用「·」连，同判定语的写法）。"""
    from app.analysis.phrases import key_text

    return key_text(list(subject)) if isinstance(subject, (list, tuple)) else str(subject)


def norm(name: Any) -> str:
    """比段名、因子名用的写法：全半角、大小写归一，去掉首尾空白。"""
    return unicodedata.normalize("NFKC", str(name or "")).casefold().strip()


def expected(exp: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """期望 SQL 上算出的那份分析结论 → 这一项的期望值（GoldSet.values 里的一项）：判定类、点名的段、方向、判定语，连同整份
    结论（评分时说法表按它核对正文；比较作答前后两次期望时去掉随运行当天、现查时刻变的几项，见 steady）。算不了的照实记。"""
    op, mode = result.get("op"), result.get("mode")
    out: dict[str, Any] = {"kind": exp.get("kind"), "status": result.get("status"), "op": op, "mode": mode,
                           "method": op_text(str(op), mode), "phrase": result.get("phrase") or ""}
    if result.get("status") != "ok":
        out.update(reason_code=result.get("reason_code"), reason=result.get("reason") or "原因未知")
        out["result"] = copy.deepcopy(dict(result))
        return out
    verdict = result.get("verdict") or {}
    judgement = judgement_code(op, mode, verdict.get("code"))
    subjects = [_segment(s) for s in verdict.get("subjects") or []]
    if judgement == "source":
        subjects = subjects[:int(exp.get("top_n") or 1)]
    elif judgement not in ("differs", "no_evidence"):
        subjects = []
    out.update(verdict=verdict.get("code"), judgement=judgement, subjects=subjects,
               direction=verdict.get("direction") if judgement in _DIRECTED else None,
               criterion=result.get("criterion") or "")
    out["result"] = copy.deepcopy(dict(result))
    return out


def steady(value: Any) -> Any:
    """比较用的期望值：分析结论里随运行当天（params.today）、现查时刻（probes[*].at）、输入快照的工件（inputs[*].artifact，
    快照里记着查询用时，同一份数据每次查出来工件也不同）变的几项去掉。别的期望原样。作答前后两次期望（gold._fingerprint）、
    和上一次比的「数据变了」（runner.gold_key）都按它比。"""
    if not (isinstance(value, Mapping) and value.get("kind") in ANALYSIS_KINDS and isinstance(value.get("result"), Mapping)):
        return value
    result = copy.deepcopy(dict(value["result"]))
    if isinstance(result.get("params"), dict):
        result["params"].pop("today", None)
    for probe in result.get("probes") or []:
        if isinstance(probe, dict):
            probe.pop("at", None)
    for item in result.get("inputs") or []:
        if isinstance(item, dict):
            item.pop("artifact", None)
    return {**value, "result": result}


def view(value: Mapping[str, Any]) -> dict[str, Any]:
    """评分结果里给人看的那一份（期望、答案各一份）：判定类、点名的段、方向、判定语、哪一份结论（A 编号）。界面照原样写，
    叫法在前端 termsEval。"""
    keys = ("judgement", "subjects", "direction", "phrase", "method", "alias", "status", "text")
    out = {k: copy.deepcopy(value[k]) for k in keys if value.get(k) not in (None, "", [])}
    if value.get("status") == "refused" and value.get("reason"):
        out["reason"] = value["reason"]
    return out


# ==========================================================================
# 答案里的分析结论
# ==========================================================================


def record_of(result: Mapping[str, Any], *, artifact: str, info: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """答案运行台账里的一份分析结论 → 评分用的那一份：算子、模式、判定类、点名的段、方向、判定语；输入（查询快照、上一层
    结论的工件）给逐题 E1 找产出答案的查询。alias、cited 由读答案的地方填。"""
    verdict = result.get("verdict") or {}
    op, mode = result.get("op"), result.get("mode")
    out: dict[str, Any] = {
        "artifact": artifact, "op": op, "mode": mode, "method": op_text(str(op), mode), "status": result.get("status"),
        "verdict": verdict.get("code"), "judgement": judgement_code(op, mode, verdict.get("code")),
        "subjects": [_segment(s) for s in verdict.get("subjects") or []],
        "phrase": result.get("phrase") or "", "cited": [],
        "inputs": list((info or {}).get("inputs") or [i.get("artifact") for i in result.get("inputs") or []
                                                      if isinstance(i, Mapping) and i.get("artifact")]),
    }
    judgement = out["judgement"]
    out["direction"] = verdict.get("direction") if judgement in _DIRECTED else None
    if result.get("status") != "ok":
        out["reason"] = result.get("reason") or "原因未知"
    if (info or {}).get("node_id"):
        out["node_id"] = info["node_id"]
    return out


def _same_family(want: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    """答案里的这份结论是不是这一项期望的同类分析：同一个算子；变化拆解还要同一种模式（可加、比率、乘积说的不是一回事）。"""
    if record.get("op") != want.get("op"):
        return False
    return want.get("op") != "decompose" or record.get("mode") == want.get("mode")


def _names(items: Iterable[Any]) -> set[str]:
    return {norm(x) for x in items if norm(x)}


def mismatch(want: Mapping[str, Any], got: Mapping[str, Any]) -> str | None:
    """答案里一份同类的分析结论和期望对不对得上：对得上为 None，否则是对不上的那一句（给人看）。"""
    if got.get("status") != "ok":
        return f"这份分析没有做成（{got.get('reason') or '原因未知'}）"
    judgement = want.get("judgement")
    if got.get("judgement") != judgement:
        return "判定和期望不是一类"
    if judgement in ("differs", "no_evidence"):
        mine, theirs = [norm(s) for s in got.get("subjects") or []], [norm(s) for s in want.get("subjects") or []]
        if len(theirs) == 2 and (len(mine) != 2 or set(mine) != set(theirs)):
            return f"比的是{'和'.join(got.get('subjects') or []) or '别的组'}，期望比的是{'和'.join(want.get('subjects') or [])}"
        if judgement == "differs" and len(theirs) == 2 and want.get("direction") and got.get("direction"):
            same_order = mine == theirs
            if (got["direction"] == want["direction"]) != same_order:
                return "高低写反了"
        return None
    if judgement == "source":
        missing = [s for s in want.get("subjects") or [] if norm(s) not in _names(got.get("subjects") or [])]
        if missing:
            return f"主要来源是{'、'.join(got.get('subjects') or []) or '别的段'}，不是{'、'.join(missing)}"
    if judgement in _DIRECTED and want.get("direction") and got.get("direction") \
            and want["direction"] != got["direction"]:
        return "方向相反"
    return None


# ==========================================================================
# 正文里的判断词：按说法表认，拿期望的那份分析结论核对（engine/analysis_claims 同一套词表和绑定规则）
# ==========================================================================

_SENTENCE_END = re.compile(r"(?<=[。！？!?\n])")


def _said_something(text: str) -> bool:
    """这句话里有没有评分要看的说法：说法表的判断词，或者明确说两期不能直接比（_INCOMPARABLE）。"""
    from app.engine import analysis_claims as said

    return bool(said.scan(text) or incomparable_spans(text))


def sentences_of_doc(doc: Mapping[str, Any], aliases: Iterable[str]) -> list[dict[str, Any]]:
    """报告文档里有判断词（或者明确说两期不能直接比）的句子：{text: 写作者自己写的字（引用渲染出来的值、判定语、引文涂成
    \\x00）, names: 点名用的样子（引用渲染出来的值留着）, analyses: 这句挂的分析结论编号}。标题、代码块、表格不算（同说法
    核对）。aliases 是文档目录里分析结论的编号。"""
    from app.engine import analysis_claims as said
    from app.engine.evidence import iter_units

    known = {a: {} for a in aliases}
    out: list[dict[str, Any]] = []
    for block, unit in iter_units(dict(doc)):
        if block.get("type") in ("heading", "code", "table") or unit.get("kind") in ("heading", "code") \
                or unit.get("loc") is not None:
            continue
        sentence = said._sentence(unit, unit.get("cites") or [], known)
        if sentence is not None and _said_something(sentence.text):
            out.append({"text": sentence.text, "names": sentence.names, "analyses": list(sentence.analyses)})
    return out


def sentences_of_text(text: str) -> list[dict[str, Any]]:
    """没有报告文档时（成果里的文字）：按句末标点切开，有判断词（或者明确说两期不能直接比）的句子。"""
    out = []
    for piece in _SENTENCE_END.split(text or ""):
        piece = piece.strip()
        if piece and _said_something(piece):
            out.append({"text": piece, "names": piece, "analyses": []})
    return out


def _snippet(names: str, start: int, end: int) -> str:
    """判断词连同它所在的分句（给人看）。"""
    from app.engine import analysis_claims as said

    left, right = said._clause(names, start, end)
    text = names[left:right].replace("\x00", "").strip()
    return text[:_SNIPPET_MAX] + ("…" if len(text) > _SNIPPET_MAX else "")


def _said_judgement(claim: str, direction: str | None) -> str | None:
    if claim == "trend":
        return {"up": "rising", "down": "falling"}.get(direction or "")
    return _CLAIM_JUDGEMENT.get(claim)


def said_in(want: Mapping[str, Any], sentences: Sequence[Mapping[str, Any]], *,
            only: Iterable[str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """正文里和这项期望同一类的判断词，拿期望的那份分析结论（want.result）按说法表的绑定规则核对：(说出了期望的那几处,
    和期望对不上的那几处)。每处 {text: 所在分句, judgement: 它说出的判定类, subjects: 点名的段, reason: 对不上的原因}。
    only 给了时只看挂了这几份结论的句子。

    规则同 engine/analysis_claims.check_sentence（同一个 _check）：因果词点名的是期望拆解里的段的按来源核；点名的段要在期望
    点名的段里，拖累、拉动按那一段的涨跌核；「连续 N 期」按末尾连续同向的期数核。另外按期望的方向核主要来源那句话的涨跌
    （「下降主要来自华东」）。"""
    from app.engine import analysis_claims as said

    gold = want.get("result") or {}
    op = want.get("op")
    affirms = _AFFIRMS.get(str(want.get("judgement")), frozenset())
    lex = said.lexicon()
    wanted = set(only) if only is not None else None
    good: list[dict[str, Any]] = []
    bad: list[dict[str, Any]] = []
    for raw in sentences:
        if wanted is not None and not wanted & set(raw.get("analyses") or []):
            continue
        sentence = said.Sentence(text=str(raw.get("text") or ""), names=str(raw.get("names") or ""))
        for hit in said.scan(sentence.text):
            claim = lex.claim(hit.claim)
            left, right = said._clause(sentence.text, hit.start, hit.end)
            names = sentence.names[left:right]
            spans = said._object(sentence, hit, left, right)
            if claim.key == "causal":
                if op != "decompose":
                    continue
                cause = said._cause(sentence, hit, left, right)
                if not said._named(gold, cause):
                    continue
                hit, claim, spans = replace(hit, claim="source", side="cause"), lex.claim("source"), (cause,)
            if claim.op != op:
                continue
            miss = said._check(lex, hit, gold, names, spans=spans)
            reason = miss.human if miss is not None else None
            named = next((found for span in spans if (found := said._named(gold, span))), [])
            if reason is None and hit.claim == "source" and hit.side != "pull" and want.get("direction") \
                    and hit.direction and hit.direction != want["direction"]:
                reason = "涨跌写反了"
            item = {"text": _snippet(sentence.names, hit.start, hit.end),
                    "judgement": _said_judgement(hit.claim, hit.direction), "subjects": named}
            if reason is not None:
                bad.append({**item, "reason": reason})
            elif hit.claim in affirms:
                good.append(item)
    return good, bad


def incomparable_spans(text: str) -> list[tuple[int, int]]:
    """一句话（写作者自己写的字）里明确说两期不能直接比的几处 [(起, 止)]（_INCOMPARABLE）。同一分句里前面有否定的、
    句子里前面有假设或疑问的不算，问句整句不算。"""
    from app.engine import analysis_claims as said

    if text.rstrip("\x00 ").endswith(("？", "?")):
        return []
    out = []
    for m in _INCOMPARABLE.finditer(text):
        left, _ = said._clause(text, m.start(), m.end())
        if _INCOMPARABLE_DENIED.search(text[left:m.start()]) or _INCOMPARABLE_SUPPOSED.search(text[:m.start()]):
            continue
        out.append((m.start(), m.end()))
    return out


def said_incomparable(sentences: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """正文里明确说两期不能直接比的几处（可比性的期望是不可比时，算说出了它）：{text: 所在分句, judgement: incomparable}。"""
    out = []
    for raw in sentences:
        text = str(raw.get("text") or "")
        names = str(raw.get("names") or text)
        for start, end in incomparable_spans(text):
            out.append({"text": _snippet(names, start, end), "judgement": "incomparable", "subjects": []})
    return out


def _covered(want: Mapping[str, Any], good: Sequence[Mapping[str, Any]]) -> list[str]:
    """主要来源期望点名的段里，正文说对的那几处一处都没点到的（top_n 要点名的前几段都要说到）。"""
    if want.get("judgement") != "source":
        return []
    named = _names(s for item in good for s in item.get("subjects") or [])
    return [s for s in want.get("subjects") or [] if norm(s) not in named]


# ==========================================================================
# 评分
# ==========================================================================


def score(exp: Mapping[str, Any], value: Mapping[str, Any], answer: Any) -> dict[str, Any]:
    """一项分析期望的结论（score.OUTCOMES 之一）。value 是期望值（expected 的形状），answer 是 score.Answer（analyses：
    答案运行台账里的分析结论，cited 记着报告怎么引用了它；sentences：有判断词的句子）。纯计算。"""
    want = view(value)
    if value.get("status") != "ok":
        return {"outcome": "ungradable", "want": want, "reason": f"期望算不了：{value.get('reason') or '原因未知'}"}
    if value.get("judgement") is None:
        return {"outcome": "ungradable", "want": want, "reason": "期望的分析没有给出判定"}
    records = [r for r in getattr(answer, "analyses", None) or [] if _same_family(value, r)]
    cited = [r for r in records if r.get("cited") and r.get("alias")]
    sentences = list(getattr(answer, "sentences", None) or [])
    matched = [r for r in cited if mismatch(value, r) is None]
    if matched:
        found = matched[0]
        _, bad = said_in(value, sentences, only=[r["alias"] for r in matched])
        if bad:
            return {"outcome": "wrong", "want": want, "found": view({**found, "text": bad[0]["text"]}),
                    "reason": f"引用了 {found['alias']}，同一句却写成「{bad[0]['text']}」：{bad[0]['reason']}"}
        return {"outcome": "traced", "want": want, "found": view(found)}
    good, bad = said_in(value, sentences)
    if value.get("judgement") == "incomparable":
        good += said_incomparable(sentences)
    if good:
        missing = _covered(value, good)
        if not missing:
            return {"outcome": "untraced", "want": want, "found": view(good[0])}
        return {"outcome": "wrong", "want": want, "found": view(good[0]),
                "reason": f"正文只点名了{'、'.join(s for g in good for s in g.get('subjects') or []) or '别的段'}，"
                          f"期望的主要来源还有{'、'.join(missing)}"}
    if cited:
        first = cited[0]
        return {"outcome": "wrong", "want": want, "found": view(first),
                "reason": f"答案引用的 {first['alias']}（{first.get('phrase') or first.get('method')}）"
                          f"和期望对不上：{mismatch(value, first)}"}
    if bad:
        return {"outcome": "wrong", "want": want, "found": view(bad[0]),
                "reason": f"正文写的「{bad[0]['text']}」和期望对不上：{bad[0]['reason']}"}
    method = op_text(str(value.get("op")), value.get("mode"))
    if records:
        first = records[0]
        shown = f"（{first['alias']}）" if first.get("alias") else ""
        return {"outcome": "wrong", "want": want, "found": view(first),
                "reason": f"答案做了{method}{shown}，但没有引用，正文里也没有写出这个判断"}
    return {"outcome": "wrong", "want": want, "reason": f"答案里没有做{method}，正文里也没有写出这个判断"}


__all__ = ["ANALYSIS_KINDS", "GOLD_REF", "JUDGEMENTS", "ParamsInvalid", "TOP_N_MAX", "VERDICT_OPS", "check_params",
           "expected", "incomparable_spans", "judgement_code", "mismatch", "norm", "op_text", "param_names",
           "record_of", "request_of", "said_in", "said_incomparable", "score", "sentences_of_doc", "sentences_of_text",
           "steady", "template_texts", "view"]
