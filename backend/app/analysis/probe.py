"""可比性检查的现查（细案 01 §1「只读快照、不发查询」的唯一例外，§6）：表里这一列的实际日期范围、按运行当天现算的界。

算子本身不查库。要现查的只有两样，都走 data/coverage 现成的只读探查（只读守卫、遮罩列不探、每条 PROBE_TIMEOUT_S 秒、总
时限、同一张表 5 分钟内不重查、现查不成退回剖析事实）：

- 范围：(数据源名, 表, 列) → contract.Probe {source, table, column, min, max, basis, at}，min / max 是 ISO 日期；表里这一列
  一个值都没有时 min、max 都为 None（basis live）。basis 是 live（现查）/ profile（按剖析）/ observed（按取值观察），at 是现查的
  时刻（服务端时区，ISO，秒）或剖析、观察的日期。
- 界：SQL 里按运行当天现算的界（CURRENT_DATE、DATE_SUB(…)、date('now', …)）在同一个库里算出来：(数据源名, 界的 SQL 写法)
  → Probe {source, expr, value, basis: live, at}，value 是库里算出的日期（带时刻的写成「YYYY-MM-DD HH:MM:SS」）。

**结论里记下用到的现查结果，复算用记下的值**：Book 是一次分析的现查簿。run 时算子第一遍只登记要查什么（拿到的都是「不
知道」），入口查完再算一遍，直到不再要新的（算子在拿到范围之后才可能要界，最多三轮）；复算时 Book 里装的是结论记下的
probes，不现查。结论的 probes 只记最后一遍真用到、查得到的，按用到的先后——同样的输入、同样的现查结果，结论逐字节相同。

入口（agent 分析工具、分析卡、口径卡的可比性挂钩）用 make_prober 生成 contract.Prober 交进 context["probe"]，见它的说明。
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from app.analysis.contract import Probe
from app.data.names import name_key

#: 算子在拿到现查结果之后可能还要新的（先要范围、再要界）：run 最多这么多轮
MAX_ROUNDS = 3


def _range_key(source: str, table: str, column: str) -> tuple[str, str, str, str]:
    return ("range", source or "", name_key(str(table)), name_key(str(column)))


def _bound_key(source: str, expr: str) -> tuple[str, str, str]:
    return ("bound", source or "", str(expr))


def probe_key(probe: Mapping[str, Any]) -> tuple[str, ...]:
    """一条记下的现查结果的键（Book 按它查）。"""
    if probe.get("expr") is not None:
        return _bound_key(str(probe.get("source") or ""), str(probe["expr"]))
    return _range_key(str(probe.get("source") or ""), str(probe.get("table") or ""), str(probe.get("column") or ""))


class Book:
    """一次分析的现查簿。known：{键: Probe 或 None（查过、查不到）}。算子经 range / bound 取；asked 是按先后登记的键，missing
    是登记了、簿里还没有的（run 据此去查），used 是最后真用到、查得到的（写进结论的 probes）。"""

    def __init__(self, known: Mapping[tuple[str, ...], Probe | None] | Iterable[Probe] = ()) -> None:
        if isinstance(known, Mapping):
            self._known: dict[tuple[str, ...], Probe | None] = dict(known)
            self._closed = False
        else:
            self._known = {probe_key(p): p for p in known}
            self._closed = True          # 复算：记下的就是全部，没记的当查不到
        self.asked: list[tuple[str, ...]] = []

    def _ask(self, key: tuple[str, ...]) -> Probe | None:
        if key not in self.asked:
            self.asked.append(key)
        return self._known.get(key)

    def range(self, source: str, table: str, column: str) -> Probe | None:
        return self._ask(_range_key(source, table, column))

    def bound(self, source: str, expr: str) -> Probe | None:
        return self._ask(_bound_key(source, expr))

    @property
    def missing(self) -> list[tuple[str, ...]]:
        return [] if self._closed else [k for k in self.asked if k not in self._known]

    @property
    def used(self) -> list[Probe]:
        return [p for p in (self._known.get(k) for k in self.asked) if p is not None]


def _now() -> str:
    return datetime.now().astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


async def fetch(book: Book, context: Mapping[str, Any]) -> dict[tuple[str, ...], Probe | None]:
    """把簿里登记了、还没有的现查一遍：先 context["probe"]，查不到的范围退回 context["facts"]（数据源名 → {表名: 剖析事实}，
    或数据源名 → 取剖析事实的函数），都没有记 None。交回新查到的那几条。"""
    from app.data import coverage

    prober = context.get("probe")
    facts = context.get("facts") or {}
    out: dict[tuple[str, ...], Probe | None] = {}
    for key in book.missing:
        found: Probe | None = None
        if key[0] == "range":
            _, source, table, column = key
            if prober is not None:
                try:
                    found = await prober(source, table, column)
                except Exception:  # noqa: BLE001 - 探查失败就当不知道
                    found = None
            if found is None:
                found = await _from_facts(facts.get(source), source, table, column, coverage)
        else:
            _, source, expr = key
            bounds = getattr(prober, "bounds", None)
            if bounds is not None:
                try:
                    found = await bounds(source, expr)
                except Exception:  # noqa: BLE001
                    found = None
        out[key] = found
    return out


async def _from_facts(facts: Any, source: str, table: str, column: str, coverage: Any) -> Probe | None:
    """探查不成时按剖析事实兜底：facts 是 {表名: last_facts 的那一项} 或取它的函数（coverage.Facts）。"""
    found: Any = None
    try:
        if callable(facts):
            found = await facts(table)
        elif isinstance(facts, Mapping):
            found = next((v for k, v in facts.items() if name_key(str(k)) == name_key(table)
                          or name_key(str(k).rsplit(".", 1)[-1]) == name_key(table.rsplit(".", 1)[-1])), None)
    except Exception:  # noqa: BLE001
        return None
    data = coverage.facts_range(found, column)
    return _range_probe(source, table, column, data) if data else None


def _range_probe(source: str, table: str, column: str, data: Mapping[str, Any]) -> Probe:
    basis = data.get("basis") if data.get("basis") in ("live", "profile", "observed") else "live"
    at = _now() if basis == "live" else str(data.get("at") or "")
    return {"source": source, "table": table, "column": column, "min": data.get("min"), "max": data.get("max"),
            "basis": basis, "at": at}  # type: ignore[typeddict-item]


class LiveProber:
    """contract.Prober：await prober(数据源名, 表, 列) → Probe | None；另有 await prober.bounds(数据源名, 界的 SQL) 在库里算界，
    prober.dialect(数据源名) 是这个源的 sqlglot 方言（算子按它读 SQL、写界）。见 make_prober。"""

    def __init__(self, sources: Mapping[str, Any], *, masked: Mapping[str, Iterable[str]] | None,
                 facts: Mapping[str, Callable[[str], Awaitable[Mapping[str, Any] | None]]] | None,
                 cache: Mapping[str, Any] | None, budget: float,
                 clock: Callable[[], str] | None) -> None:
        from app.data import coverage

        self._sources = dict(sources)
        self._masked = {k: {str(c).strip().lower() for c in v} for k, v in (masked or {}).items()}
        self._facts = dict(facts or {})
        self._cache: dict[str, Any] = dict(cache or {})
        self._budget = budget
        self._deadline: float | None = None
        self._clock = clock or _now
        self._bounds: dict[tuple[str, str], Any] = {}
        self._coverage = coverage

    def dialect(self, source: str) -> str | None:
        from app.data.sqlcheck import dialect_of

        found = self._sources.get(source)
        return dialect_of(getattr(found, "kind", None)) if found is not None else None

    def _expired(self) -> bool:
        if self._deadline is None:
            self._deadline = time.monotonic() + self._budget
        return time.monotonic() > self._deadline

    def _hidden(self, source: Any, name: str) -> set[str]:
        if name in self._masked:
            return self._masked[name]
        from app.data.engine import masked_columns

        return {c.lower() for c in masked_columns(getattr(source, "options", None) or {})}

    def _facts_of(self, name: str, source: Any) -> Callable[[str], Awaitable[Mapping[str, Any] | None]]:
        return self._facts.get(name) or profile_facts(source)

    async def __call__(self, source: str, table: str, column: str) -> Probe | None:
        found = self._sources.get(source)
        if found is None or str(column).strip().lower() in self._hidden(found, source):
            return None
        if self._expired():
            return None
        cache = self._cache.setdefault(source, self._coverage.Cache())
        data = await self._coverage.column_range(found, table, column, facts=self._facts_of(source, found), cache=cache)
        if data is None:
            return None
        probe = _range_probe(source, table, column, data)
        if probe["basis"] == "live":
            probe["at"] = self._clock()
        return probe

    async def bounds(self, source: str, expr: str) -> Probe | None:
        found = self._sources.get(source)
        if found is None or self._expired():
            return None
        key = (source, expr)
        if key not in self._bounds:
            values = await self._coverage.bound_values(found, [expr])
            self._bounds[key] = values[0] if values else None
        value = self._bounds[key]
        if value is None:
            return None
        return {"source": source, "expr": expr, "value": str(value), "basis": "live",  # type: ignore[typeddict-unknown-key]
                "at": self._clock()}


def profile_facts(source: Any) -> Callable[[str], Awaitable[Mapping[str, Any] | None]]:
    """这个数据源上一张表的剖析事实（catalog_profile_jobs.last_facts 的那一项，同 tools/datasource._coverage 的退回）：上传
    表格的快照对不上这次读的那一版时不用。"""
    async def facts(table: str) -> Mapping[str, Any] | None:
        from app.data import catalog as data_catalog
        from app.data.catalog_profile_jobs import last_facts
        from app.db.base import SessionLocal

        source_id = getattr(source, "id", None)
        if not source_id:
            return None
        name = data_catalog.resolve_table_name(getattr(source, "schema_cache", None), table) or table
        async with SessionLocal() as session:
            found = (await last_facts(session, str(source_id), [name])).get(name)
        snapshot = getattr(source, "snapshot_id", None)
        if found and snapshot and found.get("snapshot_id") != snapshot:
            return None
        return found

    return facts


def make_prober(sources: Mapping[str, Any], *, masked: Mapping[str, Iterable[str]] | None = None,
                facts: Mapping[str, Callable[[str], Awaitable[Mapping[str, Any] | None]]] | None = None,
                cache: Mapping[str, Any] | None = None, budget: float | None = None,
                clock: Callable[[], str] | None = None) -> LiveProber:
    """生成一次分析调用用的探查器（contract.Prober，交进 context["probe"]）。

    - sources：数据源名 → 数据源对象（DataSource 行，或上传源绑定快照的视图：同 tools/datasource 建工具时的 source，要有
      name、kind、options、schema_cache；上传源另有 snapshot_id）。名字和快照里的 source 一致。没给的源不探。
    - masked：数据源名 → 遮罩列（不分大小写）。缺省按数据源 options 里设的遮罩列（data/engine.masked_columns）。遮罩列上
      不探，探查器直接交回 None。
    - facts：数据源名 → 取剖析事实的函数（coverage.Facts：表名 → last_facts 的那一项）。缺省按 profile_facts 现读库（同查询
      工具的覆盖检查）。
    - cache：数据源名 → coverage.Cache（同一张表、同一个界一次运行里只查一次）；不给就每个探查器各自一份。另外 coverage
      在进程里把现查到的范围记 5 分钟。
    - budget：这个探查器所有探查加起来最多多少秒（缺省 coverage.BUDGET_S，从第一次探查算起）；过了的一律交回 None。一次
      分析调用建一个。
    - clock：现查时刻的写法（测试用）；缺省是服务端时区的 ISO 时间（到秒）。
    """
    from app.data import coverage

    return LiveProber(sources, masked=masked, facts=facts, cache=cache,
                      budget=coverage.BUDGET_S if budget is None else budget, clock=clock)


__all__ = ["Book", "LiveProber", "MAX_ROUNDS", "fetch", "make_prober", "probe_key", "profile_facts"]
