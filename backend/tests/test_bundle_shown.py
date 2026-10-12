"""可验证报告核对正文显示的字（bundle/verifier.js 的显示规则）。

以前页面上 ⑤ 只核「引用的值 = 口径卡的值或快照的格子」，不核正文里显示的字：只改显示文字（cite.value 不动）、重算
全部哈希的伪造页照样全部通过。现在浏览器按服务端的显示规则（engine/evidence.py 的 render_number / render_cell）把那个
值渲染一遍，和正文显示的字逐字比对（只去掉首尾空白）。这里用 node 直接载入 verifier.js（没有页面时它交出核对用的纯函数）：

- 移植的渲染和服务端逐条对得上：千分位、不分组、小数位（含负数位）、比率显示成百分数、万 / 亿换算、pct、int、.N、
  货币等单位后缀、负数、0、超过 2^53 的整数（按 JSON 原文，不经 double）、DECIMAL 落成的数字文本、文字压成一行、竖线、
  日期时间按文档记下的显示规则版本；服务端渲染不出来的，浏览器也不给出一个字。本页没有移植的写法（对象、列表、非 ASCII
  数字）一律渲染不出来，判无法核对，不会给出一串不一样的字；
- 整份核对（verify）：compose_doc 真实组装的报告原样通过；只改一个数字或取值的显示文字、重算全部哈希的伪造页，
  ①–④ 照样对得上，⑤ 里那一项不通过，原因点名「正文显示「X」，引用的值是「Y」」；认不出的换算判无法核对，不判通过；
- 口径卡单位前后带空白（「元 」，服务端照原样接）：两边都只去首尾空白，原样导出照样通过；运行输入照旧无法核对，显示
  文字拿引用的值核对，只改显示文字的不通过；出处面板里数字文本照原文写；
- 正文的排法：核对通过的字要原样显示——伪造的链接藏不起它（整句按原文显示），正文里不显示的判不通过；紧挨着补数字、
  负号（长得像负号的横线也算，隔着不占位置的字也算），紧挨着显示成什么说不准的字（控制字符、组合符号这类），同一句里
  有改变文字方向的字（方向控制字符、希伯来文和阿拉伯文这类从右往左书写的文字），判无法核对；区间、日期这类真报告里的
  写法照常通过；列表缩进有上限、表格只排有格的列，伪造的缩进、列号挤不走核对过的数。

数据全是虚构的。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import app.bundle
from app.core.artifact_store import canonical_json
from app.engine.evidence import RenderError, _Snapshots, render_cell, render_number

NODE = shutil.which("node")
VERIFIER = Path(app.bundle.__file__).parent / "verifier.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="没有 node，无法执行页面上的核对脚本")

#: 载入 verifier.js（在 node 里没有 document，它把核对用的纯函数交给 module.exports），按 stdin 交来的任务算。
#: noBigInt：载入时把 BigInt 遮掉，当作较早的浏览器
HARNESS = r"""
const fs = require('fs')
const { createHash } = require('crypto')
const input = JSON.parse(fs.readFileSync(0, 'utf8'))
const mod = { exports: {} }
new Function('module', 'BigInt', fs.readFileSync(process.argv[1], 'utf8'))(mod, input.noBigInt ? undefined : BigInt)
const v = mod.exports

function render(c) {
  try {
    const value = v.parseJson(c.json)
    const token = v.tokenAt(v.numberTokens(c.json), [])
    if (c.cell) return { shown: v.renderCell(value, token, c.conv, c.kind, c.display) }
    const decimals = v.parseJson(c.decimals)
    return { shown: v.renderNumber(value, token, { unit: c.unit, decimals,
      decimalsToken: v.tokenAt(v.numberTokens(c.decimals), []), fmt: c.format, conv: c.conv }) }
  } catch (e) {
    return { shown: null, why: String(e && e.message), unshown: e instanceof v.Unshown }
  }
}

async function main() {
  if (input.task === 'render') return input.cases.map(render)
  // 列表各项显示的缩进级数、表格排出来的列号（核对和显示共用的那两个函数）
  if (input.task === 'layout') {
    return input.blocks.map((b) => (b.type === 'list' ? v.listItems(b.units).map((i) => i.depth)
      : v.tableCells(b.units).cols))
  }
  const digest = async (bytes) => createHash('sha256').update(bytes).digest('hex')
  const out = await v.verify(input.data, digest, input.seal)
  const results = (items) => items.map((i) => i.result)
  return {
    events: results(out.events), manifest: results(out.manifest), artifacts: results(out.artifacts),
    chain: results(out.chain), meta: results(out.meta),
    // shownOk：显示文字核对过、对上了；truthText：出处面板「快照里的值」写的字（数字文本照原文）；onPage：正文里这处
    // 可点的数字上读的人看到的字（没有显示成可点的数字是 null）
    numbers: out.numbers.map((n) => ({ key: n.key, ref: n.seg.ref ?? null, text: String(n.seg.text ?? ''),
      result: n.result, reason: n.reason || '', expected: n.expected ?? null, shownOk: !!n.shownOk,
      truthText: n.truthText ?? null, onPage: n.onPage ?? null })),
  }
}
main().then((r) => process.stdout.write(JSON.stringify(r)), (e) => {
  console.error((e && e.stack) || String(e))
  process.exit(1)
})
"""


def node(task: str, **payload: Any) -> Any:
    done = subprocess.run([NODE, "-e", HARNESS, str(VERIFIER)], input=json.dumps({"task": task, **payload}),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


# --------------------------------------------------------------------------
# 渲染规则逐条对照服务端
# --------------------------------------------------------------------------

#: 口径卡、运行输入里的值（render_number）。浮点、整数（含超过 2^53 的、1000 位上下的）、0 和 -0.0、文字、布尔、空值、
#: 非有限数（服务端渲染不出来）
NUMBERS: list[Any] = [
    0, 7, -7, 12840, 8381.25, 74613.65, -74613.65, 1234567890.5, 0.0235, -0.0815, 0.346, 5.8113, 2.675, -2.675,
    0.1 + 0.2, 1e16, 1.5e-7, 0.004, -0.0, 999.995, 0.005, 1e21, 123456789012345678901234567890, 2**53 + 1,
    202609071234567890, -202609071234567891, 1e300, 10**999, 10**1000, 49196.64, 30278.22,
    float("nan"), float("inf"), True, False, None, "华南", "  华南 \n 大区\u3000", "a|b", "2026", "",
]
FORMATS = [None, "thousands", "plain", "percent_of_ratio", "bogus"]
DECIMALS = ["null", "0", "1", "2", "4", "-2", "6", "100", "101", "-101", "true", "2.0", "\"2\""]
CONVS = [None, "", "万", "亿", "pct", "int", ".0", ".3", ".6", ".7", "千", "PCT"]
#: 单位照口径卡上写的原样接在后面：前后带空白（含全角空格）的也照接，服务端不去空白
UNITS = ["", "元", "美元", "%", "％", "人次", "元 ", "元\u3000", " 元"]


def py_number(value: Any, unit: str, decimals: Any, fmt: Any, conv: Any) -> str | None:
    try:
        return render_number(value, unit=unit, decimals=decimals, fmt=fmt, conv=conv)
    except RenderError:
        return None


def py_cell(raw: Any, conv: Any, kind: Any, display: int) -> str | None:
    try:
        return render_cell(_Snapshots(display=display).shown(raw), conv, kind=kind)
    except RenderError:
        return None


def number_cases() -> list[dict[str, Any]]:
    cases = []
    for value in NUMBERS:
        text = json.dumps(value, ensure_ascii=False)
        for fmt in FORMATS:
            for decimals in DECIMALS:
                for conv in CONVS:
                    for unit in (UNITS if fmt in (None, "thousands") and decimals in ("null", "2") else ("元", "%")):
                        cases.append({"json": text, "decimals": decimals, "format": fmt, "conv": conv, "unit": unit,
                                      "py": py_number(json.loads(text), unit, json.loads(decimals), fmt, conv)})
    # 随机：各种量级、小数位的浮点和整数，按口径卡常见的几种格式
    rng = random.Random(20261006)
    for _ in range(4000):
        scale = 10 ** rng.randint(-8, 14)
        value: Any = rng.choice([round(rng.uniform(-1, 1) * scale, rng.randint(0, 10)), rng.randint(-10**18, 10**18),
                                 rng.uniform(-1, 1) * scale, rng.randint(-999, 999) / 1000])
        fmt = rng.choice(FORMATS[:4])
        decimals = rng.choice(["null", "0", "1", "2", "3", "4", "-1", "-3", "8"])
        conv = rng.choice([None, None, "万", "亿", "pct", "int", ".1", ".2", ".5"])
        unit = rng.choice(["", "元", "单"])
        text = json.dumps(value)
        cases.append({"json": text, "decimals": decimals, "format": fmt, "conv": conv, "unit": unit,
                      "py": py_number(json.loads(text), unit, json.loads(decimals), fmt, conv)})
    return cases


#: 查询快照里的一格（render_cell）：数、DECIMAL 落成的数字文本（带空白、指数、前导零的编号、正号）、文字（多个空白、
#: 全角空格、竖线、换行）、ISO 日期时间（各种写法，2 版显示规则把 T 换成空格）、布尔、空值、超长的整数文本
CELLS: list[Any] = [
    1280.5, 732, -1200.75, 0, -0.0, 202609071234567890, 2**63, 0.1 + 0.2, 1e-11, 1e22,
    "45678.50", "960.00", "-12.5", " 12 ", "1E+5", "1E-7", "1E+999", "1E+1000", "0.000000000049", "0012", "+5",
    "1e5", "-0", "1.", ".5", "1" * 4301, "9" * 4300,
    "华南", "  多  空格\n换行 ", "\u3000全角空格\u3000", "a|b|c", "\x1c分隔\x1f", "\ufeff零宽\ufeff",
    "2026-09-07T08:21:39", "2026-09-07T08:21:39.123+08:00", " 2026-09-07T08:21Z ", "2026-09-07T8:21",
    "2026-09-07 08:21:39", "2026-09-07", True, False, None,
]
CELL_KINDS = [None, "number", "text", "datetime"]
CELL_CONVS = [None, "万", "亿", "pct", "int", ".2", "千"]


def cell_cases() -> list[dict[str, Any]]:
    cases = []
    for raw in CELLS:
        text = json.dumps(raw, ensure_ascii=False)
        for kind in CELL_KINDS:
            for conv in CELL_CONVS:
                for display in (1, 2):
                    cases.append({"cell": True, "json": text, "kind": kind, "conv": conv, "display": display,
                                  "py": py_cell(json.loads(text), conv, kind, display)})
    return cases


def test_the_ported_rules_render_exactly_like_the_server():
    """服务端渲染得出来的，浏览器逐字一样；服务端渲染不出来的（引用就算解析不了，不会出现在报告里），浏览器也不给出字。"""
    cases = number_cases() + cell_cases()
    got = node("render", cases=[{k: v for k, v in c.items() if k != "py"} for c in cases])
    wrong = [(c, g) for c, g in zip(cases, got) if g["shown"] != c["py"]]
    assert not wrong, [(c["json"][:40], c.get("format"), c.get("decimals"), c.get("unit"), c.get("conv"),
                        c.get("kind"), c.get("display"), c["py"], g) for c, g in wrong[:12]]
    # 服务端渲染不出来的，浏览器给的是「核对不了」（Unshown），不是脚本自己出错
    assert all(g["unshown"] for c, g in zip(cases, got) if c["py"] is None)


@pytest.mark.parametrize("raw", [{"a": 1, "b": [1, 2.0]}, [1, "x"], "1٢٣", "0.٥"])
@pytest.mark.parametrize("kind", [None, "number"])
def test_shapes_the_page_does_not_port_cannot_be_checked(raw, kind):
    """对象、列表（服务端按 Python 的 json.dumps 写）、非 ASCII 数字的数字文本（Python 按数渲染）：本页没有移植，
    渲染不出来，判无法核对；不会给出一串和服务端不一样的字。"""
    text = json.dumps(raw, ensure_ascii=False)
    [got] = node("render", cases=[{"cell": True, "json": text, "kind": kind, "conv": None, "display": 2}])
    assert got["shown"] is None and got["unshown"], got


def test_integers_beyond_double_precision_render_from_the_json_text():
    """订单号这类超过 2^53 的整数：JSON.parse 读成 double 会丢位，渲染按 JSON 原文取精确值。"""
    order = 202609071234567891
    [number, cell] = node("render", cases=[
        {"json": str(order), "decimals": "null", "format": None, "conv": None, "unit": "号"},
        {"cell": True, "json": str(order), "kind": "number", "conv": None, "display": 2}])
    assert number["shown"] == "202,609,071,234,567,891号" and cell["shown"] == "202,609,071,234,567,891"


def test_floats_round_half_up_on_their_shortest_decimal_form():
    """浮点按最短的十进制写法（Python 的 repr、JS 的 String 同一个）四舍五入：2.675 留两位是 2.68，不是 double
    里的 2.67499…；-8.15 留一位是 -8.2（远离 0）。"""
    got = node("render", cases=[
        {"json": "2.675", "decimals": "2", "format": None, "conv": None, "unit": ""},
        {"json": "-0.0815", "decimals": "3", "format": "percent_of_ratio", "conv": None, "unit": ""},
        {"json": "999.995", "decimals": "2", "format": None, "conv": None, "unit": "元"}])
    assert [g["shown"] for g in got] == ["2.68", "-8.2%", "1,000.00元"]


# --------------------------------------------------------------------------
# 整份核对：原样导出全部通过，只改显示文字的伪造页点名不通过
# --------------------------------------------------------------------------


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def seal_of(bundle: dict[str, Any]) -> str:
    lines = [f"{e['seq']}|{e['type']}|{e['node_id'] or ''}|{e['data_sha256']}"
             for e in sorted(bundle["events"], key=lambda e: e["seq"])]
    return sha("\n".join(lines))


def doc_of(bundle: dict[str, Any], node_id: str = "write") -> tuple[dict[str, Any], dict[str, Any]]:
    doc_id = next(r["doc_artifact"] for r in bundle["reports"] if r["node_id"] == node_id)
    art = next(a for a in bundle["artifacts"] if a["id"] == doc_id)
    return art, json.loads(base64.b64decode(art["b64"]))


def segment(doc: dict[str, Any], ref: str) -> dict[str, Any]:
    return next(s for b in doc["blocks"] for u in b["units"] for s in u["segments"] if s.get("ref") == ref)


def reseal(bundle: dict[str, Any], art: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """改过的文档重新落成工件：文档 id、引用它的事件（规范字符串和哈希）、封存清单哈希全部重算。和服务端写工件一样
    用规范 JSON：① 到 ④ 都对得上，伪造只可能在 ⑤ 露馅。伪造者还能写进不成对的代理项（JSON 里的 \\ud835 转义），
    规范 JSON 编不成 UTF-8：这时照 JSON 的转义写，页面只看字节的哈希，照样对得上。"""
    try:
        raw = canonical_json(doc).encode()
    except UnicodeEncodeError:
        raw = json.dumps(doc, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    old, new = art["id"], sha(raw)
    art["id"], art["b64"] = new, base64.b64encode(raw).decode()
    for report in bundle["reports"]:
        if report["doc_artifact"] == old:
            report["doc_artifact"] = new
    for event in bundle["events"]:
        if isinstance(event.get("canonical"), str) and old in event["canonical"]:
            event["canonical"] = event["canonical"].replace(old, new)
            event["data_sha256"] = sha(event["canonical"])
    bundle["meta"]["manifest_hash"] = seal_of(bundle)
    return bundle


def forged(ref: str, text: str, bundle: dict[str, Any] | None = None, **cite: Any) -> dict[str, Any]:
    """经营月报样本（或给的 bundle）里一个片段的显示文字改成 text（cite.value 不动，cite.rendered 跟着改，cite 另改
    cite 里给的几项），重算全部哈希。"""
    from tests.bundle_samples import formats_bundle

    bundle = bundle or formats_bundle()
    art, doc = doc_of(bundle)
    seg = segment(doc, ref)
    seg["text"] = text
    seg["cite"].update(rendered=text, **cite)
    return reseal(bundle, art, doc)


def rewritten(bundle: dict[str, Any], change: Any) -> dict[str, Any]:
    """change(文档) 就地改报告文档，再重算全部哈希（同 reseal）。"""
    art, doc = doc_of(bundle)
    change(doc)
    return reseal(bundle, art, doc)


def by_id(doc: dict[str, Any], seg_id: str) -> dict[str, Any]:
    return next(s for b in doc["blocks"] for u in b["units"] for s in u["segments"] if s.get("id") == seg_id)


def verdicts(bundle: dict[str, Any]) -> dict[str, Any]:
    return node("verify", data=bundle, seal=bundle["meta"]["manifest_hash"])


def item(got: dict[str, Any], ref: str) -> dict[str, Any]:
    return next(n for n in got["numbers"] if n["ref"] == ref)


def test_every_format_in_the_monthly_sample_passes_as_exported():
    """经营月报样本（compose_doc 真实组装：货币单位、万 / 亿、比率、负数、不分组、小数位、pct、DECIMAL 文本、
    超过 2^53 的订单号、日期时间、文字取值）原样导出：每个数字和取值都通过，显示文字核对过（expected 等于正文）。"""
    from tests.bundle_samples import formats_bundle

    got = verdicts(formats_bundle())
    assert all(r == "pass" for key in ("events", "manifest", "artifacts", "chain", "meta") for r in got[key]), got
    numbers = got["numbers"]
    assert len(numbers) == 19
    assert all(n["result"] == "pass" and n["expected"] == n["text"] for n in numbers), \
        [n for n in numbers if n["result"] != "pass" or n["expected"] != n["text"]]
    shown = {n["ref"]: n["text"] for n in numbers}
    assert shown == {
        "m:gmv": "74,613.65元", "m:gmv|万": "7.46万元", "m:annual|亿": "12.35亿元", "m:refund": "-3,120.50元",
        "m:conv_rate": "2.35%", "m:growth": "-8.2%", "m:orders": "12840单", "m:avg": "5.81元", "m:avg|.3": "5.811元",
        "m:avg|int": "6元", "m:share|pct": "34.6%", "m:export": "15,230.4美元",
        "v:Q1.r0.region": "华南", "v:Q1.r0.gmv": "45,678.5", "v:Q1.r0.gmv|万": "4.57万", "v:Q1.r1.region": "华东",
        "v:Q1.r1.gmv": "-1,200.75", "v:Q1.r0.order_no": "202,609,071,234,567,890",
        "v:Q1.r0.paid_at": "2026-09-07 08:21:39",
    }


@pytest.mark.parametrize("ref, text, cited, expected", [
    ("v:Q1.r0.region", "华东", "华南", None),                                          # 文字取值：换了地区
    ("m:gmv", "84,613.65元", "74613.65", "74,613.65元"),                               # 千分位 + 货币单位
    ("m:gmv|万", "8.46万元", "74613.65", "7.46万元"),                                  # 口径卡引用换算成万
    ("m:annual|亿", "13.35亿元", "1234567890.5", "12.35亿元"),                         # 换算成亿
    ("m:refund", "3,120.50元", "-3120.5", "-3,120.50元"),                              # 负数去掉负号
    ("m:conv_rate", "3.35%", "0.0235", "2.35%"),                                       # 比率显示成百分数
    ("m:growth", "8.2%", "-0.0815", "-8.2%"),                                          # 负的百分数
    ("m:share|pct", "43.6%", "0.346", "34.6%"),                                        # pct 换算
    ("m:orders", "13840单", "12840", "12840单"),                                       # 不分组
    ("m:avg", "5.80元", "5.8113", "5.81元"),                                           # 口径卡的小数位
    ("m:avg|.3", "5.812元", "5.8113", "5.811元"),                                      # |.N
    ("m:avg|int", "5元", "5.8113", "6元"),                                             # |int
    ("m:export", "15,230.4元", "15230.4", "15,230.4美元"),                             # 换了货币单位
    ("v:Q1.r0.gmv", "54,678.5", "45678.5", "45,678.5"),                                # DECIMAL 落成的数字文本
    ("v:Q1.r1.gmv", "1,200.75", "-1200.75", "-1,200.75"),                              # 负数单元格
    ("v:Q1.r0.gmv|万", "5.57万", "45678.5", "4.57万"),                                 # 单元格换算成万
    ("v:Q1.r0.order_no", "202,609,071,234,567,891", "202609071234567890",
     "202,609,071,234,567,890"),                                                       # 超过 2^53 的整数
    ("v:Q1.r0.paid_at", "2026-09-08 08:21:39", "2026-09-07T08:21:39", "2026-09-07 08:21:39"),   # 日期时间
])
def test_changing_only_the_shown_text_fails_and_names_both(ref, text, cited, expected):
    """只改显示文字、引用值不动、重算全部哈希：①–④ 照样通过，⑤ 里只有这一项不通过，原因点名正文显示的字和引用的值
    （数字另写按显示规则应显示的字）。"""
    got = verdicts(forged(ref, text))
    assert all(r == "pass" for key in ("events", "manifest", "artifacts", "chain") for r in got[key]), got
    hit = next(n for n in got["numbers"] if n["ref"] == ref)
    assert hit["result"] == "fail", hit
    assert hit["reason"].startswith(f"正文显示「{text}」，引用的值是「{cited}」"), hit["reason"]
    if expected is None:
        assert "应显示为" not in hit["reason"], hit["reason"]
    else:
        assert hit["reason"].endswith(f"，按显示规则应显示为「{expected}」"), hit["reason"]
    assert all(n["result"] == "pass" for n in got["numbers"] if n["ref"] != ref)


def test_surrounding_blanks_in_the_shown_text_are_tolerated():
    """只去掉首尾空白：「 华南 」仍算和「华南」一致；中间多一个字就不是。"""
    assert next(n for n in verdicts(forged("v:Q1.r0.region", " 华南\u3000"))["numbers"]
                if n["ref"] == "v:Q1.r0.region")["result"] == "pass"
    assert next(n for n in verdicts(forged("v:Q1.r0.region", "华南区"))["numbers"]
                if n["ref"] == "v:Q1.r0.region")["result"] == "fail"


@pytest.mark.parametrize("ref, cite, text", [
    ("m:gmv", {"conv": "千"}, "74.61千元"),           # 系统没有的换算
    ("v:Q1.r0.gmv", {"conv": "千"}, "45.68千"),
    ("m:gmv", {"conv": 4}, "7.46万元"),               # 换算不是文字
])
def test_a_format_the_page_does_not_know_cannot_pass(ref, cite, text):
    """不认识的写法：渲染不出来，判无法核对显示文字，不判通过。"""
    hit = next(n for n in verdicts(forged(ref, text, **cite))["numbers"] if n["ref"] == ref)
    assert hit["result"] == "na" and hit["reason"].startswith("无法核对显示文字："), hit


def test_an_unknown_cell_display_version_cannot_pass():
    """文档记下的单元格显示规则版本认不出（服务端复核记 bad_schema）：单元格的显示文字判无法核对；口径卡的照常核对。"""
    from tests.bundle_samples import formats_bundle

    bundle = formats_bundle()
    art, doc = doc_of(bundle)
    doc["cell_display"] = 3
    got = verdicts(reseal(bundle, art, doc))
    cells = [n for n in got["numbers"] if n["ref"].startswith("v:")]
    assert cells and all(n["result"] == "na" and n["reason"].startswith("无法核对显示文字：") for n in cells)
    assert all(n["result"] == "pass" for n in got["numbers"] if n["ref"].startswith("m:"))


def test_a_browser_without_bigint_cannot_check_numbers_but_still_checks_the_rest():
    """较早的浏览器没有 BigInt：数字的显示文字判无法核对（写明原因），文字取值照常核对，别的核对项不受影响，脚本不出错。"""
    from tests.bundle_samples import formats_bundle

    bundle = formats_bundle()
    got = node("verify", data=bundle, seal=bundle["meta"]["manifest_hash"], noBigInt=True)
    assert all(r == "pass" for key in ("events", "manifest", "artifacts", "chain", "meta") for r in got[key])
    texts = {"v:Q1.r0.region", "v:Q1.r1.region", "v:Q1.r0.paid_at"}
    assert all(n["result"] == "pass" for n in got["numbers"] if n["ref"] in texts)
    # 原因写给读报告的人：说清该怎么办，不提 BigInt、正则这类实现细节
    assert all(n["result"] == "na" and n["reason"] == "无法核对显示文字：这个浏览器版本较旧，请用新版浏览器打开本文件"
               for n in got["numbers"] if n["ref"] not in texts)


def test_every_sample_page_still_passes_its_display_check():
    """check-bundle 的样本页（tests/bundle_samples.py）原样导出：每个核对得到出处的数字和取值，显示文字都核对通过；
    没有一项因为显示文字不通过或核对不了。"""
    from tests.bundle_samples import bundles

    for name, bundle in bundles().items():
        got = verdicts(bundle)
        assert not [n for n in got["numbers"] if n["reason"].startswith(("正文显示", "无法核对显示文字"))], name
        assert all(n["expected"] == n["text"] for n in got["numbers"] if n["result"] == "pass"), name


# --------------------------------------------------------------------------
# 单位前后带空白：服务端照原样接在数后面，只去首尾空白时两边一样去
# --------------------------------------------------------------------------


def card_with_unit(unit: str) -> dict[str, Any]:
    """经营月报的口径卡，销售额的单位换成 unit。"""
    from tests.bundle_samples import FORMATS_CARD

    card = copy.deepcopy(FORMATS_CARD)
    card["metrics"][0]["unit"] = unit
    return card


@pytest.mark.parametrize("unit", ["元 ", "元\u3000", " 元", "元 \u3000"])
def test_a_unit_with_surrounding_blanks_passes_as_exported(unit):
    """单位输入框不去空白，指标节点照存：服务端把「元 」原样接在数后面，正文显示「74,613.65元 」。只去掉首尾空白时
    两边一样去：原样导出的销售额和换算成万的那处都通过，不会被说成文件被改过。"""
    from tests.bundle_samples import formats_bundle

    got = verdicts(formats_bundle(card=card_with_unit(unit)))
    for ref in ("m:gmv", "m:gmv|万"):
        hit = item(got, ref)
        assert hit["result"] == "pass" and hit["shownOk"] and hit["expected"] == hit["text"], hit
    assert all(n["result"] == "pass" for n in got["numbers"]), [n for n in got["numbers"] if n["result"] != "pass"]


@pytest.mark.parametrize("unit", ["元 ", "元\u3000"])
@pytest.mark.parametrize("ref, digits", [("m:gmv", "84,613.65"), ("m:gmv|万", "8.46万")])
def test_a_unit_with_trailing_blanks_still_catches_a_changed_digit(unit, ref, digits):
    """单位带尾随空白时，改了一位数字照样不通过，原因点名改过的字。"""
    from tests.bundle_samples import formats_bundle

    text = f"{digits}{unit}"
    hit = item(verdicts(forged(ref, text, formats_bundle(card=card_with_unit(unit)))), ref)
    assert hit["result"] == "fail" and hit["reason"].startswith(f"正文显示「{text}」，引用的值是「74613.65」"), hit


# --------------------------------------------------------------------------
# 运行输入：输入本身不随文件附带，显示文字照样拿引用的值核对
# --------------------------------------------------------------------------

INPUTS = {"week_start": "2026-09-01", "target": 50000, "region": "华南", "rate": 0.125, "reviewed": True}
INPUT_TEXT = ("## 周报\n\n统计周期自 [[i:week_start]] 起，目标 [[i:target]]（约 [[i:target|万]]），重点地区 [[i:region]]，"
              "达成率 [[i:rate|pct]]，已复核：[[i:reviewed]]。")


def test_input_citations_check_their_shown_text():
    """运行输入不随文件附带，引用的值核对不了（照旧无法核对，原因不变）；正文显示的字照样拿引用的值按服务端的写法
    （不分组、不接单位，可带换算）重新渲染比对：原样导出时对得上，出处面板注明一致。"""
    from tests.bundle_samples import formats_bundle

    got = verdicts(formats_bundle(text=INPUT_TEXT, inputs=INPUTS))
    inputs = [n for n in got["numbers"] if n["ref"].startswith("i:")]
    assert {n["ref"]: n["text"] for n in inputs} == {
        "i:week_start": "2026-09-01", "i:target": "50000", "i:target|万": "5万", "i:region": "华南",
        "i:rate|pct": "12.5%", "i:reviewed": "是"}
    assert all(n["result"] == "na" and n["reason"] == "引用的是运行输入，运行输入未随文件附带" and n["shownOk"]
               for n in inputs), inputs


@pytest.mark.parametrize("ref, text, cited, expected", [
    ("i:week_start", "2026-09-08", "2026-09-01", None),          # 改了统计周期（文字取值）
    ("i:region", "华东", "华南", None),
    ("i:target", "60000", "50000", None),
    ("i:target|万", "6万", "50000", "5万"),
    ("i:rate|pct", "21.5%", "0.125", "12.5%"),
])
def test_changing_only_the_shown_text_of_an_input_fails(ref, text, cited, expected):
    """只改运行输入的显示文字（引用的值不动）、重算全部哈希：不通过，原因点名正文显示的字和引用的值。"""
    from tests.bundle_samples import formats_bundle

    hit = item(verdicts(forged(ref, text, formats_bundle(text=INPUT_TEXT, inputs=INPUTS))), ref)
    assert hit["result"] == "fail" and hit["reason"].startswith(f"正文显示「{text}」，引用的值是「{cited}」"), hit
    if expected:
        assert hit["reason"].endswith(f"，按显示规则应显示为「{expected}」"), hit


# --------------------------------------------------------------------------
# 出处面板：落成数字文本的值照原文写
# --------------------------------------------------------------------------


def test_numeric_text_cells_show_their_own_digits_in_the_panel():
    """DECIMAL(20,0) 落成数字文本的订单号：按数比对时读成 double 会丢位，出处面板「快照里的值」照快照的原文写，
    和「引用的值」一位不差；45678.50 这类也照原文写。"""
    from tests.bundle_samples import LEDGER, formats_bundle

    table = copy.deepcopy(LEDGER)
    table["rows"][0][2] = "202609071234567891"
    got = verdicts(formats_bundle(table=table))
    order = item(got, "v:Q1.r0.order_no")
    assert order["result"] == "pass" and order["text"] == "202,609,071,234,567,891", order
    assert order["truthText"] == "202609071234567891", order
    assert item(got, "v:Q1.r0.gmv")["truthText"] == "45678.50"
    # 不是数字文本的格子不另写原文：照旧按值写
    assert item(got, "v:Q1.r1.gmv")["truthText"] is None and item(got, "v:Q1.r0.region")["truthText"] is None


# --------------------------------------------------------------------------
# 正文的排法：核对通过的字要原样显示，旁边的字不能把它改读成别的数
# --------------------------------------------------------------------------


def test_every_checked_value_is_shown_on_the_page_as_checked():
    """原样导出：每个数字和取值都显示成可点的数字，读的人看到的字就是核对过的字。"""
    from tests.bundle_samples import formats_bundle

    got = verdicts(formats_bundle())
    assert all(n["onPage"] == n["text"] for n in got["numbers"]), [n for n in got["numbers"] if n["onPage"] != n["text"]]


def test_inline_markup_cannot_hide_a_checked_value():
    """伪造的链接把核对通过的数字、取值塞进链接地址（链接只显示文字、不显示地址），链接文字写成别的数：这一句改按
    未排版的原文显示，核对过的字一个不少地露出来，读的人看得到两个数。"""
    from tests.bundle_samples import formats_bundle

    def change(doc: dict[str, Any]) -> None:
        by_id(doc, "s3")["text"] += "[84,613.65元](x "          # 本月销售额 [84,613.65元](x 74,613.65元)（约 …
        by_id(doc, "s5")["text"] = ")" + by_id(doc, "s5")["text"]
        by_id(doc, "s36")["text"] += "[华南](x "                 # ），[华南](x 华东)冲减 …
        by_id(doc, "s38")["text"] = ")" + by_id(doc, "s38")["text"]

    got = verdicts(rewritten(formats_bundle(), change))
    for ref, text in (("m:gmv", "74,613.65元"), ("v:Q1.r1.region", "华东")):
        hit = item(got, ref)
        assert hit["result"] == "pass" and hit["onPage"] == text, hit


GLUED = "无法核对显示文字：紧挨着的正文里还有数字或负号，读起来和核对过的不是同一个值"
BLURRED = "无法核对显示文字：紧挨着的正文里有看不见或在不同设备上显示不同的字，读起来可能和核对过的不是同一个值"
TURNED = "无法核对显示文字：这一句里有从右往左书写的文字或改变文字方向的控制字符，显示出来的顺序可能和核对过的不一样"
#: 经营月报第一段里的数字（第二段是 Q1 的取值）
FIRST = {"m:gmv", "m:gmv|万", "m:annual|亿", "m:refund", "m:conv_rate", "m:growth", "m:orders", "m:avg", "m:avg|.3",
         "m:avg|int", "m:share|pct", "m:export"}


def _glue_before(seg_id: str, extra: str, after: str = ""):
    def change(doc: dict[str, Any]) -> None:
        by_id(doc, seg_id)["text"] += extra
        if after:
            nxt = by_id(doc, f"s{int(seg_id[1:]) + 2}")
            nxt["text"] = after + nxt["text"]
    return change


def _glue_after(seg_id: str, extra: str):
    def change(doc: dict[str, Any]) -> None:
        by_id(doc, seg_id)["text"] = extra + by_id(doc, seg_id)["text"]
    return change


def _split_pair(high: str, low: str):
    """销售额前面插两个文字片段：前一个以代理对的前半结尾，后一个是后半。页面上两半挨着，拼成一个字显示。"""
    def change(doc: dict[str, Any]) -> None:
        unit = next(u for b in doc["blocks"] for u in b["units"] if any(s.get("id") == "s4" for s in u["segments"]))
        at = next(i for i, s in enumerate(unit["segments"]) if s.get("id") == "s4")
        by_id(doc, "s3")["text"] += high
        unit["segments"].insert(at, {"kind": "text", "id": "s3b", "text": low, "state": "neutral"})
    return change


@pytest.mark.parametrize("ref, change", [
    ("m:gmv", _glue_before("s3", "1")),                      # 前面多一位：174,613.65元
    ("m:gmv", _glue_before("s3", "1" + chr(0x200B))),        # 隔着看不见的零宽空格
    ("m:gmv", _glue_before("s3", "1**", after="**")),        # 隔着排版时藏起来的粗体标记
    ("m:gmv", _glue_before("s3", chr(0x1D7F7))),             # 数学字母区的数字 1（占两个 UTF-16 单元）
    # 隔着显示时不占位置的字（Chrome 里量过，宽 0 到 1px 不到）：控制字符 DEL、未分配的默认不显示字、韩文填充符、
    # 对象替换符、发丝空格、蒙古文里一个宽 0 的字、叠在数字上的组合符号、变体选择符
    ("m:gmv", _glue_before("s3", "1" + chr(0x7F))),
    ("m:gmv", _glue_before("s3", "1" + chr(0xFFF0))),
    ("m:gmv", _glue_before("s3", "1" + chr(0x115F))),
    ("m:gmv", _glue_before("s3", "1" + chr(0xFFFC))),
    ("m:gmv", _glue_before("s3", "1" + chr(0x200A))),
    ("m:gmv", _glue_before("s3", "1" + chr(0x1843))),
    ("m:gmv", _glue_before("s3", "1" + chr(0x0301))),
    ("m:gmv", _glue_before("s3", "1" + chr(0xFE0F))),
    ("m:gmv", _glue_before("s3", "1" + chr(0x201A))),        # 和半角逗号长得一样的低位单引号（U+201A）
    ("m:gmv", _glue_before("s3", chr(0xD800) + "1")),        # 不成对的代理项不和后面的 1 拼成一个字
    ("m:gmv", _split_pair(chr(0xD835), chr(0xDFF7))),        # 代理对分在两个文字片段里，页面上照样拼成数字 1
    ("v:Q1.r0.gmv", _glue_after("s34", "0")),                # 后面多一位：45,678.50
    ("v:Q1.r0.gmv", _glue_after("s34", ".5")),               # 后面接小数：45,678.5.5
    ("v:Q1.r0.gmv", _glue_after("s34", chr(0x2024) + "5")),  # 和句点长得一样的单点前导符
    ("m:conv_rate", _glue_before("s12", "-")),               # 前面加负号：-2.35%
    ("m:conv_rate", _glue_before("s12", chr(0xFF0D))),       # 全角负号
    # 长得像负号的横线：连字符、不断行连字符、数字宽的横线、短破折号、长破折号、修饰字母负号、粗减号、小号长破折号、
    # 数学减号、上标负号，和显示出来同样是一道横线的汉字「一」、假名长音、制表符横线、罗马盎司符号（码点在两个
    # UTF-16 单元里）
    *[("m:conv_rate", _glue_before("s12", chr(cp))) for cp in (
        0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x02D7, 0x2796, 0xFE58, 0x2212, 0x207B, 0x4E00, 0x30FC, 0x2500,
        0x10191)],
    ("m:refund", _glue_before("s9", "-")),                   # 负数前面再加负号：--3,120.50元
    ("v:Q1.r0.paid_at", _glue_after("s45", "5")),            # 取值以数字结尾：2026-09-07 08:21:395
])
def test_digits_glued_to_a_checked_value_cannot_pass(ref, change):
    """伪造者在相邻的文字片段里补一位数、一个负号（隔着不占位置、显示成什么说不准的字，藏起来的粗体标记也算紧挨着；
    长得像负号的横线也算负号）：显示出来的数不止核对过的这几位，这一项判无法核对、写明原因；别的照常通过。"""
    from tests.bundle_samples import formats_bundle

    got = verdicts(rewritten(formats_bundle(), change))
    hit = item(got, ref)
    assert hit["result"] == "na" and hit["reason"] == GLUED and hit["shownOk"], hit
    assert all(n["result"] == "pass" for n in got["numbers"] if n["ref"] != ref)


@pytest.mark.parametrize("ref, change", [
    ("m:gmv", _glue_before("s3", chr(0x0336))),               # 叠在空格上的长横线：页面上像一个负号
    ("m:gmv", _glue_before("s3", chr(0x82))),                 # 控制字符，Chrome 里画成一道短横
    ("m:gmv", _glue_before("s3", chr(0x7F))),                 # 控制字符 DEL，什么都不显示
    ("m:gmv", _glue_before("s3", chr(0xAD))),                 # 可选连字符：在它那里换行时显示成连字符
    ("m:gmv", _glue_before("s3", chr(0xE000))),               # 私用区的字：各家字体画成什么都有
    ("v:Q1.r0.gmv", _glue_after("s34", chr(0x0378))),         # 未分配的码点紧跟在数字后面
    ("v:Q1.r0.gmv", _glue_after("s34", "." + chr(0x7F))),     # 隔着句点紧跟一个说不准的字
])
def test_characters_that_may_show_as_anything_next_to_a_value_cannot_pass(ref, change):
    """紧挨着数字的是显示成什么说不准的字（控制字符、组合符号、可选连字符、私用区和未分配的字）：各家浏览器、字体
    可能把它显示成数字、横线或者什么都不显示，判无法核对，原因说清是这类字；别的照常通过。"""
    from tests.bundle_samples import formats_bundle

    got = verdicts(rewritten(formats_bundle(), change))
    hit = item(got, ref)
    assert hit["result"] == "na" and hit["reason"] == BLURRED and hit["shownOk"], hit
    assert all(n["result"] == "pass" for n in got["numbers"] if n["ref"] != ref)


def test_uncertain_characters_where_they_cannot_change_the_value_still_pass():
    """说不准的字挨着的不是数字的头尾（销售额以「元」结尾，后面跟一个控制字符），表情后面的变体选择符，数字前面的波浪线，
    都改不了读到的数：照常通过。"""
    from tests.bundle_samples import formats_bundle

    def change(doc: dict[str, Any]) -> None:
        by_id(doc, "s5")["text"] = chr(0x7F) + chr(0x0336) + by_id(doc, "s5")["text"]
        by_id(doc, "s12")["text"] += chr(0x2705) + chr(0xFE0F)    # 转化率紧前面：对勾表情加上表情样式的变体选择符
        by_id(doc, "s14")["text"] += chr(0x301C)                   # 环比紧前面：波浪线不是横线，不算负号

    got = verdicts(rewritten(formats_bundle(), change))
    assert all(n["result"] == "pass" and n["onPage"] == n["text"] for n in got["numbers"]), got["numbers"]


@pytest.mark.parametrize("change, scope", [
    (lambda doc: by_id(doc, "s5").update(text=chr(0x202E) + by_id(doc, "s5")["text"]), FIRST),   # 从右往左覆盖
    # 希伯来字母：逻辑上在销售额后面的「5」显示在销售额前面（页面上读作 574,613.65元）
    (lambda doc: (by_id(doc, "s3").update(text=by_id(doc, "s3")["text"] + chr(0x05D0)),
                  by_id(doc, "s5").update(text="5" + by_id(doc, "s5")["text"])), FIRST),
    # 希伯来字母后面的「5」显示在 45,678.50 紧后面（读作 45,678.505）；阿拉伯字母同样
    (lambda doc: by_id(doc, "s34").update(text=chr(0x05D0) + "5" + by_id(doc, "s34")["text"]), None),
    (lambda doc: by_id(doc, "s34").update(text=chr(0x0628) + "5" + by_id(doc, "s34")["text"]), None),
    # 腓尼基字母（从右往左书写，码点在两个 UTF-16 单元里）
    (lambda doc: by_id(doc, "s34").update(text=chr(0x10900) + "5" + by_id(doc, "s34")["text"]), None),
])
def test_text_that_changes_direction_next_to_checked_values_cannot_pass(change, scope):
    """改变文字方向的控制字符、从右往左书写的文字（希伯来文、阿拉伯文这类）能把后面的一位数挪到数字的另一边显示，
    按字的先后看不出紧挨着：同一段里出现在别的片段里，这一段的数字、取值判无法核对；别的段照常通过。scope 是受影响的
    那一段（None：经营月报第二段，Q1 的取值）。"""
    from tests.bundle_samples import formats_bundle

    got = verdicts(rewritten(formats_bundle(), change))
    for n in got["numbers"]:
        inside = n["ref"] in scope if scope is not None else n["ref"] not in FIRST
        if inside:
            assert n["result"] == "na" and n["reason"] == TURNED, n
        else:
            assert n["result"] == "pass", n


def test_ranges_dates_and_signs_written_by_the_system_still_pass():
    """真报告里常见的写法不算紧挨着：两个数之间的连字符、破折号（区间）、日期和数字之间隔着字或空白、负数自己的负号。"""
    from tests.bundle_samples import formats_bundle

    text = ("## 区间\n\n销售额区间 [[m:gmv]]-[[m:export]]，流水区间 [[v:Q1.r0.gmv]]-[[v:Q1.r1.gmv]]。"
            "截至2026年9月[[m:orders]]，2026-09-07 当天客单价 [[m:avg]]，环比 [[m:growth]]，"
            "退款区间 [[m:refund]]" + chr(0x2014) + "[[m:annual|亿]]。")
    got = verdicts(formats_bundle(text=text))
    assert len(got["numbers"]) == 9
    assert all(n["result"] == "pass" and n["onPage"] == n["text"] for n in got["numbers"]), got["numbers"]


NOT_SHOWN = "正文里没有显示这一处：导出时不会出现这种情况，文件可能被改过"


def test_a_checked_value_in_a_block_the_page_does_not_show_fails():
    """分隔线不显示句子：把一段改成分隔线，这一段里核对通过的数字、取值读的人都看不到（伪造的字可以写在别处），
    服务端导出时不会出现，判不通过；别的段照常通过。"""
    from tests.bundle_samples import formats_bundle

    def change(doc: dict[str, Any]) -> None:
        doc["blocks"][2]["type"] = "hr"

    got = verdicts(rewritten(formats_bundle(), change))
    second = {"v:Q1.r0.region", "v:Q1.r0.gmv", "v:Q1.r0.gmv|万", "v:Q1.r1.region", "v:Q1.r1.gmv",
              "v:Q1.r0.order_no", "v:Q1.r0.paid_at"}
    for n in got["numbers"]:
        if n["ref"] in second:
            assert n["result"] == "fail" and n["reason"] == NOT_SHOWN and n["onPage"] is None, n
        else:
            assert n["result"] == "pass" and n["onPage"] == n["text"], n


def test_a_table_cell_outside_the_rows_and_columns_fails():
    """表格只显示表头和各行各列上的格：把一格的位置改成第 1.5 列，读的人看不到它，判不通过。原样导出时表格里每一格都
    显示成可点的数字。"""
    from tests.bundle_samples import FORMATS, formats_bundle

    bundle = formats_bundle(text=FORMATS + "\n\n[[table:Q1 cols=region,gmv rows=0-1]]")
    got = verdicts(bundle)
    assert all(n["result"] == "pass" and n["onPage"] == n["text"] for n in got["numbers"])
    moved: list[str] = []

    def change(doc: dict[str, Any]) -> None:
        table = next(b for b in doc["blocks"] if b["type"] == "table")
        unit = next(u for u in table["units"] if u.get("loc") == {"row": 1, "col": 1})
        unit["loc"] = {"row": 1, "col": 1.5}
        moved.extend(f"write|{s['id']}" for s in unit["segments"] if s["kind"] in ("number", "value"))

    got = verdicts(rewritten(bundle, change))
    assert len(moved) == 1
    for n in got["numbers"]:
        if n["key"] in moved:
            assert n["result"] == "fail" and n["reason"] == NOT_SHOWN, n
        else:
            assert n["result"] == "pass", n


def _list_unit(depth: Any) -> dict[str, Any]:
    return {"segments": [{"kind": "structural", "text": "- "}, {"kind": "text", "text": "一项"}], "depth": depth}


def test_list_indentation_is_capped_on_screen():
    """列表照级数缩进（每级 14px），最多 8 级：服务端按原文的缩进算级数、没有上限，伪造的级数写成十万或负数，那一项
    也留在屏幕上（读的人看得到核对过的数，窄屏不横向溢出）。核对和显示用的是同一份级数（listItems）。"""
    depths = [0, 1, 3, 8, 9, 100000, -1, -100000, 2.7, "x", None, "4"]
    got = node("layout", blocks=[{"type": "list", "units": [_list_unit(d) for d in depths]}])
    assert got == [[0, 1, 3, 8, 8, 8, 0, 0, 2, 0, 0, 4]]


def test_table_columns_without_cells_are_not_laid_out():
    """表格只排有格的列：服务端每行从第 0 列起一格不落，列号连着；伪造的列号写成十万，那一格紧接着前一列显示，不会
    被几万个空列挤到表格右边看不见的地方。列号不是非负整数的格照旧不排（判不通过，见上一条）。"""
    units = [{"loc": {"row": r, "col": c}, "segments": []}
             for r, c in [(-1, 0), (-1, 1), (0, 0), (0, 1), (0, 100000), (1, 1.5), (1, -1), (1, "x"), (1, "2")]]
    assert node("layout", blocks=[{"type": "table", "units": units}]) == [[0, 1, 2, 100000]]


def test_a_huge_column_number_neither_hides_nor_breaks_the_check():
    """列号写成 2^32：以前按列号建空列，数组长度超出上限，整段核对出错；现在那一格紧接着显示、照常核对通过。"""
    from tests.bundle_samples import FORMATS, formats_bundle

    def change(doc: dict[str, Any]) -> None:
        table = next(b for b in doc["blocks"] if b["type"] == "table")
        unit = next(u for u in table["units"] if u.get("loc") == {"row": 1, "col": 1})
        unit["loc"] = {"row": 1, "col": 2 ** 32}

    got = verdicts(rewritten(formats_bundle(text=FORMATS + "\n\n[[table:Q1 cols=region,gmv rows=0-1]]"), change))
    assert all(n["result"] == "pass" and n["onPage"] == n["text"] for n in got["numbers"]), got["numbers"]


def test_reasons_for_readers_do_not_name_implementation_details():
    """核对页是发给业务读者的文件：「无法核对显示文字」的原因说清是什么情况、该怎么办，不提 BigInt、正则、ASCII。"""
    source = VERIFIER.read_text(encoding="utf-8")
    start = source.index("const T = {")
    block = source[start:source.index("\n  }\n", start)]
    for word in ("BigInt", "Unicode", "ASCII", "正则", "重新渲染时出错"):
        assert word not in block, word
