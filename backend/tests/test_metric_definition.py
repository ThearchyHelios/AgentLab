"""指标定义卡（definition）与参数（params）：结构校验、四种 kind（公式/外部常量/规则/评估）的执行与标签。

定义文本、参数进内容指纹和 descriptor；不认识的 kind 写进图里要按错误挡住，免得被当成能算的指标。
"""
from __future__ import annotations

from app.engine.schema import GraphSpec, validate_graph
from tests import ledger_samples as S


def _graph(**metric) -> dict:
    graph = S.caliber_graph()
    graph["nodes"][2]["data"]["config"]["metrics"][0].update(metric)
    return graph


def _errors(graph: dict) -> list:
    spec = GraphSpec.model_validate(graph)
    return [i for i in validate_graph(spec).issues if i.level == "error"]


def test_a_formula_metric_can_carry_a_definition_and_params():
    graph = _graph(kind="formula", definition={"text": "已支付订单的销售额", "scope": "全部门店",
                                               "owner": "运营", "effective_from": "2026-01-01"},
                   params=[{"name": "store", "type": "string", "default": "all"},
                           {"name": "min_amount", "type": "number"}])
    assert _errors(graph) == []


def test_an_unimplemented_kind_is_refused():
    errors = _errors(_graph(kind="normative"))
    assert any(i.code == "metrics.kind_unsupported" and "不认识" in i.message for i in errors)


def test_params_must_be_named_and_typed_once_each():
    errors = _errors(_graph(params=[{"name": "", "type": "string"},
                                    {"name": "store", "type": "string"},
                                    {"name": "store", "type": "date"}]))
    codes = [i.code for i in errors]
    assert codes.count("metrics.params_invalid") >= 3


def test_definition_must_be_an_object_with_text_fields():
    errors = _errors(_graph(definition={"text": 1, "exclusions": "新客"}))
    assert [i.code for i in errors].count("metrics.definition_invalid") >= 2


async def test_an_external_constant_metric_runs_without_a_query():
    from app.engine.runner import run_manager
    from tests import ledger_samples as S

    await run_manager.setup()
    try:
        graph = S.chain(
            S.node("start", "input"),
            S.node("card", "metrics", caliber="年度目标", caliber_version="v1", metrics=[{
                "id": "target", "name": "年度目标", "unit": "元", "decimals": 0, "kind": "external",
                "definition": {"text": "董事会批准的年度目标", "owner": "财务"},
                "external": {"value": 120000000, "source": "2026 年度预算决议", "as_of": "2026-01-01",
                             "stale_after_days": 365},
            }]),
            S.node("out", "output", fields=[{"name": "v", "value": "{{ nodes.card.metrics[0].rendered }}"}]))
        started = await run_manager.start(graph=graph, input_payload={})
        row = await S.wait(started.id)
        assert row.status == "succeeded", row.error
        assert row.output["v"] == "120,000,000元"
        from app.api.evidence import _sealed, _sealed_cards

        sealed = await _sealed(row.id)
        [card] = [c for _, _, c in _sealed_cards(sealed)]
        metric = card["metrics"][0]
        assert metric["external"]["source"] == "2026 年度预算决议"
        assert metric["external"]["stale"] is False
        assert metric["recompute_ok"] is None and metric["recompute_reason"]
    finally:
        await run_manager.shutdown()


def test_an_external_constant_needs_a_source_and_an_effective_date():
    errors = _errors(_graph(kind="external", external={"value": 10, "as_of": "not-a-date"}))
    codes = [i.code for i in errors]
    assert codes.count("metrics.external_invalid") >= 2
    assert _errors(_graph(kind="external", external={
        "value": 10, "source": "预算", "as_of": "2026-01-01", "stale_after_days": 30})) == []


def test_an_external_metric_is_labelled_external_not_reasoning():
    from app.ledger.labels import metric_label

    card = {"definition_hash": "abc", "metrics": [{"id": "target", "external": {
        "source": "预算", "as_of": "2026-01-01", "stale": True}}]}
    label = metric_label(card, "target", {}, {})
    assert label == {"kind": "external", "source": "预算", "as_of": "2026-01-01", "stale": True,
                     "definition_hash": "abc"}


def test_a_rule_metric_needs_inputs_conditions_and_a_known_operator():
    bad = _errors(_graph(kind="rule", rule={"inputs": {}, "all": [{"input": "x", "op": "like", "value": 1}]}))
    assert [i.code for i in bad].count("metrics.rule_invalid") >= 2
    good = _graph(kind="rule", rule={
        "inputs": {"status": "cell(nodes.fetch, 0, 'status')"},
        "all": [{"input": "status", "op": "eq", "value": "paid"}],
    })
    assert _errors(good) == []


async def test_a_rule_metric_evaluates_cells_deterministically(tmp_path):
    from app.api.evidence import _sealed, _sealed_cards
    from app.engine.runner import run_manager

    await run_manager.setup()
    try:
        await S.fresh_source(tmp_path / "rule.db")
        graph = S.chain(
            S.node("start", "input"),
            S.node("fetch", "tool", tool=S.tool(),
                   args={"sql": "SELECT status, amount FROM orders WHERE id = 1"}),
            S.node("card", "metrics", caliber="大额判断", caliber_version="v1", metrics=[{
                "id": "big", "name": "大额已支付", "unit": "1", "kind": "rule",
                "rule": {"inputs": {"status": "cell(nodes.fetch, 0, 'status')",
                                    "amount": "cell(nodes.fetch, 0, 'amount')"},
                         "all": [{"input": "status", "op": "eq", "value": "paid"},
                                 {"input": "amount", "op": "gte", "value": 50}],
                         "then": 1, "else": 0},
            }]),
            S.node("out", "output", fields=[{"name": "v", "value": "{{ nodes.card.metrics[0].value }}"}]))
        started = await run_manager.start(graph=graph, input_payload={})
        row = await S.wait(started.id)
        assert row.status == "succeeded", row.error
        assert row.output["v"] == "1"
        sealed = await _sealed(row.id)
        [card] = [c for _, _, c in _sealed_cards(sealed)]
        metric = card["metrics"][0]
        assert metric["rule"]["then"] == 1 and metric["value"] == 1
        assert [i["path"] for i in metric["inputs"]] == ["cell(nodes.fetch, 0, 'status')",
                                                         "cell(nodes.fetch, 0, 'amount')"]
    finally:
        await run_manager.shutdown()


def test_a_rule_metric_is_labelled_derived_with_its_input_queries():
    from app.ledger.labels import metric_label

    card = {"definition_hash": "abc", "metrics": [{
        "id": "big", "rule": {"all": [{"input": "status", "op": "eq", "value": "paid"}]},
        "inputs": [{"artifact": "q1", "via": "tool_cell", "path": "cell(nodes.fetch, 0, 'status')"}],
    }]}
    label = metric_label(card, "big", {}, {})
    assert label["kind"] == "derived" and label["queries"] == ["q1"]
    assert label["definition_hash"] == "abc" and label["rule"]["conditions"] == 1
    assert "paid" not in str(label)


def test_an_assessment_metric_needs_unique_dimensions_and_descending_levels():
    bad = _errors(_graph(kind="assessment", assessment={
        "dimensions": [{"name": "a", "input": "x"}, {"name": "a", "input": "y"}],
        "levels": [{"name": "B", "min": 70}, {"name": "A", "min": 85}]}))
    assert [i.code for i in bad].count("metrics.assessment_invalid") >= 2
    good = _graph(kind="assessment", assessment={
        "dimensions": [{"name": "响应", "input": "cell(nodes.fetch, 0, 'response')", "weight": 0.5},
                       {"name": "解决", "input": "cell(nodes.fetch, 0, 'resolution')", "weight": 0.5}],
        "levels": [{"name": "A", "min": 85}, {"name": "B", "min": 70}, {"name": "C", "min": 0}]})
    assert _errors(good) == []


async def test_an_assessment_metric_scores_and_picks_a_level(tmp_path):
    from app.api.evidence import _sealed, _sealed_cards
    from app.engine.runner import run_manager

    await run_manager.setup()
    try:
        await S.fresh_source(tmp_path / "assessment.db")
        graph = S.chain(
            S.node("start", "input"),
            S.node("fetch", "tool", tool=S.tool(), args={"sql": "SELECT 90 AS response, 70 AS resolution"}),
            S.node("card", "metrics", caliber="服务质量", caliber_version="v1", metrics=[{
                "id": "quality", "name": "服务质量", "unit": "分", "kind": "assessment",
                "assessment": {
                    "dimensions": [{"name": "响应", "input": "cell(nodes.fetch, 0, 'response')", "weight": 0.5},
                                   {"name": "解决", "input": "cell(nodes.fetch, 0, 'resolution')", "weight": 0.5}],
                    "levels": [{"name": "A", "min": 85}, {"name": "B", "min": 70}, {"name": "C", "min": 0}],
                },
            }]),
            S.node("out", "output", fields=[{"name": "v", "value": "{{ nodes.card.metrics[0].value }}"}]))
        started = await run_manager.start(graph=graph, input_payload={})
        row = await S.wait(started.id)
        assert row.status == "succeeded", row.error
        assert float(row.output["v"]) == 80
        sealed = await _sealed(row.id)
        [card] = [c for _, _, c in _sealed_cards(sealed)]
        assessment = card["metrics"][0]["assessment"]
        assert assessment["level"] == "B" and assessment["score"] == 80
        assert [d["normalized"] for d in assessment["dimensions"]] == [90.0, 70.0]
    finally:
        await run_manager.shutdown()


def test_an_assessment_metric_is_labelled_assessed():
    from app.ledger.labels import metric_label

    card = {"definition_hash": "abc", "metrics": [{
        "id": "quality",
        "assessment": {"score": 80, "level": "B",
                       "dimensions": [{"name": "手机号", "weight": 1, "value": "19900000501"}]},
        "inputs": [{"artifact": "q1", "via": "tool_cell"}],
    }]}
    label = metric_label(card, "quality", {}, {})
    assert label["kind"] == "assessed" and label["queries"] == ["q1"]
    assert label["assessment"]["level"] == "B" and label["definition_hash"] == "abc"
    assert "19900000501" not in str(label)
