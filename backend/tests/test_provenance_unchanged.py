"""期 4「期 4 之前不变」的金样断言（P4-SPEC 6.6、1.4，WP-E）。

金样 `tests/fixtures/evidence/pre_p4_golden.json` 由 WP-0 在期 4 改动之前的提交（2cdbf9a）上真跑管线生成，自带三份
报告（配方源、简单导入、手工源）的全部输入工件和四项输出：verify_doc、裁判摘录、_chain（单元格片段的查询步骤）、
文档本身。这里不重跑管线（导入清单里有 created_at，重跑得到的 id 会不同），而是：

1. 把金样里的输入工件逐个 put_json 进本测试的临时数据目录，断言返回的 id 等于金样记下的键（内容寻址：对不上说明
   金样被改过，后面的比较就没有意义）；
2. 用新代码对同样的输入重算，逐字比金样。

为什么要这一层：期 4 的承诺是「期 4 之前的运行、非上传源的面板、复核、裁判摘录一个字都不变」（0.2 补充 1）。
文档标记 `provenance` 是唯一的开关（1.4）：没有它的文档必须走和期 4 之前完全相同的路径，连 provenance、
direct_select 两个模块的函数都不许碰（3.3）；带上它之后，非上传源仍然一字不差，上传源只在约定的位置多出约定的东西
（查询步骤只多 `provenance: true`；摘录只在 SQL 那一行之后、「列：」之前多出来历行）。

金样里的 `_chain` 输出是用预填 memo 的 `_Masks` 替身算的（不查库），这里用同样的替身；`_Sealed` 按金样记下的封存
范围在内存里拼（6.6）。夹具全部合成、假名，没有任何模型调用。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.api import evidence as api
from app.core import artifact_store
from app.core.config import settings
from app.data import provenance as provenance_mod
from app.data.provenance_types import DOC_PROVENANCE, CellRef, contract_problems
from app.data.tabular import UNSHAPED_NOTE
from app.engine import direct_select as direct_select_mod
from app.engine import judge
from app.engine.evidence import iter_segments, verify_doc

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "evidence" / "pre_p4_golden.json"
GOLDEN: dict[str, Any] = json.loads(GOLDEN_PATH.read_text("utf-8"))
REPORTS = ("recipe", "simple", "manual")
#: 上传表格的两份（查询快照有 data_version）：带上标记后查询步骤多 provenance: true、摘录多来历行
UPLOADS = ("recipe", "simple")

#: 来历行（P4-SPEC 3.2）可能的行首：来源、来源的分期行（两个空格缩进）、口径、已接受、区域外文字、截断说明
NEW_LINE_HEADS = ("来源：", "  ", "口径：", "已接受（", "区域外", "另有 ", "…另有部分来历信息未列出")


# ==========================================================================
# 夹具：临时数据目录里重建金样的输入工件
# ==========================================================================


@pytest.fixture(autouse=True)
async def rebuilt(monkeypatch, tmp_path) -> dict[str, str]:
    """每个用例一个独立的数据目录，金样的输入工件和三份文档逐个 put_json 进去，id 必须等于金样的键。

    独立目录的理由：A26 那一例要把导入清单的文件改坏，不能影响别的用例；工件按内容寻址、不带运行 id，放在共用的
    数据目录里会和别的测试串。"""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "data_dir", data)
    ids: dict[str, str] = {}
    for aid, content in GOLDEN["artifacts"].items():
        got = await artifact_store.put_json(copy.deepcopy(content), kind="golden_input")
        assert got == aid, f"金样里的工件 {aid[:12]}… 重建出来的 id 是 {got[:12]}…：金样被改过"
        ids[aid] = got
    for name in REPORTS:
        rep = GOLDEN["reports"][name]
        got = await artifact_store.put_json(copy.deepcopy(rep["outputs"]["doc"]), kind="report_doc")
        assert got == rep["doc_artifact"], f"{name} 的文档重建出来的 id 不对：金样被改过"
    return ids


@pytest.fixture
def tripwire(monkeypatch) -> list[str]:
    """把 provenance、direct_select 两个模块的公开函数全换成一调用就记下来并抛错的桩（P4-SPEC 3.3、6.6）。

    老文档（没有标记）走的路径必须和期 4 之前完全相同，连这两个模块都不许碰：桩被调用一次，用例就失败。函数体内
    `from app.data.provenance import judge_lines` 这种写法每次调用时现取模块属性，同样会撞上桩。"""
    hits: list[str] = []
    patched: list[str] = []
    for mod in (provenance_mod, direct_select_mod):
        for name, obj in vars(mod).items():
            if name.startswith("_") or not callable(obj) or getattr(obj, "__module__", None) != mod.__name__ \
                    or isinstance(obj, type):
                continue

            def boom(*_a: Any, __name: str = f"{mod.__name__}.{name}", **_kw: Any) -> Any:
                hits.append(__name)
                raise AssertionError(f"期 4 之前的文档不该调用 {__name}")

            monkeypatch.setattr(mod, name, boom)
            patched.append(name)
    # 桩真的装上了：入口函数一个不漏（漏了的话「没被调用」就是空话）
    assert {"recognize", "tables_in", "judge_lines", "resolve_chain", "chain_artifacts", "version_view", "locate",
            "related_checks"} <= set(patched), patched
    return hits


# ==========================================================================
# 小工具
# ==========================================================================


def report(name: str) -> dict[str, Any]:
    return GOLDEN["reports"][name]


def old_doc(name: str) -> dict[str, Any]:
    doc = copy.deepcopy(report(name)["outputs"]["doc"])
    assert "provenance" not in doc, "金样里的文档是期 4 之前组装的，不该有 provenance 键"
    return doc


def marked_doc(name: str) -> dict[str, Any]:
    return {**old_doc(name), "provenance": DOC_PROVENANCE}


def sealed_of(name: str) -> api._Sealed:
    """金样记下的封存范围（6.6）：内存里拼，不读运行记录。"""
    s = report(name)["sealed"]
    return api._Sealed(run_id=report(name)["run_id"], graph={}, seal=dict(s["seal"]), events=[],
                       queries={k: dict(v) for k, v in s["queries"].items()},
                       schemas={k: dict(v) for k, v in s["schemas"].items()})


class _StubMasks:
    """金样生成时 `_Masks` 的替身：预填 memo（源名 → (遮罩列小写, 数据源还在)），不查库。"""

    def __init__(self, memo: dict[str, tuple[set[str], bool]]) -> None:
        self._memo = memo

    async def of(self, source: Any) -> tuple[set[str], bool]:
        name = str(source or "")
        if not name:
            return set(), True
        return self._memo.get(name, (set(), True))


def stub_masks(monkeypatch, name: str) -> None:
    memo = {k: ({c.lower() for c in v}, True) for k, v in report(name)["masks"].items()}
    monkeypatch.setattr(api, "_Masks", lambda: _StubMasks(memo))


def cell_segments(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in iter_segments(doc) if (s.get("cite") or {}).get("kind") == "cell"]


async def chains(doc: dict[str, Any], name: str) -> dict[str, list[dict[str, Any]]]:
    sealed = sealed_of(name)
    out = {}
    for seg in cell_segments(doc):
        out[seg["id"]] = await api._chain(seg, doc, sealed, report(name)["node_id"])
    # 金样存的是 JSON：元组、浮点数都按 JSON 往返一次再比
    return json.loads(json.dumps(out, ensure_ascii=False))


def excerpts(doc: dict[str, Any], name: str, *, loader: Any = None, masked: Any = None) -> dict[str, str]:
    return judge.prepare(doc, report(name)["catalog"], loader=loader or artifact_store.load, masked=masked).excerpts


def verified(doc: dict[str, Any], name: str) -> dict[str, Any]:
    v = verify_doc(doc, report(name)["catalog"], loader=artifact_store.load)
    return {"ok": v["ok"], "stats": v["stats"], "violations": v["violations"]}


def state_mismatches(v: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for x in v["violations"] if x.get("code") == "state_mismatch"]


def split_new_lines(text: str) -> tuple[list[str], list[str]]:
    """查询摘录 → (去掉来历行之后的行, 来历行)。来历行只许出现在「SQL：」那一行之后、「列：」那一行之前（3.2）。"""
    lines = text.split("\n")
    sql = [i for i, line in enumerate(lines) if line.startswith("SQL：")]
    cols = [i for i, line in enumerate(lines) if line.startswith("列：")]
    assert len(sql) == 1 and len(cols) == 1 and sql[0] < cols[0], f"查询摘录的形状变了：\n{text}"
    return lines[:sql[0] + 1] + lines[cols[0]:], lines[sql[0] + 1:cols[0]]


def golden_tables(schema: dict[str, Any]) -> dict[str, list[str]]:
    """冻结表结构 → {表: [列…]}（按定义顺序），direct_select.tables_in 的入参（3.3）。"""
    return {name: [str(c.get("name")) for c in (meta.get("columns") or [])]
            for name, meta in (schema.get("tables") or {}).items()}


def expected_judge_lines(name: str, alias: str) -> list[str]:
    """契约（3.3）：_query_excerpt 先用 tables_in 算出 SQL 用到的表，再调 judge_lines，把返回的行原样插进摘录。
    金样的报告没有遮罩（masks 为空、快照没有 mask_columns），hidden 为空集。"""
    entry = report(name)["catalog"][alias]
    query = artifact_store.load(entry["artifact"])
    schema = artifact_store.load(query["schema_artifact"]) if query.get("schema_artifact") else None
    tables = direct_select_mod.tables_in(query["sql"], golden_tables(schema or {}))
    return provenance_mod.judge_lines(query, schema, artifact_store.load, tables=tables, hidden=set())


# ==========================================================================
# 金样本身
# ==========================================================================


def test_golden_records_three_kinds_of_sources(rebuilt):
    """金样的三份报告正好是规格要的三种源（6.6）：配方源有 import_manifests、简单导入的表结构快照没有 import_mode、
    手工源的查询快照没有 data_version。重建出的工件 id 全部等于金样的键（夹具里已逐个断言）。"""
    arts = GOLDEN["artifacts"]
    assert set(rebuilt) == set(arts) and all(k == v for k, v in rebuilt.items())
    assert set(GOLDEN["reports"]) == set(REPORTS)
    rq = arts[report("recipe")["artifacts"][0]]
    rs = arts[rq["schema_artifact"]]
    assert rq.get("data_version") and rs.get("import_mode") == "recipe" and len(rs["import_manifests"]) == 1
    sq = arts[report("simple")["artifacts"][0]]
    ss = arts[sq["schema_artifact"]]
    assert sq.get("data_version") and "import_mode" not in ss and ss["tables"]["月报"]["comment"] == UNSHAPED_NOTE
    mq = arts[report("manual")["artifacts"][0]]
    assert "data_version" not in mq and "schema_artifact" not in mq
    for name in REPORTS:
        assert cell_segments(old_doc(name)), name
        assert report(name)["outputs"]["excerpts"], name
        assert report(name)["outputs"]["chain"], name


# ==========================================================================
# 期 4 之前的文档（没有标记）：一个字都不变，也不碰新模块
# ==========================================================================


@pytest.mark.parametrize("name", REPORTS)
def test_old_documents_verify_exactly_as_before(name, tripwire):
    """① verify_doc 逐字等于金样，没有 state_mismatch（verify_doc 不看未知的顶层键，也不读裁判摘录，1.4）。"""
    got = verified(old_doc(name), name)
    assert got == report(name)["outputs"]["verify_doc"]
    assert state_mismatches(got) == []
    assert tripwire == []


@pytest.mark.parametrize("name", REPORTS)
def test_old_documents_give_the_same_judge_excerpts(name, tripwire):
    """② 裁判摘录逐字等于金样（摘录进断点指纹和逐句复用键，变一个字已判过的句子就不复用了，3.1）。"""
    assert excerpts(old_doc(name), name) == report(name)["outputs"]["excerpts"]
    assert tripwire == []


@pytest.mark.parametrize("name", REPORTS)
async def test_old_documents_give_the_same_query_steps(name, monkeypatch, tripwire):
    """③ 单元格片段的出处链（查询步骤）逐字等于金样：没有 provenance 键，界面不多发请求（2.8.2）。"""
    stub_masks(monkeypatch, name)
    got = await chains(old_doc(name), name)
    assert got == report(name)["outputs"]["chain"]
    assert not any("provenance" in step for steps in got.values() for step in steps)
    assert tripwire == []


@pytest.mark.parametrize("name", REPORTS)
async def test_old_documents_answer_legacy_doc(name, tripwire):
    """推断来源接口对老文档一律回 none · legacy_doc（R0），不标红、没有数据版本；cell 照样给（与 status 无关，2.8.1）。
    R0 在一切取证之前判，所以连查询快照都不用取，也不碰新模块。"""
    doc = old_doc(name)
    rep = report(name)
    chosen = api._Report(node_id=rep["node_id"], doc_artifact=rep["doc_artifact"], ok=True, repairs=0, stats=None,
                         doc=doc, hash_ok=True, fields=[])
    for seg in cell_segments(doc):
        out = await api.segment_provenance(chosen, seg, sealed_of(name), masks=_StubMasks({}))
        assert contract_problems(out) == []
        assert (out.status, out.reason.code, out.alert, out.version, out.cell_source, out.checks) == \
            ("none", "legacy_doc", None, None, None, [])
        assert out.sealed is True and out.segment == seg["id"]
        loc = seg["cite"]["locator"]
        assert out.cell == CellRef(alias=seg["cite"]["alias"], row=loc["row"], column=loc["column"],
                                   artifact=rep["catalog"][seg["cite"]["alias"]]["artifact"])
        assert out.report.node_id == rep["node_id"] and out.report.doc_artifact == rep["doc_artifact"]
    assert tripwire == []


async def test_old_document_on_demand_judge_ignores_a_broken_manifest(tripwire):
    """A26（金样一侧）：老文档走按需裁判的 loader（`_sealed_loader`，chain 按文档标记取，老文档为假），把表结构快照
    列出的导入清单文件改坏一个字节，摘录仍逐字等于金样：chain 为假时根本不去取清单（2.8.3）。"""
    name = "recipe"
    doc = old_doc(name)
    rep = report(name)
    schema = artifact_store.load(artifact_store.load(rep["artifacts"][0])["schema_artifact"])
    [manifest] = schema["import_manifests"]
    path = artifact_store._path_of(manifest)
    raw = path.read_bytes()
    at = raw.index(b'"seq":')
    path.write_bytes(raw[:at] + b'"seQ":' + raw[at + 6:])
    with pytest.raises(ValueError):
        artifact_store.load(manifest)
    # _judge_now 的同一套取法：loader 按文档标记决定扩不扩展、遮罩按这个 loader 查
    loader = api._sealed_loader(sealed_of(name), chain=doc.get("provenance") == DOC_PROVENANCE)
    assert loader(manifest) is None
    masked = await judge.source_masks(rep["catalog"], loader=loader)
    assert not any(masked.values()), f"金样的数据源不该设遮罩：{masked}"
    assert excerpts(doc, name, loader=loader, masked=masked) == rep["outputs"]["excerpts"]
    assert tripwire == []


# ==========================================================================
# 带上标记之后：非上传源一字不差；上传源只在约定的位置多出约定的东西
# ==========================================================================


async def test_marked_manual_document_is_unchanged(monkeypatch):
    """手工源（查询快照没有 data_version）的文档加上标记：①②③仍逐字等于金样（1.4「非上传源」，3.3）。"""
    name = "manual"
    doc = marked_doc(name)
    out = report(name)["outputs"]
    assert verified(doc, name) == out["verify_doc"]
    assert excerpts(doc, name) == out["excerpts"]
    stub_masks(monkeypatch, name)
    assert await chains(doc, name) == out["chain"]


@pytest.mark.parametrize("name", UPLOADS)
def test_marked_upload_documents_still_verify_the_same(name):
    """标记写在文档顶层、参与内容哈希，但 verify_doc 不看它：ok、stats、violations 与金样逐字相同，没有 state_mismatch。"""
    got = verified(marked_doc(name), name)
    assert got == report(name)["outputs"]["verify_doc"] and state_mismatches(got) == []


@pytest.mark.parametrize("name", UPLOADS)
async def test_marked_upload_query_steps_only_gain_the_hint(name, monkeypatch):
    """③ 上传表格的单元格片段：查询步骤只多一个 `provenance: true`，其余每个键逐字等于金样（2.8.2）。"""
    stub_masks(monkeypatch, name)
    got = await chains(marked_doc(name), name)
    want = report(name)["outputs"]["chain"]
    assert set(got) == set(want)
    for sid, steps in got.items():
        assert len(steps) == len(want[sid]) == 1, sid
        for step, old in zip(steps, want[sid]):
            assert set(step) - set(old) == {"provenance"} and set(old) <= set(step), (sid, set(step) ^ set(old))
            assert step["provenance"] is True
            assert {k: v for k, v in step.items() if k != "provenance"} == old, sid


@pytest.mark.parametrize("name", UPLOADS)
def test_marked_upload_excerpts_keep_the_golden_text_around_the_new_lines(name):
    """② 上传表格加上标记后，摘录里新增的东西只许插在查询摘录「SQL：」那一行之后、「列：」那一行之前：去掉这段之后
    逐字等于金样；表名实体等其余摘录逐字等于金样（3.2 的位置约定）。新行一律以 3.2 的几种行首开头。"""
    got = excerpts(marked_doc(name), name)
    want = report(name)["outputs"]["excerpts"]
    assert set(got) == set(want)
    for alias, text in got.items():
        if not alias.startswith("Q"):
            assert text == want[alias], alias
            continue
        kept, new = split_new_lines(text)
        golden_kept, golden_new = split_new_lines(want[alias])
        assert golden_new == [], "金样的查询摘录在 SQL 与「列：」之间不该有别的行"
        assert kept == golden_kept, alias
        assert all(line.startswith(NEW_LINE_HEADS) for line in new), (alias, new)


def test_marked_recipe_excerpts_gain_the_provenance_lines():
    """② 配方源的文档加上标记：两条查询摘录都多出 3.2 的来历行，内容就是 judge_lines 对同样输入给出的行，原样插在
    「SQL：」与「列：」之间（3.3）。断言几样确定的内容：

    - 来源行：上传的表格、每期替换、第 1 次导入、统计期取自 客流汇总!B2、文件名与 sha256 前 12 位、区域（S9）；
    - 口径：SQL 用到的表的说明（冻结表结构的 comment 原文）、查询结果列的说明；
    - 区域外文字：金样的清单是期 4 之前导入的，OutsideText 没有 hidden 字段，只计条数、不送全文（调整 10）；
    - 没有接受理由（D00 没有接受）。"""
    name = "recipe"
    got = excerpts(marked_doc(name), name)
    rep = report(name)
    query = artifact_store.load(rep["catalog"]["Q1"]["artifact"])
    schema = artifact_store.load(query["schema_artifact"])
    manifest = artifact_store.load(schema["import_manifests"][0])
    for alias, table in (("Q1", "日客流"), ("Q2", "时段客流")):
        _, new = split_new_lines(got[alias])
        assert new, f"{alias} 的摘录没有来历行"
        assert new == expected_judge_lines(name, alias), alias
        source = new[0]
        assert source.startswith("来源：上传的表格，每期替换，第 1 次导入：统计期 2026-08-01 至 2026-08-31（取自 客流汇总!B2）")
        assert f"文件「{manifest['file']['name']}」（sha256 {manifest['file']['raw_sha256'][:12]}）" in source
        assert "区域 客流汇总!" in source
        comment = schema["tables"][table]["comment"]
        assert f"口径：表 {table} 的说明：{comment}" in new
        assert not any(line.startswith("已接受") for line in new)
        assert "另有 1 处区域外文字未列出（所在行列被隐藏，或导入时未记录是否隐藏），见证据面板" in new
        assert not any("客流汇总表" in line for line in new), "隐藏与否未记录的区域外文字不该送全文"
    _, new = split_new_lines(got["Q1"])
    for col in ("日期", "全日客流", "分区甲"):
        note = next(c["comment"] for c in schema["tables"]["日客流"]["columns"] if c["name"] == col)
        assert f"口径：列 {col} 的说明：{note}" in new
    assert len("\n".join(new)) <= 1200


def test_marked_simple_upload_excerpt_gains_only_the_caliber_line():
    """② 简单导入的文档加上标记：只多口径行（调整 6：未规整的表写 UNSHAPED_NOTE，裁判才知道不能直接对列求和）；
    没有导入清单，来源、已接受、区域外文字三类都不出。"""
    name = "simple"
    got = excerpts(marked_doc(name), name)
    _, new = split_new_lines(got["Q1"])
    assert new == expected_judge_lines(name, "Q1")
    assert f"口径：表 月报 的说明：{UNSHAPED_NOTE}" in new
    assert not any(line.startswith(("来源：", "已接受", "区域外")) for line in new), new
