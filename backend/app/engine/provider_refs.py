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
按 land 算出修完以后执行时落到哪里，落不稳的候选不给。一个候选都落不稳时修复为空，这时不指到发布前检查（那里什么
都不给），说去设置里核对、启用，再说卡在哪里（第一处落不稳的地方，比如成员只写了停用接入的模型）——不然人去设置里
找那个写错的名字，找不到，也看不出真正挡住修复的是别处。点名的名字恰好是某个启用接入的模型时，末尾补一句这是哪个
接入的模型、在这一处怎么用它（_model_note：按检查器里有哪几栏写，不叫人去一个没有的「接入」栏留空）。

节点没写模型、点名的接入（自己的或全图默认的）本身不存在或已停用时，校验那条「没有指定模型，将使用接入「X」的
默认模型」（schema.MODEL_UNSET）和这里的问题互相矛盾，由 quiet_unset_model 去掉前者。

接入表要读库，门禁本身不查库（governance.lint_for_publish），所以由接口这一层读出接入表交进来，同数据目录
SQL 检查器的做法。设置里点名接入的几项由 api/settings.py 在保存时查。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.labels import field_label, judge_label, type_label
from app.engine.schema import CLAIMS_JUDGE, MODEL_UNSET, GraphNode, GraphSpec, NodeType, ValidationIssue
from app.providers.factory import (
    NAMED_FIX, ProviderNotConfigured, fallback_provider, list_providers, model_fact, model_note, named_provider,
    provider_for_model, serves,
)

#: 这类问题的编号：provider.missing（点名的接入不存在）、provider.disabled（已停用；只写了模型、提供它的接入停用了也是它）
CODES = frozenset({"provider.missing", "provider.disabled"})
#: 提示的后半句：这一处写的是接入还是模型、检查器里有没有这一项的下拉框。检查器里没有下拉框的，发布前检查给了修复
#: 才指到那里（IN_CHECK），否则不写后半句
_WHERE = {"provider": "或在节点中换一个接入", "model": "或在节点中换一个模型"}
IN_CHECK = "或在「发布前检查」中改用其他接入"
#: 检查器里没有下拉框、发布前检查也给不出修复时，提示末尾说卡在哪里（Landing.why）
NO_SWITCH = "无法改用其他接入："
#: 一个启用的接入都没有：没点名接入的执行时换成内置演示模型，点名的调不了
NO_ENABLED = "没有启用的模型接入"
#: 修完以后执行时落到演示类接入（kind 为 mock）：说明里照实写。K1 要防的就是悄悄换成演示模型——落到它是人选的，
#: 得让人知道选的是什么
DEMO_LANDING = "（演示用，不调用真实模型）"
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
#: 核查节点：接入只读节点自己的配置（verify_model_spec），图级默认的接入是写作模型的。值是修复标题里的称呼
#:（「选择……的盲算模型改用的模型接入」）；问题里说「……的「盲算模型 · 接入」指定的」，写检查器上那一栏的叫法
_VERIFY = {NodeType.DUAL_CHECK: "的盲算模型", NodeType.CHALLENGE: "的反方模型"}


def _name(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


@dataclass(frozen=True)
class ProviderRef:
    """图里写着接入或模型的一处：落在哪个节点（全图默认为 None）、哪个字段，写的名字，句子里怎么称呼这一处，检查器里
    有没有这一项的下拉框；kind 是写的接入（provider）还是模型（model）。

    who 是修复标题里的称呼（「选择{who}改用的模型接入」）；said 是「{said}指定的模型接入「X」」里的主语，默认同 who。
    核查节点两者不同：指定接入的是「盲算模型 · 接入」这一栏，不是盲算模型本身。"""

    node_id: str | None
    field: str
    name: str
    who: str
    editable: bool = False
    kind: str = "provider"
    said: str = ""

    @property
    def subject(self) -> str:
        return self.said or self.who


def _place(node_id: str | None, field: str, value: Any, who: str, *, editable: bool = False,
           kind: str = "provider", said: str = "") -> ProviderRef | None:
    name = _name(value)
    return ProviderRef(node_id, field, name, who, editable, kind, said) if name else None


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


def node_uses(spec: GraphSpec, node: GraphNode) -> list[Use]:
    """一个节点执行时取模型的每一处（uses 里属于它的那几项）。只读这个节点自己的配置和全图默认：改一个节点的配置
    只影响它自己这几处，修复规划（autofix 的 provider.*）按这一点只重算被改的那个节点，不再每试一个候选就把整张图
    重算一遍——几十个节点都点名同一个停用接入时，那样画布每改一下都要多算上万处。"""
    out: list[Use] = []
    cfg = node.config
    who = f"「{node.title}」（{type_label(node.type)}）"
    if node.type in _VERIFY:
        # 接入、模型哪一项写了就整组以节点为准，都没写交给设置（verify_model_spec）
        if cfg.get("enabled") is not False:
            vwho = who + _VERIFY[node.type]
            p = _place(node.id, "provider", cfg.get("provider"), vwho, editable=True,
                       said=f"{who}的「{field_label('provider', node.type)}」")
            m = _place(node.id, "model", cfg.get("model"), vwho, editable=True, kind="model",
                       said=f"{who}的「{field_label('model', node.type)}」")
            out.append(Use((node.id, "verify"), vwho, p, m, None if p or m else "verify"))
        return out
    if node.type not in _TAKES_MODEL:
        return out
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


def uses(spec: GraphSpec) -> list[Use]:
    """图里执行时取模型的每一处，按画布上节点的顺序；只算执行时真会取模型的（见模块说明）。"""
    return [use for node in spec.nodes for use in node_uses(spec, node)]


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


def _tail(spec: GraphSpec, ref: ProviderRef, providers: list[Any]) -> str:
    """提示的后半句：检查器里有这一项的下拉框，指到节点；没有的，发布前检查给得出修复才指到那里——一个候选都落不稳
    时（比如一个启用的接入都没有，或者改动波及的别处都落不稳）那里什么都不给，指过去只会让人白找，改说卡在哪里。
    只问给不给得出、卡在哪里（autofix.provider_fixable，找到第一个落得稳的候选就停），不拼整条修复。"""
    if ref.editable:
        return f"，{_WHERE[ref.kind]}"
    # 在这里导入：autofix 导入了门禁（governance），门禁又在 lint_for_publish 里导入本模块
    from app.engine.autofix import provider_fixable

    fixable, stuck = provider_fixable(spec, ref, providers)
    if fixable:
        return f"，{IN_CHECK}"
    return f"。{NO_SWITCH}{stuck}" if stuck else ""


def _model_note(spec: GraphSpec, ref: ProviderRef, e: ProviderNotConfigured, providers: list[Any]) -> str:
    """点名的名字恰好是某个启用接入的模型时补的那一句，按这一处在检查器里有哪几栏写（运行时报错、设置页的说法见
    factory.model_note）。多数节点的检查器里没有接入这一栏，叫人「接入留空」做不到：
    - 核查节点、报告自己的结论句裁判：接入、模型两栏都在（模型是输入框），照设置页的写法；
    - 检查器里有模型下拉框、没有接入一栏的节点（模型调用、Agent……）：在节点的「模型」中选它，接入在发布前检查里
      改用这个接入或不指定（修复里有哪一项说哪一项）；
    - 协作成员、全图默认这些两栏都没有的：发布前检查里能改用这个接入时说一句，否则只说这是哪个接入的模型。"""
    name, owner = e.name, e.model_owner
    if ref.editable:
        node = spec.node_map()[ref.node_id] if ref.node_id is not None else None
        labels = (judge_label("model"), judge_label("provider")) if ref.field.startswith("judge.") or node is None \
            else (field_label("model", node.type), field_label("provider", node.type))
        return model_note(name, owner, f"「{labels[0]}」", f"「{labels[1]}」", "填写")
    from app.engine.autofix import PROVIDER_CLEAR, provider_choice

    fact = model_fact(name, owner)
    choice = provider_choice(spec, ref, providers)
    values = {o["value"] for o in choice["options"]} if choice else set()
    node = spec.node_map().get(ref.node_id) if ref.node_id is not None else None
    if node is not None and ref.field == "provider" and node.type in _MODEL_PICKER:
        picks = ([f"改用「{owner}」"] if owner in values else []) + \
            (["选择「不指定接入」"] if PROVIDER_CLEAR in values else [])
        if not picks:
            return fact
        return f"{fact}：如需使用该模型，请在节点的「{field_label('model')}」中选择它，并在「发布前检查」中{'或'.join(picks)}"
    return f"{fact}，可在「发布前检查」中改用「{owner}」" if owner in values else fact


def issues(spec: GraphSpec, providers: list[Any], *, level: str) -> list[ValidationIssue]:
    """执行时会报错的每一处一条问题。providers 是接入表（factory.list_providers），level 由调用方定：画布校验 warning，
    发布一类 error。原因和运行时报的同一句，提示按这一处在界面上能不能改（_tail）；点名的名字恰好是某个启用接入的
    模型时补一句（_model_note）。"""
    out = []
    for ref, e in broken(provider_refs(spec), providers):
        note = f"。{_model_note(spec, ref, e, providers)}" if e.model_owner else ""
        out.append(ValidationIssue(level=level, node_id=ref.node_id, field=ref.field,  # type: ignore[arg-type]
                                   code=f"provider.{e.code}",
                                   message=f"{ref.subject}指定的{e.reason}。{NAMED_FIX[e.code]}"
                                           f"{_tail(spec, ref, providers)}{note}"))
    return out


def quiet_unset_model(spec: GraphSpec, found: list[Any], provider_issues: list[Any]) -> list[Any]:
    """去掉被接入问题盖住的「没有指定模型，将使用接入「X」的默认模型」（schema.MODEL_UNSET）。

    节点没写模型、取到的接入（自己点名的，没写就是全图默认点名的，同 NodeContext.cfg）本身不存在或已停用时，那条说
    会用 X 的默认模型照常执行，接入那一条说执行到这里会失败，读者无从判断哪条是真的。接入那一条带着原因和改法，留它。
    found 是校验的问题（ValidationIssue），provider_issues 是 issues() 的结果；画布校验和发布一类各调一次。"""
    hit = {(i.node_id, i.field) for i in provider_issues if i.code in CODES}
    if not hit:
        return found
    nodes = spec.node_map()

    def covered(issue: Any) -> bool:
        if issue.code != MODEL_UNSET or (node := nodes.get(issue.node_id)) is None:
            return False
        own = node.config.get("provider") not in (None, "")
        return ((node.id, "provider") if own else (None, "defaults.provider")) in hit

    return [i for i in found if not covered(i)]


@dataclass(frozen=True)
class Landing:
    """执行时这一处落到哪里：where 是说给人听的（「工作流默认设置指定的接入「甲」」……），model 是用哪个模型（交给设置时
    为 None），ok 是修复能不能把它落在这里。落到演示类接入时 demo 为真、where 末尾带着 DEMO_LANDING。落不稳时 why 说
    为什么（一句话，接在「无法改用其他接入：」后面），修复一个候选都给不出时拿第一处的 why 告诉人卡在哪里。"""

    where: str
    model: str | None
    ok: bool
    demo: bool = False
    why: str = ""


def land(use: Use, providers: list[Any]) -> Landing:
    """按执行器的取法（get_chat_model → resolve_provider → build_chat_model）算这一处执行时用哪个接入、哪个模型。

    ok 为真才算落得稳：点名的接入启用着、有模型可用，并且提供这一处写的模型——拿 A 家的模型名去调 B 家必然失败；
    只写了模型的，按模型名找到启用的接入。按模型名落到停用的接入（运行时报错）、模型名认不出来（落到第一个可用的
    接入，它多半不提供）、一个启用的接入都没有（悄悄换成内置演示模型）都不算。

    交给设置的（核查、裁判）算落得稳：设置保存时查过点名的接入，之后接入出了问题设置页会标出来。启用的接入全是演示类
    时，设置这一级不管写的是什么，执行时落到的都是演示模型（点名的启用接入、按模型名找到的、取值链最后的第一个可用的
    接入，只能是演示类），照实带上 DEMO_LANDING；一个启用的接入都没有时不算落得稳，同上面「换成内置演示模型」。"""
    if use.settings:
        enabled = [p for p in providers if getattr(p, "enabled", False)]
        if not enabled:
            return Landing(_SETTINGS[use.settings], None, False, why=NO_ENABLED)
        demo = all(getattr(p, "kind", None) == "mock" for p in enabled)
        return Landing(_SETTINGS[use.settings] + (DEMO_LANDING if demo else ""), None, True, demo=demo)
    model = use.model.name if use.model is not None else None
    if use.provider is not None:
        try:
            found = named_provider(providers, use.provider.name)
        except ProviderNotConfigured as e:
            return Landing(e.reason, model, False, why=f"{use.provider.subject}指定的{e.reason}")
        where = f"{use.provider.subject}指定的接入「{found.name}」"
    elif use.model is not None and model:
        try:
            found = provider_for_model(providers, model)
        except ProviderNotConfigured as e:
            return Landing(e.reason, model, False, why=f"{use.model.subject}指定的{e.reason}")
        if found is None:
            return Landing(f"认不出的模型「{model}」", model, False,
                           why=f"{use.model.subject}指定的模型「{model}」不属于任何启用的模型接入")
        where = f"提供模型「{model}」的接入「{found.name}」"
    else:
        found = fallback_provider(providers)
        if found is None:
            return Landing("内置演示模型", None, False, why=NO_ENABLED)
        where = f"第一个可用的模型接入「{found.name}」"
    demo = getattr(found, "kind", None) == "mock"
    if demo:
        where += DEMO_LANDING
    used = model or found.default_model
    if not used:
        return Landing(where, None, False, demo, why=f"模型接入「{found.name}」没有可用的模型")
    if use.model is not None and model and not serves(found, model):
        return Landing(where, model, False, demo, why=f"模型接入「{found.name}」不提供{use.model.subject}指定的模型「{model}」")
    return Landing(where, used, True, demo)


async def load_issues(session: AsyncSession, spec: GraphSpec, *, level: str) -> list[ValidationIssue]:
    """读接入表、查一遍。图里执行时取模型的几处都没写接入和模型时不读库。"""
    if not provider_refs(spec):
        return []
    return issues(spec, await list_providers(session), level=level)


async def load_providers(session: AsyncSession, spec: GraphSpec) -> list[Any] | None:
    """发布、发布前检查、自动修复交给门禁的接入表（governance.lint_for_publish 的 providers）。图里执行时取模型的几处
    都没写接入和模型时不读库，给空表：没有要查的，修复也用不上候选。"""
    return await list_providers(session) if provider_refs(spec) else []
