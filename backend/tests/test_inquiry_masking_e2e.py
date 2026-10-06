"""审批质询的遮罩收口，端到端（engine/inquiry.py 经核查 agent 的工具边界）：

- 分档写在子查询、WITH 里、外层按档位分组：档位列交给模型、存下、渲染进回答都照常，标签不当遮罩值，SQL 照原样封存；
  以前回答成了「已遮罩有 3 笔，已遮罩有 2 笔。」
- 交给质询 agent 的证据摘录先遮：报告取数把遮罩列改了名字选出来（buyer_phone AS 手机号），摘录里不给这一列；别的列、
  SQL 里写着的遮罩值（换了写法的号码也算）换成「已遮罩」。以前摘录只按列名遮，号码原样交给模型

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，区域、金额、手机号都是虚构的。
"""
from __future__ import annotations

import json

import pytest

from app.core import artifact_store
from tests.test_inquiry_api import (  # noqa: F401 - engine_up 是自动用上的夹具，client、big_shop 是夹具
    AMOUNTS,
    Models,
    Play,
    ask,
    big_shop,
    client,
    engine_up,
    events,
    fetching,
    pause,
    pending,
)

BAND = "CASE WHEN amount >= 20000 THEN '大单' ELSE '小单' END"


@pytest.mark.parametrize("sql", [
    pytest.param(f"SELECT 档位, COUNT(*) AS 笔数 FROM (SELECT *, {BAND} AS 档位 FROM sales) GROUP BY 档位 ORDER BY 档位",
                 id="子查询里 * 加分档"),
    pytest.param(f"WITH b AS (SELECT {BAND} AS 档位 FROM sales) SELECT 档位, COUNT(*) AS 笔数 FROM b GROUP BY 档位 "
                 "ORDER BY 档位", id="WITH 里只有分档"),
])
async def test_a_band_grouped_outside_its_derived_source_reads_as_written(monkeypatch, big_shop, client, sql):
    """金额设了遮罩，问两万元上下各有几笔：分档写在派生来源里、外层按档位分组。模型看得到档位、回答里引用的档位和它写的
    「大单」「小单」都照原样，SQL 照原样封存，质询工件里档位列照常显示。"""
    said = "[[v:Q1.r0.档位]]有 [[v:Q1.r0.笔数]] 笔，[[v:Q1.r1.档位]]有 [[v:Q1.r1.笔数]] 笔，大单少于小单。[[see:Q1]]"
    models = Models(monkeypatch, big_shop, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]",
                    plays={"两万元上下": Play([sql], said)})
    paused = await pause(big_shop, graph=fetching(big_shop, AMOUNTS))
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="两万元上下各有几笔？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert (item["answer"]["ok"], item["answer"]["text"]) == (True, "大单有 3 笔，小单有 2 笔，大单少于小单。")
    [query] = item["queries"]
    assert query["sql"] == sql, "SQL 里的标签不当遮罩值换"
    [event] = await events(paused.id, "approval.inquiry")
    assert event.data["queries"][0]["sql"] == sql
    snapshot = artifact_store.load(query["artifact"])
    assert snapshot["rows"] == [["大单", 3], ["小单", 2]] and snapshot["masked_at_query"] == []
    seen = [str(m.content) for messages in models.finals for m in messages]
    assert any('r0：档位="大单"，笔数=3' in text for text in seen), "模型看得到档位"
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
    assert got.status_code == 200, got.text
    assert (got.json()["content"]["rows"], got.json()["masked"]) == ([["大单", 3], ["小单", 2]], [])
