"""分析算子测试用的样例：虚构零售库 shop（地区华东、华南、华北、西南，渠道线上、线下，销售额、订单数、退款），表名、列名、
数字都是编的。analyze 跑一次 analysis.run，顺手用 recompute 复算一遍、核对逐字段相同（contract.same），所以每个用例都同时
钉住「复算和运行逐字段相同」。"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app import analysis
from app.analysis.contract import CallError, effective_settings, same
from app.core.artifact_store import canonical_json, content_hash

#: 测试里的「运行当天」
TODAY = date(2026, 10, 21)


def snap(ref: str, sql: str, columns: Sequence[str], rows: Sequence[Sequence[Any]], *,
         types: Mapping[str, str] | None = None, source: str = "shop", truncated: bool = False,
         masked: Sequence[str] = (), **extra: Any) -> dict[str, Any]:
    """一份输入快照（InputSnapshot 的形状）。artifact 按内容算，复算按它取回。"""
    body = {"source": source, "sql": sql, "columns": list(columns), "rows": [list(r) for r in rows],
            "truncated": truncated, **({"column_types": dict(types)} if types else {}), **extra}
    return {"ref": ref, "artifact": content_hash(canonical_json(body)), "node_id": "q1", "masked": list(masked), **body}


def item(value: Any, status: str = "confirmed", source: str = "human") -> dict[str, Any]:
    return {"value": value, "source": source, "status": status}


def notes(*, columns: Mapping[str, Mapping[str, Any]] | None = None, business_date: str | None = None) -> dict[str, Any]:
    """一张表的目录 notes：columns 是 {列: {label / measure: 值或 item(...)}}。"""
    out: dict[str, Any] = {}
    if columns:
        out["columns"] = {c: {k: v if isinstance(v, Mapping) else item(v) for k, v in spec.items()}
                          for c, spec in columns.items()}
    if business_date:
        out["business_date"] = item({"column": business_date, "rule": "下单日", "timezone": "Asia/Shanghai"})
    return out


#: orders 表的目录：地区、渠道、实收金额（流量）、退款单数
ORDERS = notes(columns={"region": {"label": "地区"}, "channel": {"label": "渠道"},
                        "amount": {"label": "销售额", "measure": "flow"},
                        "refund_flag": {"label": "退款单数", "measure": "flow"},
                        "order_date": {"label": "下单日"}},
               business_date="order_date")
CATALOGS = {"shop": {"orders": ORDERS}}


async def analyze(op: str, mode: str | None = None, *, snaps: Sequence[Mapping[str, Any]], today: date = TODAY,
                  catalogs: Mapping[str, Any] | None = None, probe: Any = None, facts: Any = None,
                  parents: Mapping[str, Mapping[str, Any]] | None = None, settings: Mapping[str, Any] | None = None,
                  hypothesis: str | None = None, check: bool = True, **params: Any) -> dict[str, Any]:
    """跑一次 analysis.run；check 时再复算一遍，核对和封存的逐字段相同（hypothesis 除外）。"""
    by_ref = {s["ref"]: s for s in snaps}
    request: dict[str, Any] = {"op": op, "mode": mode, "params": params}
    if hypothesis is not None:
        request["hypothesis"] = hypothesis
    context: dict[str, Any] = {"today": today, "catalogs": CATALOGS if catalogs is None else catalogs}
    if probe is not None:
        context["probe"] = probe
    if facts is not None:
        context["facts"] = facts
    result = await analysis.run(request, by_ref.get, settings=effective_settings(settings), context=context,
                                parents=(parents or {}).get)
    if check:
        again = recomputed(result, snaps, parents)
        assert same(result, again), _diff(result, again)
        assert again["hypothesis"] == result["hypothesis"]
    return dict(result)


def recomputed(result: Mapping[str, Any], snaps: Sequence[Mapping[str, Any]],
               parents: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """按结论里记下的复算：快照按 artifact 取回（只交快照内容本身，不交入口加的出处），上一层结论按内容哈希取回。"""
    by_artifact = {s["artifact"]: {k: v for k, v in s.items() if k not in ("ref", "artifact", "node_id", "masked")}
                   for s in snaps}
    by_hash = {content_hash(canonical_json(r)): r for r in (parents or {}).values()}
    return dict(analysis.recompute(result, by_artifact.get, load_result=by_hash.get))  # type: ignore[arg-type]


def _diff(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return "；".join(f"{k}: {json.dumps(a.get(k), ensure_ascii=False)} ≠ {json.dumps(b.get(k), ensure_ascii=False)}"
                     for k in keys)


async def call_error(op: str, mode: str | None = None, **kwargs: Any) -> CallError:
    """跑一次，期望是调用错误。"""
    try:
        await analyze(op, mode, check=False, **kwargs)
    except CallError as e:
        return e
    raise AssertionError("应当是调用错误")


def fixed_prober(ranges: Mapping[tuple[str, str], tuple[str | None, str | None]] | None = None, *,
                 bounds: Mapping[str, str] | None = None, at: str = "2026-10-21T09:00:00") -> Any:
    """测试用的探查器：ranges 是 {(表, 列): (最早, 最晚)}，bounds 是 {界的 SQL: 日期}；没写的查不到。"""

    class Prober:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        async def __call__(self, source: str, table: str, column: str) -> dict[str, Any] | None:
            self.calls.append((source, table, column))
            found = (ranges or {}).get((table, column))
            if found is None:
                return None
            return {"source": source, "table": table, "column": column, "min": found[0], "max": found[1],
                    "basis": "live", "at": at}

        async def bounds(self, source: str, expr: str) -> dict[str, Any] | None:
            self.calls.append((source, expr))
            value = (bounds or {}).get(expr)
            return None if value is None else {"source": source, "expr": expr, "value": value, "basis": "live", "at": at}

        def dialect(self, source: str) -> str | None:
            return "sqlite"

    return Prober()


__all__ = ["CATALOGS", "ORDERS", "TODAY", "analyze", "call_error", "fixed_prober", "item", "notes", "recomputed",
           "snap"]
