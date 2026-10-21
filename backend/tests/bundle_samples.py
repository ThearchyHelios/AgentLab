"""可验证报告的虚构样本页：给 scripts/check-bundle.mjs 在浏览器里核对（pytest 不执行页面上的核对脚本）。

    python -m tests.bundle_samples <目录>

在目录里写下几份页面（<名字>.html）。每份都是 build_bundle + render_html 真实生成的，封存哈希按 runner 的算法
（artifact_store.manifest_hash）对事件真实算出，报告文档用 compose_doc 按证据目录真实组装（标题、段落、列表、
表格、粗体、斜体，数字和取值的引用都和真实运行一样）。check-bundle 断网打开它们，逐项断言核对结果和原因，再在
这几份上做篡改。这样遮罩快照、多份报告、早期运行、服务端缺文件这些分支，不论真数据里有没有，都有人在浏览器里核过。

数据全是虚构的：门店、会员、金额都是编的。

- base：一份报告，引用口径卡、门店流水（没有遮罩）、会员消费（有遮罩）各一处，一个没有出处的数，一张门店流水整表
  （数字列右对齐，数字与取值共 21 个，超过折叠的条数）；一条封存之后追加的按需裁判
- two-reports：另有一份月报，两份报告各自的引用链
- metric-only：只引用口径卡指标的报告——正文没有一处 [[v:]]，门店流水的快照从口径卡的输入这一条路随文件附带，
  引用链（④）按「已核对的口径卡的输入」认它；点开数字看得到指标、原式、代入式、输入和复算
- legacy：早期运行，封存范围按结束事件推算（运行结束事件在范围外，运行结果无法核对）
- failed：失败结束的运行（没有出具判定），封存说明多一句「继续运行会重新封存」
- rejected：被审批人驳回结束的运行（运行记录 cancelled + end_reason=rejected，最后一条是运行取消）：运行结果写「被驳回」，
  写明能核对的只是「运行取消」这一半、被驳回取自运行记录；不提重新封存
- continued：早期运行先失败、经继续运行跑到成功：范围里有那条运行失败，之后运行又继续了，成功结束的那条在范围外
- degraded：两条出具判定，先完整出具、后降档出具，认最后一条
- missing-doc / missing-card：导出时服务端已没有报告文档 / 口径卡的文件
- masked-card：口径卡上两个指标的输入取自会员消费的号码列（设了遮罩）：联系电话位数、首位会员手机号（值就是号码），卡上
  记着号码原值。整张卡只给 id，引用它的数字无法核对；卡的节点完成事件（预览里逐条写着「指标（名称）= 值」）只给哈希；
  报告文档按现在的报告撰写节点写，证据目录里这两个指标写「已遮罩」，照常随文件附带
- masked-old-doc：同一张卡，报告文档是之前的报告撰写节点写的，证据目录里这两个指标写着原值：整份文档只给哈希、不附带它
  引用的工件，报告的节点完成事件也只给哈希，页面照实说明原因
- masked-source：会员库设了号码遮罩（查询当时记下了，数据源现在也设着）。报告引用会员按地区汇总的查询（没碰遮罩列：
  快照随文件附带，数字核对得过）和选出了号码的那份会员消费（只给哈希）
- nan：运行开始事件和门店流水快照里有 json.dumps 写出的裸 NaN（快照里那一列报告没有引用）
- stray：报告自查记下的文档是另一次运行写的
- formats：经营月报，每种显示格式各引用一处：口径卡的千分位加货币单位、换算成万 / 亿、比率显示成百分数、负数、不分组、
  小数位与 |.N / |int、pct；流水快照里的文字取值（地区）、DECIMAL 落成的数字文本、负数、换算成万、超过 2^53 的订单号、
  ISO 日期时间（文档记下 2 版显示规则）。check-bundle 在它上面只改显示文字做伪造，tests/test_bundle_shown.py 在 node 里
  逐个格式核一遍
- formats-unit：同经营月报，销售额的单位带尾随空白（「元 」：单位输入框不去空白，指标节点照存），正文显示「74,613.65元 」，
  原样导出照样全部通过
- prose：同经营月报的口径卡和流水，正文是写作者自己写在引用旁边的普通文字（「运费统一[[m:…]]」「合计——[[m:…]]」
  「SKU-[[v:…]]」「共[[m:…]]家」这类）：原样导出全部通过、总览是绿的，紧挨着核对的规则不误判正常写法
- analysis：异动周报（分析能力一轮）。Agent 在虚构零售库 shop 上查了两周的总数（Q1）和按地区两周的销售额（Q2），做了按地区的
  变化拆解（A1，带总数核对）、波动基线（A2，两周历史不够，算不了）、两期可比性检查（A3），结论都是 app.analysis 真算的；
  报告引用 A1 的判定语、具名的数（占比）、结果表的格，正文后面是系统写的「分析范围」（engine/analysis_scope 按虚构的数据
  目录算出没拆的渠道）。结论和输入快照随文件附带、导出时复算过，原样导出全部通过；分析范围里的编号点得开（A2、A3 正文没引用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from app.bundle.build import build_bundle, render_html
from app.core.artifact_store import canonical_json, content_hash, manifest_hash
from app.engine.evidence import build_catalog, compose_doc, withhold_metrics

RUN_ID = "5a3c0f4b9d2e4e1f8b7a6c5d4e3f2a1b"
RUN = {"id": RUN_ID, "workflow_id": "wf-sample-weekly", "workflow_name": "门店周报", "version": 3,
       "version_hash": "ab" * 32, "run_class": "formal", "status": "succeeded",
       "started_at": "2026-09-07T01:00:00+00:00", "finished_at": "2026-09-07T01:02:03+00:00"}
GRAPH = {"nodes": [{"id": n, "type": t, "data": {"label": label}} for n, t, label in [
    ("start", "input", "开始"), ("fetch", "tool", "门店流水"), ("members", "tool", "会员消费"),
    ("regions", "tool", "会员地区汇总"), ("card", "metrics", "周报口径"), ("write", "report", "写周报"),
    ("monthly", "report", "写月报"), ("out", "output", "成果")]]}
EXPORTED_AT = "2026-10-05T08:00:00+00:00"

STORES = [["青石店", 1280.5], ["白沙店", "960.00"], ["临川店", 732], ["望江店", 1105.25], ["枫桥店", 418],
          ["云岭店", 2210], ["石桥店", 655.5], ["柳湾店", 980]]
CRM = {"columns": ["name", "phone", "spend"], "column_types": {"name": "text", "phone": "text", "spend": "number"},
       "rows": [["林一", "000-0001", 320.0], ["周二", "000-0002", 188.5]], "source": "crm",
       "sql": "SELECT name, phone, spend FROM members ORDER BY id", "truncated": False, "mask_columns": ["phone"]}
#: 会员按地区汇总：同一个设了号码遮罩的会员库，SQL 没碰号码列
REGIONS = {"columns": ["region", "spend"], "column_types": {"region": "text", "spend": "number"},
           "rows": [["华东", 320.0], ["华南", 188.5]], "source": "crm",
           "sql": "SELECT region, SUM(spend) AS spend FROM members GROUP BY region ORDER BY region", "truncated": False,
           "mask_columns": ["phone"]}


def _num(value: Any) -> str:
    """代入式里的一个数：照 Python 字面量的样子（整型不带 .0）；DECIMAL 落成的数字文本原样。"""
    if isinstance(value, str):
        return value
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def card_of(shop_id: str) -> dict[str, Any]:
    """周报口径卡：销售额是八家门店流水之和。和真实口径卡一样带着计算方式（原式、代入式）、每个输入
    （取自哪次查询的哪一格）和复算结果——可验证报告点开数字时据此展示，引用链据此附上输入指向的查询快照。"""
    cells = [f"cell(nodes.fetch, {i}, 'amount')" for i in range(len(STORES))]
    gmv = {"id": "gmv", "name": "销售额", "value": round(sum(float(v) for _, v in STORES), 2), "unit": "元",
           "decimals": 2, "format": "thousands", "expression": " + ".join(cells),
           "substituted": " + ".join(_num(v) for _, v in STORES), "recompute_ok": True,
           "inputs": [{"path": f"cell(nodes.fetch, {i}, 'amount')", "value": v, "node_id": "fetch",
                       "via": "tool_cell", "artifact": shop_id, "locator": {"row": i, "column": "amount"},
                       "status": "verified"}
                      for i, (_, v) in enumerate(STORES)]}
    return {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v2", "metrics": [gmv]}


def masked_card(card: dict[str, Any], crm_id: str) -> dict[str, Any]:
    """口径卡多两个指标：联系电话位数、首位会员手机号，输入都是会员消费第 0 行的号码（phone 设了遮罩），卡上记着号码原值
    和代入式；首位会员手机号的值就是号码。"""
    cell, phone = "cell(nodes.members, 0, 'phone')", CRM["rows"][0][1]
    read = {"path": cell, "value": phone, "node_id": "members", "via": "tool_cell", "artifact": crm_id,
            "locator": {"row": 0, "column": "phone"}, "status": "verified"}
    contact = {"id": "contact", "name": "联系电话位数", "value": 8, "unit": "位", "decimals": 0, "format": "thousands",
               "expression": f"len({cell})", "substituted": f"len('{phone}')", "inputs": [dict(read)]}
    first = {"id": "phone_raw", "name": "首位会员手机号", "value": phone, "unit": "", "decimals": None, "format": "plain",
             "expression": cell, "substituted": f"'{phone}'", "inputs": [dict(read)]}
    return {**card, "metrics": [*card["metrics"], contact, first]}


WEEKLY = ("## 门店周报\n\n本周销售额 [[m:gmv]]，*按发生周统计*。青石店流水 [[v:Q1.r0.amount]]，白沙店 [[v:Q1.r1.amount]]，"
          "**会员消费** [[v:Q2.r0.spend]]，另有 42 笔退款待入账。[[see:Q1]]\n\n- 云岭店流水最高\n- 枫桥店流水最低\n\n"
          "[[table:Q1 cols=store,amount rows=0-7]]")
MONTHLY = "## 门店月报\n\n本月销售额 [[m:gmv]]，青石店流水 [[v:Q1.r0.amount]]。[[see:Q1]]"

#: 经营月报的口径卡：每个指标一种显示格式（千分位加货币单位、不分组、比率显示成百分数、负数、小数位、不写小数位）
FORMATS_CARD = {"kind": "metric_set", "caliber": "经营月报口径", "caliber_version": "v1", "metrics": [
    {"id": "gmv", "name": "销售额", "value": 74613.65, "unit": "元", "decimals": 2, "format": "thousands"},
    {"id": "annual", "name": "年度销售额", "value": 1234567890.5, "unit": "元", "decimals": 2, "format": "thousands"},
    {"id": "refund", "name": "退款", "value": -3120.5, "unit": "元", "decimals": 2, "format": "thousands"},
    {"id": "conv_rate", "name": "转化率", "value": 0.0235, "unit": "", "decimals": 4, "format": "percent_of_ratio"},
    {"id": "growth", "name": "环比", "value": -0.0815, "unit": "", "decimals": 3, "format": "percent_of_ratio"},
    {"id": "orders", "name": "订单数", "value": 12840, "unit": "单", "decimals": 0, "format": "plain"},
    {"id": "avg", "name": "客单价", "value": 5.8113, "unit": "元", "decimals": 2, "format": "thousands"},
    {"id": "share", "name": "线上占比", "value": 0.346, "unit": "", "decimals": None, "format": "thousands"},
    {"id": "export", "name": "出口额", "value": 15230.4, "unit": "美元", "decimals": 1, "format": "thousands"},
]}
#: 同上，销售额的单位带尾随空白：服务端照原样接在数后面
FORMATS_UNIT_CARD = {**FORMATS_CARD, "metrics": [{**FORMATS_CARD["metrics"][0], "unit": "元 "},
                                                 *FORMATS_CARD["metrics"][1:]]}
#: 经营流水：地区是文字，金额一格是 DECIMAL 落成的数字文本、一格是负数，订单号超过 2^53，付款时间是 ISO 日期时间
LEDGER = {"columns": ["region", "gmv", "order_no", "paid_at"],
          "column_types": {"region": "text", "gmv": "number", "order_no": "number", "paid_at": "datetime"},
          "rows": [["华南", "45678.50", 202609071234567890, "2026-09-07T08:21:39"],
                   ["华东", -1200.75, 202609071234567891, "2026-09-08T19:02:11"]],
          "source": "shop", "sql": "SELECT region, gmv, order_no, paid_at FROM ledger ORDER BY region DESC",
          "truncated": False}
FORMATS = ("## 经营月报\n\n本月销售额 [[m:gmv]]（约 [[m:gmv|万]]），年度累计 [[m:annual|亿]]，退款 [[m:refund]]。"
           "转化率 [[m:conv_rate]]，环比 [[m:growth]]，订单 [[m:orders]]。客单价 [[m:avg]]，精确些是 [[m:avg|.3]]，"
           "取整是 [[m:avg|int]]；线上占比 [[m:share|pct]]，出口额 [[m:export]]。\n\n"
           "[[v:Q1.r0.region]]流水 [[v:Q1.r0.gmv]]（约 [[v:Q1.r0.gmv|万]]），[[v:Q1.r1.region]]冲减 [[v:Q1.r1.gmv]]。"
           "最大一单的订单号 [[v:Q1.r0.order_no]]，付款时间 [[v:Q1.r0.paid_at]]。[[see:Q1]]")
#: 经营月报的另一种写法：写作者自己写在引用旁边的普通文字——「统一」后面紧跟数（汉字「一」不是负号）、破折号（——、—、
#: ―，小号的）后面紧跟数、段内软换行紧挨着数（页面上显示成一个空格）、金额前面的「￥」「¥」、编号「SKU-」「A-」后面紧跟数
#: （连字符前面是字母，是编号的一部分）、「共…家」、全角括号、「NO.」后面跟数、假名长音和制表符横线、粗体斜体包着的数。
#: 原样导出要全部通过、总览是绿的。en dash（–）紧挨在数前面读起来像负号，判无法核对，不在这里
PROSE = ("## 正文\n\n全场运费统一[[m:orders]]，消费合计——[[m:gmv]]，退款—[[m:refund]]，转化率\n[[m:conv_rate]]，"
         "环比―[[m:growth]]，客单价\ufe58[[m:avg]]，折合\uffe5[[m:avg]]、\u00a5[[m:avg|int]]。\n\n"
         "最大一单 SKU-[[v:Q1.r0.order_no]]，编号A-[[m:orders]]，共[[m:orders]]家，线上占比（[[m:share|pct]]），"
         "排名NO.[[m:orders]]，出口额ー[[m:export]]，年度─[[m:annual|亿]]，**[[m:gmv]]**，*[[v:Q1.r0.gmv]]*。")
#: 会员周报：Q3 是会员按地区汇总（没碰遮罩列），Q2 是选出了号码的会员消费
MEMBERS = ("## 会员周报\n\n华东会员消费 [[v:Q3.r0.spend]]，华南 [[v:Q3.r1.spend]]。[[see:Q3]]\n\n"
           "会员林一消费 [[v:Q2.r0.spend]]。")
#: 只引用指标的报告：正文没有一处 [[v:]]，门店流水的快照只能从口径卡的输入这一条路随文件附带（引用链 ④）
METRIC_ONLY = "## 门店周报\n\n本周销售额 [[m:gmv]]，各门店明细随后补上。"


def stored(obj: Any) -> tuple[str, bytes]:
    """照工件库的样子落一份：规范 JSON 的字节，id 是它的 sha256。"""
    raw = canonical_json(obj).encode()
    return content_hash(raw), raw


def shop(nan: bool = False) -> dict[str, Any]:
    """门店流水的查询快照。ratio 那一列报告不引用；nan=True 时第一行是浮点 NaN（json.dumps 写成裸的 NaN）。"""
    rows = [[name, amount, float("nan") if nan and i == 0 else round(0.05 * (i + 1), 2)]
            for i, (name, amount) in enumerate(STORES)]
    return {"columns": ["store", "amount", "ratio"],
            "column_types": {"store": "text", "amount": "number", "ratio": "number"}, "rows": rows, "source": "shop",
            "sql": "SELECT store, amount, ratio FROM orders ORDER BY id", "truncated": False}


#: 报告撰写节点把值取自遮罩格的指标写成的字（同 api/evidence.py 的 MASKED）
MASKED = "已遮罩"


#: masked_card 上值不给的指标（和报告撰写节点的写作目录同一个判断：输入确定取自遮罩格）
MASKED_METRICS = ("m:contact", "m:phone_raw")


def card_preview(card: dict[str, Any], artifact: str) -> dict[str, Any]:
    """口径卡节点完成事件的预览，照 metrics 节点产出的样子（compiler._preview）：text 里逐条写着「指标（名称）= 值」。"""
    lines = [f"- {m['id']}（{m['name']}）= {m['value']}{m.get('unit') or ''}" for m in card["metrics"]]
    return {"kind": "metric_set", "caliber": card["caliber"], "caliber_version": card["caliber_version"],
            "metrics": f"[{len(card['metrics'])} 项]",
            "text": f"指标清单（口径 {card['caliber']} @ {card['caliber_version']}）：\n" + "\n".join(lines),
            "artifact": artifact}


class Sample:
    """一次虚构的已封存运行：工件（原字节）、证据目录、报告文档、事件。masked=True 时口径卡带着取自遮罩列的输入；
    regions=True 时会员库另有一次按地区汇总的查询（目录里排在 Q3）。card、table 换掉口径卡和门店流水的快照（经营月报）；
    inputs 是运行输入（报告里写 [[i:字段]] 引用）。"""

    def __init__(self, *, nan: bool = False, masked: bool = False, regions: bool = False,
                 card: dict[str, Any] | None = None, table: dict[str, Any] | None = None,
                 inputs: dict[str, Any] | None = None) -> None:
        self.files: dict[str, bytes] = {}
        snapshot = table or shop(nan)
        self.shop_id = self.put(snapshot)
        self.crm_id = self.put(CRM)
        self.card = card or (masked_card(card_of(self.shop_id), self.crm_id) if masked else card_of(self.shop_id))
        self.card_id = self.put(self.card)
        self.entries = {
            "fetch": {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": self.shop_id,
                      "tool": "db_query__shop", "source": "shop", "columns": snapshot["columns"],
                      "rows": len(snapshot["rows"]), "truncated": False},
            "members": {"kind": "query", "node_id": "members", "exec": 1, "artifact": self.crm_id,
                        "tool": "db_query__crm", "source": "crm", "columns": CRM["columns"], "rows": len(CRM["rows"]),
                        "truncated": False},
        }
        if regions:
            self.entries["regions"] = {"kind": "query", "node_id": "regions", "exec": 1,
                                       "artifact": self.put(REGIONS), "tool": "db_query__crm", "source": "crm",
                                       "columns": REGIONS["columns"], "rows": len(REGIONS["rows"]), "truncated": False}
        self.entries["card"] = {"kind": "metric_set", "node_id": "card", "artifact": self.card_id}
        self.ledger = list(self.entries.values())
        self.catalog = build_catalog(nodes={"card": self.card}, ledger=self.ledger, inputs=inputs)

    def put(self, obj: Any) -> str:
        artifact, raw = stored(obj)
        self.files[artifact] = raw
        return artifact

    def load(self, artifact: str) -> Any:
        raw = self.files.get(artifact)
        return json.loads(raw) if raw is not None else None

    def doc(self, text: str, node_id: str, run_id: str = RUN_ID, *, masked: tuple[str, ...] = ()) -> str:
        """组装一份报告文档。masked：值不给的指标，照现在的报告撰写节点写成「已遮罩」（withhold_metrics）；不给的是之前的
        报告撰写节点写的样子（目录里原样写着值）。"""
        catalog = withhold_metrics(self.catalog, masked, MASKED)
        return self.put(compose_doc(text, catalog, node_id=node_id, run_id=run_id, loader=self.load))

    def events(self, reports: dict[str, str], *, ending: str = "finished", tiers: tuple[str, ...] = ("formal",),
               nan: bool = False) -> tuple[list[tuple[int, str, str | None, Any]], int]:
        """(全部事件, 封存范围)。封存范围到结束事件为止，后面再追加一条按需裁判。"""
        rows: list[tuple[int, str, str | None, Any]] = []

        def add(etype: str, node_id: str | None, data: dict[str, Any]) -> None:
            rows.append((len(rows) + 1, etype, node_id, data))

        add("run.started", None, {"nodes": len(GRAPH["nodes"]), "resumed": False, "memory_scope": "default",
                                  "collection": "default", "approval_default": None, "tool_trust": "formal",
                                  "agent_limits": None, "data_versions": None, "replay_protocol": 2,
                                  **({"sample_rate": float("nan")} if nan else {})})
        add("node.started", "start", {"node_type": "input", "label": "开始"})
        add("node.finished", "start", {"duration_ms": 1, "attempt": 1, "preview": {"week": "2026-W37"}})
        add("node.started", "fetch", {"node_type": "tool", "label": "门店流水"})
        add("tool.end", "fetch", {"tool": "db_query__shop", "duration_ms": 12, "query_artifact": self.shop_id,
                                  "preview": json.dumps({"columns": ["store", "amount"], "rows": STORES[:2]},
                                                        ensure_ascii=False)})
        add("node.finished", "fetch", {"duration_ms": 14, "attempt": 1, "preview": "{\"rows\": …}",
                                       "evidence": [self.entries["fetch"]]})
        add("tool.end", "members", {"tool": "db_query__crm", "duration_ms": 9, "query_artifact": self.crm_id,
                                    "preview": json.dumps(CRM["rows"][:1], ensure_ascii=False)})
        add("node.finished", "members", {"duration_ms": 10, "attempt": 1, "evidence": [self.entries["members"]]})
        if "regions" in self.entries:
            add("tool.end", "regions", {"tool": "db_query__crm", "duration_ms": 7,
                                        "query_artifact": self.entries["regions"]["artifact"],
                                        "preview": json.dumps(REGIONS["rows"], ensure_ascii=False)})
            add("node.finished", "regions", {"duration_ms": 8, "attempt": 1, "evidence": [self.entries["regions"]]})
        add("node.finished", "card", {"duration_ms": 3, "attempt": 1, "preview": card_preview(self.card, self.card_id),
                                      "evidence": [self.entries["card"]]})
        for node_id, doc_id in reports.items():
            add("llm.end", node_id, {"model": "mock", "output": "（写作草稿）"})
            add("report.checked", node_id, {"doc_artifact": doc_id, "ok": True})
            add("node.finished", node_id, {"duration_ms": 20, "attempt": 1,
                                           "preview": {"text": "（报告正文）", "doc_artifact": doc_id}})
        if ending == "finished":
            for tier in tiers:
                add("issuance", "out", {"tier": tier, "mode": "citations"})
            add("node.finished", "out", {"duration_ms": 2, "attempt": 1, "preview": {"周报": "（报告正文）"}})
            add("run.finished", None, {"output": {"周报": "（报告正文）"}})
        elif ending == "continued":
            # 先失败，再从成果节点继续运行到成功（runner.continue_failed 记一条 run.resumed）
            add("node.failed", "out", {"error": "出口复核没有通过"})
            add("run.failed", None, {"error": "出口复核没有通过"})
            add("run.resumed", None, {"from": ["out"], "message": "从「成果」继续运行，保留前面 6 个节点的结果",
                                      "actor": "值班分析员"})
            for tier in tiers:
                add("issuance", "out", {"tier": tier, "mode": "citations"})
            add("node.finished", "out", {"duration_ms": 2, "attempt": 1, "preview": {"周报": "（报告正文）"}})
            add("run.finished", None, {"output": {"周报": "（报告正文）"}})
        elif ending == "rejected":
            # 审批人在成果之前驳回（engine/runner：run.cancelled 带 reason=rejected、在哪一步、谁、备注；内容不随文件附带）
            add("node.failed", "out", {"error": "人工驳回：数字对不上"})
            add("run.cancelled", None, {"reason": "rejected", "node_id": "out", "label": "成果", "kind": "approve",
                                        "decision": "reject", "actor": "值班分析员", "note": "数字对不上"})
        else:
            add("node.failed", "out", {"error": "出口复核没有通过"})
            add("run.failed", None, {"error": "出口复核没有通过"})
        bound = len(rows)
        add("evidence.judged", next(iter(reports), None),
            {"verdicts": {"u1": {"verdict": "supported"}}, "model": "judge-x", "post_seal": True})
        return rows, bound


def build(sample: Sample, events: list[tuple[int, str, str | None, Any]], bound: int, *,
          run: dict[str, Any] | None = None, legacy: bool = False, drop: tuple[str, ...] = (),
          source_masks: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """真实封存哈希的页面数据（bundle-data）。drop：导出时服务端已没有文件的工件；source_masks：数据源现在设的遮罩。"""
    sealed = [e for e in events if e[0] <= bound]
    files = {k: v for k, v in sample.files.items() if k not in drop}
    return build_bundle(run or RUN, events, manifest_seq=bound, manifest_hash=manifest_hash(sealed), read=files.get,
                        exported_at=EXPORTED_AT, legacy=legacy, graph=GRAPH, source_masks=source_masks or {})


def formats_bundle(*, card: dict[str, Any] = FORMATS_CARD, table: dict[str, Any] = LEDGER, text: str = FORMATS,
                   inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """经营月报（formats）的页面数据：每种显示格式各引用一处。tests/test_bundle_shown.py 换掉口径卡、流水、正文或者
    加上运行输入，组装别的写法。"""
    f = Sample(card=card, table=table, inputs=inputs)
    events, bound = f.events({"write": f.doc(text, "write")})
    return build(f, events, bound)


#: 分析样本（analysis）的两份查询：虚构零售库 shop 两周的总数（Q1）、四个地区两周的销售额（Q2）
AN_TOTALS = {"columns": ["week", "gmv"], "column_types": {"week": "text", "gmv": "number"},
             "rows": [["2026-W41", 2220], ["2026-W42", 2026]], "source": "shop",
             "sql": "SELECT week, SUM(amount) AS gmv FROM orders GROUP BY week ORDER BY week", "truncated": False,
             "row_count": 2}
AN_REGIONS = {"columns": ["region", "week", "gmv"], "column_types": {"region": "text", "week": "text", "gmv": "number"},
              "rows": [["华东", "2026-W41", 820], ["华东", "2026-W42", 700], ["华北", "2026-W41", 600],
                       ["华北", "2026-W42", 560], ["华南", "2026-W41", 700], ["华南", "2026-W42", 666],
                       ["西南", "2026-W41", 100], ["西南", "2026-W42", 100]], "source": "shop",
              "sql": "SELECT region, week, SUM(amount) AS gmv FROM orders GROUP BY region, week ORDER BY region, week",
              "truncated": False, "row_count": 8}
#: Agent 的三次分析调用（工具名、请求），照分析工具交给 app.analysis.run 的写法
AN_CALLS = [
    ("analysis_decompose", {"op": "decompose", "mode": "additive", "hypothesis": "下降是不是集中在某个地区",
                            "params": {"query": "Q2", "by": ["region"], "period": "week", "base": "2026-W41",
                                       "target": "2026-W42", "measure": "gmv", "label": "销售额",
                                       "headline": {"base": "Q1.r0.gmv", "target": "Q1.r1.gmv"}}}),
    ("analysis_baseline", {"op": "baseline", "mode": None, "hypothesis": "这周的下降算不算异常",
                           "params": {"query": "Q1", "period": "week", "measure": "gmv", "label": "销售额"}}),
    ("analysis_comparable", {"op": "comparable", "mode": None, "hypothesis": "两周能不能直接比",
                             "params": {"query": "Q2", "period": "week", "base": "2026-W41", "target": "2026-W42",
                                        "measure": "gmv"}}),
]
#: 异动周报：引用 A1 的判定语、具名的数（占比）、结果表的格；A2、A3 只在系统写的分析范围里
AN_REPORT = ("## 异动周报\n\n[[a:A1]]。华东占总变化的 [[a:A1.top_share|pct]]，华东的变化是 [[v:A1.r0.delta]]。[[see:A1]]\n\n"
             "各地区的变化见下表。\n\n[[table:A1 cols=region,base,target,delta rows=0-3]]")


def _shop_notes() -> dict[str, Any]:
    """虚构零售库 shop 的数据目录（orders 是事实表；地区、渠道是属性列，码值表列全）：分析范围按它算没拆的候选维度。"""
    from app.data.catalog import make_item

    def column(label: str, measure: str, codes: tuple[str, ...] = ()) -> dict[str, Any]:
        out = {"label": make_item(label, "human"), "measure": make_item(measure, "human")}
        if codes:
            out["codes"] = make_item({c: "" for c in codes}, "profile", "verified")
        return out

    return {"orders": {"kind": make_item("fact", "human"), "columns": {
        "region": column("地区", "attribute", ("华东", "华北", "华南", "西南")),
        "channel": column("渠道", "attribute", ("线上", "线下")),
        "week": column("周", "attribute"), "amount": column("实收金额", "flow")}}}


def analysis_bundle() -> dict[str, Any]:
    """异动周报（analysis）的页面数据：结论按两份快照真算（app.analysis.run），Agent 节点的事件照分析工具真实的写法
    （tool.end 带结论工件和判定那一段），报告文档按写作目录真实组装，分析范围按虚构的数据目录真实算出。"""
    import asyncio
    from datetime import date

    from app import analysis
    from app.analysis.contract import effective_settings, ledger_entry
    from app.engine import analysis_scope
    from app.tools.analysis import event_view, input_snapshot

    run = {**RUN, "id": "9c2e4a6b8d0f4e1a3c5b7d9f1e3a5c7b", "workflow_id": "wf-sample-anomaly", "workflow_name": "异动周报",
           "started_at": "2026-10-19T01:00:00+00:00", "finished_at": "2026-10-19T01:01:30+00:00"}
    graph = {"nodes": [{"id": n, "type": t, "data": {"label": label}} for n, t, label in [
        ("start", "input", "开始"), ("bot", "agent", "问数据"), ("write", "report", "写周报"), ("out", "output", "成果")]]}
    files: dict[str, bytes] = {}

    def put(obj: Any) -> str:
        artifact, raw = stored(obj)
        files[artifact] = raw
        return artifact

    def load(artifact: str) -> Any:
        raw = files.get(artifact)
        return json.loads(raw) if raw is not None else None

    queries = [(put(snap), snap) for snap in (AN_TOTALS, AN_REGIONS)]
    # 照分析工具交给算子的写法：查询快照原样加上出处；目录事实（列的叫法、度量类型）按虚构的数据目录给，结论里记下
    snapshots = {f"Q{i}": input_snapshot(snap, ref=f"Q{i}", artifact=sha, node_id="bot")
                 for i, (sha, snap) in enumerate(queries, 1)}
    context = {"today": date(2026, 10, 19), "catalogs": {"shop": _shop_notes()}}
    ledger: list[dict[str, Any]] = [
        {"kind": "query", "node_id": "bot", "exec": 1, "artifact": sha, "call_id": f"call_{i}", "tool": "db_query__shop",
         "source": "shop", "columns": snap["columns"], "rows": len(snap["rows"]), "truncated": False}
        for i, (sha, snap) in enumerate(queries)]
    results = []
    for n, (tool, request) in enumerate(AN_CALLS, len(queries)):
        result = dict(asyncio.run(analysis.run(request, snapshots.get, settings=effective_settings(None), context=context)))
        artifact = put(result)
        results.append((tool, f"call_{n}", artifact, result))
        ledger.append(ledger_entry(result, node_id="bot", exec_no=1, artifact=artifact, call_id=f"call_{n}"))
    catalog = build_catalog(nodes={}, ledger=ledger, loader=load)
    doc = compose_doc(AN_REPORT, catalog, node_id="write", run_id=run["id"], loader=load)
    doc["analysis_scope"] = analysis_scope.build(catalog, sources={"shop": {"tables": _shop_notes(), "masked": []}},
                                                 load=load)
    doc_id = put(doc)

    events: list[tuple[int, str, str | None, Any]] = []

    def add(etype: str, node_id: str | None, data: dict[str, Any]) -> None:
        events.append((len(events) + 1, etype, node_id, data))

    add("run.started", None, {"nodes": len(graph["nodes"]), "resumed": False, "memory_scope": "default",
                              "collection": "default", "approval_default": None, "tool_trust": "formal",
                              "agent_limits": {}, "data_versions": None, "replay_protocol": 2})
    add("node.started", "bot", {"node_type": "agent", "label": "问数据"})
    for i, (sha, snap) in enumerate(queries):
        add("tool.end", "bot", {"tool": "db_query__shop", "call_id": f"call_{i}", "duration_ms": 9, "query_artifact": sha,
                                "preview": json.dumps({"columns": snap["columns"], "rows": snap["rows"][:2]},
                                                      ensure_ascii=False)})
    for k, (tool, call_id, artifact, result) in enumerate(results, 1):
        add("tool.end", "bot", {"tool": tool, "call_id": call_id, "duration_ms": 6, "analysis_artifact": artifact,
                                "preview": json.dumps({"analysis_artifact": artifact}), "analysis": event_view(result, f"A{k}")})
    add("node.finished", "bot", {"duration_ms": 240, "attempt": 1, "preview": {"text": "销售额下降，主要来自华东。"},
                                 "evidence": ledger})
    add("llm.end", "write", {"model": "mock", "output": "（写作草稿）"})
    add("report.checked", "write", {"doc_artifact": doc_id, "ok": True})
    add("node.finished", "write", {"duration_ms": 20, "attempt": 1, "preview": {"text": "（报告正文）", "doc_artifact": doc_id}})
    add("issuance", "out", {"tier": "formal", "mode": "citations"})
    add("node.finished", "out", {"duration_ms": 2, "attempt": 1, "preview": {"周报": "（报告正文）"}})
    add("run.finished", None, {"output": {"周报": "（报告正文）"}})
    return build_bundle(run, events, manifest_seq=len(events), manifest_hash=manifest_hash(events), read=files.get,
                        exported_at=EXPORTED_AT, graph=graph, source_masks={})


def bundles() -> dict[str, dict[str, Any]]:
    """{页面名: 页面数据}"""
    out: dict[str, dict[str, Any]] = {}
    s = Sample()
    weekly = s.doc(WEEKLY, "write")
    events, bound = s.events({"write": weekly})
    out["base"] = build(s, events, bound)
    out["missing-doc"] = build(s, events, bound, drop=(weekly,))
    out["missing-card"] = build(s, events, bound, drop=(s.card_id,))
    # 早期运行：运行结束事件落库之前封存，范围到它前一条为止（runner.verify_manifest 的老口径）
    finished = next(seq for seq, etype, _, _ in events if etype == "run.finished")
    out["legacy"] = build(s, events, finished - 1, legacy=True)

    monthly = s.doc(MONTHLY, "monthly")
    events, bound = s.events({"write": weekly, "monthly": monthly})
    out["two-reports"] = build(s, events, bound)

    events, bound = s.events({"write": weekly}, ending="failed")
    out["failed"] = build(s, events, bound, run={**RUN, "status": "failed"})

    events, bound = s.events({"write": weekly}, ending="rejected")
    out["rejected"] = build(s, events, bound, run={**RUN, "status": "cancelled", "end_reason": "rejected"})

    # 早期运行先失败、经继续运行跑到成功：封存范围同样按结束事件推算，到成功结束的前一条为止
    events, _ = s.events({"write": weekly}, ending="continued")
    finished = next(seq for seq, etype, _, _ in events if etype == "run.finished")
    out["continued"] = build(s, events, finished - 1, legacy=True)

    events, bound = s.events({"write": weekly}, tiers=("formal", "degraded"))
    out["degraded"] = build(s, events, bound)

    stray = s.doc(WEEKLY, "write", run_id="another-run-0000")
    events, bound = s.events({"write": stray})
    out["stray"] = build(s, events, bound)

    n = Sample(nan=True)
    events, bound = n.events({"write": n.doc(WEEKLY, "write")}, nan=True)
    out["nan"] = build(n, events, bound)

    # 只引用指标的报告：门店流水的快照没被正文引用，从口径卡的输入这一条路随文件附带、引用链认得它
    out["metric-only"] = build(s, *s.events({"write": s.doc(METRIC_ONLY, "write")}))

    m = Sample(masked=True)
    events, bound = m.events({"write": m.doc(WEEKLY, "write", masked=MASKED_METRICS)})
    out["masked-card"] = build(m, events, bound)
    events, bound = m.events({"write": m.doc(WEEKLY, "write")})
    out["masked-old-doc"] = build(m, events, bound)

    r = Sample(regions=True)
    events, bound = r.events({"write": r.doc(MEMBERS, "write")})
    out["masked-source"] = build(r, events, bound, source_masks={"crm": ["phone"]})

    out["formats"] = formats_bundle()
    out["formats-unit"] = formats_bundle(card=FORMATS_UNIT_CARD)
    out["prose"] = formats_bundle(text=PROSE)
    out["analysis"] = analysis_bundle()
    return out


def pages() -> dict[str, str]:
    """{页面名: HTML}"""
    return {name: render_html(bundle) for name, bundle in bundles().items()}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：python -m tests.bundle_samples <目录>", file=sys.stderr)
        return 2
    target = Path(argv[1])
    target.mkdir(parents=True, exist_ok=True)
    for name, page in pages().items():
        (target / f"{name}.html").write_text(page, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
