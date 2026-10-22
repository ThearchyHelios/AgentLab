"""报告文档里系统写的「分析范围」（docs/design/analysis-round/00-总方案.md §3.6，契约 contract.ScopeBlock）。

报告里用到了分析结论（这次运行有分析结论）时，系统按写作目录里的 A 条目照实写一段：查了什么、排除了什么、哪些没查。
它是报告文档的结构化字段 analysis_scope（D17），不进正文、不过裸数字和说法核对（不是模型写的），写作者改不了，随文档
封存；网页、导出、可验证报告各自按它渲染，编号点得开。

- **现象**（phenomena）：做成了的可比性、基线、趋势结论
- **拆过**（decomposed）：做成了的变化拆解，按下钻链的先后（上一层在前，下钻的紧跟在它后面）
- **排除**（excluded）：判定为分散的拆解、差异可能只是样本波动的检验——拆了没发现集中也是结论
- **算不了**（refused）：各种算子算不了的结论，带原因
- **没拆**（not_decomposed）：候选维度里没拆过的。候选维度确定地从数据目录算：拆解所用查询的 SQL 里的事实表（目录标了
  fact / log；没标的、查询只读一张表时就是它）上标成属性、状态（attribute / status）、不遮罩、取值不多（码值表不超过
  CANDIDATE_MAX 个取值，或者已列全）的列，加上它关联的维度表（目录标了 dimension，或者关系是多对一）上同样的列。
  「取值不多」只认码值表：剖析目前不记每列的取值数（码值列另有取值分布的观察、键列只核唯一），方案 §3.6 说的「剖析的
  取值数 ≤ 50」等剖析记了每列取值数再补（第 1 波复核 R34，记为偏离）；没有码值表的属性列不进候选。
  拆解按的列对不上目录、读不到目录、认不出事实表的，not_decomposed 为 None，note 写一句为什么核对不了——不乱列
- **检验**（tests）：这次做了几次差异检验（做成了的）；corrected：一次检验比了多对的按次数校正过，几次检验之间没有校正，
  做了不止一次时为 False（契约之外加的一项，见交付说明）

agent 写的 hypothesis 不进这一段（模型写的字不进系统渲染的正文，细案 01 §7.3）。build 是纯函数（目录、遮罩由调用方读好
交进来）；scope_of 给报告节点用，读这次运行的数据目录视图（data/catalog_view），读不到当核对不了，不挡生成。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.analysis.contract import ScopeBlock, ScopeItem, catalog_result
from app.data.names import name_key

logger = logging.getLogger(__name__)

#: 候选维度「取值不多」的上限：码值表里的取值不超过这么多（取值观察最多也只取这么多，data/catalog_profile_observe）
CANDIDATE_MAX = 50
#: 现象：可比性、基线、趋势
_PHENOMENA = ("comparable", "baseline", "trend")
#: 事实表的表类型（数据目录的 kind）：流水、日志也是一行一件事
_FACT_KINDS = ("fact", "log")
#: 候选维度的度量类型
_DIMENSION_MEASURES = ("attribute", "status")
#: 拆解的输入快照（下钻的上一层不是快照）
_DATA_ROLES = ("data", "base", "target")

NOTE_NO_CATALOG = "这次运行读不到数据目录，候选维度核对不了"
NOTE_NO_NOTES = "拆解所用的数据源还没有数据目录，候选维度核对不了"
NOTE_NO_DECOMPOSE = "这次没有按维度拆解，候选维度没有核对"
NOTE_NO_QUERY = "拆解所用的查询取不回来，候选维度核对不了"
NOTE_NO_FACT = "拆解所用查询的表在数据目录里认不出哪张是事实表，候选维度核对不了"
NOTE_NOT_MAINTAINED = ("数据目录还没标表类型和字段的度量类型（目录还没维护），列不出候选维度；"
                       "在数据目录里把事实表、属性列这些标上，这一处就能列出没拆过的维度")


def _alias_order(alias: str) -> tuple[int, str]:
    tail = alias[1:]
    return (int(tail), alias) if tail.isdigit() else (1 << 30, alias)


def _value(item: Any) -> Any:
    """目录里一个项的值：驳回的（rejected）当没有。"""
    if not isinstance(item, Mapping) or item.get("status") == "rejected":
        return None
    return item.get("value")


def _dims(result: Mapping[str, Any]) -> list[str]:
    """拆解按的维度列（params.by），乘积拆解没有维度。"""
    if result.get("op") != "decompose" or result.get("mode") == "factor":
        return []
    by = (result.get("params") or {}).get("by")
    return [str(b) for b in (by if isinstance(by, list) else [by] if isinstance(by, str) and by else [])]


def _dim_labels(result: Mapping[str, Any]) -> list[str]:
    labels = ((result.get("table") or {}).get("labels") or {})
    return [str(labels.get(d) or d) for d in _dims(result)]


def _code(result: Mapping[str, Any]) -> str | None:
    """结论的判定码；没有判定（算不了）为 None。"""
    verdict = result.get("verdict")
    return verdict.get("code") if isinstance(verdict, Mapping) else None


def _item(alias: str, result: Mapping[str, Any], parent: str | None) -> ScopeItem:
    return {"alias": alias, "op": str(result.get("op") or ""), "mode": result.get("mode"),
            "dims": _dim_labels(result), "verdict": _code(result),
            "phrase": str(result.get("phrase") or ""), "parent": parent,
            "reason": result.get("reason") if result.get("status") != "ok" else None}


def _parent(result: Mapping[str, Any], by_artifact: Mapping[str, str]) -> str | None:
    """下钻的上一层：输入里 role 为 parent 的那份结论（按工件对回写作目录里的编号）。"""
    for item in result.get("inputs") or []:
        if isinstance(item, Mapping) and item.get("role") == "parent" and item.get("artifact") in by_artifact:
            return by_artifact[str(item["artifact"])]
    return None


def _chain(items: list[ScopeItem]) -> list[ScopeItem]:
    """按下钻链排：上一层在前，下钻的紧跟在它后面（同一层按编号）。上一层不在这几项里的当作最上层。"""
    known = {i["alias"] for i in items}
    children: dict[str | None, list[ScopeItem]] = {}
    for item in items:
        children.setdefault(item["parent"] if item["parent"] in known else None, []).append(item)
    out: list[ScopeItem] = []
    seen: set[str] = set()

    def walk(parent: str | None) -> None:
        for item in children.get(parent, []):
            if item["alias"] in seen:
                continue
            seen.add(item["alias"])
            out.append(item)
            walk(item["alias"])

    walk(None)
    out.extend(i for i in items if i["alias"] not in seen)     # 链成了环（不该有）也照列
    return out


def results(catalog: Mapping[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """写作目录里的分析条目，按编号：[(A 编号, 条目, 结论)]。"""
    found = [(str(alias), entry, dict(result)) for alias, entry in catalog.items()
             if isinstance(entry, dict) and (result := catalog_result(entry)) is not None]
    return sorted(found, key=lambda t: _alias_order(t[0]))


# --------------------------------------------------------------------------
# 候选维度
# --------------------------------------------------------------------------


def _table_notes(tables: Mapping[str, Any], name: str) -> tuple[str, dict[str, Any]] | None:
    """SQL 里写的表名在数据目录里的那一张：按 name_key 比，schema 前缀去掉再比一次。"""
    wanted = {name_key(name), name_key(name.rsplit(".", 1)[-1])}
    for table, notes in tables.items():
        if isinstance(notes, Mapping) and (name_key(str(table)) in wanted
                                           or name_key(str(table).rsplit(".", 1)[-1]) in wanted):
            return str(table), dict(notes)
    return None


def _few_values(column: Mapping[str, Any]) -> bool:
    """取值不多：码值表列出的取值不超过 CANDIDATE_MAX 个，或者标了已列全。只认码值表：剖析不记每列的取值数（见模块说明）。"""
    codes = column.get("codes")
    if not isinstance(codes, Mapping) or codes.get("status") == "rejected":
        return False
    values = codes.get("value")
    return codes.get("complete") is True or (isinstance(values, Mapping) and 0 < len(values) <= CANDIDATE_MAX)


def _candidates(table: str, notes: Mapping[str, Any], masked: set[str]) -> list[tuple[str, str, str]]:
    """一张表上的候选维度：(表, 列, 叫法)，按目录里的顺序。"""
    out = []
    for column, body in (notes.get("columns") or {}).items():
        if not isinstance(body, Mapping) or name_key(str(column)) in masked:
            continue
        if _value(body.get("measure")) in _DIMENSION_MEASURES and _few_values(body):
            label = _value(body.get("label"))
            out.append((table, str(column), str(label) if isinstance(label, str) and label.strip() else str(column)))
    return out


def _related(notes: Mapping[str, Any], tables: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """事实表关联的维度表：关系没被驳回、对面那张表目录里标了 dimension 或关系是多对一。"""
    out = []
    for rel in notes.get("relations") or []:
        if not isinstance(rel, Mapping) or rel.get("status") == "rejected" or not rel.get("to_table"):
            continue
        found = _table_notes(tables, str(rel["to_table"]))
        if found and (_value(found[1].get("kind")) == "dimension" or rel.get("cardinality") == "many_to_one"):
            out.append(found)
    return out


def _maintained(tables: Mapping[str, Any]) -> bool:
    """目录维护过没有：任何一张表标了类型，或者任何一个字段标了度量类型。"""
    for notes in tables.values():
        if not isinstance(notes, Mapping):
            continue
        if _value(notes.get("kind")):
            return True
        for body in (notes.get("columns") or {}).values():
            if isinstance(body, Mapping) and _value(body.get("measure")):
                return True
    return False


def _fact_tables(names: list[str], tables: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]] | None:
    """查询里的事实表；认不出的为 None。"""
    found = [hit for name in names if (hit := _table_notes(tables, name))]
    facts = [(t, n) for t, n in found if _value(n.get("kind")) in _FACT_KINDS]
    if facts:
        return facts
    if len(names) == 1 and len(found) == 1 and _value(found[0][1].get("kind")) in (None, ""):
        return found                    # 只读一张表、目录没标表类型：就是它
    return None


def _names(raw: Any) -> list[str]:
    """遮罩列：列表，或者「a, b」这样的一段文字（同数据源设置里的写法）。"""
    from app.data.engine import masked_columns

    return masked_columns({"mask_columns": raw}) if raw else []


def _undecomposed(decomposes: list[dict[str, Any]], catalog: Mapping[str, Any],
                  sources: Mapping[str, Mapping[str, Any]] | None,
                  load: Callable[[str], Any]) -> tuple[list[str] | None, str | None]:
    """(没拆过的候选维度的叫法, 核对不了时的说明)。"""
    from app.engine.evidence import sql_tables

    if sources is None:
        return None, NOTE_NO_CATALOG
    if not decomposes:
        return None, NOTE_NO_DECOMPOSE
    queries = {str(e.get("artifact")): e for e in catalog.values()
               if isinstance(e, dict) and e.get("kind") == "query" and e.get("artifact")}
    pool: dict[tuple[str, str, str], str] = {}          # (源, 表, 列 name_key) → 叫法，按目录里的顺序
    used: set[tuple[str, str, str]] = set()
    for result in decomposes:
        snapshots = [i.get("artifact") for i in result.get("inputs") or []
                     if isinstance(i, Mapping) and i.get("role") in _DATA_ROLES and i.get("artifact")]
        if not snapshots:
            return None, NOTE_NO_QUERY
        known: dict[str, tuple[str, str, str]] = {}     # 这份拆解能对上的列：name_key → (源, 表, 列)
        bare = False                                    # 目录还没维护（没有任何列说明）：对不上是目录的事，不是数据的事
        for artifact in snapshots:
            try:
                snapshot = load(str(artifact))
            except (ValueError, OSError):
                snapshot = None
            if not isinstance(snapshot, Mapping):
                return None, NOTE_NO_QUERY
            source = str(snapshot.get("source") or (queries.get(str(artifact)) or {}).get("source") or "")
            facts_of = sources.get(source) if source else None
            if facts_of is None:
                return None, NOTE_NO_CATALOG
            tables = facts_of.get("tables") or {}
            if not tables:
                return None, NOTE_NO_NOTES
            masked = {name_key(str(c)) for c in [*_names(facts_of.get("masked")), *_names(snapshot.get("mask_columns"))]}
            facts = _fact_tables(sql_tables(str(snapshot.get("sql") or "")), tables)
            if facts is None:
                # 目录还没维护（表类型、字段的度量类型都没标）时说清楚是目录没维护，不说成「认不出事实表」——
                # 后者听着像数据的问题，实际上补一下目录就能列
                return None, NOTE_NO_FACT if _maintained(tables) else NOTE_NOT_MAINTAINED
            bare = bare or not any((n.get("columns") or {}) for _, n in facts)
            for table, notes in [*facts, *(r for t, n in facts for r in _related(n, tables))]:
                for column in (notes.get("columns") or {}):
                    known.setdefault(name_key(str(column)), (source, table, name_key(str(column))))
                for _, column, label in _candidates(table, notes, masked):
                    pool.setdefault((source, table, name_key(column)), label)
        for dim, label in zip(_dims(result), _dim_labels(result)):
            hit = known.get(name_key(dim))
            if hit is None:
                # 目录一列说明都没有时，「对不上」是目录没维护造成的（单表查询时 _fact_tables 会兜底认出那张表，
                # 走不到上面 NOTE_NOT_MAINTAINED 那一条），照实说目录没维护，不说成数据对不上
                if bare or not _maintained(tables):
                    return None, NOTE_NOT_MAINTAINED
                return None, f"候选维度核对不了：拆解按的「{label}」对不上数据目录里的列"
            used.add(hit)
    return [label for key, label in pool.items() if key not in used], None


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def build(catalog: Mapping[str, Any], *, sources: Mapping[str, Mapping[str, Any]] | None = None,
          load: Callable[[str], Any] | None = None) -> ScopeBlock | None:
    """按写作目录里的分析条目算「分析范围」；目录里没有分析结论返回 None（不写这一项）。

    sources 是 {数据源名: {"tables": {表: 数据目录 notes}, "masked": [遮罩列]}}（读不到目录时为 None，候选维度核对不了）；
    load 按 sha256 取回拆解的输入快照（缺省 artifact_store.load），认 SQL 用的是哪几张表。"""
    if load is None:
        from app.core.artifact_store import load as load_artifact

        load = load_artifact
    found = results(catalog)
    if not found:
        return None
    by_artifact = {str(e.get("artifact")): alias for alias, e, _ in found if e.get("artifact")}
    items = {alias: _item(alias, r, _parent(r, by_artifact)) for alias, _, r in found}
    ok = [(alias, r) for alias, _, r in found if r.get("status") == "ok"]
    decomposes = [r for _, r in ok if r.get("op") == "decompose"]
    excluded = [items[a] for a, r in ok
                if (r.get("op") == "decompose" and _code(r) == "dispersed")
                or (r.get("op") == "differ" and _code(r) == "no_evidence")]
    tests = sum(1 for _, r in ok if r.get("op") == "differ")
    try:
        missing, note = _undecomposed([r for r in decomposes if _dims(r)], catalog, sources, load)
    except Exception:  # noqa: BLE001 - 分析范围是附带的说明，算不出来照实说核对不了，不挡报告
        logger.exception("分析范围的候选维度没有算成")
        missing, note = None, "候选维度核对不了：核对时出了错"
    return {
        "phenomena": [items[a] for a, r in ok if r.get("op") in _PHENOMENA],
        "decomposed": _chain([items[a] for a, r in ok if r.get("op") == "decompose"]),
        "excluded": excluded,
        "refused": [items[a] for a, _, r in found if r.get("status") != "ok"],
        "not_decomposed": missing,
        "note": note,
        "tests": tests,
        "corrected": None if not tests else tests == 1,
    }


async def source_facts(names: Iterable[str], view: Any = None) -> dict[str, dict[str, Any]] | None:
    """几个数据源（按名字）在这次运行的目录视图下的数据目录和遮罩列：{源名: {"tables": {表: notes}, "masked": [列]}}。
    目录视图关掉的、读库出错的返回 None（候选维度核对不了）；没有目录的源 tables 为空。"""
    from sqlalchemy import select

    from app.data import catalog_view
    from app.data.engine import masked_columns
    from app.db.base import SessionLocal
    from app.db.models import DataSource

    wanted = [n for n in dict.fromkeys(str(n) for n in names) if n]
    view = catalog_view.CatalogView.parse(view)
    if view.mode == "off":
        return None
    try:
        async with SessionLocal() as session:
            rows = list((await session.execute(select(DataSource).where(DataSource.name.in_(wanted)))).scalars()) \
                if wanted else []
            entries = await catalog_view.read_many(session, view, [r.id for r in rows])
    except Exception:  # noqa: BLE001 - 目录是附带的说明，读不到照实说核对不了
        logger.exception("分析范围读取数据目录失败")
        return None
    return {row.name: {"tables": {t: e.notes for t, e in entries.get(row.id, {}).items()},
                       "masked": masked_columns(row.options)} for row in rows}


async def scope_of(catalog: Mapping[str, Any], view: Any = None, *,
                   load: Callable[[str], Any] | None = None) -> ScopeBlock | None:
    """报告节点用：读好拆解所用查询的数据源的目录，算「分析范围」。没有分析结论返回 None。"""
    if load is None:
        from app.core.artifact_store import load as load_artifact

        load = load_artifact
    found = results(catalog)
    if not found:
        return None
    sources: dict[str, dict[str, Any]] | None = None
    try:
        sources = await source_facts(_source_names(found, catalog, load), view)
    except Exception:  # noqa: BLE001 - 分析范围是附带的说明，读不到目录照实说核对不了，不挡报告
        logger.exception("分析范围没有读成数据目录")
    return build(catalog, sources=sources, load=load)


def _source_names(found: list[tuple[str, dict[str, Any], dict[str, Any]]], catalog: Mapping[str, Any],
                  load: Callable[[str], Any]) -> list[str]:
    """分析结论的输入快照来自哪几个数据源：写作目录里那次查询记的源名，没有就读快照。"""
    names: list[str] = []
    for _, _, result in found:
        for item in result.get("inputs") or []:
            if not (isinstance(item, Mapping) and item.get("role") in _DATA_ROLES and item.get("artifact")):
                continue
            entry = next((e for e in catalog.values() if isinstance(e, dict) and e.get("kind") == "query"
                          and e.get("artifact") == item["artifact"]), None)
            name = (entry or {}).get("source")
            if not name:
                try:
                    snapshot = load(str(item["artifact"]))
                except (ValueError, OSError):
                    snapshot = None
                name = snapshot.get("source") if isinstance(snapshot, Mapping) else None
            if name:
                names.append(str(name))
    return names


__all__ = ["CANDIDATE_MAX", "NOTE_NO_CATALOG", "NOTE_NO_DECOMPOSE", "NOTE_NO_FACT", "NOTE_NO_NOTES", "NOTE_NO_QUERY", "build",
           "results", "scope_of", "source_facts"]
