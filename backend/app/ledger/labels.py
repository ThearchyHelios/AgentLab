"""逐个数标口径（docs/design/next-round/02-口径账本.md 做法 3）：报告里每个引用了查询格、口径卡指标的数，来自哪一份口径。

标签四种（KINDS）：
- caliber：来自口径「X」vN。how 是怎么认的：independent「等价已证明」（这次 Agent 自己写的 SQL 被证明和口径的取数等价）、
  composed「助手选用」（助手 use_caliber 按口径组合出来的取数，证明了和那一版一致：ledger/match 的 composed）。
- related：相关口径——同一个数据源、查了同样的表的已发布口径，但不等价；附差在哪（explain_sql_diff 的条目）。
- reasoning：这次的推理——没有相关的已发布口径。
- unknown：没比对、比对没做完（超时、解析失败）、快照读不出来。

怎么认：
- 单元格（[[v:]]、[[table:]] 里的格）：按证据目录找到查询快照，取那条查询的比对状态。等价时按列映射写出口径那边的列名。
- 指标（[[m:]]）：指标集工件里这个指标的每个输入都要来自查询快照，而且这些快照都被证明等价于同一份口径的同一版、绑定一致；
  指标式还要和那一版里某张口径卡上的某个指标相同——两边的式子把每个输入换成「口径的哪个取数节点、第几行、哪一列」之后
  一字不差（caliber_from 转抄来的式子本来就是那一张卡的，同样按这个比）。输入有不来自查询的（运行输入、整形、代码）不算来自口径。

整题（whole_of）：主报告里所有带标签的数都来自同一口径的同一版、绑定一致，就记 equivalent，并写出还缺的入口字段（口径入口
节点声明了、这次的绑定里没有的；required 且没有缺省值的另列）。

这里只是纯函数：标签在比对时算好、存进比对工件（ledger/match.py）；接口读的时候按这次运行查到过的遮罩值遮一遍（view_label）：
绑定的值是遮罩值的不给值，差异条目里的 SQL 片段走证据面板同一套遮罩（api/evidence._Shown）。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

#: 标签的四种、认法的两种
KINDS = ("caliber", "related", "reasoning", "unknown")
HOWS = ("independent", "composed")
#: 界面上标签要写明的两句：比对在封存之后做，不在封存范围内；事后补比的是按现在的口径回看
POST_SEAL_NOTE = "封存后比对，不在封存范围内"
RETRO_NOTE = "按现在的口径回看"
#: 发布级别的叫法（同 blueprints/publish_plan.LEVEL_TEXT、前端 lib/terms.ts）
LEVEL_TEXT = {"published": "已发布", "governed": "受管"}
MASKED = "已遮罩"

#: unknown 的几种原因
NO_RECORD = "这一格出自的查询没有比对记录"
CARD_UNREADABLE = "指标集读不出来，没有比对"
METRIC_GONE = "指标集里没有这个指标，没有比对"
UNFINISHED = "比对没有做完"
#: related 的几种说明
FORMULA_DIFFERS = "取数和口径等价，但指标的算法和口径卡上的不同"
INPUTS_ELSEWHERE = "指标有的输入不来自查询（运行输入、整形或代码），不能算来自口径"
MIXED_CALIBERS = "指标的几个输入分别对上了不同的口径"
BINDING_CONFLICT = "指标的几个输入对上了同一份口径，但参数不一致"

#: 能追到查询快照里那一格的输入（同 engine/lineage.SNAPSHOT_VIAS）
_SNAPSHOT_VIAS = ("tool_cell", "agent_field")


def caliber_key(workflow_id: str, version: int) -> str:
    """口径版本的写法「工作流 id@版本」：工件 meta、召回反查、统计都按它认。"""
    return f"{workflow_id}@{version}"


def _unknown(reason: str) -> dict[str, Any]:
    return {"kind": "unknown", "reason": reason}


def _related(query: Mapping[str, Any], *, reason: str | None = None) -> dict[str, Any]:
    """相关口径：这条查询比过、没等价的那几份（附差在哪）。一份相关的都没有时是这次的推理。"""
    related = [{k: cand[k] for k in ("caliber", "workflow_id", "version", "level", "node_id", "comparable",
                                     "reason", "diff", "binding", "via") if k in cand}
               for cand in query.get("candidates") or [] if cand.get("status") == "related"]
    if not related:
        return {"kind": "reasoning"}
    out: dict[str, Any] = {"kind": "related", "related": related}
    if reason:
        out["reason"] = reason
    return out


def _caliber(cand: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    return {"kind": "caliber", "how": "composed" if cand.get("how") == "composed" else "independent",
            "caliber": cand.get("caliber"),
            "workflow_id": cand.get("workflow_id"), "version": cand.get("version"), "level": cand.get("level"),
            "binding": dict(cand.get("binding") or {}), **extra}


def _proven(query: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [c for c in query.get("candidates") or [] if c.get("status") == "proven"]


def _caliber_column(cand: Mapping[str, Any], column: Any) -> str | None:
    """这次结果里的列 → 口径那边同一位置的列名（列映射，按位置）；没有映射、对不上时为 None。"""
    for item in cand.get("columns") or []:
        if item.get("ours") == column:
            return item.get("caliber")
    return None


def cell_label(artifact: str, column: Any, queries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """单元格的标签：它所在的那条查询的比对状态。"""
    query = queries.get(artifact)
    if query is None:
        return _unknown(NO_RECORD)
    status = query.get("status")
    if status in ("proven", "composed"):
        cand = _proven(query)[0] if _proven(query) else {}
        label = _caliber(cand, query=artifact, node_id=cand.get("node_id"),
                         column=_caliber_column(cand, column) if column is not None else None)
        if status == "composed":
            label["how"] = "composed"
        return label
    if status == "related":
        return {**_related(query), "query": artifact}
    if status == "none":
        return {"kind": "reasoning", "query": artifact}
    return {**_unknown(str(query.get("reason") or UNFINISHED)), "query": artifact}


def _leaf_token(node_id: Any, row: Any, column: Any) -> str:
    """把指标式里的一个输入换成「口径的哪个取数节点、第几行、哪一列」：两边的式子按这个换完再逐字比。"""
    return f"⟦{node_id}|{row}|{column}⟧"


def _mapped_expression(expression: str, tokens: Mapping[str, str]) -> str | None:
    """指标式里的输入按 tokens（输入路径 → 记号）换掉之后的写法；解析不了、有输入没换到的为 None。"""
    from app.engine.expressions import ExpressionError, leaf_refs, parse_expression, substitute

    try:
        tree = parse_expression(expression)[0]
    except ExpressionError:
        return None
    leaves = leaf_refs(tree)
    if not leaves or any(leaf not in tokens for leaf in leaves):
        return None
    return substitute(tree, dict(tokens))


def card_expressions(cards: list[Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    """口径版本里每张源头口径卡的每个指标：(卡的节点 id, 指标 id, 换成记号后的式子)。卡上的输入要是 cell(nodes.X, 行, '列')
    （look/calibers.card_inputs 认的那种），别的写法的指标不收。"""
    import ast

    from app.engine.expressions import ExpressionError, cell_parts, leaf_refs, parse_expression

    out: list[tuple[str, str, str]] = []
    for card in cards:
        for metric in card.get("metrics") or []:
            expression = str((metric or {}).get("expression") or "")
            try:
                tree = parse_expression(expression)[0]
            except ExpressionError:
                continue
            tokens: dict[str, str] = {}
            for leaf in leaf_refs(tree):
                parts = cell_parts(leaf)
                if parts is None or not parts[0].startswith("nodes."):
                    break
                try:
                    row, column = ast.literal_eval(parts[1]), ast.literal_eval(parts[2])
                except (ValueError, SyntaxError):
                    break
                tokens[leaf] = _leaf_token(parts[0][len("nodes."):], row, column)
            else:
                if (mapped := _mapped_expression(expression, tokens)) is not None:
                    out.append((str(card.get("node_id")), str(metric.get("id")), mapped))
    return out


def metric_label(card: Mapping[str, Any] | None, metric_id: Any, queries: Mapping[str, Mapping[str, Any]],
                 calibers: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """指标的标签（见模块说明）。card 是封存的指标集工件（读不出为 None）；calibers 是 {「工作流@版本」: {level, cards}}，
    cards 是那一版里源头口径卡的指标式（match 从版本快照取）。"""
    if card is None:
        return _unknown(CARD_UNREADABLE)
    metric = next((m for m in card.get("metrics") or [] if isinstance(m, dict) and m.get("id") == metric_id), None)
    if metric is None:
        return _unknown(METRIC_GONE)
    inputs = [i for i in metric.get("inputs") or [] if isinstance(i, dict)]
    from_queries = [i for i in inputs if i.get("artifact") and i.get("via") in _SNAPSHOT_VIAS]
    if not from_queries:
        return {"kind": "reasoning"}
    found = [queries.get(str(i["artifact"])) for i in from_queries]
    if any(q is None for q in found):
        return _unknown(NO_RECORD)
    if any(q.get("status") == "skipped" for q in found):      # type: ignore[union-attr]
        return _unknown(next(str(q.get("reason") or UNFINISHED) for q in found
                             if q.get("status") == "skipped"))  # type: ignore[union-attr]
    queries_found: list[Mapping[str, Any]] = [q for q in found if q is not None]
    proven_keys = [{str(c.get("caliber")) for c in _proven(q)} for q in queries_found]
    common = set.intersection(*proven_keys) if proven_keys else set()
    nearby = _brief([c for q in queries_found for c in q.get("candidates") or []
                     if c.get("status") in ("proven", "related")])
    queried = {"queries": [str(i["artifact"]) for i in from_queries]}
    if len(from_queries) < len(inputs):
        return {"kind": "related", "related": nearby, "reason": INPUTS_ELSEWHERE, **queried} if nearby \
            else {"kind": "reasoning", **queried}
    if not common:
        if all(proven_keys):
            return {"kind": "related", "related": nearby, "reason": MIXED_CALIBERS, **queried}
        related = next((q for q in queries_found if q.get("status") == "related"), None)
        return {**_related(related), **queried} if related is not None else {"kind": "reasoning", **queried}
    reason = FORMULA_DIFFERS
    for key in sorted(common):
        chosen = [next(c for c in _proven(q) if c.get("caliber") == key) for q in queries_found]
        binding: dict[str, Any] = {}
        conflict = False
        for cand in chosen:
            for name, value in (cand.get("binding") or {}).items():
                if name in binding and binding[name] != value:
                    conflict = True
                binding.setdefault(name, value)
        if conflict:
            reason = BINDING_CONFLICT
            continue
        tokens: dict[str, str] = {}
        for item, cand in zip(from_queries, chosen):
            locator = item.get("locator") if isinstance(item.get("locator"), dict) else {}
            mapped = _caliber_column(cand, locator.get("column"))
            if mapped is None or item.get("path") is None:
                tokens = {}
                break
            tokens[str(item["path"])] = _leaf_token(cand.get("node_id"), locator.get("row"), mapped)
        mine = _mapped_expression(str(metric.get("expression") or ""), tokens) if tokens else None
        if mine is None:
            continue
        hit = next(((node, mid) for node, mid, theirs in (calibers.get(key) or {}).get("expressions") or []
                    if theirs == mine), None)
        if hit is None:
            continue
        label = {**_caliber(chosen[0], card=hit[0], metric=hit[1]), "binding": binding,
                 "queries": [str(i["artifact"]) for i in from_queries]}
        if any(c.get("how") == "composed" for c in chosen):
            # 几个输入里有一个是助手选用的，整个指标就按助手选用说（不能说成都是独立写出来的）
            label["how"] = "composed"
        return label
    return {"kind": "related", "related": _brief([c for q in queries_found for c in _proven(q)
                                                  if c.get("caliber") in common]), "reason": reason, **queried}


def _brief(cands: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """指标上的相关口径：只写是哪一版、哪个取数节点（差在哪在各个输入的查询上），同一版只写一次，最多 5 个。"""
    out: list[dict[str, Any]] = []
    for cand in cands:
        if any(o.get("caliber") == cand.get("caliber") for o in out):
            continue
        out.append({k: cand[k] for k in ("caliber", "workflow_id", "version", "level", "node_id") if k in cand})
    return out[:5]


def label_doc(doc: Mapping[str, Any], queries: Mapping[str, Mapping[str, Any]],
              calibers: Mapping[str, Mapping[str, Any]],
              load_card: Callable[[str], Mapping[str, Any] | None]) -> dict[str, dict[str, Any]]:
    """一份报告文档里每个数的标签：{片段 id: 标签}。只标引用解析了的数字、取值片段（单元格、指标）；运行输入、表名、引文、
    没有引用的数不标。"""
    from app.engine.evidence import iter_units

    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    cards: dict[str, Mapping[str, Any] | None] = {}
    out: dict[str, dict[str, Any]] = {}
    for _, unit in iter_units(doc):
        for seg in unit.get("segments") or []:
            if not isinstance(seg, dict) or seg.get("kind") not in ("number", "value") or not seg.get("id"):
                continue
            cite = seg.get("cite") if isinstance(seg.get("cite"), dict) else {}
            if cite.get("status") != "resolved":
                continue
            entry = catalog.get(cite.get("alias")) if isinstance(catalog.get(cite.get("alias")), dict) else {}
            locator = cite.get("locator") if isinstance(cite.get("locator"), dict) else {}
            artifact = str(entry.get("artifact") or "")
            if cite.get("kind") == "cell" and artifact:
                out[str(seg["id"])] = cell_label(artifact, locator.get("column"), queries)
            elif cite.get("kind") == "metric" and artifact:
                if artifact not in cards:
                    cards[artifact] = load_card(artifact)
                out[str(seg["id"])] = metric_label(cards[artifact], locator.get("metric"), queries, calibers)
    return out


def whole_of(labels: Mapping[str, Mapping[str, Any]], calibers: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """整题判断（主报告）：所有带标签的数都来自同一口径的同一版、绑定一致时 equivalent，附还缺的入口字段。"""
    counts = {kind: 0 for kind in KINDS}
    for label in labels.values():
        counts[str(label.get("kind"))] = counts.get(str(label.get("kind")), 0) + 1
    if not labels:
        return {"equivalent": False, "reason": "报告里没有可标口径的数", "counts": counts}
    keys = {label.get("caliber") for label in labels.values() if label.get("kind") == "caliber"}
    if counts["caliber"] != len(labels):
        reason = ("有的数没有比对" if counts["unknown"] else "有的数是这次的推理" if counts["caliber"]
                  else "没有数来自口径")
        return {"equivalent": False, "reason": reason, "counts": counts}
    if len(keys) != 1:
        return {"equivalent": False, "reason": "数来自不同的口径", "counts": counts}
    binding: dict[str, Any] = {}
    for label in labels.values():
        for name, value in (label.get("binding") or {}).items():
            if name in binding and binding[name] != value:
                return {"equivalent": False, "reason": "各个数的参数不一致", "counts": counts}
            binding.setdefault(name, value)
    key = str(next(iter(keys)))
    info = calibers.get(key) or {}
    fields = [f for f in info.get("fields") or [] if isinstance(f, dict) and f.get("name")]
    first = next(iter(labels.values()))
    return {
        "equivalent": True, "caliber": key, "workflow_id": first.get("workflow_id"), "version": first.get("version"),
        "level": first.get("level"),
        "how": "composed" if any(label.get("how") == "composed" for label in labels.values()) else "independent",
        "binding": binding,
        "missing": [f["name"] for f in fields if f["name"] not in binding],
        "missing_required": [f["name"] for f in fields
                             if f["name"] not in binding and f.get("required") and not f.get("has_default")],
        "counts": counts,
    }


# --------------------------------------------------------------------------
# 读的时候遮：绑定的值、差异条目里的 SQL 片段
# --------------------------------------------------------------------------


def binding_view(binding: Mapping[str, Any] | None, hide: Callable[[Any], bool]) -> list[dict[str, Any]]:
    """绑定 → [{field, value} 或 {field, masked: true}]：值是这次运行查到过的遮罩值的不给值（也就不能拿它代填出具）。"""
    out = []
    for name, value in (binding or {}).items():
        out.append({"field": name, "masked": True, "value": MASKED} if hide(value) else {"field": name, "value": value})
    return out


def diff_view(diff: Mapping[str, Any] | None, shown: Callable[[Any], Any]) -> dict[str, Any] | None:
    """差在哪：条目的两边写法、说明和逐行对照里的 SQL 片段按遮罩值换掉。"""
    if not isinstance(diff, Mapping):
        return None
    items = [{**item, "a": shown(item.get("a")), "b": shown(item.get("b")), "text": shown(item.get("text"))}
             for item in diff.get("items") or [] if isinstance(item, Mapping)]
    return {"items": items, "fallback": bool(diff.get("fallback")),
            "text_diff": [shown(line) for line in diff.get("text_diff") or []]}


def view_label(label: Mapping[str, Any], *, hide: Callable[[Any], bool], shown: Callable[[Any], Any],
               names: Mapping[str, str]) -> dict[str, Any]:
    """一个标签交给界面的样子：绑定按遮罩给、差在哪按遮罩换，口径写上工作流名和级别的叫法（名字按现在的）。"""
    out = {k: v for k, v in label.items() if k not in ("binding", "related")}
    if label.get("kind") == "caliber":
        out["binding"] = binding_view(label.get("binding"), hide)
        out["workflow_name"] = names.get(str(label.get("workflow_id") or ""))
        out["level_text"] = LEVEL_TEXT.get(str(label.get("level") or ""), "")
    if label.get("kind") == "related":
        out["related"] = [related_view(item, hide=hide, shown=shown, names=names)
                          for item in label.get("related") or []]
    return out


def related_view(item: Mapping[str, Any], *, hide: Callable[[Any], bool], shown: Callable[[Any], Any],
                 names: Mapping[str, str]) -> dict[str, Any]:
    out = {k: item[k] for k in ("caliber", "workflow_id", "version", "level", "node_id", "comparable", "reason", "via")
           if k in item}
    out["workflow_name"] = names.get(str(item.get("workflow_id") or ""))
    out["level_text"] = LEVEL_TEXT.get(str(item.get("level") or ""), "")
    if "binding" in item:
        out["binding"] = binding_view(item.get("binding"), hide)
    if "diff" in item:
        out["diff"] = diff_view(item.get("diff"), shown)
    return out


__all__ = ["HOWS", "KINDS", "LEVEL_TEXT", "POST_SEAL_NOTE", "RETRO_NOTE", "binding_view", "caliber_key",
           "card_expressions", "cell_label", "diff_view", "label_doc", "metric_label", "related_view", "view_label",
           "whole_of"]
