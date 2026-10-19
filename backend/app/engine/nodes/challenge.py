"""反方审稿节点：报告写完后，挑几句关键结论，各派一个只读 agent 站在反方主动找反例。

结论句裁判（engine/judge.py）只看写作者挂的证据，判「说法有没有被证据支持」；可证据本身就可能挑过——只查了含
退款的销售额，只看了一个月。反方审稿换个方向：不看证据够不够，自己写查询去证伪。

1. 选句：报告文档里的结论句按裁判的预筛排好（judge.candidates / claim_priority：挂了依据的、带比较或因果的在前），
   取前「最多审几句」句；裁判判成「不是结论」的不审。同一份文档永远选出同样的句子、同样的顺序
2. 反查：每句一个只读 agent（verify_agent.run_verify_agent）。工具只给这句引用的查询所在的数据源（指标按它的输入
   取自的查询算，合并查询按它合并的那几个源算）；这句没挂依据，就在这份报告引用的数据源里查。任务里给这句结论
   和它挂的证据摘录（judge.prepare，数据源遮罩的列照旧不给；摘录里原报告的编号都标成「原报告 Qn」，和 agent 自己
   的 Q1 分开），要它先想清楚「如果这句不成立，数据里会看到什么」，写只读查询去找，最后交结论（经受住 / 被削弱 /
   被推翻 / 无法判断）和理由。理由里的数字只能引用它自己查询的格子，裸数字由 compose_doc 标出来。
   遮罩：agent 的查询结果在工具边界就遮好了；这份报告引用的查询快照里的遮罩值（按 SQL 认出改了名字的遮罩列，同审批
   质询）反查之前就交给查询工具，agent 查到写着同一个号码的别的表时也看不到。任务里的证据摘录先遮（_task，和工具
   边界同一套口径）：遮罩列、按 SQL 认出改了名字的遮罩列（buyer_phone AS 手机号）不给，文字格、SQL 的字符串和注释里
   的遮罩值按值换，和遮罩列做等值比较的字面量都换；数值格、范围比较的阈值、LIMIT 照原样。理由正文和理由文档仍按值换成
   「已遮罩」（换了写法的号码也认，agent 查询时遮掉的值也算），这句结论原文里写着的、agent 自己 SQL 里照原样留着的
   阈值和行数不换，目录里的列名同质询照快照换（_masked_rationale）；换完正文、原文里还认得出的，这句记为无法判断、
   理由不记（_kept，同审批质询的最后一道关）
3. 出处：一条查询都没执行成功的，判什么都记为无法判断；判「被推翻」的，理由里还得引用到自己的查询结果——它要让
   正式出具降档，审稿记录里得查得到凭什么。不采信时系统写明原因，反方原本的结论和理由照录在后面（纯文字：解析
   不了的引用写成「（引用无效）」，裸数字注明「（无出处）」）。这句该查的数据源都不存在或已停用、核查模型本身用
   不了时不派 agent（模型用不了的，后面的句子照同一个原因记，不再逐句去建模型）；只停用了一部分的照常派，理由
   末尾由系统注明哪几个没查（写进理由文档，见 with_note）
4. 记录：每句的结论、理由（渲染后的正文和组装好的文档）、查询快照、用量写进审稿记录（challenge_artifact），随运行
   封存。查询照常发 tool.end（role: verify，带查询快照，可下钻）；台账只记 kind challenge，绝不记 query——核查的
   查询进了写作目录，下游的写作者会把它们当成可以引用的证据。有句子没能反查的另记一条 challenge_unreviewed、
   只查了部分数据源的另记一条 challenge_sources_lost，不然时间线上只有一句「审了 N 句」，看不出根本没审成、
   没审全（这两个代码还不在期 3 约定的日志代码表里，界面认不得时照原话显示）
5. 处置：被推翻的按 on_refuted 处置。degrade（默认）在产出里写 issuance_gaps，成果节点的出具契约照单收进缺口、
   出具降档（nodes/io.py 的 _upstream_issuance_gaps，所以反方审稿要接在成果节点上游，画布校验会提醒）；fail 判节点
   失败；flag 只记录。产出和审批卡里的 on_refuted 是实际生效的那一种（只有 degrade / flag，见 _finish）；degrades
   说被推翻的到底会不会让出具降档（按降档处置、又接在带出具契约的成果节点上游），unreviewed 是未能反查的句数（核查
   模型不可用、触顶没给出结论、数据源不在了、报告没有引用数据库查询、运行时长上限将至没来得及审……这几句记为无法
   判断，但反查没有完成，触顶的可能已经查过几次）。每一句都记为无法判断，所以 unreviewed 不会大于 counts 里的
   inconclusive。审批卡（human.py 的 _reviewed_reports）附上审稿概况，据这两项说「出具会降档」「N 句未能反查」

上限：句数、每句查询次数、每句时长、全局每日金额（核查和裁判各记各的账，engine/spend.py）。都按发起时快照的核查
设置（run.options["verify"]），节点上写了的盖过它：审批停下期间改了设置，恢复、接着跑仍按发起时的那份。没有快照
的运行按设置的当前值。「启用」节点上写了就按节点（写开启时全局关闭也照审），没写跟随设置；不启用时直通，报告没有
可审的结论句、报告文档读不到时也直通（都记一条 challenge_skipped）。

逐句串行反查：并行的话几句的工具调用在时间线上交错，看不出哪条查询在审哪一句；每日余额按先后扣，结果也才确定。
串行就可能撞上运行的时长上限（默认 5 句、每句 90 秒再加 30 秒收尾宽限，最坏 600 秒，正好是一段执行的上限），整次运行
被中止。所以每句开始之前看这一段执行还剩多少时间（engine/deadline.py）：给下游留出余量后不够再审一句，这句和后面的
都不再开始，记为未能反查，节点照常收尾；够的话这句的时长上限按剩余时间收紧，到点照样按查到的结果收尾。这两种在
时间线上都带 cause: run_deadline（没开始的另起一条 challenge_unreviewed，收紧后到点的是 challenge_limit 里单独的
一条），界面据此说运行时长上限将至，不叫人调大每句的上限，也不叫人去查模型和数据源。
整个反查连同存审稿记录、发日志都在一个 @task 里：接着跑时取 checkpoint 里的结果，不重复查库、不重复记账，时间线
上也不再发一遍。结果带着输入指纹，接着跑时改了审稿本身的配置就重审；同一次执行里的重试沿用已经审完的（见 _reviewed）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Collection, Iterable
from dataclasses import replace
from typing import Any

from langgraph.func import task
from sqlalchemy import select

from app.core import artifact_store
from app.core.artifact_store import canonical_json, content_hash
from app.core.errors import describe_exception
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.data import catalog_view
from app.engine import deadline, verify_agent
from app.engine import judge as judging
from app.engine.context import NodeContext, NodeError
from app.engine.evidence import ENTITY_KINDS, iter_units, ledger_enabled, next_exec
from app.engine.judge import _usd
from app.engine.merge_query import MERGE_SOURCE
from app.engine.replay import _in_graph
from app.engine.labels import CHALLENGE_LABEL, CHALLENGE_ON_REFUTED_LABEL
from app.engine.schema import GraphSpec, NodeType, _ancestors, challenge_problems, challenge_report
from app.engine.state import GraphState
from app.engine.verify_agent import RATIONALE, VerifyOutcome, run_verify_agent
from app.tools.datasource import run_versions

logger = logging.getLogger(__name__)

#: 四种结论：经受住 / 被削弱 / 被推翻 / 无法判断。counts 的键照这个顺序
VERDICTS = ("stands", "weakened", "refuted", "inconclusive")
#: 理由按值遮之后还认得出遮罩值时，这句的理由换成这段系统说明（_kept）
LEAKED = "反方的理由里有设置了遮罩的值，无法遮住，这句的结论不予采信，记为无法判断"
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
#: 系统改判时照录反方原本的结论用的叫法（界面的叫法见 lib/terms.ts 的 CHALLENGE_TEXT.verdict）
_VERDICT_LABEL = {"stands": "经受住", "weakened": "被削弱", "refuted": "被推翻", "inconclusive": "无法判断"}
#: 照录反方的理由时：解析不了的引用换成的字、裸数字后面加的注（见 _said）
_INVALID_REF = "（引用无效）"
_UNSOURCED = "（无出处）"
#: 解析不了的引用在渲染后的正文里的占位（evidence.placeholder），连同两边隔开它的一个空格
_PLACEHOLDER = re.compile(r" ?⟦\?[^⟧]*⟧ ?")
#: 接着跑时 checkpoint 里缓存的审稿最多翻几条去找这一份配置的（每条都是按先前某一份配置审的，对不上就往后翻）
_STALE_LIMIT = 16
#: 同一次执行里审完的结果放在 RunContext.extra 的这个键下：{(节点, 第几次执行, 输入指纹): 审稿结果}。重试沿用它
_REVIEWS_KEY = "challenge_reviews"
#: 运行时长上限将至、不再开始的句子（engine/deadline.py）：每句的理由，时间线上的原因（这句和后面的一共几句）
_LATE_CLAIM = "本次运行即将达到 {seconds:g} 秒的时长上限，为保证运行按时完成，这句未反查"
_LATE_REST = "本次运行即将达到 {seconds:g} 秒的时长上限，为保证运行按时完成，剩下 {count} 句未审"
#: 时长按剩余时间收紧过的句子触了时长上限：单记一类（_limit_words 说运行时长上限将至，不说「每句时长上限 90 秒」——
#: 那会叫人调大每句的上限，调大了只会更快撞线），challenge_limit 照记 timeout_s（老界面认得），另带 cause
_SQUEEZED = "squeezed"

CHALLENGE_SYSTEM = (
    "你是反方审稿人：报告已经写完，你站在反方，专门找其中一句结论的反例。你不写报告，也不改报告。\n"
    "- 先想清楚：如果这句结论不成立，数据里会看到什么；再写只读查询去找这样的反例\n"
    "- 换一个角度核对：按别的维度拆开、换时间范围、核对口径和过滤条件（退款、作废、重复记录）、找被忽略的数据，"
    "不要只是重复写作者的查询\n"
    "- verdict 四选一：stands 认真找过、没有找到反例；weakened 大体成立，但有重要的例外或前提，或者把范围、程度、"
    "原因说过了头；refuted 查到的数据和这句结论相矛盾；inconclusive 查不到判断所需的数据\n"
    "- 没有找到反例不等于结论被推翻：只有数据和结论冲突时才判 refuted\n"
    "- 结论要有出处：一条查询都没执行成功时，判什么都不会被采信；判 refuted 时，理由里至少要引用一处你自己查询的"
    "结果：写 [[v:Qn.rX.列名]] 引用一格，或者在那句的句末写 [[see:Qn]]，否则同样不会被采信"
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
    outcome = await _reviewed(ctx, state, review)
    return _finish(ctx, state, outcome, on_refuted=str(config.get("on_refuted") or "degrade"))


async def _reviewed(ctx: NodeContext, state: GraphState, review: _Review) -> dict[str, Any]:
    """这一次执行的审稿结果。

    - 进 checkpoint：整个反查在一个 @task 里，接着跑时直接取这里的结果，不再查库、不再记账（每日账也记在这里面）；
      事件都在这里面发，取缓存时不会再发一遍
    - 带输入指纹（_Review.key）：接着跑时可以带着改过配置的图（runner.continue_failed）。只改了「结论被推翻时」的，
      审稿沿用（判失败的报错就是这么建议的）；改了要审的报告、反方模型、上限的，缓存里那份是按旧配置审的，对不上就
      往后翻、重新审——不然换了报告，审批卡会把对另一份报告的审稿挂上来（做法同 report.py 的裁判）
    - 同一次执行里的重试（节点的「失败重试次数」）沿用已经审完的：重试把执行器整个再调一遍，task 成了这次执行里的
      又一次调用，不命中缓存。再审一遍既多花一份钱，模型换个说法还会把「被推翻」重审掉，最严的处置反倒放行
    """
    reviews = ctx.run.extra.setdefault(_REVIEWS_KEY, {})
    slot = (ctx.node.id, next_exec(state, ctx.node.id), review.key)
    if slot not in reviews:
        reviews[slot] = await _review_once(review)
    return reviews[slot]


async def _review_once(review: _Review) -> dict[str, Any]:
    if _in_graph():
        @task
        async def challenge_step(key: str) -> dict[str, Any]:
            return {**await review.run(), "key": key}

        for _ in range(_STALE_LIMIT):
            outcome = await challenge_step(review.key)
            if outcome.get("key") == review.key:
                return outcome
    return await review.run()


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


def _claim_of(cand: judging.Candidate, outcome: VerifyOutcome) -> tuple[dict[str, Any], str | None]:
    """反查 agent 交回的 → (审稿记录里的一句, 这句没能反查的原因)。反查过了的原因是 None。

    反方的结论要有出处才采信：一条查询都没执行成功的（没有可用的工具、查询全部失败、根本没查），判什么都记为无法
    判断——零查询的「被推翻」能让正式出具降档，零查询的「经受住」会在审批卡上给人假安心。判「被推翻」的，理由里还得
    引用到自己的查询结果（resolved 的 [[v:]] / [[see:]]），审稿记录里才查得到凭什么推翻。"""
    if outcome.result is None:
        # 没交成结果（模型不可用、调用失败、触顶时连收尾都没赶上）：查过的照记，结论只能是无法判断
        why = outcome.error or "原因不明"
        return _claim(cand, "inconclusive", f"反查未完成：{why}", queries=outcome.queries, usage=outcome.usage), why
    verdict = str(outcome.result.get("verdict") or "").strip().lower()
    verdict = verdict if verdict in VERDICTS else "inconclusive"
    rationale = str(outcome.result.get(RATIONALE) or "")
    if not outcome.queries:
        why = "反查没有执行成功任何查询"
        return _unfounded(cand, outcome, verdict, why), why
    if verdict == "refuted" and not _cites(outcome.rationale):
        # 理由是空的（只写了占位词的，核查 agent 已经按没给理由交回，见 verify_agent._no_rationale）：照实说没给理由，
        # 不说「理由没有引用到…」再接「反方没有给出理由」
        why = "理由没有引用到任何查询结果" if rationale.strip() else None
        return _unfounded(cand, outcome, verdict, why), None
    return _claim(cand, verdict, rationale, doc=outcome.rationale, queries=outcome.queries, usage=outcome.usage), None


def _spared(seed: Any, claim: str) -> Any:
    """报告快照里的遮罩值（seed）去掉这句结论原文里写着的，交给这一句的反查。seed 本身不动（下一句另算）。"""
    known = seed.copy()
    known.protect([claim])
    return known


def _masked_rationale(outcome: VerifyOutcome, secrets: Any, *, given: Iterable[str | None],
                      kinds: dict[str, Any] | None = None, masks: dict[str, Any] | None = None) -> VerifyOutcome:
    """反方的理由里的遮罩值换成「已遮罩」，换了写法的号码也认（199-1474-2785、+86 …）。值是这份报告引用的查询快照里的
    （secrets，反查之前就交给了查询工具），加上 agent 查询时在工具边界遮掉的（outcome.masked_values）；被审的那句结论
    原文里写着的（given）已经摆在读者眼前，不换。agent 自己 SQL 里照原样留着的范围边界、LIMIT 的行数（_shown：kinds 是
    数据源的类型，masks 是数据源现在设的遮罩列）在正文里复述也不换，同审批质询的回答（「金额不低于 20000 元」）。
    反方 agent 的查询结果在工具边界就遮好了、证据摘录也先遮了，这里是第二道防线。
    换的办法同审批质询的回答（_scrubbed_source）：原文里的值先换掉，再照换过列名的目录重新组装理由文档（同质询的
    _renamed_entry：快照遮好的样子列名换过了，引用对得上）；引用到的格子按遮好的快照渲染（文字格里夹着的遮罩值换掉
    那一截）。理由、目录列名里都认不出遮罩值的原样交回。换完还认得出的过最后一道关（_kept）。"""
    from app.engine.evidence import compose_doc
    from app.engine.inquiry import _leaked, _renamed_entry
    from app.engine.masking import _masked_view, _scrubbed_source, _Secrets

    doc, result = outcome.rationale, outcome.result
    # agent 查询时遮掉的值先收（masked_values 里已经排在报告的值前面）：报告的值收满了上限 SECRETS_MAX，也不丢 agent 查到的
    pool = _Secrets()
    pool.merge(outcome.masked_values)
    pool.merge(secrets)
    pool.protect(given)
    if result is None or not isinstance(doc, dict) or not pool.strong:
        return outcome
    pool.show(_shown(outcome, pool, kinds or {}, masks or {}))
    pool.prepare()
    source = str(doc.get("source") or "")
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    renamed = {alias: _renamed_entry(entry, pool) for alias, entry in catalog.items()}
    # 正文里认得出遮罩值的（照原样留着的阈值写进单元格引用里的也算，_leaked）、原文、目录列名里有的，才重新组装
    if renamed == catalog and not (_leaked(doc, pool) or pool.spans(source, prose=True)):
        return _kept(outcome, pool)
    views: dict[str, Any] = {}
    fetch = _fetcher()
    for query in outcome.queries:
        snapshot = fetch(query.get("artifact"))
        if isinstance(snapshot, dict) and isinstance(snapshot.get("rows"), list):
            views[str(query["artifact"])] = _masked_view(snapshot, set(), pool, embedded=True)[0]
    def load(artifact: str) -> Any:
        return views[artifact] if artifact in views else artifact_store.load(artifact)

    try:
        again = compose_doc(_scrubbed_source(source, pool), renamed, node_id=str(doc.get("node_id") or ""),
                            run_id=doc.get("run_id"), loader=load)
    except Exception:  # noqa: BLE001 - 重新组装出了意外，值也不能留着
        # 正文按值换掉，理由文档不要了（界面照正文显示；判「被推翻」的因此查不到出处，按无法判断记）
        said = pool.scrub(str(doc.get("markdown") or ""), prose=True)
        return _kept(replace(outcome, rationale=None, result={**result, RATIONALE: said}), pool)
    return _kept(replace(outcome, rationale=again, result={**result, RATIONALE: again["markdown"]}), pool)


def _shown(outcome: VerifyOutcome, pool: Any, kinds: dict[str, Any], masks: dict[str, Any]) -> set[str]:
    """反方 agent 自己的 SQL 里照原样留着的数（范围比较的边界、LIMIT 的行数，审批质询的 _shown_literals）：理由正文里
    复述它们不换，最后一道关也不算露了遮罩值——它们随 SQL 存进审稿记录、摆在读者眼前，换掉只会让理由和 SQL 对不上。
    哪些名字是遮罩列按那份快照认（_derived_hidden 的名字，含改了名字的），快照取不到的按数据源现在设的遮罩列。"""
    from app.engine.inquiry import _shown_literals
    from app.engine.masking import _derived_hidden

    fetch = _fetcher()
    sqls: list[tuple[str, str | None, set[str]]] = []
    for query in outcome.queries:
        name = str(query.get("source") or "")
        base = {str(c).lower() for c in masks.get(name) or []}
        snapshot = fetch(query.get("artifact"))
        if isinstance(snapshot, dict):
            base |= {str(c).lower() for c in snapshot.get("mask_columns") or [] if isinstance(c, str)}
        compared = _derived_hidden(snapshot, base, kinds.get(name))[1] if isinstance(snapshot, dict) and base \
            else base
        sqls.append((str(query.get("sql") or ""), kinds.get(name), compared))
    return _shown_literals(sqls, pool)


def _kept(outcome: VerifyOutcome, pool: Any) -> VerifyOutcome:
    """最后一道关（同审批质询的 _leaked）：按值遮之后，理由正文、理由文档的正文和原文里还认得出遮罩值的（换了写法的号码
    也算），这句的结论不采信、理由不记：记为无法判断，理由换成系统的说明（LEAKED）。遮不住的「被推翻」不能让出具降档，
    也不能把号码存进审稿记录、随运行封存。只看正文和原文：目录里的列名是 agent 自己起的名字（不低于20000的笔数），
    已经按值换过（_masked_rationale），再拿来认会把恰好等于遮罩值的阈值当成露了值，整句改判；agent 自己 SQL 里照原样
    留着的阈值写在正文里不算（pool.show），渲染进单元格引用里的照算（同质询的 _leaked）。"""
    from app.engine.inquiry import _leaked

    doc = outcome.rationale if isinstance(outcome.rationale, dict) else {}
    said = str((outcome.result or {}).get(RATIONALE) or "")
    if not (pool.spans(said, prose=True) or pool.spans(str(doc.get("source") or ""), prose=True)
            or (doc and _leaked(doc, pool))):
        return outcome
    logger.warning("反方审稿的理由里有遮罩值遮不住，这句记为无法判断（运行 %s）", doc.get("run_id"))
    return replace(outcome, rationale=None, result={**(outcome.result or {}), "verdict": "inconclusive",
                                                    RATIONALE: LEAKED})


def _cites(doc: dict[str, Any] | None) -> bool:
    """理由文档里有没有一处引用解析到了 agent 自己的查询结果（compose_doc 只把解析成功的引用记进 unit.cites）。"""
    return isinstance(doc, dict) and any(unit.get("cites") for _, unit in iter_units(doc))


def _unfounded(cand: judging.Candidate, outcome: VerifyOutcome, verdict: str, why: str | None) -> dict[str, Any]:
    """没有出处的结论：记为无法判断，系统写明原因，反方原本的结论和理由照录在后面（照录的写法见 _said）。why 为 None：
    反方判了结论却根本没给理由，一句话说清（不再先说「理由没有引用到…」、后面又说「没有给出理由」）。
    rationale_doc 为 None：这段话是系统说的，界面照 rationale 显示，不拿反方那份文档冒充理由。查过的照记。"""
    label = _VERDICT_LABEL[verdict]
    if why is None:
        text = f"反方判了「{label}」，但没有给出理由，不予采信，记为无法判断。"
        return _claim(cand, "inconclusive", text, queries=outcome.queries, usage=outcome.usage)
    taken = f"反方给出的「{label}」不予采信，记为无法判断" if verdict != "inconclusive" else "记为无法判断"
    rationale = _said(outcome.rationale, str((outcome.result or {}).get(RATIONALE) or "")).strip()
    said = f"反方的理由：{rationale}" if rationale else "反方没有给出理由。"
    return _claim(cand, "inconclusive", f"{why}，{taken}。{said}", queries=outcome.queries, usage=outcome.usage)


def _said(doc: dict[str, Any] | None, rendered: str) -> str:
    """反方的理由照录成纯文字。界面没有文档可画、按文字显示，文档里标出来的东西得写成字：解析不了的引用渲染成内部
    的占位（⟦?v:Q1.r9.列⟧），换成「（引用无效）」，连同两边隔开它的空格；裸数字后面注明「（无出处）」。按片段在正文
    里的偏移改（片段互不重叠，从后往前改偏移不乱）；文档和正文对不上时只换占位。"""
    if not isinstance(doc, dict) or doc.get("markdown") != rendered:
        return _PLACEHOLDER.sub(_INVALID_REF, rendered)
    edits: list[tuple[int, int, str]] = []
    for _, unit in iter_units(doc):
        for seg in unit.get("segments") or []:
            span = seg.get("span")
            if not (isinstance(span, list) and len(span) == 2 and all(isinstance(x, int) for x in span)):
                continue
            if seg.get("issue") == "uncited_number":
                edits.append((span[1], span[1], _UNSOURCED))
            elif seg.get("issue") == "unresolved_ref" and str(seg.get("text") or "").startswith("⟦?"):
                edits.append((span[0], span[1], _INVALID_REF))
    text = rendered
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text.replace(f" {_INVALID_REF}", _INVALID_REF).replace(f"{_INVALID_REF} ", _INVALID_REF)


def with_note(doc: dict[str, Any], note: str) -> dict[str, Any]:
    """理由文档末尾另起一段，写系统的说明（界面按文档画理由，说明只写进 rationale 正文的话看不到）。

    照 compose_doc 的样子接：段与段之间的空行是这一句开头的结构片段，说明是一句连接性的话，块、句、片段的编号接着
    往下数，统计跟着加——和把说明写进原文再组装出来的一模一样。只是说明不挂出处、不查裸数字（是系统的话，数据源的
    名字里也可能有数字），source（反方的原文）不动。传进来的文档不改，返回新的一份。"""
    markdown = str(doc.get("markdown") or "")
    blocks = list(doc.get("blocks") or [])
    units = [u for b in blocks if isinstance(b, dict) for u in b.get("units") or []]
    first = sum(len(u.get("segments") or []) for u in units if isinstance(u, dict))
    gap = "\n\n" if markdown else ""
    start = len(markdown) + len(gap)
    pieces = [("structural", gap, len(markdown))] if gap else []
    pieces.append(("text", note, start))
    segments = [{"kind": kind, "text": text, "state": "neutral", "span": [at, at + len(text)], "id": f"s{first + i}"}
                for i, (kind, text, at) in enumerate(pieces)]
    unit = {"id": f"u{len(units)}", "kind": "connective", "span": [start, start + len(note)], "cites": [], "see": [],
            "segments": segments}
    out = {**doc, "markdown": markdown + gap + note,
           "blocks": [*blocks, {"type": "paragraph", "units": [unit], "id": f"b{len(blocks)}"}]}
    if isinstance(doc.get("stats"), dict):
        out["stats"] = stats = dict(doc["stats"])
        for key, more in (("units", 1), ("segments", len(segments)), ("connective", 1)):
            if isinstance(stats.get(key), int):
                stats[key] += more
    return out


def _noted(claim: dict[str, Any], note: str) -> dict[str, Any]:
    """审稿记录里的一句，理由末尾加上系统的说明：有理由文档的写进文档（with_note），正文照文档；没有的接在正文后面。"""
    doc = claim.get("rationale_doc")
    if isinstance(doc, dict):
        doc = with_note(doc, note)
        return {**claim, "rationale": doc["markdown"], "rationale_doc": doc}
    said = str(claim.get("rationale") or "").rstrip()
    return {**claim, "rationale": f"{said}\n\n{note}" if said else note}


async def _usable_sources(names: Iterable[str]) -> set[str]:
    """这几个数据源里现在能查的：存在、没有停用（核查建工具时也只认这样的，tools/datasource.build_datasource_tools）。
    报告写完到审稿之间源被停用了，这句就没有可查的源：不派 agent，免得白建一次模型、白花一次钱。"""
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return set()
    async with SessionLocal() as session:
        rows = await session.execute(
            select(DataSource.name).where(DataSource.name.in_(wanted), DataSource.enabled.is_(True)))
    return {str(name) for name in rows.scalars()}


def mark_original(excerpt: str, alias: str, queries: Collection[str]) -> str:
    """证据摘录里指向原报告编号的写法都标上「原报告」：开头的【编号】，和正文里的「查询 Qn」（指标的输入取自哪次
    查询、那次查询的 SQL，表名、字段名出现在哪次查询）。核查 agent 自己的查询也编 Q1、Q2…，不分开的话，它在理由里
    引用的 Q1 会被当成原报告那份快照的格子。只认摘录自己的这两种写法、只认这份报告目录里的查询编号：数据里的值
    （季度列里的 Q1）不动。"""
    text = excerpt.replace(f"【{alias}】", f"【原报告 {alias}】", 1)
    if not queries:
        return text
    names = "|".join(re.escape(q) for q in sorted(queries, key=len, reverse=True))
    return re.sub(rf"查询 ({names})(?!\w)", r"原报告 \1", text)


def _quoted(names: Iterable[str]) -> str:
    """「a」、「b」：点名几个数据源。"""
    return "、".join(f"「{n}」" for n in names)


def _named(claims: list[dict[str, Any]]) -> str:
    """「「…」、「…」」：点名最多 _NAMED 句，每句截到 _QUOTE_CHARS 个字。"""
    names = "、".join(f"「{judging.clip_text(c['text'], _QUOTE_CHARS)}」" for c in claims[:_NAMED])
    return names + ("等" if len(claims) > _NAMED else "")


def _refuted_gap(claims: list[dict[str, Any]]) -> str:
    """出具缺口里的那句：「推翻了 N 句结论：「…」」。不带节点名也不带「反方审稿」：出具契约统一加「反方审稿「节点名」：」
    （没改名的节点只加「反方审稿：」），正文再说一遍就成了「反方审稿：反方审稿推翻了……」。"""
    return f"推翻了 {len(claims)} 句结论：{_named(claims)}"


def _refuted_words(claims: list[dict[str, Any]]) -> str:
    """时间线、报错里的那句：「反方审稿推翻了 N 句结论：「…」」。它们不经出具契约加前缀，主语得自己带上。"""
    return f"反方审稿{_refuted_gap(claims)}"


def pick_items(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """节点产出、审批卡里列的几句 {unit, text, verdict}：最多 ITEMS_MAX 句，照原来的顺序。多出来的时候先留被推翻、
    被削弱、没能判断的，再按先后——被推翻的那句不能因为排得靠后就不给审批人看。"""
    keep = range(len(claims)) if len(claims) <= ITEMS_MAX else \
        sorted(sorted(range(len(claims)), key=lambda i: (_RANK.get(claims[i]["verdict"], len(_RANK)), i))[:ITEMS_MAX])
    return [{k: claims[i][k] for k in ("unit", "text", "verdict")} for i in keep]


class _Review:
    """一次审稿：逐句派核查 agent，写审稿记录，发日志。run() 的返回值进 checkpoint，只放节点产出用得到的。

    key 是这次审稿的输入指纹：审哪份报告的哪一稿、反方模型、四个上限、数据源现在的遮罩（摘录按它遮）。「结论被推翻时」
    不在里面：它只管审完之后怎么处置，改了它接着跑不必重审。"""

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
        #: 报告用到的数据源的类型（摘录按 SQL 认改了名字的遮罩列时按它的方言解析）。run() 里查好
        self.kinds: dict[str, Any] = {}
        #: 这一段执行的总时长（engine/deadline.py）：有句子的时长按剩余时间收紧过、或者不再开始时记下，说明和日志里写
        self.run_seconds: float | None = None
        #: 这份报告目录里的查询编号：摘录里指向它们的写法标上「原报告」（mark_original）
        self.queries = {a for a, e in catalog.items() if isinstance(e, dict) and e.get("kind") == "query"}
        self.key = content_hash(canonical_json({
            "report": report_id, "doc": doc_artifact, "model": [spec.provider, spec.model],
            "limits": [max_claims, max_queries, timeout_s, daily_max_usd], "masks": masks,
        }))

    def _limit_words(self, limit: str) -> str:
        if limit == "max_claims":
            return f"每份报告最多审 {self.max_claims} 句"
        if limit == _SQUEEZED:
            return f"本次运行即将达到 {self.run_seconds:g} 秒的时长上限，反查时长已按剩余时间收紧"
        if limit == "max_queries":
            return f"每句最多查询 {self.max_queries} 次"
        if limit == "timeout_s":
            return f"每句时长上限 {self.timeout_s:g} 秒"
        return f"全局每日核查金额上限 {_usd(self.daily_max_usd)}"

    def _capped(self) -> str:
        """当天的核查金额用完、这句一次模型都没调用时的理由：派出去就撞上的和后面不再派出的，是同一回事。"""
        return f"已达上限（{self._limit_words('daily_max_usd')}），这句未反查"

    def _task(self, cand: judging.Candidate, sources: list[str], lost: list[str], *, own: bool,
              known: Any = None) -> str:
        """交给核查 agent 的任务：这句结论、它挂的证据摘录（原报告的编号标明，见 mark_original）、可以查的数据源；该查的
        源有停用了的，也告诉它查不了哪几个，免得它把查不到当成没有反例。own：源是这句自己引用的，不是没挂依据、按整份
        报告用到的源查的。
        摘录先遮，模型从任务里也看不到遮罩值（judge.prepare 的 mask，inquiry.ExcerptMask，和工具边界同一套口径）：遮罩列、
        按 SQL 认出改了名字的（buyer_phone AS 手机号）不给；文字格、SQL 的字符串和注释里的遮罩值按值换成「已遮罩」，换了
        写法的号码也认，和遮罩列做等值比较的字面量都换；数值格、范围比较的阈值、LIMIT 照原样（known：报告快照里的遮罩值
        去掉这句结论原文里写着的，同交给查询工具的那一份）。解析 SQL、按值换是纯计算，在线程里做。"""
        from app.engine.inquiry import ExcerptMask   # inquiry 导入了这个模块，用到时再导入

        request = judging.prepare(self.doc, self.catalog, units=[cand.unit], loader=self.fetch, masked=self.masks,
                                  mask=ExcerptMask(self.kinds, known))
        aliases = dict.fromkeys(a for c in request.cands for a in c.aliases if a in request.excerpts)
        evidence = "\n\n".join(mark_original(request.excerpts[a], a, self.queries) for a in aliases)
        gone = f"（{'这句还引用了' if own else '这份报告还用到了'}{_quoted(lost)}，已不存在或已停用，无法查询）" \
            if lost else ""
        return CHALLENGE_TASK_PROMPT.format(
            report=self.title, section=f"，所在小节「{cand.section}」" if cand.section else "", claim=cand.text,
            evidence=evidence or "（这句没有挂依据）", sources="、".join(sources) + gone)

    async def run(self) -> dict[str, Any]:
        ctx = self.ctx
        if len(self.picked) < self.total:
            skipped = self.total - len(self.picked)
            ctx.emit(EventType.LOG, level="warn", code="challenge_limit", limit="max_claims", claims_skipped=skipped,
                     message=f"报告「{self.title}」有 {self.total} 句可审的结论，已达上限（{self._limit_words('max_claims')}），"
                             f"{skipped} 句未审")
        # 这句没挂依据时在整份报告引用的数据源里查：写作者看得到的就是这几个库
        everywhere = claim_sources(self.catalog, self.catalog, self.fetch)
        usable = await _usable_sources(everywhere)
        self.kinds = await self._kinds(everywhere)
        # 报告引用的查询快照里遮罩列的值：反查之前交给查询工具（agent 查到写着同一个号码的别的表时看不到），反查之后
        # 据此换掉理由里的遮罩值（_masked_rationale）
        seed = await self._seed()
        claims: list[dict[str, Any]] = []
        usage = _zero_usage()
        model: str | None = None
        warned = False
        hit: dict[str, list[int]] = {}       # 上限 → [触顶的句数, 因此没能给出结论的句数]
        broke = False                        # 全局每日金额用完了：后面的句子不再派 agent
        broken: str | None = None            # 核查模型本身用不了：后面的句子照这个原因记，不再逐句去建模型
        unreviewed: list[str] = []           # 没能反查的句子各自的原因（触顶的由 challenge_limit 说，不在这里）
        partial: list[list[str]] = []        # 反查了、但该查的源有停用了的句子：各自没查成的源
        late: tuple[str, str] | None = None  # 运行时长上限将至：(每句的理由, 时间线上的原因)，后面的句子都照这个记
        overdue = 0                          # 因此没开始的句数：时间线另起一条（带 cause），不混进 unreviewed
        # 下游这一段里还要调模型的节点：各多留一份余量（engine/deadline.py）。图在运行里不变，算一次
        after = deadline.models_after(ctx.run.spec, ctx.node.id)

        def unreached(cand: judging.Candidate, rationale: str, why: str) -> None:
            claims.append(_claim(cand, "inconclusive", rationale))
            unreviewed.append(why)

        for at, cand in enumerate(self.picked):
            if broke:
                hit["daily_max_usd"][1] += 1
                claims.append(_claim(cand, "inconclusive", self._capped()))
                continue
            if broken is not None:
                unreached(cand, f"反查未完成：{broken}", broken)
                continue
            if late is not None:
                claims.append(_claim(cand, "inconclusive", late[0]))
                overdue += 1
                continue
            # 这句结论原文里写着的值已经摆在读者眼前，不当遮罩值。上万个值过一遍要几十毫秒，放进线程
            known = await asyncio.to_thread(_spared, seed, cand.text)
            try:
                own = claim_sources(cand.aliases, self.catalog, self.fetch)
                sources = own or everywhere
                live = [s for s in sources if s in usable]
                lost = [s for s in sources if s not in usable]
                task_text = await asyncio.to_thread(self._task, cand, live, lost, own=bool(own), known=known) \
                    if live else ""
            except Exception as e:  # noqa: BLE001 - 一句的摘录出了意外（坏形状的工件），只让这一句没审成
                why = f"整理证据摘录时出错（{describe_exception(e)}）"
                unreached(cand, f"反查未完成：{why}", why)
                continue
            # 没挂依据的句子查的是整份报告用到的源，不是「这句引用的」
            where = "这句引用的" if own else "这份报告用到的"
            if not sources:
                why = "这份报告引用的证据中没有数据库查询，没有可以反查的数据源"
                unreached(cand, f"{why}，这句未反查", why)
                continue
            if not live:
                why = f"数据源{_quoted(sources)}已不存在或已停用"
                unreached(cand, f"{where}{why}，没有可以反查的数据源，这句未反查", why)
                continue
            # 派出去之前看这一段执行还剩多少时间：给下游留出余量后不够再审一句，这句和后面的都不再开始、记为未能反查，
            # 节点照常收尾，运行不被中止；够的话这句的时长上限按剩余时间收紧。在 @task 里定，接着运行时不再重新定
            allot = deadline.allot(self.timeout_s, grace=verify_agent.SETTLE_GRACE_S, after=after)
            if not allot.start:
                self.run_seconds = allot.seconds
                late = (_LATE_CLAIM.format(seconds=allot.seconds),
                        _LATE_REST.format(seconds=allot.seconds, count=len(self.picked) - at))
                claims.append(_claim(cand, "inconclusive", late[0]))
                overdue += 1
                continue
            if allot.tightened:
                self.run_seconds = allot.seconds
            # 收紧过的：核查 agent 到点自己发的 verify_limit 改说运行时长上限将至（deadline.relabel），不写收紧后的秒数
            outcome = await run_verify_agent(
                spec=self.spec, system=CHALLENGE_SYSTEM, task=task_text, sources=live,
                output_schema=CHALLENGE_SCHEMA, max_queries=self.max_queries, timeout_s=allot.timeout_s,
                daily_max_usd=self.daily_max_usd, emit=deadline.relabel(ctx.emit, allot), run_id=ctx.run.run_id,
                node_id=ctx.node.id, data_versions=run_versions(ctx.run), masked_values=known,
                catalog_view=catalog_view.of_run(ctx.run))
            outcome = await asyncio.to_thread(_masked_rationale, outcome, known, given=[cand.text], kinds=self.kinds,
                                              masks=self.masks)
            if outcome.error and not outcome.model:
                broken = outcome.error      # 连模型都没建成（没配好、接入出错）：换一句也一样
            model = model or outcome.model
            if not warned and outcome.model and outcome.model == self.writer_model:
                warned = True
                ctx.emit(EventType.LOG, level="warn", code="challenge_same_model", model=outcome.model,
                         message=f"反方模型与报告的写作模型相同（「{outcome.model}」），难以发现写作模型自身的错误。"
                                 f"请在设置的「核查」一节选择另一个模型，或在节点的「{CHALLENGE_LABEL['model']}」中指定")
            claim, why = _claim_of(cand, outcome)
            # 触顶的计在哪一组：时长按剩余时间收紧过的另算一组（_SQUEEZED），说运行时长上限将至
            group = _SQUEEZED if outcome.limit == "timeout_s" and allot.tightened else outcome.limit
            if outcome.limit == "daily_max_usd" and outcome.result is None and not outcome.usage.get("calls"):
                # 派出去就撞上每日金额上限、一次模型都没调用：和后面不再派出的句子是同一回事，同一种说法
                claim = _claim(cand, "inconclusive", self._capped(), queries=outcome.queries, usage=outcome.usage)
            elif outcome.result is None and outcome.error == verify_agent.SETTLE_TIMEOUT:
                # 查完了、交结论那次超过时长上限：照上限说，和时间线上的 challenge_limit 一致。核查 agent 的原话（「收尾时
                # 超过时长上限，没能交结果」）是内部的说法，不摆上审批卡
                words = self._limit_words(_SQUEEZED if allot.tightened else "timeout_s")
                claim = _claim(cand, "inconclusive", f"反查未完成：已达上限（{words}）", queries=outcome.queries,
                               usage=outcome.usage)
            elif lost and why is None:
                # 反查了，可该查的源有停用了的：判断照常采信，理由末尾写明哪几个没查（只写在任务里的话，审稿记录和
                # 审批卡上的「经受住」看着就像每个源都查过了）
                claim = _noted(claim, f"注：{where}数据源{_quoted(lost)}已不存在或已停用，未能查询，以上判断不含其中的数据。")
                partial.append(lost)
            claims.append(claim)
            usage = _add_usage(usage, outcome.usage)
            if outcome.limit in _CLAIM_LIMITS:
                counted = hit.setdefault(group, [0, 0])
                counted[0] += 1
                counted[1] += why is not None
                broke = outcome.limit == "daily_max_usd"
            elif why is not None:
                unreviewed.append(why)

        counts = {v: sum(1 for c in claims if c["verdict"] == v) for v in VERDICTS}
        # 没能反查的句数：没派出去、派出去却没查成的（unreviewed 里各有原因），运行时长上限将至没开始的，加上触顶时没能给出
        # 结论的。都记为无法判断，可「无法判断 2 句」既可能是查了、数据不够，也可能是根本没查：审批卡要分开说
        missed = len(unreviewed) + overdue + sum(skipped for _, skipped in hit.values())
        # 报告按这次运行里的 id 记（子工作流里的写「子工作流节点 id/报告」，同 report.checked、报告文档上的 node_id）：
        # 证据面板、可验证报告按它认审的是哪份报告
        artifact = await self._store({"report": self.ctx.run.global_id(self.report_id), "doc_artifact": self.doc_artifact,
                                      "claims": claims, "model": model})
        for limit, (touched, skipped) in hit.items():
            words = self._limit_words(limit)
            message = f"已达上限（{words}），{skipped} 句未反查" if limit == "daily_max_usd" else \
                f"有 {touched} 句反查已达上限（{words}），已按查到的结果收尾" + \
                (f"，其中 {skipped} 句未能给出结论" if skipped else "")
            # 收紧过的那一组照记 timeout_s（老界面认得），另带 cause、这一段的总时长和触顶的句数：界面据此说运行时长
            # 上限将至，不叫人调高每句的上限；同一节点里还有句子触了自己的上限时，两条也分得开
            squeezed = {"cause": deadline.RUN_DEADLINE, "run_seconds": self.run_seconds, "claims": touched} \
                if limit == _SQUEEZED else {}
            ctx.emit(EventType.LOG, level="warn", code="challenge_limit",
                     limit="timeout_s" if limit == _SQUEEZED else limit, claims_skipped=skipped, message=message,
                     **squeezed)
        if unreviewed:
            more = "等" if len(set(unreviewed)) > 1 else ""
            ctx.emit(EventType.LOG, level="warn", code="challenge_unreviewed", claims_skipped=len(unreviewed),
                     reason=unreviewed[0],
                     message=f"反方审稿有 {len(unreviewed)} 句结论未能反查，已记为无法判断：{unreviewed[0]}{more}")
        if overdue and late is not None:
            # 运行时长上限将至没开始的另起一条，带 cause：不被前面句子的原因盖掉、藏进「等」里，界面也不会为它叫人去查
            # 模型和数据源
            ctx.emit(EventType.LOG, level="warn", code="challenge_unreviewed", cause=deadline.RUN_DEADLINE,
                     run_seconds=self.run_seconds, claims_skipped=overdue, reason=late[1],
                     message=f"反方审稿有 {overdue} 句结论未能反查，已记为无法判断：{late[1]}")
        if partial:
            names = list(dict.fromkeys(s for gone in partial for s in gone))
            ctx.emit(EventType.LOG, level="warn", code="challenge_sources_lost", claims=len(partial), sources=names,
                     message=f"反方审稿有 {len(partial)} 句结论只查了部分数据源：{_quoted(names)}已不存在或已停用，"
                             f"未能查询，{'这句' if len(partial) == 1 else '这几句'}的判断不含其中的数据")
        # 真正查过的和没查成的分开说：「无法判断 2 句」可能是数据不够，也可能是一条查询都没执行成功
        idle = sum(1 for c in claims if not c["queries"])
        ctx.emit(EventType.LOG, level="info", code="challenge_done", counts=counts, artifact=artifact,
                 message=f"反方审稿完成：审了 {len(claims)} 句结论，经受住 {counts['stands']} 句、被削弱 "
                         f"{counts['weakened']} 句、被推翻 {counts['refuted']} 句、无法判断 {counts['inconclusive']} 句"
                         + (f"，其中 {idle} 句未能反查（没有执行成功任何查询）" if idle else ""))
        if refuted := [c for c in claims if c["verdict"] == "refuted"]:
            ctx.emit(EventType.LOG, level="warn", code="challenge_refuted",
                     items=[{"unit": c["unit"], "text": c["text"]} for c in refuted[:ITEMS_MAX]],
                     message=_refuted_words(refuted))
        return {"artifact": artifact, "counts": counts, "usage": usage, "unreviewed": missed,
                "claims": [{k: c[k] for k in ("unit", "text", "verdict")} for c in claims]}

    async def _seed(self) -> Any:
        """这份报告目录里的查询快照里遮罩列的值（engine/masking 的 _Secrets）。收法同审批质询收报告快照
        （inquiry.snapshot_secrets：按 SQL 认出改了名字的遮罩列，查询当时记下的遮罩加上数据源现在设的）。"""
        from app.engine.inquiry import snapshot_secrets   # inquiry 导入了这个模块，用到时再导入

        return await snapshot_secrets(self.fetch(entry.get("artifact")) for entry in self.catalog.values()
                                      if isinstance(entry, dict) and entry.get("kind") == "query")

    async def _kinds(self, names: Iterable[str]) -> dict[str, Any]:
        """这几个数据源的类型（摘录按 SQL 认改了名字的遮罩列时按它的方言解析）。names 是整份报告用到的数据源
        （claim_sources：查询的、合并查询合并的、指标输入取自的、表和字段的）。"""
        from app.engine.inquiry import _source_kinds   # inquiry 导入了这个模块，用到时再导入

        return await _source_kinds(names)

    async def _store(self, record: dict[str, Any]) -> str | None:
        try:
            return await artifact_store.put_json(record, kind="challenge", run_id=self.ctx.run.run_id,
                                                 node_id=self.ctx.run.global_id(self.ctx.node.id),
                                                 meta={"report": self.ctx.run.global_id(self.report_id)})
        except Exception as e:  # noqa: BLE001 - 记录写失败不毁掉运行：结论照常处置，只是理由和查询点不开
            self.ctx.emit(EventType.LOG, level="warn", code="evidence_store_failed",
                          message=f"反方审稿记录保存失败（{describe_exception(e)}），审稿的理由和查询将无法查看")
            return None


def _finish(ctx: NodeContext, state: GraphState, outcome: dict[str, Any], *, on_refuted: str) -> dict[str, Any]:
    """反查的结果 → 节点产出；被推翻的按 on_refuted 处置。重放时 outcome 取自 checkpoint，算出来的一样。

    产出里的 on_refuted 写实际生效的处置，只有 degrade / flag 两种（期 3 的约定：审批卡据此决定写不写「出具会降档」）。
    配的是「报错并停止运行」却走到了这里，只有两种情形：没有句子被推翻，什么都没处置，记成只标注；有被推翻的却又配了
    「出错时：记录错误并继续」（画布校验拦这个组合，跳过校验的旧图才会走到）——这时报错会被当成普通错误吞掉，被推翻
    的结论照常完整出具，比默认的降档还松，所以改按出具降档处置。"""
    claims = outcome.get("claims") or []
    refuted = [c for c in claims if c.get("verdict") == "refuted"]
    if on_refuted == "fail":
        if refuted and ctx.node.config.get("on_error") != "continue":
            which = "这句" if len(refuted) == 1 else "这几句"
            raise NodeError(ctx.node.id, f"{_refuted_words(refuted)}。请核对{which}结论；如只需记录，可将"
                                         f"「{CHALLENGE_LABEL['on_refuted']}」改为「{CHALLENGE_ON_REFUTED_LABEL['flag']}」")
        on_refuted = "degrade" if refuted else "flag"
    result: dict[str, Any] = {"challenge_artifact": outcome.get("artifact"), "counts": outcome.get("counts") or {},
                              "items": pick_items(claims), "on_refuted": on_refuted,
                              "degrades": on_refuted == "degrade" and feeds_issuance(ctx.run.spec, ctx.node.id)}
    # checkpoint 里升级前审的结果没有这一项：不知道就不写，审批卡照旧不说
    if isinstance(outcome.get("unreviewed"), int):
        result["unreviewed"] = outcome["unreviewed"]
    if refuted and on_refuted == "degrade":
        result["issuance_gaps"] = [_refuted_gap(refuted)]
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


def feeds_issuance(spec: GraphSpec, node_id: str) -> bool:
    """这个节点在不在某个带出具契约的成果节点上游。成果节点的出具契约只收上游报上来的缺口（nodes/io.py 的
    _upstream_issuance_gaps）：接在侧枝上、或者在成果之后，被推翻的结论记了缺口也进不了出具。认法同画布校验
    （schema._check_challenge 的「不在成果节点上游」警告）。"""
    return any(node_id in _ancestors(spec, n.id) for n in spec.nodes
               if n.type == NodeType.OUTPUT and n.config.get("contract"))


def challenge_summary(spec: GraphSpec, outputs: dict[str, Any], report_id: str) -> dict[str, Any] | None:
    """审批卡上这份报告的反方审稿概况 {counts, items, artifact, on_refuted, unreviewed, degrades}：审它的、已经审完的
    反方审稿节点给的。没有（没加反方审稿、还没审到、直通了）返回 None，审批卡不加这个键。几个反方审稿节点审同一份报告
    时取画布上靠后的那个。只看图和状态：重放时算出来的一样。unreviewed、degrades 是期 4 加的：升级前的产出没有，
    概况也不带（审批卡按旧规则说）。"""
    found: dict[str, Any] | None = None
    for node in spec.nodes:
        result = outputs.get(node.id)
        if node.type != NodeType.CHALLENGE or not isinstance(result, dict) or not isinstance(result.get("counts"), dict):
            continue
        if challenge_report(spec, node)[0] != report_id:
            continue
        found = {"counts": dict(result["counts"]), "items": list(result.get("items") or [])[:ITEMS_MAX],
                 "artifact": result.get("challenge_artifact"), "on_refuted": result.get("on_refuted") or "degrade"}
        if isinstance(result.get("unreviewed"), int) and not isinstance(result["unreviewed"], bool):
            found["unreviewed"] = result["unreviewed"]
        if isinstance(result.get("degrades"), bool):
            found["degrades"] = result["degrades"]
    return found


__all__ = ["CHALLENGE_SYSTEM", "ITEMS_MAX", "VERDICTS", "challenge_summary", "claim_sources", "feeds_issuance",
           "mark_original", "pick_claims", "pick_items", "run_challenge", "with_note"]
