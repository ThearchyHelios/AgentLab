"""组件目录（app/apps/catalog.py）的 props 写给人看的中文名。

- 每个组件的每一项 props 都有 title，而且是中文：应用编辑的属性面板按它写属性名，不露 label、chart、sort 这些键名
- 枚举都有 x-enum-labels，和 enum 一一对应（同表单 agentlab.form/1 的写法），选项不露 line / bar
- title、description、x-enum-labels 只是注解：校验照旧，取值不对的照样报 app.props_invalid
"""
from __future__ import annotations

import re

from jsonschema import Draft202012Validator

from app.apps.catalog import BY_NAME, COMPONENTS, catalog_payload

_CJK = re.compile(r"[㐀-鿿]")


def test_every_prop_has_a_chinese_title():
    missing = [(c["name"], key) for c in COMPONENTS for key, prop in c["props"]["properties"].items()
               if not (isinstance(prop.get("title"), str) and _CJK.search(prop["title"]))]
    assert missing == []
    # 接口交出去的就是这一份
    assert catalog_payload()["components"][0]["props"]["properties"]["title"]["title"] == "标题"


def test_enums_have_labels_in_the_same_order():
    enums = [(c["name"], key, prop) for c in COMPONENTS for key, prop in c["props"]["properties"].items() if "enum" in prop]
    assert enums, "目录里应当有枚举属性"
    for name, key, prop in enums:
        labels = prop.get("x-enum-labels")
        assert isinstance(labels, list) and len(labels) == len(prop["enum"]), (name, key)
        assert all(isinstance(t, str) and _CJK.search(t) for t in labels), (name, key)
    assert BY_NAME["series"]["props"]["properties"]["chart"]["x-enum-labels"] == ["折线图", "柱状图"]


def test_annotations_do_not_change_validation():
    schema = BY_NAME["notice"]["props"]
    assert not list(Draft202012Validator(schema).iter_errors({"text": "说明", "tone": "warn"}))
    errors = list(Draft202012Validator(schema).iter_errors({"tone": "提醒"}))
    assert [list(e.absolute_path) for e in errors] == [["tone"]]
    table = BY_NAME["table"]["props"]
    assert not list(Draft202012Validator(table).iter_errors({"title": "门店", "sort": "-gmv", "limit": 20}))
    assert list(Draft202012Validator(table).iter_errors({"limit": 0}))
    assert list(Draft202012Validator(table).iter_errors({"unknown": 1}))
