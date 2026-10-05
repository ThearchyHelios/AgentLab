"""反方审稿节点：报告写完后，挑几句关键结论，各派一个只读 agent 站在反方主动找反例。

结论句裁判（engine/judge.py）只看写作者挂的证据，判「说法有没有被证据支持」；可证据本身就可能挑过——只查了含
退款的销售额，只看了一个月。反方审稿换个方向：不看证据够不够，自己写查询去证伪。

1. 选句：报告文档里的结论句按裁判的预筛排好（judge.candidates / claim_priority：挂了依据的、带比较或因果的在前），
   取前「最多审几句」句；裁判判成「不是结论」的不审。同一份文档永远选出同样的句子、同样的顺序
2. 反查：每句一个只读 agent（verify_agent.run_verify_agent）。工具只给这句引用的查询所在的数据源（指标按它的输入
   取自的查询算，合并查询按它合并的那几个源算）；这句没挂依据，就在这份报告引用的数据源里查。任务里给这句结论
   和它挂的证据摘录（judge.prepare，数据源遮罩的列照旧不给），要它先想清楚「如果这句不成立，数据里会看到什么」，
   写只读查询去找，最后交结论（经受住 / 被削弱 / 被推翻 / 无法判断）和理由。理由里的数字只能引用它自己查询的
   格子，裸数字由 compose_doc 标出来
3. 记录：每句的结论、理由（渲染后的正文和组装好的文档）、查询快照、用量写进审稿记录（challenge_artifact），随运行
   封存。查询照常发 tool.end（role: verify，带查询快照，可下钻）；台账只记 kind challenge，绝不记 query——核查的
   查询进了写作目录，下游的写作者会把它们当成可以引用的证据
4. 处置：被推翻的按 on_refuted 处置。degrade（默认）在产出里写 issuance_gaps，成果节点的出具契约照单收进缺口、
   出具降档（nodes/io.py 的 _upstream_issuance_gaps）；fail 判节点失败；flag 只记录。审批卡（human.py 的
   _reviewed_reports）附上审稿概况

上限：句数、每句查询次数、每句时长、全局每日金额（核查和裁判各记各的账，engine/spend.py）。都按发起时快照的核查
设置（run.options["verify"]），节点上写了的盖过它：审批停下期间改了设置，恢复、接着跑仍按发起时的那份。没有快照
的运行按设置的当前值。全局关闭且节点没写「启用」时直通；报告没有可审的结论句也直通。

逐句串行反查：并行的话几句的工具调用在时间线上交错，看不出哪条查询在审哪一句；每日余额按先后扣，结果也才确定。
整个反查连同存审稿记录、发日志都在一个 @task 里：接着跑时取 checkpoint 里的结果，不重复查库、不重复记账，时间线
上也不再发一遍。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from langgraph.func import task

from app.core import artifact_store
from app.core.errors import describe_exception
from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine import judge as judging
from app.engine.context import NodeContext, NodeError
from app.engine.evidence import ENTITY_KINDS, iter_units, ledger_enabled, next_exec
from app.engine.judge import _usd
from app.engine.merge_query import MERGE_SOURCE
from app.engine.replay import _in_graph
from app.engine.schema import CHALLENGE_LABEL, CHALLENGE_ON_REFUTED_LABEL, GraphSpec, NodeType, challenge_problems, \
    challenge_report
from app.engine.state import GraphState
from app.engine.verify_agent import RATIONALE, VerifyOutcome, run_verify_agent
from app.tools.datasource import run_versions

#: 四种结论：经受住 / 被削弱 / 被推翻 / 无法判断。counts 的键照这个顺序
VERDICTS = ("stands", "weakened", "refuted", "inconclusive")
#: 节点产出、审批卡里最多列几句（审稿记录里一句不少）
ITEMS_MAX = 10
#: 出具缺口、报错里最多点几句的原文，每句最多几个字
_NAMED = 3
_QUOTE_CHARS = 40
#: 审稿记录里每次查询记的几项
_QUERY_KEYS = ("alias", "artifact", "sql", "row_count", "truncated", "source")
#: 每句反查都会遇到的上限：触顶时发 challenge_limit。步数上限（verify_agent 的 max_steps）由核查自己的
#: verify_limit 说明，不在这里另记
_CLAIM_LIMITS = ("max_queries", "timeout_s", "daily_max_usd")
#: 列「审批卡上的几句」时谁先留：被推翻、被削弱、没能判断的比经受住的更要让审批人看到
_RANK = {"refuted": 0, "weakened": 1, "inconclusive": 2, "stands": 3}

CHALLENGE_SYSTEM = (
    "你是反方审稿人：报告已经写完，你站在反方，专门找其中一句结论的反例。你不写报告，也不改报告。\n"
    "- 先想清楚：如果这句结论不成立，数据里会看到什么；再写只读查询去找这样的反例\n"
    "- 换一个角度核对：按别的维度拆开、换时间范围、核对口径和过滤条件（退款、作废、重复记录）、找被忽略的数据，"
    "不要只是重复写作者的查询\n"
    "- verdict 四选一：stands 认真找过、没有找到反例；weakened 大体成立，但有重要的例外或前提，或者把范围、程度、"
    "原因说过了头；refuted 查到的数据和这句结论相矛盾；inconclusive 查不到判断所需的数据\n"
    "- 没有找到反例不等于结论被推翻：只有数据和结论冲突时才判 refuted"
)
CHALLENGE_TASK_PROMPT = (
    "要审的结论（报告「{report}」中的一句{section}）：\n「{claim}」\n\n"
    "写作者给这句挂的证据摘录（编号属于原报告；你的查询另起编号，理由里只能引用你自己的查询）：\n{evidence}\n\n"
    "可以查询的数据源：{sources}\n\n"
    "按上面的做法反查，最后交结论（verdict）和理由（rationale）：理由说清查了什么、看到了什么，里面的数字一律写成"
    "对你自己查询结果的引用，不要直接写数字。"
)
#: 交给核查 agent 的结果结构：结论一项；理由由 verify_agent 自动加上（rationale）
CHALLENGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"verdict": {
        "type": "string", "enum": list(VERDICTS),
        "description": "stands 经受住、weakened 被削弱、refuted 被推翻、inconclusive 无法判断",
    }},
    "required": ["verdict"],
}


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------


async def run_challenge(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    spec, node = ctx.run.spec, ctx.node
    # 节点自己的配置直接读 node.config，不走 ctx.cfg：图级 defaults 里的 provider / model 是写作模型的，
    # 拿来当反方模型就成了自己审自己
    config = node.config
    # 画布校验也拦这几项；执行时再守一道，报的是同一句话（跳过校验执行的旧图、改坏了的配置）
    if problems := challenge_problems(config):
        raise NodeError(node.id, "；".join(message for _, message in problems))
    report_id, problem = challenge_report(spec, node)
    if report_id is None:
        raise NodeError(node.id, problem or "反方审稿找不到要审的报告")
    title = spec.node_map()[report_id].title

    settings = await _verify_settings(ctx)
    part = settings.get("challenge") if isinstance(settings.get("challenge"), dict) else {}
    enabled = config.get("enabled")
    if not (part.get("enabled", True) if enabled is None else enabled):
        from app.api.settings import VERIFY_SETTING_LABEL

        return _skip(ctx, "disabled", f"反方审稿已关闭（设置中的「{VERIFY_SETTING_LABEL['challenge.enabled']}」，"
                                      f"或节点的「{CHALLENGE_LABEL['enabled']}」），本节点已跳过")
    written = (state.get("nodes") or {}).get(report_id)
    written = written if isinstance(written, dict) else {}
    doc_artifact = written.get("doc_artifact") if isinstance(written.get("doc_artifact"), str) else None
    doc = _load_doc(doc_artifact)
    if doc is None or doc_artifact is None:
        return _skip(ctx, "no_report", f"报告「{title}」的报告文档无法读取（没有写出，或保存失败），本节点已跳过")
    claims = pick_claims(doc)
    if not claims:
        return _skip(ctx, "no_claims", f"报告「{title}」中没有可审的结论句，本节点已跳过")

    max_claims = _limit(config, part, "max_claims")
    max_queries = _limit(config, part, "max_queries")
    timeout_s = _limit(config, part, "timeout_s")
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    from app.api.settings import verify_model_spec   # 在这里导入：api.settings 导入了 engine 里的模块

    async with SessionLocal() as session:
        model_spec = await verify_model_spec(session, config, settings=settings)
    review = _Review(
        ctx, report_id=report_id, title=title, doc_artifact=doc_artifact, doc=doc, catalog=catalog, claims=claims,
        max_claims=None if max_claims is None else int(max_claims),
        max_queries=None if max_queries is None else int(max_queries),
        timeout_s=None if timeout_s is None else float(timeout_s), daily_max_usd=settings.get("daily_max_usd"),
        spec=model_spec, masks=await judging.source_masks(catalog),
        writer_model=written.get("model") if isinstance(written.get("model"), str) else None,
    )
    if _in_graph():
        @task
        async def challenge_step() -> dict[str, Any]:
            # 反查进 checkpoint：接着跑时直接取这里的结果，不再查库、不再记账（每日账也记在这里面）；
            # 事件都在这里面发，取缓存时不会再发一遍
            return await review.run()

        outcome = await challenge_step()
    else:
        outcome = await review.run()
    return _finish(ctx, state, outcome, on_refuted=str(config.get("on_refuted") or "degrade"))


def _skip(ctx: NodeContext, reason: str, message: str) -> dict[str, Any]:
    """直通：不改任何状态，记一条 challenge_skipped。审批卡认不出审稿结果（没有 counts），不会附上概况。"""
    ctx.emit(EventType.LOG, level="info", code="challenge_skipped", reason=reason, message=message)
    return {"nodes": {ctx.node.id: {"skipped": True, "reason": reason}}}


async def _verify_settings(ctx: NodeContext) -> dict[str, Any]:
    """核查设置：发起时快照进运行选项的那份（run.options["verify"]），补齐默认、去掉坏值；没有快照的运行按设置的
    当前值。"""
    from app.api.settings import verify_settings, verify_values
    from app.engine.runner import RUN_OPTIONS_KEY

    options = ctx.run.extra.get(RUN_OPTIONS_KEY)
    snapshot = options.get("verify") if isinstance(options, dict) else None
    if isinstance(snapshot, dict):
        return verify_values(snapshot)
    async with SessionLocal() as session:
        return await verify_settings(session)


def _limit(config: dict[str, Any], part: dict[str, Any], key: str) -> Any:
    """节点上写了的上限盖过设置；留空（不写、写 null）沿用设置，设置里的 null 才是不限。"""
    value = config.get(key)
    return part.get(key) if value in (None, "") else value


def _load_doc(artifact: str | None) -> dict[str, Any] | None:
    if not artifact:
        return None
    try:
        doc = artifact_store.load(artifact)
    except ValueError:      # 和哈希对不上：被改过的文档不审
        return None
    return doc if isinstance(doc, dict) else None


def pick_claims(doc: dict[str, Any]) -> list[judging.Candidate]:
    """要审的结论句：裁判预筛排好的候选（judge.candidates），去掉裁判已经判成「不是结论」的。"""
    units = {u.get("id"): u for _, u in iter_units(doc)}
    out: list[judging.Candidate] = []
    for cand in judging.candidates(doc):
        verdict = (units.get(cand.unit) or {}).get("verdict")
        if isinstance(verdict, dict) and verdict.get("status") == "not_a_claim":
            continue
        out.append(cand)
    return out


# --------------------------------------------------------------------------
# 数据源：这句引用的证据背后是哪几个库
# --------------------------------------------------------------------------


def _fetcher() -> Callable[[Any], Any]:
    """按工件 id 取工件（复验哈希），同一份只读一次；取不到、被改过的当没有。"""
    memo: dict[str, Any] = {}

    def fetch(artifact: Any) -> Any:
        key = str(artifact or "")
        if key not in memo:
            try:
                memo[key] = artifact_store.load(key) if key else None
            except Exception:  # noqa: BLE001 - 取不回来就当没有：少一个源，不让整个审稿失败
                memo[key] = None
        return memo[key]

    return fetch


def _snapshot_sources(snapshot: Any) -> list[str]:
    """一份查询快照背后的数据源：合并查询是它合并的那几个源，其余是快照记的源。"""
    if not isinstance(snapshot, dict):
        return []
    if snapshot.get("source") == MERGE_SOURCE:
        return [str(s) for s in snapshot.get("sources") or [] if s]
    return [str(snapshot["source"])] if snapshot.get("source") else []


def claim_sources(aliases: Iterable[str], catalog: dict[str, Any], fetch: Callable[[Any], Any]) -> list[str]:
    """这几件证据背后的数据源（去重保序）：查询的、口径卡指标的输入取自的查询的、表和字段的。知识库检索、运行输入
    背后没有数据源。认法同裁判找遮罩（judge.source_masks）。"""
    out: list[str] = []
    for alias in aliases:
        entry = catalog.get(alias)
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        found: list[str] = []
        if kind == "query":
            source = entry.get("source")
            found = [str(source)] if source and source != MERGE_SOURCE else _snapshot_sources(fetch(entry.get("artifact")))
        elif kind == "metric":
            card = fetch(entry.get("artifact"))
            wanted = (entry.get("locator") or {}).get("metric")
            for metric in (card.get("metrics") if isinstance(card, dict) else None) or []:
                if isinstance(metric, dict) and metric.get("id") == wanted:
                    for item in metric.get("inputs") or []:
                        if isinstance(item, dict) and item.get("artifact"):
                            found += _snapshot_sources(fetch(item["artifact"]))
        elif kind in ENTITY_KINDS:
            source = entry.get("source")
            found = [str(source)] if source else next(
                (_snapshot_sources(fetch(o.get("artifact"))) for o in entry.get("sources") or []
                 if isinstance(o, dict) and o.get("kind") == "schema"), [])
        out.extend(s for s in found if s not in out)
    return out


# --------------------------------------------------------------------------
# 逐句反查
# --------------------------------------------------------------------------


def _zero_usage() -> dict[str, Any]:
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "calls": 0, "unpriced_calls": 0}


def _add_usage(total: dict[str, Any], more: dict[str, Any]) -> dict[str, Any]:
    out = dict(total)
    for key, value in (more or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool) and key in out:
            out[key] += value
    out["cost_usd"] = round(out["cost_usd"], 6)
    return out


def _claim(cand: judging.Candidate, verdict: str, rationale: str, *, doc: dict[str, Any] | None = None,
           queries: Iterable[dict[str, Any]] = (), usage: dict[str, Any] | None = None) -> dict[str, Any]:
    """审稿记录里的一句。rationale 是渲染后的正文；系统替它说的（没有可查的源、没能反查）rationale_doc 为 None。"""
    return {"unit": cand.unit, "text": cand.text, "verdict": verdict, "rationale": rationale, "rationale_doc": doc,
            "queries": [{k: q.get(k) for k in _QUERY_KEYS} for q in queries], "usage": usage or _zero_usage()}


def _claim_of(cand: judging.Candidate, outcome: VerifyOutcome) -> dict[str, Any]:
    if outcome.result is None:
        # 没交成结果（模型不可用、调用失败、触顶时连收尾都没赶上）：查过的照记，结论只能是无法判断
        return _claim(cand, "inconclusive", f"反查未完成：{outcome.error or '原因不明'}", queries=outcome.queries,
                      usage=outcome.usage)
    verdict = str(outcome.result.get("verdict") or "").strip().lower()
    return _claim(cand, verdict if verdict in VERDICTS else "inconclusive", str(outcome.result.get(RATIONALE) or ""),
                  doc=outcome.rationale, queries=outcome.queries, usage=outcome.usage)


def _named(claims: list[dict[str, Any]]) -> str:
    """「「…」、「…」」：点名最多 _NAMED 句，每句截到 _QUOTE_CHARS 个字。"""
    names = "、".join(f"「{judging.clip_text(c['text'], _QUOTE_CHARS)}」" for c in claims[:_NAMED])
    return names + ("等" if len(claims) > _NAMED else "")


def _refuted_words(claims: list[dict[str, Any]]) -> str:
    """出具缺口、时间线、报错里的那句：「反方审稿推翻了 N 句结论：「…」」。不带节点名：出具契约统一加前缀。"""
    return f"反方审稿推翻了 {len(claims)} 句结论：{_named(claims)}"


def pick_items(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """节点产出、审批卡里列的几句 {unit, text, verdict}：最多 ITEMS_MAX 句，照原来的顺序。多出来的时候先留被推翻、
    被削弱、没能判断的，再按先后——被推翻的那句不能因为排得靠后就不给审批人看。"""
    keep = range(len(claims)) if len(claims) <= ITEMS_MAX else \
        sorted(sorted(range(len(claims)), key=lambda i: (_RANK.get(claims[i]["verdict"], len(_RANK)), i))[:ITEMS_MAX])
    return [{k: claims[i][k] for k in ("unit", "text", "verdict")} for i in keep]


class _Review:
    """一次审稿：逐句派核查 agent，写审稿记录，发日志。run() 的返回值进 checkpoint，只放节点产出用得到的。"""

    def __init__(self, ctx: NodeContext, *, report_id: str, title: str, doc_artifact: str, doc: dict[str, Any],
                 catalog: dict[str, Any], claims: list[judging.Candidate], max_claims: int | None,
                 max_queries: int | None, timeout_s: float | None, daily_max_usd: float | None, spec: Any,
                 masks: dict[str, Any], writer_model: str | None) -> None:
        self.ctx = ctx
        self.report_id, self.title, self.doc_artifact, self.doc, self.catalog = \
            report_id, title, doc_artifact, doc, catalog
        self.total = len(claims)
        self.picked = claims if max_claims is None else claims[:max_claims]
        self.max_claims, self.max_queries, self.timeout_s, self.daily_max_usd = \
            max_claims, max_queries, timeout_s, daily_max_usd
        self.spec, self.masks, self.writer_model = spec, masks, writer_model
        self.fetch = _fetcher()

    def _limit_words(self, limit: str) -> str:
        if limit == "max_claims":
            return f"每份报告最多审 {self.max_claims} 句"
        if limit == "max_queries":
            return f"每句最多查询 {self.max_queries} 次"
        if limit == "timeout_s":
            return f"每句时长上限 {self.timeout_s:g} 秒"
        return f"全局每日核查金额上限 {_usd(self.daily_max_usd)}"

    def _task(self, cand: judging.Candidate, sources: list[str]) -> str:
        """交给核查 agent 的任务：这句结论、它挂的证据摘录（遮罩照旧）、可以查的数据源。摘录开头的编号改成
        「原报告 Qn」：agent 自己的查询也编 Q1、Q2，不分开的话理由里引用的会是另一份快照的格子。"""
        request = judging.prepare(self.doc, self.catalog, units=[cand.unit], loader=self.fetch, masked=self.masks)
        aliases = dict.fromkeys(a for c in request.cands for a in c.aliases if a in request.excerpts)
        evidence = "\n\n".join(request.excerpts[a].replace(f"【{a}】", f"【原报告 {a}】", 1) for a in aliases)
        return CHALLENGE_TASK_PROMPT.format(
            report=self.title, section=f"，所在小节「{cand.section}」" if cand.section else "", claim=cand.text,
            evidence=evidence or "（这句没有挂依据）", sources="、".join(sources))

    async def run(self) -> dict[str, Any]:
        ctx = self.ctx
        if len(self.picked) < self.total:
            skipped = self.total - len(self.picked)
            ctx.emit(EventType.LOG, level="warn", code="challenge_limit", limit="max_claims", claims_skipped=skipped,
                     message=f"报告「{self.title}」有 {self.total} 句可审的结论，已达上限（{self._limit_words('max_claims')}），"
                             f"{skipped} 句未审")
        # 这句没挂依据时在整份报告引用的数据源里查：写作者看得到的就是这几个库
        everywhere = claim_sources(self.catalog, self.catalog, self.fetch)
        claims: list[dict[str, Any]] = []
        usage = _zero_usage()
        model: str | None = None
        warned = False
        hit: dict[str, list[int]] = {}       # 上限 → [触顶的句数, 因此没能给出结论的句数]
        broke = False                        # 全局每日金额用完了：后面的句子不再派 agent
        for cand in self.picked:
            if broke:
                hit["daily_max_usd"][1] += 1
                claims.append(_claim(cand, "inconclusive", f"已达上限（{self._limit_words('daily_max_usd')}），这句未反查"))
                continue
            try:
                sources = claim_sources(cand.aliases, self.catalog, self.fetch) or everywhere
                task_text = self._task(cand, sources) if sources else ""
            except Exception as e:  # noqa: BLE001 - 一句的摘录出了意外（坏形状的工件），只让这一句没审成
                claims.append(_claim(cand, "inconclusive", f"反查未完成：{describe_exception(e)}"))
                continue
            if not sources:
                claims.append(_claim(cand, "inconclusive",
                                     "这份报告引用的证据中没有数据库查询，没有可以反查的数据源，这句未反查"))
                continue
            outcome = await run_verify_agent(
                spec=self.spec, system=CHALLENGE_SYSTEM, task=task_text, sources=sources,
                output_schema=CHALLENGE_SCHEMA, max_queries=self.max_queries, timeout_s=self.timeout_s,
                daily_max_usd=self.daily_max_usd, emit=ctx.emit, run_id=ctx.run.run_id, node_id=ctx.node.id,
                data_versions=run_versions(ctx.run))
            model = model or outcome.model
            if not warned and outcome.model and outcome.model == self.writer_model:
                warned = True
                ctx.emit(EventType.LOG, level="warn", code="challenge_same_model", model=outcome.model,
                         message=f"反方模型与报告的写作模型相同（「{outcome.model}」），难以发现写作模型自身的错误。"
                                 f"请在设置的「核查」一节选择另一个模型，或在节点的「{CHALLENGE_LABEL['model']}」中指定")
            claims.append(_claim_of(cand, outcome))
            usage = _add_usage(usage, outcome.usage)
            if outcome.limit in _CLAIM_LIMITS:
                counted = hit.setdefault(outcome.limit, [0, 0])
                counted[0] += 1
                counted[1] += outcome.result is None
                broke = outcome.limit == "daily_max_usd"

        counts = {v: sum(1 for c in claims if c["verdict"] == v) for v in VERDICTS}
        artifact = await self._store({"report": self.report_id, "doc_artifact": self.doc_artifact, "claims": claims,
                                      "model": model})
        for limit, (touched, skipped) in hit.items():
            words = self._limit_words(limit)
            message = f"已达上限（{words}），{skipped} 句未反查" if limit == "daily_max_usd" else \
                f"有 {touched} 句反查已达上限（{words}），已按查到的结果收尾" + \
                (f"，其中 {skipped} 句未能给出结论" if skipped else "")
            ctx.emit(EventType.LOG, level="warn", code="challenge_limit", limit=limit, claims_skipped=skipped,
                     message=message)
        ctx.emit(EventType.LOG, level="info", code="challenge_done", counts=counts, artifact=artifact,
                 message=f"反方审稿完成：审了 {len(claims)} 句结论，经受住 {counts['stands']} 句、被削弱 "
                         f"{counts['weakened']} 句、被推翻 {counts['refuted']} 句、无法判断 {counts['inconclusive']} 句")
        if refuted := [c for c in claims if c["verdict"] == "refuted"]:
            ctx.emit(EventType.LOG, level="warn", code="challenge_refuted",
                     items=[{"unit": c["unit"], "text": c["text"]} for c in refuted[:ITEMS_MAX]],
                     message=_refuted_words(refuted))
        return {"artifact": artifact, "counts": counts, "usage": usage,
                "claims": [{k: c[k] for k in ("unit", "text", "verdict")} for c in claims]}

    async def _store(self, record: dict[str, Any]) -> str | None:
        try:
            return await artifact_store.put_json(record, kind="challenge", run_id=self.ctx.run.run_id,
                                                 node_id=self.ctx.node.id, meta={"report": self.report_id})
        except Exception as e:  # noqa: BLE001 - 记录写失败不毁掉运行：结论照常处置，只是理由和查询点不开
            self.ctx.emit(EventType.LOG, level="warn", code="evidence_store_failed",
                          message=f"反方审稿记录保存失败（{describe_exception(e)}），审稿的理由和查询将无法查看")
            return None


def _finish(ctx: NodeContext, state: GraphState, outcome: dict[str, Any], *, on_refuted: str) -> dict[str, Any]:
    """反查的结果 → 节点产出；被推翻的按 on_refuted 处置。重放时 outcome 取自 checkpoint，算出来的一样。"""
    claims = outcome.get("claims") or []
    refuted = [c for c in claims if c.get("verdict") == "refuted"]
    if refuted and on_refuted == "fail":
        raise NodeError(ctx.node.id, f"{_refuted_words(refuted)}。请核对这几句结论；如只需记录，可将"
                                     f"「{CHALLENGE_LABEL['on_refuted']}」改为「{CHALLENGE_ON_REFUTED_LABEL['flag']}」")
    result: dict[str, Any] = {"challenge_artifact": outcome.get("artifact"), "counts": outcome.get("counts") or {},
                              "items": pick_items(claims), "on_refuted": on_refuted}
    if refuted and on_refuted == "degrade":
        result["issuance_gaps"] = [_refuted_words(refuted)]
    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}}
    spent = outcome.get("usage") or {}
    if spent.get("calls"):
        updates["usage"] = {k: spent.get(k, 0) for k in ("input_tokens", "output_tokens", "total_tokens", "cost_usd",
                                                          "calls")}
    if outcome.get("artifact") and ledger_enabled(ctx.run):
        # 台账只记审稿记录本身（kind challenge）：它的查询不进写作目录，报告按台账编 Q1… 时看不到它们
        updates["evidence"] = [{"kind": "challenge", "node_id": ctx.node.id, "exec": next_exec(state, ctx.node.id),
                                "artifact": outcome["artifact"]}]
    return updates


# --------------------------------------------------------------------------
# 审批卡
# --------------------------------------------------------------------------


def challenge_summary(spec: GraphSpec, outputs: dict[str, Any], report_id: str) -> dict[str, Any] | None:
    """审批卡上这份报告的反方审稿概况 {counts, items, artifact, on_refuted}：审它的、已经审完的反方审稿节点给的。
    没有（没加反方审稿、还没审到、直通了）返回 None，审批卡不加这个键。几个反方审稿节点审同一份报告时取画布上
    靠后的那个。只看图和状态：重放时算出来的一样。"""
    found: dict[str, Any] | None = None
    for node in spec.nodes:
        result = outputs.get(node.id)
        if node.type != NodeType.CHALLENGE or not isinstance(result, dict) or not isinstance(result.get("counts"), dict):
            continue
        if challenge_report(spec, node)[0] != report_id:
            continue
        found = {"counts": dict(result["counts"]), "items": list(result.get("items") or [])[:ITEMS_MAX],
                 "artifact": result.get("challenge_artifact"), "on_refuted": result.get("on_refuted") or "degrade"}
    return found


__all__ = ["CHALLENGE_SYSTEM", "ITEMS_MAX", "VERDICTS", "challenge_summary", "claim_sources", "pick_claims",
           "pick_items", "run_challenge"]
