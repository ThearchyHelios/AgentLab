"""重排：让模型把初筛结果重新排一遍。

混合检索给的是"大致相关"——BM25 看词频，向量看空间距离，两者都不读句子。
重排让模型把 query 和每个候选放在一起判断，这是检索链路上单点收益最大的一步。

用已配置的对话模型做，不引入本地 cross-encoder：后者要下模型权重，和这个
项目"零配置能跑起来"的基线冲突（同理不照抄 RAGFlow 那套 Elasticsearch +
Infinity，官方建议 32GB 内存起）。代价是每次检索多一次模型调用，所以默认关闭。

失败一律退回原顺序：重排是增强，不该因为模型抽风或没配 key 就让整个检索挂掉。
"""

from __future__ import annotations

import json
import re
from typing import Any

_PROMPT = """给下面每个片段打分：它对回答这个问题有多大帮助。

问题：{query}

片段：
{candidates}

只输出一行 JSON，形如 {{"scores": [{example}]}}——{n} 个 0 到 10 的整数，
按片段顺序一一对应。不要输出任何别的东西。
0 表示完全无关，10 表示直接回答了问题。"""


class Unusable(ValueError):
    """回复用不了。message 就是要交给用户的那句话。"""


def _parse_scores(text: str, n: int) -> list[float]:
    """从模型回复里抠出分数数组。用不了就抛 Unusable，message 说清楚是哪种用不了。

    模型偶尔会加围栏或者说两句废话，所以不直接 json.loads 整段；
    但也不做过度容错——解析不出来就退回原顺序，比按半截数据重排安全。
    """
    if not text.strip():
        # 实测 deepseek-v4-pro：失败那次 out_tokens 正好等于 max_tokens，
        # 输出预算被耗光后正文什么都不剩。报"解析不出来"会把人引去查 JSON 格式，
        # 而该调的是 max_tokens——llm.py 里对同一个现象也是这么说的
        raise Unusable("模型没有输出正文，多半是输出 token 预算用完了。把节点上的最大输出 token 调大")
    match = re.search(r"\{[^{}]*\"scores\"\s*:\s*\[[^\]]*\][^{}]*\}", text, re.S)
    if not match:
        raise Unusable(f"模型回复里找不到 scores：{text.strip()[:80]}")
    try:
        scores = json.loads(match.group(0)).get("scores")
    except json.JSONDecodeError as e:
        raise Unusable(f"scores 不是合法 JSON：{e}") from e
    if not isinstance(scores, list) or len(scores) != n:
        # 个数对不上就没法一一对应，硬凑只会把顺序搅乱
        raise Unusable(f"给了 {len(scores) if isinstance(scores, list) else '?'} 个分数，"
                       f"候选有 {n} 个，对不上")
    out: list[float] = []
    for s in scores:
        if not isinstance(s, (int, float)):
            raise Unusable(f"分数里混了非数字：{s!r}")
        out.append(float(s))
    return out


async def rerank(
    model: Any, query: str, hits: list[dict[str, Any]], *, top_n: int,
    on_note: Any = None,
) -> list[dict[str, Any]]:
    """把 hits 重排后取前 top_n。失败原样返回前 top_n。

    model 是任意带 ainvoke 的 LangChain 模型；传 None 表示不重排。
    """
    if not hits or model is None or len(hits) <= 1:
        return hits[:top_n]

    listing = "\n\n".join(
        # 截断是必要的：初筛 20 条 × 每条 800 字会把 prompt 撑到两万字，
        # 而判断相关性用不着整段
        f"[{i + 1}] {h.get('content', '')[:400]}"
        for i, h in enumerate(hits)
    )
    prompt = _PROMPT.format(
        query=query, candidates=listing, n=len(hits),
        example=", ".join(["8"] * min(len(hits), 3)) + (", …" if len(hits) > 3 else ""),
    )

    try:
        reply = await model.ainvoke(prompt)
        text = reply if isinstance(reply, str) else getattr(reply, "content", "")
        if isinstance(text, list):  # Anthropic 的块状 content
            text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
        scores = _parse_scores(str(text), len(hits))
    except Unusable as e:
        if on_note:
            on_note(f"重排没用上，按初筛顺序返回：{e}")
        return hits[:top_n]
    except Exception as e:  # noqa: BLE001
        if on_note:
            on_note(f"重排失败，按初筛顺序返回：{type(e).__name__}: {e}")
        return hits[:top_n]

    ranked = sorted(zip(hits, scores), key=lambda t: t[1], reverse=True)
    out: list[dict[str, Any]] = []
    for hit, score in ranked[:top_n]:
        item = dict(hit)
        item["rerank_score"] = score
        # 初筛分保留：重排把它挪到第几位、原来是第几位，事后要能对照
        item.setdefault("signals", {})
        out.append(item)
    return out
