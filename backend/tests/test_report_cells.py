"""报告直接引用查询快照：单元格 [[v:Q1.r0.amount]] 和整表 [[table:Q1 cols=a,b rows=0-4]]。

一期的报告只能引用口径卡的指标，查询结果只能挂在 [[see:]] 里当依据。问数据页那种
「查一下各区域销售额」的答案，数字全在查询结果里，没有口径卡可引——要么硬塞一张只做
搬运的口径卡，要么模型照抄数字、出处又断了。二期让报告直接引用快照里的那一格：

- 值一律从快照取（取回时复验哈希），按规则渲染；模型只写位置
- 行越界、列不存在、快照取不回来、被改过，都判解析不了，原因写清楚
- 整表由系统从快照生成，每一格都是带出处的片段
- 沙箱代码节点的产出不能直接引用：那种数要进口径卡
- 受管出具没声明 cells 时，复核可以把单元格引用判成解析不了
- 流式渲染和整篇渲染逐字一致
"""
from __future__ import annotations

import copy
import random

import pytest

from app.core import artifact_store
from app.engine.evidence import (
    CELLS_REASON,
    CODE_REASON,
    StreamRenderer,
    build_catalog,
    catalog_prompt,
    compose_doc,
    iter_segments,
    make_eid,
    render_markers,
    resolve_ref,
    verify_doc,
)

# --------------------------------------------------------------------------
# 夹具：一份查询快照（形状同数据源工具落的 query_snapshot），外加一张口径卡
# --------------------------------------------------------------------------

SNAP = {
    "columns": ["region", "amount", "orders", "note"],
    "rows": [
        ["华东", 18230.5, 120, "新客多"],
        ["华南", 12004.0, 98, None],
        ["华北", "9001.25", 77, "含|竖线"],   # DECIMAL 列经 JSON 落盘后是字符串
        ["西南", 0, 0, "多行\n备注"],
        ["西北", -12.5, 3, "x"],
        ["东北", 1, 1, "y"],
        ["中部", 2, 2, "z"],
    ],
    "row_count": 7, "truncated": False, "elapsed_ms": 3,
    "sql": "SELECT region, SUM(amount) AS amount, COUNT(*) AS orders, MAX(note) AS note FROM orders GROUP BY region",
    "source": "shop",
}
# 列名本身带数字：系统生成的表头不能被判成裸数字
DIGITS = {"columns": ["销售额2025", "周"], "rows": [[1.5, "2026-W37"]], "row_count": 1,
          "truncated": False, "elapsed_ms": 1, "sql": "SELECT 1", "source": "shop"}
WIDE = {"columns": [f"c{i}" for i in range(13)], "rows": [list(range(13))], "row_count": 1,
        "truncated": False, "elapsed_ms": 1, "sql": "SELECT 1", "source": "shop"}
EMPTY = {"columns": ["amount"], "rows": [], "row_count": 0, "truncated": False, "elapsed_ms": 1,
         "sql": "SELECT 1", "source": "shop"}

TOOL_SNAP = "b" * 64
GONE = "f" * 64          # 台账里有、工件库里没有的快照
CARD_ART = "c" * 64
CARD = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v2", "artifact": CARD_ART,
        "metrics": [{"id": "gmv", "name": "销售额", "unit": "元", "value": 45678.5, "decimals": 1,
                     "format": "thousands", "status": "ok", "expression": "…"}]}


def _query(artifact: str, snap: dict, node: str = "fetch", call: str = "call_1") -> dict:
    return {"kind": "query", "node_id": node, "exec": 1, "artifact": artifact, "via": TOOL_SNAP,
            "call_id": call, "tool": "db_query__shop", "source": "shop", "columns": snap["columns"],
            "rows": len(snap["rows"]), "truncated": False}


def _store(snap: dict) -> str:
    """按 put_json 的落盘方式写进（测试用的临时）工件库：取回走真实的 load，复验哈希。"""
    text = artifact_store.canonical_json(snap)
    artifact = artifact_store.content_hash(text)
    path = artifact_store._path_of(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return artifact


@pytest.fixture(scope="module")
def stored() -> dict[str, str]:
    return {name: _store(snap) for name, snap in (("snap", SNAP), ("digits", DIGITS), ("wide", WIDE), ("empty", EMPTY))}


@pytest.fixture
def catalog(stored):
    ledger = [
        {"kind": "metric_set", "node_id": "caliber", "exec": 1, "artifact": CARD_ART, "caliber": "周报口径",
         "version": "v2", "metrics": ["gmv"]},
        _query(stored["snap"], SNAP),                                   # Q1
        {"kind": "retrieval", "node_id": "kb", "exec": 1, "artifact": "d" * 64, "source": "docs", "rows": 3},
        _query(GONE, SNAP, call="call_2"),                              # Q2：快照取不回来
        _query(stored["digits"], DIGITS, call="call_3"),                # Q3
        _query(stored["wide"], WIDE, call="call_4"),                    # Q4
        _query(stored["empty"], EMPTY, call="call_5"),                  # Q5
        {"kind": "node_output", "node_id": "calc", "exec": 1, "artifact": "e" * 64, "code_sha": "0" * 64,
         "role": "compute", "language": "python"},
    ]
    return build_catalog(nodes={"caliber": CARD}, ledger=ledger, inputs={"week": "2026-W37"})


def cell_eid(artifact, row, column):
    return make_eid("cell", artifact, {"row": row, "column": column})


def segs(doc, kind=None):
    return [s for s in iter_segments(doc) if kind is None or s["kind"] == kind]


def bad(doc):
    return {v["ref"]: v["message"] for v in doc["violations"] if v["code"] == "unresolved_ref"}


# --------------------------------------------------------------------------
# 单元格
# --------------------------------------------------------------------------


def test_cell_values_come_from_the_snapshot(catalog, stored):
    doc = compose_doc("[[v:Q1.r0.region]]销售额 [[v:Q1.r0.amount]]，订单 [[v:Q1.r0.orders]]。[[see:Q1]]",
                      catalog)
    assert doc["markdown"] == "华东销售额 18,230.5，订单 120。"
    assert doc["violations"] == []
    region, amount, orders = [s for s in segs(doc) if s.get("ref")]
    assert region["kind"] == "value" and amount["kind"] == "number" and orders["kind"] == "number"
    cite = amount["cite"]
    assert cite["status"] == "resolved" and cite["kind"] == "cell" and cite["role"] == "value"
    assert cite["alias"] == "Q1" and cite["ref"] == "Q1.r0.amount"
    assert cite["locator"] == {"row": 0, "column": "amount"} and cite["value"] == 18230.5
    assert cite["eid"] == cell_eid(stored["snap"], 0, "amount")
    assert amount["state"] == "deterministic" and amount["ref"] == "v:Q1.r0.amount"
    assert doc["stats"]["numbers_cited"] == 2 and doc["stats"]["values"] == 1
    assert doc["blocks"][0]["units"][0]["cites"] == ["Q1"]


@pytest.mark.parametrize("marker, shown, value", [
    ("[[v:Q1.r2.amount]]", "9,001.25", 9001.25),        # DECIMAL 字符串按数渲染
    ("[[v:Q1.r1.note]]", "—", None),                    # NULL 就是「—」，不是 0
    ("[[v:Q1.r2.note]]", "含¦竖线", "含|竖线"),           # 竖线换掉，免得把表格切出一格
    ("[[v:Q1.r3.note]]", "多行 备注", "多行\n备注"),       # 文本压成一行
    ("[[v:Q1.r3.amount]]", "0", 0),
    ("[[v:Q1.r4.amount]]", "-12.5", -12.5),
    ("[[v:Q1.r0.amount|万]]", "1.82万", 18230.5),        # 换算由渲染器完成
])
def test_cell_rendering_rules(catalog, marker, shown, value):
    doc = compose_doc(f"值 {marker}", catalog)
    assert doc["violations"] == [], doc["violations"]
    [seg] = [s for s in segs(doc) if s.get("ref")]
    assert seg["text"] == shown and seg["cite"]["value"] == value


def test_integer_decimal_text_renders_as_a_number():
    """小数位为 0 的 DECIMAL（整数列的 SUM()）落盘后是没有小数点的 "45678"：照样按数渲染、按数引用。"""
    snap = {"columns": ["gmv", "refunds", "code", "tiny"], "rows": [["45678", "29", "00123", "1E-7"]],
            "row_count": 1, "truncated": False, "elapsed_ms": 1, "sql": "SELECT 1", "source": "shop"}
    catalog = build_catalog(nodes={}, ledger=[_query(_store(snap), snap)])
    doc = compose_doc("销售额 [[v:Q1.r0.gmv]]（[[v:Q1.r0.gmv|万]]），退款 [[v:Q1.r0.refunds]] 单，"
                      "编号 [[v:Q1.r0.code]]，极小值 [[v:Q1.r0.tiny]]。", catalog)
    assert doc["violations"] == [], doc["violations"]
    assert doc["markdown"] == "销售额 45,678（4.57万），退款 29 单，编号 00123，极小值 0.0000001。"
    gmv, wan, refunds, code, tiny = [s for s in segs(doc) if s.get("ref")]
    assert gmv["kind"] == "number" and gmv["cite"]["value"] == 45678 and isinstance(gmv["cite"]["value"], int)
    assert refunds["kind"] == "number" and refunds["cite"]["value"] == 29
    assert code["kind"] == "value" and code["cite"]["value"] == "00123"      # 带前导零的是编号
    assert tiny["kind"] == "number" and tiny["cite"]["value"] == 1e-7
    assert "gmv=45,678" in catalog_prompt(catalog)


@pytest.mark.parametrize("marker, words", [
    ("[[v:Q1.r7.amount]]", "只有 7 行"),
    ("[[v:Q1.r0.gmv]]", "没有列「gmv」"),
    ("[[v:Q9.r0.amount]]", "查询结果 Q9 不存在"),
    ("[[v:K1.r0.amount]]", "不是查询结果"),
    ("[[v:Q1.amount]]", "单元格引用格式无法识别"),
    ("[[v:Q2.r0.amount]]", "无法读取"),
    ("[[v:Q1.r0.region|万]]", "不是数"),
    ("[[v:N:calc.total]]", CODE_REASON),
    ("[[v:N:fetch.total]]", "不支持引用节点字段"),
])
def test_unresolvable_cells_say_why(catalog, marker, words):
    doc = compose_doc(f"值 {marker}。", catalog)
    [(ref, message)] = bad(doc).items()
    assert ref == marker[2:-2] and words in message, message
    [seg] = [s for s in segs(doc) if s.get("ref")]
    assert seg["state"] == "none" and seg["issue"] == "unresolved_ref" and seg["text"].startswith("⟦?v:")


def test_reasons_for_people_and_rewrite_instructions_for_the_writer_are_separate(catalog):
    """解析不了的原因给人看（证据面板、出具横幅、节点报错），写法指令只交回写作者（模型）改写：
    「单元格要写成 Q<编号>.r<行>.<列>」这类话不上界面，也不存进文档。"""
    from app.engine.evidence import describe_violations

    doc = compose_doc("值 [[v:Q1.amount]]，另见 [[v:Q9.r0.amount]]。", catalog)
    by_ref = {v["ref"]: v for v in doc["violations"] if v["code"] == "unresolved_ref"}
    shape, missing = by_ref["v:Q1.amount"], by_ref["v:Q9.r0.amount"]
    assert shape["message"] == "引用 [[v:Q1.amount]] 无法解析：单元格引用格式无法识别"
    assert "Q<编号>.r<行>.<列>" in shape["for_model"] and "Q<编号>" not in shape["message"]
    assert "查询结果 Q9 不存在" in missing["message"] and "证据目录" in missing["for_model"]
    for seg in segs(doc):
        assert "for_model" not in (seg.get("cite") or {}), "文档里只存给人看的原因"
        assert "Q<编号>" not in str((seg.get("cite") or {}).get("reason") or "")
    written = describe_violations(doc["violations"])
    assert "Q<编号>.r<行>.<列>" in written and "目录里没有 Q9" in written and "单元格引用格式无法识别" not in written


def test_tampered_snapshot_is_refused(stored, tmp_path):
    """取回时复验哈希：工件文件被改过，单元格就解析不了，而不是把改过的数当真。"""
    path = artifact_store._path_of(stored["snap"])
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(original.replace("18230.5", "99999.5"), encoding="utf-8")
        catalog = build_catalog(nodes={}, ledger=[_query(stored["snap"], SNAP)])
        doc = compose_doc("值 [[v:Q1.r0.amount]]", catalog)
        assert "与哈希不一致" in bad(doc)["v:Q1.r0.amount"]
        assert "99,999.5" not in doc["markdown"]
    finally:
        path.write_text(original, encoding="utf-8")


def test_code_node_outputs_are_not_citable(catalog):
    """沙箱算出来的数不在目录里给编号，也不能按节点字段引用：要进口径卡。"""
    assert catalog["N:calc"]["kind"] == "node_output" and catalog["N:calc"]["code"] is True
    assert not any(e.get("node_id") == "calc" for a, e in catalog.items() if a.startswith("Q"))
    prompt = catalog_prompt(catalog)
    assert "N:calc" not in prompt


# --------------------------------------------------------------------------
# 整表
# --------------------------------------------------------------------------


def test_table_marker_expands_into_a_table_of_cited_cells(catalog, stored):
    doc = compose_doc("各区域如下：\n\n[[table:Q1 cols=region,amount rows=0-2]]\n\n华东最高。", catalog)
    assert doc["violations"] == [], doc["violations"]
    assert doc["markdown"] == ("各区域如下：\n\n| region | amount |\n| --- | --- |\n| 华东 | 18,230.5 |\n"
                               "| 华南 | 12,004 |\n| 华北 | 9,001.25 |\n\n华东最高。")
    assert [b["type"] for b in doc["blocks"]] == ["paragraph", "table", "paragraph"]
    table = doc["blocks"][1]
    header = [u for u in table["units"] if u["loc"]["row"] == -1]
    cells = [u for u in table["units"] if u["loc"]["row"] >= 0]
    assert len(header) == 2 and len(cells) == 6
    for unit in cells:
        [seg] = [s for s in unit["segments"] if s["kind"] != "structural"]
        row, col = unit["loc"]["row"], ["region", "amount"][unit["loc"]["col"]]
        assert seg["ref"] == f"v:Q1.r{row}.{col}" and seg["state"] == "deterministic"
        assert seg["cite"]["eid"] == cell_eid(stored["snap"], row, col)
        assert unit["cites"] == ["Q1"]
    # 模型的原文里只有那一个标记，展开是系统做的
    assert doc["source"].count("[[") == 1


def test_table_defaults_to_the_first_five_rows_and_all_columns(catalog):
    doc = compose_doc("[[table:Q1]]", catalog)
    assert doc["violations"] == []
    table = doc["blocks"][0]
    assert {u["loc"]["row"] for u in table["units"]} == {-1, 0, 1, 2, 3, 4}
    assert {u["loc"]["col"] for u in table["units"]} == {0, 1, 2, 3}


@pytest.mark.parametrize("marker, words", [
    ("[[table:Q1 rows=0-25]]", "最多 20 行"),
    ("[[table:Q1 rows=5-9]]", "只有 7 行"),
    ("[[table:Q1 rows=3-1]]", "rows"),
    ("[[table:Q1 cols=region,gmv]]", "没有列「gmv」"),
    ("[[table:Q1 limit=3]]", "无法识别的选项"),
    ("[[table:Q4]]", "超过整表上限"),
    ("[[table:Q5]]", "0 行"),
    ("[[table:Q2]]", "无法读取"),
    ("[[table:K1]]", "不是查询结果"),
])
def test_table_problems_are_violations(catalog, marker, words):
    doc = compose_doc(f"见下表：\n\n{marker}", catalog)
    [(ref, message)] = bad(doc).items()
    assert ref == marker[2:-2] and words in message, message
    assert "|" not in doc["markdown"]


def test_table_written_inline_still_becomes_a_table(catalog):
    doc = compose_doc("见表 [[table:Q1 cols=region rows=0-1]] 如上。", catalog)
    assert doc["violations"] == []
    assert doc["markdown"] == "见表 \n| region |\n| --- |\n| 华东 |\n| 华南 |\n 如上。"
    assert [b["type"] for b in doc["blocks"]] == ["paragraph", "table", "paragraph"]


def test_generated_header_digits_are_not_bare_numbers(catalog):
    doc = compose_doc("[[table:Q3]]", catalog)
    assert doc["violations"] == [], doc["violations"]
    # 同样的字写在正文里照样要出处
    doc = compose_doc("销售额2025 是多少", catalog)
    assert [v["code"] for v in doc["violations"]] == ["uncited_number"]


# --------------------------------------------------------------------------
# 复核
# --------------------------------------------------------------------------


def test_verify_doc_rechecks_cells_independently(catalog):
    doc = compose_doc("华东 [[v:Q1.r0.amount]]。\n\n[[table:Q1 cols=amount rows=0-1]]", catalog)
    again = verify_doc(copy.deepcopy(doc), catalog)
    assert again["violations"] == doc["violations"] == [] and again["stats"] == doc["stats"]
    forged = copy.deepcopy(doc)
    seg = next(s for s in iter_segments(forged) if s.get("ref") == "v:Q1.r0.amount")
    seg["cite"]["eid"] = cell_eid("0" * 64, 0, "amount")
    assert "eid_mismatch" in [v["code"] for v in verify_doc(forged, catalog)["violations"]]


def test_cells_can_be_refused_by_the_checker(catalog):
    """受管出具没声明 cells：复核按参数把每一个单元格引用（含整表里的格）判成解析不了。"""
    doc = compose_doc("华东 [[v:Q1.r0.amount]]，指标 [[m:gmv]]。\n\n[[table:Q1 cols=amount rows=0-1]]", catalog)
    assert doc["violations"] == []
    checked = verify_doc(doc, catalog, cells_allowed=False)
    refused = [v for v in checked["violations"] if v["code"] == "unresolved_ref"]
    assert [v["ref"] for v in refused] == ["v:Q1.r0.amount", "v:Q1.r0.amount", "v:Q1.r1.amount"]
    assert all(CELLS_REASON in v["message"] for v in refused)
    assert resolve_ref("m:gmv", catalog, cells_allowed=False)["status"] == "resolved"
    # 写的时候就不许：整表标记整个解析不了，不展开
    early = compose_doc("[[table:Q1 cols=amount rows=0-1]] 和 [[v:Q1.r0.amount]]", catalog, cells_allowed=False)
    assert set(bad(early)) == {"table:Q1 cols=amount rows=0-1", "v:Q1.r0.amount"}
    assert all(CELLS_REASON in m for m in bad(early).values())


# --------------------------------------------------------------------------
# 流式 = 整篇
# --------------------------------------------------------------------------

STREAMED = [
    "各区域如下：\n\n[[table:Q1 cols=region,amount rows=0-2]]\n\n华东 [[v:Q1.r0.amount]] 最高。",
    "见表 [[table:Q1 cols=region rows=0-1]] 如上。",
    "[[table:Q1]]",
    "开头[[table:Q1 rows=0-0]]",
    "[[table:Q1 rows=0-0]]结尾",
    "[[[table:Q1 rows=0-0]]]]",
    "两张\n[[table:Q1 cols=region rows=0-1]][[table:Q3]]\n完",
    "坏的 [[table:Q1 rows=0-30]] 和 [[v:Q1.r9.amount]]，好的 [[v:Q1.r2.amount|万]]。",
    "**加粗 [[v:Q1.r0.amount]]** 与 [[m:gmv]]",
]


def _chunks(text: str, rng: random.Random) -> list[str]:
    out, i = [], 0
    while i < len(text):
        step = rng.randint(1, 7)
        out.append(text[i:i + step])
        i += step
    return out


@pytest.mark.parametrize("text", STREAMED)
def test_streaming_equals_full_rendering(catalog, text):
    full = render_markers(text, catalog)
    rng = random.Random(len(text))
    for _ in range(60):
        renderer = StreamRenderer(catalog)
        streamed = "".join(renderer.feed(c) for c in _chunks(text, rng)) + renderer.flush()
        assert streamed == full
    # 一个字一个字地喂也一样
    renderer = StreamRenderer(catalog)
    assert "".join(renderer.feed(c) for c in text) + renderer.flush() == full
    # 不含粗体的，流里看到的就是最终文档的正文
    if "**" not in text:
        assert compose_doc(text, catalog)["markdown"] == full.strip()


def test_streaming_with_cells_refused_matches_full(catalog):
    text = STREAMED[0]
    full = render_markers(text, catalog, cells_allowed=False)
    renderer = StreamRenderer(catalog, cells_allowed=False)
    assert "".join(renderer.feed(c) for c in text) + renderer.flush() == full
    assert "⟦?table:Q1" in full and "⟦?v:Q1.r0.amount⟧" in full


# --------------------------------------------------------------------------
# 性质：带单元格和整表的任意原文，片段铺满正文，组装和复核一致
# --------------------------------------------------------------------------

INTEGRITY = {"segment_mismatch", "structural_text", "state_mismatch", "render_mismatch", "eid_mismatch", "bad_schema"}


def _corpus(n: int) -> list[str]:
    rng = random.Random(20260928)
    alphabet = [*"我们。！？\n- *#>|`[]:0123 .,", "[[v:Q1.r0.amount]]", "[[v:Q1.r1.note]]", "[[v:Q1.r9.x]]",
                "[[table:Q1 cols=region rows=0-1]]", "[[table:Q3]]", "[[table:Q1 rows=0-40]]", "[[m:gmv]]",
                "**", "\n\n", "| a |", "[[see:Q1]]"]
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30))) for _ in range(n)]


@pytest.mark.parametrize("raw", _corpus(250))
def test_composition_invariants_with_cells(catalog, raw):
    doc = compose_doc(raw, catalog)
    assert "".join(s["text"] for s in iter_segments(doc)) == doc["markdown"]
    again = verify_doc(copy.deepcopy(doc), catalog)
    assert again["violations"] == doc["violations"] and again["stats"] == doc["stats"]
    assert not INTEGRITY & {v["code"] for v in doc["violations"]}, doc["violations"]


# --------------------------------------------------------------------------
# 给写作者看的目录
# --------------------------------------------------------------------------


def test_catalog_prompt_shows_query_rows_and_how_to_cite(catalog):
    prompt = catalog_prompt(catalog)
    assert "Q1" in prompt and "region, amount, orders, note" in prompt
    assert "[[v:Q1.r0.amount]]" in prompt and "[[table:Q1" in prompt
    assert "r0" in prompt and "华东" in prompt and "18,230.5" in prompt
    assert "Q2" in prompt and "无法读取" in prompt
    refused = catalog_prompt(catalog, cells_allowed=False)
    assert "[[v:" not in refused and "[[see:Q1]]" in refused


def test_cell_rules_only_show_when_there_are_cells_to_cite(catalog):
    """写作规则本身还是一期那份：升级前发起的运行在升级后跑到报告节点，提示一字不差；只有口径卡的目录
    也不该教写作者去引用不存在的 Q1。单元格和整表的写法跟着目录里的查询出现。"""
    from app.engine.evidence import MARKER_RULES

    assert "[[v:" not in MARKER_RULES and "[[table:" not in MARKER_RULES and "Q1" not in MARKER_RULES
    for kinds in (("metric", "input"), ("metric", "retrieval")):
        partial = {a: e for a, e in catalog.items() if e.get("kind") in kinds}
        prompt = catalog_prompt(partial) + MARKER_RULES
        assert "[[v:" not in prompt and "[[table:" not in prompt, kinds
    full = catalog_prompt(catalog)
    assert "[[v:Q1.r0.列名]]" in full and "[[table:Q1 cols=列a,列b rows=0-4]]" in full
    refused = catalog_prompt(catalog, cells_allowed=False)
    assert "[[v:" not in refused and "[[table:" not in refused
