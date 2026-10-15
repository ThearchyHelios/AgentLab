"""白话层（docs/design/normal-user-apps.md N10，app/plain.py）和反问收尾的服务端部分（N11、G6）。

- 覆盖：代码里出现的问题编号（工作流发布门禁 contract.* / governed.* / action.* / process.*、子工作流和口径卡固定的版本、
  应用检查 app.*、蓝图 bp.*、模板 tpl.*、个人应用 personal.*、发布单 plan.*）都要有白话。B1、B3、B4 新增的编号由各包
  自己补；这里扫的是代码里实际出现的全部编号，报出没有白话的那几个；
- 谁来改（who）：要管理员的编号 → admin；自动修复给的是 choice → you，auto / assist → assistant；应用检查只有人能处理的
  （APP_HUMAN_ONLY）→ you，其余 → assistant。白话里登记的那一档和这条规则一致（一种说法，P5）；
- 文案：白话里不写「钉住」、不裸写配置键名（check-copy 同一套规则在 scripts/check-copy.mjs 里扫）；
- 反问收尾：只问不改的一轮记进历史时，下一轮的上下文写「助手反问了什么」，不写「没有结果」；只问了一句的那一轮记的
  空图不算「上一轮的图」。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.db import models  # noqa: F401 - 建表要先登记全部模型（conftest 按 Base.metadata 建表）
from app.plain import ADMIN_CODES, PLAIN, PLAIN_PREFIX, plain_of, who_of, with_plain

APP = Path(__file__).resolve().parent.parent / "app"
#: 白话层要覆盖的编号前缀
PREFIXES = ("contract", "governed", "action", "process", "app", "bp", "tpl", "personal", "plan")
_CODE = r"((?:%s)\.[a-z_]+)" % "|".join(PREFIXES)
#: 问题编号在代码里出现的几种写法：code="x"、"code": "x"、out.error("x"、add("x"、flag 的元组写法、
#: 异常类的默认编号、模块级常量（PERSONAL_OFF = "personal.off"）
PATTERNS = [
    re.compile(r'\bcode\s*=\s*"%s"(?!\s*,\s*")' % _CODE),
    re.compile(r'"code"\s*:\s*"%s"' % _CODE),
    re.compile(r'\.(?:error|warning|info)\(\s*"%s"' % _CODE),
    re.compile(r'\badd\(\s*"%s"' % _CODE),
    re.compile(r'\bcode\s*=\s*"[^"]*",\s*"%s"' % _CODE),
    re.compile(r'\bcode\s*:\s*str\s*=\s*"%s"' % _CODE),
    re.compile(r'^[A-Z][A-Z0-9_]*\s*(?::\s*\w+\s*)?=\s*"%s"' % _CODE, re.M),
    re.compile(r'Refused\([^)]*?"%s"\s*\)' % _CODE),
]
#: 不是问题编号的同名写法：运行事件的种类（core/events.py 的 action.sent 一类）
SKIP_FILES = {APP / "core" / "events.py"}


def codes_in_code() -> dict[str, str]:
    """代码里出现的问题编号 → 第一次出现在哪个文件。"""
    out: dict[str, str] = {}
    for path in sorted(APP.rglob("*.py")):
        if path in SKIP_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in PATTERNS:
            for code in pattern.findall(text):
                out.setdefault(code, str(path.relative_to(APP)))
    return out


def test_the_scan_finds_codes_in_every_family():
    found = codes_in_code()
    for code in ("contract.required_missing", "contract.report_from_invalid", "governed.metric_untraced",
                 "action.prod_missing", "process.human_in_subgraph", "app.text_number", "app.acl_empty",
                 "app.polish_refused", "plan.graph_changed", "plan.preflight", "bp.refused"):
        assert code in found, code
    # 事件种类不是问题编号
    assert "action.sent" not in found


def test_every_problem_code_in_the_code_has_a_plain_wording():
    missing = {code: where for code, where in codes_in_code().items() if plain_of(code) is None}
    assert not missing, "这些问题编号还没有白话（app/plain.py）：" + "；".join(
        f"{code}（{where}）" for code, where in sorted(missing.items()))


def test_subgraph_and_caliber_version_codes_have_plain_wording():
    from app.apps.versions import CALIBER_RETIRED, CALIBER_UNPUBLISHED, SUBGRAPH_RETIRED, SUBGRAPH_UNPUBLISHED

    for code in (SUBGRAPH_UNPUBLISHED, SUBGRAPH_RETIRED, CALIBER_UNPUBLISHED, CALIBER_RETIRED):
        assert plain_of(code) is not None, code


#: 工作流发布门禁、应用检查、发布单的白话（包 B2 写的）：一句话、为什么要紧、怎么办三样都要有
FULL = ("contract.", "governed.", "action.", "process.", "app.", "plan.", "subgraph_version_", "caliber_version_")


def test_every_plain_wording_is_complete_and_follows_the_copy_rules():
    keys = ("report_from", "metrics_from", "caliber_from", "on_timeout", "assignee", "upgrade_policy",
            "evidence_role", "output_schema", "cite_fields")
    for code, plain in [*PLAIN.items(), *PLAIN_PREFIX.items()]:
        assert plain.title and plain.action, code
        assert plain.why or not code.startswith(FULL), code
        assert plain.who in ("you", "assistant", "admin"), code
        text = plain.title + plain.why + plain.action
        assert "钉" not in text and "跑" not in text, code
        assert not any(k in text for k in keys), code
        # 中文句子用全角标点
        assert not re.search(r"[一-鿿][,;:?!]|[,;:?!][一-鿿]", text), code


def test_who_follows_one_rule():
    from app.api.copilot_gen import APP_HUMAN_ONLY

    # 应用检查（apps/lint.py 报的）在白话里登记的那一档：只有人能处理的「你来定」，其余「助手可以修」（助手的自查修它们）
    lint = {code for code, where in codes_in_code().items() if where == "apps/lint.py"}
    for code in lint:
        assert PLAIN[code].who == ("you" if code in APP_HUMAN_ONLY else "assistant"), code
    assert who_of({"code": "app.slot_unbound"}) == "you"
    assert who_of({"code": "app.text_number"}) == "assistant"
    # 自动修复给的种类：要人选的你来定，能自动修、能交给助手的助手可以修
    assert who_of({"code": "contract.required_missing", "fix_kind": "choice"}) == "you"
    assert who_of({"code": "governed.subgraph_unpinned", "fix_kind": "auto"}) == "assistant"
    assert who_of({"code": "governed.supervisor", "fix_kind": "assist"}) == "assistant"
    # 要管理员的：端点、能力登记、受管级和降级的发布
    assert {"action.prod_missing", "action.reconcile_unplanned", "plan.admin_required", "plan.downgrade_admin"} \
        <= ADMIN_CODES
    assert who_of({"code": "action.prod_missing", "fix_kind": "assist"}) == "admin"
    # 没登记白话的：照旧显示技术说法（不补 plain）
    issue = {"code": "no.such_code", "message": "x"}
    assert with_plain(issue) is issue
    out = with_plain({"code": "subgraph_version_retired", "message": "x"})
    assert out["plain"]["title"] and out["plain"]["who"] == "you"


@pytest.fixture
async def conversation():
    from app.db.base import SessionLocal
    from app.db.models import Conversation, ConversationTurn

    async with SessionLocal() as session:
        conv = Conversation(title="反问")
        session.add(conv)
        await session.flush()
        session.add(ConversationTurn(conversation_id=conv.id, seq=1, question="订单金额", answer="", status="done",
                                     graph={"nodes": [], "edges": []},
                                     meta={"asked": {"question": "按下单时间还是付款时间算？",
                                                     "options": ["下单时间", "付款时间"]}}))
        # 问数据页的写法：meta.asked 只记 true，问题写在这一轮的答案里
        session.add(ConversationTurn(conversation_id=conv.id, seq=2, question="付款时间", status="done",
                                     answer="要不要去掉已退款的订单？", meta={"asked": True}))
        await session.commit()
        return conv.id


async def test_history_says_what_the_assistant_asked(conversation):
    from app.api.copilot import _history_section, _previous_graph
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        text = await _history_section(session, conversation)
        previous = await _previous_graph(session, conversation)
    assert "助手反问" in text and "按下单时间还是付款时间算？" in text and "下单时间、付款时间" in text
    assert "（助手反问，等用户回答：要不要去掉已退款的订单？）" in text
    assert "（没有结果）" not in text
    # 只问了一句的那一轮记的是空图：不算「上一轮的图」
    assert previous is None and "上一轮用的工作流" not in text
