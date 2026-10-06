"""双盲复算的说明：依据写进括号不再断句，占位词不当说明（期 5 真实模型验收 M1）。

盲算 agent 和反方审稿、审批质询共用核查 agent 交结果的提示，同样会把 [[see:Q1]] 写进括号；真实模型验收里还见过盲算交回
字面的「placeholder」当说明，审批卡上照着显示。

模型一律是 MockChatModel 剧本，订单库是虚构的（tests/fixtures/dual_check_shop.py）。
"""
from __future__ import annotations

from app.core import artifact_store
from tests.fixtures.dual_check_shop import BY_ORDER_DATE, RIGHT, Blind
from tests.test_dual_check_node import (  # noqa: F401 - 夹具按名字引入
    clean_settings,
    engine_up,
    graph,
    pending,
    shop,
    start,
)


async def _blind_input(monkeypatch, shop, rationale: str) -> dict:
    name, _ = shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")}, rationale=rationale)
    run = await start(graph(name, BY_ORDER_DATE))
    assert run.status == "interrupted", run.error
    card = artifact_store.load((await pending(run.id)).payload["card_artifact"])
    [mine] = card["metrics"][0]["blind"]["inputs"]
    return mine


async def test_see_in_brackets_does_not_break_the_blind_rationale(monkeypatch, shop):
    mine = await _blind_input(monkeypatch, shop, "按口径说明统计（[[see:Q1]]，已排除退款），结果是 {ref}（[[see:Q1]]）。")
    assert mine["rationale"] == "按口径说明统计（已排除退款），结果是 330。"
    doc = mine["rationale_doc"]
    assert doc["markdown"] == mine["rationale"] and doc["violations"] == []


async def test_a_placeholder_rationale_is_not_shown_as_the_explanation(monkeypatch, shop):
    mine = await _blind_input(monkeypatch, shop, "placeholder")
    assert mine["value"] == 330.0, "数照样采用"
    assert (mine["rationale"], mine["rationale_doc"]) == ("", None)
