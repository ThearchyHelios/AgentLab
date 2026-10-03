from __future__ import annotations

import ast
import json
import math
import operator
import re
from typing import Any

# --------------------------------------------------------------------------
# 变量插值： {{ input.query }} / {{ nodes.n1.text }} / {{ vars.items | json }}
# --------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def resolve_path(ctx: dict[str, Any], path: str) -> Any:
    """按点号路径取值，支持 a.b[0].c。取不到返回 None 而不是抛错 —— 编排里
    半成品状态很常见，让它渲染成空字符串比中断整个 run 有用。"""
    cur: Any = ctx
    for raw in path.split("."):
        raw = raw.strip()
        if not raw:
            continue
        # 拆出 name[0][1] 形式的下标
        m = re.match(r"^([^\[\]]*)((?:\[[^\[\]]+\])*)$", raw)
        if not m:
            return None
        name, idx_part = m.group(1), m.group(2)
        if name:
            if isinstance(cur, dict):
                cur = cur.get(name)
            else:
                cur = getattr(cur, name, None)
            if cur is None:
                return None
        for idx in re.findall(r"\[([^\[\]]+)\]", idx_part):
            idx = idx.strip().strip("'\"")
            try:
                if isinstance(cur, (list, tuple, str)):
                    cur = cur[int(idx)]
                elif isinstance(cur, dict):
                    cur = cur.get(idx)
                else:
                    return None
            except (ValueError, IndexError, KeyError, TypeError):
                return None
    return cur


_FILTERS = {
    "json": lambda v: json.dumps(v, ensure_ascii=False, indent=2, default=str),
    "compact": lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str),
    "upper": lambda v: str(v).upper(),
    "lower": lambda v: str(v).lower(),
    "trim": lambda v: str(v).strip(),
    "length": lambda v: len(v) if hasattr(v, "__len__") else 0,
    "first": lambda v: v[0] if isinstance(v, (list, tuple)) and v else None,
    "last": lambda v: v[-1] if isinstance(v, (list, tuple)) and v else None,
}


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def render_template(text: str, ctx: dict[str, Any]) -> str:
    """把字符串里的 {{ ... }} 全部替换掉。支持 `路径 | 过滤器` 语法。"""
    if not text or "{{" not in text:
        return text or ""

    def _sub(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if not expr:
            return ""
        parts = [p.strip() for p in expr.split("|")]
        value = resolve_path(ctx, parts[0])
        for filt in parts[1:]:
            fn = _FILTERS.get(filt)
            if fn:
                try:
                    value = fn(value)
                except Exception:  # noqa: BLE001 - 过滤器不该让 run 挂掉
                    value = None
        return _stringify(value)

    return _TEMPLATE_RE.sub(_sub, text)


def render_deep(value: Any, ctx: dict[str, Any]) -> Any:
    """递归渲染嵌套结构里的所有模板字符串。"""
    if isinstance(value, str):
        return render_template(value, ctx)
    if isinstance(value, dict):
        return {k: render_deep(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render_deep(v, ctx) for v in value]
    return value


# --------------------------------------------------------------------------
# 安全表达式求值（分支条件用）
# --------------------------------------------------------------------------

class ExpressionError(ValueError):
    pass


# 幂运算的上界。`9**9**9**9` 只有 11 个 AST 节点，轻松过掉节点数护栏，
# 却会让 CPython 无限期地算一个天文数字的大整数 —— 而求值是在事件循环里
# 同步执行的（分支条件、transform、口径卡、每个节点的 skip_if 都走这里），
# 既没有线程池隔离也没有超时，asyncio 的 cancel 打断不了同步 CPU。
# 一条画布上的分支条件就能把整个后端冻死。
_MAX_POW_EXPONENT = 1024
_MAX_POW_BASE = 1e15


def _safe_pow(base: Any, exponent: Any) -> Any:
    """带上界的幂运算：超界直接报错，而不是让进程算到天荒地老。"""
    try:
        if abs(exponent) > _MAX_POW_EXPONENT:
            raise ExpressionError(f"指数超过上限 {_MAX_POW_EXPONENT}")
        if abs(base) > _MAX_POW_BASE and exponent > 1:
            raise ExpressionError(f"底数超过上限 {_MAX_POW_BASE:g}")
    except TypeError as e:  # 非数值类型交给 operator.pow 自己报
        raise ExpressionError(f"幂运算的操作数必须是数值：{e}") from e
    return operator.pow(base, exponent)


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: _safe_pow,
}
_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}
_UNARY_OPS = {ast.Not: operator.not_, ast.USub: operator.neg, ast.UAdd: operator.pos}


# --------------------------------------------------------------------------
# 查询结果里的一格：cell(x, row, column)
#
# 口径卡直接从 tool 节点的查询结果取数时，以前只能写 vars.q.rows[0][2]：列的顺序一改，
# 取到的就是另一列，而且报告点开只追得到这条路径。cell() 按列名取，来历能精确到
# 那次查询的那一格。报告里的 [[v:Q1.r0.amount]] 用的也是这里的规则，两边取到的永远是同一个值。
# --------------------------------------------------------------------------

#: 数据源把 DECIMAL 一律落成 str(Decimal)（data/engine.py 的 _jsonable）："45678.50"；小数位为 0 的
#: ——MySQL 对整数列 SUM()（包括 SUM(CASE WHEN … THEN 1 ELSE 0 END) 这种计数）、Postgres 的
#: SUM(bigint)、NUMERIC(10,0)——是没有小数点的 "45678"；很小的写成 "1E-7"。这些都按数算。
#: 带前导零的 "00123" 是编号，照文本。快照里没有列类型：文本列里的值恰好全是不带前导零的数字时，
#: 同样会被当成数，要文本就写 str(cell(...))
_NUMERIC_TEXT = re.compile(r"^\s*-?(?:0|[1-9]\d*)(?:\.\d+)?(?:E[+-]?\d+)?\s*$")
#: 字段声明成数时，前导零、正号、小写 e 也按数读：写 schema 的人说了它是数
_LOOSE_NUMERIC_TEXT = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\s*$")
_INTEGER_TEXT = re.compile(r"^\s*[+-]?\d+\s*$")


class CellError(ExpressionError):
    """取不到那一格。消息是人话：第几行、有几行、有哪些列。口径卡、分支条件照接住
    表达式错误的路子接住它。"""


def numeric_text(raw: Any, *, loose: bool = False) -> int | float | None:
    """数字文本 → 数；不是数字文本（或者根本不是文本）返回 None。

    没有小数点、没有指数的给整数：不经 float，大整数不丢位，45678 也不会变成 45678.0。
    loose=True 连前导零、正号也认，只在字段声明成数时用。
    """
    if not isinstance(raw, str) or not (_LOOSE_NUMERIC_TEXT if loose else _NUMERIC_TEXT).match(raw):
        return None
    try:
        return int(raw) if _INTEGER_TEXT.match(raw) else float(raw)
    except ValueError:      # 超过 Python 整数位数上限（4300 位）的：照文本，不让它在渲染、核对里炸开
        return None


def cell_value(raw: Any, kind: str | None = None) -> Any:
    """快照里的一格 → 参与运算、比对的值：DECIMAL 落成的数字文本换成数，其余原样。

    kind 是查询时从驱动的原始值记下的列类型（query_snapshot 的 column_types）：text 列原样是文本，
    VARCHAR 的 "2026" 不会变成 2026。不给（老快照没记、口径卡表达式里的 cell()）就按值猜。
    """
    if kind == "text":
        return raw
    number = numeric_text(raw)
    return raw if number is None else number


def column_kind(table: Any, column: Any) -> str | None:
    """查询时记下的这一列的类型：number / text / date / datetime / time / boolean。老快照没记，返回 None。"""
    types = table.get("column_types") if isinstance(table, dict) else None
    kind = types.get(column) if isinstance(types, dict) and isinstance(column, str) else None
    return kind if isinstance(kind, str) else None


def same_value(model: Any, truth: Any) -> bool:
    """模型报的值和快照里的一格是不是一回事：数按数比（数字文本也算数，45678.0 和 "45678" 一致），
    文本压成一行比。cite_fields 的核对和口径卡的来历用同一把尺子。"""
    if model is None or truth is None:
        return model is None and truth is None
    m, t = cell_value(model), cell_value(truth)
    if isinstance(m, bool) or isinstance(t, bool):
        return m == t
    if isinstance(m, (int, float)) and isinstance(t, (int, float)):
        return math.isclose(float(m), float(t), rel_tol=1e-9, abs_tol=1e-9)
    return re.sub(r"\s+", " ", str(m)).strip() == re.sub(r"\s+", " ", str(t)).strip()


def _table_of(table: Any) -> dict[str, Any]:
    data = table
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            # 查库失败时工具交回的是一句话（「查询失败：…」）：原话照搬，比「不是 JSON」有用
            raise CellError(f"需要查询结果，实际得到的是「{data.strip()[:80]}」") from None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise CellError("第一个参数需要是查询结果：查询工具返回的 JSON 文本，或 {columns, rows} 结构")
    return data


def locate_cell(table: Any, row: Any, column: Any) -> tuple[Any, str]:
    """(那一格的原值, 列名)。列号换成列名：按列名和按列号引用同一格，得到同一个 eid。"""
    data = _table_of(table)
    rows = data["rows"]
    if isinstance(row, bool) or not isinstance(row, int):
        raise CellError(f"行号需为从 0 开始的整数，当前为「{row}」")
    if not rows:
        raise CellError("查询结果为空（0 行）")
    if not 0 <= row < len(rows):
        raise CellError(f"请求第 {row} 行，但查询结果只有 {len(rows)} 行（行号从 0 开始）")
    record = rows[row]
    columns = [str(c) for c in data.get("columns") or []]
    if isinstance(record, dict):
        columns = columns or [str(k) for k in record]
    if isinstance(column, int) and not isinstance(column, bool):
        if not 0 <= column < len(columns):
            raise CellError(f"请求第 {column} 列，但查询结果只有 {len(columns)} 列（列号从 0 开始）")
        column = columns[column]
    elif not isinstance(column, str):
        raise CellError(f"列需填写列名或从 0 开始的列号，当前为「{column}」")
    if isinstance(record, dict):
        if column not in record:
            raise CellError(f"没有列「{column}」，可用的列：{'、'.join(columns[:12])}")
        return record[column], column
    if column not in columns:
        raise CellError(f"没有列「{column}」，可用的列：{'、'.join(columns[:12])}")
    index = columns.index(column)
    if not isinstance(record, (list, tuple)) or index >= len(record):
        raise CellError(f"第 {row} 行中没有「{column}」对应的单元格")
    return record[index], column


def table_cell(table: Any, row: Any, column: Any) -> Any:
    """表达式里的 cell(x, row, column)：x 是查询工具交回的 JSON 文本或 {columns, rows}。

    不看列类型、照旧按值猜：口径卡是在算数，数字写成文本的列（CAST 成文本、VARCHAR 存的金额）多半
    就是要拿来算的。要文本写 str(cell(...))。
    """
    try:
        raw, _ = locate_cell(table, row, column)
    except CellError as e:
        raise CellError(f"cell() {e}") from None
    return cell_value(raw)


# --------------------------------------------------------------------------
# 截断的结果：按行号取单格照常，当成整组用时记一笔
#
# 查询撞了行数或字节上限时，数据层只交回前面一截，标 truncated: true（data/engine.py 的 run_query）。
# 这一截的前 N 行就是完整结果的前 N 行，所以 cell(x, 0, 'gmv')、rows[0] 取到的那一格是真实的；
# 可 len(rows) 只是取回的行数，sum / max / sorted / 成员判断 / 倒数第几行，说的都是这一截而不是
# 完整结果。口径卡拿这种数出具，等于把「前 1000 行的人次」当成「入园人次」。
#
# 不按正则猜表达式（写法千变万化，get(x, 'rows')、x['rows']、x.rows 都是同一件事），而是在求值时跟踪：
# 从截断结果里取 rows 的那一刻包一层 TruncatedRows，它照常当列表用，只在被整组用到时往 TruncatedUse
# 里记一笔。数据源工具交回的是 JSON 文本，表达式里只有 cell() 读得了它；经数据整形解析成对象之后
# 才有 .rows 可取，所以这里认的是「带 truncated: true 和 rows 列表的对象」这种形状。
# --------------------------------------------------------------------------


class TruncatedUse:
    """一次求值里，哪些截断的结果被当成了整组：sources 是 {键: {path, rows, artifact?}}。

    - path：取到这组行的取值链（vars.visits；agent 的数组字段是字段本身 vars.kpi.amounts）
    - rows：取回了几行——完整结果至少比这多
    - artifact：查询快照的工件 id，结果里带着的才有
    同一份结果按工件 id（没有就按 path）只记一次：len(x.rows) + x.row_count 只用了一份结果。

    edge(path) 给调用方认定的截断整组：口径卡用它标出 agent 经 cite_fields 交来、引用的行段到了截断结果
    末行的数组字段。返回 {rows, artifact?} 或 None。求值器只对取到列表的取值链问它。
    """

    __slots__ = ("sources", "edge")

    def __init__(self, edge: Any = None) -> None:
        self.sources: dict[str, dict[str, Any]] = {}
        self.edge = edge

    def record(self, path: str, rows: int, artifact: Any = None) -> None:
        key = artifact if isinstance(artifact, str) and artifact else path
        self.sources.setdefault(key, {"path": path, "rows": rows,
                                      **({"artifact": artifact} if isinstance(artifact, str) and artifact else {})})


class TruncatedRows:
    """截断结果里的一组行。只活在一次求值里：eval_expression 交出结果前一律换回普通 list。

    整组用到的写法都要经过这里的某个特殊方法：len()、迭代（sum / min / max / sorted / any / all）、
    in、比较、拼接、转成文本、倒数第几行、取回的行之后的行号。按行号取取回范围之内的一行不记；
    真假判断也不记——截断的结果至少有一行，「有数据」这件事是真的。

    不继承 list：list 的 C 实现里有绕过子类方法的快路径（json 编码、[] + x 的 sq_concat 都直接读底层数组），
    记漏一处就是把截断的数当成完整的出具。属性一律带下划线，表达式碰不到（私有属性在静态检查里就拒了）。
    """

    __slots__ = ("_rows", "_use", "_path", "_count", "_artifact")
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, rows: list[Any], use: TruncatedUse, path: str, count: int, artifact: Any = None) -> None:
        self._rows, self._use, self._path, self._count, self._artifact = rows, use, path, count, artifact

    def _whole(self) -> list[Any]:
        self._use.record(self._path, self._count, self._artifact)
        return self._rows

    def __len__(self) -> int:
        return len(self._whole())

    def __iter__(self) -> Any:
        return iter(self._whole())

    def __reversed__(self) -> Any:
        return reversed(self._whole())

    def __contains__(self, item: Any) -> bool:
        return item in self._whole()

    def __getitem__(self, index: Any) -> Any:
        # 倒数第几行：完整结果的最后一行不是取回的最后一行；取回范围之外的行号：完整结果里有这一行，
        # 只是没取回来（照旧抛 IndexError，求值器按取不到处理，原因记在这里）
        if isinstance(index, int) and not isinstance(index, bool) and not 0 <= index < len(self._rows):
            self._whole()
        return self._rows[index]

    def __bool__(self) -> bool:
        return bool(self._rows)

    def __eq__(self, other: Any) -> bool:
        return self._whole() == _plain_rows(other)

    def __ne__(self, other: Any) -> bool:
        return self._whole() != _plain_rows(other)

    def __lt__(self, other: Any) -> bool:
        return self._whole() < _plain_rows(other)

    def __le__(self, other: Any) -> bool:
        return self._whole() <= _plain_rows(other)

    def __gt__(self, other: Any) -> bool:
        return self._whole() > _plain_rows(other)

    def __ge__(self, other: Any) -> bool:
        return self._whole() >= _plain_rows(other)

    def __add__(self, other: Any) -> Any:
        return self._whole() + _plain_rows(other)

    def __radd__(self, other: Any) -> Any:
        return _plain_rows(other) + self._whole()

    def __mul__(self, times: Any) -> Any:
        return self._whole() * times

    __rmul__ = __mul__

    def __repr__(self) -> str:
        return repr(self._whole())


def _plain_rows(value: Any) -> Any:
    return value._whole() if isinstance(value, TruncatedRows) else value


def _truncated_table(value: Any) -> bool:
    """查询结果的形状、而且被截断了：数据源工具交回的 JSON 经数据整形解析成对象之后就是这样。"""
    return isinstance(value, dict) and value.get("truncated") is True and isinstance(value.get("rows"), list)


def _settle(value: Any) -> Any:
    """交出结果前把 TruncatedRows 换回普通 list（整组交出去也是整组用到）。没有包装的原样返回、不复制。"""
    if isinstance(value, TruncatedRows):
        return list(value._whole())
    if type(value) is list:
        items = [_settle(v) for v in value]
        return items if any(a is not b for a, b in zip(items, value)) else value
    if type(value) is dict:
        pairs = {k: _settle(v) for k, v in value.items()}
        return pairs if any(pairs[k] is not v for k, v in value.items()) else value
    return value


_SAFE_FUNCS: dict[str, Any] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "round": round,
    "sorted": sorted,
    "any": any,
    "all": all,
    "lower": lambda s: str(s).lower(),
    "upper": lambda s: str(s).upper(),
    "contains": lambda hay, needle: needle in hay if hay is not None else False,
    "startswith": lambda s, p: str(s).startswith(p),
    "endswith": lambda s, p: str(s).endswith(p),
    "matches": lambda s, p: bool(re.search(p, str(s))),
    "get": lambda obj, key, default=None: (obj or {}).get(key, default),
    "cell": table_cell,
}

_MAX_NODES = 200

#: 表达式能引用的根名字，就是 state.template_context 的那几个键（有测试钉住两边一致）。
#: 不在这里、也不是白名单函数或 true/false/null 的名字，运行时一律取到 None——
#: `gate == "ok"` 这种漏写了 vars. 的条件永远不成立，而且不报错
EXPRESSION_ROOTS = frozenset(
    {"input", "nodes", "vars", "output", "loops", "usage", "messages", "last_message"}
)
_LITERAL_NAMES = frozenset({"true", "True", "false", "False", "null", "None"})

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Attribute, ast.Subscript,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.IfExp,
    ast.Call, ast.keyword, ast.List, ast.Tuple, ast.Dict,
    *_BIN_OPS, *_UNARY_OPS, *_CMP_OPS,
)
_NODE_NAMES = {
    "ListComp": "列表推导式", "SetComp": "集合推导式", "DictComp": "字典推导式",
    "GeneratorExp": "生成器表达式", "Lambda": "lambda", "JoinedStr": "f-string",
    "NamedExpr": ":=", "Slice": "切片 a[i:j]", "Starred": "* 展开", "Await": "await",
    "Set": "集合写法",
}
#: 不支持的运算符给人看的写法：报错里写符号，不写 Python 的类名（BitOr）
_OP_SYMBOLS = {
    "BitOr": "|", "BitAnd": "&", "BitXor": "^", "LShift": "<<", "RShift": ">>", "MatMult": "@",
    "Invert": "~",
}


def _unsupported(name: str) -> str:
    """「表达式中不支持列表推导式」「表达式中不支持 lambda」：英文写法前面空一格。"""
    label = _NODE_NAMES.get(name, name)
    return f"表达式中不支持{' ' if label[:1].isascii() else ''}{label}"




class _Unbrace(ast.NodeTransformer):
    """`{{ vars.x }}` 在 Python 里是"装着一个集合的集合"。表达式不允许集合，所以
    它在表达式里只可能是一种笔误：把模板写法带了进来——模型和人都这么写（开发库
    里四次运行就死在"表达式里不允许出现 Set"）。意思毫无歧义，按 vars.x 理解。

    在语法树上剥，不在文本上剥：字符串字面量里的花括号原样保留。
    """

    def __init__(self) -> None:
        self.found: list[str] = []

    def visit_Set(self, node: ast.Set) -> ast.AST:
        self.generic_visit(node)
        inner = node.elts[0] if len(node.elts) == 1 else None
        if isinstance(inner, ast.Set) and len(inner.elts) == 1:
            self.found.append(ast.unparse(inner.elts[0]))
            return inner.elts[0]
        return node


def _check_static(tree: ast.Expression) -> list[str]:
    """不求值，只看结构：违规直接抛 ExpressionError，返回不认识的名字（留给提示用）。

    运行时求值会短路（and/or、三元只走一边），没走到的那一半写错了也发现不了；
    画布校验和 Copilot 自查要的是"整条表达式都能跑"，所以得把整棵树走一遍。
    """
    unknown: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and type(node.op) not in _BIN_OPS:
            if isinstance(node.op, ast.BitOr):
                raise ExpressionError(
                    "表达式中不支持 | 过滤器（那是模板的写法）：取长度请写 len(x)，转换大小写请写 upper(x) / lower(x)"
                )
            op_name = type(node.op).__name__
            raise ExpressionError(f"不支持运算符 {_OP_SYMBOLS.get(op_name, op_name)}")
        if not isinstance(node, _ALLOWED_NODES):
            if isinstance(node, ast.Set):
                raise ExpressionError("表达式中不支持集合写法 {…}：如需判断是否属于其中之一，请使用列表，例如 x in ['a', 'b']")
            raise ExpressionError(_unsupported(type(node).__name__))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_FUNCS:
                called = node.func.id if isinstance(node.func, ast.Name) else ast.unparse(node.func)
                raise ExpressionError(f"不支持函数 {called}()，可用的函数：{'、'.join(_SAFE_FUNCS)}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExpressionError("不允许访问私有属性")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and _TEMPLATE_RE.search(node.value)):
            raise ExpressionError(
                f"字符串「{node.value}」中的 {{{{ }}}} 不会被渲染：表达式不经过模板渲染，请直接写路径，例如 vars.x"
            )
        elif (isinstance(node, ast.Name) and node.id not in EXPRESSION_ROOTS
              and node.id not in _SAFE_FUNCS and node.id not in _LITERAL_NAMES):
            unknown.append(node.id)
    return list(dict.fromkeys(unknown))


def parse_expression(expr: str) -> tuple[ast.Expression, list[str], list[str]]:
    """解析 + 静态检查：(语法树, 被剥掉的 {{ }}, 不认识的名字)。结构违规抛 ExpressionError。

    运行时求值、画布校验、Copilot 自查都走这一个入口——三处各写一套规则的话，
    迟早出现"校验说能跑、跑起来报错"。
    """
    text = (expr or "").strip()
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        hint = "（判断相等请写 ==）" if re.search(r"(?<![=!<>])=(?!=)", text) else ""
        raise ExpressionError(f"表达式语法错误：{e.msg}{hint}") from e
    unbrace = _Unbrace()
    tree = ast.fix_missing_locations(unbrace.visit(tree))
    if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
        raise ExpressionError("表达式过于复杂")
    unknown = _check_static(tree)
    return tree, unbrace.found, unknown


# --------------------------------------------------------------------------
# 引用与代入：口径卡要说清「这个数是拿哪几个输入算出来的」
# --------------------------------------------------------------------------


def _chain_root(node: ast.AST) -> str | None:
    """node 是不是一条从根名字出发的取值链（vars.kpi.gmv、nodes.q.rows[0]）。是就返回根。"""
    cur = node
    while isinstance(cur, (ast.Attribute, ast.Subscript)):
        cur = cur.value
    if isinstance(cur, ast.Name) and cur.id in EXPRESSION_ROOTS:
        return cur.id
    return None


def _cell_call(node: ast.AST) -> bool:
    """cell(<取值链>, row, column)：第一个参数是从根名字出发的取值链，整个调用算一个输入。"""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "cell"
            and len(node.args) == 3 and not node.keywords and _chain_root(node.args[0]) is not None)


def cell_parts(path: str) -> tuple[str, str, str] | None:
    """leaf_refs 给出的 cell(...) 路径拆回三段源码：(取值链, 行, 列)。不是 cell 调用返回 None。"""
    try:
        body = ast.parse(path, mode="eval").body
    except SyntaxError:
        return None
    if not _cell_call(body):
        return None
    return tuple(ast.unparse(a) for a in body.args)  # type: ignore[return-value]


def _walk_refs(node: ast.AST, found: list[ast.AST]) -> None:
    if _cell_call(node):
        # 取哪一格由行、列决定，它们里面的引用是独立的输入；取值链本身只是这一格的来处
        found.append(node)
        for arg in node.args[1:]:
            _walk_refs(arg, found)
        return
    if isinstance(node, (ast.Attribute, ast.Subscript, ast.Name)) and _chain_root(node):
        found.append(node)
        # 链本身不再往里拆（vars.kpi 是 vars.kpi.gmv 的一部分），但下标里的表达式是
        # 独立的输入：vars.rows[vars.i] 取哪一格由 vars.i 决定
        cur = node
        while isinstance(cur, (ast.Attribute, ast.Subscript)):
            if isinstance(cur, ast.Subscript):
                _walk_refs(cur.slice, found)
            cur = cur.value
        return
    for child in ast.iter_child_nodes(node):
        _walk_refs(child, found)


def leaf_refs(tree: ast.AST) -> list[str]:
    """表达式引用了哪些值：每条从根名字出发的最长取值链，按出现顺序去重。

    parse_expression 只告诉你「有没有不认识的名字」；口径卡要记来源，得知道
    round((vars.kpi.gmv - vars.kpi.gmv_prev) / …) 里用到的是 vars.kpi.gmv 和
    vars.kpi.gmv_prev 这两个值，而不是 vars、kpi 这些零件。
    cell(nodes.fetch, 0, 'gmv') 整个调用算一个值：它的来历是那次查询的那一格。
    路径用 ast.unparse 的写法，substitute 按同样的写法认。
    """
    found: list[ast.AST] = []
    _walk_refs(tree, found)
    return list(dict.fromkeys(ast.unparse(n) for n in found))


_SCALARS = (int, float, str, bool, type(None))
#: 字符串长过这个数就不代进去：上游模型写的一大段文本抄进代入式（和 metric_set 工件），
#: 没人看得懂，只是把工件撑大
_INLINE_STR_MAX = 80


def _inlinable(value: Any) -> bool:
    """这个值能不能以字面量的样子写进代入式。

    容器不行；含 {{ 或 }} 的字符串不行——parse_expression 会把整条代入式当成
    「表达式里写了模板」拒掉，复算就成了假的「对不上」；太长的字符串也不代。
    """
    if not isinstance(value, _SCALARS):
        return False
    if isinstance(value, str):
        return len(value) <= _INLINE_STR_MAX and "{{" not in value and "}}" not in value
    return True


def _literal(value: Any) -> ast.expr:
    """负数写成一元负号：Constant(-3) 放在幂运算左边会被 unparse 成 -3 ** 2，
    读起来和算起来都是 -(3 ** 2)。一元负号节点 unparse 时会自己加括号。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
        return ast.UnaryOp(op=ast.USub(), operand=ast.Constant(-value))
    return ast.Constant(value)


class _Substitute(ast.NodeTransformer):
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def _replace(self, node: ast.AST) -> ast.AST:
        if _chain_root(node):
            path = ast.unparse(node)
            if path in self.values and _inlinable(self.values[path]):
                return ast.copy_location(_literal(self.values[path]), node)
            if path in self.values:
                return node      # 容器、含 {{ }} 的或太长的字符串留原路径，复算时照样取得到
        return self.generic_visit(node)

    visit_Attribute = visit_Subscript = visit_Name = _replace

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if _cell_call(node):
            path = ast.unparse(node)
            if path in self.values and _inlinable(self.values[path]):
                return ast.copy_location(_literal(self.values[path]), node)
        return self.generic_visit(node)


def substitute(tree: ast.AST, values: dict[str, Any]) -> str:
    """把引用换成它当时的值，得到代入式：round((45678.5 - 42010.0) / 42010.0 * 100, 1)。

    只代标量（含 {{ }} 或长过 80 字的字符串除外）。代入式本身还是一条合法表达式——拿同一个上下文再求一遍，结果必须和
    原式一样，这是口径卡复算（recompute_ok）的依据。不改动传进来的语法树。
    """
    import copy

    replaced = _Substitute(values).visit(copy.deepcopy(tree))
    return ast.unparse(ast.fix_missing_locations(replaced))


def eval_expression(expr: str, ctx: dict[str, Any], *, track: TruncatedUse | None = None) -> Any:
    """求值一个受限的 Python 表达式。

    只放行字面量、比较、布尔/算术运算、下标、属性访问和一小撮白名单函数。
    没有 import、没有属性魔法、没有函数定义，所以用户在画布上写条件是安全的。

    track：跟踪截断的结果有没有被当成整组用，记进 track.sources（口径卡用，见 TruncatedUse）。
    不给就不跟踪：分支条件、数据整形、复算的行为和以前完全一样。
    """
    expr = (expr or "").strip()
    if not expr:
        return None
    tree, _, _ = parse_expression(expr)

    def _tracked(base_src: Any, full_src: Any, base: Any, key: Any, value: Any) -> Any:
        """取到的值要不要包一层：截断结果的 rows 包成 TruncatedRows，row_count 直接记一笔
        （它就是取回的行数，读它就是在用整组）；调用方经 edge 认定的截断整组也包起来。
        base_src / full_src 是取值链的源码（截断结果本身 / 取到的这个值），要用时才拼。"""
        if track is None or isinstance(value, TruncatedRows):
            return value
        if isinstance(key, str) and key in ("rows", "row_count") and _truncated_table(base):
            rows, artifact = base["rows"], base.get("artifact")
            if key == "row_count":
                track.record(base_src(), len(rows), artifact)
                return value
            return TruncatedRows(rows, track, base_src(), len(rows), artifact)
        if isinstance(value, list) and track.edge is not None:
            path = full_src()
            info = track.edge(path)
            if info:
                return TruncatedRows(value, track, path, int(info.get("rows") or len(value)), info.get("artifact"))
        return value

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in ctx:
                return ctx[node.id]
            if node.id in _SAFE_FUNCS:
                return _SAFE_FUNCS[node.id]
            if node.id in ("true", "True"):
                return True
            if node.id in ("false", "False"):
                return False
            if node.id in ("null", "None"):
                return None
            return None
        if isinstance(node, ast.Attribute):
            base = _eval(node.value)
            if isinstance(base, dict):
                return _tracked(lambda: ast.unparse(node.value), lambda: ast.unparse(node), base, node.attr,
                                base.get(node.attr))
            if node.attr.startswith("_"):
                raise ExpressionError("不允许访问私有属性")
            return getattr(base, node.attr, None)
        if isinstance(node, ast.Subscript):
            base = _eval(node.value)
            key = _eval(node.slice)
            try:
                value = base[key]
            except (KeyError, IndexError, TypeError):
                return None
            return _tracked(lambda: ast.unparse(node.value), lambda: ast.unparse(node), base, key, value)
        if isinstance(node, ast.BinOp):
            op = _BIN_OPS.get(type(node.op))
            if not op:
                raise ExpressionError("不支持的运算符")
            return op(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op = _UNARY_OPS.get(type(node.op))
            if not op:
                raise ExpressionError("不支持的一元运算符")
            return op(_eval(node.operand))
        if isinstance(node, ast.BoolOp):
            # Python 的 and/or 返回的是操作数本身而不是布尔值，并且短路求值。
            # `vars.x or '默认值'` 是模板里最常见的写法，返回 True 就全错了。
            is_and = isinstance(node.op, ast.And)
            result: Any = None
            for i, operand in enumerate(node.values):
                result = _eval(operand)
                truthy = bool(result)
                if i < len(node.values) - 1 and (truthy is not is_and):
                    return result  # and 遇假、or 遇真：短路返回该操作数
            return result
        if isinstance(node, ast.Compare):
            left = _eval(node.left)
            for op_node, comparator in zip(node.ops, node.comparators):
                op = _CMP_OPS.get(type(op_node))
                if not op:
                    raise ExpressionError("不支持的比较运算符")
                right = _eval(comparator)
                try:
                    if not op(left, right):
                        return False
                except TypeError:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return _eval(node.body) if _eval(node.test) else _eval(node.orelse)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_FUNCS:
                raise ExpressionError("只能调用允许的函数")
            args = [_eval(a) for a in node.args]
            kwargs = {kw.arg: _eval(kw.value) for kw in node.keywords if kw.arg}
            result = _SAFE_FUNCS[node.func.id](*args, **kwargs)
            if node.func.id == "get" and len(args) >= 2 and isinstance(args[1], str):
                # get(x, 'rows') 和 x.rows 是同一件事，按 x['rows'] 的写法交给 edge
                source = node.args[0]
                result = _tracked(lambda: ast.unparse(source), lambda: f"{ast.unparse(source)}[{args[1]!r}]",
                                  args[0], args[1], result)
            return result
        if isinstance(node, (ast.List, ast.Tuple)):
            return [_eval(e) for e in node.elts]
        if isinstance(node, ast.Dict):
            return {
                _eval(k): _eval(v)
                for k, v in zip(node.keys, node.values)
                if k is not None
            }
        raise ExpressionError(_unsupported(type(node).__name__))

    result = _eval(tree)
    return _settle(result) if track is not None else result


def eval_condition(expr: str, ctx: dict[str, Any]) -> bool:
    """求值成布尔。条件写错时返回 False 而不是炸掉整个 run。"""
    try:
        return bool(eval_expression(expr, ctx))
    except ExpressionError:
        raise
    except Exception:  # noqa: BLE001
        return False
