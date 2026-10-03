"""数据剖析（阶段 2A）里不连库的部分：设置的解析与校验、SQL 字面量、各方言的剖析 SQL 都过得了守卫、
目录对剖析结果的容纳（待填含义的码值、撤销审阅回到剖析的结论）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.data.catalog_profile import PROFILE_OPTION, ProfileSettings, profile_settings, profile_settings_problem
from app.data.engine import build_url

# ---------------------------------------------------------------- 设置


def test_settings_default_off_and_parse_leniently():
    s = profile_settings({})
    assert s == ProfileSettings()
    assert s.enabled is False and s.max_queries == 60 and s.query_timeout_s == 10 and s.sample_size == 2000
    assert s.max_scan_rows == 100_000 and s.max_total_s == 120
    on = profile_settings({PROFILE_OPTION: {"enabled": True, "max_queries": "30", "sample_size": 500}})
    assert on.enabled is True and on.max_queries == 30 and on.sample_size == 500
    # 库里存着的值不对（老数据、手改过）：那一项用缺省，不因此开启，也不报错
    odd = profile_settings({PROFILE_OPTION: {"enabled": "yes", "max_queries": 0, "query_timeout_s": "x"}})
    assert odd.enabled is False and odd.max_queries == 60 and odd.query_timeout_s == 10
    assert profile_settings({PROFILE_OPTION: "on"}) == ProfileSettings()


@pytest.mark.parametrize("value, label", [
    ({"max_queries": 0}, "查询次数上限"),
    ({"max_queries": 501}, "查询次数上限"),
    ({"max_queries": 2.5}, "查询次数上限"),
    ({"query_timeout_s": 61}, "单条查询时限"),
    ({"sample_size": 5}, "抽样键值数"),
    ({"max_scan_rows": -1}, "整表统计行数上限"),
    ({"max_total_s": 5}, "总时长上限"),
    ({"enabled": "true"}, "开启数据剖析"),
    ({"max_queries": True}, "查询次数上限"),
])
def test_settings_problem_names_the_field_in_chinese(value, label):
    problem = profile_settings_problem({PROFILE_OPTION: value})
    assert problem and label in problem and "数据剖析" in problem


def test_settings_problem_rejects_unknown_keys_and_wrong_shape():
    assert "无法识别" in profile_settings_problem({PROFILE_OPTION: {"enabled": True, "rows": 1}})
    assert profile_settings_problem({PROFILE_OPTION: ["enabled"]})
    assert profile_settings_problem({}) is None
    assert profile_settings_problem({PROFILE_OPTION: None}) is None
    assert profile_settings_problem({PROFILE_OPTION: {"enabled": True, "max_queries": 500, "query_timeout_s": 1.5,
                                                      "sample_size": 10, "max_scan_rows": 0,
                                                      "max_total_s": 600}}) is None


def test_profile_option_never_reaches_the_connection_url():
    """剖析设置是 AgentLab 自己的配置：拼进连接串，驱动会把它当未知参数拒掉。"""
    source = SimpleNamespace(kind="mysql", host="db.example", port=3306, database="shop", username="ro",
                             password=None, options={"charset": "utf8mb4", PROFILE_OPTION: {"enabled": True}})
    url = build_url(source)
    assert "charset=utf8mb4" in url and PROFILE_OPTION not in url and "enabled" not in url
