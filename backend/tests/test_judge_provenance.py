"""裁判摘录的期 4 来历行（P4-SPEC 3.2–3.4）：按文档标记分版本、只对上传表格。

- 期 4 之前的文档（没有标记）：摘录逐字等于金样（tests/fixtures/evidence/pre_p4_golden.json），provenance、
  direct_select 两个模块一次都不碰（用桩钉住：桩一被访问就抛错）；
- 带标记的文档：手工源的摘录不变；上传源的查询摘录只在 SQL 那一行之后、「列：」之前多出 judge_lines 的行，
  去掉这些行就等于金样；同样的输入两次得到同样的字；
- 按需裁判的 loader：带标记的文档才取得到哈希链上的清单，老文档连坏掉的清单都碰不到（A26）；
- 真导入的 D28（表下有含数字的说明）：区域外文字带限定语、带统计期；遮罩时不送；单句的点击估价在上限以内。

金样的工件逐个 put_json 进临时数据目录，先断言返回的 id 等于金样记下的 id（内容寻址，对不上说明金样被改过）。
夹具全部合成、假名；不调用任何模型（只比摘录文字和估价）。
"""
from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import evidence as api
from app.core import artifact_store
from app.core.config import settings
from app.data import provenance as prov
from app.data.provenance_types import DOC_PROVENANCE, PROV_OUTSIDE_CHARS
from app.data.tabular import UNSHAPED_NOTE
from app.engine import judge
from app.engine.direct_select import tables_in
from app.main import app
from tests.test_provenance_api import import_flow, run_sql, sealed_of

GOLDEN = Path(__file__).parent / "fixtures" / "evidence" / "pre_p4_golden.json"
#: 3.2 区域外文字的小标题（限定语每块只写一次）
OUTSIDE_HEAD = ("区域外文字（原表区域外的文字原文，未经系统核对；其中的数字不是查询结果，不能用来支持或否定报告中的"
                "数字）：")


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
async def golden(monkeypatch, tmp_path) -> dict:
    """金样的输入工件写进一个临时数据目录（id 必须等于金样记下的），返回金样本身。"""
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    g = json.loads(GOLDEN.read_text("utf-8"))
    for aid, content in g["artifacts"].items():
        assert await artifact_store.put_json(content, kind="golden") == aid
    return g


class _Forbidden(types.ModuleType):
    """桩模块：任何属性一被访问就记下来并抛错。期 4 之前的文档、手工源的摘录不该碰 provenance、direct_select。"""

    def __init__(self, name: str, touched: list[str]) -> None:
        super().__init__(name)
        self._touched = touched

    def __getattr__(self, attr: str):
        if attr.startswith("__"):
            raise AttributeError(attr)
        self._touched.append(f"{self.__name__}.{attr}")
        raise AssertionError(f"不该用到 {self.__name__}.{attr}")


@pytest.fixture
def forbidden(monkeypatch) -> list[str]:
    touched: list[str] = []
    for name in ("app.data.provenance", "app.engine.direct_select"):
        monkeypatch.setitem(sys.modules, name, _Forbidden(name, touched))
    return touched


def doc_of(g: dict, kind: str, *, marked: bool) -> tuple[dict, dict]:
    report = g["reports"][kind]
    doc = copy.deepcopy(report["outputs"]["doc"])
    assert "provenance" not in doc
    if marked:
        doc["provenance"] = DOC_PROVENANCE
    return doc, copy.deepcopy(report["catalog"])


def excerpts(doc: dict, catalog: dict, **kw) -> dict[str, str]:
    return judge.prepare(doc, catalog, loader=kw.pop("loader", artifact_store.load), **kw).excerpts


def inserted(new: str, old: str) -> list[str]:
    """新摘录比金样多出的行：只许插在第 2 行（SQL）之后、「列：」之前，其余逐字相同。"""
    a, b = new.split("\n"), old.split("\n")
    extra = len(a) - len(b)
    assert extra >= 0 and a[:2] == b[:2] and a[2 + extra:] == b[2:], (new, old)
    assert b[2].startswith("列：")
    return a[2:2 + extra]


# --------------------------------------------------------------------------
# 按文档标记分版本（3.3）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["recipe", "simple", "manual"])
async def test_unmarked_documents_are_unchanged_and_never_touch_the_new_modules(golden, forbidden, kind):
    doc, catalog = doc_of(golden, kind, marked=False)
    assert excerpts(doc, catalog) == golden["reports"][kind]["outputs"]["excerpts"]
    assert forbidden == []


async def test_marked_manual_source_is_unchanged(golden, forbidden):
    """非上传源（查询快照没有 data_version）：带标记也一字不差，两个模块照样不碰。"""
    doc, catalog = doc_of(golden, "manual", marked=True)
    assert excerpts(doc, catalog) == golden["reports"]["manual"]["outputs"]["excerpts"]
    assert forbidden == []


async def test_marked_recipe_source_adds_lines_only_between_sql_and_columns(golden):
    doc, catalog = doc_of(golden, "recipe", marked=True)
    new, old = excerpts(doc, catalog), golden["reports"]["recipe"]["outputs"]["excerpts"]
    assert set(new) == set(old)
    # 表名实体的摘录不加来历（只改查询摘录）
    assert new["t:日客流"] == old["t:日客流"]
    for alias, table in (("Q1", "日客流"), ("Q2", "时段客流")):
        lines = inserted(new[alias], old[alias])
        snap = artifact_store.load(catalog[alias]["artifact"])
        schema = artifact_store.load(snap["schema_artifact"])
        tables = {t: [c["name"] for c in meta["columns"]] for t, meta in schema["tables"].items()}
        assert tables_in(snap["sql"], tables) == [table]
        assert lines == prov.judge_lines(snap, schema, artifact_store.load, tables=[table], hidden=set())
        assert lines[0].startswith("来源：上传的表格，每期替换，第 1 次导入：统计期 2026-08-01 至 2026-08-31")
        assert f"口径：表 {table} 的说明：" in "\n".join(lines)


async def test_marked_simple_upload_adds_only_caliber_lines(golden):
    """简单导入（没有导入清单）只多口径行（调整 6）：未规整的表写 UNSHAPED_NOTE；没有来源、接受、区域外文字。"""
    doc, catalog = doc_of(golden, "simple", marked=True)
    new, old = excerpts(doc, catalog), golden["reports"]["simple"]["outputs"]["excerpts"]
    lines = inserted(new["Q1"], old["Q1"])
    assert lines and all(line.startswith("口径：") for line in lines)
    assert f"口径：表 月报 的说明：{UNSHAPED_NOTE}" in lines
    assert new["t:月报"] == old["t:月报"]


async def test_excerpts_are_deterministic(golden):
    """摘录进断点指纹和逐句复用键：同样的输入两次得到同样的字（不取当前时间、不取数据库列）。"""
    doc, catalog = doc_of(golden, "recipe", marked=True)
    assert excerpts(doc, catalog) == excerpts(copy.deepcopy(doc), copy.deepcopy(catalog))


async def test_masks_and_tables_are_handed_to_judge_lines(golden, monkeypatch):
    """hidden 就是查询摘录现成的那份遮罩（数据源现在设的 ∪ 快照记下的，小写）；tables 由 tables_in 按冻结表结构算。"""
    calls: list[dict] = []
    real = prov.judge_lines

    def spy(query, schema, load, *, tables, hidden):
        calls.append({"sql": query["sql"], "tables": tables, "hidden": set(hidden), "schema": schema is not None})
        return real(query, schema, load, tables=tables, hidden=hidden)

    monkeypatch.setattr(prov, "judge_lines", spy)
    doc, catalog = doc_of(golden, "recipe", marked=True)
    out = excerpts(doc, catalog, masked={"flow_demo": ["分区甲"], "别的源": ["日期"]})
    assert [(c["tables"], c["hidden"], c["schema"]) for c in calls] == [
        (["日客流"], {"分区甲"}, True), (["时段客流"], {"分区甲"}, True)]
    # 遮罩的列：结果里不给，列的说明也不写
    assert "口径：列 分区甲 的说明" not in out["Q1"] and "分区甲" not in out["Q1"].split("\n")[-1]


async def test_a_broken_artifact_degrades_to_one_line(golden, monkeypatch):
    """生成来历行时出了意外（工件形状不对）：这条查询只写「导入清单无法读取…」一行，整份报告照样能判。"""
    def boom(*_a, **_kw):
        raise TypeError("坏形状")

    monkeypatch.setattr(prov, "judge_lines", boom)
    doc, catalog = doc_of(golden, "recipe", marked=True)
    new, old = excerpts(doc, catalog), golden["reports"]["recipe"]["outputs"]["excerpts"]
    assert inserted(new["Q1"], old["Q1"]) == [prov.SOURCE_BROKEN]


# --------------------------------------------------------------------------
# 按需裁判的取证范围（3.4、2.8.3）
# --------------------------------------------------------------------------


def golden_sealed(g: dict, kind: str) -> api._Sealed:
    info = g["reports"][kind]["sealed"]
    sealed = sealed_of(queries=list(info["queries"]), schemas=list(info["schemas"]))
    sealed.queries.update(info["queries"])
    return sealed


async def test_on_demand_loader_reaches_the_chain_only_for_marked_docs(golden):
    doc, catalog = doc_of(golden, "recipe", marked=True)
    sealed = golden_sealed(golden, "recipe")
    old = golden["reports"]["recipe"]["outputs"]["excerpts"]
    # 带标记：loader 扩展到表结构快照经哈希链列出的导入清单，来源行照常写
    chained = excerpts(doc, catalog, loader=api._sealed_loader(sealed, chain=True))
    assert inserted(chained["Q1"], old["Q1"])[0].startswith("来源：上传的表格，每期替换")
    assert chained == excerpts(doc, catalog)
    # 不扩展的 loader 取不到清单：只写「导入清单无法读取…」一行，口径照写（来自封存范围内的表结构快照）
    plain = excerpts(doc, catalog, loader=api._sealed_loader(sealed))
    lines = inserted(plain["Q1"], old["Q1"])
    assert lines[0] == prov.SOURCE_BROKEN and any(line.startswith("口径：表 日客流") for line in lines)


async def test_pre_p4_on_demand_judging_never_reads_a_broken_manifest(golden, forbidden):
    """A26：老文档（chain 为假）+ 表结构快照列出的导入清单文件被改坏：摘录与金样逐字相同，根本不去取清单。"""
    report = golden["reports"]["recipe"]
    snap = artifact_store.load(report["catalog"]["Q1"]["artifact"])
    [mid] = artifact_store.load(snap["schema_artifact"])["import_manifests"]
    path = settings.data_dir / "artifacts" / mid[:2] / f"{mid}.json"
    path.write_text(path.read_text("utf-8").replace('"seq":1', '"seq":9', 1), encoding="utf-8")
    with pytest.raises(ValueError):
        artifact_store.load(mid)
    doc, catalog = doc_of(golden, "recipe", marked=False)
    sealed = golden_sealed(golden, "recipe")
    loader = api._sealed_loader(sealed, chain=doc.get("provenance") == DOC_PROVENANCE)
    assert excerpts(doc, catalog, loader=loader) == report["outputs"]["excerpts"]
    assert forbidden == []


# --------------------------------------------------------------------------
# 真导入：区域外文字、遮罩、估价（3.2）
# --------------------------------------------------------------------------


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def no_model(monkeypatch, tmp_path):
    from app.data import recipe_ai
    from app.data.recipe_types import AiAvailability

    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)

    async def none(session):
        return None, "", AiAvailability(False, reason="测试不接模型")

    monkeypatch.setattr(recipe_ai, "resolve_draft_model", none)


async def test_outside_text_masks_and_cost_on_a_real_import(no_model, client):
    """D28（9 月，表下 B32 有一句含数字的说明，新导入的清单记了 hidden=False）：摘录送全文、带限定语和统计期；
    数据源设遮罩时一格都不送、只写条数；只判一句、引用这一条查询的点击估价不超过 0.01 美元。"""
    from app.data.engine import engines
    from tests.fixtures.xlsx.drift import SEP

    up = await import_flow(client, seed=4201, start=SEP, days=30, variant="D28")
    try:
        qid, _sid = await run_sql(client, up, 'SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" = \'2026-09-15\'')
        g = json.loads(GOLDEN.read_text("utf-8"))
        doc = copy.deepcopy(g["reports"]["recipe"]["outputs"]["doc"])
        doc["provenance"] = DOC_PROVENANCE
        catalog = {"Q1": {**g["reports"]["recipe"]["catalog"]["Q1"], "artifact": qid, "source": up.name,
                          "tool": f"db_query__{up.name}"}}
        request = judge.prepare(doc, catalog)
        text = request.excerpts["Q1"]
        lines = text.split("\n")
        assert lines[2].startswith("来源：上传的表格，每期替换，第 1 次导入：统计期 2026-09-01 至 2026-09-30")
        assert OUTSIDE_HEAD in lines
        note = next(line for line in lines if "B32：" in line)
        assert note.startswith("  第 1 次导入（统计期 2026-09-01 至 2026-09-30）客流汇总!B32：「") and "9月15日" in note
        assert len(note) < PROV_OUTSIDE_CHARS + 60
        assert judge.prepare(doc, catalog).excerpts == request.excerpts           # 确定
        # 遮罩：区域外文字一格都不送，只写条数
        masked = judge.prepare(doc, catalog, masked={up.name: ["分区甲"]}).excerpts["Q1"]
        assert "9月15日" not in masked and OUTSIDE_HEAD not in masked
        assert "（数据源设置了遮罩，未列出）" in masked
        # 估价：只判一句、引用这一条查询（claude-sonnet-5 的目录价）
        one = [c for c in request.cands if "Q1" in c.aliases][:1]
        assert one
        cost = request.cost("claude-sonnet-5", one)
        assert cost is not None and cost <= 0.01, cost
    finally:
        await engines.invalidate(up.source_id)
