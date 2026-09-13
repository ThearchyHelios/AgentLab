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

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
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


class ExpressionError(ValueError):
    pass


def eval_expression(expr: str, ctx: dict[str, Any]) -> Any:
    """求值一个受限的 Python 表达式。

    只放行字面量、比较、布尔/算术运算、下标、属性访问和一小撮白名单函数。
    没有 import、没有属性魔法、没有函数定义，所以用户在画布上写条件是安全的。
    """
    expr = (expr or "").strip()
    if not expr:
        return None
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"表达式语法错误：{e.msg}") from e

    if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
        raise ExpressionError("表达式太复杂")

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
