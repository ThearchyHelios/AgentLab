"""已发布口径的定义说明要能被选口径的模型看到（app/look/calibers.definition_text 与 detail）。

定义是文档起草那条路唯一带过来的东西：模型看不到它，就只能凭指标名猜这份口径答的是什么问题，
文档里写的「不包含退款」也就白写了。
"""
from __future__ import annotations

import pytest

from app.look.calibers import DEFINITION_MAX, CaliberKind, CaliberView, definition_text


def _view(definition: str = "", **over: object) -> CaliberView:
    base = dict(workflow_id="w1", workflow_name="周销售", version=1, level="governed", node_id="card",
                card="口径卡", caliber="周销售 @ v1", metric_id="gmv", name="周销售额", unit="元",
                expression="cell(nodes.fetch, 0, 'gmv')",
                inputs=(("fetch", "db_query__shop", ("gmv",)),), reason=None)
    base.update(over)
    return CaliberView(definition=definition, **base)  # type: ignore[arg-type]


def test_definition_text_keeps_three_parts() -> None:
    text = definition_text({"text": "本周已支付订单金额合计", "scope": "全渠道", "exclusions": ["退款", "测试账号"],
                            "owner": "财务部", "effective_from": "2026-01-01"})
    assert text == "本周已支付订单金额合计；适用范围：全渠道；不包含：退款、测试账号"
    # 负责人和生效日期是治理信息，不占模型的额度
    assert "财务部" not in text and "2026-01-01" not in text


def test_definition_text_is_capped_and_empty_when_missing() -> None:
    long = definition_text({"text": "长" * (DEFINITION_MAX + 50)})
    assert len(long) == DEFINITION_MAX + 1 and long.endswith("…")
    assert definition_text(None) == ""
    assert definition_text({}) == ""
    assert definition_text({"owner": "财务部"}) == ""


@pytest.mark.asyncio
async def test_detail_shows_definition_only_when_present() -> None:
    with_def = _view("本周已支付订单金额合计；不包含：退款")
    plain = _view()
    text, record = await CaliberKind([with_def, plain]).detail([(with_def, "named")], budget=10_000)
    assert "定义：本周已支付订单金额合计；不包含：退款" in text
    assert record[0]["kind"] == "caliber"

    text, _ = await CaliberKind([plain]).detail([(plain, "named")], budget=10_000)
    assert "定义：" not in text
