"""可验证报告核对正文显示的字（bundle/verifier.js 的显示规则）。

以前页面上 ⑤ 只核「引用的值 = 口径卡的值或快照的格子」，不核正文里显示的字：只改显示文字（cite.value 不动）、重算
全部哈希的伪造页照样全部通过。现在浏览器按服务端的显示规则（engine/evidence.py 的 render_number / render_cell）把那个
值渲染一遍，和正文显示的字逐字比对（只去掉首尾空白）。这里用 node 直接载入 verifier.js（没有页面时它交出核对用的纯函数）：

- 移植的渲染和服务端逐条对得上：千分位、不分组、小数位（含负数位）、比率显示成百分数、万 / 亿换算、pct、int、.N、
  货币等单位后缀、负数、0、超过 2^53 的整数（按 JSON 原文，不经 double）、DECIMAL 落成的数字文本、文字压成一行、竖线、
  日期时间按文档记下的显示规则版本；服务端渲染不出来的，浏览器也不给出一个字。本页没有移植的写法（对象、列表、非 ASCII
  数字）一律渲染不出来，判无法核对，不会给出一串不一样的字；
- 整份核对（verify）：compose_doc 真实组装的报告原样通过；只改一个数字或取值的显示文字、重算全部哈希的伪造页，
  ①–④ 照样对得上，⑤ 里那一项不通过，原因点名「正文显示「X」，引用的值是「Y」」；认不出的换算判无法核对，不判通过。

数据全是虚构的。
"""
from __future__ import annotations

import base64
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
  const digest = async (bytes) => createHash('sha256').update(bytes).digest('hex')
  const out = await v.verify(input.data, digest, input.seal)
  const results = (items) => items.map((i) => i.result)
  return {
    events: results(out.events), manifest: results(out.manifest), artifacts: results(out.artifacts),
    chain: results(out.chain), meta: results(out.meta),
    numbers: out.numbers.map((n) => ({ key: n.key, ref: n.seg.ref ?? null, text: String(n.seg.text ?? ''),
      result: n.result, reason: n.reason || '', expected: n.expected ?? null })),
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
UNITS = ["", "元", "美元", "%", "％", "人次"]


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
    用规范 JSON：① 到 ④ 都对得上，伪造只可能在 ⑤ 露馅。"""
    raw = canonical_json(doc).encode()
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


def forged(ref: str, text: str, **cite: Any) -> dict[str, Any]:
    """经营月报样本里一个片段的显示文字改成 text（cite.value 不动，cite.rendered 跟着改，cite 另改 cite 里给的几项），
    重算全部哈希。"""
    from tests.bundle_samples import formats_bundle

    bundle = formats_bundle()
    art, doc = doc_of(bundle)
    seg = segment(doc, ref)
    seg["text"] = text
    seg["cite"].update(rendered=text, **cite)
    return reseal(bundle, art, doc)


def verdicts(bundle: dict[str, Any]) -> dict[str, Any]:
    return node("verify", data=bundle, seal=bundle["meta"]["manifest_hash"])


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
    assert all(n["result"] == "na" and n["reason"] == "无法核对显示文字：这个浏览器不支持按系统的显示规则重新渲染（缺少 BigInt 或 "
               "Unicode 正则）" for n in got["numbers"] if n["ref"] not in texts)


def test_every_sample_page_still_passes_its_display_check():
    """check-bundle 的样本页（tests/bundle_samples.py）原样导出：每个核对得到出处的数字和取值，显示文字都核对通过；
    没有一项因为显示文字不通过或核对不了。"""
    from tests.bundle_samples import bundles

    for name, bundle in bundles().items():
        got = verdicts(bundle)
        assert not [n for n in got["numbers"] if n["reason"].startswith(("正文显示", "无法核对显示文字"))], name
        assert all(n["expected"] == n["text"] for n in got["numbers"] if n["result"] == "pass"), name
