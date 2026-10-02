"""AI 兜底起草（期 2，WP-4）：规则起草不完整时，经用户同意把表格**结构**发给模型，由模型写配方。

**模型只写规则、不碰数据**（用户决定 B）。发出去的是压缩表示（compress）：结构文字照发，数字格只给类型、
公式先脱敏、隐藏行列只给个数、列表数据列只给画像，任何一个数据值都不出门。模型写回来的配方走和规则草稿
同一条路：静态校验（注入的 validate，origin="ai"）、执行器干跑（注入的 dry_run）、交给用户逐条确认，
必须人工确认之后才能启用。每月重传按已确认的配方重放，不再调用模型。

**为什么不用 get_chat_model。** 它在没有接入时会静默退回演示模型；拿演示模型起草等于假装起草了。这里
自己按助手的模型设置解析接入，没有、停用、只有演示接入、缺 key 都在调用模型之前判定为不可用。

**调用参数照 c837cdb 修过的问题来**：关思考（thinking="off"）、额度 32768、服务方嫌额度大时退回 8192；
正文一律经 message_text 抽取（回复可能是块列表）；输出被截断单独记一种失败，不当成「不是 JSON」去修订。

写给模型的文字（系统提示、提示词、压缩表示里的固定片段、回灌的问题模板）都放在名字含 _SYSTEM / _PROMPT
的模块级常量里，拼接只用 .format()。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import recipe_parsers as P
from app.data import recipe_suggest as S
from app.data.names import canon, to_sql_name
from app.data.recipe_suggest import AsyncDryRunFn, DryRunFn, ValidateFn
from app.data.recipe_types import (
    AiAvailability,
    AiUsage,
    Card,
    Draft,
    DraftFacts,
    Grid,
    GridCell,
    Recipe,
)
from app.data.tabular import EXCEL_ERRORS
from app.data.xlsx_scan import WorkbookScan, cell_ref, col_letter

logger = logging.getLogger(__name__)


class AiUnavailable(RuntimeError):
    """没有可用的模型（没有接入、停用、只有演示接入、缺 key）。message 给人看；在调用模型之前判定。"""


class AiTooLarge(ValueError):
    """压缩表示超过字数上限。同样在调用模型之前判定（接口 413 ai_too_large）。"""


# 没有 AiFailed：调用之后的任何失败都体现在返回值里（draft=None、error、usage 照记）

#: 助手的模型设置键（与 app/api/copilot.py 的 COPILOT_SETTING_KEY 同值；数据层不导入接口层，测试断言两者相等）
COPILOT_SETTING_KEY = "copilot"
#: 1 次起草 + 最多 2 次修订
AI_MAX_ATTEMPTS = 3
#: 单次调用的超时（秒）；测试 monkeypatch 调小
AI_TIMEOUT_S = 90
#: 输出额度（与 copilot.COPILOT_MAX_TOKENS 同值）：会思考的模型的思考也算在里面，8192 时常常想完就没额度写正文
AI_MAX_TOKENS = 32768
#: 服务方嫌额度太大时退回这个（同 copilot._FALLBACK_MAX_TOKENS）
AI_FALLBACK_MAX_TOKENS = 8192
COMPRESS_MAX_CHARS_PER_SHEET = 12_000
COMPRESS_MAX_CHARS = 20_000
#: 行数超过这个数的工作表，只给前 COMPRESS_HEAD_ROWS 行、后 COMPRESS_TAIL_ROWS 行，中间折成列画像
COMPRESS_FOLD_ROWS = 300
COMPRESS_HEAD_ROWS = 200
COMPRESS_TAIL_ROWS = 20
#: 结构文字超过这个字数就截断
COMPRESS_TEXT_MAX = 40
#: 同模板的连续格至少几格折成「序列」；序列不超过这个数时把每一格都列出来
SEQ_MIN, SEQ_LIST_MAX = 3, 12
#: 服务方嫌额度太大时报错里会出现的字样（同 copilot._BUDGET_WORDS）
_BUDGET_WORDS = ("max_tokens", "max_completion_tokens", "max_output_tokens", "maximum context length")


@dataclass
class Compressed:
    """压缩表示。text 就是提示词的前三段（工作表、系统发现、分段标题候选词），ai_draft 只用 text：
    接口层按这四个字段存库、取回再传进来，发出去的和预览给用户看的逐字相同。"""

    text: str
    chars: int
    truncated: bool
    #: text 的 sha256：同意记录和清单里存它，事后能核对发出去的是哪一份
    sha256: str


@dataclass
class AiDraftResult:
    #: origin="ai"；None 表示没有得到静态校验通过的配方
    draft: Draft | None
    usage: list[AiUsage]
    attempts: int
    #: 给人看的失败原因：「AI 未能起草出合法的配方…」「输出被截断…」「调用超时…」
    error: str | None
    compressed_chars: int
    compressed_sha256: str


# ==========================================================================
# 写给模型的文字（check-copy 按常量名排除）
# ==========================================================================

RECIPE_DRAFT_SYSTEM = (
    "你为 Excel 工作表起草「取数配方」：一个符合下面 JSON Schema 的 JSON 对象。系统会按配方确定性地执行、"
    "用 SQL 核对，再交给用户逐条确认；你只写规则，不接触数据。\n"
    "规则：\n"
    "1. 只输出一个 JSON 对象：不要解释，不要代码块标记，不要在思考里草拟 JSON。\n"
    "2. 配方语言是封闭的：没有正则，不写单元格坐标；只能用 Schema 里有的字段和取值。\n"
    "3. measures 的键必须与 labels.expect 里的标签逐字相同、一一对应。\n"
    "4. const 的 pick 只能从「分段标题候选词」里选。\n"
    "5. 占位符只能表示空值（无数据或不适用）。\n"
    "6. 单位只能取自「单位词表」；标签或表头末尾括号里的单位拆进 tables[].units，列名去掉单位，"
    "单位必须与括号里写的一致；括号里的单位不在词表里时，列名保留括号、不写单位。\n"
    "7. 合计、小计行用 role=derived 核对，并用 keep_as 另存成一张表。\n"
    "8. 「系统发现」的每一条必须被 relations 恰好认领一次：成立写 sum_eq，口径不同写 not_comparable，"
    "判断为巧合写 dismissed 并给理由；认领的成员必须正是该发现里的那几行，分段 id 必须与发现里写的一致。\n"
    "9. 分段 id、块 id 在整份配方内不能重复。\n"
    "10. 不写年份、日期和表格里的任何数值；tables[].note 留空。\n"
    "11. 表名、列名用简短的中文。"
)

#: 前三段：就是压缩表示（Compressed.text），预览里展示的也是这一段
RECIPE_SECTIONS_PROMPT = "# 工作表压缩表示\n{sheets}\n\n# 系统发现（F1…）\n{facts}\n\n# 分段标题候选词\n{candidates}"

#: 后三段：单位词表、规则起草的半成品与失败原因、配方 JSON Schema
RECIPE_REQUEST_PROMPT = (
    "# 单位词表\n{units}\n\n"
    "# 规则起草的半成品与失败原因\n{rules_draft}\n\n"
    "# 配方 JSON Schema\n{schema}\n\n"
    "请输出配方 JSON。"
)

#: 完整的首次提示词（.format(sheets=, facts=, candidates=, units=, rules_draft=, schema=)）。ai_draft 发的是
#: Compressed.text + 「\n\n」+ RECIPE_REQUEST_PROMPT，与它逐字相同，只是前三段直接取压缩表示、不再重算
RECIPE_DRAFT_PROMPT = RECIPE_SECTIONS_PROMPT + "\n\n" + RECIPE_REQUEST_PROMPT

RECIPE_REPAIR_PROMPT = (
    "上一次的配方有以下问题，请输出修正后的完整配方 JSON（只输出一个 JSON 对象）：\n{problems}"
)

#: 规则草稿那一段、回灌的问题行
RULES_DRAFT_PROMPT = {
    "none": "无",
    "recipe": "半成品配方：\n{recipe}",
    "failures": "失败原因：\n{lines}",
    "line": "- {text}",
    "not_json": "上一次的回复不是合法的 JSON 对象（{detail}）",
}

#: 压缩表示里的固定片段
COMPRESS_PROMPT_LABELS = {
    "sheet_head": "## 工作表「{name}」 已用区域 {area}，非空格 {count}",
    "sheet_cut": "（起草时只读取了前 {n} 行）",
    "merges": "合并：{items}",
    "none": "无",
    "texts": "文字：",
    "text_item": "  {ref}「{text}」",
    "long_text": "{head}…（共 {n} 字）",
    "seq_all": "  {ref} 序列「{tpl}」{n} 格：{items}",
    "seq_ends": "  {ref} 序列「{tpl}」{n} 格：{first} … {last}",
    "repeat": "  {ref}「{text}」×{n}",
    "numeric_text": "<文本数字>",
    "other_text": "  {ref} 文字（{n} 字）",
    "profile": "  {col} 列「{header}」是数据列（只给画像）：{n} 格，不同取值 {distinct} 个，最长 {longest} 字，空值 {blank}%",
    "masked": "  {col} 列「{header}」是遮罩列（只给画像）：{n} 格，不同取值 {distinct} 个，最长 {longest} 字，空值 {blank}%",
    "numbers": "数字（只有类型）：",
    "rect": "  {ref} {sig}",
    "int": "整数",
    "float": "小数",
    "date": "日期",
    "time": "时间",
    "bool": "布尔",
    "error": "错误值",
    "text_formula": "文字",
    "formula": "，公式",
    "uncached": "，无保存值",
    "formulas": "公式形状：",
    "shape_one": "  {ref} {formula}",
    "shape_many": "  {ref} {formula} {how}（{n} 格，{cache}）",
    "by_col": "逐列平移",
    "by_row": "逐行平移",
    "by_cell": "逐格平移",
    "all_cached": "均有保存值",
    "some_uncached": "{n} 格无保存值",
    "hidden": "隐藏行：{rows}  隐藏列：{cols}",
    "hidden_rows": "第 {span} 行（文字 {t} 格、数字 {n} 格）",
    "hidden_cols": "{span} 列（文字 {t} 格、数字 {n} 格）",
    "fold": "第 {a}–{b} 行（共 {n} 行）折叠为列画像：",
    "fold_col": "  {col} 列：{kinds}，空值 {blank}%，不同取值 {distinct} 个",
    "kind_count": "{kind} {n}",
    "hidden_sheets": "另有隐藏工作表 {n} 个（不发送内容）",
    "facts_none": "无",
    "fact_sum": "{id}（等式：用 sum_eq 认领，或判断为巧合写 dismissed）：{text}；分段 id「{segment}」，合计行「{total}」，组成行{parts}",
    "fact_ne": "{id}（口径不同：用 not_comparable 认领，或判断为巧合写 dismissed）：{text}；明细分段 id {a}，对比分段 id「{b_segment}」的「{b}」",
    "quoted": "「{text}」",
    "candidates_none": "无",
    "candidate": "「{title}」：{words}",
    "hidden_text": "<隐藏>",
}

_L = COMPRESS_PROMPT_LABELS

#: 给人看的失败原因。只说出了什么事：下一步取决于暂存区有没有工作配方（没有时配方面板里只能粘贴一份），
#: 由接口层（recipe_imports）补上
_ERR_INVALID = "AI 未能起草出合法的配方"
_ERR_TRUNCATED = "AI 的输出被截断，配方没有写完（输出被截断）"
_ERR_TIMEOUT = "调用超时：{n} 秒内没有收到 AI 的回复"
_ERR_CALL = "调用失败：{reason}"


# ==========================================================================
# 用哪个模型
# ==========================================================================


def _budget_rejected(e: BaseException) -> bool:
    """服务方一开口就嫌额度太大（400 / 422，报错里带 max_tokens 等字样）。写法同 copilot._budget_rejected。"""
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    return status in (400, 422) and any(w in str(e).lower() for w in _BUDGET_WORDS)


class _BudgetFallback:
    """先按大额度调；服务方嫌额度太大时按小额度重来，之后的调用（修订）都用小的。同 copilot._BudgetFallback，
    只是这里是一次性的 ainvoke，不是流式。"""

    def __init__(self, big: Any, small: Any, model_name: str = "") -> None:
        self.model, self._small = big, small
        #: 模型 id：ai_draft(model=…) 拿它记用量。不给的话 getattr(model, "model") 拿到的是底层的模型对象
        self.model_name = model_name

    @property
    def max_tokens(self) -> Any:
        return getattr(self.model, "max_tokens", None)

    async def ainvoke(self, messages: list[Any]) -> Any:
        try:
            return await self.model.ainvoke(messages)
        except Exception as e:
            if self.model is self._small or not _budget_rejected(e):
                raise
            self.model = self._small
        return await self.model.ainvoke(messages)


def _unconfigured(e: BaseException) -> str:
    """写法照 copilot._unconfigured。"""
    from app.core.errors import not_configured

    return f"助手使用的模型尚未配置完成：{not_configured(e, with_hint=False)}。请到「设置 → 模型接入」检查"


async def resolve_draft_model(session: AsyncSession) -> tuple[Any | None, str, AiAvailability]:
    """(可调用的模型, 模型 id, 可用性)；不可用时模型为 None。只建模型对象，不发请求。"""
    from app.db.models import Setting
    from app.providers.factory import ModelSpec, ProviderNotConfigured, build_chat_model, resolve_provider

    row = await session.get(Setting, COPILOT_SETTING_KEY)
    saved = (row.value if row else None) or {}
    spec = ModelSpec(provider=saved.get("provider") or None, model=saved.get("model") or None,
                     max_tokens=AI_MAX_TOKENS, temperature=0, thinking="off")
    try:
        provider = await resolve_provider(session, spec.provider, spec.model)
    except ProviderNotConfigured as e:
        # 只存了模型名、提供它的接入已停用：resolve_provider 自己会报出来
        return None, "", AiAvailability(False, reason=_unconfigured(e))
    if provider is None:
        if spec.provider:
            reason = f"助手设置里的模型接入「{spec.provider}」不存在。请到「设置 → 模型接入」检查"
        else:
            reason = "未配置模型接入，无法使用 AI 起草。请到「设置 → 模型接入」添加"
        return None, "", AiAvailability(False, reason=reason)
    if provider.kind == "mock":
        return None, "", AiAvailability(False, reason="当前只有演示用的模型接入，无法用于 AI 起草。"
                                                      "请到「设置 → 模型接入」添加真实的模型接入")
    if not provider.enabled:
        # 存的是接入名时 resolve_provider 会把停用的接入原样返回，build_chat_model 也不查 enabled
        return None, "", AiAvailability(False, reason=f"助手使用的模型接入「{provider.name}」已停用。"
                                                      "请到「设置 → 模型接入」启用它，或为助手换一个模型")
    try:
        big = build_chat_model(provider, spec)
        small = build_chat_model(provider, spec.model_copy(update={"max_tokens": AI_FALLBACK_MAX_TOKENS}))
    except ProviderNotConfigured as e:
        return None, "", AiAvailability(False, reason=_unconfigured(e))
    model_id = spec.model or provider.default_model or ""
    return (_BudgetFallback(big, small, model_id), model_id,
            AiAvailability(True, model=model_id, provider=provider.name))


async def ai_availability(session: AsyncSession) -> AiAvailability:
    return (await resolve_draft_model(session))[2]


# ==========================================================================
# 压缩表示
# ==========================================================================

_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def _tpl(text: str) -> str:
    return _DIGITS.sub("{}", text)


def _rng(r1: int, c1: int, r2: int, c2: int) -> str:
    a, b = cell_ref(r1, c1), cell_ref(r2, c2)
    return a if a == b else f"{a}:{b}"


def _flat(text: str) -> str:
    return _WS.sub(" ", text.strip())


def _clip(text: str) -> str:
    flat = _flat(text)
    n = len(canon(flat))
    if n <= COMPRESS_TEXT_MAX:
        return flat
    return _L["long_text"].format(head=flat[:20], n=n)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _value_sig(cell: GridCell) -> str | None:
    """数字格、日期格、布尔、公式格的类型签名；文字格 None。"""
    v = cell.value
    formula = cell.formula is not None
    if formula and v is None:
        return _L["formula"].lstrip("，") + _L["uncached"]
    if isinstance(v, bool):
        base = _L["bool"]
    elif _num(v):
        base = _L["int"] if isinstance(v, int) or float(v).is_integer() else _L["float"]
    elif isinstance(v, (_dt.datetime, _dt.date)):
        base = _L["date"]
    elif isinstance(v, _dt.time):
        base = _L["time"]
    elif isinstance(v, str) and v.strip().upper() in EXCEL_ERRORS:
        base = _L["error"]
    elif formula:
        base = _L["text_formula"]
    else:
        return None
    return base + (_L["formula"] if formula else "")


def _rects(cells: dict[tuple[int, int], str]) -> list[tuple[int, int, int, int, str]]:
    """同签名的格合成矩形：先按行成段，再把上下相同的段接起来。"""
    by_row: dict[int, list[int]] = {}
    for (r, c) in cells:
        by_row.setdefault(r, []).append(c)
    segs: dict[int, list[tuple[int, int, str]]] = {}
    for r in sorted(by_row):
        cols = sorted(by_row[r])
        start = prev = cols[0]
        for c in cols[1:] + [None]:  # type: ignore[list-item]
            if c is not None and c == prev + 1 and cells[(r, c)] == cells[(r, start)]:
                prev = c
                continue
            segs.setdefault(r, []).append((start, prev, cells[(r, start)]))
            if c is not None:
                start = prev = c
    rects: list[list[Any]] = []
    open_: dict[tuple[int, int, str], list[Any]] = {}
    for r in sorted(segs):
        nxt: dict[tuple[int, int, str], list[Any]] = {}
        for c0, c1, sig in segs[r]:
            key = (c0, c1, sig)
            if key in open_ and open_[key][2] == r - 1:
                open_[key][2] = r
                nxt[key] = open_[key]
            else:
                rect = [r, c0, r, c1, sig]
                rects.append(rect)
                nxt[key] = rect
        open_ = nxt
    return sorted((tuple(x) for x in rects), key=lambda t: (t[0], t[1]))  # type: ignore[misc]


def sanitize_formula(formula: str) -> str:
    """公式脱敏：数字常量 → <数>，字符串常量 → <文本>，跨工作表的引用只留表名（'内部'!<格>）。

    「=3000+2500」这种把数写成算式的格本身就是数据值；「=C5*1.0675」里的税率、「=IF(B5>50000,…)」里的阈值
    是业务数据，字符串常量同理。同表的单元格引用、区域、函数名、运算符照留：模型要靠它们认出合计。
    """
    from openpyxl.formula.tokenizer import Token, Tokenizer

    try:
        tok = Tokenizer(formula if formula.startswith("=") else "=" + formula)
    except Exception:  # noqa: BLE001 - 切不开的公式整条不发
        return "=<公式>"
    out: list[str] = []
    for t in tok.items:
        if t.type == Token.OPERAND and t.subtype == Token.NUMBER:
            out.append("<数>")
        elif t.type == Token.OPERAND and t.subtype == Token.TEXT:
            out.append("<文本>")
        elif t.type == Token.OPERAND and t.subtype == Token.RANGE and "!" in t.value:
            out.append(t.value.rsplit("!", 1)[0] + "!<格>")
        else:
            out.append(t.value)
    return "=" + "".join(out)


def _translate(formula: str, origin: str, dest: str) -> str | None:
    from openpyxl.formula.translate import Translator

    try:
        return Translator(formula, origin=origin).translate_formula(dest)
    except Exception:  # noqa: BLE001
        return None


def _formula_shapes(grid: Grid, cells: list[tuple[int, int]]) -> list[str]:
    """公式形状：同一行里逐列平移的折成一段，上下相同的段再接成矩形（Translator 判平移，比较的是原文）。"""
    by_row: dict[int, list[int]] = {}
    for (r, c) in cells:
        by_row.setdefault(r, []).append(c)
    runs: list[list[int]] = []        # [r, c0, c1]
    for r in sorted(by_row):
        cols = sorted(by_row[r])
        cur = [r, cols[0], cols[0]]
        for c in cols[1:]:
            first = grid.get(r, cur[1])
            if (c == cur[2] + 1 and first is not None and first.formula
                    and _translate(first.formula, cell_ref(r, cur[1]), cell_ref(r, c)) == grid.get(r, c).formula):  # type: ignore[union-attr]
                cur[2] = c
            else:
                runs.append(cur)
                cur = [r, c, c]
        runs.append(cur)
    rects: list[list[int]] = []       # [r0, c0, r1, c1]
    for r, c0, c1 in runs:
        prev = next((x for x in rects if x[2] == r - 1 and x[1] == c0 and x[3] == c1), None)
        top = grid.get(prev[0], c0) if prev else None
        cur = grid.get(r, c0)
        if (prev is not None and top is not None and cur is not None and top.formula
                and _translate(top.formula, cell_ref(prev[0], c0), cell_ref(r, c0)) == cur.formula):
            prev[2] = r
        else:
            rects.append([r, c0, r, c1])
    lines: list[str] = []
    for r0, c0, r1, c1 in sorted(rects):
        first = grid.get(r0, c0)
        formula = sanitize_formula(first.formula or "") if first is not None else "=<公式>"
        n = (r1 - r0 + 1) * (c1 - c0 + 1)
        if n == 1:
            lines.append(_L["shape_one"].format(ref=cell_ref(r0, c0), formula=formula))
            continue
        how = _L["by_cell"] if r1 > r0 and c1 > c0 else (_L["by_row"] if r1 > r0 else _L["by_col"])
        uncached = sum(1 for rr in range(r0, r1 + 1) for cc in range(c0, c1 + 1)
                       if (x := grid.get(rr, cc)) is not None and x.value is None)
        cache = _L["all_cached"] if not uncached else _L["some_uncached"].format(n=uncached)
        lines.append(_L["shape_many"].format(ref=_rng(r0, c0, r1, c1), formula=formula, how=how, n=n, cache=cache))
    return lines


def _spans(nums: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in sorted(set(nums)):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def _fold_items(items: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """结构文字：同值的连续格折成「×N」，同模板的连续格折成「序列」（横向优先，再纵向）。"""
    texts = {(r, c): t for r, c, t in items}
    used: set[tuple[int, int]] = set()
    out: list[tuple[int, int, str]] = []
    for horizontal in (True, False):
        keys = sorted(texts, key=(lambda k: (k[0], k[1])) if horizontal else (lambda k: (k[1], k[0])))
        i = 0
        while i < len(keys):
            k = keys[i]
            if k in used:
                i += 1
                continue
            run = [k]
            j = i + 1
            while j < len(keys):
                nk, pk = keys[j], run[-1]
                adj = (nk[0] == pk[0] and nk[1] == pk[1] + 1) if horizontal else (nk[1] == pk[1] and nk[0] == pk[0] + 1)
                if not adj or nk in used or _tpl(texts[nk]) != _tpl(texts[run[0]]):
                    break
                run.append(nk)
                j += 1
            vals = [texts[x] for x in run]
            ref = _rng(run[0][0], run[0][1], run[-1][0], run[-1][1])
            if len(run) >= 2 and len(set(vals)) == 1:
                out.append((run[0][0], run[0][1], _L["repeat"].format(ref=ref, text=vals[0], n=len(run))))
            elif len(run) >= SEQ_MIN and "{}" in _tpl(vals[0]):
                if len(run) <= SEQ_LIST_MAX:
                    line = _L["seq_all"].format(ref=ref, tpl=_tpl(vals[0]), n=len(run), items="、".join(vals))
                else:
                    line = _L["seq_ends"].format(ref=ref, tpl=_tpl(vals[0]), n=len(run), first=vals[0], last=vals[-1])
                out.append((run[0][0], run[0][1], line))
            else:
                i += 1
                continue
            used.update(run)
            i = j
    for (r, c), t in texts.items():
        if (r, c) not in used:
            out.append((r, c, _L["text_item"].format(ref=cell_ref(r, c), text=t)))
    return out


def _structural_parse(v: Any) -> bool:
    return any(f(v) is not None for f in (P.hour_range, P.hour_range_total, P.month_day_or_date,
                                          P.cn_date_range, P.cn_year_month))


def _profile(cells: list[tuple[int, str]], span: tuple[int, int]) -> tuple[int, int, int, int]:
    """(个数, 不同取值个数, 最长字数, 空值百分比)。"""
    n = len(cells)
    distinct = len({canon(t) for _, t in cells})
    longest = max((len(canon(t)) for _, t in cells), default=0)
    rows = span[1] - span[0] + 1
    blank = round(100 * (rows - n) / rows) if rows > 0 else 0
    return n, distinct, longest, max(blank, 0)


def _compress_sheet(grid: Grid, scan: WorkbookScan, mask_keys: set[str]) -> tuple[str, bool, set[str]]:
    """一张工作表的压缩表示。返回 (文本, 是否截断/折叠, 这张表上被隐藏或遮罩的文字)。"""
    name = grid.sheet
    sheet_scan = scan.sheet(name)
    hints = S.layout_hints(grid)
    rows_all = grid.row_numbers()
    hidden_r, hidden_c = set(grid.hidden_rows), set(grid.hidden_cols)
    secret: set[str] = set()
    lines: list[str] = []
    if grid.bounds is not None:
        r1, c1, r2, c2 = grid.bounds
    elif rows_all:
        r1, r2 = rows_all[0], rows_all[-1]
        c1 = min(c for (_, c) in grid.cells)
        c2 = max(c for (_, c) in grid.cells)
    else:
        r1 = c1 = r2 = c2 = 1
    count = sheet_scan.nonempty if sheet_scan is not None else len(grid.cells)
    lines.append(_L["sheet_head"].format(name=name, area=_rng(r1, c1, r2, c2), count=count))
    truncated = grid.truncated
    if grid.truncated:
        lines.append(_L["sheet_cut"].format(n=rows_all[-1] if rows_all else 0))

    # 大表：中间的行折成列画像
    folded: set[int] = set()
    fold_range: tuple[int, int] | None = None
    if rows_all and r2 - r1 + 1 > COMPRESS_FOLD_ROWS:
        a, b = r1 + COMPRESS_HEAD_ROWS, r2 - COMPRESS_TAIL_ROWS
        folded = {r for r in rows_all if a <= r <= b}
        fold_range = (a, b)
        truncated = True

    merges = [m for m in grid.merges if not all(r in hidden_r for r in range(m[0], m[2] + 1))]
    lines.append(_L["merges"].format(items="、".join(_rng(*m) for m in sorted(merges)) or _L["none"]))

    # 遮罩列：表头格之下的整列只给画像
    masked: dict[int, tuple[int, str]] = {}
    if mask_keys:
        for (r, c), cell in sorted(grid.cells.items()):
            if isinstance(cell.value, str) and P.match_key(cell.value) in mask_keys and c not in masked:
                masked[c] = (r, _flat(cell.value))

    axis_row, label_col = hints.axis_row, hints.label_col
    header_rows = hints.header_rows
    top_header = hints.top_header_row

    def positional(r: int, c: int) -> bool:
        if axis_row is not None:
            return c == label_col or r <= axis_row
        if top_header is not None:
            return r in header_rows or r < top_header
        return False

    numbers: dict[tuple[int, int], str] = {}
    formulas: list[tuple[int, int]] = []
    structural: list[tuple[int, int, str]] = []
    numeric_like: list[tuple[int, int, str]] = []
    others: list[tuple[int, int, str]] = []
    data_candidates: dict[int, list[tuple[int, str]]] = {}
    masked_cells: dict[int, list[tuple[int, str]]] = {}
    hidden_rows_count: dict[int, list[int]] = {}
    hidden_cols_count: dict[int, list[int]] = {}

    #: 每行最左的可见非空格所在的列：列表合计行的标签（「合计」）在这一列
    leftmost: dict[int, int] = {}
    for (r, c) in grid.cells:
        if r not in hidden_r and c not in hidden_c and (r not in leftmost or c < leftmost[r]):
            leftmost[r] = c

    for (r, c), cell in sorted(grid.cells.items()):
        if r in hidden_r or c in hidden_c:
            is_text = isinstance(cell.value, str) and cell.formula is None
            bucket = hidden_rows_count if r in hidden_r else hidden_cols_count
            key = r if r in hidden_r else c
            pair = bucket.setdefault(key, [0, 0])
            pair[0 if is_text else 1] += 1
            if isinstance(cell.value, str):
                secret.add(_flat(cell.value))
            continue
        if r in folded:
            continue
        sig = _value_sig(cell)
        if sig is not None:
            # 日期格（包括日期轴上的）只给类型签名、不给值（P2-SPEC 6.2）：表格里没有统计期格时，轴上的
            # 年月日就是这一期的数据；模型从「C4:AG4 日期」已经看得出这一行是日期表头
            numbers[(r, c)] = sig
            if cell.formula is not None:
                formulas.append((r, c))
            continue
        text = cell.value if isinstance(cell.value, str) else str(cell.value)
        if c in masked and r > masked[c][0]:
            masked_cells.setdefault(c, []).append((r, text))
            secret.add(_flat(text))
            continue
        if P.looks_numeric_text(text):
            numeric_like.append((r, c, _L["text_item"].format(ref=cell_ref(r, c), text=_L["numeric_text"])))
            secret.add(_flat(text))
            continue
        if c in masked and r == masked[c][0]:
            continue                      # 遮罩列的表头放在画像那一行里
        # 合计字样只在结构位置或一行最左的格（列表合计行的标签）里照发：数据列中间以「合计」「累计」开头的格
        # （「累计消费12000元」）是数据，和同列其他格一样只进画像（SE-3）
        if (positional(r, c) or (S._total_word(text) and leftmost.get(r) == c)
                or S._placeholder_like(text)):
            structural.append((r, c, _clip(text)))
            continue
        if axis_row is None:
            data_candidates.setdefault(c, []).append((r, text))
            continue
        if _structural_parse(text):
            structural.append((r, c, _clip(text)))
        else:
            others.append((r, c, _L["other_text"].format(ref=cell_ref(r, c), n=len(canon(text)))))
            secret.add(_flat(text))

    # 没有日期轴的工作表：同一列纵向连续 ≥4 个文字格（至多隔一个空行）、所在行还有别的非空格的，是数据列
    profiles: list[tuple[int, int, str]] = []
    row_count = {r: len(grid.row(r)) for r in rows_all}
    for c, items in sorted(data_candidates.items()):
        items.sort()
        runs: list[list[tuple[int, str]]] = []
        for r, t in items:
            if runs and r - runs[-1][-1][0] <= 2:
                runs[-1].append((r, t))
            else:
                runs.append([(r, t)])
        data: list[tuple[int, str]] = []
        span: list[int] = []
        for run in runs:
            # 冒号结尾的是表单的标签（「项目名称：」），是结构文字；它右边那一列才是要藏起来的内容
            labels = sum(1 for _, t in run if canon(t).endswith(":")) * 2 >= len(run)
            if len(run) >= 4 and not labels and all(row_count.get(r, 0) >= 2 for r, _ in run):
                data.extend(run)
                span += [run[0][0], run[-1][0]]
            else:
                for r, t in run:
                    if labels or _structural_parse(t):
                        structural.append((r, c, _clip(t)))
                    else:
                        others.append((r, c, _L["other_text"].format(ref=cell_ref(r, c), n=len(canon(t)))))
                        secret.add(_flat(t))
        if data:
            for _, t in data:
                secret.add(_flat(t))
            header = _header_above(grid, c, data[0][0], header_rows)
            n, distinct, longest, blank = _profile(data, (min(span), max(span)))
            profiles.append((data[0][0], c, _L["profile"].format(col=col_letter(c), header=header, n=n,
                                                                 distinct=distinct, longest=longest, blank=blank)))
    for c, items in sorted(masked_cells.items()):
        rows = [r for r, _ in items]
        n, distinct, longest, blank = _profile(items, (min(rows), max(rows)))
        profiles.append((masked[c][0], c, _L["masked"].format(col=col_letter(c), header=masked[c][1], n=n,
                                                              distinct=distinct, longest=longest, blank=blank)))

    text_lines = _fold_items(structural) + numeric_like + others + profiles
    lines.append(_L["texts"])
    lines.extend(t for _, _, t in sorted(text_lines, key=lambda x: (x[0], x[1])))
    rects = _rects(numbers)
    if rects:
        lines.append(_L["numbers"])
        lines.extend(_L["rect"].format(ref=_rng(a, b, c, d), sig=sig) for a, b, c, d, sig in rects)
    shapes = _formula_shapes(grid, formulas)
    if shapes:
        lines.append(_L["formulas"])
        lines.extend(shapes)
    hr = [_L["hidden_rows"].format(span=f"{a}" if a == b else f"{a}–{b}",
                                   t=sum(hidden_rows_count.get(x, [0, 0])[0] for x in range(a, b + 1)),
                                   n=sum(hidden_rows_count.get(x, [0, 0])[1] for x in range(a, b + 1)))
          for a, b in _spans(sorted(hidden_r))]
    hc = [_L["hidden_cols"].format(span=col_letter(a) if a == b else f"{col_letter(a)}–{col_letter(b)}",
                                   t=sum(hidden_cols_count.get(x, [0, 0])[0] for x in range(a, b + 1)),
                                   n=sum(hidden_cols_count.get(x, [0, 0])[1] for x in range(a, b + 1)))
          for a, b in _spans(sorted(hidden_c))]
    lines.append(_L["hidden"].format(rows="、".join(hr) or _L["none"], cols="、".join(hc) or _L["none"]))
    if fold_range is not None:
        a, b = fold_range
        lines.append(_L["fold"].format(a=a, b=b, n=b - a + 1))
        cols: dict[int, list[Any]] = {}
        for (r, c), cell in grid.cells.items():
            if r in folded and r not in hidden_r and c not in hidden_c:
                cols.setdefault(c, []).append(cell)
                if isinstance(cell.value, str):
                    secret.add(_flat(cell.value))
        for c in sorted(cols):
            kinds: dict[str, int] = {}
            for cell in cols[c]:
                k = _value_sig(cell) or _L["text_formula"]
                kinds[k] = kinds.get(k, 0) + 1
            distinct = len({repr(cell.value) for cell in cols[c]})
            blank = round(100 * (b - a + 1 - len(cols[c])) / (b - a + 1))
            lines.append(_L["fold_col"].format(
                col=col_letter(c), kinds="、".join(_L["kind_count"].format(kind=k, n=n) for k, n in sorted(kinds.items())),
                blank=max(blank, 0), distinct=distinct))
    return "\n".join(lines), truncated, secret


def _header_above(grid: Grid, c: int, r: int, header_rows: set[int]) -> str:
    rows = sorted((h for h in header_rows if h < r), reverse=True) or list(range(r - 1, max(r - 4, 0), -1))
    for h in rows:
        cell = grid.get(h, c)
        if cell is not None and isinstance(cell.value, str) and cell.value.strip():
            return _clip(cell.value)
    return ""


def _hide_text(text: str, secret: set[str]) -> str:
    """把隐藏行列、遮罩列、数据列里的文字从要发出去的段落里换掉（标签可能来自隐藏行）。

    secret 里每个文字同时按它的派生写法比：去掉末尾括号单位（「绝密分区（人次）」→「绝密分区」，起草时列名、
    单位的键、关系成员就是这么来的）、再去掉「一、」这类序号前缀，以及它们经 to_sql_name 的写法（SE-1、SE-2）。
    """
    return _hide_in(text, _variants(secret))


def _hide_in(text: str, words: set[str]) -> str:
    """把 words（已含派生写法）里 ≥2 字的文字在 text 里按子串换成 <隐藏>，长的先换。"""
    for s in sorted((x for x in words if len(x) >= 2), key=len, reverse=True):
        if s in text:
            text = text.replace(s, _L["hidden_text"])
    return text


def _variants(texts: set[str]) -> set[str]:
    """文字 → 它本身和起草时可能派生出的写法（去单位后缀、去序号前缀、to_sql_name）。"""
    out: set[str] = set()
    for x in texts:
        f = _flat(x)
        stem = _flat(P.split_unit_suffix(f)[0])
        for v in (f, stem, _flat(S._ENUM_PREFIX.sub("", stem))):
            if not v:
                continue
            out.add(v)
            n = to_sql_name(v, fallback="")
            if n:
                out.add(n)
    return out


def _hidden_texts(grids: dict[str, Grid]) -> set[str]:
    """隐藏行列里的文字（只这些，不含数据列、遮罩列）：规则半成品里只要有字符串是它的一部分，或包含它，就遮掉。"""
    out: set[str] = set()
    for g in grids.values():
        hr, hc = set(g.hidden_rows), set(g.hidden_cols)
        if not hr and not hc:
            continue
        for (r, c), cell in g.cells.items():
            if (r in hr or c in hc) and isinstance(cell.value, str) and _flat(cell.value):
                out.add(_flat(cell.value))
    return out


class _Outbound:
    """发给模型之前的最后一道清洗：首次调用里的规则半成品、失败原因，以及修订轮回灌的问题列表都过它。

    - 隐藏行列、遮罩列、数据列、认不出用途的文字（secret）及其派生写法：整串相等就换成 <隐藏>，自由文字里按
      子串换掉；
    - 隐藏行列的文字再多两道（规则半成品里的字符串）：是隐藏文字的一部分（≥3 字，列名可能被截短、取公共后缀）
      或包含隐藏文字的派生写法（≥2 字，表名、分段 id 可能拼了后缀），也换成 <隐藏>；
    - 像数的文字：整串相等、或在问题行里被「」引着的，换成 <文本数字>。

    修订轮的问题来自静态校验和干跑，消息里会引用系统发现的标签原文（「应为「全日客流（人次）」=「分区甲（人次）」
    +「…」」），系统发现并不排除隐藏行（SE-1）；首轮同意框里展示的全文不含这些，所以必须在这里清洗。
    """

    def __init__(self, secret: set[str], grids: dict[str, Grid]) -> None:
        self.secret = set(secret)
        self.exact = _variants(self.secret)
        hidden = _hidden_texts(grids)
        self.hidden_canon = [canon(h) for h in hidden]
        self.hidden_parts = {v for v in _variants(hidden) if len(canon(v)) >= 2}
        self.numeric = {S._shown(c.value) for g in grids.values() for c in g.cells.values()
                        if isinstance(c.value, str) and P.looks_numeric_text(c.value)}
        self.numeric = {_flat(x) for x in self.numeric if _flat(x)}

    def _secret_str(self, s: str) -> bool:
        f = _flat(s)
        if f in self.exact or s in self.exact:
            return True
        c = canon(f)
        if len(c) >= 3 and any(c in h for h in self.hidden_canon):
            return True
        return any(v in f for v in self.hidden_parts)

    def text(self, s: str) -> str:
        f = _flat(s)
        if f in self.numeric:
            return _L["numeric_text"]
        if self._secret_str(s):
            return _L["hidden_text"]
        return s

    def json(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {self.text(k) if isinstance(k, str) else k: self.json(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self.json(v) for v in node]
        if isinstance(node, str):
            return self.text(node)
        return node

    def line(self, text: str) -> str:
        out = _hide_in(text, self.exact)
        for part in sorted(self.hidden_parts, key=len, reverse=True):
            if part in out:
                out = out.replace(part, _L["hidden_text"])
        for n in sorted(self.numeric, key=len, reverse=True):
            q = _L["quoted"].format(text=n)
            if q in out:
                out = out.replace(q, _L["quoted"].format(text=_L["numeric_text"]))
        return out


def _facts_text(facts: DraftFacts, secret: set[str]) -> str:
    words = _variants(secret) if facts.facts else set()
    lines: list[str] = []
    for f in facts.facts:
        d = f.detail
        q = _L["quoted"]
        if f.kind == "sum_eq":
            line = _L["fact_sum"].format(id=f.id, text=f.text, segment=d.get("segment", ""),
                                         total=d.get("total", ""),
                                         parts="".join(q.format(text=p) for p in d.get("parts", [])))
        else:
            line = _L["fact_ne"].format(id=f.id, text=f.text, a="".join(q.format(text=a) for a in d.get("a", [])),
                                        b_segment=d.get("b_segment", ""), b=d.get("b", ""))
        lines.append(_hide_in(line, words))
    return "\n".join(lines) or _L["facts_none"]


def _candidates_text(facts: DraftFacts, secret: set[str]) -> str:
    lines = [_L["candidate"].format(title=_flat(t), words="、".join(w)) for t, w in facts.candidates.items()
             if _flat(t) not in secret]
    return "\n".join(lines) or _L["candidates_none"]


def compress(grids: dict[str, Grid], scan: WorkbookScan, facts: DraftFacts, *,
             mask_headers: set[str] | frozenset[str] = frozenset()) -> Compressed:
    """只发结构、不发数据的压缩表示。判定规则全部是确定性的，同一份原件的压缩文本相同。超限抛 AiTooLarge。"""
    mask_keys = {P.match_key(h) for h in mask_headers}
    order = [s.name for s in scan.sheets if s.state == "visible"] if scan.sheets else list(grids)
    names = [n for n in order if n in grids and grids[n].cells] + [
        n for n, g in grids.items() if n not in order and g.cells]
    parts: list[str] = []
    truncated = False
    secret: set[str] = set()
    for n in names:
        text, cut, hidden = _compress_sheet(grids[n], scan, mask_keys)
        if len(text) > COMPRESS_MAX_CHARS_PER_SHEET:
            raise AiTooLarge(f"工作表「{n}」的结构压缩后有 {len(text)} 字，超过上限 {COMPRESS_MAX_CHARS_PER_SHEET} 字，"
                             "无法交给 AI 起草")
        parts.append(text)
        truncated = truncated or cut
        secret |= hidden
    hidden_sheets = [s for s in scan.sheets if s.state != "visible"]
    if hidden_sheets:
        parts.append(_L["hidden_sheets"].format(n=len(hidden_sheets)))
    sheets = "\n\n".join(parts)
    facts_text = _facts_text(facts, secret)
    cand_text = _candidates_text(facts, secret)
    text = RECIPE_SECTIONS_PROMPT.format(sheets=sheets, facts=facts_text, candidates=cand_text)
    if len(text) > COMPRESS_MAX_CHARS:
        raise AiTooLarge(f"表格结构压缩后有 {len(text)} 字，超过上限 {COMPRESS_MAX_CHARS} 字，无法交给 AI 起草")
    return Compressed(text=text, chars=len(text), truncated=truncated,
                      sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


def _secret_texts(grids: dict[str, Grid], scan: WorkbookScan) -> set[str]:
    """不发给模型的文字（隐藏行列、数据列、认不出用途的文字）：拼规则草稿那一段时据此遮掉。

    不存进 Compressed：那个对象可能被接口层留存，隐藏行里的姓名不该跟着落库。
    """
    out: set[str] = set()
    for g in grids.values():
        if g.cells:
            out |= _compress_sheet(g, scan, set())[2]
    return out


# ==========================================================================
# 起草
# ==========================================================================


def _rules_section(rules: Draft | None, secret: set[str], grids: dict[str, Grid]) -> str:
    """规则起草的半成品（紧凑形式）和 failures_for_model（不用 failures：后者可以引用格子原文）。"""
    if rules is None or (rules.recipe is None and not rules.failures_for_model):
        return RULES_DRAFT_PROMPT["none"]
    scrub = _Outbound(secret, grids)
    out: list[str] = []
    if rules.recipe is not None:
        try:
            compact = Recipe.model_validate(rules.recipe).model_dump(mode="json", exclude_defaults=True)
        except ValidationError:
            compact = rules.recipe
        compact = scrub.json(compact)
        out.append(RULES_DRAFT_PROMPT["recipe"].format(
            recipe=json.dumps(compact, ensure_ascii=False, separators=(",", ":"))))
    lines = [RULES_DRAFT_PROMPT["line"].format(text=scrub.line(x)) for x in rules.failures_for_model]
    if lines:
        out.append(RULES_DRAFT_PROMPT["failures"].format(lines="\n".join(lines)))
    return "\n".join(out) or RULES_DRAFT_PROMPT["none"]


def _scrub_json(node: Any, secret: set[str], numeric: set[str]) -> Any:
    """配方里等于隐藏文字、像数的文字的字符串（值和键）换掉：配方语言写不了数，但标签可能是「2025」这种文字。

    列名是表头经 to_sql_name 变来的（「137-2222-3333」→「c_137_2222_3333」），按原文比对不上，所以也按
    to_sql_name 之后的写法、去掉单位后缀的写法比（见 _variants）：起草万一把数据行认成了表头，列名同样不能把
    数据带出去。首次调用实际用的是 _Outbound（还会按隐藏行列的子串比），这个函数留给只有文字集合的调用方。
    """
    names = _variants(secret)

    def fix(s: str) -> str:
        f = _flat(s)
        if f in numeric:
            return _L["numeric_text"]
        if f in secret or f in names or s in names:
            return _L["hidden_text"]
        return s
    if isinstance(node, dict):
        return {fix(k) if isinstance(k, str) else k: _scrub_json(v, secret, numeric) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub_json(v, secret, numeric) for v in node]
    if isinstance(node, str):
        return fix(node)
    return node


def _schema_text() -> str:
    return json.dumps(Recipe.model_json_schema(), ensure_ascii=False, separators=(",", ":"))


#: 模型回复的 JSON 最多嵌套几层。配方本身不到 10 层；更深的一律当成不合法的 JSON（深层嵌套会让 json.loads
#: 抛 RecursionError，它不是 ValueError，会逃出 ai_draft，见 SE-6）
JSON_MAX_DEPTH = 64


def _too_deep(s: str, limit: int = JSON_MAX_DEPTH) -> bool:
    depth = 0
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > limit:
                return True
        elif ch in "]}":
            depth -= 1
    return False


def _extract_json(text: str) -> dict[str, Any]:
    """回复 → JSON 对象。任何解析失败（含嵌套过深、RecursionError）都按 ValueError 抛，走修订分支。"""
    s = text.strip()
    s = re.sub(r"^```[A-Za-z]*\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object found")
    body = s[i:j + 1]
    if _too_deep(body):
        raise ValueError(f"JSON nested deeper than {JSON_MAX_DEPTH} levels")
    try:
        data = json.loads(body)
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None
    if not isinstance(data, dict):
        raise ValueError("JSON is not an object")
    return data


def _cut_off(reply: Any) -> bool:
    """输出被截断：Anthropic 是 stop_reason=max_tokens，OpenAI 系是 finish_reason=length（同 copilot._cut_off）。"""
    meta = getattr(reply, "response_metadata", None) or {}
    reason = meta.get("finish_reason") or meta.get("stop_reason")
    return reason in ("length", "max_tokens")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _usage(reply: Any, model_id: str, attempt: int, ok: bool) -> AiUsage:
    from app.providers import catalog

    meta = getattr(reply, "usage_metadata", None) or {}
    inp, out = int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)
    return AiUsage(model=model_id, input_tokens=inp, output_tokens=out,
                   cost_usd=round(catalog.estimate_cost(model_id, inp, out), 6), ok=ok, at=_now(), attempt=attempt)


def _failed_usage(model_id: str, attempt: int) -> AiUsage:
    return AiUsage(model=model_id, input_tokens=0, output_tokens=0, cost_usd=0.0, ok=False, at=_now(), attempt=attempt)


def _problems_text(lines: list[str]) -> str:
    return "\n".join(RULES_DRAFT_PROMPT["line"].format(text=x) for x in lines[:S.MODEL_LINES_MAX])


def first_call_text(compressed: Compressed, *, grids: dict[str, Grid], scan: WorkbookScan,
                    rules_draft: Draft | None) -> str:
    """ai_draft 第一次调用模型时实际发出的全文：系统提示 + 首次提示词（两段之间空一行）。

    同意框展示的必须是这份全文，而不只是压缩表示：首次提示词里还有单位词表、规则起草的半成品与失败原因、
    配方 JSON Schema，这些同样会发给模型。拼法与 ai_draft 里的逐字相同（同一组常量、同一个
    _rules_section、同一个 _schema_text），接口层按这份全文算 sha256，用户同意的是哪一份、发出去的就是哪一份。
    """
    secret = _secret_texts(grids, scan)
    prompt = "\n\n".join([compressed.text, RECIPE_REQUEST_PROMPT.format(
        units="、".join(P.UNITS),
        rules_draft=_rules_section(rules_draft, secret, grids),
        schema=_schema_text())])
    return "\n\n".join([RECIPE_DRAFT_SYSTEM, prompt])


async def ai_draft(session: AsyncSession, *, grids: dict[str, Grid], scan: WorkbookScan, facts: DraftFacts,
                   rules_draft: Draft | None, filename: str, validate: ValidateFn,
                   dry_run: AsyncDryRunFn | DryRunFn | None, model: Any | None = None,
                   compressed: Compressed | None = None, model_id: str | None = None) -> AiDraftResult:
    """AI 起草。只抛 AiUnavailable / AiTooLarge（都在第一次调用模型之前）；之后的失败一律返回 draft=None。

    这是一个跑在事件循环上的协程，CPU 活一律不在循环线程上做：静态校验（check_recipe）和卡片问题
    （questions_for）放进线程池；干跑经 S.run_dry_async——dry_run 是协程函数就 await（接口层注入的那份
    占一个解析名额、在线程池里执行），是普通函数也放进线程池。

    model 给了就用（接口层把同意时解析出的那个模型交进来，同意记录、实际调用、用量是同一个；测试注入假模型，
    需有 async ainvoke(messages) -> AIMessage），model_id 是它的模型 id；没给 model 时 resolve_draft_model。
    compressed 给了就用（接口层先在解析名额内算好、预览时给用户看的就是这一份），否则现算。
    filename 不发给模型（文件名里可能有人名、单位名）；只为与规则起草同签名。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.engine.state import message_text

    if model is None:
        model, model_id, avail = await resolve_draft_model(session)
        if model is None or not avail.available:
            raise AiUnavailable(avail.reason or "AI 起草不可用")
    elif not model_id:
        named = getattr(model, "model_name", None) or getattr(model, "model", None)
        model_id = named if isinstance(named, str) else ""
    if compressed is None:
        compressed = compress(grids, scan, facts)
    secret = _secret_texts(grids, scan)
    scrub = _Outbound(secret, grids)
    # 前三段整段取压缩表示的 text（不再重算系统发现、候选词）：接口层存库再取回的只有 text 等四个字段，
    # 发出去的必须与同意框里展示、sha256 记下的那一份逐字相同
    prompt = "\n\n".join([compressed.text, RECIPE_REQUEST_PROMPT.format(
        units="、".join(P.UNITS),
        rules_draft=_rules_section(rules_draft, secret, grids),
        schema=_schema_text())])
    messages: list[Any] = [SystemMessage(content=RECIPE_DRAFT_SYSTEM), HumanMessage(content=prompt)]
    usage: list[AiUsage] = []
    attempts = 0
    last_error = _ERR_INVALID
    best: tuple[Recipe, Any, list[str], list[str]] | None = None
    while attempts < AI_MAX_ATTEMPTS:
        attempts += 1
        try:
            reply = await asyncio.wait_for(model.ainvoke(list(messages)), timeout=AI_TIMEOUT_S)
        except asyncio.TimeoutError:
            usage.append(_failed_usage(model_id, attempts))
            last_error = _ERR_TIMEOUT.format(n=AI_TIMEOUT_S)
            logger.info("recipe ai draft attempt %d timed out (model=%s)", attempts, model_id)
            continue
        except Exception as e:  # noqa: BLE001 - 调用失败只能记一次失败，不能变成起草成功
            from app.core.errors import describe_exception

            usage.append(_failed_usage(model_id, attempts))
            last_error = _ERR_CALL.format(reason=describe_exception(e))
            logger.info("recipe ai draft attempt %d failed (model=%s): %s", attempts, model_id, type(e).__name__)
            continue
        truncated = _cut_off(reply)
        usage.append(_usage(reply, model_id, attempts, ok=not truncated))
        u = usage[-1]
        logger.info("recipe ai draft attempt %d model=%s in=%d out=%d truncated=%s",
                    attempts, model_id, u.input_tokens, u.output_tokens, truncated)
        if truncated:
            # 半截文本不拿去修订：同样的消息列表重来
            last_error = _ERR_TRUNCATED
            continue
        last_error = _ERR_INVALID
        # 拿到回复之后的任何异常都只算这一次没起草成：用量已记，结果走返回值（SE-6）
        try:
            text = message_text(reply)
            try:
                data = _extract_json(text)
            except (ValueError, json.JSONDecodeError) as e:
                detail = e.msg if isinstance(e, json.JSONDecodeError) else str(e)
                messages += [reply, HumanMessage(content=RECIPE_REPAIR_PROMPT.format(
                    problems=_problems_text([RULES_DRAFT_PROMPT["not_json"].format(detail=detail)])))]
                continue
            human, for_model, parsed = await asyncio.to_thread(S.check_recipe, data, facts, "ai", validate)
            if parsed is None:
                # 静态校验的消息会引用系统发现的标签原文（可能来自隐藏行），回灌前清洗（SE-1）
                messages += [reply, HumanMessage(content=RECIPE_REPAIR_PROMPT.format(
                    problems=_problems_text([scrub.line(x) for x in for_model])))]
                continue
            ext, dh, dm = (None, [], [])
            if dry_run is not None:
                ext, dh, dm = await S.run_dry_async(parsed, dry_run, grids)
            best = (parsed, ext, dh, dm)
            if dh and attempts < AI_MAX_ATTEMPTS:
                messages += [reply, HumanMessage(content=RECIPE_REPAIR_PROMPT.format(
                    problems=_problems_text([scrub.line(x) for x in dm])))]
                continue
        except Exception as e:  # noqa: BLE001
            logger.warning("recipe ai draft attempt %d: handling the reply failed: %s", attempts, type(e).__name__)
            last_error = _ERR_INVALID
            break
        break
    if best is None:
        return AiDraftResult(None, usage, attempts, last_error, compressed.chars, compressed.sha256)
    parsed, ext, dh, dm = best
    recipe = parsed.model_dump(mode="json")
    try:
        cards, questions = await asyncio.to_thread(S.questions_for, recipe, facts, grids, extraction=ext)
    except Exception:  # noqa: BLE001 - 卡片是辅助信息
        cards, questions = [], []
    cards.insert(0, Card("ai_origin", "AI 起草：请逐项核对",
                         "这份配方由 AI 根据表格结构起草，系统已做静态校验和检查；导入前请逐项核对下面的卡片和问题，"
                         "提交时需要逐条确认"))
    result = Draft(recipe=recipe, complete=not dh, origin="ai", cards=cards, questions=questions, facts=facts,
                   failures=dh, failures_for_model=dm)
    return AiDraftResult(result, usage, attempts, None, compressed.chars, compressed.sha256)


__all__ = [
    "AI_FALLBACK_MAX_TOKENS", "AI_MAX_ATTEMPTS", "AI_MAX_TOKENS", "AI_TIMEOUT_S", "AiDraftResult", "AiTooLarge",
    "AiUnavailable", "COMPRESS_MAX_CHARS", "COMPRESS_MAX_CHARS_PER_SHEET", "COMPRESS_PROMPT_LABELS", "JSON_MAX_DEPTH",
    "COPILOT_SETTING_KEY", "Compressed", "RECIPE_DRAFT_PROMPT", "RECIPE_DRAFT_SYSTEM", "RECIPE_REPAIR_PROMPT",
    "ai_availability", "ai_draft", "compress", "first_call_text", "resolve_draft_model", "sanitize_formula",
]
