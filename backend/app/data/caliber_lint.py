"""口径取数 SQL 的书写 lint：取整、补 0 应当交给口径卡，不写进取数 SQL。

口径账本（ledger/match）证明「问数据的查询与口径卡等价」时只认规范化后完全相同的结果。口径卡的取数 SQL 里写了
ROUND(...)，未取整的查询无论数值对不对，规范化后都多一层取整；写了 COALESCE(..., 0)，没有数据时算出 0，两边
永远对不上。所以这两处要在画布校验和发布门禁里提示/拦截——不能在比对时放松，放松就认不出真正不同的口径了。

只做 SQL 级检查，不读 GraphSpec：「哪些节点是口径卡的上游取数节点」由 engine 侧找（engine/schema.py）。
"""

from __future__ import annotations

from typing import Any

ROUND_IN_SOURCE = "caliber.round_in_source"
ZERO_DEFAULT = "caliber.zero_default"

ROUND_MESSAGE = (
    "取数 SQL 里写了 ROUND(...)：取整交给口径卡的小数位和展示；取数 SQL 里写 ROUND 会让未取整的查询"
    "永远认不成等价（口径账本只认规范化后完全相同的结果）"
)
ZERO_MESSAGE = (
    "取数 SQL 里用 COALESCE / IFNULL / NVL 给缺省值补了 0：缺省值交给口径卡；取数里补 0 会把「没有数据」"
    "算成 0，且永远认不成等价（口径账本只认规范化后完全相同的结果）"
)

#: excerpt 的截断长度：够看清出问题的那一段，又不至于把整条查询塞进提示
EXCERPT_CHARS = 200

_ZERO_FUNCS = frozenset({"COALESCE", "IFNULL", "NVL"})


def issues_for_sql(sql: str) -> list[dict[str, Any]]:
    """检查一条取数 SQL 的书写问题：[{code, message, excerpt}]。

    解析不了、不是字符串、空串都返回空列表：lint 是附加提示，不能挡住主流程。同时有 ROUND 和补 0 时按它们在
    SQL 里出现的先后返回；调用方每个取数节点只取第一条。
    """
    if not isinstance(sql, str) or not sql.strip():
        return []
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return []
    try:
        tree = sqlglot.parse_one(sql)
    except Exception:  # noqa: BLE001 - 语法错、方言特性、sqlglot 不支持的写法：一律不查
        return []
    if tree is None:
        return []

    rounds = list(tree.find_all(exp.Round))
    coalesces = list(tree.find_all(exp.Coalesce))
    # 兜底按函数名认：某些方言/写法下 sqlglot 不把函数落到专门的节点上（Anonymous），节点名才是唯一的依据
    for func in tree.find_all(exp.Func):
        name = _function_name(func, exp)
        if name == "ROUND" and not isinstance(func, exp.Round):
            rounds.append(func)
        elif name in _ZERO_FUNCS and not isinstance(func, exp.Coalesce):
            coalesces.append(func)

    found: list[tuple[int, dict[str, Any]]] = []
    found.extend((_position(func), _issue(ROUND_IN_SOURCE, ROUND_MESSAGE, func)) for func in rounds)
    found.extend((_position(func), _issue(ZERO_DEFAULT, ZERO_MESSAGE, func))
                 for func in coalesces if _ends_with_zero(func, exp))
    found.sort(key=lambda item: item[0])
    return [issue for _, issue in found]


def _function_name(func: Any, exp: Any) -> str:
    """函数名的大写写法。Anonymous 的 sql_name() 只给出 ANONYMOUS，要看它自己记的名字。"""
    name = func.sql_name()
    if isinstance(func, exp.Anonymous) and isinstance(func.this, str):
        name = func.this
    return str(name or "").upper()


def _arguments(func: Any, exp: Any) -> list[Any]:
    """函数的参数列表。Coalesce 把第一个参数放在 this 上，其余在 expressions。"""
    if isinstance(func, exp.Coalesce):
        return [func.this, *func.expressions]
    return list(func.expressions)


def _ends_with_zero(func: Any, exp: Any) -> bool:
    """最后一个参数是数值 0：COALESCE(x, 0) / IFNULL(x, 0) / NVL(x, 0)。

    只认常量 0：COALESCE(x, y) 是正常的缺省列，不该报。
    """
    args = _arguments(func, exp)
    if len(args) < 2 or args[-1] is None:
        return False
    last = args[-1]
    while isinstance(last, (exp.Paren, exp.Neg)):
        last = last.this
    if not isinstance(last, exp.Literal) or not last.is_number:
        return False
    try:
        return float(last.name) == 0
    except (TypeError, ValueError):
        return False


def _position(func: Any) -> int:
    """节点在原文里的起始位置，用来按出现先后排序；拿不到位置的排在最后。"""
    start = (getattr(func, "meta", None) or {}).get("start")
    return int(start) if isinstance(start, int) else 1 << 30


def _issue(code: str, message: str, func: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "excerpt": _excerpt(func.sql())}


def _excerpt(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS - 1] + "…"
