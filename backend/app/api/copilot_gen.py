"""助手的生成过程（总方案 L1，look 文档 D9）：画布改图、问数据、发布前修复、升级证据、应用润色共用一个循环。

循环只管四件事：读到 look 就停下、执行查看、接着写、计次数和上限。怎么落地（Applier）、怎么自查（Checker）由调用方
注入（app/look/base.py 的协议）：图用 GraphApplier / GraphChecker，应用用 AppApplier / AppChecker。

**接着写时的消息结构**（look 文档 §2.5.5）：
  [system]     和起草时相同，不变
  [human]      这一轮的请求（含历史）
  [assistant]  模型到 look 那一行为止的输出（look 之后的内容已丢弃）
  [human]      查看结果 + 「接着输出操作，已经落地的不要重复」+ 已落地的对象 id
看几次就重复几组 assistant / human。已落地的操作由 Applier 去重：完全相同的 add_edge 忽略、和已落地节点完全相同的
add_node 不转发、plan 只留第一次。

**ask。** 模型输出 {"op":"ask","question":…} 时这一轮就结束：循环里不等用户回答，不再自查修正；已经落地的改动照常
交付（附上自查问题），用户的回答就是下一轮。

**自查修正。** 起草完由 Checker 找出挡路的问题，交回模型改（最多 SELF_CHECK_ROUNDS 轮）；修正请求里带上这一轮的查看
结果（和起草时看到的是同一份文字），修正阶段另给 REPAIR_LOOK_LIMIT 次查看。

copilot.py 原来的读流（_iter_ops）、解析（_parse_op_line）、落地（_apply_op、merge_node_config）搬到这里，copilot.py
照原名再导出。
"""
from __future__ import annotations

import copy
import json
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from app.engine.schema import NodeType
from app.look.base import LOOK_LIMIT, REPAIR_LOOK_LIMIT, Applied, Applier, Checker, LookBudget
from app.look.service import LookService
from app.providers.budget import cut_off as _cut_off

#: 自查最多交回去改几轮。一轮只花一次"只输出改动"的调用；两轮还改不好，多半是需求本身有歧义，该让人看了
SELF_CHECK_ROUNDS = 2
#: 读到这几种操作就停下：done 说完了，look 要查看，ask 要等用户回答
_STOP_OPS = ("done", "look", "ask")
#: 不占次数的查看回话（need 没写、次数用完）连着来几次就不再接着写：模型在原地打转，再要下去只是花钱
_IDLE_LOOKS = 2


# ==========================================================================
# 读流
# ==========================================================================


def _parse_op_line(line: str) -> dict[str, Any] | None:
    """解析一行操作。围栏行、空行、解析失败、缺 op 的一律返回 None——
    模型偶尔犯规不该毁掉整条流，跳过就是。"""
    text = line.strip()
    if not text or text.startswith("```"):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "op" not in obj:
        return None
    return obj


class CopilotIncomplete(Exception):
    """模型这一轮没说完：输出被截断，或者一个操作都没给。画布不能按「无需修改」交付。"""

    def __init__(self, reason: str, *, hint: str = "", detail: str = "") -> None:
        super().__init__(reason)
        self.hint = hint or "画布未修改。可以重试，或把需求拆成几步分别提出"
        self.detail = detail


async def _iter_ops(model: Any, messages: list[Any], *, lines: list[str] | None = None):
    """流式读模型输出，按行切出操作。done、look、ask 之后即停——后面就算有闲话也不要了；look 之后停下也就是
    「读到这一行就停止本次输出」，模型那条流随之关掉。

    除了操作，还把模型的思考原样转出去（thinking_text：Claude 的 thinking 块、OpenAI 兼容系的 reasoning_content），
    不把非 JSON 行当成思考。lines 给了的话，认出来的操作行原样记进去：接着写时作为 assistant 消息交回模型。

    没等到 done 就因为额度用完停下的，抛 CopilotIncomplete：截断之前的操作照常转出去，由调用方决定整轮作废。
    """
    from app.engine.state import message_text as _text
    from app.engine.state import thinking_text

    buffer = ""
    cut = ""
    async with aclosing(model.astream(messages)) as stream:
        async for chunk in stream:
            reasoning = thinking_text(chunk)
            if reasoning:
                yield {"op": "thinking", "delta": reasoning}

            buffer += _text(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                op = _parse_op_line(line)
                if op:
                    if lines is not None:
                        lines.append(line.strip())
                    yield op
                    if op.get("op") in _STOP_OPS:
                        return
            cut = _cut_off(chunk) or cut
    op = _parse_op_line(buffer)
    if op:
        if lines is not None:
            lines.append(buffer.strip())
        yield op
        if op.get("op") in _STOP_OPS:
            return
    if cut:
        # 截断的流和说完了的流读起来一模一样：不在这里拦下，上面就会拿原图（或半张图）
        # 去自查、交付，界面上写「无需修改」
        limit = getattr(model, "max_tokens", None)
        raise CopilotIncomplete(
            "模型的输出超出了长度上限，修改没有写完",
            detail=f"finish_reason={cut}" + (f"，本轮输出上限 {limit} token" if limit else ""))


# ==========================================================================
# 图的落地
# ==========================================================================

_NODE_TYPES = frozenset(t.value for t in NodeType)

#: 协作成员条目里写 "remove": true 就把这个成员拿掉。不复用 null：null 在合并里
#: 的意思是「删这个字段」，成员本身不是一个字段
_REMOVE_MEMBER = "remove"


def _member_key(member: dict[str, Any], index: int) -> str:
    # 和 engine/nodes/multi.py 的叫法一致：没起名字的成员按位置叫 agentN
    return str(member.get("name") or f"agent{index}")


def _merge_fields(old: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """按字段合并：写了的覆盖，写 null 的删掉，没写的原样保留。返回新 dict。"""
    out = dict(old)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def _merge_members(old: list[Any], patch: list[Any]) -> list[Any]:
    """协作成员按 name 合并。原有成员保持原来的次序，新名字接在后面。"""
    merged: list[Any] = list(old)
    where = {_member_key(m, i): i for i, m in enumerate(merged) if isinstance(m, dict)}
    dropped: set[int] = set()
    for i, entry in enumerate(patch):
        if not isinstance(entry, dict):
            continue
        key = _member_key(entry, i)
        if entry.get(_REMOVE_MEMBER) is True:
            if key in where:
                dropped.add(where[key])
            continue
        fields = {k: v for k, v in entry.items() if k != _REMOVE_MEMBER}
        if key in where:
            merged[where[key]] = _merge_fields(merged[where[key]], fields)
        else:
            where[key] = len(merged)
            merged.append({k: v for k, v in fields.items() if v is not None})
    return [m for i, m in enumerate(merged) if i not in dropped]


def merge_node_config(
    node_type: str, old: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """update_node 的 config 怎么落到节点上。前端 store/studio.ts 必须是同一套规则。

    以前是整体替换：模型只想改一句提示词，照协议也得把整份 config 抄一遍，
    漏抄的字段就没了。真出过事——改写提示词时漏了 tools，三个要查库的 agent
    全丢了工具，一次库都没查，最后把一堆工具调用的原始标记当答案交了出去。
    改成按字段合并之后，漏写等于不改；真要删一个字段得显式写 null。
    """
    if node_type == NodeType.SUPERVISOR.value and isinstance(patch.get("agents"), list):
        members = old.get("agents") if isinstance(old.get("agents"), list) else []
        patch = {**patch, "agents": _merge_members(members, patch["agents"])}
    return _merge_fields(old, patch)


def _apply_op(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], op: dict[str, Any]) -> bool:
    """把一个操作应用到服务端维护的图状态。返回是否真的改了图。"""
    kind = op.get("op")
    if kind == "add_node":
        node = op.get("node") or {}
        # 类型不认识的节点不进图、也不转给前端：前端按类型查节点定义，查不到
        # 以前是整站白屏，连带没保存的编辑一起丢
        if not node.get("id") or node.get("type") not in _NODE_TYPES:
            return False
        nodes[node["id"]] = {
            "id": node["id"],
            "type": node["type"],
            "position": {"x": 0, "y": 0},
            "data": {"label": node.get("label") or "", "config": node.get("config") or {}},
        }
        return True
    if kind == "update_node":
        node = nodes.get(op.get("id") or "")
        if not node:
            return False
        data = node.setdefault("data", {})
        if op.get("label") is not None:
            data["label"] = op["label"]
        if isinstance(op.get("config"), dict):
            data["config"] = merge_node_config(
                str(node.get("type") or ""), data.get("config") or {}, op["config"])
        return True
    if kind == "remove_node":
        node_id = op.get("id")
        if node_id not in nodes:
            return False
        nodes.pop(node_id)
        edges[:] = [e for e in edges if e["source"] != node_id and e["target"] != node_id]
        return True
    if kind == "add_edge":
        edge = op.get("edge") or {}
        if not edge.get("source") or not edge.get("target"):
            return False
        edges.append(
            {
                "source": edge["source"],
                "target": edge["target"],
                "sourceHandle": edge.get("sourceHandle"),
            }
        )
        return True
    if kind == "remove_edge":
        before = len(edges)
        edges[:] = [
            e
            for e in edges
            if not (
                e["source"] == op.get("source")
                and e["target"] == op.get("target")
                and (op.get("sourceHandle") is None or e.get("sourceHandle") == op.get("sourceHandle"))
            )
        ]
        return len(edges) != before
    return False


class GraphApplier:
    """图的落地：_apply_op 改服务端维护的图（nodes、edges 原地改，调用方手里的同一份），外加这一轮要记下的东西。

    - seen：起草时收到的图操作（含 plan、done），兜底重放到上一轮的图上时用（replay_onto）
    - tried / effective：试过的、真改了图的操作（发布前修复、升级按它们复核）
    - replied：模型说这句话不需要工作流时的 reply；之后只再收目录修改提案
    - questions：发布前修复、升级里模型要人拿主意的 question（questions=True 时 reply 也算在里面）
    - skipped_types：模型编出来的节点类型，收尾时说出来

    patches：目录修改提案转不转（只有用户亲口说了数据事实的画布、问数据才转）。
    """

    def __init__(self, nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], *, patches: bool = True,
                 questions: bool = False) -> None:
        self.nodes = nodes
        self.edges = edges
        self.patches = patches
        self.questions_mode = questions
        self.seen: list[dict[str, Any]] = []
        self.tried: list[dict[str, Any]] = []
        self.effective: list[dict[str, Any]] = []
        self.skipped_types: list[str] = []
        self.questions: list[dict[str, Any]] = []
        self.replied: dict[str, Any] | None = None
        self.plan: dict[str, Any] | None = None
        self.explanation = ""
        # 建完要不要跑，由模型在 done.run 里表态
        self.autorun = True
        self._landed: list[str] = []
        self._continuing = False
        self._repairing = False

    def apply(self, op: dict[str, Any]) -> Applied:
        kind = op.get("op")
        if kind == "catalog_patch":
            # 目录修改提案不进图：转给调用方核对后做成卡片（人点「保存」才写）
            return Applied(False, self.patches and not self._repairing)
        if kind in ("question", "reply") and self.questions_mode:
            self.questions.append(op)
            return Applied(False, False)
        if self._repairing and kind == "reply":
            return Applied(False, False)
        if self.replied is not None:
            return Applied(False, False)
        if kind == "reply":
            # 这句话不需要工作流：先压着，等模型说完（只再收它后面的提案）由调用方转出、收尾
            self.replied = op
            return Applied(False, False)
        if kind == "plan":
            if self.plan is not None and self._continuing:
                return Applied(False, False)
            self.plan = self.plan or op
            self.seen.append(op)
            return Applied(False, True)
        if kind == "done":
            self.explanation = str(op.get("explanation", ""))
            # 用户要的是流程本身时（"设计一个每天跑的工作流"），建完就跑等于替他多花一次钱
            self.autorun = op.get("run") is not False
            self.seen.append(op)
            return Applied(False, True)
        if self._continuing and self._repeats(op):
            return Applied(False, False)
        self.seen.append(op)
        self.tried.append(op)
        changed = _apply_op(self.nodes, self.edges, op)
        if changed:
            self.effective.append(op)
            nid = (op.get("node") or {}).get("id") if kind == "add_node" else op.get("id")
            if isinstance(nid, str) and nid not in self._landed and kind in ("add_node", "update_node"):
                self._landed.append(nid)
        elif kind == "add_node":
            bad = (op.get("node") or {}).get("type")
            if bad and bad not in _NODE_TYPES:
                self.skipped_types.append(str(bad))
        return Applied(changed, changed)

    def _repeats(self, op: dict[str, Any]) -> bool:
        """接着写时和已落地完全相同的操作（look §2.5.5）：已有的同一条连线、和已落地节点一模一样的节点、重发的同一个操作。"""
        kind = op.get("op")
        if kind == "add_edge":
            edge = op.get("edge") or {}
            return any(e["source"] == edge.get("source") and e["target"] == edge.get("target")
                       and e.get("sourceHandle") == edge.get("sourceHandle") for e in self.edges)
        if kind == "add_node":
            node = op.get("node") or {}
            have = self.nodes.get(str(node.get("id") or ""))
            return have is not None and have.get("type") == node.get("type") \
                and (have.get("data") or {}).get("label") == (node.get("label") or "") \
                and (have.get("data") or {}).get("config") == (node.get("config") or {})
        return op in self.effective

    def landed(self) -> list[str]:
        return list(self._landed)

    def continuing(self) -> None:
        self._continuing = True

    def repairing(self) -> None:
        self._repairing = True

    def replay_onto(self, graph: dict[str, Any]) -> None:
        """一个操作都没落地、而上一轮有图时：模型把这一轮当成「改上一张图」，只发了改动操作，而累加器是从空图起步的。
        与其报一句「图是空的」，不如按它本来的意思做——把这些操作重放到上一轮那张图上。"""
        self.nodes.clear()
        self.nodes.update({n["id"]: copy.deepcopy(n) for n in graph.get("nodes") or [] if n.get("id")})
        # 原地改，不能重新绑定：调用方手里拿的是同一份
        self.edges[:] = [{k: v for k, v in e.items() if k in ("source", "target", "sourceHandle") and v}
                         for e in graph.get("edges") or []]
        for op in self.seen:
            _apply_op(self.nodes, self.edges, op)


class GraphChecker:
    """图的自查：按运行时同一套规则（copilot._blocking_issues）挑出挡路的问题，交回模型改。

    known 给了（升级证据）：只把这一轮冒出来的问题交回去（图上原来就有的不归这一轮管），一个修改都没落地时不修。
    """

    def __init__(self, applier: GraphApplier, *, scope: set[str] | None = None, sources: list[Any] | None = None,
                 baseline: list[dict[str, Any]] | None = None, level: str | None = None,
                 defaults: dict[str, Any] | None = None, checkers: dict[str, Any] | None = None,
                 known: set[Any] | None = None, chat_user: bool = False) -> None:
        self.applier = applier
        #: 图要以开登录后 user 角色的身份在问数据里发起（D-c）：使用者不能用的节点、工具也算挡路
        self.chat_user = chat_user
        self.scope = scope
        self.sources = sources
        self.baseline = baseline
        self.level = level
        self.defaults = defaults
        self.checkers = checkers
        self.known = known

    def blocking(self) -> list[dict[str, Any]] | None:
        from app.api.copilot import _blocking_issues

        return _blocking_issues(self.applier.nodes, self.applier.edges, self.scope, sources=self.sources,
                                baseline=self.baseline, level=self.level, defaults=self.defaults,
                                checkers=self.checkers, chat_user=self.chat_user)

    async def issues(self) -> list[dict[str, Any]]:
        errors = self.blocking()
        if self.known is None:
            return errors or []
        from app.engine.autofix import _sig

        if not self.applier.effective:
            return []
        return [e for e in errors or [] if _sig(e) not in self.known]

    def repair_request(self, issues: list[dict[str, Any]], looks_text: str) -> str:
        from app.api.copilot import _repair_request

        return _repair_request(self.applier.nodes, self.applier.edges, issues, looks=looks_text)


# ==========================================================================
# 应用的落地
# ==========================================================================

APP_REPAIR_PROMPT = (
    "这是你刚改好的应用草稿：\n{spec}\n\n"
    "应用检查（和发布同一套规则）发现下面这些问题：\n{issues}\n\n"
    "只修这些问题，别的不要动。用应用操作输出修正；岗位绑定要人来做，不要替人绑。最后一行输出 done。{looks}"
)
REPAIR_LOOKS_PROMPT = "\n\n这一轮查看过的内容（和起草时看到的是同一份）：\n{text}"

#: 应用检查里只有人能处理的问题：钉住哪个版本、升版怎么处置、岗位和核实人绑给谁、授权、通知渠道。不交回模型改，
#: 照常列在交付的问题里
APP_HUMAN_ONLY = frozenset({
    "app.pin_unpublished", "app.pin_retired", "app.pin_hash", "app.upgrade_policy", "app.slot_unbound",
    "app.reconciler_unbound", "app.acl_empty", "app.notify_channel_missing", "app.process_unsafe",
    "app.view_source_empty", "subgraph_version_unpublished", "subgraph_version_retired",
})


def _components(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(item)
            out += _components(item.get("children") or [])
    return out


def app_component(spec: dict[str, Any], component_id: str) -> dict[str, Any] | None:
    """spec 里按 id 找组件（各页的组件树，含子组件）。"""
    for page in spec.get("pages") or []:
        for item in _components(page.get("body") or [] if isinstance(page, dict) else []):
            if item.get("id") == component_id:
                return item
    return None


class AppApplier:
    """应用的落地：app/apps/ops.apply_app_op 改 spec（原地改），外加接着写时的去重。

    plan、done 记下说明；reply 记下（这句话不需要改应用）；图操作、目录修改提案在这里一概不收。

    润色只改呈现（D-h，app/apps/ops.polish_problem）：组件操作、页面、待办外观（task_ui）、表单的外观键照收；改钉住的
    版本、入口、视图、岗位、表单的数据约束一律拒（Applied(False, False, problem)）。拒掉的原因记在 refused 里：自查时
    交回模型（AppChecker.issues），交付时列给人看（refusals），不悄悄吞掉。
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.effective: list[dict[str, Any]] = []
        self.plan: dict[str, Any] | None = None
        self.replied: dict[str, Any] | None = None
        self.explanation = ""
        #: 越界被拒的操作：[{op, path, message}]，按到来的顺序
        self.refused: list[dict[str, Any]] = []
        self._reported = 0
        self._landed: list[str] = []
        self._continuing = False
        self._repairing = False

    def apply(self, op: dict[str, Any]) -> Applied:
        from app.apps import ops as app_ops

        kind = str(op.get("op") or "")
        if kind == "plan":
            if self.plan is not None and self._continuing:
                return Applied(False, False)
            self.plan = self.plan or op
            return Applied(False, True)
        if kind == "done":
            self.explanation = str(op.get("explanation", ""))
            return Applied(False, True)
        if kind == "reply":
            if self.replied is None and not self._repairing:
                self.replied = op
            return Applied(False, False)
        if not kind.startswith("app_"):
            return Applied(False, False)
        if self._continuing and self._repeats(op):
            return Applied(False, False)
        if problem := app_ops.polish_problem(self.spec, op):
            path = f"{op.get('path')}.{op.get('key')}" if op.get("key") else str(op.get("path") or "")
            self.refused.append({"op": kind, "path": path, "message": problem})
            return Applied(False, False, problem)
        applied = app_ops.apply_app_op(self.spec, op)
        if applied.changed:
            self.effective.append(op)
            target = op.get("id") or (op.get("component") or {}).get("id") or (op.get("page") or {}).get("id") \
                or op.get("key")
            if isinstance(target, str) and target not in self._landed:
                self._landed.append(target)
        return applied

    def _repeats(self, op: dict[str, Any]) -> bool:
        """接着写时：重发的同一个操作；加一个 spec 里已经一模一样存在的组件。"""
        if op in self.effective:
            return True
        if op.get("op") == "app_add":
            component = op.get("component") or {}
            have = app_component(self.spec, str(component.get("id") or ""))
            return have is not None and have == component
        return False

    def landed(self) -> list[str]:
        return list(self._landed)

    def continuing(self) -> None:
        self._continuing = True

    def repairing(self) -> None:
        self._repairing = True

    def unreported(self) -> list[dict[str, Any]]:
        """还没交回过模型的越界操作，取走即算交回过：同一条不在后面几轮自查里反复交回。"""
        out = self.refused[self._reported:]
        self._reported = len(self.refused)
        return out

    def refusals(self) -> list[dict[str, Any]]:
        """交付时列给人看的：形状同应用检查的问题（警告级），说明助手想改什么、为什么没改。"""
        return [{"message": f"助手想改的这一处没有采纳：{r['message']}", "level": "warning", "code": POLISH_REFUSED,
                 "path": r["path"] or None} for r in self.refused]


#: 助手润色越界被拒（D-h）。不是应用检查的编号：不进发布检查，只在润色交付时列出来
POLISH_REFUSED = "app.polish_refused"


class AppChecker:
    """应用的自查：app/apps/lint.lint_app 的 error，去掉只有人能处理的（APP_HUMAN_ONLY），交回模型改。"""

    def __init__(self, applier: AppApplier, *, bindings: dict[str, Any] | None = None) -> None:
        self.applier = applier
        self.bindings = bindings or {}

    async def lint(self) -> list[dict[str, Any]]:
        """全部问题（各级都在），交付时列出来。另开短会话：流里请求的会话已经不能用了。"""
        from app.apps import lint as app_lint
        from app.db.base import SessionLocal

        async with SessionLocal() as session:
            return list(await app_lint.lint_app(session, self.applier.spec, self.bindings))

    async def issues(self) -> list[dict[str, Any]]:
        """交回模型改的：应用检查的 error（去掉只有人能处理的），加上这之前越界被拒、还没交回过的操作。"""
        refused = [{"message": r["message"], "level": "error", "code": POLISH_REFUSED, "path": r["path"] or None}
                   for r in self.applier.unreported()]
        return [*refused,
                *(i for i in await self.lint() if i.get("level") == "error" and i.get("code") not in APP_HUMAN_ONLY)]

    def repair_request(self, issues: list[dict[str, Any]], looks_text: str) -> str:
        lines = "\n".join(f"- {i.get('path') or ''} {i.get('message') or ''}".strip() for i in issues)
        return APP_REPAIR_PROMPT.format(spec=json.dumps(self.applier.spec, ensure_ascii=False, indent=2),
                                        issues=lines,
                                        looks=REPAIR_LOOKS_PROMPT.format(text=looks_text) if looks_text else "")


# ==========================================================================
# 生成过程
# ==========================================================================


class Generation:
    """一轮生成：起草（draft）→ 自查修正（repair）。两段之间调用方可以插自己的判断（只回答了一句、兜底重放……）。

    model 是按操作流写的模型（copilot._op_model）；looks 是这一轮的查看服务（app/look/service.py），起草和修正共用，
    看过的对象、交回过的文字都在里面。asked：模型问了用户一个问题（这一轮结束）。
    """

    def __init__(self, model: Any, *, system: str, applier: Applier, checker: Checker | None = None,
                 looks: LookService | None = None, started: float | None = None) -> None:
        self.model = model
        self.system = system
        self.applier = applier
        self.checker = checker
        self.looks = looks if looks is not None else LookService([])
        self.started = time.monotonic() if started is None else started
        self.asked: dict[str, Any] | None = None
        self.done: dict[str, Any] | None = None
        self.repaired = 0
        self.repair_error: Exception | None = None
        #: 这一轮调了几次主模型（含接着写、修正），验收时统计
        self.calls = 0

    async def draft(self, request: str) -> AsyncIterator[dict[str, Any]]:
        async for op in self._converse(request, LookBudget(LOOK_LIMIT), repair=False):
            yield op

    async def repair(self, rounds: int = SELF_CHECK_ROUNDS) -> AsyncIterator[dict[str, Any]]:
        """按 Checker 自查，挡路的问题交回模型改，改完再查。修不成（报错、截断）就停，照实发一条 check error，原因记在
        repair_error；改到第几轮记在 repaired。模型问了用户（asked）的这一轮不修。"""
        if self.checker is None or self.asked is not None:
            return
        budget = LookBudget(REPAIR_LOOK_LIMIT)
        self.applier.repairing()
        for round_no in range(1, rounds + 1):
            errors = await self.checker.issues()
            if not errors:
                break
            yield {"op": "check", "status": "repairing", "round": round_no, "issues": errors}
            request = self.checker.repair_request(errors, self.looks.looked())
            try:
                async for op in self._converse(request, budget, repair=True):
                    yield op
            except Exception as e:  # noqa: BLE001 - 修不成就照实交付，调用方列出剩下的问题
                from app.core.errors import explain, raw

                self.repair_error = e
                yield {"op": "check", "status": "error", "message": f"自查修正失败：{explain(e)[0]}",
                       "detail": raw(e)}
                break
            self.repaired = round_no
            if self.asked is not None:
                break

    def _elapsed(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    async def _converse(self, request: str, budget: LookBudget, *, repair: bool) -> AsyncIterator[dict[str, Any]]:
        """一段对话：请模型写，读到 look 就停下、查看、把结果作为下一条消息交回，再请它接着写，直到 done / ask / 流结束。"""
        messages: list[tuple[str, str]] = [("system", self.system), ("human", request)]
        idle = 0
        while True:
            lines: list[str] = []
            look: dict[str, Any] | None = None
            self.calls += 1
            async with aclosing(_iter_ops(self.model, messages, lines=lines)) as ops:
                async for op in ops:
                    kind = op.get("op")
                    if kind == "thinking":
                        yield op
                    elif kind == "look":
                        look = op
                    elif kind == "ask":
                        self.asked = op
                        yield op
                    elif repair and kind in ("plan", "done"):
                        # 修补轮自己的开场和收尾，不是新方案，不覆盖原来的说明
                        continue
                    else:
                        if kind == "done":
                            self.done = op
                        if self.applier.apply(op).forward:
                            yield op
            if look is None:
                return
            # 查看可能要等挑选模型：心跳的阶段换成「正在查看」
            yield {"op": "heartbeat", "phase": "looking", "elapsed_ms": self._elapsed()}
            result = await self.looks.look(look, budget)
            if result.record is not None:
                yield result.record
            if not result.counted:
                idle += 1
                if idle >= _IDLE_LOOKS:
                    return
            messages = [*messages, ("ai", "\n".join(lines)),
                        ("human", result.text + self.looks.continue_prompt(self.applier.landed()))]
            self.applier.continuing()


async def generate(model: Any, system: str, request: str, applier: Applier, checker: Checker | None = None,
                   looks: LookService | None = None, *, rounds: int = SELF_CHECK_ROUNDS) -> AsyncIterator[dict[str, Any]]:
    """附录 C 的生成过程协议：起草（含查看）→ 自查修正，产出一串操作和事件。中间不需要插判断的调用方直接用它。"""
    gen = Generation(model, system=system, applier=applier, checker=checker, looks=looks)
    async for op in gen.draft(request):
        yield op
    async for op in gen.repair(rounds):
        yield op
