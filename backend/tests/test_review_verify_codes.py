"""跑完之后的复核层认得双盲复算、反方审稿的 warn 日志代码（engine/review.py 的 WARN_CODES）。

没登记的代码一律按 degraded、kind 记成笼统的 warn：时间线复核和问数据页说不清是哪一种缺口，模型相同这种只关乎独立性、
不影响结论的提醒也会被当成缺口去叫一次复核模型。这里守住两类：
- 模型相同（dual_check_same_model、challenge_same_model）同 judge_same_model，核查模型不在价格目录中（verify_unpriced）
  同 judge_unpriced，是配置上的提醒，不进信号；
- 触顶、不一致、无法复算、被推翻、未能反查、只查了部分数据源是缺口，按各自的 kind 记成 degraded。核查 agent 自己触顶
  （verify_limit，双盲复算、反方审稿都会发）也是：步数上限只有它说。
"""
from __future__ import annotations

import pytest

from app.core.events import EventType
from app.engine import review as rv

OK_OUTPUT = {"结论": "本周订单 120 单。"}


def warn(message: str, code: str) -> dict:
    return {"type": str(EventType.LOG), "node_id": "gate", "data": {"level": "warn", "message": message, "code": code}}


@pytest.mark.parametrize("code", ["dual_check_same_model", "challenge_same_model", "verify_unpriced"])
def test_configuration_reminders_do_not_trigger_review(code):
    """和 judge_same_model、judge_unpriced 一样：配置上的提醒，答案本身没受影响，不值得为它叫一次复核模型、
    在答案上多一句降级说明"""
    assert rv.WARN_CODES[code] == (code, rv.BENIGN)
    assert rv.scan([warn("核查模型与原流程相同（「mx-1」）", code)], OK_OUTPUT) == []


@pytest.mark.parametrize("code", [
    "dual_check_limit", "dual_check_disagreed", "dual_check_unchecked",
    "challenge_limit", "challenge_refuted", "challenge_unreviewed", "challenge_sources_lost", "verify_limit",
])
def test_verify_gaps_are_degraded_signals_of_their_own_kind(code):
    """核查没做完、没通过：答案能用，但有缺口要说清是哪一种（kind 是代码本身，不是笼统的 warn）"""
    assert rv.WARN_CODES[code] == (code, rv.DEGRADED)
    [signal] = rv.scan([warn("核查的一条提醒", code)], OK_OUTPUT)
    assert (signal.kind, signal.severity, signal.detail) == (code, rv.DEGRADED, "核查的一条提醒")
    # 缺口不是断裂：不触发「重跑一次大概率能好」
    assert not rv.should_retry([signal])
