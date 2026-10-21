"""agent 分析工具（docs/design/analysis-round/00-总方案.md §3.2）：变化拆解、波动基线、趋势、差异检验、可比性检查，一个算子
一个工具（名字取 contract.TOOL_NAMES），参数各自聚焦，模型少写错。

- **参数只有「算什么」**：引用本节点查过的结果（query: "Q3"，和交回模型的「【证据 Q3】」同一个编号）、列的角色、期次、
  总数、上一层拆解、度量的叫法、这次在验证什么。没有任何阈值参数：阈值只来自设置快照（D4），模型不能把阈值调到想要的
  结论。参数名写错、多写了参数，照别的工具一样交回「它接受什么」（registry.prepare_args）。
- **调用写错**（contract.CallError：列不存在、列被遮罩、比率当可加、这个节点的分析次数用完……）不记结论，原话交回模型改；
  **算不了**（历史不够、本期不完整、截断……）是一条结论，照样落成工件、进台账。
- **方法说明**：五个工具共用一份「分析方法」（METHOD_GUIDE），绑了任一个就注入 system，同一份只注入一次。
- 工具本身不查库：读的是本节点的查询容器（app/tools/analysis.AnalysisScope），能力登记里不认它们是查询
  （capabilities.registry.is_platform_query 只认数据源查询和声明了表格的端点读取）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.analysis import contract
from app.analysis.contract import CallError
from app.tools.registry import ToolContext, ToolGuide, register

CATEGORY = "分析"

#: 「分析方法」：五个分析工具共用，绑了任一个就注入 system（第一版，第 2 波定稿）。改了方法就改 id 里的版本号：评测的
#: prompt_fingerprint 认得出提示词变了
ANALYSIS_METHOD_PROMPT = """\
# 分析方法（平台内置，用分析工具时照这个做）

1. 先定现象：取本期和问题对应的基期，用 analysis_comparable 确认两期能直接比；历史够长（缺省至少 8 期）就用 analysis_baseline 放回历史看——是常规波动就照实说，不硬找原因。
2. 指标是乘积或比率时，先用 analysis_decompose 的 factor、ratio 模式拆因子、拆结构。
3. 按数据目录里的候选维度由粗到细拆（additive 模式），每次用 headline 给出总数核对拆开后是否闭合；找到主要来源再用 parent 下钻一层：下钻的 SQL 由你写（只查那一段），系统按上一层那一段的值核对闭合。
4. 每次调用在 hypothesis 里写明这次在验证什么；拆了没发现集中也是结论，照实写。
5. 不作因果断言：数据只说明变化来自哪里；原因要有知识库原话或输入作依据，否则写成待核实的推测。
6. 阈值和方法由平台定，工具参数里没有阈值；算不了时照实说算不了的原因，不要换个说法硬下判断。

分析结论的编号「【分析 An】」只在本节点有效：下钻时 parent 写它（如 {"of": "A1", "key": ["华东"]}）。判定语由系统写好了，结论里照它的意思说，不要改写判断的强弱。"""
METHOD_GUIDE = ToolGuide(id="analysis.method@1", text=ANALYSIS_METHOD_PROMPT)

#: 交回 agent 的说法：这个节点里没有可分析的查询容器（调用工具节点、工具库试用、没记台账的老运行）
NO_SCOPE = ("分析工具只能由 Agent 在本节点查过数之后调用：它分析的是本节点交回过的查询结果（【证据 Qn】的编号）。"
            "模板里要固定做的分析，请改用「分析卡」节点")
#: 交回 agent 的说法：本节点还没有成功的查询
NO_QUERIES = "本节点还没有成功的查询可以分析：先用数据库查询工具查出要分析的数，再按交回的编号（Q1、Q2……）调用分析工具"

_QUERY = "要分析的查询结果：本节点交回过的编号，如 Q3（和「【证据 Q3】」同一个）"
_LABEL = "度量的叫法，系统写判定语时用（如「销售额」）；不要写数字、百分号和单位，留空就用列名"
_HYPOTHESIS = "这次在验证什么，一句话（如「下降是不是集中在某个地区」）：只在证据面板显示，不进报告正文"
_PERIOD = "期次列的列名（日期、周、月……）"
_GRAIN = "期次的粒度：day、week、month、quarter；留空按期次的写法推断"


class Headline(BaseModel):
    """总数的两格：拆开后各段之和要分别等于它们（拆解闭合）。"""

    base: str = Field(description="基期总数那一格，写成「编号.r行号.列名」，如 Q1.r1.gmv（行号从 0 数）")
    target: str = Field(description="本期总数那一格，如 Q1.r0.gmv")


class Parent(BaseModel):
    """下钻：这一层是上一层拆解里某一段的拆解。"""

    of: str = Field(description="上一层拆解的编号，如 A1（「【分析 A1】」里的那个）")
    key: list[str] = Field(description="上一层里要往下拆的那一段的维度值，一个维度一个值（如 华东）")


class DecomposeArgs(BaseModel):
    mode: Literal["additive", "ratio", "factor"] = Field(
        description="additive：可加的量（销售额、订单数）按维度算各段对总变化的贡献；ratio：比率（退款率、客单价）"
                    "拆成结构变化和各段自身的变化；factor：乘积（销售额 = 订单数 × 客单价）拆成各因子的贡献")
    query: str | None = Field(default=None, description=_QUERY + "；这份结果里有期次列，用 base、target 指定比哪两期")
    base_query: str | None = Field(default=None,
                                   description="基期、本期是两份查询时：基期那份的编号（这时不写 query、period）")
    target_query: str | None = Field(default=None, description="基期、本期是两份查询时：本期那份的编号")
    by: list[str] | None = Field(default=None, description="维度列的列名，1 到 2 列（additive、ratio 用）")
    period: str | None = Field(default=None, description=_PERIOD)
    base: str | None = Field(default=None, description="基期：期次列里的一个值，如 2026-W41")
    target: str | None = Field(default=None, description="本期：期次列里的一个值，如 2026-W42")
    measure: str | None = Field(default=None, description="度量列（additive 用）")
    numerator: str | None = Field(default=None, description="分子列（ratio 用，如退款单数）")
    denominator: str | None = Field(default=None, description="分母列（ratio 用，如订单数）")
    product: str | None = Field(default=None, description="乘积列（factor 用，如销售额）")
    factors: list[str] | None = Field(default=None, description="因子列，2 到 4 列（factor 用，如订单数、客单价）")
    headline: Headline | None = Field(default=None, description="可选。总数的两格，用来核对拆开后各段之和对不对得上")
    parent: Parent | None = Field(default=None, description="可选。下钻：这一层是哪次拆解里哪一段的拆解")
    label: str | None = Field(default=None, description=_LABEL)
    hypothesis: str | None = Field(default=None, description=_HYPOTHESIS)


class BaselineArgs(BaseModel):
    query: str = Field(description=_QUERY + "；要一条按期次排的序列（要看某一段就在 SQL 里筛好）")
    period: str = Field(description=_PERIOD)
    measure: str = Field(description="度量列")
    target: str | None = Field(default=None, description="本期：期次列里的一个值；留空取最后一期")
    basis: Literal["change", "level"] | None = Field(
        default=None, description="change：和比较期比的变化率（缺省）；level：值本身（存量、比率用）")
    grain: Literal["day", "week", "month", "quarter"] | None = Field(default=None, description=_GRAIN)
    lag: int | None = Field(default=None, description="和往前第几期比：日缺省 7（和上周同一天比），周、月缺省 1；"
                                                      "要同比写 52（周）或 12（月），历史要相应够长")
    label: str | None = Field(default=None, description=_LABEL)
    hypothesis: str | None = Field(default=None, description=_HYPOTHESIS)


class TrendArgs(BaseModel):
    query: str = Field(description=_QUERY + "；要一条按期次排的序列")
    period: str = Field(description=_PERIOD)
    measure: str = Field(description="度量列")
    grain: Literal["day", "week", "month", "quarter"] | None = Field(default=None, description=_GRAIN)
    label: str | None = Field(default=None, description=_LABEL)
    hypothesis: str | None = Field(default=None, description=_HYPOTHESIS)


class DifferArgs(BaseModel):
    mode: Literal["rate", "mean", "rows"] = Field(
        description="rate：比率（转化率、退款率），给分子、分母两列；mean：均值，给每组的均值、标准差、样本数三列，"
                    "或合计、平方和、样本数三列；rows：逐行的值，算子自己算均值和方差（结果不能截断）")
    query: str = Field(description=_QUERY)
    by: list[str] | None = Field(default=None, description="按组比时：分组列的列名（一列）")
    pairs: list[list[str]] | None = Field(default=None, description="只比这几对：每对写两个组在分组列里的值")
    reference: str | None = Field(default=None, description="对照组：分组列里的一个值，其余各组各和它比")
    period: str | None = Field(default=None, description="按期次比同一组的两期时：期次列（SQL 里先筛好那一组）")
    base: str | None = Field(default=None, description="按期次比时：基期")
    target: str | None = Field(default=None, description="按期次比时：本期")
    numerator: str | None = Field(default=None, description="分子列：成功数（rate 用，如退款单数）")
    denominator: str | None = Field(default=None, description="分母列：试验数（rate 用，如订单数）")
    mean: str | None = Field(default=None, description="每组的均值列（mean 用，和标准差、样本数一起给）")
    sd: str | None = Field(default=None, description="每组的标准差列（mean 用）")
    sum: str | None = Field(default=None, description="每组的合计列（mean 用，没有标准差时和平方和、样本数一起给）")
    sumsq: str | None = Field(default=None, description="每组的平方和列（mean 用）")
    n: str | None = Field(default=None, description="每组的样本数列（mean 用）")
    value: str | None = Field(default=None, description="逐行的值列（rows 用）")
    label: str | None = Field(default=None, description=_LABEL)
    hypothesis: str | None = Field(default=None, description=_HYPOTHESIS)


class ComparableArgs(BaseModel):
    base_query: str | None = Field(default=None, description="基期那份查询的编号，如 Q1")
    target_query: str | None = Field(default=None, description="本期那份查询的编号，如 Q2")
    query: str | None = Field(default=None, description="两期在同一份查询里按期次分组时：那份的编号（这时不写上面两项）")
    period: str | None = Field(default=None, description="同一份查询时：期次列")
    base: str | None = Field(default=None, description="同一份查询时：基期")
    target: str | None = Field(default=None, description="同一份查询时：本期")
    measure: str | None = Field(default=None, description="度量列：按它找时间条件、认它是不是可累加的量；只有一列数值列时可以不写")
    grain: Literal["day", "week", "month", "quarter"] | None = Field(default=None, description=_GRAIN)
    hypothesis: str | None = Field(default=None, description=_HYPOTHESIS)


#: 算子 → 参数模型。分析卡的一项按同一份参数模型校验参数（engine/nodes/analysis.py）
ARGS: dict[str, type[BaseModel]] = {
    "decompose": DecomposeArgs, "baseline": BaselineArgs, "trend": TrendArgs, "differ": DifferArgs,
    "comparable": ComparableArgs,
}
#: 不算「算什么」的参数：模式另放，这次在验证什么另放（contract.AnalysisRequest）
NOT_PARAMS = frozenset({"mode", "hypothesis"})


def param_names(op: str) -> list[str]:
    """一个算子的参数名（不含模式和「在验证什么」），按参数模型里的先后。分析卡校验一项的参数时按它。"""
    return [k for k in ARGS[op].model_fields if k not in NOT_PARAMS]


def request_of(op: str, args: BaseModel) -> contract.AnalysisRequest:
    """校验过的参数 → 一次分析的请求（contract.AnalysisRequest）：没填的参数不带。"""
    from app.tools.analysis import hypothesis_of

    data = args.model_dump(exclude_none=True)
    request: contract.AnalysisRequest = {
        "op": op, "mode": data.get("mode") if op in contract.MODES else None,
        "params": {k: v for k, v in data.items() if k not in NOT_PARAMS},
    }
    if hypothesis := hypothesis_of(data.get("hypothesis")):
        request["hypothesis"] = hypothesis
    return request


async def run_tool(op: str, ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """一次 agent 分析：读本节点的查询容器、按设置快照取阈值、调算子、结论落成工件。交回 {analysis_artifact, result}：
    记台账、编号 An、给模型看的那一段由节点在记台账时做（engine/nodes/llm.py 的 _record_call），重放时照样做一遍。

    调用写错（含这个节点的分析次数用完，analysis.exhausted）抛 contract.CallError，节点作为工具错误交回模型、不记台账。
    结论原样存成工件、一个字段都不加：下钻时算子按上一层结论的内容哈希认 parent 的工件。"""
    import app.analysis as analysis_ops   # 按模块取 run：测试换替身时只换模块上的这一个函数

    from app.tools.analysis import context_of, prepare, scope_of, settings_of, store

    scope = scope_of(ctx.extra)
    if scope is None:
        raise CallError("no_scope", NO_SCOPE)
    if not scope.queries:
        raise CallError("no_queries", NO_QUERIES)
    settings = await settings_of(scope.snapshot)
    if len(scope.results) >= settings["max_calls"]:
        raise analysis_ops.exhausted(settings)          # 次数用完的说法只有算子那一处
    request = request_of(op, args)
    await prepare(scope)
    context = await context_of(list(scope.queries.values()), tool_ctx=ctx, cache=scope.source_cache,
                               probe_cache=scope.probe_cache)
    result = await analysis_ops.run(request, scope.resolve, settings=settings, context=context, parents=scope.parent)
    artifact = await store(result, run_id=ctx.run_id, node_id=scope.node_id)
    return {"analysis_artifact": artifact, "result": result}


@register(
    name=contract.TOOL_NAMES["decompose"],
    category=CATEGORY,
    description="变化拆解：说清一个量的变化从哪来。读本节点查过的结果（【证据 Qn】的编号），按维度算各段对总变化的贡献"
                "（additive）、把比率的变化拆成结构变化和各段自身的变化（ratio）、把乘积拆成各因子的贡献（factor）；给了总数"
                "就核对各段之和对不对得上，parent 接着上一次拆解的某一段往下钻。主要来自哪一段、是否分散由系统按平台的阈值判定。",
    args_schema=DecomposeArgs,
    guide=METHOD_GUIDE,
)
async def analysis_decompose(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    return await run_tool("decompose", ctx, DecomposeArgs(**kwargs))


@register(
    name=contract.TOOL_NAMES["baseline"],
    category=CATEGORY,
    description="波动基线：把本期放回它自己的历史里看，判断这次的变化在不在常规波动范围内。要一份按期次排的序列"
                "（期次列 + 度量列）；历史不够长、本期还没过完时照实交回算不了的原因。",
    args_schema=BaselineArgs,
    guide=METHOD_GUIDE,
)
async def analysis_baseline(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    return await run_tool("baseline", ctx, BaselineArgs(**kwargs))


@register(
    name=contract.TOOL_NAMES["trend"],
    category=CATEGORY,
    description="趋势：判断一条按期次排的序列是在持续上升、持续下降，还是没有明显趋势（只是一次波动）。",
    args_schema=TrendArgs,
    guide=METHOD_GUIDE,
)
async def analysis_trend(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    return await run_tool("trend", ctx, TrendArgs(**kwargs))


@register(
    name=contract.TOOL_NAMES["differ"],
    category=CATEGORY,
    description="差异检验：判断两组（或同一组的两期）的差异站得住，还是可能只是样本波动。比率给分子、分母两列（rate），"
                "均值给均值、标准差、样本数或合计、平方和、样本数（mean），也可以给逐行的值（rows）；样本小时照实标出。",
    args_schema=DifferArgs,
    guide=METHOD_GUIDE,
)
async def analysis_differ(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    return await run_tool("differ", ctx, DifferArgs(**kwargs))


@register(
    name=contract.TOOL_NAMES["comparable"],
    category=CATEGORY,
    description="可比性检查：比两期之前先确认能不能直接比——两份查询是不是只差日期、窗口是否一样长、是不是整期、"
                "星期构成是否相同。判定为可比、有条件可比或不可比，并列出原因。",
    args_schema=ComparableArgs,
    guide=METHOD_GUIDE,
)
async def analysis_comparable(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    return await run_tool("comparable", ctx, ComparableArgs(**kwargs))


__all__ = ["ANALYSIS_METHOD_PROMPT", "ARGS", "METHOD_GUIDE", "param_names", "request_of", "run_tool"]
