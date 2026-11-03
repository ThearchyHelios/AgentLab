"""口径的内容指纹：同一位置的口径定义变没变，按定义本身的哈希认，不看运行时算出来的值。

口径的身份原先是「工作流 id@版本」（ledger/labels.caliber_key）。同一版不会变（版本快照不可变），但复制、改名、迁移之后
就认不回是同一份口径了；后续做「定义变更影响分析」也需要一份内容指纹。这里给的就是这份指纹：

- definition_hash：一张口径卡的指标定义（caliber、caliber_version 和每个指标的语义字段）的 sha256；
- version_hash：一份口径版本里各卡的内容哈希排序后再取 sha256，也就是整份版本的定义指纹；
- card_hash_of：从 {node_id, metrics, caliber?, caliber_version?} 这种卡形状取哈希（运行时指标卡、账本里源头口径卡的
  两种写法共用）。

**只取语义字段。** 指标只留 id / name / expression / unit / decimals / format / kind / definition / params 里存在的键，
按 id 排序（顺序不影响哈希）；空字符串、None、空的列表和字典不写进规范 JSON。值、渲染后的字、输入出处、状态这些都是
运行时产物，不进哈希——同一份定义重跑一次，或者补了无关的字段，哈希不变。

**这是内容指纹，不是跨图稳定的 ID。** 表达式里写的是节点 id（cell(nodes.fetch, 0, 'gmv')），取数节点改了名，式子跟着改，
哈希就会变；复制到另一张图、指标定义一字不差时哈希相同，正是用来认回同一份定义。要跨图认同一份口径，仍靠版本引用
（caliber_from）和「工作流@版本」。

**口径名、版本进不进哈希。** 两个函数的入参可以是空串：运行时指标卡和账本两侧算指标集指纹时都不查库、不拿名字，
只按 metrics 算（空串时规范 JSON 里省略这两个键），这样两边的哈希才对得上。名字、版本要不要算进去由调用方决定。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: 指标的语义字段：只有这几样算定义，运行时值（value、rendered、inputs……）不参与哈希
SEMANTIC_FIELDS = ("id", "name", "expression", "unit", "decimals", "format", "kind", "definition", "params")


def _blank(value: Any) -> bool:
    """空值、缺省键：None、空字符串、空的列表和字典不写进规范 JSON。0、False 是有意义的取值，照写。"""
    return value is None or value == "" or (isinstance(value, (list, dict, tuple)) and not value)


def _metric_content(metric: Any) -> dict[str, Any]:
    """一个指标的语义内容：只留 SEMANTIC_FIELDS 里存在的键，空值略去。"""
    if not isinstance(metric, dict):
        return {}
    return {field: metric[field] for field in SEMANTIC_FIELDS if field in metric and not _blank(metric[field])}


def definition_hash(caliber: str, caliber_version: str, metrics: Iterable[dict[str, Any]]) -> str:
    """一张口径卡的指标定义 → 内容指纹。指标按 id 排序，顺序不影响哈希；caliber / caliber_version 为空时不进规范 JSON
    （运行时和账本两侧只按 metrics 算，见模块说明）。"""
    from app.core.artifact_store import canonical_json, content_hash

    content: dict[str, Any] = {}
    if not _blank(caliber):
        content["caliber"] = str(caliber)
    if not _blank(caliber_version):
        content["caliber_version"] = str(caliber_version)
    kept = [_metric_content(metric) for metric in metrics or []]
    content["metrics"] = sorted(kept, key=lambda metric: str(metric.get("id") or ""))
    return content_hash(canonical_json(content))


def card_hash_of(card: dict[str, Any]) -> str:
    """一张口径卡（card）的内容指纹：{node_id, metrics, caliber?, caliber_version?}。没有 caliber 时按空串算
    （即只按 metrics）。"""
    if not isinstance(card, dict):
        return definition_hash("", "", [])
    return definition_hash(str(card.get("caliber") or ""), str(card.get("caliber_version") or ""),
                           card.get("metrics") or [])


def version_hash(card_hashes: Iterable[str]) -> str:
    """一份口径版本的内容指纹：各卡的内容哈希（card_hash_of 的结果）排序后取 sha256，和卡片的先后无关。"""
    from app.core.artifact_store import content_hash

    return content_hash("\n".join(sorted(str(h) for h in card_hashes or () if h)))


__all__ = ["SEMANTIC_FIELDS", "card_hash_of", "definition_hash", "version_hash"]
