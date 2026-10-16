"""评测的变体：这一次用哪几个模型、读哪一份目录、重复几次（细案 01 做法 9、12；评测-D5、D10）。

变体原样存进 EvalRun.variant（发起时规范化过），实际解析出的模型 id 另存 resolved_models。几项：

- assistant {provider, model}：助手（建图、挑表、复核）用哪个模型；不写按「编排」页助手栏的设置（copilot 那一项），和
  问数据页同一条取值链（GenerateIn.provider / model 按请求覆盖，挑表、复核跟着换）。
- writer {provider, model}：写作模型——答案运行里模型节点的缺省（写进图的 defaults）。节点自己点了模型的不覆盖，结果里
  照实标出（answer.py 的 own_models）。不写就是各节点自己的、再不然按接入的缺省（和问数据页一样）。
- judge：结论裁判开不开（缺省按设置 eval.judge）。打开了才判，走按需裁判同一条路，花费记在评测那本账上；模型按设置里的
  证据裁判模型（按需裁判就是这么取的，变体不另换裁判模型）。文字要点的期望也只在打开时判。
- catalog {mode, snapshot}：读哪一份目录（评测-D5）——live 当前目录、reviewed 只用已核实和已确认的项、off 关目录、snapshot
  指定一份目录快照（EvalCatalogSnapshot 的 id，影子起草的或拍好的）。live、reviewed 在开评时都拍一份当前目录的快照，按
  快照读：评测一跑几十分钟，期间起草、剖析照常改目录，也不影响这次的可比性（做法 9「不占数据源的队」）。
- repeats：每题重复几次（1–5）。定时跑 1 次，对照缺省 3 次（评测-D10）。
- label：这一臂叫什么（A/B 时给人看，「当前目录」「关目录」……）。

**同一变体**（和上一次比时挑基准用，做法 12）：助手、写作两项实际解析出的模型、目录的 mode（指定快照的连同快照 id）；
重复次数、裁判开不开不算——它们不改变答案本身。variant_key 是它的指纹。

**目录贡献对照**（做法 12）：一组预设的 A/B——当前目录 vs 关，可以再加第三臂「只用已核实和已确认的项」（catalog_preset）。
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.eval.cases import CaseInvalid

#: 变体不合规（422）的机器码
VARIANT_INVALID = "eval_variant_invalid"
#: 变体里点名的模型、快照用不了（422）
VARIANT_UNAVAILABLE = "eval_variant_unavailable"

CATALOG_MODES = ("live", "reviewed", "off", "snapshot")
CATALOG_LABEL = {"live": "当前目录", "reviewed": "只用已核实和已确认的项", "off": "关目录", "snapshot": "目录快照"}
REPEATS_MAX = 5
#: 定时跑 1 次，A/B 对照缺省 3 次（评测-D10）
SCHEDULE_REPEATS = 1
PAIR_REPEATS = 3
#: 一次 A/B 最多几臂（目录贡献对照最多三臂）
ARMS_MAX = 3
LABEL_MAX = 40
_KEYS = ("assistant", "writer", "judge", "catalog", "repeats", "label")


def _invalid(message: str, field_name: str) -> CaseInvalid:
    return CaseInvalid(message, field_name, VARIANT_INVALID)


def _model_pair(raw: Any, where: str, field_name: str) -> dict[str, str | None]:
    if raw in (None, {}):
        return {"provider": None, "model": None}
    if not isinstance(raw, Mapping) or set(raw) - {"provider", "model"}:
        raise _invalid(f"{where}需要是 {{provider, model}} 的对象（都可以留空）", field_name)
    out: dict[str, str | None] = {}
    for key in ("provider", "model"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise _invalid(f"{where}的「{key}」需要是文字", f"{field_name}.{key}")
        out[key] = value.strip()[:200] or None if isinstance(value, str) else None
    return out


def normalize_variant(raw: Any, *, judge_default: bool = False, repeats_default: int = 1) -> dict[str, Any]:
    """请求里的变体 → 规范化的形状（全部键都在）。不合规抛 CaseInvalid（code=eval_variant_invalid）。"""
    if raw in (None, {}):
        raw = {}
    if not isinstance(raw, Mapping):
        raise _invalid("变体需要是一个对象", "variant")
    unknown = sorted(set(raw) - set(_KEYS))
    if unknown:
        raise _invalid(f"变体里不认识这些项：{'、'.join(unknown)}（认识的：{'、'.join(_KEYS)}）", f"variant.{unknown[0]}")
    judge = raw.get("judge", judge_default)
    if not isinstance(judge, bool):
        raise _invalid("变体的「结论裁判」需要是 true 或 false", "variant.judge")
    catalog = raw.get("catalog") or {"mode": "live"}
    if isinstance(catalog, str):
        catalog = {"mode": catalog}
    if not isinstance(catalog, Mapping) or catalog.get("mode") not in CATALOG_MODES \
            or set(catalog) - {"mode", "snapshot"}:
        raise _invalid(f"变体的目录需要是 {{mode, snapshot}}，mode 只能是 {'、'.join(CATALOG_MODES)}", "variant.catalog")
    snapshot = catalog.get("snapshot")
    if catalog["mode"] == "snapshot":
        if not isinstance(snapshot, str) or not snapshot.strip():
            raise _invalid("目录选「目录快照」时要指定是哪一份快照（snapshot）", "variant.catalog.snapshot")
        snapshot = snapshot.strip()
    elif snapshot not in (None, ""):
        raise _invalid("只有目录选「目录快照」时才指定快照", "variant.catalog.snapshot")
    else:
        snapshot = None
    repeats = raw.get("repeats", repeats_default)
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= REPEATS_MAX:
        raise _invalid(f"每题重复的次数需要是 1 到 {REPEATS_MAX} 的整数", "variant.repeats")
    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        raise _invalid("这一臂的名字需要是文字", "variant.label")
    return {"assistant": _model_pair(raw.get("assistant"), "助手模型", "variant.assistant"),
            "writer": _model_pair(raw.get("writer"), "写作模型", "variant.writer"),
            "judge": judge, "catalog": {"mode": catalog["mode"], "snapshot": snapshot}, "repeats": repeats,
            "label": (label or "").strip()[:LABEL_MAX] or None}


def catalog_preset(base: Mapping[str, Any], *, reviewed: bool = False) -> list[dict[str, Any]]:
    """「目录的贡献」预设对照：当前目录 vs 关（reviewed 时再加「只用已核实和已确认的项」）。模型、裁判、重复次数照 base。"""
    arms = [("live", CATALOG_LABEL["live"]), ("off", CATALOG_LABEL["off"])]
    if reviewed:
        arms.insert(1, ("reviewed", CATALOG_LABEL["reviewed"]))
    return [{**copy.deepcopy(dict(base)), "catalog": {"mode": mode, "snapshot": None}, "label": label}
            for mode, label in arms]


def arm_name(variant: Mapping[str, Any], index: int) -> str:
    """这一臂的名字：变体里写了就用，否则按目录（只有目录不同时一看就懂），再不然 A、B、C。"""
    return str(variant.get("label") or "") or "ABC"[index]


# ==========================================================================
# 解析：实际用哪个模型、读哪份目录
# ==========================================================================


@dataclass
class Resolved:
    """发起时解析出来的：各角色实际的模型 id、目录快照（工件 id）、目录视图（Run.options['catalog_view'] 的形状）。"""

    models: dict[str, Any] = field(default_factory=dict)
    snapshot_artifact: str | None = None
    view: dict[str, Any] | None = None


async def _model_id(session: AsyncSession, pair: Mapping[str, Any], *, assistant: bool) -> str:
    """这一对 {provider, model} 实际是哪个模型 id。助手没点名的按助手栏的设置；用不了照实拒（VARIANT_UNAVAILABLE）。"""
    from app.providers.factory import ModelSpec, ProviderNotConfigured, get_chat_model

    try:
        if assistant:
            from app.api.copilot import GenerateIn, copilot_model_spec

            spec = await copilot_model_spec(session, GenerateIn(instruction="eval", provider=pair.get("provider"),
                                                                model=pair.get("model")))
        else:
            spec = ModelSpec(provider=pair.get("provider"), model=pair.get("model"))
        _model, model_id = await get_chat_model(session, spec)
    except ProviderNotConfigured as e:
        what = "助手模型" if assistant else "写作模型"
        raise CaseInvalid(f"{what}用不了：{e}", "variant.assistant" if assistant else "variant.writer",
                          VARIANT_UNAVAILABLE) from e
    return model_id


async def resolve_models(session: AsyncSession, variant: Mapping[str, Any]) -> dict[str, Any]:
    """{assistant: 模型 id, writer: 模型 id 或 None（没换写作模型：各节点自己的）, judge: 开不开}。"""
    writer = variant.get("writer") or {}
    return {"assistant": await _model_id(session, variant.get("assistant") or {}, assistant=True),
            "writer": await _model_id(session, writer, assistant=False) if (writer.get("provider") or writer.get("model"))
            else None,
            "judge": bool(variant.get("judge"))}


def view_of(variant: Mapping[str, Any], snapshot_artifact: str | None) -> dict[str, Any]:
    """这一臂的目录视图（data/catalog_view.CatalogView 的形状）。live、reviewed 按开评时拍的快照读。"""
    mode = (variant.get("catalog") or {}).get("mode") or "live"
    if mode == "off":
        return {"mode": "off", "statuses": "all"}
    if not snapshot_artifact:
        raise ValueError("目录视图要读快照，却没有快照工件")
    return {"mode": "snapshot", "statuses": "reviewed" if mode == "reviewed" else "all", "snapshot": snapshot_artifact}


async def snapshot_artifact(session: AsyncSession, variant: Mapping[str, Any]) -> str | None:
    """变体指定的那份目录快照的工件 id（mode=snapshot）；别的模式为 None。快照不在、还没起草完的照实拒。"""
    from app.db.models_eval import EvalCatalogSnapshot

    catalog = variant.get("catalog") or {}
    if catalog.get("mode") != "snapshot":
        return None
    row = await session.get(EvalCatalogSnapshot, str(catalog.get("snapshot") or ""))
    if row is None:
        raise CaseInvalid("变体指定的目录快照不存在", "variant.catalog.snapshot", VARIANT_UNAVAILABLE)
    if not row.artifact_id or row.status not in ("succeeded", "partial"):
        raise CaseInvalid("变体指定的目录快照还没有出来（影子起草没做完、停止了或失败了）", "variant.catalog.snapshot",
                          VARIANT_UNAVAILABLE)
    return row.artifact_id


def variant_key(variant: Mapping[str, Any], models: Mapping[str, Any]) -> str:
    """「同一变体」的指纹：助手、写作实际解析出的模型，目录的 mode（指定快照的连同快照 id）。"""
    from app.core.artifact_store import canonical_json, content_hash

    catalog = variant.get("catalog") or {}
    body = {"assistant": models.get("assistant"), "writer": models.get("writer"), "catalog": catalog.get("mode"),
            "snapshot": catalog.get("snapshot") if catalog.get("mode") == "snapshot" else None}
    return content_hash(canonical_json(body))[:16]


def describe(variant: Mapping[str, Any], models: Mapping[str, Any] | None = None) -> str:
    """给人看的一句：「助手 x · 写作 y · 当前目录 · 裁判关 · 每题 1 次」。"""
    models = models or {}
    catalog = variant.get("catalog") or {}
    parts = [f"助手 {models.get('assistant') or (variant.get('assistant') or {}).get('model') or '缺省'}",
             f"写作 {models.get('writer') or (variant.get('writer') or {}).get('model') or '各节点自己的'}",
             CATALOG_LABEL.get(catalog.get("mode") or "live", "当前目录"),
             f"结论裁判{'开' if variant.get('judge') else '关'}", f"每题 {variant.get('repeats') or 1} 次"]
    return " · ".join(parts)


__all__ = ["ARMS_MAX", "CATALOG_LABEL", "CATALOG_MODES", "PAIR_REPEATS", "REPEATS_MAX", "Resolved", "SCHEDULE_REPEATS",
           "VARIANT_INVALID", "VARIANT_UNAVAILABLE", "arm_name", "catalog_preset", "describe", "normalize_variant",
           "resolve_models", "snapshot_artifact", "variant_key", "view_of"]
