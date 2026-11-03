"""业务文档 + 数据目录 → 一份一次到位的口径草稿：定义、取数 SQL、入口字段、指标表达式（docs/design 做法 8 的主路径）。

**为什么这次要模型写 SQL。** 文档里只有业务说法，没有表名。但只抽定义，人还得自己去问一次数、再把运行 id 接回来，等于把
「口径怎么写」拆成两段，谁都说不清草稿什么时候算数。主路径改成一次产出可试跑的图：模型读文档、对照数据目录写一条只读
SELECT，服务端把 SQL、入口字段、口径卡、报告串成图，后台跑一次，草稿页直接给「定义 + 数 + 原文引用」。

**为什么目录是唯一依据。** 文档里没有表名列名，模型只能从调用方给的目录里挑。所以没有数据源、或数据源还没探查表结构时，
调用方必须拒收（api/ledger 的 422）：让模型凭空猜表，猜出来的图跑不了，还不如不做。normalize_plan 再核对一遍 SQL 里出现的
表都在目录里，防止模型从示例、同义词里编出表名。

**为什么 SQL 只收单条只读 SELECT。** 草稿会在后台自动跑一次。多语句、DML/DDL、注释后藏的第二条都可能在试跑时改库；
这里按 sqlglot 解析后的语句数判定，不靠字符串里有没有分号（字符串字面量里的分号不算）。注释里的第二条照样会被解析成
第二条语句，所以也拒。

**为什么表达式必须是 cell(nodes.fetch, 0, '列名')。** 试跑和之后按口径出具都从口径卡读值。取数节点固定叫 fetch（见
build_graph），单行结果取第 0 行的某一列；写成别的样子，卡上就没有值，图能建但出不了数。

**为什么引用核对还是照 docdraft 的老规矩。** 定义的事实来源还是文档原文。模型转述得再像，也要能在原文里逐字定位；一条
引用都核不上的指标整条丢弃，写进 problems 交给人看。这里只做校验，不改 docdraft 的规则。

**为什么不落库、不导 app.api。** 这个模块是纯逻辑加一次模型调用：从哪读文档、允许哪些表、结果存成什么，都由调用方决定。
测试注入假模型，不会发真实请求。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.ledger import docdraft

#: 一次发给模型的材料上限（按 chunk 边界截断；表目录先截到这个数以内）
MAX_INPUT_CHARS = docdraft.MAX_INPUT_CHARS
MAX_TABLES_CHARS = 24000
#: 取数 SQL 的长度上限（字符）。再长的查询不是草稿该承载的
MAX_SQL_CHARS = 20000

#: 入口字段能写的类型（apps/spec.FORM_TYPES 的子集；日期用 string + description 说清）
ENTRY_TYPES = ("string", "number", "integer", "boolean")
#: 指标里固定出现的字段（顺序照接口契约）
METRIC_FIELDS = ("id", "name", "synonyms", "unit", "decimals", "expression", "definition", "open_questions")

#: SQL 模板里的入口占位符：{{ input.x }}（引号内外都认）
_INPUT = re.compile(r"\{\{\s*input\.([A-Za-z_][\w-]*)\s*\}\}")
#: 口径卡的取值表达式，列名可以用单引号或双引号
_EXPRESSION = re.compile(r"^cell\(\s*nodes\.fetch\s*,\s*0\s*,\s*['\"]([^'\"]+)['\"]\s*\)$")
#: 只读查询的根节点：普通 SELECT，或各分支都是 SELECT 的 UNION
_WRITE_NODES = ("Insert", "Update", "Delete", "Create", "Drop", "Alter", "Merge", "TruncateTable", "Command")

_SYSTEM = """你是业务口径与取数模板的起草助手。读给定的业务文档和数据目录，一次产出一份可试跑的口径。

硬性约束：
1. 只输出一个 JSON 对象，不要写解释，不要写 Markdown 代码块之外的话。
2. sql 是取数模板，只能是**一条只读 SELECT**：不要多语句、不要 INSERT/UPDATE/DELETE/DDL，也不要在注释后面再藏一条。
   表和列只能来自「数据目录」，表名、列名照抄，不许猜、不许编。
3. sql 里需要按入口筛选时写 '{{ input.字段名 }}'（字符串）或 {{ input.字段名 }}（数），并在 entry_fields 里声明同名
   字段；不要把日期、门店之类的字面量写死。
4. 每条指标都要有 expression，固定写成 cell(nodes.fetch, 0, '列名')，列名是 sql 最外层 SELECT 的输出列名。
5. 每条指标的定义必须带 citations：引用逐字抄原文，chunk 写材料里方括号内的 id。核对不上、或原文里没有依据的定义
   不要写进指标，写进 open_questions。
6. 拿不准的不要猜，写进 open_questions，用一句问得清的话。不要编造文档里没有的数字、单位、生效日期。

输出格式：
{
  "entry_fields": [
    {"name": "start", "type": "string", "required": true, "default": "", "description": "开始日期，格式 YYYY-MM-DD"}
  ],
  "sql": "SELECT SUM(amount) AS gmv FROM orders WHERE paid_at >= '{{ input.start }}'",
  "metrics": [
    {
      "id": "m1",
      "name": "指标名称",
      "synonyms": ["同义词"],
      "unit": "单位（文档没写就留空）",
      "decimals": 2,
      "expression": "cell(nodes.fetch, 0, 'gmv')",
      "definition": {
        "text": "口径说明",
        "scope": "适用范围",
        "exclusions": ["不包含的情况"],
        "owner": "文档写明的负责人或部门",
        "effective_from": "生效日期（文档原文写法）",
        "citations": [{"chunk": "c1", "quote": "逐字原文"}]
      },
      "open_questions": ["还没说清、需要业务确认的问题"]
    }
  ],
  "open_questions": ["跨指标、以及整条 SQL 还没把握的问题"]
}
"""


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _order(chunks: Mapping[str, dict]) -> list[str]:
    def key(chunk_id: str) -> tuple[int, str]:
        chunk = chunks[chunk_id] if isinstance(chunks[chunk_id], Mapping) else {}
        ordinal = chunk.get("ordinal")
        return (ordinal if isinstance(ordinal, int) else 0, chunk_id)

    return sorted(chunks, key=key)


def _doc_line(doc: Any) -> str:
    if not isinstance(doc, Mapping):
        return str(doc)
    doc_id = doc.get("document_id") or doc.get("id") or ""
    title = doc.get("title") or doc.get("filename") or doc.get("name") or doc_id
    return f"- {doc_id}：{title}"


def _chunk_block(chunk_id: str, chunk: Any) -> str:
    chunk = chunk if isinstance(chunk, Mapping) else {}
    ordinal = chunk.get("ordinal")
    where = f" · 第 {ordinal} 段" if isinstance(ordinal, int) else ""
    document = chunk.get("document_id") or ""
    return f"[{chunk_id}]（文档 {document}{where}）\n{chunk.get('content') or ''}"


def _human(docs: list[dict], chunks: Mapping[str, dict], guidance: str, datasource: dict | None,
           tables_text: str) -> str:
    parts = ["文档清单：", *(_doc_line(doc) for doc in docs), "",
             "逐段原文（引用时 chunk 写方括号里的 id，quote 逐字抄）："]
    parts.extend(_chunk_block(chunk_id, chunks[chunk_id]) for chunk_id in _order(chunks))
    parts.append("")
    if datasource:
        bits = [str(datasource.get(key)) for key in ("name", "type", "dialect", "description")
                if datasource.get(key)]
        parts.append("数据源：{}".format(" · ".join(bits)))
    parts.append("数据目录（sql 只能用这里的表和列，表名列名照抄）：")
    parts.append(tables_text.strip() or "（没有给出表结构，不能写 sql）")
    parts.append("")
    parts.append("用户补充要求：{}".format(guidance.strip() if guidance and guidance.strip() else "（无）"))
    return "\n".join(parts)


def prompt(docs: list[dict], chunks: dict[str, dict], guidance: str = "", datasource: dict | None = None,
           tables_text: str = "") -> list[Any]:
    """构造 system + human：文档原文 + 数据目录。目录是 sql 的唯一依据，和原文一样原样交给模型。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    return [SystemMessage(content=_SYSTEM),
            HumanMessage(content=_human(docs, chunks, guidance, datasource, tables_text))]


def _chars(messages: list[Any]) -> int:
    return sum(len(str(getattr(message, "content", "") or "")) for message in messages)


def _fit(docs: list[dict], chunks: dict[str, dict], guidance: str,
         datasource: dict | None, tables_text: str) -> tuple[dict[str, dict], str, bool]:
    """把材料压到 MAX_INPUT_CHARS 以内：先截表目录尾部，再从后往前按 chunk 边界丢，最后截仅剩的那一段。

    表目录先截：SQL 必须写在目录里的表上，目录比文档的细枝末节更硬；被截掉的是表详情，表名索引还在最前面。
    """
    kept = dict(chunks)
    tables = tables_text
    truncated = False
    if len(tables) > MAX_TABLES_CHARS:
        tables = tables[:MAX_TABLES_CHARS]
        truncated = True

    def size() -> int:
        return _chars(prompt(docs, kept, guidance, datasource, tables))

    order = _order(kept)
    while size() > MAX_INPUT_CHARS and len(order) > 1:
        kept.pop(order.pop())
        truncated = True
    if size() > MAX_INPUT_CHARS and order:
        chunk_id = order[0]
        chunk = dict(kept[chunk_id]) if isinstance(kept[chunk_id], Mapping) else {}
        content = str(chunk.get("content") or "")
        excess = size() - MAX_INPUT_CHARS
        chunk["content"] = content[:max(0, len(content) - excess)]
        kept[chunk_id] = chunk
        truncated = True
    if size() > MAX_INPUT_CHARS:
        excess = size() - MAX_INPUT_CHARS
        tables = tables[:max(0, len(tables) - excess)]
        truncated = True
    return kept, tables, truncated


async def plan(session: Any, *, docs: list[dict], chunks: dict[str, dict], guidance: str = "",
               datasource: dict | None = None, tables_text: str = "", model: Any = None) -> dict:
    """一次模型调用，返回 {entry_fields, sql, metrics, open_questions, model_id, truncated}（未校验）。

    不落库。model 可注入（测试用假对象：有 async ainvoke(messages)，回复有 .content）；不给就按设置解析，模型不可用时抛
    docdraft.DraftUnavailable。SQL 的对错、引用能不能核对，交给 normalize_plan。
    """
    from app.engine.state import message_text

    if model is None:
        from app.data.recipe_ai import resolve_draft_model

        model, model_id, availability = await resolve_draft_model(session)
        if model is None or not getattr(availability, "available", False):
            raise docdraft.DraftUnavailable(getattr(availability, "reason", "") or "AI 起草不可用")
    else:
        named = getattr(model, "model_name", None) or getattr(model, "model", None)
        model_id = named if isinstance(named, str) else ""

    kept, tables, truncated = _fit(docs, chunks, guidance, datasource, tables_text)
    reply = await model.ainvoke(prompt(docs, kept, guidance, datasource, tables))
    data = docdraft.parse_reply(message_text(reply))
    metrics = data.get("metrics")
    questions = data.get("open_questions")
    return {
        "entry_fields": data.get("entry_fields") if isinstance(data.get("entry_fields"), list) else [],
        "sql": data.get("sql") if isinstance(data.get("sql"), str) else "",
        "metrics": metrics if isinstance(metrics, list) else [],
        "open_questions": [q for q in questions if isinstance(q, str) and q.strip()]
        if isinstance(questions, list) else [],
        "model_id": model_id,
        "truncated": truncated,
    }


# ==========================================================================
# 校验
# ==========================================================================


def _entry_fields(value: Any, problems: list[dict]) -> list[dict]:
    """入口字段归一化：名字必填、不重名；类型落回能认的几种，认不出的按文字。"""
    out: list[dict] = []
    seen: set[str] = set()
    for position, item in enumerate(value if isinstance(value, list) else [], start=1):
        if not isinstance(item, Mapping):
            problems.append({"name": f"第 {position} 个入口字段", "reason": "不是一个对象"})
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append({"name": f"第 {position} 个入口字段", "reason": "缺少字段名"})
            continue
        name = name.strip()
        if name in seen:
            problems.append({"name": name, "reason": "入口字段重名"})
            continue
        seen.add(name)
        kind = item.get("type")
        if kind not in ENTRY_TYPES:
            kind = "string"
        default = item.get("default")
        if not isinstance(default, (str, int, float, bool)):
            default = None
        description = item.get("description")
        out.append({"name": name, "type": kind, "required": bool(item.get("required")),
                    "default": default,
                    "description": description.strip() if isinstance(description, str) else ""})
    return out


def _sql_problem(sql: str, allowed_tables: set[str]) -> str | None:
    """单条只读 SELECT、表都在目录里；不满足时返回一句给人看的原因。"""
    if len(sql) > MAX_SQL_CHARS:
        return f"SQL 超过 {MAX_SQL_CHARS} 字，草稿不承载这么长的查询"
    body = _INPUT.sub("?", sql)
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:  # pragma: no cover - sqlglot 是现有依赖，装不上时照实拒
        return "没有 SQL 解析器，不能校验这条查询"
    try:
        statements = [item for item in sqlglot.parse(body) if item is not None]
    except Exception as e:  # noqa: BLE001 - 语法错、方言特性：都按解析不了
        return f"SQL 解析不了：{e}"
    if len(statements) != 1:
        return "只能是一条语句：多语句、或注释后藏着第二条语句的，一律不收"
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union)):
        return "只能是只读 SELECT"
    if isinstance(tree, exp.Union) and not all(isinstance(part, exp.Select) for part in tree.find_all(exp.Select)):
        return "只能是只读 SELECT"
    if any(tree.find(getattr(exp, name)) is not None for name in _WRITE_NODES):
        return "只能是只读 SELECT，不能有写操作或建表、删表这类语句"
    if isinstance(tree, exp.Select) and tree.args.get("into"):
        return "只能是只读 SELECT，不能把结果写进另一张表"
    cte_names = {str(cte.alias or "").casefold() for cte in tree.find_all(exp.CTE)}
    allowed = {str(name).casefold() for name in allowed_tables}
    used: list[str] = []
    for table in tree.find_all(exp.Table):
        name = str(table.name or "")
        short = name.rsplit(".", 1)[-1]
        if not name or name.casefold() in cte_names or short.casefold() in cte_names:
            continue
        db = str(table.db or "")
        keys = {name.casefold(), short.casefold()}
        if db:
            keys.add(f"{db}.{name}".casefold())
        if not (keys & allowed):
            used.append(name)
    if used:
        return f"SQL 用了数据目录里没有的表：{'、'.join(dict.fromkeys(used))}"
    if not any(True for table in tree.find_all(exp.Table)
               if str(table.name or "").casefold() not in cte_names):
        return "SQL 没有引用任何表：请对照数据目录写取数"
    return None


def _metric(item: Mapping, position: int, problems: list[dict]) -> dict | None:
    """一条已经过 docdraft 引用核对的指标：再查 expression，归一成契约里的字段。"""
    name = str(item.get("name") or "").strip()
    expression = item.get("expression")
    if not isinstance(expression, str) or not _EXPRESSION.match(expression.strip()):
        problems.append({"name": name or f"第 {position} 条",
                         "reason": "expression 必须是 cell(nodes.fetch, 0, '列名')，列名用 sql 的输出列名"})
        return None
    definition = item.get("definition") if isinstance(item.get("definition"), Mapping) else {}
    citations = definition.get("citations") if isinstance(definition.get("citations"), list) else []
    decimals = item.get("decimals")
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        decimals = None
    synonyms = item.get("synonyms")
    questions = item.get("open_questions")
    return {
        "id": str(item.get("id") or "").strip(),
        "name": name,
        "synonyms": [s.strip() for s in synonyms if isinstance(s, str) and s.strip()]
        if isinstance(synonyms, list) else [],
        "unit": item.get("unit") if isinstance(item.get("unit"), str) else "",
        "decimals": decimals,
        "expression": expression.strip(),
        "definition": {
            "text": definition.get("text") if isinstance(definition.get("text"), str) else "",
            "scope": definition.get("scope") if isinstance(definition.get("scope"), str) else "",
            "exclusions": [str(v) for v in definition.get("exclusions") or [] if isinstance(v, str)]
            if isinstance(definition.get("exclusions"), list) else [],
            "owner": definition.get("owner") if isinstance(definition.get("owner"), str) else "",
            "effective_from": definition.get("effective_from")
            if isinstance(definition.get("effective_from"), str) else "",
            "citations": citations,
        },
        "open_questions": [q for q in questions if isinstance(q, str) and q.strip()]
        if isinstance(questions, list) else [],
    }


def normalize_plan(raw: Any, chunks: Mapping[str, dict], *, allowed_tables: set[str]) -> tuple[dict, list[dict]]:
    """校验模型交回的草案。返回 (可落库的 plan, problems)。

    problems 的形状是 {"name": …, "reason": …}，name 能认出是哪一条就用名字，认不出按序号说。硬规则：SQL 单条只读、表都在
    目录里；每条指标的 expression 是 cell(nodes.fetch, 0, '列名')；至少一条引用能在原文里逐字核对上（复用
    docdraft.normalize_metrics 的核对）。个别指标不合格只丢那一条，problems 里写清为什么；SQL 不合格时 plan["sql"] 为空，
    由调用方决定整份草稿算失败。
    """
    body = raw if isinstance(raw, Mapping) else {}
    problems: list[dict] = []
    entry_fields = _entry_fields(body.get("entry_fields"), problems)

    sql = body.get("sql")
    sql_text = sql.strip() if isinstance(sql, str) else ""
    if not sql_text:
        problems.append({"name": "sql", "reason": "没有给出取数 SQL"})
    else:
        reason = _sql_problem(sql_text, allowed_tables)
        if reason:
            problems.append({"name": "sql", "reason": reason})
            sql_text = ""
        else:
            declared = {field["name"] for field in entry_fields}
            missing = sorted(set(_INPUT.findall(sql_text)) - declared)
            if missing:
                problems.append({"name": "sql",
                                 "reason": f"SQL 用了没有声明的入口字段：{'、'.join(missing)}"})
                sql_text = ""

    checked, dropped = docdraft.normalize_metrics(body.get("metrics"), dict(chunks))
    problems.extend(dropped)
    metrics: list[dict] = []
    seen_ids: set[str] = set()
    for position, item in enumerate(checked, start=1):
        metric = _metric(item, position, problems)
        if metric is None:
            continue
        if not metric["id"] or metric["id"] in seen_ids:
            metric["id"] = f"m{len(metrics) + 1}"
            while metric["id"] in seen_ids:
                metric["id"] += "_"
        seen_ids.add(metric["id"])
        metrics.append(metric)
    if not metrics:
        problems.append({"name": "metrics", "reason": "没有一条指标通过校验"})

    questions = body.get("open_questions")
    plan_out = {
        "entry_fields": entry_fields,
        "sql": sql_text,
        "metrics": metrics,
        "open_questions": [q.strip() for q in questions if isinstance(q, str) and q.strip()]
        if isinstance(questions, list) else [],
    }
    return plan_out, problems


# ==========================================================================
# 建图（确定性，和 certify.build_graph 同一个形状）
# ==========================================================================


def build_graph(plan: dict, *, name: str, question: str = "") -> dict:
    """草案 → 草稿图：start(input) → fetch(tool) → card(metrics) → write(report) → out(output)，过 auto_layout。

    plan["datasource"] 是取数节点指向的数据源名（调用方按这次的数据源补，不采信模型回复里的名字）。调用两次结果逐字一致：
    节点 id、坐标都由输入决定，没有时间戳。报告配置、出口契约照 certify.build_graph 的写法，发布门禁认的就是这一套
    （REPORT_POLICY、metrics_from、required）。
    """
    from app.engine.governance import REPORT_POLICY
    from app.engine.layout import auto_layout
    from app.engine.schema import GraphSpec
    from app.ledger.certify import REPORT_INSTRUCTIONS, REPORT_INSTRUCTIONS_PARAMS
    from app.tools.datasource import QUERY_PREFIX

    fields = [dict(f) for f in plan.get("entry_fields") or []]
    metrics = [dict(m) for m in plan.get("metrics") or []]
    source = str(plan.get("datasource") or "")
    report_cfg: dict[str, Any] = {}
    if fields:
        report_cfg["instructions"] = REPORT_INSTRUCTIONS_PARAMS.format(
            params="，".join(f"{f['name']} = {{{{ input.{f['name']} }}}}" for f in fields))
    else:
        report_cfg["instructions"] = REPORT_INSTRUCTIONS.format(question=question or name or "（问题）")
    report_cfg.update({key: value for key, value, _ in REPORT_POLICY})
    report_cfg["metrics_from"] = ["card"]
    contract: dict[str, Any] = {"report_from": "write", "metrics_from": "card",
                                "required": [m["id"] for m in metrics]}
    chain = [
        {"id": "start", "type": "input", "position": {"x": 0, "y": 0},
         "data": {"label": "入口", "config": {"fields": fields}}},
        {"id": "fetch", "type": "tool", "position": {"x": 0, "y": 0},
         "data": {"label": "取数", "config": {"tool": f"{QUERY_PREFIX}{source}",
                                             "args": {"sql": str(plan.get("sql") or "")}}}},
        {"id": "card", "type": "metrics", "position": {"x": 0, "y": 0},
         "data": {"label": "口径卡", "config": {"caliber": (name.strip() or question or "口径")[:40],
                                              "caliber_version": "v1", "metrics": metrics}}},
        {"id": "write", "type": "report", "position": {"x": 0, "y": 0},
         "data": {"label": "报告", "config": report_cfg}},
        {"id": "out", "type": "output", "position": {"x": 0, "y": 0},
         "data": {"label": "出口", "config": {"fields": [{"name": "报告", "value": "{{ nodes.write.text }}"}],
                                             "contract": contract}}},
    ]
    edges = [{"source": a["id"], "target": b["id"]} for a, b in zip(chain, chain[1:])]
    return auto_layout(GraphSpec.model_validate({"nodes": chain, "edges": edges})).model_dump(mode="json")


__all__ = ["ENTRY_TYPES", "MAX_INPUT_CHARS", "MAX_SQL_CHARS", "MAX_TABLES_CHARS", "METRIC_FIELDS",
           "build_graph", "normalize_plan", "plan", "prompt"]
