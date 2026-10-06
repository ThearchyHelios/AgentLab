"""工作流里点名的模型接入：不存在或已停用的，画布校验、发布前检查、发布时照实提出来。

运行时点名的接入不存在或已停用直接报错（providers/factory.named_provider），以前是悄悄换成演示模型。这类问题发布前
就该看得到：画布校验给警告（探索运行仍可发起，执行到那里报错），发布、发布前检查、自动修复的复核给错误、挡住
发布——正式运行到那里一定失败，或者少一块核查。

查的是执行时真会去取模型的那几处，取法和执行器一致（uses）：
- 模型调用、Agent、报告撰写、多 Agent 协作的接入；条件分支只在「让模型分类」时、结构校验只在「让模型修复」且允许
  重试时（max_retries 为 0 一次都不修）、知识检索只在「让模型重排」时才取模型。这几类节点没写自己的接入、模型时
  各自取全图默认（NodeContext.cfg）；
- 多 Agent 协作各成员：自己没写的接入、模型各自跟随节点（再到全图默认，nodes/multi._member）；报告撰写交给模型
  裁判结论句时，「结论句裁判」里的接入、模型（节点没写裁判配置时取全图默认里的那一份，同 nodes/report.report_judge）；
- 双盲复算、反方审稿的接入、模型（核查模型）：只读节点自己的配置，不取全图默认。节点自己的开关关着时执行器直接
  跳过、不取模型，不查；设置里的总开关不看——它随时会被打开，打开时这一处就会失败；
- 全图默认的接入、模型：有节点会取到它时才查。
取到了接入的，查点名的那个接入（不存在、已停用）；哪一级都没写接入、只写了模型的，按模型名找接入，只有停用的接入
提供它时同样是问题（provider.disabled，落在写模型的那一处）——运行时 resolve_provider 报的就是这一句，而多数节点的
检查器里只能选模型，这是最常见的写法。核查、裁判的接入、模型都没写时交给设置，不归这里查。

说法分两种：检查器里有这一项下拉框的几处（双盲复算、反方审稿、节点自己的结论句裁判的接入和模型，几类节点自己的模型）
说「在节点中换一个接入 / 模型」；别处（节点自己的接入、协作成员、全图默认）界面上没有下拉框，说「在发布前检查中改用
其他接入」——发布前检查给这类问题配了修复（engine/autofix.py 的 provider.*：改用一个启用的接入，或不再指定），修复
按 land 算出修完以后执行时落到哪里，落不稳的候选不给。

接入表要读库，门禁本身不查库（governance.lint_for_publish），所以由接口这一层读出接入表交进来，同数据目录
SQL 检查器的做法。设置里点名接入的几项由 api/settings.py 在保存时查。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.labels import field_label, type_label
from app.engine.schema import CLAIMS_JUDGE, GraphNode, GraphSpec, NodeType, ValidationIssue
from app.providers.factory import (
    NAMED_FIX, ProviderNotConfigured, fallback_provider, list_providers, named_provider, provider_for_model, serves,
)

#: 这类问题的编号：provider.missing（点名的接入不存在）、provider.disabled（已停用；只写了模型、提供它的接入停用了也是它）
CODES = frozenset({"provider.missing", "provider.disabled"})
#: 提示的后半句：这一处写的是接入还是模型、检查器里有没有这一项的下拉框
_WHERE = {("provider", True): "或在节点中换一个接入", ("model", True): "或在节点中换一个模型",
          ("provider", False): "或在「发布前检查」中改用其他接入", ("model", False): "或在「发布前检查」中改用其他接入"}
#: 检查器里有模型下拉框的几类节点（前端 nodeDefs 的 MODEL_FIELDS；条件分支只在「让模型分类」时显示，也只在那时才查）
_MODEL_PICKER = frozenset({NodeType.LLM, NodeType.AGENT, NodeType.SUPERVISOR, NodeType.BRANCH, NodeType.REPORT})
#: 核查、裁判的接入和模型都没写时执行用设置里的哪一项（api/settings.verify_model_spec、judge.judge_model_spec）
_SETTINGS = {"verify": "设置中的核查模型", "judge": "设置中的裁判模型"}


def _cfg(spec: GraphSpec, node: GraphNode, key: str, default: Any = None) -> Any:
    """和 NodeContext.cfg 同一个规则：节点上没写（None、空串）就取全图默认。"""
    value = node.config.get(key)
    return spec.defaults.get(key, default) if value in (None, "") else value


def _repairs(spec: GraphSpec, node: GraphNode) -> bool:
    """结构校验会不会让模型修复：允许修复，而且至少重试一次（nodes/human.run_validate 同一个取值）。认不出的重试次数
    按会修复算：执行时它照样报错，这里宁可多提一句。"""
    if not _cfg(spec, node, "repair_with_llm", True):
        return False
    try:
        return int(_cfg(spec, node, "max_retries", 2) or 0) > 0
    except (TypeError, ValueError):
        return True


#: 按 NodeContext.cfg 取接入的节点 → 执行时真会去取模型的条件（None 是总会取）
_TAKES_MODEL: dict[NodeType, Callable[[GraphSpec, GraphNode], bool] | None] = {
    NodeType.LLM: None,
    NodeType.AGENT: None,
    NodeType.REPORT: None,
    NodeType.SUPERVISOR: None,
    NodeType.BRANCH: lambda spec, node: _cfg(spec, node, "mode", "expression") == "llm",
    NodeType.VALIDATE: _repairs,
    NodeType.RETRIEVE: lambda spec, node: _cfg(spec, node, "rerank", "off") == "model",
}
#: 核查节点：接入只读节点自己的配置（verify_model_spec），图级默认的接入是写作模型的
_VERIFY = {NodeType.DUAL_CHECK: "的盲算模型", NodeType.CHALLENGE: "的反方模型"}


def _name(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


@dataclass(frozen=True)
class ProviderRef:
    """图里写着接入或模型的一处：落在哪个节点（全图默认为 None）、哪个字段，写的名字，句子里怎么称呼这一处，检查器里
    有没有这一项的下拉框；kind 是写的接入（provider）还是模型（model）。"""

    node_id: str | None
    field: str
    name: str
    who: str
    editable: bool = False
    kind: str = "provider"


def _place(node_id: str | None, field: str, value: Any, who: str, *, editable: bool = False,
           kind: str = "provider") -> ProviderRef | None:
    name = _name(value)
    return ProviderRef(node_id, field, name, who, editable, kind) if name else None


@dataclass(frozen=True)
class Use:
    """执行时取一次模型：key 是（节点 id，哪一次：node、agents[i]、judge、verify），who 是句子里怎么称呼它；接入、模型
    各取自哪一处（哪一级都没写为 None）。settings 是核查、裁判的接入和模型都没写、交给设置的那一项（verify / judge）。"""

    key: tuple[str, str]
    who: str
    provider: ProviderRef | None
    model: ProviderRef | None
    settings: str | None = None


def _own_or_default(spec: GraphSpec, node: GraphNode, key: str, who: str) -> ProviderRef | None:
    """节点的 provider / model 取自哪一处，同 NodeContext.cfg：节点上没写（None、空串）就取全图默认。"""
    if node.config.get(key) in (None, ""):
        return _place(None, f"defaults.{key}", spec.defaults.get(key), field_label("defaults"), kind=key)
    return _place(node.id, key, node.config.get(key), who, kind=key,
                  editable=key == "model" and node.type in _MODEL_PICKER)


def uses(spec: GraphSpec) -> list[Use]:
    """图里执行时取模型的每一处，按画布上节点的顺序；只算执行时真会取模型的（见模块说明）。"""
    out: list[Use] = []
    for node in spec.nodes:
        cfg = node.config
        who = f"「{node.title}」（{type_label(node.type)}）"
        if node.type in _VERIFY:
            # 接入、模型哪一项写了就整组以节点为准，都没写交给设置（verify_model_spec）
            if cfg.get("enabled") is not False:
                vwho = who + _VERIFY[node.type]
                p = _place(node.id, "provider", cfg.get("provider"), vwho, editable=True)
                m = _place(node.id, "model", cfg.get("model"), vwho, editable=True, kind="model")
                out.append(Use((node.id, "verify"), vwho, p, m, None if p or m else "verify"))
            continue
        if node.type not in _TAKES_MODEL:
            continue
        provider, model = _own_or_default(spec, node, "provider", who), _own_or_default(spec, node, "model", who)
        cond = _TAKES_MODEL[node.type]
        if cond is None or cond(spec, node):
            out.append(Use((node.id, "node"), who, provider, model))
        if node.type == NodeType.SUPERVISOR and isinstance(cfg.get("agents"), list):
            # 成员自己写了（非空）就用自己的，否则跟随节点，同 nodes/multi._member 的 `cfg.get(...) or ctx.cfg(...)`
            for i, member in enumerate(cfg["agents"]):
                if not isinstance(member, dict):
                    continue
                label = _name(member.get("name")) or f"第 {i + 1} 位"
                mwho = f"{who}的成员「{label}」"
                own_p, own_m = member.get("provider"), member.get("model")
                out.append(Use((node.id, f"agents[{i}]"), mwho,
                               _place(node.id, f"agents[{i}].provider", own_p, mwho) if own_p else provider,
                               _place(node.id, f"agents[{i}].model", own_m, mwho, kind="model") if own_m else model))
        if node.type == NodeType.REPORT and _cfg(spec, node, "claims", "off") == CLAIMS_JUDGE:
            judge = _cfg(spec, node, "judge")
            # 节点没写裁判配置，执行时用全图默认里的那一份：问题记在全图默认上，几个报告共用时只记一条
            own = judge is cfg.get("judge")
            nid, at, jwho = (node.id, "judge", f"{who}的{field_label('judge')}") if own else \
                (None, "defaults.judge", f"{field_label('defaults')}中的{field_label('judge')}")
            judge = judge if isinstance(judge, dict) else {}
            p = _place(nid, f"{at}.provider", judge.get("provider"), jwho, editable=own)
            m = _place(nid, f"{at}.model", judge.get("model"), jwho, editable=own, kind="model")
            out.append(Use((node.id, "judge"), jwho, p, m, None if p or m else "judge"))
    return out


def provider_refs(spec: GraphSpec) -> list[ProviderRef]:
    """执行时要查的那几处，按画布上节点的顺序，全图默认排最后，同一处只记一次：取到了接入的是那个接入；哪一级都没写
    接入、只写了模型的是那个模型（按模型名找接入）。"""
    found: dict[tuple[str | None, str], ProviderRef] = {}
    for use in uses(spec):
        if (ref := use.provider or use.model) is not None:
            found.setdefault((ref.node_id, ref.field), ref)
    return sorted(found.values(), key=lambda r: r.node_id is None)


def broken(refs: list[ProviderRef], providers: list[Any]) -> list[tuple[ProviderRef, ProviderNotConfigured]]:
    """执行时会报错的那几处，带着运行时报的那个错（code 是 missing / disabled）：点名的接入按名字找（named_provider），
    只写了模型的按模型名找（provider_for_model，认不出的模型名运行时放行，不算）。"""
    out: list[tuple[ProviderRef, ProviderNotConfigured]] = []
    for ref in refs:
        try:
            if ref.kind == "model":
                provider_for_model(providers, ref.name)
            else:
                named_provider(providers, ref.name)
        except ProviderNotConfigured as e:
            out.append((ref, e))
    return out


def issues(refs: list[ProviderRef], providers: list[Any], *, level: str) -> list[ValidationIssue]:
    """执行时会报错的每一处一条问题。providers 是接入表（factory.list_providers），level 由调用方定：画布校验 warning，
    发布一类 error。原因和运行时报的同一句，提示按这一处在界面上能不能改。"""
    return [ValidationIssue(level=level, node_id=ref.node_id, field=ref.field,  # type: ignore[arg-type]
                            code=f"provider.{e.code}",
                            message=f"{ref.who}指定的{e.reason}。{NAMED_FIX[e.code]}，{_WHERE[ref.kind, ref.editable]}")
            for ref, e in broken(refs, providers)]


@dataclass(frozen=True)
class Landing:
    """执行时这一处落到哪里：where 是说给人听的（「工作流默认设置指定的接入「甲」」……），model 是用哪个模型（交给设置时
    为 None），ok 是修复能不能把它落在这里。"""

    where: str
    model: str | None
    ok: bool


def land(use: Use, providers: list[Any]) -> Landing:
    """按执行器的取法（get_chat_model → resolve_provider → build_chat_model）算这一处执行时用哪个接入、哪个模型。

    ok 为真才算落得稳：点名的接入启用着、有模型可用，并且提供这一处写的模型——拿 A 家的模型名去调 B 家必然失败；
    只写了模型的，按模型名找到启用的接入。按模型名落到停用的接入（运行时报错）、模型名认不出来（落到第一个可用的
    接入，它多半不提供）、一个启用的接入都没有（悄悄换成内置演示模型）都不算。交给设置的（核查、裁判）算落得稳：
    设置保存时查过点名的接入，之后接入出了问题设置页会标出来。"""
    if use.settings:
        return Landing(_SETTINGS[use.settings], None, True)
    model = use.model.name if use.model is not None else None
    if use.provider is not None:
        try:
            found = named_provider(providers, use.provider.name)
        except ProviderNotConfigured as e:
            return Landing(e.reason, model, False)
        where = f"{use.provider.who}指定的接入「{found.name}」"
    elif model:
        try:
            found = provider_for_model(providers, model)
        except ProviderNotConfigured as e:
            return Landing(e.reason, model, False)
        if found is None:
            return Landing(f"认不出的模型「{model}」", model, False)
        where = f"提供模型「{model}」的接入「{found.name}」"
    else:
        found = fallback_provider(providers)
        if found is None:
            return Landing("内置演示模型", None, False)
        where = f"第一个可用的模型接入「{found.name}」"
    used = model or found.default_model
    return Landing(where, used, bool(used) and (model is None or serves(found, model)))


async def load_issues(session: AsyncSession, spec: GraphSpec, *, level: str) -> list[ValidationIssue]:
    """读接入表、查一遍。图里执行时取模型的几处都没写接入和模型时不读库。"""
    refs = provider_refs(spec)
    if not refs:
        return []
    return issues(refs, await list_providers(session), level=level)


async def load_providers(session: AsyncSession, spec: GraphSpec) -> list[Any] | None:
    """发布、发布前检查、自动修复交给门禁的接入表（governance.lint_for_publish 的 providers）。图里执行时取模型的几处
    都没写接入和模型时不读库，给空表：没有要查的，修复也用不上候选。"""
    return await list_providers(session) if provider_refs(spec) else []
