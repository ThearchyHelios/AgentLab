"""确定性分析算子（docs/design/analysis-round/）：变化拆解、波动基线、趋势、差异检验、可比性检查。

入口只有两个函数，agent 分析工具（tools/builtin）和分析卡（engine/nodes/analysis.py）都只经它们调用：

- run：按请求算一份分析结论。resolve 按调用里写的名字（Q3、来源节点 id）取回输入快照；单元格写成「名字.r行.列」
  （Q1.r0.gmv），由这里拆开再经 resolve 取快照。parents 按名字（A3、分析卡里的项 id）取回上一层拆解，下钻用。
  调用写错抛 contract.CallError；算不了返回 status refused 的结论。唯一的异步来自 context["probe"]（现查表的日期范围）。
- recompute：按结论里记下的 params、settings、probes 和输入快照（按 sha256 取回）重算一遍，不现查；和封存的用
  contract.same 比。

契约（名字、形状、缺省阈值）在 contract.py。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.analysis.contract import AnalysisRequest, AnalysisResult, Context, InputSnapshot

#: 按调用里写的名字取回输入快照；没有这个名字返回 None
Resolver = Callable[[str], InputSnapshot | None]
#: 按名字取回上一层的分析结论（下钻）；没有返回 None
ParentResolver = Callable[[str], AnalysisResult | None]


async def run(request: AnalysisRequest, resolve: Resolver, *, settings: Mapping[str, Any], context: Context,
              parents: ParentResolver | None = None) -> AnalysisResult:
    """算一份分析结论。settings 是 contract.effective_settings 算好的阈值。"""
    raise NotImplementedError("analysis.run：第 1 波 W1-a 实现")


def recompute(result: AnalysisResult, load: Callable[[str], InputSnapshot | None], *,
              load_result: Callable[[str], AnalysisResult | None] | None = None) -> AnalysisResult:
    """复算：load 按 sha256 取回输入快照，load_result 按 sha256 取回 parent 那份结论。"""
    raise NotImplementedError("analysis.recompute：第 1 波 W1-a 实现")


__all__ = ["ParentResolver", "Resolver", "recompute", "run"]
