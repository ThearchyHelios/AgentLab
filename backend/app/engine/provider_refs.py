"""工作流里点名的模型接入：不存在或已停用的，画布校验、发布前检查、发布时照实提出来。

运行时点名的接入不存在或已停用直接报错（providers/factory.named_provider），以前是悄悄换成演示模型。这类问题发布前
就该看得到：画布校验给警告（探索运行仍可发起，执行到那里报错），发布、发布前检查、自动修复的复核给错误、挡住
发布——正式运行到那里一定失败，或者少一块核查。

查的是执行时真会去取模型的那几处，取法和执行器一致：
- 模型调用、Agent、报告撰写、多 Agent 协作的接入；条件分支只在「让模型分类」时、结构校验只在「让模型修复」且允许
  重试时（max_retries 为 0 一次都不修）、知识检索只在「让模型重排」时才取模型。这几类节点没写自己的接入时取全图
  默认（NodeContext.cfg）；
- 多 Agent 协作各成员的接入；报告撰写交给模型裁判结论句时，「结论句裁判」里的接入（节点没写裁判配置时取全图默认
  里的那一份，同 nodes/report.report_judge）；
- 双盲复算、反方审稿的接入（核查模型）：只读节点自己的配置，不取全图默认。节点自己的开关关着时执行器直接跳过、
  不取模型，不查；设置里的总开关不看——它随时会被打开，打开时这一处就会失败；
- 全图默认的接入：有节点会取到它时才查。

说法分两种：检查器里有接入下拉框的几处（双盲复算、反方审稿、节点自己的结论句裁判）说「在节点中换一个接入」；
别处（节点自己的接入、协作成员、全图默认）界面上没有下拉框，说「在发布前检查中改用其他接入」——发布前检查给这类
问题配了修复（engine/autofix.py 的 provider.*：改用一个启用的接入，或不再指定）。

接入表要读库，门禁本身不查库（governance.lint_for_publish），所以由接口这一层读出接入表交进来，同数据目录
SQL 检查器的做法。设置里点名接入的几项由 api/settings.py 在保存时查。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.labels import field_label, type_label
from app.engine.schema import CLAIMS_JUDGE, GraphNode, GraphSpec, NodeType, ValidationIssue
from app.providers.factory import NAMED_FIX, ProviderNotConfigured, list_providers, named_provider

#: 这类问题的编号：provider.missing（点名的接入不存在）、provider.disabled（已停用）
CODES = frozenset({"provider.missing", "provider.disabled"})
#: 提示的后半句：这一处在检查器里有没有接入下拉框
_WHERE = {True: "或在节点中换一个接入", False: "或在「发布前检查」中改用其他接入"}


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
    """图里点名接入的一处：落在哪个节点（全图默认为 None）、哪个字段，点的名字，句子里怎么称呼这一处，
    检查器里有没有这一处的接入下拉框。"""

    node_id: str | None
    field: str
    name: str
    who: str
    editable: bool = False


def provider_refs(spec: GraphSpec) -> list[ProviderRef]:
    """图里执行时会用到的接入点名，按画布上节点的顺序，全图默认排最后。"""
    out: list[ProviderRef] = []
    falls_back = False
    default_judge = False
    for node in spec.nodes:
        cfg = node.config
        who = f"「{node.title}」（{type_label(node.type)}）"
        if node.type in _VERIFY:
            if cfg.get("enabled") is not False and (name := _name(cfg.get("provider"))):
                out.append(ProviderRef(node.id, "provider", name, who + _VERIFY[node.type], editable=True))
            continue
        if node.type not in _TAKES_MODEL:
            continue
        cond = _TAKES_MODEL[node.type]
        if cond is None or cond(spec, node):
            if name := _name(cfg.get("provider")):
                out.append(ProviderRef(node.id, "provider", name, who))
            else:
                falls_back = True
        if node.type == NodeType.SUPERVISOR and isinstance(cfg.get("agents"), list):
            for i, member in enumerate(cfg["agents"]):
                if isinstance(member, dict) and (name := _name(member.get("provider"))):
                    label = _name(member.get("name")) or f"第 {i + 1} 位"
                    out.append(ProviderRef(node.id, f"agents[{i}].provider", name, f"{who}的成员「{label}」"))
        if node.type == NodeType.REPORT and _cfg(spec, node, "claims", "off") == CLAIMS_JUDGE:
            judge = _cfg(spec, node, "judge")
            if judge is not cfg.get("judge"):
                # 节点没写裁判配置，执行时用全图默认里的那一份：问题记在全图默认上，几个报告共用时只记一条
                default_judge = True
            elif isinstance(judge, dict) and (name := _name(judge.get("provider"))):
                out.append(ProviderRef(node.id, "judge.provider", name, f"{who}的{field_label('judge')}",
                                       editable=True))
    if falls_back and (name := _name(spec.defaults.get("provider"))):
        out.append(ProviderRef(None, "defaults.provider", name, field_label("defaults")))
    judge = spec.defaults.get("judge")
    if default_judge and isinstance(judge, dict) and (name := _name(judge.get("provider"))):
        out.append(ProviderRef(None, "defaults.judge.provider", name,
                               f"{field_label('defaults')}中的{field_label('judge')}"))
    return out


def broken(refs: list[ProviderRef], providers: list[Any]) -> list[tuple[ProviderRef, ProviderNotConfigured]]:
    """点名的接入不存在或已停用的那几处，带着 named_provider 报的错（code 是 missing / disabled）。"""
    out: list[tuple[ProviderRef, ProviderNotConfigured]] = []
    for ref in refs:
        try:
            named_provider(providers, ref.name)
        except ProviderNotConfigured as e:
            out.append((ref, e))
    return out


def issues(refs: list[ProviderRef], providers: list[Any], *, level: str) -> list[ValidationIssue]:
    """点名的接入不存在或已停用的每一处一条问题。providers 是接入表（factory.list_providers），level 由调用方定：
    画布校验 warning，发布一类 error。原因和运行时报的同一句（named_provider），提示按这一处在界面上能不能改。"""
    return [ValidationIssue(level=level, node_id=ref.node_id, field=ref.field,  # type: ignore[arg-type]
                            code=f"provider.{e.code}",
                            message=f"{ref.who}指定的{e.reason}。{NAMED_FIX[e.code]}，{_WHERE[ref.editable]}")
            for ref, e in broken(refs, providers)]


async def load_issues(session: AsyncSession, spec: GraphSpec, *, level: str) -> list[ValidationIssue]:
    """读接入表、查一遍。图里没点名任何接入时不读库。"""
    refs = provider_refs(spec)
    if not refs:
        return []
    return issues(refs, await list_providers(session), level=level)


async def load_providers(session: AsyncSession, spec: GraphSpec) -> list[Any] | None:
    """发布、发布前检查、自动修复交给门禁的接入表（governance.lint_for_publish 的 providers）。图里没点名任何接入时
    不读库，给空表：没有要查的，修复也用不上候选。"""
    return await list_providers(session) if provider_refs(spec) else []
