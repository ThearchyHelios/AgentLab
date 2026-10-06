"""工作流里点名的模型接入：不存在或已停用的，画布校验、发布前检查、发布时照实提出来。

运行时点名的接入不存在或已停用直接报错（providers/factory.named_provider），以前是悄悄换成演示模型。这类问题发布前
就该看得到：画布校验给警告（探索运行仍可发起，执行到那里报错），发布、发布前检查、自动修复的复核给错误、挡住
发布——正式运行到那里一定失败，或者少一块核查。

查的是执行时真会去取模型的那几处，取法和执行器一致：
- 模型调用、Agent、报告撰写、多 Agent 协作的接入；条件分支只在「让模型分类」时、结构校验只在「让模型修复」时、
  知识检索只在「让模型重排」时才取模型。这几类节点没写自己的接入时取全图默认（NodeContext.cfg）；
- 多 Agent 协作各成员的接入；报告撰写交给模型裁判结论句时，「结论句裁判」里的接入；
- 双盲复算、反方审稿的接入（核查模型）：只读节点自己的配置，不取全图默认；
- 全图默认的接入：有节点会取到它时才查。

接入表要读库，门禁本身不查库（governance.lint_for_publish），所以由接口这一层读出接入表交给 issues()，同数据目录
SQL 检查器的做法。设置里点名接入的几项由 api/settings.py 在保存时查。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.labels import field_label, type_label
from app.engine.schema import CLAIMS_JUDGE, GraphNode, GraphSpec, NodeType, ValidationIssue
from app.providers.factory import ProviderNotConfigured, list_providers, named_provider


def _cfg(spec: GraphSpec, node: GraphNode, key: str, default: Any = None) -> Any:
    """和 NodeContext.cfg 同一个规则：节点上没写（None、空串）就取全图默认。"""
    value = node.config.get(key)
    return spec.defaults.get(key, default) if value in (None, "") else value


#: 按 NodeContext.cfg 取接入的节点 → 执行时真会去取模型的条件（None 是总会取）
_TAKES_MODEL: dict[NodeType, Callable[[GraphSpec, GraphNode], bool] | None] = {
    NodeType.LLM: None,
    NodeType.AGENT: None,
    NodeType.REPORT: None,
    NodeType.SUPERVISOR: None,
    NodeType.BRANCH: lambda spec, node: _cfg(spec, node, "mode", "expression") == "llm",
    NodeType.VALIDATE: lambda spec, node: bool(_cfg(spec, node, "repair_with_llm", True)),
    NodeType.RETRIEVE: lambda spec, node: _cfg(spec, node, "rerank", "off") == "model",
}
#: 核查节点：接入只读节点自己的配置（verify_model_spec），图级默认的接入是写作模型的
_VERIFY = {NodeType.DUAL_CHECK: "的盲算模型", NodeType.CHALLENGE: "的反方模型"}


def _name(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


@dataclass(frozen=True)
class ProviderRef:
    """图里点名接入的一处：落在哪个节点（全图默认为 None）、哪个字段，点的名字，句子里怎么称呼这一处。"""

    node_id: str | None
    field: str
    name: str
    who: str


def provider_refs(spec: GraphSpec) -> list[ProviderRef]:
    """图里执行时会用到的接入点名，按画布上节点的顺序，全图默认排最后。"""
    out: list[ProviderRef] = []
    falls_back = False
    for node in spec.nodes:
        cfg = node.config
        who = f"「{node.title}」（{type_label(node.type)}）"
        if node.type in _VERIFY:
            if name := _name(cfg.get("provider")):
                out.append(ProviderRef(node.id, "provider", name, who + _VERIFY[node.type]))
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
        if node.type == NodeType.REPORT and _cfg(spec, node, "claims", "off") == CLAIMS_JUDGE \
                and isinstance(cfg.get("judge"), dict) and (name := _name(cfg["judge"].get("provider"))):
            out.append(ProviderRef(node.id, "judge.provider", name, f"{who}的{field_label('judge')}"))
    if falls_back and (name := _name(spec.defaults.get("provider"))):
        out.append(ProviderRef(None, "defaults.provider", name, field_label("defaults")))
    return out


def issues(refs: list[ProviderRef], providers: list[Any], *, level: str) -> list[ValidationIssue]:
    """点名的接入不存在或已停用的每一处一条问题。providers 是接入表（factory.list_providers），level 由调用方定：
    画布校验 warning，发布一类 error。说法取自 named_provider，和运行时报的原因、提示同一句。"""
    out: list[ValidationIssue] = []
    for ref in refs:
        try:
            named_provider(providers, ref.name)
        except ProviderNotConfigured as e:
            out.append(ValidationIssue(level=level, node_id=ref.node_id, field=ref.field,  # type: ignore[arg-type]
                                       code=f"provider.{e.code}", message=f"{ref.who}指定的{e.reason}。{e.hint}"))
    return out


async def load_issues(session: AsyncSession, spec: GraphSpec, *, level: str) -> list[ValidationIssue]:
    """读接入表、查一遍。图里没点名任何接入时不读库。"""
    refs = provider_refs(spec)
    if not refs:
        return []
    return issues(refs, await list_providers(session), level=level)
