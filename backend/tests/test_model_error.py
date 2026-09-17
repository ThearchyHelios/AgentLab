"""模型调用失败时说的话。

真实случай：供应商的模型列表里列了两个它根本不提供的 id，用户在节点上选了
其中一个，界面上抛出来的是

    OpenAIAuthenticationError: Error code: 401 - {'error': {'code':
    'invalid_model', 'message': 'The model does not exist or you do not have
    access to it.', ...}}

401 + Authentication 看着像 key 过期，实际上 key 好好的。照着原文去换 key 是
白费功夫。而且整条轨迹里没有任何地方记着试的是哪个模型——因为 llm.start 只带
message_count。
"""

from __future__ import annotations

from app.engine.nodes.llm import explain_model_error


class _FakeAuthError(Exception):
    pass


def test_invalid_model_says_model_not_key() -> None:
    exc = _FakeAuthError(
        "Error code: 401 - {'error': {'code': 'invalid_model', 'message': "
        "'The model does not exist or you do not have access to it.', "
        "'type': 'invalid_request_error'}}"
    )
    msg = explain_model_error(exc, "deepseek-chat")

    assert "deepseek-chat" in msg, "必须说清是哪个模型，否则没法排查"
    assert "不存在" in msg
    assert "设置" in msg and "供应商" in msg, "要给出下一步去哪儿改"
    # 别把用户往换 key 的方向带
    assert "401" not in msg
    assert "Authentication" not in msg


def test_quota_is_not_confused_with_missing_model() -> None:
    exc = _FakeAuthError("Error code: 429 - insufficient_quota: balance is 0")
    msg = explain_model_error(exc, "gpt-4o")
    assert "额度" in msg and "gpt-4o" in msg
    assert "不存在" not in msg


def test_unknown_error_keeps_the_original_text() -> None:
    """不认识的错误照抄原文——猜错了比不猜更糟。"""
    exc = _FakeAuthError("connection reset by peer")
    msg = explain_model_error(exc, "some-model")
    assert "some-model" in msg
    assert "connection reset by peer" in msg


def test_long_errors_are_trimmed() -> None:
    exc = _FakeAuthError("x" * 5000)
    msg = explain_model_error(exc, "m")
    assert len(msg) < 400
