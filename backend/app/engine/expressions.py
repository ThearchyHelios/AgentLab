from __future__ import annotations

import ast
import json
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
}


class ExpressionError(ValueError):
    pass


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
                    "表达式里没有 | 过滤器（那是模板的写法）：取长度写 len(x)，大小写写 upper(x) / lower(x)"
                )
            raise ExpressionError(f"不支持的运算符 {type(node.op).__name__}")
        if not isinstance(node, _ALLOWED_NODES):
            if isinstance(node, ast.Set):
                raise ExpressionError("表达式里不支持集合 {…}：要判断是不是其中之一，用列表 x in ['a', 'b']")
            name = type(node).__name__
            raise ExpressionError(f"表达式里不允许出现 {_NODE_NAMES.get(name, name)}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_FUNCS:
                called = node.func.id if isinstance(node.func, ast.Name) else ast.unparse(node.func)
                raise ExpressionError(f"不认识的函数 {called}()，能用的只有：{'、'.join(_SAFE_FUNCS)}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExpressionError("不允许访问私有属性")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and _TEMPLATE_RE.search(node.value)):
            raise ExpressionError(
                f"字符串 {node.value!r} 里的 {{{{ }}}} 不会被渲染——表达式不过模板，直接写路径，比如 vars.x"
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
        hint = "（判断相等要写 ==）" if re.search(r"(?<![=!<>])=(?!=)", text) else ""
        raise ExpressionError(f"表达式语法错误：{e.msg}{hint}") from e
    unbrace = _Unbrace()
    tree = ast.fix_missing_locations(unbrace.visit(tree))
    if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
        raise ExpressionError("表达式太复杂")
    unknown = _check_static(tree)
    return tree, unbrace.found, unknown


def eval_expression(expr: str, ctx: dict[str, Any]) -> Any:
    """求值一个受限的 Python 表达式。

    只放行字面量、比较、布尔/算术运算、下标、属性访问和一小撮白名单函数。
    没有 import、没有属性魔法、没有函数定义，所以用户在画布上写条件是安全的。
    """
    expr = (expr or "").strip()
    if not expr:
        return None
    tree, _, _ = parse_expression(expr)

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
                return base.get(node.attr)
            if node.attr.startswith("_"):
                raise ExpressionError("不允许访问私有属性")
            return getattr(base, node.attr, None)
        if isinstance(node, ast.Subscript):
            base = _eval(node.value)
            key = _eval(node.slice)
            try:
                return base[key]
            except (KeyError, IndexError, TypeError):
                return None
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
                raise ExpressionError("只能调用白名单函数")
            args = [_eval(a) for a in node.args]
            kwargs = {kw.arg: _eval(kw.value) for kw in node.keywords if kw.arg}
            return _SAFE_FUNCS[node.func.id](*args, **kwargs)
        if isinstance(node, (ast.List, ast.Tuple)):
            return [_eval(e) for e in node.elts]
        if isinstance(node, ast.Dict):
            return {
                _eval(k): _eval(v)
                for k, v in zip(node.keys, node.values)
                if k is not None
            }
        raise ExpressionError(f"表达式里不允许出现 {type(node).__name__}")

    return _eval(tree)


def eval_condition(expr: str, ctx: dict[str, Any]) -> bool:
    """求值成布尔。条件写错时返回 False 而不是炸掉整个 run。"""
    try:
        return bool(eval_expression(expr, ctx))
    except ExpressionError:
        raise
    except Exception:  # noqa: BLE001
        return False
