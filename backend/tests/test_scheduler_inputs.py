"""定时任务的输入模板（app/scheduler/service.py）：日期变量按触发时间在任务时区算，模板写错就说出来。

- 变量都按 fire_at（计划触发时间）在任务时区算，不按「现在」：补跑晚了一小时，昨天还是计划那天的昨天
- 跨月、跨年、跨时区（同一时刻在上海已经是周一，在纽约还是周日）；周从周一算起
- schedule.last_fire_time 是上一次计划触发的时间（按 cron 往前找），和本次一样写成带偏移的 ISO 时间
- 模板写错（未知变量、未知过滤器、{{ }} 不成对、空的 {{ }}、值不是文本）抛 TemplateError，中文说明哪个输入
- 声明了入口字段的工作流：模板里多出来的字段名、缺的必填输入（没有默认值）都拦下

夹具的工作流、字段名都是虚构的。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.scheduler import service
from app.scheduler.service import TemplateError

UTC = timezone.utc


def values(fire_at: datetime, tz: str = "Asia/Shanghai", cron: str = "0 9 * * 1") -> dict[str, str]:
    return service.template_values(fire_at, tz, cron)


# --------------------------------------------------------------------------
# 变量按 fire_at 在任务时区算
# --------------------------------------------------------------------------


def test_monday_morning_in_shanghai():
    v = values(datetime(2026, 10, 5, 1, 0, tzinfo=UTC))   # 上海 2026-10-05（周一）09:00
    assert v == {
        "today": "2026-10-05",
        "yesterday": "2026-10-04",
        "this_week_start": "2026-10-05",
        "last_week_start": "2026-09-28",
        "last_week_end": "2026-10-04",
        "this_month_start": "2026-10-01",
        "last_month_start": "2026-09-01",
        "last_month_end": "2026-09-30",
        "fire_time": "2026-10-05T09:00:00+08:00",
        "last_fire_time": "2026-09-28T09:00:00+08:00",
    }


def test_across_a_month_boundary():
    # 上海 2026-03-01（周日）00:30，UTC 还是 2 月 28 日
    v = values(datetime(2026, 2, 28, 16, 30, tzinfo=UTC), cron="30 0 * * *")
    assert (v["today"], v["yesterday"]) == ("2026-03-01", "2026-02-28")
    assert (v["this_week_start"], v["last_week_start"], v["last_week_end"]) == ("2026-02-23", "2026-02-16", "2026-02-22")
    assert (v["this_month_start"], v["last_month_start"], v["last_month_end"]) == ("2026-03-01", "2026-02-01", "2026-02-28")
    assert v["last_fire_time"] == "2026-02-28T00:30:00+08:00"


def test_across_a_year_boundary():
    v = values(datetime(2027, 1, 1, 0, 0, tzinfo=UTC), cron="0 8 1 * *")   # 上海 2027-01-01（周五）08:00
    assert (v["today"], v["yesterday"]) == ("2027-01-01", "2026-12-31")
    assert (v["this_week_start"], v["last_week_start"], v["last_week_end"]) == ("2026-12-28", "2026-12-21", "2026-12-27")
    assert (v["this_month_start"], v["last_month_start"], v["last_month_end"]) == ("2027-01-01", "2026-12-01", "2026-12-31")
    assert v["last_fire_time"] == "2026-12-01T08:00:00+08:00"


def test_leap_february_ends_on_the_29th():
    v = values(datetime(2028, 3, 1, 1, 0, tzinfo=UTC), cron="0 9 1 * *")
    assert (v["last_month_start"], v["last_month_end"]) == ("2028-02-01", "2028-02-29")


def test_same_moment_in_two_timezones():
    """UTC 2026-10-04 20:00：上海已经是周一 04:00，纽约还是周日 16:00"""
    moment = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)
    shanghai = values(moment, "Asia/Shanghai", "0 4 * * *")
    new_york = values(moment, "America/New_York", "0 16 * * *")
    assert (shanghai["today"], shanghai["this_week_start"]) == ("2026-10-05", "2026-10-05")
    assert (new_york["today"], new_york["this_week_start"]) == ("2026-10-04", "2026-09-28")
    assert shanghai["fire_time"] == "2026-10-05T04:00:00+08:00"
    assert new_york["fire_time"] == "2026-10-04T16:00:00-04:00"


def test_fire_time_drops_sub_seconds():
    """立即运行的触发时间是点下去的那一刻，写进输入时只到秒"""
    v = values(datetime(2026, 10, 5, 3, 4, 5, 678901, tzinfo=UTC))
    assert v["fire_time"] == "2026-10-05T11:04:05+08:00"


# --------------------------------------------------------------------------
# 渲染与校验
# --------------------------------------------------------------------------


def test_render_inputs():
    v = values(datetime(2026, 10, 5, 1, 0, tzinfo=UTC))
    out = service.render_inputs({
        "stat_date": "{{ schedule.yesterday }}",
        "week": "{{schedule.last_week_start}}~{{ schedule.last_week_end }}",
        "region": "华东",
        "tag": "{{ schedule.today | upper }}",
    }, v)
    assert out == {"stat_date": "2026-10-04", "week": "2026-09-28~2026-10-04", "region": "华东", "tag": "2026-10-05"}


def test_valid_templates_pass():
    assert service.check_template({"a": "{{ schedule.today }}", "b": "固定值", "c": "{{ schedule.fire_time | trim }}"}) == {
        "a": "{{ schedule.today }}", "b": "固定值", "c": "{{ schedule.fire_time | trim }}"}
    assert service.check_template(None) == {}


@pytest.mark.parametrize("template, fragments", [
    ({"stat_date": "{{ schedule.yesterdy }}"}, ["stat_date", "schedule.yesterdy", "schedule.yesterday"]),
    ({"stat_date": "{{ input.day }}"}, ["stat_date", "input.day"]),
    ({"stat_date": "{{ schedule.today"}, ["stat_date", "成对"]),
    ({"stat_date": "schedule.today }}"}, ["stat_date", "成对"]),
    ({"stat_date": "{{ }}"}, ["stat_date", "空的"]),
    ({"stat_date": "{{ schedule.today | shout }}"}, ["stat_date", "shout", "过滤器"]),
    ({"stat_date": 20261005}, ["stat_date", "文本"]),
    ({"": "{{ schedule.today }}"}, ["字段名"]),
    (["{{ schedule.today }}"], ["输入模板"]),
])
def test_bad_templates_explain_in_chinese(template, fragments):
    with pytest.raises(TemplateError) as caught:
        service.check_template(template)
    for fragment in fragments:
        assert fragment in str(caught.value), str(caught.value)


def test_field_names_are_trimmed_and_unique():
    assert service.check_template({" stat_date ": "{{ schedule.yesterday }}"}) == {"stat_date": "{{ schedule.yesterday }}"}
    with pytest.raises(TemplateError) as caught:
        service.check_template({"stat_date": "a", "stat_date ": "b"})
    assert "stat_date" in str(caught.value) and "两次" in str(caught.value)


def test_variable_catalog():
    items = service.variables(datetime(2026, 10, 5, 1, 0, tzinfo=UTC), "Asia/Shanghai")
    names = [i["name"] for i in items]
    assert names == [f"schedule.{key}" for key in (
        "today", "yesterday", "this_week_start", "last_week_start", "last_week_end",
        "this_month_start", "last_month_start", "last_month_end", "fire_time", "last_fire_time")]
    by_name = {i["name"]: i for i in items}
    assert by_name["schedule.yesterday"] == {"name": "schedule.yesterday", "label": "昨天（YYYY-MM-DD）",
                                             "example": "2026-10-04"}
    assert all(i["label"] and i["example"] for i in items)


# --------------------------------------------------------------------------
# 和工作流入口字段对照
# --------------------------------------------------------------------------


def graph(*fields: dict) -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "input", "position": {"x": 0, "y": 0},
             "data": {"label": "输入", "config": {"fields": list(fields)}}},
            {"id": "out", "type": "output", "position": {"x": 300, "y": 0},
             "data": {"label": "成果", "config": {"fields": [{"name": "r", "value": "完成"}]}}},
        ],
        "edges": [{"source": "start", "target": "out"}],
    }


def test_fields_of_a_graph():
    g = graph({"name": "stat_date", "required": True}, {"name": "region", "default": "华东"})
    assert [f["name"] for f in service.input_fields(g)] == ["stat_date", "region"]
    assert service.input_fields({"nodes": "坏掉的图"}) == []


def test_unknown_field_names_are_refused():
    fields = service.input_fields(graph({"name": "stat_date"}, {"name": "region"}))
    with pytest.raises(TemplateError) as caught:
        service.check_fields({"stat_dat": "{{ schedule.yesterday }}"}, fields, "门店周报")
    assert "stat_dat" in str(caught.value) and "门店周报" in str(caught.value) and "stat_date" in str(caught.value)


def test_missing_required_inputs_are_refused_only_when_asked():
    fields = service.input_fields(graph({"name": "stat_date", "required": True},
                                        {"name": "region", "required": True, "default": "华东"}))
    with pytest.raises(TemplateError) as caught:
        service.check_fields({}, fields, "门店周报")
    assert "stat_date" in str(caught.value) and "必填" in str(caught.value)
    # 预览时还在填，不查缺的；有默认值的必填项不算缺
    service.check_fields({}, fields, "门店周报", require=False)
    service.check_fields({"stat_date": "{{ schedule.yesterday }}"}, fields, "门店周报")


def test_graph_without_declared_fields_accepts_any_name():
    """入口没声明字段时输入是开放的：运行时传什么键都收"""
    service.check_fields({"anything": "x"}, service.input_fields(graph()), "门店周报")
