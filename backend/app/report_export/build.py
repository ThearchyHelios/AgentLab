"""报告导出的构建：报告文档 → 自包含的 HTML（正文服务端渲染 + 每个数字的出处链嵌进数据块）。

拿到文件的人在浏览器里打开：不执行脚本也读得到整份报告（正文是服务端按文档的块、句、片段渲染好的 HTML）；
执行脚本后，带出处的数字、无证据的数字、可疑的表名字段名都点得开——面板里是网页上证据面板同一个拼法的
出处链（api/evidence._chain）：指标步骤（口径卡、口径与版本、原式、代入式、输入、复算、双盲复算）、
输入步骤（值取自哪个节点、哪次查询的哪一格）、查询步骤（SQL、被引用的行、遮罩列、SQL 检查），
以及运行输入、表名字段名、引文各自的步骤。封存状态照网页上的样子逐项标明（已封存 / 工件哈希核对）。

这份文件不是核对件：口径、出处和封存状态都是导出那一刻系统里的记录，拿去和系统对照要用封存哈希
（可验证报告，app/bundle）。页面页脚写明这一点。

主体是纯函数：不碰数据库。调用方（api/evidence 的路由）取好报告文档、逐个片段算好出处链再交进来。
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA = "agentlab.report/1"

#: 和 bundle/build.py 的附件名同一个规矩：文件名里不能有的字（Windows 最严，按它来）换成下划线
_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_NAME_MAX = 80
FILE_SUFFIX = "-报告.html"
#: 没挂在工作流上的运行叫「未保存的工作流」（同 bundle/build.py、前端 lib/terms.ts 的 runName）
UNSAVED_NAME = "未保存的工作流"
_UNSAVED_NAMES = frozenset({"", "临时图", UNSAVED_NAME})


def display_name(run: Mapping[str, Any]) -> str:
    """页面标题、附件名用的运行名：工作流名；没保存的工作流一律叫「未保存的工作流」。（同 bundle/build.py）"""
    name = str(run.get("workflow_name") or "").strip()
    if not run.get("workflow_id") and name in _UNSAVED_NAMES:
        return UNSAVED_NAME
    return name or f"运行 {str(run.get('id') or '')[:8]}".strip()


def attachment_name(run: Mapping[str, Any]) -> str:
    """附件名「<运行名>-报告.html」。"""
    name = _BAD_FILENAME.sub("_", display_name(run)).strip(" .")[:_NAME_MAX].strip(" .")
    return f"{name or UNSAVED_NAME}{FILE_SUFFIX}"


def content_disposition(filename: str, *, run_id: str) -> str:
    """附件头：中文名按 RFC 5987 写在 filename* 里；filename 给不认 filename* 的老客户端一个纯 ASCII 的名字。
    （同 bundle/build.py 的拼法，脚本 check-bundle.mjs 对这份文件同样断言附件名。）"""
    ascii_id = re.sub(r"[^A-Za-z0-9]", "", run_id)[:8]
    return f"attachment; filename=\"agentlab-{ascii_id}-report.html\"; filename*=UTF-8''{quote(filename, safe='')}"


# --------------------------------------------------------------------------
# 正文：报告文档 → HTML（服务端渲染）
# --------------------------------------------------------------------------

#: 行内语法的写法和 bundle/verifier.js、前端 run/Markdown.tsx 同一套：代码（反引号）、链接（只显示文字：
#: 页面不连网）、粗体、斜体、删除线。斜体左边紧挨 * 的不算（否则 **粗体** 会先被它拆掉）
_INLINE = re.compile(
    r"(`+)([^`]+?)\1|\[([^\]]+)\]\(([^)\s]+)[^)]*\)|\*\*([^*]+?)\*\*|__([^_]+?)__"
    r"|\*([^*\n]+?)\*(?!\*)|~~([^~]+?)~~")
_CODE, _LINK, _BOLD, _EM, _STRIKE = 1, 2, 4, 8, 16
#: 列表项的开头：行首的 -、*、+ 或者序号（同 verifier.js 的 ITEM_START）
_ITEM_START = re.compile(r"(^|\n)[ \t]*([-*+]|\d+[.)])[ \t]+$")
#: 表格里看着是数的列（右对齐）：同 verifier.js 的 NUMERIC_CELL
_NUMERIC_CELL = re.compile(r"^[-+−]?[\d,]+(?:\.\d+)?%?[^\d\s]{0,4}$")

#: 片段的状态 → 正文里的线型（class）。同网页上证据文档的颜色语言：实线绿是有出处，点线是核对不了，
#: 波浪红是可疑（反引号里发现的编造名字这类）。probabilistic / candidate 按未裁判、候选画
_STATE_CLASS = {
    "deterministic": "ok", "none": "na", "suspect": "bad", "unverified": "bad",
    "unjudged": "na", "candidate": "na",
}
_KIND_LABEL = {"number": "数字", "value": "取值", "entity": "名称", "quote": "引文"}


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _inline_matches(part: str) -> list[re.Match[str]]:
    """一句里的行内语法，按先后。斜体左边紧挨着 * 的跳过、从下一个字接着找（同 verifier.js）。"""
    out: list[re.Match[str]] = []
    pos = 0
    while True:
        m = _INLINE.search(part, pos)
        if m is None:
            return out
        if m.group(7) is not None and m.start() > 0 and part[m.start() - 1] == "*":
            pos = m.start() + 1
            continue
        out.append(m)
        pos = m.end()


def _inline_marks(text: str) -> tuple[bytearray, bytearray]:
    """整句算一次行内样式：hide 是语法符号占的位置（不显示），style 是每个可见字的样式位。
    粗体、代码可能跨片段（服务端把一句切成了好几个片段），所以按整句算、每个片段取自己那一截。
    （ verifier.js inlineStyles 的 Python 版，规则一致。）"""
    hide = bytearray(len(text))
    style = bytearray(len(text))

    def walk(frm: int, to: int, inherited: int, depth: int) -> None:
        part = text[frm:to]
        last = 0
        for m in _inline_matches(part):
            code, link_text, bold1, bold2, italic, strike = (
                m.group(2), m.group(3), m.group(5), m.group(6), m.group(7), m.group(8))
            for i in range(last, m.start()):
                style[frm + i] = inherited
            inner = next((v for v in (code, link_text, bold1, bold2, italic, strike) if v is not None), "") or ""
            base = frm + m.start()
            whole = m.group(0)
            off = len(m.group(1) or "") if code is not None else whole.find(inner)
            for i in range(len(whole)):
                hide[base + i] = 1
            for i in range(len(inner)):
                hide[base + off + i] = 0
            bit = _CODE if code is not None else _LINK if link_text is not None \
                else _BOLD if bold1 is not None or bold2 is not None \
                else _EM if italic is not None else _STRIKE
            if code is not None or link_text is not None or depth >= 3:
                for i in range(len(inner)):
                    style[base + off + i] = inherited | bit
            else:
                walk(base + off, base + off + len(inner), inherited | bit, depth + 1)
            last = m.end()
        for i in range(last, len(part)):
            style[frm + i] = inherited

    walk(0, len(text), 0, 0)
    return hide, style


def _styled_html(text: str, hide: bytearray, style: bytearray, from_: int, to: int) -> str:
    """一句里 [from_, to) 那一段按算好的样式写成 HTML。"""
    out: list[str] = []
    i = from_
    while i < to:
        if hide[i]:
            i += 1
            continue
        st = style[i]
        j = i + 1
        while j < to and not hide[j] and style[j] == st:
            j += 1
        piece = _esc(text[i:j])
        if st & _CODE:
            piece = f"<code>{piece}</code>"
        if st & _EM:
            piece = f"<em>{piece}</em>"
        if st & _BOLD:
            piece = f"<strong>{piece}</strong>"
        if st & _STRIKE:
            piece = f"<s>{piece}</s>"
        if st & _LINK:
            piece = f'<span class="link">{piece}</span>'
        out.append(piece)
        i = j
    return "".join(out)


def segment_state(seg: Mapping[str, Any]) -> str | None:
    """片段的状态（同前端 segmentState 的分法）：文字、结构片段是 None（不画线、点不开）。
    导出的路由按它决定哪个片段要算出处链，正文渲染按它画线，两边同一个分法。"""
    if seg.get("kind") == "structural":
        return None
    state = seg.get("state")
    if state == "deterministic":
        return "deterministic"
    if state == "none":
        if seg.get("issue") == "unknown_entity" or (seg.get("cite") or {}).get("unknown"):
            return "suspect"
        if seg.get("issue") == "unverified_entity" or (seg.get("cite") or {}).get("unverified"):
            return "unverified"
        return "none"
    if state in ("probabilistic", "candidate"):
        return "unjudged" if state == "probabilistic" else "candidate"
    return None


def _seg_html(seg: Mapping[str, Any], body: str) -> str:
    """一个片段：有状态的是按钮（点开出处面板），文字、结构片段照字渲染。"""
    state = segment_state(seg)
    kind = str(seg.get("kind") or "")
    if state is None:
        inner = f"<strong>{body}</strong>" if seg.get("strong") else body
        return f"<span>{inner}</span>"
    cls = _STATE_CLASS.get(state, "na")
    label = _KIND_LABEL.get(kind, "片段")
    title = f"{label}「{_esc(seg.get('text'))}」：点开查看出处"
    attrs = (f'class="num num-{cls}" data-seg="{_esc(seg["id"])}" data-state="{_esc(state)}"'
             f' data-kind="{_esc(kind)}" title="{title}"')
    if seg.get("strong"):
        body = f"<strong>{body}</strong>"
    return f'<button type="button" {attrs}>{body}</button>'


def _render_unit(unit: Mapping[str, Any]) -> str:
    """一句：片段先拼成整句算行内样式，再各自取自己那一截（结构片段不进正文，列表序号由 <li> 自己画）。"""
    segs = [s for s in unit.get("segments") or [] if isinstance(s, dict) and s.get("kind") != "structural"]
    text = "".join(str(s.get("text") or "") for s in segs)
    hide, style = _inline_marks(text)
    out: list[str] = []
    pos = 0
    for seg in segs:
        body = _styled_html(text, hide, style, pos, pos + len(str(seg.get("text") or "")))
        out.append(_seg_html(seg, body))
        pos += len(str(seg.get("text") or ""))
    return "".join(out)


def _unit_text(unit: Mapping[str, Any]) -> str:
    return "".join(str(s.get("text") or "") for s in unit.get("segments") or []
                   if isinstance(s, dict) and s.get("kind") != "structural")


def _render_block(block: Mapping[str, Any]) -> str:
    """一块：标题、正文、引用、代码、列表、表格、分隔线。（verifier.js renderBlock 的 Python 版。）"""
    units = [u for u in block.get("units") or [] if isinstance(u, dict)]
    btype = str(block.get("type") or "paragraph")
    if btype == "heading":
        level = min(max(int(block.get("level") or 2), 1), 3)
        return f'<h{level + 3} class="doc-h doc-h{level}">{"".join(_render_unit(u) for u in units)}</h{level + 3}>'
    if btype == "hr":
        return "<hr>"
    if btype == "quote":
        return f'<blockquote>{"".join(_render_unit(u) for u in units)}</blockquote>'
    if btype == "code":
        return f'<pre><code>{_esc("".join(_unit_text(u) for u in units))}</code></pre>'
    if btype == "list":
        items: list[dict[str, Any]] = []
        for u in units:
            lead = (u.get("segments") or [None])[0]
            starts = not items or (isinstance(lead, dict) and lead.get("kind") == "structural"
                                   and _ITEM_START.match(str(lead.get("text") or "")))
            if starts:
                items.append({"depth": int(u.get("depth") or 0), "units": [u]})
            else:
                items[-1]["units"].append(u)
        tag = "ol" if block.get("ordered") else "ul"
        start = f' start="{int(block["start"])}"' if block.get("ordered") \
            and isinstance(block.get("start"), int) and block["start"] != 1 else ""
        rows = []
        for item in items:
            margin = f' style="margin-left:{item["depth"] * 14}px"' if item["depth"] else ""
            rows.append(f'<li{margin}>{"".join(_render_unit(u) for u in item["units"])}</li>')
        return f"<{tag}{start}>{''.join(rows)}</{tag}>"
    if btype == "table":
        # 表头 row = -1；一格可能被分成几句，按位置归拢（同 verifier.js）
        cells: dict[str, list[Mapping[str, Any]]] = {}
        row_ids: list[int] = []
        cols = 0
        for u in units:
            loc = u.get("loc") if isinstance(u.get("loc"), dict) else {"row": 0, "col": 0}
            key = f'{loc.get("row")}:{loc.get("col")}'
            cells.setdefault(key, []).append(u)
            cols = max(cols, int(loc.get("col") or 0) + 1)
            if int(loc.get("row") or 0) >= 0 and int(loc.get("row") or 0) not in row_ids:
                row_ids.append(int(loc.get("row")))
        row_ids.sort()

        def text_at(r: int, c: int) -> str:
            return re.sub(r"[*`]", "", "".join(_unit_text(u) for u in cells.get(f"{r}:{c}", []))).strip()

        numeric = []
        for c in range(cols):
            texts = [text_at(r, c) for r in row_ids if text_at(r, c)]
            numeric.append(bool(texts) and all(_NUMERIC_CELL.match(t) for t in texts))

        def cell(tag: str, r: int, c: int) -> str:
            cls = f' class="n"' if tag == "td" and numeric[c] else ""
            return f"<{tag}{cls}>{''.join(_render_unit(u) for u in cells.get(f'{r}:{c}', []))}</{tag}>"

        head = "".join(cell("th", -1, c) for c in range(cols))
        body = "".join("<tr>" + "".join(cell("td", r, c) for c in range(cols)) + "</tr>" for r in row_ids)
        return (f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
                f"<tbody>{body}</tbody></table></div>")
    return f'<p>{"".join(_render_unit(u) for u in units)}</p>'


def render_body(doc: Mapping[str, Any]) -> str:
    """报告文档的正文 HTML。块都读不出来时退回 markdown 原文（等宽显示，至少读得到）。"""
    blocks = [b for b in doc.get("blocks") or [] if isinstance(b, dict)]
    if not blocks:
        return f'<div class="doc"><pre>{_esc(doc.get("markdown") or "")}</pre></div>'
    return f'<div class="doc">{"".join(_render_block(b) for b in blocks)}</div>'


# --------------------------------------------------------------------------
# 数据块和页面
# --------------------------------------------------------------------------

#: 数据放在 <script type="application/json"> 里：< > & 换成转义，正文里的 </script>、<!-- 截不断数据块
#: （同 bundle/build.py 的 _JSON_ESCAPES）
_JSON_ESCAPES = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
                               "\u2028": "\\u2028", "\u2029": "\\u2029"})
_PLACEHOLDER = re.compile(r"__REPORT_(TITLE|NAME|SEAL|RUN|BODY|CSP|DATA|SCRIPT)__")


def build_report(
    run: Mapping[str, Any],
    *,
    report_node: str,
    doc_artifact: str,
    doc_hash_ok: bool | None,
    doc: Mapping[str, Any],
    seal: Mapping[str, Any],
    chains: Mapping[str, Sequence[Mapping[str, Any]]],
    refs: Mapping[str, str],
    notes: Mapping[str, str],
    nodes: Mapping[str, Mapping[str, Any]],
    exported_at: str,
) -> dict[str, Any]:
    """页面里的数据（report-data）。

    - run：运行记录的几样（id、workflow_id、workflow_name、run_class、status、started_at、finished_at）
    - report_node / doc_artifact / doc_hash_ok：选中的报告节点、它的文档工件、取回时哈希核对的结果
    - doc：报告文档（正文的服务端渲染另走 render_body，这里不给：数据块只装点开数字要用的东西）
    - seal：{_sealed 的 seal 字段里的几样}（sealed、ok、manifest_hash、manifest_seq）
    - chains：每处引用的出处链（api/evidence._chain 的原样输出），键是调用方编的引用键
    - refs：片段 id → 引用键（点开这个片段看哪条链）
    - notes：片段 id → 为什么是现在这个状态（api/evidence._note）
    - nodes：节点 id → {label, type}
    """
    return {
        "schema": SCHEMA,
        "meta": {
            "run_id": run.get("id"), "workflow_id": run.get("workflow_id"),
            "workflow_name": display_name(run), "run_class": run.get("run_class"), "status": run.get("status"),
            "started_at": _iso(run.get("started_at")), "finished_at": _iso(run.get("finished_at")),
            "report": {"node_id": report_node, "label": (nodes.get(report_node) or {}).get("label") or report_node},
            "doc_artifact": doc_artifact, "doc_hash_ok": doc_hash_ok,
            "seal": {k: seal.get(k) for k in ("sealed", "ok", "manifest_hash", "manifest_seq") if k in seal},
            "exported_at": exported_at,
        },
        "nodes": dict(nodes),
        "chains": {k: [*v] for k, v in chains.items()},
        "refs": dict(refs),
        "notes": dict(notes),
    }


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat() if hasattr(value, "isoformat") else str(value)


@lru_cache(maxsize=1)
def _assets() -> tuple[str, str]:
    here = Path(__file__).parent
    return (here / "template.html").read_text(encoding="utf-8"), (here / "runtime.js").read_text(encoding="utf-8")


def _json_text(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).translate(_JSON_ESCAPES)


def render_html(data: Mapping[str, Any], doc: Mapping[str, Any]) -> str:
    """把正文和数据填进模板：样式、脚本都内联，零外链。

    内容安全策略按哈希只放行这一段脚本，默认什么都不许连：页面打开后不会、也不能向任何地方发请求。
    正文是服务端渲染的 HTML，封存哈希、运行 ID 直接写进页面：邮件、即时通讯的附件预览常常不执行脚本，
    也要读得到报告、看得到该拿什么去系统里对照；执行脚本后头部换成完整的信息、数字点得开出处。
    占位符一次替换完，填进去的内容不会再被当成占位符。（同 bundle/build.py render_html 的做法。）"""
    template, script = _assets()
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    meta = data.get("meta") or {}
    name = str(meta.get("workflow_name") or UNSAVED_NAME)
    seal = meta.get("seal") or {}
    manifest = str(seal.get("manifest_hash") or "")
    # 不执行脚本时封存那一格写什么：封存了给哈希（拿去系统对照），没封存照实说
    seal_text = f"封存清单哈希 <code>{_esc(manifest)}</code>（运行结束并封存后可用它与系统对照）" if manifest \
        else "导出时这次运行尚未封存：出处与封存状态是导出那一刻的记录"
    values = {
        "TITLE": _esc(f"{name} · 报告"),
        "NAME": _esc(name),
        "SEAL": seal_text,
        "RUN": _esc(str(meta.get("run_id") or "")),
        "BODY": render_body(doc),
        "CSP": f"'sha256-{digest}'",
        "DATA": _json_text(data),
        "SCRIPT": script,
    }
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
