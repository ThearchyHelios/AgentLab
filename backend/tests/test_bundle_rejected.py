"""可验证报告里被驳回的运行写「被驳回」，不写成手动停止的「已取消」（05-已知遗留 (a)、总方案 X9）。

人的驳回让运行结束在 cancelled + end_reason=rejected，手动停止也是 cancelled（end_reason=stopped）。封存里二者是同一种
结束事件（运行取消），分开它们的原因在那条事件的内容里，内容不随文件附带。所以：

- 运行结果照运行记录（元信息的 end_reason）写「被驳回」，核对的仍是「最后一条结束类事件是运行取消」这一半，旁边写明
  被驳回取自运行记录，不把它说成已核对出来的；
- 手动停止、老运行（没有 end_reason）照旧「已取消」；
- 文件里写着被驳回、最后一条事件却是运行结束的，照旧不通过，点名文件里写的「被驳回」。

用 node 直接载入 verifier.js（同 test_bundle_shown.py），只看运行信息（⑥）这一块。数据全是虚构的。
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import app.bundle
from tests.bundle_samples import RUN, Sample, WEEKLY, build, bundles

NODE = shutil.which("node")
VERIFIER = Path(app.bundle.__file__).parent / "verifier.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="没有 node，无法执行页面上的核对脚本")

#: 载入 verifier.js（node 里没有 document，它把核对用的纯函数交给 module.exports），交回运行信息每一项
HARNESS = r"""
const fs = require('fs')
const { createHash } = require('crypto')
const input = JSON.parse(fs.readFileSync(0, 'utf8'))
const mod = { exports: {} }
new Function('module', fs.readFileSync(process.argv[1], 'utf8'))(mod)
const digest = async (bytes) => createHash('sha256').update(bytes).digest('hex')
mod.exports.verify(input.data, digest, input.data.meta.manifest_hash).then((out) => {
  process.stdout.write(JSON.stringify(out.meta.map((m) => ({ field: m.field, shown: m.shown, claim: m.claim,
    result: m.result, reason: m.reason || '', mark: m.mark ?? null }))))
}, (e) => { console.error((e && e.stack) || String(e)); process.exit(1) })
"""


def status_of(bundle: dict[str, Any]) -> dict[str, Any]:
    done = subprocess.run([NODE, "-e", HARNESS, str(VERIFIER)], input=json.dumps({"data": bundle}),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return next(m for m in json.loads(done.stdout) if m["field"] == "status")


def ended(end_reason: str | None) -> dict[str, Any]:
    """同一串事件（最后一条是运行取消），运行记录分别写被驳回、手动停止、没写（老运行）。"""
    s = Sample()
    events, bound = s.events({"write": s.doc(WEEKLY, "write")}, ending="rejected")
    return build(s, events, bound, run={**RUN, "status": "cancelled", "end_reason": end_reason})


def test_a_rejected_run_reads_rejected_and_says_which_half_is_checked():
    got = status_of(bundles()["rejected"])
    assert got["result"] == "pass" and got["shown"] == "被驳回" and got["claim"] == "被驳回", got
    assert "运行取消" in got["reason"] and "取自运行记录" in got["reason"], got["reason"]
    # 旁边那一句不说成全核对过了
    assert got["mark"] == "已核对运行取消，被驳回取自运行记录", got


@pytest.mark.parametrize("end_reason", ["stopped", None])
def test_a_stopped_or_old_cancelled_run_still_reads_cancelled(end_reason):
    got = status_of(ended(end_reason))
    assert (got["result"], got["shown"], got["claim"], got["mark"]) == ("pass", "已取消", "已取消", None), got


def test_a_file_claiming_rejected_over_a_finished_run_fails_and_names_the_claim():
    forged = copy.deepcopy(bundles()["base"])
    forged["meta"].update(status="cancelled", end_reason="rejected")
    got = status_of(forged)
    assert got["result"] == "fail" and got["shown"] == "已完成" and "被驳回" in got["reason"], got
