"""规则 DSL 的编译与 fixture 校验：结构化条件 → 一条只读 SQL，发布前用样例行跑一遍。

和口径卡里的 `kind=rule` 不是同一层：口径卡里的规则在单元格上确定性求值（不需要查库）；这里给作者和发布检查用，
把「表 + 条件 → then / else」编译成 SQL，再用 fixture 验证条件写对了。编译只生成 SELECT，不执行任何写操作。

规则形状：
    {
      "inputs": {"status": "status", "amount": "amount"},   # 名字 → 表里的列
      "all": [{"input": "status", "op": "eq", "value": "paid"},
              {"input": "amount", "op": "gte", "value": 50}],
      "then": 1, "else": 0
    }
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, Mapping

OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RuleInvalid(ValueError):
    """规则本身写错了：message 给人看。"""


def _ident(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not _IDENT.match(text):
        raise RuleInvalid(f"{what}「{value}」不是合法的标识符（字母或下划线开头，只含字母、数字、下划线）")
    return text


def _literal(exp: Any, value: Any) -> Any:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    if value is None:
        return exp.Null()
    return exp.Literal.string(str(value))


def _condition(exp: Any, cond: Mapping[str, Any], inputs: Mapping[str, str]) -> Any:
    name = str(cond.get("input") or "")
    if name not in inputs:
        raise RuleInvalid(f"条件指向了没声明的输入「{name}」")
    column = exp.column(_ident(inputs[name], "列名"))
    op = str(cond.get("op") or "")
    if op not in OPS:
        raise RuleInvalid(f"条件运算符只能是 {'、'.join(OPS)}，当前为「{op}」")
    if op in ("in", "not_in"):
        values = cond.get("values")
        if not isinstance(values, list) or not values:
            raise RuleInvalid("in / not_in 需要非空的 values 列表")
        node = exp.In(this=column, expressions=[_literal(exp, v) for v in values])
        return node if op == "in" else exp.Not(this=node)
    if "value" not in cond:
        raise RuleInvalid("条件需要 value")
    right = _literal(exp, cond["value"])
    return {
        "eq": exp.EQ, "ne": exp.NEQ, "gt": exp.GT, "gte": exp.GTE, "lt": exp.LT, "lte": exp.LTE,
    }[op](this=column, expression=right)


def compile_rule(rule: Mapping[str, Any], table: str, *, dialect: str | None = None) -> str:
    """结构化规则 → `SELECT CASE WHEN … THEN … ELSE … END AS value FROM <table>`。"""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError as e:  # pragma: no cover - 依赖缺失时给一句人话
        raise RuleInvalid("没有安装 sqlglot，无法编译规则") from e
    table_name = _ident(table, "表名")
    if not isinstance(rule, Mapping):
        raise RuleInvalid("规则需要是一个对象")
    inputs = rule.get("inputs")
    if not isinstance(inputs, Mapping) or not inputs:
        raise RuleInvalid("规则需要 inputs：名字 → 表里的列")
    columns = {str(k): _ident(v, "列名") for k, v in inputs.items()}
    conditions = rule.get("all")
    if not isinstance(conditions, list) or not conditions:
        raise RuleInvalid("规则需要 all：至少一条条件")
    where = None
    for cond in conditions:
        node = _condition(exp, cond, columns)
        where = node if where is None else exp.And(this=where, expression=node)
    case = exp.Case(
        ifs=[exp.If(this=where, true=_literal(exp, rule.get("then", 1)))],
        default=_literal(exp, rule.get("else", 0)),
    )
    query = exp.select(exp.alias_(case, "value")).from_(exp.table_(table_name))
    return query.sql(dialect=dialect or "sqlite")


def run_fixtures(rule: Mapping[str, Any], table: str, fixtures: Iterable[Mapping[str, Any]], *,
                 dialect: str = "sqlite") -> list[dict[str, Any]]:
    """用一条只读 SQL 在内存 SQLite 上跑 fixture：每项 {rows, expected}，返回 [{ok, expected, actual}]。

    fixture 的 rows 是列名 → 值；表结构按第一行推（整数 / 实数 / 文本）。空 fixture 返回空列表。
    """
    items = [dict(f) for f in fixtures]
    if not items:
        return []
    sql = compile_rule(rule, table, dialect="sqlite" if dialect != "sqlite" else dialect)
    db = sqlite3.connect(":memory:")
    try:
        columns: dict[str, str] = {}
        for item in items:
            for row in item.get("rows") or []:
                if not isinstance(row, Mapping):
                    raise RuleInvalid("fixture 的 rows 需要是对象")
                for name, value in row.items():
                    key = _ident(name, "fixture 列名")
                    columns.setdefault(key, "INTEGER" if isinstance(value, int) and not isinstance(value, bool)
                                       else "REAL" if isinstance(value, float) else "TEXT")
        if not columns:
            raise RuleInvalid("fixture 需要至少一行 rows")
        db.execute(f"CREATE TABLE {table} (" + ", ".join(f"{name} {kind}" for name, kind in columns.items()) + ")")
        out: list[dict[str, Any]] = []
        for item in items:
            db.execute(f"DELETE FROM {table}")
            for row in item.get("rows") or []:
                db.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                           list(row.values()))
            actual = db.execute(sql).fetchone()[0]
            out.append({"ok": actual == item.get("expected"), "expected": item.get("expected"), "actual": actual})
        return out
    finally:
        db.close()


__all__ = ["OPS", "RuleInvalid", "compile_rule", "run_fixtures"]
