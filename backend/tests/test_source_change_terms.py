"""变化的叫法前后端一致（总方案 X10：一种说法）。

服务端给人看的一句（source_changes.summary）用 KIND_LABEL，前端「变化」标签页、表详情的那一节用 lib/termsSource.ts 的
SOURCE_CHANGE_KIND_LABEL：两边逐字对照，一边改了另一边没跟上，同一条变化在接口和界面上就是两种说法。写法同
test_notify_events.py 对照通知事件的叫法。
"""
from __future__ import annotations

import re
from pathlib import Path

import app.db.models  # noqa: F401 - 模型都登记上（source_objects 的外键指向 data_sources）
from app.data import source_changes as sc
from app.data import source_maintenance as sm

TERMS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "termsSource.ts"


def _block(source: str, head: str) -> str:
    start = source.index(head)
    start = source.index("{", start) + 1
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
    return source[start:i - 1]


def _entries(block: str) -> dict[str, str]:
    return dict(re.findall(r"'?([a-z_]+)'?:\s*'([^']*)'", block))


def test_change_kind_labels_match_word_for_word():
    source = TERMS.read_text(encoding="utf-8")
    labels = _entries(_block(source, "export const SOURCE_CHANGE_KIND_LABEL"))
    assert labels == sc.KIND_LABEL, "「变化」标签页的叫法和服务端的对不上"
    assert set(labels) == set(sc.KINDS)


def test_maintenance_step_labels_match_word_for_word():
    source = TERMS.read_text(encoding="utf-8")
    block = _block(source, "export const MAINTENANCE_TEXT")
    steps = _entries(_block(block, "step:"))
    assert steps == sm.STEP_LABEL, "定时维护的步骤叫法和服务端的对不上（没执行的原因里用的是服务端那一份）"
