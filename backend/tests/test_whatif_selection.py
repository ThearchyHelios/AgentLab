"""数字推演取哪一份：同一张口径卡执行过几次时推演哪一次、那次执行之前各节点的产出和跑完的时刻、快照什么时候交回。

照 api/evidence.py 的 _Sealed 手写事件（虚构的工件 id），不起运行、不碰库：真跑的循环图里报告总是引用最后一次执行，
节点也常常不会在口径卡之后再跑，这几条边界只有手写的事件才拿得到。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.api.coded import CodedHTTPException
from app.api.evidence import _Sealed
from app.api.whatif import _card_of, _finished_before, _outputs_before, _returned

FILL1, FILL2, A1, A2, OUT1, OUT2, Q1 = ("f1" * 32, "f2" * 32, "a1" * 32, "a2" * 32, "c1" * 32, "c2" * 32, "d1" * 32)


@dataclass
class Report:
    node_id: str
    doc: dict[str, Any] | None = field(default_factory=dict)


def sealed(events: list[tuple[int, str, str | None, dict[str, Any]]]) -> _Sealed:
    return _Sealed(run_id="r1", graph={"nodes": [{"id": "card", "type": "metrics"}]},
                   seal={"sealed": True, "ok": True, "manifest_seq": 99, "legacy": False}, events=events)


#: 循环两轮：fill → card，fill → card。第一轮的卡读到 FILL1，第二轮读到 FILL2
LOOP = [
    (1, "node.started", "fill", {}),
    (2, "tool.end", "fill", {"query_artifact": Q1}),
    (3, "node.finished", "fill", {"artifact": FILL1}),
    (4, "node.started", "card", {}),
    (5, "node.finished", "card", {"artifact": OUT1, "evidence": [{"kind": "metric_set", "artifact": A1}]}),
    (6, "node.started", "fill", {}),
    (7, "node.finished", "fill", {"artifact": FILL2, "evidence": [{"kind": "query", "artifact": Q1}]}),
    (8, "node.started", "card", {}),
    (9, "node.finished", "card", {"artifact": OUT2, "evidence": [{"kind": "metric_set", "artifact": A2}]}),
]
CARDS = [("card", A1, {"metrics": [{"id": "s", "value": 1}]}), ("card", A2, {"metrics": [{"id": "s", "value": 2}]})]


def cites(*artifacts: str, node: str = "write") -> Report:
    return Report(node, {"catalog": {f"m:{i}": {"kind": "metric", "node_id": "card", "artifact": a}
                                     for i, a in enumerate(artifacts)}})


def test_the_execution_the_report_cites_is_chosen_over_the_last_one():
    artifact, card, started = _card_of(sealed(LOOP), CARDS, "card", [cites(A1)], None)
    assert (artifact, card["metrics"][0]["value"], started) == (A1, 1, 4)


def test_without_a_citation_the_last_execution_is_used():
    assert _card_of(sealed(LOOP), CARDS, "card", [cites()], None)[0] == A2
    assert _card_of(sealed(LOOP), CARDS, "card", [], None)[0] == A2


def test_a_chosen_report_decides_which_execution_is_used():
    """两份报告各引用一次执行：指定了 report 就按那一份，不指定按任意一份引用的（取后一次）。"""
    reports = [cites(A1, node="weekly"), cites(A2, node="daily")]
    assert _card_of(sealed(LOOP), CARDS, "card", reports, "weekly")[0] == A1
    assert _card_of(sealed(LOOP), CARDS, "card", reports, "daily")[0] == A2
    assert _card_of(sealed(LOOP), CARDS, "card", reports, None)[0] == A2


def test_a_card_whose_content_does_not_verify_is_refused():
    with pytest.raises(CodedHTTPException) as caught:
        _card_of(sealed(LOOP), CARDS[1:], "card", [cites(A1)], None)
    assert caught.value.status_code == 409


def test_node_outputs_and_finish_times_stop_at_the_chosen_execution():
    """第一轮的卡（第 4 条开始）读到的是 FILL1：第二轮 fill 的产出在它之后，不算。"""
    events = sealed(LOOP)
    assert _outputs_before(events, 4) == {"fill": FILL1}
    assert _finished_before(events, 4) == {"fill": 3}
    assert _outputs_before(events, 8) == {"fill": FILL2, "card": OUT1}
    assert _finished_before(events, 8) == {"fill": 7, "card": 5}


def test_a_snapshot_counts_as_returned_the_first_time_it_shows_up():
    assert _returned(sealed(LOOP)) == {Q1: 2}
    merged_only = [(3, "node.finished", "merged", {"artifact": OUT1, "evidence": [{"kind": "query", "artifact": Q1}]})]
    assert _returned(sealed(merged_only)) == {Q1: 3}
    # 同一条查询又跑了一次（结果一样，工件 id 也一样）：仍按第一次交回算，第一次之后算出来的值都可能受影响
    again = [(2, "tool.end", "fill", {"query_artifact": Q1}), (6, "tool.end", "fill", {"query_artifact": Q1})]
    assert _returned(sealed(again)) == {Q1: 2}
