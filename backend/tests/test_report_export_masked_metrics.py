"""报告导出成 HTML 时，取自遮罩格的口径卡指标不随文件出去（报告导出与可验证报告的遮罩改造合并时补上）。

报告导出照文档原样渲染正文、按证据面板的拼法嵌出处链。现在的报告撰写节点把这类指标写成「已遮罩」，导出的文件里本来就
没有原值；升级前写的老文档（或写完之后数据源才设的遮罩）证据目录里还带着原值，正文引用了的话正文里也是——可验证报告把
这样的文档整份只给哈希，报告导出同样会拿给系统外的人，按同一个判断把引用了它们的片段写成「已遮罩」，正文、出处链里都
不出现原值；普通指标照常。

真跑的部分用 tests/test_report_masked_metrics.py 的虚构会员库（号码是编的），写作者是 MockChatModel 照剧本写。
"""
from __future__ import annotations

import json
import re

from tests.test_report_masked_metrics import (  # noqa: F401 - 夹具按名字引入
    CITES,
    CLEAN,
    client,
    engine_up,
    finish,
    graph,
    leaks,
    script,
    vip,
)


async def _export(client, run_id: str) -> tuple[str, dict]:
    res = await client.get(f"/api/runs/{run_id}/report.html")
    assert res.status_code == 200, res.text
    html = res.text
    block = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)
    assert block, "页面里有数据块"
    return html, json.loads(block.group(1))


def _metric_step(data: dict, metric: str) -> dict:
    return next(step for chain in data["chains"].values() for step in chain
                if step.get("step") == "metric" and step.get("metric") == metric)


async def test_a_new_doc_exports_without_the_value(client, monkeypatch, vip):
    """现在的报告撰写节点写的文档（写作者坚持引用了 [[m:phone_raw]]，标注照常产出）：正文、出处链里都是「已遮罩」。"""
    script(monkeypatch, CITES)
    row = await finish(graph(on_violation="flag"))
    assert row.status == "succeeded", row.error
    html, data = await _export(client, row.id)
    assert "手机号 </span>" in html and ">已遮罩</button>" in html
    step = _metric_step(data, "phone_raw")
    assert (step["value"], step["rendered"], step["render_ok"]) == ("已遮罩", "已遮罩", True), step
    assert leaks(html) == []


async def test_an_old_doc_exports_the_masked_metric_as_masked(client, monkeypatch, vip):
    """升级前的报告撰写节点写的文档（这里把写作前认遮罩的那一步换成什么都不认）：目录、正文里写着号码。导出时引用它的那一处
    写「已遮罩」，出处链的值、渲染也是，渲染一致；同一句里的普通指标（客户编号）照常；文件里一个号码都没有。"""
    import app.api.evidence as evidence_api

    async def nothing(*_args, **_kwargs):
        return []

    real = evidence_api.withheld_metrics
    monkeypatch.setattr(evidence_api, "withheld_metrics", nothing)
    script(monkeypatch, CITES)
    row = await finish(graph(on_violation="flag"))
    assert row.status == "succeeded", row.error
    monkeypatch.setattr(evidence_api, "withheld_metrics", real)
    html, data = await _export(client, row.id)
    assert ">已遮罩</button>" in html and "title=\"数字「已遮罩」：点开查看出处\"" in html
    step = _metric_step(data, "phone_raw")
    assert (step["value"], step["rendered"], step["render_ok"]) == ("已遮罩", "已遮罩", True), step
    first = _metric_step(data, "first_id")
    assert (first["value"], first["rendered"], first["render_ok"]) == (1, "1", True), first
    assert leaks(html) == []


async def test_ordinary_metrics_export_as_before(client, monkeypatch, vip):
    """只引用普通指标的报告：导出照旧，客户数、编号照写，出处链照常；文件里没有号码（会员查询的行里号码列写「已遮罩」）。"""
    script(monkeypatch, CLEAN)
    row = await finish(graph(on_violation="flag"))
    assert row.status == "succeeded", row.error
    html, data = await _export(client, row.id)
    assert ">3位</button>" in html and ">1</button>" in html
    assert _metric_step(data, "n_cust")["value"] == 3
    assert leaks(html) == []
