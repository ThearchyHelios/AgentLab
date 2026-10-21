"""分析结论在两份自包含页面里的渲染（第 1 波 W1-b）：报告导出（report_export/runtime.js）点开判定语、分析范围里的编号；
可验证报告（bundle/verifier.js）点开判定语看分析结论一节、正文后面的分析范围。

浏览器里的完整检查（check-report-export、check-bundle）第 2 波由编排者跑；这里在 node 里用一个最小的 DOM 替身把两份脚本
真正跑一遍，挡住渲染分析结论那几步时的脚本错误（引用了不存在的文案、函数），并核对面板里写出来的字。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import app.bundle
import app.report_export
from app.main import app as asgi
from tests.test_analysis_bundle import bundle_of
from tests.test_analysis_evidence_api import SCOPE, TEXT, sealed_run, seg_id

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="没有 node，无法执行页面上的脚本")

RUNTIME = Path(app.report_export.__file__).parent / "runtime.js"
VERIFIER = Path(app.bundle.__file__).parent / "verifier.js"

#: 最小的 DOM 替身：元素、文字、片段，getElementById 按 id 建元素；textContent 递归拼接
DOM = r"""
const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase())
class Node_ {
  constructor() { this.children = []; this.parent = null }
  append(...kids) {
    for (const k of kids) {
      const n = typeof k === 'string' ? new Text_(k) : k
      if (n instanceof Frag_) { for (const c of n.children) { c.parent = this; this.children.push(c) } n.children = [] }
      else { n.parent = this; this.children.push(n) }
    }
  }
  replaceChildren(...kids) { this.children = []; this.append(...kids) }
  get textContent() { return this.children.map((c) => c.textContent).join('') }
  set textContent(v) { this.children = [new Text_(String(v))] }
  walk(f) { f(this); for (const c of this.children) c.walk(f) }
}
class Text_ extends Node_ {
  constructor(t) { super(); this.text = t }
  get textContent() { return this.text }
  set textContent(v) { this.text = String(v) }
}
class Frag_ extends Node_ {}
class El_ extends Node_ {
  constructor(tag) {
    super(); this.tag = tag; this.attrs = {}; this.dataset = {}; this.style = {}; this.hidden = false; this.listeners = {}
    this.className = ''; const cls = new Set(); this.classList = { add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c) }
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v) }
  getAttribute(k) { return this.attrs[k] ?? null }
  removeAttribute(k) { delete this.attrs[k] }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f) }
  focus() {} scrollIntoView() {}
  getBoundingClientRect() { return { top: 0, bottom: 0 } }
  querySelector() { return new El_('span') }
  closest() { return this }
}
globalThis.Node = Node_
const ids = {}
globalThis.window = { scrollBy() {} }
globalThis.document = {
  readyState: 'complete', activeElement: null, documentElement: new El_('html'),
  getElementById(id) { return ids[id] ||= new El_('div') },
  createElement(tag) { return new El_(tag) }, createTextNode(t) { return new Text_(t) },
  createDocumentFragment() { return new Frag_() }, addEventListener() {}, querySelectorAll() { return [] },
}
const fs = require('fs')
const input = JSON.parse(fs.readFileSync(0, 'utf8'))
const find = (root, pred) => { const out = []; root.walk((n) => { if (n instanceof El_ && pred(n)) out.push(n) }); return out }
"""

RUN_RUNTIME = DOM + r"""
document.getElementById('report-data').textContent = input.data
new Function(fs.readFileSync(process.argv[1], 'utf8'))()
const out = {}
for (const seg of input.segs) {
  const el = new El_('button')
  for (const [k, v] of Object.entries(seg)) el.setAttribute(`data-${k}`, v)
  el.textContent = seg.text
  for (const f of document.getElementById('report').listeners.click || []) f({ target: el })
  out[seg.seg] = { title: document.getElementById('detail-title').textContent,
    body: document.getElementById('detail-body').textContent,
    steps: find(document.getElementById('detail-body'), (n) => n.attrs['data-step']).map((n) => n.attrs['data-step']) }
}
process.stdout.write(JSON.stringify(out))
"""

RUN_VERIFIER = DOM + r"""
document.getElementById('bundle-data').textContent = input.data
document.getElementById('seal-hash').textContent = input.seal
new Function(fs.readFileSync(process.argv[1], 'utf8'))()
setTimeout(() => {
  const reports = document.getElementById('reports')
  const out = { scope: find(reports, (n) => 'data-scope' in n.attrs).map((n) => n.textContent), opened: {} }
  for (const btn of find(reports, (n) => n.attrs['data-num'])) {
    for (const f of btn.listeners.click || []) f()
    out.opened[btn.attrs['data-num']] = document.getElementById('detail-body').textContent
  }
  out.verified = document.documentElement.attrs['data-verified'] ?? null
  process.stdout.write(JSON.stringify(out))
}, 200)
"""


def run_node(script: str, path: Path, payload: dict[str, Any]) -> Any:
    done = subprocess.run([NODE, "-e", script, str(path)], input=json.dumps(payload), capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout)


async def test_the_export_page_opens_the_analysis_steps_and_the_scope_numbers():
    made = await sealed_run(TEXT, doc_extra=SCOPE)
    async with AsyncClient(transport=ASGITransport(app=asgi), base_url="http://test") as client:
        html = (await client.get(f"/api/runs/{made['run']}/report.html")).text
    data = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S).group(1)
    phrase = seg_id(made["doc"], "a:A1")
    cell = seg_id(made["doc"], "v:A1.r0.delta")
    segs = [{"seg": phrase, "kind": "analysis", "state": "deterministic", "text": made["result"]["phrase"]},
            {"seg": cell, "kind": "number", "state": "deterministic", "text": "-120"},
            {"seg": "scope:A1", "kind": "analysis", "state": "deterministic", "text": "A1"}]
    out = run_node(RUN_RUNTIME, RUNTIME, {"data": json.loads(json.dumps(data)), "segs": segs})
    opened = out[phrase]
    assert opened["steps"] == ["analysis", "analysis_method", "analysis_calc", "analysis_input", "query",
                               "analysis_recompute", "analysis_seal"]
    assert opened["title"].startswith("判定「销售额下降")
    body = opened["body"]
    for text in ("判定标准一段占总变化一半以上算主要来源", "Agent 写的假设地区拖累", "实际用的阈值", "「主要来源」的占比",
                 "华东的占比 = 华东的变化 ÷ 总变化 = -120 ÷ -194 = 61.9%", "结果表（列出 4 行，共 4 行）",
                 "复算失败：系统里还没有这种分析的复算实现", "封存范围内的台账记过这份分析结论"):
        assert text in body, text
    assert "结果表第 1 行「delta」" in out[cell]["body"]
    assert out["scope:A1"]["steps"] == opened["steps"]


async def test_the_bundle_page_shows_the_analysis_detail_and_the_scope():
    bundle, _, _ = bundle_of(doc_extra=SCOPE)
    out = run_node(RUN_VERIFIER, VERIFIER, {"data": json.dumps(bundle), "seal": bundle["meta"]["manifest_hash"]})
    assert out["verified"] == ""
    [scope] = out["scope"]
    assert scope.startswith("分析范围") and "现象A1：销售额下降" in scope and "拆过A1（按地区）：主要来自华东" in scope
    assert "没拆渠道" in scope and "这次做了 2 次差异检验" in scope
    phrase = next(text for text in out["opened"].values() if "出处分析结论 A1 的判定语" in text)
    assert "核对结果✓ 通过" in phrase and "分析结论里的值「销售额下降 194 万" in phrase
    assert "判定语销售额下降 194 万" in phrase and "方法与版本decompose.additive@1" in phrase
    assert "复算失败：系统里还没有这种分析的复算实现（导出时服务端按封存的输入复算的结论；本页只核哈希" in phrase
    value = next(text for text in out["opened"].values() if "具名的数「top_share」" in text)
    assert "引用的值0.6186" in value and "正文显示61.9%（与按显示规则渲染的结果一致）" in value
    scope_item = next(text for key, text in out["opened"].items() if key.endswith("|scope:A1"))
    assert "核对结果✓ 通过" in scope_item and "正文显示" not in scope_item
