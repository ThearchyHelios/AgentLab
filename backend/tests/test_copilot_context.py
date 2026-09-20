"""Copilot 的数据源上下文注入。

这组守的是"Copilot 知道有哪些库、写得出能跑的 SQL"。断了的表现很隐蔽：
它不会报错，只会退回让用户自己写 SQL 的代码节点——接了数据库等于白接。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.copilot import GRAPH_SCHEMA, _DATASOURCE_BUDGET_CHARS
from app.data.introspect import summary


def _source(n_tables: int, *, name: str = "warehouse", schema: str | None = "ANALYTICS"):
    tables = {
        f"t{i}": {
            "qualified": f"{schema}.t{i}" if schema else f"t{i}",
            "schema": schema,
            "columns": [
                {"name": f"c{j}", "type": "VARCHAR", "nullable": True, "comment": None}
                for j in range(20)
            ],
            "primary_key": [], "comment": None, "is_view": True,
        }
        for i in range(n_tables)
    }
    return SimpleNamespace(
        id="x", name=name, kind="oracle", readonly=True,
        description="测试库", options={}, schema_cache={"tables": tables, "schema": schema},
    )


def test_graph_schema_allows_arbitrary_config_keys():
    """config 的键随节点类型千变万化，没法枚举成 properties。

    不显式写 additionalProperties=True，某些 provider 的结构化输出会按
    "不允许额外字段"处理，把 config 整个过滤成 {}——生成的图每个节点都是
    空壳，校验挂在"工具节点还没选工具"。这条回归过一次，代价是整条非流式
    生成路径不可用。
    """
    node = GRAPH_SCHEMA["properties"]["nodes"]["items"]
    assert node["properties"]["config"]["additionalProperties"] is True
    assert "config" in node["required"]


def test_compact_mode_is_substantially_smaller():
    """压缩比要真的有效，否则降级没意义。实测约 35%。"""
    full = summary(_source(53))
    compact = summary(_source(53), detail=False)
    assert len(compact) < len(full) * 0.5


def test_summary_without_schema_uses_bare_names():
    """没有 schema 概念的库不要硬拼前缀，否则生成的 SQL 反而错。"""
    text = summary(_source(2, schema=None))
    assert "t0" in text and "None.t0" not in text


def test_budget_constant_is_sane():
    """预算太小会让单个库就触发降级，太大等于没有预算。"""
    assert 3000 <= _DATASOURCE_BUDGET_CHARS <= 20000


@pytest.mark.parametrize("n_tables,expect_detail", [(3, True), (40, True), (53, False)])
def test_object_count_decides_detail_level(n_tables, expect_detail):
    """对象超过截断阈值就切紧凑模式。

    关键取舍：detail 模式每库只列 40 个对象，多出来的 Copilot 根本看不见。
    对 53 个对象的库，"40 张表带字段"不如"53 张表只给名字"——找不到那张表，
    字段写得再全也没用。截断丢信息比省 token 更糟。
    """
    from app.api.copilot import _DETAIL_TABLE_LIMIT
    from app.data.introspect import table_names

    src = _source(n_tables)
    assert (len(table_names(src)) <= _DETAIL_TABLE_LIMIT) is expect_detail


def test_many_sources_trigger_compact_mode():
    """单库不超标，几个库加起来超预算时整体降级。"""
    sources = [_source(35, name=f"db{i}") for i in range(6)]
    full = sum(len(summary(s)) for s in sources)
    assert full > _DATASOURCE_BUDGET_CHARS
    compact = sum(len(summary(s, detail=False)) for s in sources)
    assert compact < _DATASOURCE_BUDGET_CHARS
