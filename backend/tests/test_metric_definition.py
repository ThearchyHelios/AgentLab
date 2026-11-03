"""指标定义卡（definition）与参数（params）的最小结构校验。

执行还是公式这一种；定义文本、参数先作为语义元数据校验、进内容指纹、进 descriptor。规则/评估/外部常量没实现前，
写进图里要按错误挡住，免得被当成能算的指标。
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
    errors = _errors(_graph(kind="rule"))
    assert any(i.code == "metrics.kind_unsupported" and "还没有实现" in i.message for i in errors)


def test_params_must_be_named_and_typed_once_each():
    errors = _errors(_graph(params=[{"name": "", "type": "string"},
                                    {"name": "store", "type": "string"},
                                    {"name": "store", "type": "date"}]))
    codes = [i.code for i in errors]
    assert codes.count("metrics.params_invalid") >= 3


def test_definition_must_be_an_object_with_text_fields():
    errors = _errors(_graph(definition={"text": 1, "exclusions": "新客"}))
    assert [i.code for i in errors].count("metrics.definition_invalid") >= 2
