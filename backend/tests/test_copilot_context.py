"""Copilot 的数据源上下文注入。

这组守的是"Copilot 知道有哪些库、写得出能跑的 SQL"。断了的表现很隐蔽：
它不会报错，只会退回让用户自己写 SQL 的代码节点——接了数据库等于白接。

按库的大小定「给详细摘要还是只列表名」的那一套（预算、40 张表的阈值）已经拿掉：system 里每个库一行概览，
要看表时模型自己 look（look 文档 D3，见 test_copilot_table_pick.py）。这里留着结构化输出和摘要本身的几条。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.api.copilot import GRAPH_SCHEMA
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
