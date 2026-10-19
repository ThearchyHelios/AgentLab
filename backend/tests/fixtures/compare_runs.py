"""运行对比的虚构运行（tests/test_compare_engine.py、tests/test_compare_api.py 共用）。

两次周报运行照真实运行的事件形状手写（区域、金额、日期都是编的）：取数节点 fetch 查一条按区域汇总的 SQL，Agent 节点
bot 自己再查一次、把各区域的金额作为数组字段 amounts 交给口径卡，口径卡算几个指标。工件按内容寻址放在字典里，
接口测试把它们原样落进工件库、事件落进运行记录。
"""
from __future__ import annotations

import json

from app.core.artifact_store import canonical_json, content_hash
from app.engine import compare, recompute
from app.engine.evidence import render_number
from app.engine.expressions import parse_expression, substitute
from app.engine.merge_query import MERGE_SOURCE

TOOL = "db_query__shop"
WEEK36 = ("2026-08-31", "2026-09-06")
WEEK37 = ("2026-09-07", "2026-09-13")
COLUMNS = ["region", "gmv", "refund"]
TYPES = {"region": "text", "gmv": "number", "refund": "number"}
BASE_ROWS = [["东区", 1200.5, 100.0], ["南区", 800.0, 50.0], ["西区", 600.0, 0.0]]
NEXT_ROWS = [["东区", 1500.5, 120.0], ["南区", 700.0, 40.0], ["西区", 600.0, 10.0]]
EAST = "cell(nodes.fetch, 0, 'gmv')"
REFUND = "cell(nodes.fetch, 0, 'refund')"


def region_sql(days=WEEK36, extra=""):
    return ("SELECT region, SUM(amount) AS gmv, SUM(refund) AS refund FROM orders "
            f"WHERE day BETWEEN '{days[0]}' AND '{days[1]}'{extra} GROUP BY region ORDER BY region")


def node(nid, ntype, label, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def weekly_graph(sql=None, *, between=(), after_fetch=(), card_label="周报口径卡"):
    """一条链：开始 → 取数 → [after_fetch] → Agent → [between] → 口径卡 → 写周报 → 成果。after_fetch 里的节点不在
    Agent 的下游（它的上游只有取数节点），between 里的在。"""
    return chain(
        node("start", "input", "开始"),
        node("fetch", "tool", "区域取数", tool=TOOL, args={"sql": sql or region_sql()}),
        *after_fetch,
        node("bot", "agent", "区域分析", prompt="按区域列出金额", tools=[TOOL], cite_fields=True),
        *between,
        node("card", "metrics", card_label, caliber="周报口径", caliber_version="v1"),
        node("write", "report", "写周报"),
        node("out", "output", "成果"),
    )


class Fake:
    """一次虚构的运行：工件按内容寻址放在字典里，事件照真实运行的形状写（tool.start / tool.end / node.finished
    的证据台账、口径卡的 metric_set 条目、节点产出工件）。"""

    def __init__(self, run_id, *, graph=None, version=3, inputs=None, masked=None, source="shop"):
        self.id = run_id
        self.source = source
        self.graph = graph or weekly_graph()
        self.version = version
        self.inputs = inputs or {}
        self.masked = masked or {}
        self.store: dict[str, object] = {}
        self.tampered: set[str] = set()
        self.events: list[tuple[int, str, str | None, dict]] = []
        self.cards: list[tuple[str, str, dict]] = []
        self.queries: list[str] = []
        self.elapsed = 0
        self.add("run.started", None, {"nodes": len(self.graph["nodes"])})

    def put(self, obj):
        text = canonical_json(obj)
        artifact = content_hash(text)
        self.store[artifact] = json.loads(text)
        return artifact

    def load(self, artifact):
        if artifact in self.tampered:
            raise ValueError("工件内容与哈希不符，疑似被篡改")
        return self.store.get(artifact)

    def add(self, etype, node_id, data):
        self.events.append((len(self.events) + 1, etype, node_id, data))

    def snapshot(self, sql, columns, rows, *, truncated=False, types=None, masks=None):
        self.elapsed += 1      # 真实快照带着耗时：同一条 SQL 查两次也是两件工件
        snap = {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated,
                "elapsed_ms": self.elapsed, "sql": sql, "source": self.source,
                **({"column_types": types} if types else {}), **({"mask_columns": masks} if masks else {})}
        artifact = self.put(snap)
        self.queries.append(artifact)
        return artifact

    def entry(self, node_id, artifact, columns, rows, *, tool=TOOL, call_id=None, truncated=False):
        return {"kind": "query", "node_id": node_id, "exec": 1, "artifact": artifact, "tool": tool,
                "source": self.source,
                "columns": columns, "rows": len(rows), "truncated": truncated,
                **({"call_id": call_id} if call_id else {})}

    def tool(self, node_id, sql, columns=COLUMNS, rows=BASE_ROWS, *, tool=TOOL, truncated=False, types=TYPES,
             masks=None):
        """调用工具节点查一次库。masks 是查询当时数据源设的遮罩（记进快照的 mask_columns）。"""
        artifact = self.snapshot(sql, columns, rows, truncated=truncated, types=types, masks=masks)
        output = self.put({"columns": columns, "rows": rows, "sql": sql, "source": self.source, "artifact": artifact})
        self.add("node.started", node_id, {"node_type": "tool"})
        self.add("tool.start", node_id, {"tool": tool, "args": {"sql": sql}})
        self.add("tool.end", node_id, {"tool": tool, "duration_ms": 3, "query_artifact": artifact})
        self.add("node.finished", node_id, {"duration_ms": 4, "attempt": 1, "artifact": output, "evidence": [
            self.entry(node_id, artifact, columns, rows, tool=tool, truncated=truncated)]})
        return artifact

    def agent(self, node_id, sql, columns=COLUMNS, rows=BASE_ROWS, *, types=TYPES, truncated=False, masks=None,
              verify=(), column="gmv", span=None):
        """Agent 节点自己查一次库，按出处交回数组字段 amounts（默认是快照 gmv 列的全部行；column、span 改引用的列和
        行段）。verify 是同一节点里核查类的查询（role: verify，不进台账）。"""
        artifact = self.snapshot(sql, columns, rows, truncated=truncated, types=types, masks=masks)
        first, last = span or (0, len(rows) - 1)
        at = columns.index(column)
        data = {"amounts": [r[at] for r in rows[first:last + 1]]}
        evidence = {"amounts": {"ref": f"Q1.r{first}-{last}.{column}", "call": "Q1", "artifact": artifact,
                                "locator": {"rows": [first, last], "column": column}, "status": "verified"}}
        output = self.put({"text": "按区域列出了金额", "data": data, "data_evidence": evidence})
        self.add("node.started", node_id, {"node_type": "agent"})
        self.add("tool.start", node_id, {"tool": TOOL, "args": {"sql": sql}, "call_id": "c1"})
        self.add("tool.end", node_id, {"tool": TOOL, "call_id": "c1", "duration_ms": 5, "query_artifact": artifact})
        for i, extra in enumerate(verify):
            check = self.snapshot(extra, ["n"], [[1]])
            self.add("tool.end", node_id, {"tool": TOOL, "call_id": f"v{i}", "role": "verify", "query_artifact": check})
        self.add("node.finished", node_id, {"duration_ms": 9, "attempt": 1, "artifact": output, "evidence": [
            self.entry(node_id, artifact, columns, rows, call_id="c1", truncated=truncated)]})
        return artifact

    def plain_agent(self, node_id, sqls, data, columns=COLUMNS, rows=BASE_ROWS, *, types=TYPES):
        """没开按出处核对的 Agent：查几次库，交回的 data 没有出处（data_evidence），口径卡只知道是它写的。"""
        self.add("node.started", node_id, {"node_type": "agent"})
        entries = []
        for i, sql in enumerate(sqls):
            artifact = self.snapshot(sql, columns, rows, types=types)
            self.add("tool.start", node_id, {"tool": TOOL, "args": {"sql": sql}, "call_id": f"c{i}"})
            self.add("tool.end", node_id, {"tool": TOOL, "call_id": f"c{i}", "duration_ms": 5,
                                           "query_artifact": artifact})
            entries.append(self.entry(node_id, artifact, columns, rows, call_id=f"c{i}"))
        output = self.put({"text": "按区域分析了金额", "data": data})
        self.add("node.finished", node_id, {"duration_ms": 9, "attempt": 1, "artifact": output, "evidence": entries})

    def merge(self, node_id, sql, inputs, columns, rows, *, types=None):
        """合并查询：几份上游快照按别名落进内存库，再执行一条合并 SQL。快照里记着各个输入（别名、节点、快照），
        事件照合并节点的形状写（merge.end 加 node.finished 台账里的 query 条目，没有 tool.end）。
        inputs 是 [(别名, 上游节点, 上游快照)]。"""
        snap = {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False, "sql": sql,
                "source": MERGE_SOURCE, "sources": [self.source],
                "inputs": [{"alias": alias, "node_id": upstream, "artifact": artifact,
                            "rows": len(self.store[artifact]["rows"]), "columns": self.store[artifact]["columns"],
                            "source": self.store[artifact].get("source")} for alias, upstream, artifact in inputs],
                "warnings": [], **({"column_types": types} if types else {})}
        artifact = self.put(snap)
        self.queries.append(artifact)
        output = self.put({**snap, "artifact": artifact})
        view = [{k: v for k, v in item.items() if k != "columns"} for item in snap["inputs"]]
        self.add("node.started", node_id, {"node_type": "merge"})
        self.add("merge.end", node_id, {"inputs": view, "sql": sql, "rows": len(rows), "query_artifact": artifact})
        self.add("node.finished", node_id, {"duration_ms": 2, "attempt": 1, "artifact": output, "evidence": [
            {"kind": "query", "node_id": node_id, "exec": 1, "artifact": artifact, "source": MERGE_SOURCE,
             "columns": columns, "rows": len(rows), "truncated": False}]})
        return artifact

    def code(self, node_id, value, var):
        """沙箱代码节点算出一个数写进变量：它不查库，对不上具体的查询。"""
        output = self.put({var: value})
        self.add("node.started", node_id, {"node_type": "code"})
        self.add("node.finished", node_id, {"duration_ms": 2, "attempt": 1, "artifact": output})

    def card(self, metrics, *, node_id="card", caliber="周报口径", version="v1", runs=1):
        card = {"kind": "metric_set", "caliber": caliber, "caliber_version": version, "metrics": metrics}
        artifact = self.put(card)
        for _ in range(runs):
            self.add("node.started", node_id, {"node_type": "metrics"})
            self.add("node.finished", node_id, {"duration_ms": 1, "attempt": 1, "artifact": self.put({**card, "text": "…"}),
                                                "evidence": [{"kind": "metric_set", "node_id": node_id, "exec": 1,
                                                              "artifact": artifact, "caliber": caliber,
                                                              "version": version}]})
        self.cards.append((node_id, artifact, card))
        return artifact

    def side(self):
        def sources(started, card_node=""):
            outputs = {}
            for seq, etype, nid, data in self.events:
                if seq < started and etype == "node.finished" and data.get("artifact"):
                    outputs[nid] = data["artifact"]
            return recompute.Sources(load=self.load, queries=tuple(self.queries), outputs=outputs, masked=self.masked)

        run = {"id": self.id, "workflow_name": "区域周报", "version": self.version, "run_class": "formal",
               "started_at": "2026-09-07T01:00:00+00:00", "finished_at": "2026-09-07T01:02:00+00:00",
               "schedule_id": None}
        return compare.Side(run=run, graph=self.graph, events=self.events, cards=self.cards, load=self.load,
                            masked=self.masked, sources=sources, dialects={self.source: "sqlite"}, inputs=self.inputs)


def cell_in(row, column, value, artifact, node_id="fetch"):
    return {"path": f"cell(nodes.{node_id}, {row}, '{column}')", "value": value, "node_id": node_id, "via": "tool_cell",
            "locator": {"row": row, "column": column}, "artifact": artifact,
            "eid": f"ev:cell:{artifact[:8]}:{row}:{column}", "status": "verified"}


def amounts_in(artifact, rows, *, column="gmv", span=None):
    first, last = span or (0, len(rows) - 1)
    return {"path": "vars.kpi.amounts", "value": f"[{last - first + 1} 项]", "node_id": "bot", "via": "agent_field",
            "field": "amounts", "ref": f"Q1.r{first}-{last}.{column}", "artifact": artifact,
            "locator": {"rows": [first, last], "column": column}, "eid": f"ev:rows:{artifact[:8]}:{first}:{column}",
            "status": "verified"}


def scalar_in(path, value, node_id, via):
    return {"path": path, "value": value, "node_id": node_id, "via": via, "field": path.split(".", 2)[-1],
            "status": "ok" if value is not None else "missing"}


def metric(mid, expression, value, inputs, *, name=None, unit="元", decimals=1, fmt="thousands"):
    tree = parse_expression(expression)[0]
    scalars = {i["path"]: i["value"] for i in inputs if not str(i["value"]).endswith(" 项]")}
    return {"id": mid, "name": name or mid, "unit": unit, "value": value, "expression": expression,
            "decimals": decimals, "format": fmt,
            "rendered": render_number(value, unit=unit, decimals=decimals, fmt=fmt),
            "inputs": inputs, "substituted": substitute(tree, scalars), "recompute_ok": True,
            "status": "ok" if value is not None else "missing_input"}


def week(run_id, rows=BASE_ROWS, *, sql=None, metrics=None, graph=None, version="v1", bot_rows=None, **kw):
    """一次周报运行：fetch 和 bot 各查一次，口径卡算 east（东区金额）和 total（各区域合计）。
    metrics(fetch, bot, rows) 返回口径卡的指标，不给就是这两个。"""
    run = Fake(run_id, graph=graph or weekly_graph(sql), **kw)
    sql = sql or region_sql()
    fetch = run.tool("fetch", sql, rows=rows)
    bot_rows = bot_rows if bot_rows is not None else rows
    bot = run.agent("bot", sql, rows=bot_rows)
    if metrics is None:
        metrics = default_metrics
    run.card(metrics(fetch, bot, rows, bot_rows), version=version)
    return run


def default_metrics(fetch, bot, rows, bot_rows):
    total = sum(r[1] for r in bot_rows)
    return [metric("east", EAST, rows[0][1], [cell_in(0, "gmv", rows[0][1], fetch)], name="东区金额"),
            metric("total", "sum(vars.kpi.amounts)", total, [amounts_in(bot, bot_rows)], name="各区域合计")]
