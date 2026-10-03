"""查询快照上的 SQL 检查问题：口径卡、出具契约、写作目录、运行日志共用一套读法。

数据源查询工具执行后对照数据目录检查 SQL，结果存在查询快照的 checks 里（data/sqlcheck.py）。error 级的
（一对多关联后重复计算、存量跨期加总）意味着这条查询算出来的数不可靠：

- 口径卡用它算出的指标标「存疑」（nodes/metrics.py）；
- 报告直接引用它的格、或拿它当依据，出具同样降档（nodes/io.py）；
- 写作目录里提醒写作者（engine/evidence.catalog_prompt）。

合并查询在内存 SQLite 上执行，不对照数据目录，它的快照没有自己的 checks，但记着 inputs：顺着往下找，问题记在
出问题的那条源查询上。几个指标、几处引用出自同一条查询时，问题只算一次（problems_by_query 按快照 id 归并）。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.core import artifact_store

#: 合并查询快照的 source（engine/merge_query.MERGE_SOURCE）。这里写死同一个字面量，免得为一个常量引入执行器
MERGE_SOURCE = "合并查询"
#: 一条链最多追几层合并：画布上不会无限嵌套，这只是防御坏快照
_MAX_DEPTH = 8


@dataclass
class QueryProblem:
    """一条没通过 SQL 检查的查询快照。

    artifact 是出问题的那条查询快照（合并查询的话是它的某个输入）；node_id 是跑这条查询的节点，合并快照的 inputs
    里记着，直接查询的由调用方按证据台账补（node_of）；checks 是 error 级的检查结果（快照里的原样）。
    """

    artifact: str
    node_id: str | None
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        """每条检查的问题那半句（message 的第一句），去重。改法留在证据面板的查询步骤里看。"""
        out = [str(c.get("message") or "").split("。")[0] for c in self.checks]
        return list(dict.fromkeys(p for p in out if p))

    @property
    def codes(self) -> list[str]:
        return list(dict.fromkeys(str(c.get("code")) for c in self.checks if c.get("code")))


def _load(artifact: str, loader: Callable[[str], Any] | None) -> Any:
    try:
        return (loader or artifact_store.load)(artifact)
    except Exception:  # noqa: BLE001 - 快照读不出来、被改过：这里不报，证据层另有说法
        return None


def query_problems(artifact: Any, *, loader: Callable[[str], Any] | None = None,
                   _seen: set[str] | None = None, _node: str | None = None, _depth: int = 0) -> list[QueryProblem]:
    """这份查询快照（或合并快照的各个输入）里有 error 级 SQL 检查的那几条查询，一条一个 QueryProblem。

    读不出来的快照、没有问题的快照都不出现。自引用、成环的坏快照不会卡死。
    """
    if not isinstance(artifact, str) or not artifact or _depth > _MAX_DEPTH:
        return []
    seen = _seen if _seen is not None else set()
    if artifact in seen:
        return []
    seen.add(artifact)
    snapshot = _load(artifact, loader)
    if not isinstance(snapshot, dict):
        return []
    out: list[QueryProblem] = []
    checks = snapshot.get("checks")
    errors = [c for c in checks if isinstance(c, dict) and c.get("level") == "error"] if isinstance(checks, list) else []
    if errors:
        out.append(QueryProblem(artifact, _node, errors))
    inputs = snapshot.get("inputs") if snapshot.get("source") == MERGE_SOURCE else None
    for entry in inputs if isinstance(inputs, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("artifact"), str):
            node = entry.get("node_id") if isinstance(entry.get("node_id"), str) else None
            out += query_problems(entry["artifact"], loader=loader, _seen=seen, _node=node, _depth=_depth + 1)
    return out


def error_checks(artifact: Any, *, loader: Callable[[str], Any] | None = None) -> list[dict[str, Any]]:
    """这份查询快照（含合并查询的输入）对照数据目录查出的全部 error 级检查结果，摊平成一个列表。"""
    return [c for p in query_problems(artifact, loader=loader) for c in p.checks]


def node_of(artifact: str, ledger: Iterable[Any] | None) -> str | None:
    """证据台账里交回这份查询快照的节点。台账里没有（老运行、Agent 没交台账）返回 None。"""
    for entry in ledger or []:
        if isinstance(entry, dict) and entry.get("kind") == "query" and entry.get("artifact") == artifact:
            node = entry.get("node_id")
            return node if isinstance(node, str) and node else None
    return None


def problem_clause(problems: list[str]) -> str:
    """「未通过 SQL 检查（…），结果不可靠」：前面接上是哪条查询（「所依据的查询」「查询「取数」（Q1）」）。"""
    problems = [p for p in problems if p]
    if not problems:
        return "未通过 SQL 检查，结果不可靠"
    if len(problems) == 1:
        return f"未通过 SQL 检查（{problems[0]}），结果不可靠"
    return f"有 {len(problems)} 处未通过 SQL 检查（{problems[0]}等），结果不可靠"


def reason_text(problems: list[str]) -> str:
    """「所依据的查询未通过 SQL 检查（…），结果不可靠」：口径卡、出具声明、运行日志、证据面板都用这句。"""
    return "所依据的查询" + problem_clause(problems)


def merge_problems(found: Iterable[QueryProblem]) -> list[QueryProblem]:
    """按快照 id 归并：同一条查询经几条路径（直接引用、合并查询的输入）找到，只算一次。保持先后顺序。"""
    out: dict[str, QueryProblem] = {}
    for p in found:
        if p.artifact not in out:
            out[p.artifact] = QueryProblem(p.artifact, p.node_id, list(p.checks))
        elif out[p.artifact].node_id is None and p.node_id:
            out[p.artifact].node_id = p.node_id
    return list(out.values())


__all__ = ["MERGE_SOURCE", "QueryProblem", "error_checks", "merge_problems", "node_of", "problem_clause", "query_problems",
           "reason_text"]
