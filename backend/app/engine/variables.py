"""工作流里的变量：谁产出、谁引用、现在是什么。

这套编排靠 `{{ }}` 模板串起数据流，而模板取不到值时 **渲染成空字符串**
（resolve_path 返回 None → _stringify → ""）。那是为了让半成品的图也能跑，
代价是一个拼错的变量名不会报错、不会警告、不会在任何地方留下痕迹——它只是
让下游拿到一段空文本，然后模型一本正经地基于空内容编一段回答出来。

所以需要有人在跑之前把这件事说破：

- **引用了没人产出的变量** —— 十有八九是拼错了
- **引用早于产出** —— 变量确实存在，但那个节点在它之后才跑，这时候取到的是空
- **产出了没人用** —— 不一定是错，但常常是改图改了一半的残留

顺带把"这张图里到底有哪些变量"列出来，界面上的变量表和输入补全都用它。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.engine.schema import GraphNode, GraphSpec

_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)

# 这些是状态里天然存在的命名空间，不需要谁去产出
_BUILTIN_ROOTS = {
    "input": "工作流的入口输入",
    "nodes": "各节点的输出",
    "vars": "节点用 assign_to 写入的变量",
    "output": "已收集的成果",
    "loops": "循环的当前项和轮次",
    "usage": "token 与成本累计",
    "messages": "对话历史",
    "last_message": "最近一条消息的文本",
}


class VarRef(BaseModel):
    """一处引用。"""

    node_id: str
    node_label: str
    field: str
    expr: str


class Variable(BaseModel):
    """一个可以被 {{ }} 引用的东西。"""

    path: str
    kind: str          # input | var | node | builtin | loop
    label: str
    #: 谁产出它。builtin 没有产出者
    produced_by: str | None = None
    produced_by_label: str | None = None
    #: 产出者在拓扑序里的层级，用来判断引用是不是太早
    order: int = -1
    refs: list[VarRef] = Field(default_factory=list)
    #: 这个变量声明时带的说明（input 字段的 description）
    description: str = ""


class VarIssue(BaseModel):
    level: str         # error | warning | info
    message: str
    node_id: str | None = None
    path: str = ""


class VariableReport(BaseModel):
    variables: list[Variable] = Field(default_factory=list)
    issues: list[VarIssue] = Field(default_factory=list)


# --------------------------------------------------------------------------


def _depths(spec: GraphSpec) -> dict[str, int]:
    """每个节点的拓扑层级（最长路径）。有环就在环上停住，不死循环。"""
    incoming: dict[str, list[str]] = {n.id: [] for n in spec.nodes}
    for e in spec.edges:
        if e.target in incoming and e.source in incoming:
            incoming[e.target].append(e.source)

    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def walk(node_id: str) -> int:
        if node_id in depth:
            return depth[node_id]
        if node_id in visiting:      # 环：当成同层，别递归下去
            return 0
        visiting.add(node_id)
        parents = incoming.get(node_id) or []
        depth[node_id] = 0 if not parents else max(walk(p) for p in parents) + 1
        visiting.discard(node_id)
        return depth[node_id]

    for n in spec.nodes:
        walk(n.id)
    return depth


#: 按 AST 白名单求值的字段：里面是裸表达式，不带 {{ }}。
#: cases 是数组，单独处理。
_EXPRESSION_FIELDS = ("expression", "condition", "skip_if")

#: 裸引用：vars.x / input.x / nodes.x[.y…]。只认这三个根，
#: 免得把 str(...) 里的普通标识符也当成变量引用
_BARE_REF_RE = re.compile(r"\b(?:vars|input|nodes)\.[A-Za-z_][\w.]*")


def _iter_expressions(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """节点配置里那些按表达式求值的字段。"""
    out: list[tuple[str, str]] = []
    for key in _EXPRESSION_FIELDS:
        value = cfg.get(key)
        if isinstance(value, str) and value.strip():
            out.append((key, value))
    for i, case in enumerate(cfg.get("cases") or []):
        cond = (case or {}).get("condition")
        if isinstance(cond, str) and cond.strip():
            out.append((f"cases[{i}].condition", cond))
    return out


def _loop_bodies(spec: GraphSpec) -> dict[str, set[str]]:
    """每个循环节点的"体"里有哪些节点。

    循环变量（item / item_index）只在体内部有值，而拓扑深度在这里完全不可靠：
    循环体末尾有一条回到循环节点的边，于是循环节点的深度被回边推高，反而排在
    自己的体后面。模板⑦就因此被报成"{{ vars.item }} 由「逐条循环」产出，
    但那一步在这之后才跑"——而那是循环最正常的写法。

    从 body 出口走可达闭包，遇到循环节点自己就停（那就是回边）。
    """
    bodies: dict[str, set[str]] = {}
    outgoing: dict[str, list[tuple[str | None, str]]] = {}
    for e in spec.edges:
        outgoing.setdefault(e.source, []).append((e.sourceHandle, e.target))

    for node in spec.nodes:
        if node.type != "loop":
            continue
        seen: set[str] = set()
        stack = [t for h, t in outgoing.get(node.id, []) if h == "body"]
        while stack:
            cur = stack.pop()
            if cur in seen or cur == node.id:
                continue
            seen.add(cur)
            stack.extend(t for _, t in outgoing.get(cur, []))
        bodies[node.id] = seen
    return bodies


def _iter_strings(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    """把配置里所有字符串摊平成 (字段路径, 文本)。"""
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        if "{{" in value:
            out.append((prefix or "config", value))
    elif isinstance(value, dict):
        for k, v in value.items():
            out.extend(_iter_strings(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.extend(_iter_strings(v, f"{prefix}[{i}]"))
    return out


def _root_of(expr: str) -> tuple[str, str]:
    """把 `vars.sales | json` 拆成 ('vars.sales', 'vars')。"""
    path = expr.split("|")[0].strip()
    # 去掉下标，vars.rows[0].name → vars.rows.name
    clean = re.sub(r"\[[^\]]*\]", "", path)
    root = clean.split(".")[0].strip()
    return clean, root


def _label_of(node: GraphNode) -> str:
    return node.data.label or node.id


def _resolve_fallback(
    path: str, index: dict[str, "Variable"], *, input_is_closed: bool = True
) -> "Variable | None":
    """精确路径没命中时，看看能不能退一层认下来。

    分寸全在这里：退得太松，`{{ input.quesiton }}` 会退到 `input` 被当成合法，
    拼写检查整个失效；退得太紧，`{{ loops.item }}` 会被误报——而 loops 的成员
    是运行期才有的，我根本枚举不了。

    所以按命名空间分开对待：**成员可枚举的不许退，不可枚举的退到根**。
    """
    parts = [p for p in path.split(".") if p]
    if not parts:
        return None
    root = parts[0]

    # 裸根本身就是合法引用：{{ input }} 取整个输入 dict
    if len(parts) == 1:
        return index.get(root)

    # 入口不声明字段时，input.* 是开放的：运行时传什么键都行（问数据页就是
    # 这样——问题由系统注入成 input.question，入口节点里一个字段都没有）。
    # 声明了字段才是闭集，那时候拼错才叫拼错。
    if root == "input" and not input_is_closed:
        return index.get("input")

    # 这三个的第二段可枚举（入口字段名、assign_to、节点 id），必须存在。
    # 再往下就是在往那个值**内部**钻了（vars.rows[0].name、nodes.a.text），
    # 值的结构是运行期才有的，枚举不了，所以只校验到第二段
    if root in ("input", "vars", "nodes"):
        return index.get(f"{root}.{parts[1]}")

    # loops.item / usage.total_tokens / messages[0].content 这类，
    # 成员是运行期的状态，枚举不了，根存在就算数
    return index.get(root)


def analyze(spec: GraphSpec) -> VariableReport:
    """静态分析一张图的变量。不需要跑，也不需要有运行记录。"""
    depth = _depths(spec)
    bodies = _loop_bodies(spec)
    report = VariableReport()
    index: dict[str, Variable] = {}

    def ensure(path: str, **kw: Any) -> Variable:
        if path not in index:
            index[path] = Variable(path=path, **kw)
        return index[path]

    # ---- 产出侧 ----
    for node in spec.nodes:
        cfg = node.data.config or {}
        label = _label_of(node)
        order = depth.get(node.id, 0)

        if node.type == "input":
            for field in cfg.get("fields") or []:
                name = str((field or {}).get("name") or "").strip()
                if not name:
                    continue
                desc = str((field or {}).get("description") or "")
                ensure(
                    f"input.{name}", kind="input", label=f"入口输入「{name}」",
                    produced_by=node.id, produced_by_label=label, order=order,
                    description=desc,
                )
                # 入口节点同时把每个字段写进 vars（run_input 的返回里
                # input 和 vars 是同一份）。不认这一条的话，{{ vars.topic }}
                # 这种完全合法的写法会被报成"未定义"——误报是 linter 最坏的
                # 失败模式，它会让人把整个校验一起无视掉
                ensure(
                    f"vars.{name}", kind="input", label=f"入口输入「{name}」（同名变量）",
                    produced_by=node.id, produced_by_label=label, order=order,
                    description=desc,
                )

        # 循环节点每轮往 vars 里注入当前项和它的序号（control.py:142）。
        # 产出侧漏了这一条，于是模板⑦「批量处理」里完全正确的 {{ vars.item }}
        # 被报成"没有任何节点产出"——那是唯一一个装好就红着的内置模板，
        # 新用户点开第一眼看到的就是个报错，而运行按钮还是灰的。
        #
        # 误报是 linter 最坏的失败模式：它会让人把整个校验一起无视掉。
        # kind="loop" 在 _KIND_ORDER 里一直有位置，只是从来没人产出过。
        if node.type == "loop" and str(cfg.get("mode") or "foreach") == "foreach":
            # while 模式不遍历列表，自然也没有"当前项"
            item_var = str(cfg.get("item_var") or "item").strip()
            if item_var:
                ensure(
                    f"vars.{item_var}", kind="loop", label=f"「{label}」每轮的当前项",
                    produced_by=node.id, produced_by_label=label, order=order,
                    description="只在循环体内部有值",
                )
                ensure(
                    f"vars.{item_var}_index", kind="loop", label=f"「{label}」每轮的序号",
                    produced_by=node.id, produced_by_label=label, order=order,
                    description="从 0 开始，只在循环体内部有值",
                )

        var_name = str(cfg.get("assign_to") or "").strip()
        if var_name:
            ensure(
                f"vars.{var_name}", kind="var", label=f"「{label}」写入的变量",
                produced_by=node.id, produced_by_label=label, order=order,
            )

        # 每个节点的输出都能用 nodes.<id> 引用
        ensure(
            f"nodes.{node.id}", kind="node", label=f"「{label}」这一步的输出",
            produced_by=node.id, produced_by_label=label, order=order,
        )

    # 每个根命名空间本身也是合法引用：{{ input }} 取整个输入 dict，
    # {{ vars }} 取整个变量池——template_context 里它们就是顶层的键。
    # 上一版把 input/nodes/vars 跳过了（想着"具体成员已经列过"），结果
    # {{ input }} 被报成"没有任何节点产出"，而那是完全正确的写法。
    # 误报会把整张图卡住：runs 启动前要过校验，一条假错误就意味着什么都跑不了。
    for root, desc in _BUILTIN_ROOTS.items():
        ensure(root, kind="builtin", label=desc)

    # 入口声明了字段，input.* 才是闭集；一个字段都没声明时运行时传什么键都行
    input_is_closed = any(
        n.type == "input" and (n.data.config or {}).get("fields") for n in spec.nodes
    )

    # ---- 引用侧 ----
    for node in spec.nodes:
        cfg = node.data.config or {}
        label = _label_of(node)
        order = depth.get(node.id, 0)
        # 表达式字段里写的是**裸**引用（vars.one、input.n），没有 {{ }}。
        # 只扫模板语法的话，模板⑦里 collect 那个
        # `(vars.collected or '') + str(vars.one)` 就一个引用都认不出来，
        # 于是 vars.one 被报成"产出了但没有任何地方引用"——而它明明在用。
        for field, expr_text in _iter_expressions(cfg):
            for m in _BARE_REF_RE.finditer(expr_text):
                path, root = _root_of(m.group(0))
                if not path:
                    continue
                target = index.get(path)
                if target is not None:
                    target.refs.append(VarRef(
                        node_id=node.id, node_label=label, field=field, expr=m.group(0),
                    ))

        for field, text in _iter_strings(cfg):
            for m in _TEMPLATE_RE.finditer(text):
                expr = m.group(1).strip()
                if not expr:
                    continue
                path, root = _root_of(expr)
                if not path:
                    continue
                ref = VarRef(node_id=node.id, node_label=label, field=field, expr=expr)

                target = index.get(path)
                if target is None:
                    target = _resolve_fallback(path, index, input_is_closed=input_is_closed)
                if target is not None:
                    target.refs.append(ref)
                    # 循环变量单独判：它只在循环体内部有值，而循环体的拓扑
                    # 深度因为那条回边天然比循环节点小，套用下面的先后判定
                    # 必然误报（模板⑦就是这么红的）
                    if target.kind == "loop" and target.produced_by:
                        if node.id not in bodies.get(target.produced_by, set()):
                            report.issues.append(VarIssue(
                                level="warning", node_id=node.id, path=path,
                                message=f"{{{{ {path} }}}} 是「{target.produced_by_label}」"
                                        f"每轮的循环变量，只在循环体内部有值；"
                                        f"这一步不在循环体里，取到的会是空值",
                            ))
                        continue

                    # 变量存在，但产出它的节点排在引用者之后——跑到这儿时它还是空的
                    if (
                        target.produced_by
                        and target.produced_by != node.id
                        and target.order > order
                    ):
                        report.issues.append(VarIssue(
                            level="warning", node_id=node.id, path=path,
                            message=f"{{{{ {path} }}}} 由「{target.produced_by_label}」产出，"
                                    f"但那一步在这之后才跑，这里取到的会是空值",
                        ))
                    continue

                # 谁都不认识它。模板取不到值会渲染成空字符串，不报错——
                # 所以这条必须在跑之前说出来
                # 严重度看"我有多确信"。这条会拦住整次运行（runs 启动前要过
                # 校验），所以只在真的能穷举时才报 error：
                #
                # - vars.X / input.X / nodes.X —— 这三个命名空间的成员全是
                #   我从图里扫出来的，没扫到就是真没有，多半是拼错
                # - 根本不认识的根 —— 可能只是我没建模的东西，降成 warning。
                #   假错误比漏报贵得多：漏报只是少提醒一次，假错误让整张图跑不了
                known_root = root in _BUILTIN_ROOTS
                hint = _did_you_mean(path, index)
                report.issues.append(VarIssue(
                    level="error" if known_root else "warning",
                    node_id=node.id, path=path,
                    message=(
                        f"{{{{ {path} }}}} 没有任何节点产出，"
                        f"是不是想写 {{{{ {hint} }}}}？取不到值会渲染成空字符串，不报错"
                        if hint else
                        f"{{{{ {path} }}}} 没有任何节点产出。"
                        f"取不到值会渲染成空字符串，不报错"
                    ),
                ))

    # ---- 产出了没人用 ----
    for var in index.values():
        if var.kind in ("builtin", "node"):
            continue      # nodes.<id> 是自动存在的，没人引用很正常
        # 只对**作者声明**的变量报"没人用"。系统自动提供的不算：
        #   - 循环变量（item / item_index）：循环本来就两个都注入，
        #     用不用是作者的事，提醒他"你没用 item_index"纯属噪音
        #   - vars.<入口字段> 这个别名：真正的 input.<字段> 用着呢，
        #     拿别名去报未引用只会让人以为哪里写错了
        # 内置变量（last_message 等）没有 produced_by，本来就不在这条路上。
        auto_provided = var.kind == "loop" or (
            var.kind == "input" and var.path.startswith("vars.")
        )
        if not var.refs and var.produced_by and not auto_provided:
            report.issues.append(VarIssue(
                level="info", node_id=var.produced_by, path=var.path,
                message=f"{{{{ {var.path} }}}} 产出了但没有任何地方引用",
            ))

    report.variables = sorted(
        index.values(), key=lambda v: (_KIND_ORDER.get(v.kind, 9), v.order, v.path)
    )
    return report


_KIND_ORDER = {"input": 0, "var": 1, "node": 2, "loop": 3, "builtin": 4}


def _did_you_mean(path: str, index: dict[str, Variable]) -> str:
    """拼错是这里最常见的错误，能猜就直说，别只讲"不存在"。"""
    import difflib

    matches = difflib.get_close_matches(path, list(index), n=1, cutoff=0.7)
    return matches[0] if matches else ""
