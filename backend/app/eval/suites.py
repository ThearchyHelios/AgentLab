"""题集的形状与校验（细案 01 做法 7、17；评测-D9）。

题集：name（必填）、description、timezone（题目里的 {{ schedule.* }} 按它算）、default_variant（发起评测时缺省用的变体，
第 3 波解析；这里只要求是对象）、budget（只存改过的项）、archived。改题集带上读到的 version（乐观锁，接口回 409）。

**预算**（评测-D9，用户拍板）：缺省见 db/models_eval.DEFAULT_BUDGET——同时 2 题、每题 900 秒、单次评测 2 美元、评测每日
5 美元。daily_max_usd 是评测那本账（engine/spend 的第三本，第 3 波接上）的当日上限：评测单独记账，不挤占裁判、核查的额度。
这一波只定字段和取值范围，执行（预留、找补、触顶不再开新题）在第 3 波。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.db.models_eval import DEFAULT_BUDGET
from app.eval.cases import CaseInvalid

#: 题集不合规（422）的机器码
SUITE_INVALID = "eval_suite_invalid"

NAME_MAX = 200
DESCRIPTION_MAX = 2000
#: 变体存成 JSON：太大的多半是传错了东西
VARIANT_MAX = 20_000

#: 预算每一项的取值范围：(说法, 整数还是金额, 下限, 上限)。下限都大于 0：0 美元、0 秒的上限只会让评测一题都开不了
BUDGET_RULES: dict[str, tuple[str, type, float, float]] = {
    "max_parallel": ("同时评几道题", int, 1, 8),
    "per_case_timeout_s": ("每题时限（秒）", int, 30, 7200),
    "per_run_max_usd": ("单次评测的金额上限（美元）", float, 0.01, 1000),
    "daily_max_usd": ("评测每日的金额上限（美元）", float, 0.01, 10_000),
}


def _invalid(message: str, field: str) -> CaseInvalid:
    return CaseInvalid(message, field, SUITE_INVALID)


def normalize_budget(raw: Any) -> dict[str, Any]:
    """题集里改过的预算项 → 规范化（只留认识的、和缺省不同的）。"""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping):
        raise _invalid("预算需要是一个对象", "budget")
    unknown = sorted(set(raw) - set(BUDGET_RULES))
    if unknown:
        raise _invalid(f"预算里不认识这些项：{'、'.join(unknown)}（认识的：{'、'.join(BUDGET_RULES)}）", f"budget.{unknown[0]}")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        label, kind, low, high = BUDGET_RULES[key]
        if value is None:
            continue
        ok = not isinstance(value, bool) and isinstance(value, (int, float)) and (kind is float or isinstance(value, int))
        if not ok or not low <= value <= high:
            unit = "整数" if kind is int else "数"
            raise _invalid(f"{label}需要是 {low:g} 到 {high:g} 之间的{unit}", f"budget.{key}")
        value = kind(value)
        if value != DEFAULT_BUDGET[key]:
            out[key] = value
    if out.get("per_run_max_usd", DEFAULT_BUDGET["per_run_max_usd"]) > \
            out.get("daily_max_usd", DEFAULT_BUDGET["daily_max_usd"]):
        raise _invalid("单次评测的金额上限不能高于评测每日的上限", "budget.per_run_max_usd")
    return out


def effective_budget(stored: Mapping[str, Any] | None) -> dict[str, Any]:
    """生效的预算：缺省加上题集改过的项。"""
    return {**DEFAULT_BUDGET, **{k: v for k, v in (stored or {}).items() if k in DEFAULT_BUDGET}}


def normalize_suite(body: Mapping[str, Any], *, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """请求体（改的时候和库里的合起来：没带的项照旧）→ 题集各列的值。不合规抛 CaseInvalid（code=eval_suite_invalid）。"""
    from app.core.artifact_store import canonical_json
    from app.scheduler import cron

    if not isinstance(body, Mapping):
        raise _invalid("题集需要是一个对象", "name")
    cur = dict(current or {})

    def pick(key: str, default: Any = None) -> Any:
        return body[key] if key in body else cur.get(key, default)

    name = pick("name", "")
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        raise _invalid("请填写题集的名称", "name")
    if len(name) > NAME_MAX:
        raise _invalid(f"题集的名称不能超过 {NAME_MAX} 个字符", "name")
    description = pick("description", "") or ""
    if not isinstance(description, str) or len(description.strip()) > DESCRIPTION_MAX:
        raise _invalid(f"说明需要是不超过 {DESCRIPTION_MAX} 个字符的文字", "description")
    try:
        timezone = cron.timezone_name(pick("timezone", "Asia/Shanghai") or "")
    except cron.TimezoneError as e:
        raise _invalid(str(e), "timezone") from e
    variant = pick("default_variant", {}) or {}
    if not isinstance(variant, Mapping) or len(canonical_json(variant)) > VARIANT_MAX:
        raise _invalid("默认变体需要是一个对象", "default_variant")
    archived = pick("archived", False)
    if not isinstance(archived, bool):
        raise _invalid("是否归档需要是 true 或 false", "archived")
    return {"name": name, "description": description.strip(), "timezone": timezone, "default_variant": dict(variant),
            "budget": normalize_budget(pick("budget", {})), "archived": archived}


__all__ = ["BUDGET_RULES", "SUITE_INVALID", "effective_budget", "normalize_budget", "normalize_suite"]
