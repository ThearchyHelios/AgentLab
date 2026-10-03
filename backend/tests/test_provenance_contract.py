"""期 4 证据下钻的契约（P4-SPEC 7.1，WP-0）：provenance_types 的常量、取值、原文、响应形状和中间结构。

并行的几个工作包看不到彼此的代码，只靠这份契约对齐。这里钉住的是「改了就会合错」的东西：字段名、顺序、缺省值、
asdict 之后的 JSON 键（逐层对 P4-SPEC 2.8.1）、原文覆盖每个 code、原文守文案规范。另外两份 WP-0 交付物也在这里
做最低限度的自检：前端夹具里每一份推断来源的答复都能按契约读回、满足不变式；金样的工件 id 等于内容哈希。
"""
from __future__ import annotations

import ast
import dataclasses
import json
import re
import types
import typing
from dataclasses import MISSING, asdict
from pathlib import Path
from typing import Any, get_args, get_origin

import pydantic
import pytest

from app.data import provenance_types as P
from app.data import recipe_types as T

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "frontend" / "src" / "run" / "__tests__" / "evidence-provenance.json"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "evidence" / "pre_p4_golden.json"

# ---------------------------------------------------------------------------
# 常量与取值
# ---------------------------------------------------------------------------


def test_constants():
    assert P.DOC_PROVENANCE == 1
    assert P.SCHEMA == "agentlab.provenance/1"
    assert (P.PROV_PARTS, P.PROV_ACCEPT, P.PROV_OUTSIDE, P.PROV_OUTSIDE_CHARS, P.PROV_COLUMN_NOTES, P.PROV_CHARS) == (
        3, 4, 4, 200, 6, 1200)


def test_literals():
    assert get_args(P.Status) == ("inferred", "table_only", "none")
    assert set(get_args(P.ReasonCode)) == {
        "legacy_doc", "not_cell", "not_sealed", "not_upload", "simple_upload", "manifest_unreadable",
        "chain_mismatch", "expression", "alias", "multi_table", "unparsed", "no_pk", "pk_missing", "masked",
        "null_value", "null_pk", "snapshot_gone", "db_tampered", "recheck_missing", "recheck_multiple",
        "recheck_mismatch", "no_lineage", "merge_no_lineage"}
    assert len(get_args(P.ReasonCode)) == len(set(get_args(P.ReasonCode)))
    assert set(get_args(P.AlertCode)) == {"db_tampered", "chain_mismatch", "manifest_unreadable"}
    assert set(get_args(P.AlertCode)) <= set(get_args(P.ReasonCode))
    assert get_args(P.RefusalCode) == ("expression", "alias", "multi_table", "unparsed")
    assert set(P.DETAILS) == set(get_args(P.RefusalCode))
    assert P.DETAILS["multi_table"] == ("join", "comma_join", "subquery", "cte", "compound", "authorizer",
                                        "duplicate_pk")
    assert "literal_keyword" in P.DETAILS["expression"] and "ambiguous_column" in P.DETAILS["unparsed"]
    assert get_args(P.FromRole) == ("axis_header", "row_label", "section_title", "col_header", "total_label")
    assert get_args(P.RowStatus) == ("passed", "mismatch", "unverifiable")
    assert get_args(P.CellStatus) == ("ok", "unverifiable", "unknown", "not_formula")
    assert get_args(P.RawState) == ("kept", "purged", "absent")
    assert get_args(P.Mode) == ("replace", "accumulate")
    assert get_args(P.AcceptanceKind) == ("override", "waiver")
    assert get_args(P.YearFrom) == ("period", "human")
    assert get_args(P.ColumnRole) == ("axis", "dim", "derive", "const", "measure", "value", "text")
    # 和配方契约同名的取值：照搬 CheckResult.status、TableOut.kind、PeriodOut.source，不能各写一份漂开
    assert get_args(P.PartStatus) == get_args(T.CheckStatus)
    assert get_args(P.TableKind) == get_args(typing.get_type_hints(T.TableOut)["kind"])
    assert get_args(typing.get_type_hints(P.PeriodView)["source"]) == get_args(
        typing.get_type_hints(T.PeriodOut)["source"])


def test_module_imports_only_stdlib():
    """识别器只许导入守卫、names 和这个模块（P4-SPEC 7.2）：这里不能把配方模块、pydantic 拖进来。"""
    tree = ast.parse(Path(P.__file__).read_text("utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    assert mods <= {"__future__", "logging", "re", "collections", "dataclasses", "typing"}, mods


# ---------------------------------------------------------------------------
# 每个 dataclass 的字段名、顺序和缺省值（防止实现者改名；REQ = 必填）
# ---------------------------------------------------------------------------

REQ = "<必填>"
FACTORY = "<默认工厂>"


def _shape(cls) -> list[tuple[str, object]]:
    out = []
    for f in dataclasses.fields(cls):
        if f.default is not MISSING:
            out.append((f.name, f.default))
        elif f.default_factory is not MISSING:
            out.append((f.name, FACTORY))
        else:
            out.append((f.name, REQ))
    return out


SHAPES: dict[str, list[tuple[str, object]]] = {
    # 响应（P4-SPEC 2.8.1）
    "ReportRef": [("node_id", REQ), ("doc_artifact", REQ)],
    "CellRef": [("alias", REQ), ("row", REQ), ("column", REQ), ("artifact", None)],
    "Reason": [("code", REQ), ("detail", ""), ("text", REQ)],
    "Alert": [("code", REQ), ("text", REQ)],
    "TableRef": [("name", REQ), ("kind", "data")],
    "PeriodView": [("start", REQ), ("end", REQ), ("source", REQ), ("cells", FACTORY), ("signed_by", None)],
    "AcceptanceView": [("check_id", REQ), ("title", REQ), ("kind", REQ), ("reason", REQ), ("signed_by", None),
                       ("at", None)],
    "StateNote": [("at", None), ("signed_by", None), ("reason", None)],
    "PartView": [("import_id", REQ), ("seq", REQ), ("manifest", REQ), ("period", REQ), ("file_name", REQ),
                 ("raw_sha256", REQ), ("region", None), ("excluded_rows", None), ("recipe_sha256", None),
                 ("recipe_seq", None), ("committed_at", None), ("signed_by", None), ("acceptances", FACTORY),
                 ("raw_state", None), ("purged", None), ("revoked", None), ("has_row", False)],
    "VersionView": [("source", REQ), ("snapshot_id", REQ), ("mode", REQ), ("union", REQ), ("tables", FACTORY),
                    ("manifest_view", True), ("parts", FACTORY)],
    "FromCell": [("role", REQ), ("column", REQ), ("sheet", REQ), ("cell", REQ), ("text", None),
                 ("locate_title", None)],
    "YearSource": [("source", REQ), ("cells", FACTORY), ("signed_by", None), ("mixed", False)],
    "CanonicalView": [("raw", REQ), ("canonical", REQ)],
    "Recheck": [("sql", REQ), ("params", FACTORY), ("ok", REQ)],
    "CellSource": [("table", REQ), ("column", REQ), ("column_role", REQ), ("kind", "data"), ("pk", FACTORY),
                   ("rowid", None), ("part_seq", REQ), ("part_rowid", REQ), ("sheet", REQ), ("cell", REQ),
                   ("header", None), ("unit", None), ("from", FACTORY), ("year", None), ("canonical", None),
                   ("raw_purged", False), ("merged_fill", False), ("recheck", None)],
    "RelatedCheck": [("id", REQ), ("kind", REQ), ("title", REQ), ("part_status", REQ), ("row_status", None),
                     ("cell_status", None), ("acceptance", None), ("detail", None)],
    "ProvenanceOut": [("schema", P.SCHEMA), ("report", REQ), ("segment", REQ), ("cell", None), ("status", REQ),
                      ("reason", None), ("alert", None), ("sealed", REQ), ("version", None),
                      ("cell_source", None), ("checks", FACTORY), ("merge", FACTORY)],
    # 合并查询结果里的格经过的每一次合并（P3：合并查询节点）
    "MergeHop": [("alias", REQ), ("node_id", None), ("input", None), ("query", None), ("row", None),
                 ("column", None)],
    # 中间结构（P4-SPEC 7.2–7.4）
    "DirectSelect": [("table", REQ), ("alias", REQ), ("columns", REQ)],
    "SelectRefusal": [("code", REQ), ("detail", REQ)],
    "ChainPart": [("seq", REQ), ("import_id", REQ), ("manifest_id", REQ), ("manifest", REQ), ("union_rows", REQ)],
    "Chain": [("source", REQ), ("source_id", REQ), ("snapshot_id", REQ), ("mode", REQ), ("union", REQ),
              ("expected_db_sha256", REQ), ("parts", REQ), ("schema", REQ), ("snapshot_manifest", REQ)],
    "ChainProblem": [("code", REQ), ("detail", "")],
    "Located": [("part", REQ), ("part_rowid", REQ), ("cell", REQ)],
    "LocateProblem": [("code", REQ), ("detail", "")],
    "SumEqRule": [("table", REQ), ("total", REQ), ("parts", REQ), ("types", REQ)],
    "RelatedCheckPlan": [("check", REQ), ("rule", REQ)],
    "CompileFacts": [("tables", REQ), ("result_rows", REQ), ("yields", REQ)],
}
RESPONSE = ("ReportRef", "CellRef", "Reason", "Alert", "TableRef", "PeriodView", "AcceptanceView", "StateNote",
            "PartView", "VersionView", "FromCell", "YearSource", "CanonicalView", "Recheck", "CellSource",
            "RelatedCheck", "ProvenanceOut", "MergeHop")


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_dataclass_shape(name):
    cls = getattr(P, name)
    assert dataclasses.is_dataclass(cls)
    assert _shape(cls) == SHAPES[name]


def test_every_dataclass_is_pinned():
    declared = {n for n, v in vars(P).items() if isinstance(v, type) and dataclasses.is_dataclass(v)}
    assert declared == set(SHAPES), declared ^ set(SHAPES)


@pytest.mark.parametrize("name", RESPONSE)
def test_response_types_are_keyword_only(name):
    """响应的字段多、几个包各自构造：按位置传参会被拒，传错名字立刻报错。"""
    cls = getattr(P, name)
    assert all(f.kw_only for f in dataclasses.fields(cls)), name
    with pytest.raises(TypeError):
        cls("x")


def test_intermediate_types_take_positional_args_as_the_spec_writes_them():
    assert P.ChainProblem("manifest_unreadable").detail == ""
    assert P.LocateProblem("no_lineage") == P.LocateProblem(code="no_lineage", detail="")
    assert P.SelectRefusal("alias", "alias").code == "alias"
    assert P.CompileFacts({"日客流"}, 1, 0).yields == 0
    assert P.DirectSelect("日客流", None, ["日期", "全日客流"]).columns == ["日期", "全日客流"]


# ---------------------------------------------------------------------------
# asdict 之后逐层对 P4-SPEC 2.8.1 的 JSON
# ---------------------------------------------------------------------------

KEYS = {
    "top": ["schema", "report", "segment", "cell", "status", "reason", "alert", "sealed", "version", "cell_source",
            "checks", "merge"],
    "merge": ["alias", "node_id", "input", "query", "row", "column"],
    "report": ["node_id", "doc_artifact"],
    "cell": ["alias", "row", "column", "artifact"],
    "reason": ["code", "detail", "text"],
    "alert": ["code", "text"],
    "version": ["source", "snapshot_id", "mode", "union", "tables", "manifest_view", "parts"],
    "table": ["name", "kind"],
    "part": ["import_id", "seq", "manifest", "period", "file_name", "raw_sha256", "region", "excluded_rows",
             "recipe_sha256", "recipe_seq", "committed_at", "signed_by", "acceptances", "raw_state", "purged",
             "revoked", "has_row"],
    "period": ["start", "end", "source", "cells", "signed_by"],
    "acceptance": ["check_id", "title", "kind", "reason", "signed_by", "at"],
    "state": ["at", "signed_by", "reason"],
    "cell_source": ["table", "column", "column_role", "kind", "pk", "rowid", "part_seq", "part_rowid", "sheet",
                    "cell", "header", "unit", "from", "year", "canonical", "raw_purged", "merged_fill", "recheck"],
    "from": ["role", "column", "sheet", "cell", "text", "locate_title"],
    "year": ["source", "cells", "signed_by", "mixed"],
    "canonical": ["raw", "canonical"],
    "recheck": ["sql", "params", "ok"],
    "check": ["id", "kind", "title", "part_status", "row_status", "cell_status", "acceptance", "detail"],
}


def _accept(**kw) -> P.AcceptanceView:
    return P.AcceptanceView(**{"check_id": "R1", "title": "「日客流」：全日客流 = 分区甲 + 分区乙", "kind": "override",
                               "reason": "合成理由：分区合计口径调整", "signed_by": None,
                               "at": "2026-10-01T08:00:00+00:00", **kw})


def full_out() -> P.ProvenanceOut:
    """字段齐全的一份（每个可空的嵌套对象都非空、每个列表至少一项）：空的 checks 会让键集合的比较空过。"""
    return P.ProvenanceOut(
        report=P.ReportRef(node_id="write", doc_artifact="d" * 64),
        segment="s7",
        cell=P.CellRef(alias="Q3", row=0, column="全日客流", artifact="a" * 64),
        status="inferred",
        sealed=True,
        version=P.VersionView(
            source="flow_demo", snapshot_id="b" * 64, mode="accumulate", union=True,
            tables=[P.TableRef(name="日客流", kind="data")],
            manifest_view=True,
            parts=[P.PartView(
                import_id="i1", seq=1, manifest="c" * 64,
                period=P.PeriodView(start="2026-08-01", end="2026-08-31", source="cells", cells=["客流汇总!B2"]),
                file_name="月报导出_2026-08-01_2026-08-31.xlsx", raw_sha256="e" * 64,
                region="客流汇总!B4:AG30", excluded_rows=0, recipe_sha256="f" * 64, recipe_seq=1,
                committed_at="2026-10-01T08:00:00+00:00", signed_by=None,
                acceptances=[_accept()], raw_state="purged",
                purged=P.StateNote(at="2026-10-02T08:00:00+00:00", signed_by="录入员甲", reason="合成理由"),
                revoked=P.StateNote(at="2026-10-02T09:00:00+00:00", signed_by=None, reason="合成理由"),
                has_row=True)]),
        cell_source=P.CellSource(
            table="日客流", column="全日客流", column_role="measure", kind="data", pk={"日期": "2026-08-05"},
            rowid=5, part_seq=1, part_rowid=5, sheet="客流汇总", cell="G5", header="全日客流（人次）", unit="人次",
            from_=[P.FromCell(role="axis_header", column="日期", sheet="客流汇总", cell="G4"),
                   P.FromCell(role="row_label", column=None, sheet="客流汇总", cell="B5", text="全日客流（人次）")],
            year=P.YearSource(source="period", cells=["客流汇总!B2"]),
            canonical=P.CanonicalView(raw="8–9", canonical="8-9"),
            raw_purged=True, merged_fill=False,
            recheck=P.Recheck(sql='SELECT rowid, "全日客流" FROM "日客流" WHERE "日期" = ?', params=["2026-08-05"],
                              ok=True)),
        checks=[P.RelatedCheck(id="R1", kind="relation_sum_eq", title="「日客流」：全日客流 = 分区甲 + 分区乙",
                               part_status="mismatch", row_status="passed", cell_status=None,
                               acceptance=_accept(), detail="合成的原因摘要")],
        merge=[P.MergeHop(alias="Q3", node_id="merge", input="f", query="Q1", row=5, column="全日客流")],
    )


def test_asdict_matches_the_spec_json_level_by_level():
    d = asdict(full_out())
    v, part, cs = d["version"], d["version"]["parts"][0], d["cell_source"]
    levels = {
        "top": d, "report": d["report"], "cell": d["cell"], "version": v, "table": v["tables"][0], "part": part,
        "period": part["period"], "acceptance": part["acceptances"][0], "state": part["purged"],
        "cell_source": cs, "from": cs["from"][0], "year": cs["year"], "canonical": cs["canonical"],
        "recheck": cs["recheck"], "check": d["checks"][0], "merge": d["merge"][0],
    }
    for name, obj in levels.items():
        assert isinstance(obj, dict), name
        assert list(obj) == KEYS[name], name
    assert list(part["revoked"]) == KEYS["state"]
    assert list(d["checks"][0]["acceptance"]) == KEYS["acceptance"]
    # 原因、提示：table_only 加红的一份
    red = P.ProvenanceOut(report=P.ReportRef(node_id="write", doc_artifact="d" * 64), segment="s9",
                          status="table_only", reason=P.refuse("db_tampered"), alert=P.alert("db_tampered"),
                          sealed=True, version=full_out().version)
    rd = asdict(red)
    assert list(rd["reason"]) == KEYS["reason"] and list(rd["alert"]) == KEYS["alert"]
    assert rd["cell_source"] is None and rd["checks"] == [] and rd["cell"] is None
    json.dumps(d, ensure_ascii=False)


def test_null_and_empty_defaults_match_the_spec():
    out = P.ProvenanceOut(report=P.ReportRef(node_id="write", doc_artifact="d"), segment="s1", status="none",
                          reason=P.refuse("legacy_doc"), sealed=False)
    d = asdict(out)
    assert d["schema"] == "agentlab.provenance/1"
    assert (d["cell"], d["alert"], d["version"], d["cell_source"], d["checks"], d["merge"]) == (
        None, None, None, None, [], [])
    assert d["reason"] == {"code": "legacy_doc", "detail": "", "text": P.REASON_TEXT["legacy_doc"]}


# ---------------------------------------------------------------------------
# CellSource 的「from」：Python 里是 from_，asdict / fields / replace / JSON 里是 from
# ---------------------------------------------------------------------------


def _cell(**kw) -> P.CellSource:
    base = {"table": "日客流", "column": "全日客流", "column_role": "measure", "part_seq": 1, "part_rowid": 5,
            "sheet": "客流汇总", "cell": "G5"}
    return P.CellSource(**{**base, **kw})


def test_cell_source_from_key():
    fc = P.FromCell(role="axis_header", column="日期", sheet="客流汇总", cell="G4")
    a = _cell(from_=[fc])
    assert a.from_ == [fc] and getattr(a, "from") == [fc]
    assert [f.name for f in dataclasses.fields(P.CellSource)].count("from") == 1
    assert "from_" not in {f.name for f in dataclasses.fields(P.CellSource)}
    d = asdict(a)
    assert "from" in d and "from_" not in d and d["from"][0]["cell"] == "G4"
    # 从 JSON 读回来：键是 from
    b = P.CellSource(**{**d, "from": [P.FromCell(**x) for x in d["from"]]})
    assert b == a
    # replace 不丢 from（WP-C 回查后用 replace 补 rowid、pk、recheck 是常见写法）
    c = dataclasses.replace(a, rowid=5, pk={"日期": "2026-08-05"})
    assert c.from_ == [fc] and c.rowid == 5
    # replace 换 from：两种名字都认，Python 名字优先
    fc2 = P.FromCell(role="row_label", column=None, sheet="客流汇总", cell="B5")
    assert dataclasses.replace(a, from_=[fc2]).from_ == [fc2]
    assert dataclasses.replace(a, **{"from": [fc2]}).from_ == [fc2]
    # 属性写入两种名字指同一份
    setattr(a, "from", [])
    assert a.from_ == [] and asdict(a)["from"] == []
    assert _cell().from_ == [] and _cell().from_ is not _cell().from_
    assert "from_=" in repr(_cell())
    # 注解里同样是 from（pydantic 按注解收集字段）；值存在实例字典的 from 键下
    hints = typing.get_type_hints(P.CellSource)
    assert "from" in hints and "from_" not in hints
    assert list(hints) == [f.name for f in dataclasses.fields(P.CellSource)]
    assert "from" in vars(a) and "from_" not in vars(a)
    assert not hasattr(object.__new__(P.CellSource), "from_")


def test_pydantic_sees_the_same_json_as_asdict():
    """WP-C 若把 ProvenanceOut 写成返回注解或 response_model，pydantic 会按类型再序列化一遍：输出必须等于 asdict，
    不能悄悄少掉「from」（只改字段表、不改注解时就是这样）。从 JSON 校验回来的实例也要能照常读写 from_。"""
    ta = pydantic.TypeAdapter(P.ProvenanceOut)
    out = full_out()
    want = asdict(out)
    assert ta.dump_python(out) == want and "from" in ta.dump_python(out)["cell_source"]
    assert json.loads(ta.dump_json(out)) == json.loads(json.dumps(want))
    back = ta.validate_json(json.dumps(want), strict=True)
    assert back == out and back.cell_source.from_ == out.cell_source.from_
    assert asdict(back) == want and dataclasses.replace(back.cell_source, rowid=9).from_ == out.cell_source.from_
    cs_ta = pydantic.TypeAdapter(P.CellSource)
    assert cs_ta.dump_python(out.cell_source) == asdict(out.cell_source)


async def test_fastapi_routes_return_the_same_json_however_typed():
    """三种写法的路由（推荐的 asdict + dict 注解、带类型的返回注解、response_model）回的 JSON 逐字相同。"""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()

    @app.get("/plain")
    async def plain() -> dict[str, Any]:
        return asdict(full_out())

    @app.get("/typed")
    async def typed() -> P.ProvenanceOut:
        return full_out()

    @app.get("/model", response_model=P.ProvenanceOut)
    async def model() -> Any:
        return asdict(full_out())

    want = json.loads(json.dumps(asdict(full_out())))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for path in ("/plain", "/typed", "/model"):
            r = await client.get(path)
            assert r.status_code == 200, path
            assert r.json() == want, path
            assert "from" in r.json()["cell_source"], path
        assert (await client.get("/openapi.json")).status_code == 200


# ---------------------------------------------------------------------------
# 原文
# ---------------------------------------------------------------------------


def test_reason_text_covers_every_code():
    assert set(P.REASON_TEXT) == set(get_args(P.ReasonCode))
    assert set(P.ALERT_TEXT) == set(get_args(P.AlertCode))
    for code, text in P.ALERT_TEXT.items():
        assert text == P.REASON_TEXT[code]
    # 4.2 的原文（WP-0 写定，界面原样显示）：抽几条与规格逐字对照
    assert P.REASON_TEXT["simple_upload"] == "这份表格按表头行直接导入，没有记录原表格子的溯源"
    assert P.REASON_TEXT["masked"] == "这一格涉及设置了遮罩的列，不推断来源"
    assert P.REASON_TEXT["null_value"] == "这一格是空值，不推断来源"
    assert P.REASON_TEXT["expression"] == "这一格是计算、聚合或改写的结果，只给出表级来历"
    assert "可能被修改过" in P.REASON_TEXT["db_tampered"] and "可能被修改过" in P.REASON_TEXT["chain_mismatch"]
    assert "改了名" in P.REASON_TEXT["alias"] and "同一张表多次" in P.REASON_TEXT["multi_table"]
    assert "已回收或不存在" in P.REASON_TEXT["snapshot_gone"]
    assert P.REASON_TEXT["recheck_missing"] == P.REASON_TEXT["recheck_multiple"] == P.REASON_TEXT["recheck_mismatch"]


_H = "[㐀-鿿]"
_HALF = re.compile(f"{_H}[,;!?]|[,;!?]{_H}|{_H}:(?![/\\d])|:{_H}|{_H}\\(|\\){_H}|{_H}\"|\"{_H}|[“”]|\\.\\.\\.")
_BANNED = re.compile("钉|放行|后端|跑|多半|照样|眼下|压根|取不到|没取回来|读不懂|连不上|写坏了|快照版本")


@pytest.mark.parametrize("code", sorted(P.REASON_TEXT))
def test_reason_text_follows_the_copy_rules(code):
    """原文上界面：不含数字、坐标（prose_problems），不写「钉」「放行」等禁用写法，全角标点、「」引号、不用「...」，
    不以句号收尾（界面上是一行说明，和其余面板文字一致）。"""
    text = P.REASON_TEXT[code]
    assert T.prose_problems(text) == [], (code, T.prose_problems(text))
    assert not _BANNED.search(text), code
    assert not _HALF.search(text), (code, _HALF.search(text))
    assert not text.endswith("。"), code
    assert re.search(_H, text)


def test_refuse_and_alert():
    r = P.refuse("pk_missing", 列=["日期", "时段"])
    assert r == P.Reason(code="pk_missing", detail="",
                         text="查询结果没有带齐主键列（日期、时段），无法按主键回查，只给出表级来历")
    assert "（日期）" in P.refuse("pk_missing", 列="日期").text
    assert P.refuse("multi_table", "duplicate_pk").detail == "duplicate_pk"
    assert P.refuse("multi_table", detail="compound").text == P.REASON_TEXT["multi_table"]
    assert P.refuse("no_lineage", "regions 没有 block 键").detail == "regions 没有 block 键"
    with pytest.raises(ValueError):
        P.refuse("pk_missing")
    with pytest.raises(ValueError):
        P.refuse("no_such_code")  # type: ignore[arg-type]
    assert P.alert("chain_mismatch") == P.Alert(code="chain_mismatch", text=P.ALERT_TEXT["chain_mismatch"])
    with pytest.raises(ValueError):
        P.alert("no_lineage")  # type: ignore[arg-type]


@pytest.mark.parametrize("code,detail", [
    # 原型识别器自己的名字（p4-probe/probe_recognizer.py），WP-A 照抄就会走到这里；还有 2.4 没逐条写的写法
    ("unparsed", "indexed_by"), ("multi_table", "union"), ("expression", "aggregate"),
    ("alias", "expression_or_alias"), ("multi_table", "subquery_or_compound"), ("alias", "join"),
])
def test_refuse_with_an_unknown_detail_degrades_instead_of_raising(code, detail, caplog):
    """细分取决于识别器对这条 SQL 的判断：漏了映射时推断来源接口不能回 500。照样给分组的原文、记警告，
    contract_problems 报出来（接口和验收测试据此抓到）。"""
    with caplog.at_level("WARNING", logger=P.__name__):
        r = P.refuse(code, detail)
    assert r == P.Reason(code=code, detail=detail, text=P.REASON_TEXT[code])
    assert detail in caplog.text
    out = P.ProvenanceOut(report=P.ReportRef(node_id="write", doc_artifact="d"), segment="s1", status="table_only",
                          reason=r, sealed=True, version=_no_row_version())
    assert any("DETAILS" in p for p in P.contract_problems(out))


def test_every_listed_detail_passes_and_empty_detail_is_allowed(caplog):
    v = _no_row_version()
    ref = P.ReportRef(node_id="write", doc_artifact="d")
    with caplog.at_level("WARNING", logger=P.__name__):
        for code, details in P.DETAILS.items():
            for detail in (*details, ""):
                out = P.ProvenanceOut(report=ref, segment="s1", status="table_only", reason=P.refuse(code, detail),
                                      sealed=True, version=v)
                assert P.contract_problems(out) == [], (code, detail)
    assert caplog.text == ""
    # 只有四个判据分组受 DETAILS 约束；其余 code 的细分是自由文字（ChainProblem / LocateProblem 的 detail）
    assert P.contract_problems(P.ProvenanceOut(report=ref, segment="s1", status="table_only",
                                               reason=P.refuse("no_lineage", "regions 没有 block 键"), sealed=True,
                                               version=v)) == []


def _no_row_version() -> P.VersionView:
    v = full_out().version
    for part in v.parts:
        part.has_row = False
    return v


# ---------------------------------------------------------------------------
# 不变式
# ---------------------------------------------------------------------------


def test_contract_problems_accepts_the_valid_shapes():
    assert P.contract_problems(full_out()) == []
    v = full_out().version
    for part in v.parts:
        part.has_row = False
    ref = P.ReportRef(node_id="write", doc_artifact="d")
    ok = [
        P.ProvenanceOut(report=ref, segment="s1", status="none", reason=P.refuse("legacy_doc"), sealed=True),
        P.ProvenanceOut(report=ref, segment="s1", status="none", reason=P.refuse("chain_mismatch"),
                        alert=P.alert("chain_mismatch"), sealed=True),
        P.ProvenanceOut(report=ref, segment="s1", status="table_only", reason=P.refuse("alias", "alias"),
                        sealed=False, version=v),
        P.ProvenanceOut(report=ref, segment="s1", status="table_only", reason=P.refuse("chain_mismatch"),
                        alert=P.alert("chain_mismatch"), sealed=True, version=v),
    ]
    for out in ok:
        assert P.contract_problems(out) == [], out


def test_contract_problems_catches_broken_shapes():
    ref = P.ReportRef(node_id="write", doc_artifact="d")
    v = full_out().version
    broken = [
        dataclasses.replace(full_out(), reason=P.refuse("alias", "alias")),
        dataclasses.replace(full_out(), cell_source=None),
        dataclasses.replace(full_out(), cell_source=dataclasses.replace(full_out().cell_source, recheck=None)),
        P.ProvenanceOut(report=ref, segment="s1", status="none", sealed=True),
        P.ProvenanceOut(report=ref, segment="s1", status="none", reason=P.refuse("db_tampered"), sealed=True),
        P.ProvenanceOut(report=ref, segment="s1", status="none", reason=P.refuse("legacy_doc"), sealed=True,
                        version=v),
        P.ProvenanceOut(report=ref, segment="s1", status="table_only", reason=P.refuse("no_pk"), sealed=True),
        P.ProvenanceOut(report=ref, segment="s1", status="table_only", reason=P.refuse("no_pk"), sealed=True,
                        version=v, checks=full_out().checks),
        P.ProvenanceOut(report=ref, segment="s1", status="table_only", reason=P.refuse("no_pk"),
                        alert=P.alert("db_tampered"), sealed=True, version=v),
    ]
    for out in broken:
        assert P.contract_problems(out), out


def _with_recheck(sql: str, params: list[Any], pk: dict[str, Any]) -> P.ProvenanceOut:
    out = full_out()
    out.cell_source.pk = pk
    out.cell_source.recheck = P.Recheck(sql=sql, params=params, ok=True)
    return out


def test_recheck_uses_question_marks_in_primary_key_order():
    """回查 SQL 的占位符是 ?，params 与 pk 按 primary_key 的顺序一一对应（Recheck 的说明；规格 R16 的 :p0 以此为准）。"""
    pk = {"日期": "2026-08-20", "时段": "13-14"}
    good = 'SELECT rowid, "客流" FROM "时段客流" WHERE "日期" = ? AND "时段" = ?'
    assert P.contract_problems(_with_recheck(good, ["2026-08-20", "13-14"], pk)) == []
    # 列名里的问号不算占位符
    assert P.contract_problems(_with_recheck('SELECT rowid, "是否?" FROM "表""甲" WHERE "日期" = ?',
                                             ["2026-08-05"], {"日期": "2026-08-05"})) == []
    for sql, params in [
        ('SELECT rowid, "客流" FROM "时段客流" WHERE "日期" = :p0 AND "时段" = :p1', ["2026-08-20", "13-14"]),
        (good, ["13-14", "2026-08-20"]),
        (good, ["2026-08-20"]),
        ('SELECT rowid, "客流" FROM "时段客流" WHERE "日期" = ?', ["2026-08-20", "13-14"]),
    ]:
        assert any("recheck" in p for p in P.contract_problems(_with_recheck(sql, params, pk))), (sql, params)


# ---------------------------------------------------------------------------
# recipe_types.OutsideText.hidden
# ---------------------------------------------------------------------------


def test_outside_text_hidden():
    names = [f.name for f in dataclasses.fields(T.OutsideText)]
    assert names == ["sheet", "cell", "text", "kind", "period_source", "hidden"]
    assert T.OutsideText("客流汇总", "客流汇总!B3", "客流汇总表", "text").hidden is None
    assert asdict(T.OutsideText("客流汇总", "客流汇总!B3", "注", "text", False, True))["hidden"] is True
    from app.data.recipe_imports import extraction_from_dict

    old = {"ok": True, "outside_text": [{"sheet": "客流汇总", "cell": "客流汇总!B3", "text": "客流汇总表", "kind": "text",
                                         "period_source": False}]}
    assert extraction_from_dict(old).outside_text[0].hidden is None
    new = {"ok": True, "outside_text": [{**old["outside_text"][0], "hidden": False}]}
    assert extraction_from_dict(new).outside_text[0].hidden is False


# ---------------------------------------------------------------------------
# 前端夹具：每一份推断来源的答复都按契约读得回、满足不变式
# ---------------------------------------------------------------------------


def _convert(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return _build(tp, value)
    origin = get_origin(tp)
    if origin is list:
        (arg,) = get_args(tp)
        return [_convert(arg, x) for x in value]
    if origin in (typing.Union, types.UnionType):
        for arg in get_args(tp):
            if isinstance(arg, type) and dataclasses.is_dataclass(arg):
                return _build(arg, value)
    return value


def _build(cls: type, data: dict[str, Any]) -> Any:
    """按契约把 JSON 读回 dataclass。键必须一个不多、一个不少。"""
    hints = typing.get_type_hints(cls)
    names = [f.name for f in dataclasses.fields(cls)]
    assert set(data) == set(names), (cls.__name__, set(data) ^ set(names))
    return cls(**{n: _convert(hints[n], data[n]) for n in names})


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text("utf-8"))


def test_frontend_fixture_answers_follow_the_contract():
    fx = _fixture()
    assert fx["provenance"], "夹具里至少有一份推断来源的答复"
    statuses = set()
    strict = pydantic.TypeAdapter(P.ProvenanceOut)
    for sid, body in fx["provenance"].items():
        if not isinstance(body, dict) or body.get("schema") != P.SCHEMA:
            continue  # 模拟出错的答复（{status, json}）由检查脚本按原样回放
        out = _build(P.ProvenanceOut, body)
        assert asdict(out) == body, sid
        # 键之外再按类型严格校验一遍：取值不在 Literal 里、数写成字符串、该有的对象是 null 都会报
        assert asdict(strict.validate_json(json.dumps(body), strict=True)) == body, sid
        assert P.contract_problems(out) == [], (sid, P.contract_problems(out))
        assert out.segment == sid
        if out.reason is not None:
            assert out.reason.text == P.refuse(out.reason.code, out.reason.detail, 列=_pk_cols(out)).text, sid
        if out.alert is not None:
            assert out.alert.text == P.ALERT_TEXT[out.alert.code]
        statuses.add(out.status)
    assert statuses == {"inferred", "table_only", "none"}


def _pk_cols(out: P.ProvenanceOut) -> str:
    m = re.search(r"主键列（([^）]*)）", out.reason.text if out.reason else "")
    return m.group(1) if m else ""


def test_frontend_fixture_canonical_follows_the_a8_rule():
    """CanonicalView 的口径（P4-SPEC 2.5、6.2 A8、A18）：这一行标签列（dim、合计项）的原文与数据库里的值不同时给出，
    被引用的是值列也给；相同、或这一行没有标签列（宽表的行标签是指标名，column 为 null）时为 null。"""
    fx = _fixture()
    given = set()
    for sid, body in fx["provenance"].items():
        cs = body.get("cell_source") if isinstance(body, dict) else None
        if not cs:
            continue
        if cs["column_role"] == "dim":
            # 被引用的就是标签格：原文不在 from 里（from 只列其余的格），只核对规范写法那一侧
            if cs["canonical"] is not None:
                assert cs["canonical"]["canonical"] == cs["pk"][cs["column"]], sid
                given.add(sid)
            continue
        labels = [f for f in cs["from"] if f["role"] in ("row_label", "total_label") and f["column"] is not None]
        assert len(labels) <= 1, sid
        if labels and labels[0]["text"] is not None and labels[0]["text"] != cs["pk"][labels[0]["column"]]:
            assert cs["canonical"] == {"raw": labels[0]["text"], "canonical": cs["pk"][labels[0]["column"]]}, sid
            given.add(sid)
        else:
            assert cs["canonical"] is None, sid
    # 合计表那一格（18-22 时合计）、累积并集里全角标签那一期的值格和标签格（８－９）
    cases = fx["cases"]
    assert {cases[k]["segment"] for k in ("total", "union", "fullwidth")} <= given, given
    assert fx["provenance"][cases["total"]["segment"]]["cell_source"]["canonical"] == {
        "raw": "18-22 时合计", "canonical": "18-22时合计"}
    assert fx["provenance"][cases["union"]["segment"]]["cell_source"]["canonical"] == {"raw": "８－９", "canonical": "8-9"}


def test_frontend_fixture_cases_point_at_real_segments():
    """检查脚本按片段文字认片段：cases 里每个场景的文字在文档里恰好对应一个片段，编号与之一致。"""
    fx = _fixture()
    segs = [s for b in fx["doc"]["blocks"] for u in b["units"] for s in u["segments"]]
    assert fx["doc"].get("provenance") == P.DOC_PROVENANCE
    for key, case in fx["cases"].items():
        hits = [s for s in segs if s["text"] == case["text"] and s.get("cite")]
        assert len(hits) == 1, (key, case["text"], len(hits))
        assert hits[0]["id"] == case["segment"], key
        assert case["segment"] in fx["segments"], key


# ---------------------------------------------------------------------------
# 前端类型（frontend/src/types.ts）与契约逐个字段对齐：合并后 types.ts 归 WP-D，改名会在这里暴露
# ---------------------------------------------------------------------------

TYPES_TS = ROOT / "frontend" / "src" / "types.ts"
TS_INTERFACES = {
    "EvidenceProvenance": "ProvenanceOut", "ProvenanceReportRef": "ReportRef", "ProvenanceCellRef": "CellRef",
    "ProvenanceReason": "Reason", "ProvenanceAlert": "Alert", "ProvenanceTableRef": "TableRef",
    "ProvenancePeriod": "PeriodView", "ProvenanceAcceptance": "AcceptanceView", "ProvenanceStateNote": "StateNote",
    "ProvenancePart": "PartView", "ProvenanceVersion": "VersionView", "ProvenanceFromCell": "FromCell",
    "ProvenanceYear": "YearSource", "ProvenanceCanonical": "CanonicalView", "ProvenanceRecheck": "Recheck",
    "ProvenanceCellSource": "CellSource", "ProvenanceCheck": "RelatedCheck", "ProvenanceMergeHop": "MergeHop",
}
TS_UNIONS = {
    "ProvenanceStatus": P.Status, "ProvenanceReasonCode": P.ReasonCode, "ProvenanceAlertCode": P.AlertCode,
    "ProvenanceFromRole": P.FromRole, "ProvenancePartStatus": P.PartStatus, "ProvenanceRowStatus": P.RowStatus,
    "ProvenanceCellStatus": P.CellStatus,
}


#: 有意与 Python 不同的几处（字段 → TS 里应写的联合成员）：
#: - rowid、recheck 只在 inferred 时有、那时必填（contract_problems 钉住），TS 写成非空，界面不用到处判空；
#: - schema、column_role 带开放的尾巴 `(string & {})`：以后加取值时老界面照样能编译、能显示原值
TS_OVERRIDES: dict[tuple[str, str], tuple[str, ...]] = {
    ("ProvenanceCellSource", "rowid"): ("number",),
    ("ProvenanceCellSource", "recheck"): ("ProvenanceRecheck",),
    ("EvidenceProvenance", "schema"): (f"'{P.SCHEMA}'", "(string & {})"),
    ("ProvenanceCellSource", "column_role"): (*(f"'{x}'" for x in get_args(P.ColumnRole)), "(string & {})"),
}


def _ts_fields(src: str, name: str) -> list[str]:
    return list(_ts_field_types(src, name))


def _ts_field_types(src: str, name: str) -> dict[str, str]:
    """接口的字段 → 类型原文（`name: type`、`name?: type`，可选的在键名后保留问号）。"""
    m = re.search(rf"^export interface {name} \{{\n(.*?)^\}}", src, re.S | re.M)
    assert m, name
    return {f + q: t.strip() for f, q, t in re.findall(r"^  (\w+)(\??): (.+)$", m.group(1), re.M)}


def _ts_aliases(src: str) -> dict[str, tuple[str, ...]]:
    """TS_UNIONS 里的具名联合 → 字面量成员（带引号，同 _ts_members 的写法）。"""
    out = {}
    for ts in TS_UNIONS:
        m = re.search(rf"^export type {ts} =((?:[^\n]*\n)(?:\s+\|[^\n]*\n)*)", src, re.M)
        assert m, ts
        out[ts] = tuple(f"'{x}'" for x in re.findall(r"'([^']+)'", m.group(1)))
    return out


def _ts_union(text: str, aliases: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """TS 类型原文 → 联合成员（顶层按「|」拆，具名联合展开成字面量）。这一节的类型里没有带「|」的泛型。"""
    out: list[str] = []
    for member in (m.strip() for m in text.split("|")):
        out.extend(aliases.get(member, (member,)))
    return tuple(out)


def _ts_members(tp: Any) -> tuple[str, ...]:
    """Python 注解 → 应写的 TS 联合成员：str/int/bool/Any → string/number/boolean/unknown，Literal → 带引号的取值，
    X | None → X 的成员加 null，list[X] → X[]，dict[str, X] → Record<string, X>，dataclass → 对应的 TS 接口名。"""
    if tp is type(None):
        return ("null",)
    simple = {str: "string", bool: "boolean", int: "number", Any: "unknown"}
    if tp in simple:
        return (simple[tp],)
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return (TS_NAME[tp.__name__],)
    origin = get_origin(tp)
    if origin is typing.Literal:
        return tuple(f"'{x}'" for x in get_args(tp))
    if origin in (typing.Union, types.UnionType):
        return tuple(m for arg in get_args(tp) for m in _ts_members(arg))
    if origin is list:
        (inner,) = (_ts_members(a) for a in get_args(tp))
        assert len(inner) == 1, tp
        return (f"{inner[0]}[]",)
    if origin is dict:
        key, value = get_args(tp)
        assert key is str, tp
        return (f"Record<string, {_ts_members(value)[0]}>",)
    raise AssertionError(f"契约里出现了没教过的注解：{tp!r}")


TS_NAME = {py: ts for ts, py in TS_INTERFACES.items()}


def test_frontend_types_match_the_contract():
    """字段名、顺序之外，逐个字段比类型：取值联合（含行内的 'override' | 'waiver' 这种）、可空性、数组、嵌套接口。
    合并后 types.ts 归 WP-D，这里是唯一能发现它和服务端契约漂开的地方。"""
    _ts_alignment(TYPES_TS.read_text("utf-8"))


def _ts_alignment(src: str) -> None:
    aliases = _ts_aliases(src)
    for ts, lit in TS_UNIONS.items():
        assert aliases[ts] == tuple(f"'{x}'" for x in get_args(lit)), ts
    for ts, py in TS_INTERFACES.items():
        cls = getattr(P, py)
        got = _ts_field_types(src, ts)
        names = [f.name for f in dataclasses.fields(cls)]
        # 答复里每个键都在（值可以是 null），TS 一律不写成可选
        assert list(got) == names, ts
        hints = typing.get_type_hints(cls)
        for name in names:
            want = TS_OVERRIDES.get((ts, name), _ts_members(hints[name]))
            assert _ts_union(got[name], aliases) == want, (ts, name, got[name], want)
    # 白名单不能烂掉：有意写成非空的两项在 Python 里确实可空；开放尾巴的两项 Python 一侧分别是 str、ColumnRole
    cs_hints = typing.get_type_hints(P.CellSource)
    assert _ts_members(cs_hints["rowid"]) == ("number", "null")
    assert _ts_members(cs_hints["recheck"]) == ("ProvenanceRecheck", "null")
    assert typing.get_type_hints(P.ProvenanceOut)["schema"] is str
    assert get_args(cs_hints["column_role"]) == get_args(P.ColumnRole)
    step = re.search(r"^export interface EvidenceStep .*?^\}", src, re.S | re.M).group(0)
    assert re.search(r"^  provenance\?: boolean$", step, re.M)
    outside = re.search(r"^export interface OutsideText \{.*?^\}", src, re.S | re.M).group(0)
    assert re.search(r"^  hidden\?: boolean \| null$", outside, re.M)


@pytest.mark.parametrize("old,new", [
    # 评审实测过抓不到的两处，加上几种同类的改法：都必须让上面的对齐测试失败
    ("  kind: 'override' | 'waiver'\n", "  kind: 'overrides' | 'waivers'\n"),
    ("  rowid: number\n", "  rowid: string\n"),
    ("  raw_state: 'kept' | 'purged' | 'absent' | null\n", "  raw_state: 'kept' | 'purged' | null\n"),
    ("  mode: 'replace' | 'accumulate'\n", "  mode: 'replace' | 'accumulate' | 'append'\n"),
    ("  source: 'period' | 'human'\n", "  source: 'cells' | 'human'\n"),
    ("  period: ProvenancePeriod | null\n", "  period: ProvenancePeriod\n"),
    ("  title: string | null\n  kind: 'override'", "  title: string\n  kind: 'override'"),
    ("  params: unknown[]\n", "  params: string\n"),
    ("  row_status: ProvenanceRowStatus | null\n", "  row_status: ProvenanceCellStatus | null\n"),
    ("  column_role: 'axis' | 'dim'", "  column_role: 'axis' | 'dims'"),
])
def test_frontend_type_alignment_catches_drift(old, new):
    src = TYPES_TS.read_text("utf-8")
    start = src.index("// 期 4：推断的来源")
    assert old in src[start:], old
    with pytest.raises(AssertionError):
        _ts_alignment(src[:start] + src[start:].replace(old, new, 1))


# ---------------------------------------------------------------------------
# 金样：工件 id 等于内容哈希（被人改过一个字就对不上）
# ---------------------------------------------------------------------------


def test_golden_is_content_addressed():
    from app.core.artifact_store import canonical_json, content_hash

    g = json.loads(GOLDEN.read_text("utf-8"))
    assert "2cdbf9a" in g["_comment"] and g["commit"] == "2cdbf9a"
    assert g["script"].endswith("p4-golden/gen.py")
    for aid, content in g["artifacts"].items():
        assert content_hash(canonical_json(content)) == aid
    assert set(g["reports"]) == {"recipe", "simple", "manual"}
    for rep in g["reports"].values():
        doc = rep["outputs"]["doc"]
        assert content_hash(canonical_json(doc)) == rep["doc_artifact"]
        assert "provenance" not in doc
        assert set(rep["artifacts"]) <= set(g["artifacts"])
        assert rep["outputs"]["chain"] and rep["outputs"]["excerpts"]
