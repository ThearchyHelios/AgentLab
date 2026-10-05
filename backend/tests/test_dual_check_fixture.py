"""双盲复算审批卡的前端夹具（frontend/src/dev/fixtures/dual_check.json）要和服务端的真实输出对得上。

审批卡上「对照两条 SQL」的高亮、「只差一个方面会记入数据目录」的提示，都是照 explain_sql_diff 的输出画的。它的
text_diff 是规范化之后的行（sqlglot pretty：列名带表名、条件并成一行），和原文的行对不上；夹具要是照原文手写，
check-verify 在夹具上能过，真实数据上却一处都不高亮（2026-10 复核时发现）。所以这里按夹具里的两条 SQL 重新算一遍
差异说明，逐个指标、逐个变体和夹具里写的比。sql_semantics 改了说法或规范形式，这里先变红，按下面的办法重新生成。

审批 payload 的摘要照双盲复算节点的写法核对：diff 是前 3 条差异的原话，fallback 是「除了兜底和解析失败，一条具体
差异都没有」，delta_pct 是 (盲算值 − 原流程值) ÷ |原流程值| × 100 保留两位。夹具的数据源当 SQLite 比（方言 sqlite），
两边的叫法是「原流程」「盲算」，同节点调用时一样。

重新生成夹具里的差异说明（只改 diff、摘要的 diff 和 fallback，其余原样）：
    cd backend && PYTHONPATH=. python tests/test_dual_check_fixture.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.data.sql_semantics import explain_sql_diff

FIXTURE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "dev" / "fixtures" / "dual_check.json"
DIALECT = "sqlite"
LABELS = ("原流程", "盲算")
#: 摘要里每个指标最多列几条差异（同双盲复算节点的 SUMMARY_DIFFS）
SUMMARY_DIFFS = 3


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
    FIXTURE.write_text(json.dumps(fx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    regenerate()
