"""应用的发布检查（§3.13 的 app.* 编号）。问题格式同 engine/schema.py 的 ValidationIssue：
{message, level: error|warning, code, path}。path 指向 spec 里的位置（如 pages[0].body[2].bind）。

编号是给程序认的（编辑器按它把问题落到属性面板、助手按它修），文案可以改，编号不改。
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.spec import APP_ROOTS, DISPOSITIONS, parse_spec
from app.apps.tree import bind_kind, components, input_fields, node_map, pins_by_alias, resolve_form, slot_refs

#: 按需视图缓存时效：默认 600 秒，最多一天（§2.3）
TTL_DEFAULT = 600
TTL_MAX = 86_400
#: 运行信息绑定能取的几项
RUN_FIELDS = ("status", "tier", "finished_at")
#: 入口映射里能取的当前用户的几项
USER_FIELDS = ("username", "display_name")

_DIGIT = re.compile(r"\d")
#: 文字里的一个数：千分位的逗号算在数里（「5,000」就是 5000）
_NUMBER = re.compile(r"\d[\d,，]*(?:\.\d+)?")
#: 岗位在图里的用处（tree.SLOT_ROLES），检查的说明里写清是哪一步要它
_SLOT_USE = {"assignee": "人工节点「{title}」派给", "escalation": "人工节点「{title}」超时升级给",
             "confirm": "调用工具节点「{title}」的发送确认派给"}
_REPORT_TEXT = re.compile(r"^\s*\{\{\s*nodes\.([A-Za-z0-9_-]+)\.text\s*\}\}\s*$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_㐀-鿿]+)\}")


class _Issues:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def error(self, code: str, message: str, path: str | None = None, **extra: Any) -> None:
        self.items.append({"message": message, "level": "error", "code": code, "path": path, **extra})

    def warning(self, code: str, message: str, path: str | None = None, **extra: Any) -> None:
        self.items.append({"message": message, "level": "warning", "code": code, "path": path, **extra})


def parse_map_source(source: Any) -> tuple[str, Any] | None:
    """入口映射的一项：("form", 字段) / ("user", 属性) / ("const", 值)；写法不对是 None。

    常量写成 JSON 字面量（数、true / false、"文字"）或单引号括起来的文字（'报销'）。"""
    if not isinstance(source, str) or not source.strip():
        return None
    text = source.strip()
    if text.startswith("form."):
        return ("form", text[5:]) if text[5:] else None
    if text.startswith("user."):
        return ("user", text[5:]) if text[5:] in USER_FIELDS else None
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return ("const", text[1:-1])
    try:
        return ("const", json.loads(text))
    except ValueError:
        return None


def needs_disposition(previous: Any | None, pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """钉住项相对上一个应用版本换了版本的：[{alias, from, to}]。按别名对上；别名换了的，按同一个工作流对上（唯一时）。"""
    if previous is None:
        return []
    before = [p for p in (getattr(previous, "pins", None) or []) if isinstance(p, dict)]
    out: list[dict[str, Any]] = []
    for pin in pins:
        if not isinstance(pin, dict):
            continue
        old = next((p for p in before if p.get("alias") == pin.get("alias")), None)
        if old is None:
            same = [p for p in before if p.get("workflow_id") == pin.get("workflow_id")]
            old = same[0] if len(same) == 1 else None
        if old is not None and old.get("workflow_id") == pin.get("workflow_id") and old.get("version") != pin.get("version"):
            out.append({"alias": pin.get("alias"), "from": old.get("version"), "to": pin.get("version")})
    return out


async def lint_app(session: AsyncSession, spec: dict[str, Any], bindings: dict[str, Any], *,
                   previous: Any | None = None, dispositions: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """检查一份待发布的应用。previous 是上一个应用版本（判断钉住项升版要不要处置）。"""
    from app.apps.pins import load_pins

    out = _Issues()
    try:
        parse_spec(spec)
    except ValidationError as e:
        for err in e.errors()[:20]:
            loc = ".".join(str(x) for x in err.get("loc") or ())
            out.error("app.format", f"应用描述的格式不对：{loc}（{err.get('msg')}）", loc or None)
        return out.items
    pins = await load_pins(session, spec)
    level = _level(pins)
    _lint_unique(spec, out)
    _lint_pins(spec, pins, out)
    await _lint_subgraph_pins(session, spec, pins, out)
    _lint_upgrade(spec, previous, dispositions or {}, out)
    views = spec.get("views") or {}
    await _lint_views(session, spec, pins, out)
    _lint_components(spec, pins, views, level, out)
    _lint_entries(spec, pins, out)
    await _lint_process(session, spec, pins, bindings or {}, out)
    _lint_hooks(spec, out)
    await _lint_notify(session, spec, out)
    return out.items


def _level(pins: dict[str, Any]) -> str:
    from app.apps.tree import app_level

    return app_level([{"level": p.level} for p in pins.values()])


def _lint_unique(spec: dict[str, Any], out: _Issues) -> None:
    seen: set[str] = set()
    for i, page in enumerate(spec.get("pages") or []):
        if page.get("id") in seen:
            out.error("app.format", f"页面「{page.get('id')}」重名", f"pages[{i}].id")
        seen.add(page.get("id"))
    for comp, _, at in components(spec):
        if comp.get("id") in seen:
            out.error("app.format", f"组件「{comp.get('id')}」和别的组件或页面重名", f"{at}.id")
        seen.add(comp.get("id"))
    aliases: set[str] = set()
    for i, pin in enumerate(spec.get("pins") or []):
        if pin.get("alias") in aliases:
            out.error("app.format", f"引用的工作流版本别名「{pin.get('alias')}」重复", f"pins[{i}].alias")
        aliases.add(pin.get("alias"))
    entries: set[str] = set()
    for i, entry in enumerate(spec.get("entries") or []):
        if entry.get("id") in entries:
            out.error("app.format", f"入口「{entry.get('id')}」重名", f"entries[{i}].id")
        entries.add(entry.get("id"))


def _lint_pins(spec: dict[str, Any], pins: dict[str, Any], out: _Issues) -> None:
    for i, pin in enumerate(spec.get("pins") or []):
        state = pins.get(pin.get("alias"))
        at = f"pins[{i}]"
        if state is None or state.row is None:
            out.error("app.pin_unpublished", f"引用的工作流版本「{pin.get('alias')}」不存在（工作流或 v{pin.get('version')} 已删除）", at)
        elif state.retired:
            out.error("app.pin_retired", f"引用的「{state.label}」已退役，请升级到一个在用的发布版本", at)
        elif not state.published:
            out.error("app.pin_unpublished", f"引用的「{state.label}」没有发布过，应用只能引用已发布的版本", at)
        elif state.hash != pin.get("graph_hash"):
            out.error("app.pin_hash", f"引用的「{state.label}」的内容指纹和应用里记的不一致，请重新选择这一版", at)


async def _lint_subgraph_pins(session: AsyncSession, spec: dict[str, Any], pins: dict[str, Any], out: _Issues) -> None:
    """钉住的版本里，子工作流固定的版本不是「已发布且未退役」的（D-q，apps/versions.subgraph_pin_problems）：各种用法都会
    发起这一版的正式运行（入口、流程、按需视图、给最新封存视图供数的定时任务、补算），每一次都会在发起时被拒，所以报错。
    钉住项本身不能用的（上面 _lint_pins 报过）不再往里看。"""
    from app.apps.versions import subgraph_pin_problems
    from app.engine.schema import GraphSpec

    for i, pin in enumerate(spec.get("pins") or []):
        state = pins.get(pin.get("alias"))
        if state is None or state.row is None or not state.published:
            continue
        try:
            graph = GraphSpec.model_validate(state.graph)
        except ValueError:
            continue
        for problem in await subgraph_pin_problems(session, graph):
            out.error(problem.code, f"「{state.label}」里的{problem.who}固定的{problem.reason}：这一版的正式运行"
                      "发起时会被拒绝。请在那个工作流里改好并发布新版本，再把这里的引用换成新版本", f"pins[{i}]",
                      node_id=problem.node_id)


def _lint_upgrade(spec: dict[str, Any], previous: Any | None, dispositions: dict[str, str], out: _Issues) -> None:
    from app.engine.governance import UPGRADE_POLICY_LABELS

    choices = "、".join(f"「{v}」" for v in UPGRADE_POLICY_LABELS.values())
    for item in needs_disposition(previous, spec.get("pins") or []):
        if dispositions.get(item["alias"]) not in DISPOSITIONS:
            index = next(i for i, p in enumerate(spec.get("pins") or []) if p.get("alias") == item["alias"])
            out.error("app.upgrade_policy",
                      f"引用的工作流版本「{item['alias']}」从 v{item['from']} 升到了 v{item['to']}，口径升版必须处置："
                      f"请选择 {choices} 中的一种", f"pins[{index}]", alias=item["alias"])


async def _lint_views(session: AsyncSession, spec: dict[str, Any], pins: dict[str, Any], out: _Issues) -> None:
    from app.apps.pins import reads_live
    from app.apps.resolve import latest_sealed_run
    from app.db.models import Schedule

    for vid, view in (spec.get("views") or {}).items():
        source = (view or {}).get("source") or {}
        at = f"views.{vid}.source"
        kind = source.get("kind")
        if kind == "instance":
            continue
        state = pins.get(source.get("pin"))
        if state is None:
            out.error("app.binding_target_missing", f"视图「{vid}」引用的工作流版本「{source.get('pin')}」不在应用里", at)
            continue
        if kind == "latest_sealed":
            origin = source.get("from")
            if origin not in (None, "formal") and not (isinstance(origin, str) and origin.startswith("schedule:")):
                out.error("app.format", f"视图「{vid}」的来源只能是正式运行或某个定时任务", f"{at}.from")
                continue
            if isinstance(origin, str) and origin.startswith("schedule:") \
                    and await session.get(Schedule, origin[len("schedule:"):]) is None:
                out.error("app.binding_target_missing", f"视图「{vid}」取数的定时任务已不存在", f"{at}.from")
                continue
            if state.row is not None and await latest_sealed_run(session, state, origin) is None:
                out.warning("app.view_source_empty", f"视图「{vid}」暂时没有可读的封存运行：页面上会写明还没有数据", at)
        elif kind == "on_demand":
            ttl = source.get("ttl_s")
            if ttl is not None and not (isinstance(ttl, int) and 0 < ttl <= TTL_MAX):
                out.error("app.on_demand_ttl", f"视图「{vid}」的缓存时效需要在 1 到 {TTL_MAX:,} 秒之间", f"{at}.ttl_s")
            elif ttl is None and state.row is not None and await reads_live(session, state.graph):
                out.error("app.on_demand_ttl",
                          f"视图「{vid}」按需运行时会查活库，需要设置缓存时效（建议 {TTL_DEFAULT} 秒）：不然同样的参数会一直用旧结果",
                          f"{at}.ttl_s")
            if source.get("params") is not None:
                form = resolve_form(spec, source.get("params"))
                problems = ["引用的表单不存在"] if form is None else _form_problems(form)
                for p in problems[:3]:
                    out.error("app.format", f"视图「{vid}」的参数表单：{p}", f"{at}.params")


def _form_problems(form: dict[str, Any]) -> list[str]:
    from app.apps.forms import validate_form_schema

    return validate_form_schema(form)


def _view_graphs(spec: dict[str, Any], pins: dict[str, Any], view_id: str | None) -> list[dict[str, Any]]:
    """视图背后的图：钉住项那一版的图；实例视图是各入口引用的那几版。"""
    view = (spec.get("views") or {}).get(view_id or "") or {}
    source = view.get("source") or {}
    if source.get("kind") == "instance":
        aliases = {e.get("pin") for e in spec.get("entries") or []} or set(pins)
        return [pins[a].graph for a in aliases if a in pins and pins[a].row is not None]
    state = pins.get(source.get("pin"))
    return [state.graph] if state is not None and state.row is not None else []


def _lint_components(spec: dict[str, Any], pins: dict[str, Any], views: dict[str, Any], level: str,
                     out: _Issues) -> None:
    from jsonschema import Draft202012Validator

    from app.apps.catalog import BY_NAME

    constants = _graph_numbers(pins)
    for comp, _, at in components(spec):
        ctype = comp.get("type")
        entry = BY_NAME.get(str(ctype))
        if entry is None:
            out.error("app.component_unknown", f"组件「{comp.get('id')}」的类型「{ctype}」不在组件目录里", f"{at}.type")
            continue
        for err in list(Draft202012Validator(entry["props"]).iter_errors(comp.get("props") or {}))[:3]:
            where = ".".join(str(x) for x in err.absolute_path)
            out.error("app.props_invalid", f"组件「{comp.get('id')}」（{entry['label']}）的属性{('「' + where + '」') if where else ''}不对",
                      f"{at}.props{('.' + where) if where else ''}")
        if comp.get("children") and not entry["children"]:
            out.error("app.props_invalid", f"组件「{comp.get('id')}」（{entry['label']}）不能放子组件", f"{at}.children")
        _lint_text(comp, entry, at, level, out, constants)
        _lint_bind(spec, comp, entry, pins, views, at, out)
        if level == "governed":
            _lint_metric_origin(spec, comp, pins, at, out)
        _lint_visible(comp, views, at, out)
        _lint_refs(spec, comp, at, out)


def _numbers(text: str) -> list[str]:
    return [m.replace(",", "").replace("，", "").rstrip(".") for m in _NUMBER.findall(text)]


def _graph_numbers(pins: dict[str, Any]) -> set[str]:
    """固定的版本里节点配置写着的数：条件里的金额线、时限、会签人数、表单的上下限这些流程规则。只看 config，不看标题。"""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            found.update(_numbers(f"{value:g}" if isinstance(value, float) else str(value)))
        elif isinstance(value, str):
            found.update(_numbers(value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    for state in pins.values():
        if getattr(state, "row", None) is None:
            continue
        for node in node_map(state.graph).values():
            walk(node.get("config"))
    return found


def _lint_text(comp: dict[str, Any], entry: dict[str, Any], at: str, level: str, out: _Issues,
               constants: set[str]) -> None:
    """静态文字里有数字：叙述层无算术、数字只来自证据。受管级报错，已发布级警告。

    数据要绑定指标或单元格；流程规则（审批的金额线、时限、会签人数）的出处是固定版本里的节点配置——文字里的每个数都在
    那里写着才算有出处。规则只写在说明里、流程并不照着执行的，照样报：那句话是假的。"""
    props = comp.get("props") or {}
    text = props.get("markdown") if comp.get("type") == "text" else props.get("text") if comp.get("type") == "notice" else None
    if not isinstance(text, str) or not _DIGIT.search(text):
        return
    loose = [n for n in dict.fromkeys(_numbers(text)) if n not in constants]
    if not loose:
        return
    add = out.error if level == "governed" else out.warning
    add("app.text_number", f"组件「{comp.get('id')}」（{entry['label']}）的静态文字里写了数字「{'、'.join(loose[:5])}」，"
        "没有出处：数据请改为绑定指标或单元格；流程规则（如审批的金额线、时限）要先写进工作流，文字里的数和工作流里的一致"
        "才算有出处", f"{at}.props")


def _lint_bind(spec: dict[str, Any], comp: dict[str, Any], entry: dict[str, Any], pins: dict[str, Any],
               views: dict[str, Any], at: str, out: _Issues) -> None:
    bind = comp.get("bind")
    if bind is None:
        return
    where = f"{at}.bind"
    cid = comp.get("id")
    kind = bind_kind(bind)
    if kind is not None and kind not in entry["bindings"]:
        allowed = "、".join(entry["bindings"]) or "任何数据"
        out.error("app.props_invalid", f"组件「{cid}」（{entry['label']}）不能绑定这种数据，只能绑定：{allowed}", where)
        return
    if kind in ("task", "action"):
        graphs = _view_graphs(spec, pins, next((v for v, d in views.items()
                                                if ((d or {}).get("source") or {}).get("kind") == "instance"), None))
        _lint_process_bind(cid, kind, bind[kind], graphs, where, out)
        return
    view_id = bind.get("view")
    if view_id not in views:
        out.error("app.binding_target_missing", f"组件「{cid}」绑定的视图「{view_id}」不存在", where)
        return
    if kind is None:
        return
    graphs = _view_graphs(spec, pins, view_id)
    if not graphs:
        return
    target = bind[kind]
    if not any(_target_ok(kind, target, g, out, cid, where) for g in graphs):
        out.error("app.binding_target_missing", f"组件「{cid}」绑定的{_KIND_LABEL[kind]}在引用的工作流版本里不存在", where)


def _lint_metric_origin(spec: dict[str, Any], comp: dict[str, Any], pins: dict[str, Any], at: str, out: _Issues) -> None:
    """受管级应用里绑定的指标（指标块、趋势）：表达式的输入追不到原始数据（查询快照里的格、运行输入）的给提示。

    指标照样是口径卡确定性算出来的、面板上照样「有出处」，但点开追到某个节点就断了：受管级的应用要的是一路追到数据。
    和运行时、证据面板同一套认法（engine/lineage.static_stops），只看图不看值。转抄别处口径卡的指标在上游那一版里，
    不在这里追；目标不存在的由 _lint_bind 报。"""
    from app.engine.labels import type_label
    from app.engine.lineage import static_stops

    bind = comp.get("bind")
    kind = bind_kind(bind) if isinstance(bind, dict) else None
    if kind not in ("metric", "series"):
        return
    node_id, _, metric_id = str(bind.get(kind) or "").partition(".")
    for graph in _view_graphs(spec, pins, bind.get("view")):
        nodes = node_map(graph)
        card = nodes.get(node_id)
        if card is None or card["type"] != "metrics" or isinstance(card["config"].get("caliber_from"), dict):
            continue
        metric = next((m for m in card["config"].get("metrics") or [] if isinstance(m, dict) and m.get("id") == metric_id),
                      None)
        if metric is None:
            continue
        stops = static_stops(nodes, str(metric.get("expression") or ""))
        if not stops:
            continue
        where = "；".join(
            f"{s.path} 停在「{nodes[s.node_id]['title']}」（{type_label(nodes[s.node_id]['type'])}）" if s.node_id in nodes
            else f"{s.path} 无法确定来自哪个节点" for s in stops[:3])
        out.warning("app.metric_untraced",
                    f"组件「{comp.get('id')}」绑定的指标「{metric.get('name') or metric_id}」的输入没有追到原始数据（{where}）："
                    "数是口径卡算出来的，但点开出处只能追到那里，看不到查询快照里的格。请在口径卡里用 cell() 按格取数，"
                    "或让数据整形只原样解析调用工具节点的结果", f"{at}.bind", node_id=node_id)
        return


_KIND_LABEL = {"metric": "指标", "series": "指标", "cell": "单元格", "query": "查询结果", "report": "报告",
               "input": "运行输入", "run": "运行信息", "output": "成果字段", "task": "待办", "action": "动作"}


def _target_ok(kind: str, target: Any, graph: dict[str, Any], out: _Issues, cid: Any, where: str) -> bool:
    nodes = node_map(graph)
    if kind in ("metric", "series"):
        node_id, _, metric = str(target or "").partition(".")
        node = nodes.get(node_id)
        if node is None or node["type"] != "metrics" or not metric:
            return False
        listed = [m.get("id") for m in node["config"].get("metrics") or [] if isinstance(m, dict)]
        # 转抄别处口径卡的（caliber_from）指标在上游那一版里，发布时引用的那一版不变，这里不去追
        return metric in listed or (not listed and isinstance(node["config"].get("caliber_from"), dict))
    if kind in ("cell", "query"):
        node = nodes.get(str((target or {}).get("node") if isinstance(target, dict) else ""))
        if kind == "cell" and isinstance(target, dict) and not (
                isinstance(target.get("row"), int) and isinstance(target.get("col"), str)):
            return False
        return node is not None and node["type"] in ("tool", "agent", "merge")
    if kind == "report":
        node = nodes.get(str(target))
        return node is not None and node["type"] == "report"
    if kind == "input":
        fields = input_fields(graph)
        return not fields or any(f["name"].strip() == target for f in fields)
    if kind == "run":
        return target in RUN_FIELDS
    if kind == "output":
        for node in nodes.values():
            if node["type"] != "output":
                continue
            for f in node["config"].get("fields") or []:
                if isinstance(f, dict) and f.get("name") == target:
                    value = str(f.get("value") or "")
                    report = _REPORT_TEXT.match(value)
                    if report and (nodes.get(report.group(1)) or {}).get("type") == "report":
                        return True
                    if "{{" not in value and not _DIGIT.search(value):
                        return True
                    out.error("app.binding_not_evidenced",
                              f"组件「{cid}」绑定的成果字段「{target}」不是逐字取自报告的文字，里面的数字没有证据。"
                              "请改为绑定指标、单元格或报告", where)
                    return True
        return False
    return True


def _lint_process_bind(cid: Any, kind: str, target: Any, graphs: list[dict[str, Any]], where: str, out: _Issues) -> None:
    want = "human" if kind == "task" else "tool"
    if not any((node_map(g).get(str(target)) or {}).get("type") == want for g in graphs):
        out.error("app.binding_target_missing", f"组件「{cid}」绑定的{_KIND_LABEL[kind]}节点「{target}」在入口引用的工作流版本里不存在",
                  where)


def _lint_visible(comp: dict[str, Any], views: dict[str, Any], at: str, out: _Issues) -> None:
    from app.engine.expressions import ExpressionArithmetic, ExpressionError, parse_expression

    if comp.get("scope") is not None and comp.get("scope") not in views:
        out.error("app.binding_target_missing", f"组件「{comp.get('id')}」的可见条件引用的视图「{comp.get('scope')}」不存在",
                  f"{at}.scope")
    expr = comp.get("visible")
    if not expr:
        return
    where = f"{at}.visible"
    try:
        _, _, unknown = parse_expression(str(expr), roots=APP_ROOTS, arithmetic=False)
    except ExpressionArithmetic as e:
        out.error("app.expr_arith", f"组件「{comp.get('id')}」的可见条件：{e}", where)
        return
    except ExpressionError as e:
        out.error("app.expr_invalid", f"组件「{comp.get('id')}」的可见条件写法不对：{e}", where)
        return
    if unknown:
        roots = "、".join(sorted(APP_ROOTS))
        out.error("app.expr_root", f"组件「{comp.get('id')}」的可见条件用了不认识的名字：{'、'.join(unknown)}。能用的有 {roots}",
                  where)


def _lint_refs(spec: dict[str, Any], comp: dict[str, Any], at: str, out: _Issues) -> None:
    """组件属性里按名字引用的入口、表单得在。"""
    props = comp.get("props") or {}
    if comp.get("type") in ("entry_button", "instance_list") and props.get("entry") is not None \
            and not any(e.get("id") == props["entry"] for e in spec.get("entries") or []):
        out.error("app.binding_target_missing", f"组件「{comp.get('id')}」引用的入口「{props['entry']}」不存在", f"{at}.props.entry")
    if comp.get("type") == "form" and props.get("form") not in (spec.get("forms") or {}):
        out.error("app.binding_target_missing", f"组件「{comp.get('id')}」引用的表单「{props.get('form')}」不存在", f"{at}.props.form")


def _lint_entries(spec: dict[str, Any], pins: dict[str, Any], out: _Issues) -> None:
    for i, entry in enumerate(spec.get("entries") or []):
        at = f"entries[{i}]"
        label = f"入口「{entry.get('label') or entry.get('id')}」"
        form = resolve_form(spec, entry.get("form"))
        if form is None:
            out.error("app.entry_form_mismatch", f"{label}引用的表单不存在", f"{at}.form")
            continue
        for p in _form_problems(form)[:3]:
            out.error("app.entry_form_mismatch", f"{label}的表单：{p}", f"{at}.form")
        state = pins.get(entry.get("pin"))
        if state is None:
            out.error("app.binding_target_missing", f"{label}引用的工作流版本「{entry.get('pin')}」不在应用里", f"{at}.pin")
            continue
        fields = {f["name"].strip(): f for f in input_fields(state.graph)} if state.row is not None else {}
        mapping = entry.get("map") or {}
        props = form.get("properties") or {}
        for target, source in mapping.items():
            parsed = parse_map_source(source)
            if fields and target not in fields:
                out.error("app.entry_form_mismatch", f"{label}映射的「{target}」不是「{state.label}」的入口字段", f"{at}.map.{target}")
            if parsed is None:
                out.error("app.entry_form_mismatch",
                          f"{label}映射「{target}」的取值写法不对：只能取表单字段、当前用户的用户名或显示名，或者写一个常量",
                          f"{at}.map.{target}")
            elif parsed[0] == "form" and parsed[1] not in props:
                out.error("app.entry_form_mismatch", f"{label}映射「{target}」取的表单字段「{parsed[1]}」不在表单里",
                          f"{at}.map.{target}")
        for name, f in fields.items():
            if f.get("required") and f.get("default") in (None, "") and name not in mapping:
                out.error("app.entry_unmapped_required", f"{label}没有给「{state.label}」的必填字段「{name}」取值", f"{at}.map")
        title = entry.get("instance_title")
        if isinstance(title, dict):
            listed = set(title.get("fields") or [])
            used = set(_PLACEHOLDER.findall(str(title.get("pattern") or "")))
            if bad := sorted((listed | used) - set(mapping)):
                out.error("app.entry_form_mismatch", f"{label}的实例标题引用了没有映射的字段：{'、'.join(bad)}",
                          f"{at}.instance_title")
            elif used - listed:
                out.error("app.entry_form_mismatch", f"{label}的实例标题用到的字段要写在字段列表里：{'、'.join(sorted(used - listed))}",
                          f"{at}.instance_title")


async def _lint_process(session: AsyncSession, spec: dict[str, Any], pins: dict[str, Any], bindings: dict[str, Any],
                        out: _Issues) -> None:
    """流程钉住项：图里引用到的岗位（tree.slot_refs：派给谁、超时升级给谁、动作确认派给谁）都声明了、绑了人，写入动作
    有人核实，图本身经得起升级（process.* 升成 app.process_unsafe）。岗位没人，流程走到那一步就派给了「任何人」：升级、
    确认悄悄失效，所以报错。哪些调用工具节点是写入（开发送确认卡、要人核实）按 pins.write_nodes 认，和运行时、派生同一个
    认法。"""
    from app.apps.pins import write_nodes
    from app.engine.governance import publish_issues
    from app.engine.lint_process import load_subgraphs, with_subgraphs
    from app.engine.schema import GraphSpec

    process = [s for s in pins.values() if s.pin.get("use") == "process" and s.row is not None]
    if not process:
        return
    bound = (bindings or {}).get("slots") or {}
    slots = dict(spec.get("slots") or {})
    used: dict[str, list[str]] = {}
    writes = {state.alias: await write_nodes(session, state.graph) for state in process}
    for state in process:
        for ref in slot_refs(state.graph, writes[state.alias]):
            slot, title = ref["slot"], ref["title"]
            what = _SLOT_USE[ref["role"]].format(title=title)
            used.setdefault(slot, [])
            if f"{what}它" not in used[slot]:
                used[slot].append(f"{what}它")
            if slot not in slots:
                out.error("app.slot_unbound", f"「{state.label}」里{what}岗位「{slot}」，应用里没有声明这个岗位",
                          "slots", node_id=ref["node_id"])
                slots[slot] = {"label": slot}
    for slot, info in slots.items():
        if not bound.get(slot):
            label = (info or {}).get("label") or slot
            why = f"（{'；'.join(used[slot][:3])}）：没有人，流程走到这一步就派给了任何人" if used.get(slot) else ""
            out.error("app.slot_unbound", f"岗位「{label}」还没有绑定到人或组{why}", f"slots.{slot}")
    await _lint_slot_principals(session, slots, bound, out)
    for state in process:
        nodes = node_map(state.graph)
        for node_id, cap in writes[state.alias].items():
            if not getattr(cap, "lookup", None) and not bound.get("reconciler"):
                out.error("app.reconciler_unbound", f"「{state.label}」的「{nodes[node_id]['title']}」会写外部系统，写入的能力"
                          "没有登记查询，结果未知时要人核实：请绑定核实岗位", "slots", node_id=node_id)
        try:
            graph = GraphSpec.model_validate(state.graph)
        except ValueError:
            continue
        # 子工作流里有没有人工节点要读库才知道（门禁本身不查库）：先读好放进来，process.human_in_subgraph 才真正生效
        with with_subgraphs(await load_subgraphs(session, graph)):
            issues = publish_issues(graph, level=state.level or "published")
        for issue in issues:
            if (issue.code or "").startswith("process."):
                out.error("app.process_unsafe", f"「{state.label}」作为流程不安全：{issue.message}",
                          f"pins[{_pin_index(spec, state.alias)}]", node_id=issue.node_id)


async def _lint_slot_principals(session: AsyncSession, slots: dict[str, Any], bound: dict[str, Any],
                                out: _Issues) -> None:
    """岗位绑定的人和组要在名录里（auth/directory.py，同选人控件的名录）：绑了名录里没有的、已停用的，流程走到这一步就
    派给了没人能处理的名字——界面上选人时拦着，旧客户端、直接调接口、绑好之后人被停用或删除的照样会漏过来，所以发布时
    再核一次。账号密码登录报错；单点登录的人第一次登录后才进名录，名录里还没有的只提醒；关登录不核对。"""
    from app.api.deps import auth_mode
    from app.auth.directory import load_directory, mismatch

    mode = auth_mode()
    if mode == "off" or not any(bound.get(s) for s in slots):
        return
    known = await load_directory(session)
    for slot, info in slots.items():
        label = (info or {}).get("label") or slot
        for item in bound.get(slot) or []:
            m = mismatch(known, mode, item.get("kind"), str(item.get("name"))) if isinstance(item, dict) else None
            if m is None:
                continue
            if m.blocking:
                out.error("app.slot_principal_unknown", f"岗位「{label}」绑定的{m.text}：派给这个名字的待办没有人能处理"
                          "（全体会签时整条待办办不完）。请从候选中重新选择，或从岗位里移除", f"slots.{slot}",
                          problem=m.problem)
            else:
                out.warning("app.slot_principal_unknown", f"岗位「{label}」绑定的{m.text}", f"slots.{slot}",
                            problem=m.problem)


def _pin_index(spec: dict[str, Any], alias: str) -> int:
    return next((i for i, p in enumerate(spec.get("pins") or []) if p.get("alias") == alias), 0)


def _lint_hooks(spec: dict[str, Any], out: _Issues) -> None:
    entries = {e.get("id") for e in spec.get("entries") or []}
    for i, hook in enumerate(spec.get("hooks") or []):
        if hook.get("kind") == "start" and hook.get("entry") not in entries:
            out.error("app.hook_entry_missing", f"钩子「{hook.get('name')}」要按入口发起实例，但入口「{hook.get('entry')}」不存在",
                      f"hooks[{i}].entry")


async def _lint_notify(session: AsyncSession, spec: dict[str, Any], out: _Issues) -> None:
    from app.db.models_process import NotifyChannel

    wanted = [n.get("channel") for n in spec.get("notify") or []]
    if not wanted:
        return
    known = {r for (r,) in await session.execute(select(NotifyChannel.id).where(NotifyChannel.id.in_(wanted)))}
    for i, channel in enumerate(wanted):
        if channel not in known:
            out.warning("app.notify_channel_missing", f"订阅的通知渠道「{channel}」不存在", f"notify[{i}].channel")


async def acl_issues(session: AsyncSession, app_id: str) -> list[dict[str, Any]]:
    """开了登录却没有任何 use 以上的授权：除了管理员和搭建者，谁都用不了这个应用。关登录时不报。

    授权给的人和组要在名录里（同岗位绑定，见 _lint_slot_principals）：授给名录里没有的名字，以后建了同名的人就直接拿到
    这一档权限；授给停用的人，重新启用就又能用——都说清是哪一档、哪个名字。账号密码登录报错，单点登录名录里还没有的只提醒。"""
    from app.api.deps import auth_mode
    from app.apps.access import ROLE_TEXT, grants_of
    from app.auth.directory import load_directory, mismatch

    mode = auth_mode()
    if mode == "off":
        return []
    grants = await grants_of(session, app_id)
    if not grants:
        return [{"message": "还没有授权给任何人或组使用这个应用：只有管理员和搭建者能打开它", "level": "warning",
                 "code": "app.acl_empty", "path": None}]
    out = _Issues()
    known = await load_directory(session)
    for g in grants:
        if (m := mismatch(known, mode, g.principal_kind, g.principal)) is None:
            continue
        what = f"「{ROLE_TEXT.get(g.role, g.role)}」授权给的{m.text}"
        if m.blocking:
            out.error("app.acl_principal_unknown", f"{what}：{_GRANT_RISK[m.problem]}", "grants", problem=m.problem)
        else:
            out.warning("app.acl_principal_unknown", what, "grants", problem=m.problem)
    return out.items


#: 授权给了名录里对不上的名字，会出什么事
_GRANT_RISK = {"missing": "以后建了同名的人会直接拿到这一档权限。请从授权里移除，或从候选中重新选择",
               "disabled": "重新启用后他又能用这个应用。请从授权里移除"}


def pins_with_state(spec: dict[str, Any], pins: dict[str, Any]) -> list[dict[str, Any]]:
    """发布时冻结的钉住项：按库里的那一版补上级别（不信 spec 里自报的）。"""
    out = []
    for alias, pin in pins_by_alias(spec).items():
        state = pins.get(alias)
        out.append({**pin, "level": state.level if state is not None else None})
    return out
