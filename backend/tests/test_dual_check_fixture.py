"""双盲复算审批卡的前端夹具（frontend/src/dev/fixtures/dual_check.json）要和服务端的真实输出对得上。

审批卡上「对照两条 SQL」的高亮、「只差一个方面会记入数据目录」的提示，都是照 explain_sql_diff 的输出画的。它的
text_diff 是规范化之后的行（sqlglot pretty：列名带表名、条件并成一行），和原文的行对不上；夹具要是照原文手写，
check-verify 在夹具上能过，真实数据上却一处都不高亮（2026-10 复核时发现）。所以这里按夹具里的两条 SQL 重新算一遍
差异说明，逐个指标、逐个变体和夹具里写的比。sql_semantics 改了说法或规范形式，这里先变红，按下面的办法重新生成。

审批 payload 的摘要照双盲复算节点的写法核对：diff 是前 3 条差异的原话，fallback 是「除了兜底和解析失败，一条具体
差异都没有」，delta_pct 是 (盲算值 − 原流程值) ÷ |原流程值| × 100 保留两位。夹具的数据源当 SQLite 比（方言 sqlite），
两边的叫法是「原流程」「盲算」，同节点调用时一样。

**写目录的预告。** 服务端在摘要里另带每个指标的 catalog（采用原流程、采用盲算各会往数据目录里写什么，
dual_check_facts.preview，约定之外另加的），审批卡照它说；老服务端没有时卡上按差异的形状有条件地说，两种说法都按
「服务端到底写不写」核对（2026-10 复核：卡上说会记入、服务端却不写的有好几种）。夹具里的预告（catalog_preview 按指标、
variants 各自的 catalog）也是服务端的真实输出：按下面的 SCHEMA、空目录、指标自己的范围留空算。双盲复算的服务端合入
之前没有这个模块，逐项比对那一项跳过，形状和门槛照样查。

重新生成夹具里的差异说明和预告（只改 diff、摘要的 diff 和 fallback、预告，其余原样；没有预告的模块时预告不动）：
    cd backend && PYTHONPATH=. python tests/test_dual_check_fixture.py
"""
from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.data.catalog import TABLE_FIELD_LABEL
from app.data.sql_semantics import explain_sql_diff

FIXTURE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "dev" / "fixtures" / "dual_check.json"
DIALECT = "sqlite"
LABELS = ("原流程", "盲算")
#: 摘要里每个指标最多列几条差异（同双盲复算节点的 SUMMARY_DIFFS）
SUMMARY_DIFFS = 3
#: 预告用的数据源名和表结构（虚构）：服务端要认得出表、认得出聚合的列属于哪张表，才会预告写哪一项
SOURCE = "shop"
SCHEMA: dict[str, Any] = {"tables": {
    "orders": {"name": "orders", "columns": [
        {"name": "id", "type": "INTEGER"}, {"name": "user_id", "type": "INTEGER"}, {"name": "amount", "type": "REAL"},
        {"name": "status", "type": "TEXT"}, {"name": "channel", "type": "TEXT"}, {"name": "paid_at", "type": "TEXT"},
        {"name": "created_at", "type": "TEXT"}], "primary_key": ["id"]},
    "refunds": {"name": "refunds", "columns": [
        {"name": "id", "type": "INTEGER"}, {"name": "order_id", "type": "INTEGER"}, {"name": "amount", "type": "REAL"},
        {"name": "created_at", "type": "TEXT"}], "primary_key": ["id"]},
}}
#: 写目录时认得出的三项（dual_check_facts.FACT_ITEMS）
FACT_ITEMS = ("business_date", "valid_filter", "dedup")
#: 差异里能对上这三项的方面：时间列、筛选条件、聚合
FACT_ASPECTS = ("time", "filter", "aggregate")
#: 变体换掉的是口径卡里的「销售额」：预告里的指标名照它
VARIANT_METRIC = "销售额"


def _diff(original: str, blind: str) -> dict[str, Any]:
    """工件里的 diff 只留界面用得到的几项：条目只有 aspect、text，不带两边的写法和 detail（同节点写工件时）。"""
    out = explain_sql_diff(original, blind, DIALECT, labels=LABELS).as_dict()
    return {"equivalent": out["equivalent"], "items": [{"aspect": i["aspect"], "text": i["text"]} for i in out["items"]],
            "fallback": out["fallback"], "text_diff": out["text_diff"]}


def _differing(metric: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """两边取值不同的输入（按 path 配对）：节点只比这些输入的 SQL。"""
    blind = {i["path"]: i for i in metric["blind"]["inputs"]}
    return [(i, blind[i["path"]]) for i in metric["original"]["inputs"] if i["value"] != blind[i["path"]]["value"]]


def _summary_of(metric: dict[str, Any]) -> dict[str, Any]:
    items = metric["diff"]["items"]
    original, blind = metric["original"]["value"], metric["blind"]["value"]
    return {
        "diff": [i["text"] for i in items[:SUMMARY_DIFFS]],
        "fallback": not any(i["aspect"] not in ("other", "parse") for i in items),
        "delta_pct": None if original == 0 else round((blind - original) / abs(original) * 100, 2),
        "original_rendered": metric["original"]["rendered"],
        "blind_rendered": metric["blind"]["rendered"],
    }


def _load() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _candidate(metric: str, original: str, blind: str) -> dict[str, Any]:
    """双盲复算节点交给写目录那一步的候选（少一个 choice）：只有一个输入对不上，差异条目带两边的写法和 detail（工件里
    只留 aspect、text，写目录要看完整的）。指标自己的范围留空：夹具里没有口径说明、分组和运行输入。"""
    diff = explain_sql_diff(original, blind, DIALECT, schema=SCHEMA, labels=LABELS).as_dict()
    return {"metric": metric, "source": SOURCE, "dialect": DIALECT, "sql": {"a": original, "b": blind},
            "diff": {"equivalent": diff["equivalent"], "fallback": diff["fallback"], "items": diff["items"]},
            "scope": {"texts": [], "columns": []}}


def _preview_cases(fx: dict[str, Any]) -> Iterator[tuple[str, str, str, str, dict[str, Any], dict[str, Any]]]:
    """夹具里每一份预告：(名字, 指标名, 原流程 SQL, 盲算 SQL, 工件里的差异, 预告)。口径卡的指标按对不上的那个输入，
    变体按它的一对 SQL（检查脚本拿它换掉「销售额」）。"""
    for metric in fx["card"]["metrics"]:
        (a, b), = _differing(metric)
        yield (metric["metric_id"], metric["name"], a["sql"], b["sql"], metric["diff"],
               fx["catalog_preview"][metric["metric_id"]])
    for name, variant in fx["variants"].items():
        original, blind = variant["sql"]
        yield name, VARIANT_METRIC, original, blind, variant["diff"], variant["catalog"]


def _shown(side: dict[str, Any]) -> dict[str, Any]:
    """一边的预告里审批卡用得到的：写不写，写的话写到哪、写成什么。不写的原因只在时间线上给人看，措辞不比。"""
    if side.get("write") is True:
        return {k: side.get(k) for k in ("write", "source", "table", "item", "value")}
    return {"write": side.get("write")}


def _server_preview() -> Any:
    """服务端的预告（dual_check_facts.preview）；双盲复算的服务端还没合入时返回 None。"""
    try:
        return getattr(importlib.import_module("app.engine.dual_check_facts"), "preview", None)
    except ImportError:
        return None


def test_card_metrics_carry_the_real_diff():
    fx = _load()
    for metric in fx["card"]["metrics"]:
        pairs = _differing(metric)
        # 夹具里每个指标只有一个输入对不上：节点这时直接用这一处的差异说明，几处时才会合并、加前缀
        assert len(pairs) == 1, metric["metric_id"]
        (a, b), = pairs
        assert metric["diff"] == _diff(a["sql"], b["sql"]), metric["metric_id"]


def test_summary_follows_the_card():
    fx = _load()
    summary = {s["metric_id"]: s for s in fx["approval"]["payload"]["summary"]}
    assert list(summary) == [m["metric_id"] for m in fx["card"]["metrics"]]
    for metric in fx["card"]["metrics"]:
        got = summary[metric["metric_id"]]
        assert {k: got[k] for k in _summary_of(metric)} == _summary_of(metric), metric["metric_id"]


def test_variants_carry_the_real_diff():
    fx = _load()
    assert fx["variants"]
    for name, variant in fx["variants"].items():
        original, blind = variant["sql"]
        assert variant["diff"] == _diff(original, blind), name


def test_text_diff_is_not_the_raw_text():
    """守住这次的教训：至少有一处规范化之后的行和原文的任何一行都不一样（真实输出就是这样），夹具不再照原文手写。"""
    fx = _load()
    gmv = next(m for m in fx["card"]["metrics"] if m["metric_id"] == "gmv")
    raw = {" ".join(line.split()).lower() for side in ("original", "blind") for line in gmv[side]["inputs"][0]["sql"].split("\n")}
    shown = [" ".join(line[2:].split()).lower() for line in gmv["diff"]["text_diff"]]
    assert shown and not any(line in raw for line in shown)


def test_catalog_previews_have_the_server_shape():
    """每个指标、每个变体都有预告，两边齐全；写的只能是服务端写目录的门槛之内的（两条 SQL 不等价、没有兜底、恰好一条
    差异，而且是时间列、筛选条件或聚合），写的是三项之一、有表有取值；不写的有原因。不依赖服务端的模块，一直查。"""
    fx = _load()
    assert set(fx["catalog_preview"]) == {m["metric_id"] for m in fx["card"]["metrics"]}
    cases = list(_preview_cases(fx))
    assert len(cases) == len(fx["card"]["metrics"]) + len(fx["variants"])
    for name, _, _, _, diff, preview in cases:
        assert set(preview) == {"a", "b"}, name
        single = not diff["equivalent"] and not diff["fallback"] and len(diff["items"]) == 1
        for side in ("a", "b"):
            said = preview[side]
            if said["write"] is True:
                assert single and diff["items"][0]["aspect"] in FACT_ASPECTS, (name, side)
                assert said["item"] in FACT_ITEMS and said["item"] in TABLE_FIELD_LABEL, (name, side)
                assert said["source"] == SOURCE and said["table"] in SCHEMA["tables"] and said["value"], (name, side)
            else:
                assert said["write"] is False and said["reason"], (name, side)


def test_catalog_previews_follow_the_server():
    """预告里审批卡用得到的部分（写不写、写到哪、写成什么）和服务端（dual_check_facts.preview）一致：服务端改了写目录
    的规则，这里先变红——审批卡上照预告说的、检查脚本按预告核对的有条件说法都要跟着看一遍。"""
    preview = _server_preview()
    if preview is None:
        pytest.skip("双盲复算的服务端（dual_check_facts.preview）还没合入")
    for name, metric, original, blind, _, expected in _preview_cases(_load()):
        got = preview(_candidate(metric, original, blind), SCHEMA, {})
        assert {s: _shown(got[s]) for s in ("a", "b")} == {s: _shown(expected[s]) for s in ("a", "b")}, name


def regenerate() -> None:
    fx = _load()
    for metric in fx["card"]["metrics"]:
        (a, b), = _differing(metric)
        metric["diff"] = _diff(a["sql"], b["sql"])
    summary = {s["metric_id"]: s for s in fx["approval"]["payload"]["summary"]}
    for metric in fx["card"]["metrics"]:
        summary[metric["metric_id"]].update(_summary_of(metric))
    for variant in fx["variants"].values():
        variant["diff"] = _diff(*variant["sql"])
    if (preview := _server_preview()) is not None:
        fx["catalog_preview"] = {}
        for metric in fx["card"]["metrics"]:
            (a, b), = _differing(metric)
            said = preview(_candidate(metric["name"], a["sql"], b["sql"]), SCHEMA, {})
            fx["catalog_preview"][metric["metric_id"]] = said
        for variant in fx["variants"].values():
            variant["catalog"] = preview(_candidate(VARIANT_METRIC, *variant["sql"]), SCHEMA, {})
    FIXTURE.write_text(json.dumps(fx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    regenerate()
