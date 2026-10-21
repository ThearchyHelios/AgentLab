"""测试用的分析算子替身：入口（agent 分析工具、分析卡、口径卡的可比性挂钩）的测试用它断言入口交给算子的东西、算子交回后
入口做的事，不受算子的判定细节牵连。接真算子的端到端见 tests/test_analysis_e2e*.py。

FakeOps 换掉模块上的 app.analysis.run（入口都按模块取 run），按请求交回 app.analysis.testing.fake_result 的假结论，
并记下每次调用拿到的请求、按名字解析到的输入快照、阈值、运行期依赖、上一层结论——测试据此断言入口交进去的东西。
"""
from __future__ import annotations

from typing import Any

import app.analysis as analysis_ops
from app.analysis.testing import fake_result

#: 每个算子缺省给的判定（fake_result 有现成判定语的那几个）
VERDICT = {"decompose": "driver", "baseline": "outside_band", "trend": "rising", "differ": "no_evidence",
           "comparable": "comparable"}


class FakeOps:
    """errors：按调用的先后，每次要抛的异常（None 表示照常交回）；用完了都照常交回。statuses / verdicts 同理。"""

    def __init__(self, monkeypatch, *, errors: list[Any] | None = None, statuses: list[str] | None = None,
                 verdicts: list[str] | None = None, peek: list[str] | None = None, **overrides: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        #: 每次调用时看一眼这几个名字能不能按 parents 取到上一层结论（visible）
        self.peek = list(peek or [])
        self.errors = list(errors or [])
        self.statuses = list(statuses or [])
        self.verdicts = list(verdicts or [])
        self.overrides = overrides
        monkeypatch.setattr(analysis_ops, "run", self.run)

    async def run(self, request: dict[str, Any], resolve: Any, *, settings: Any, context: Any,
                  parents: Any = None, refuse_data_errors: bool = False) -> dict[str, Any]:
        params = request.get("params") or {}
        names = [params[k] for k in ("query", "base_query", "target_query") if params.get(k)]
        snapshots = {name: resolve(name) for name in names}
        parent = None
        if isinstance(params.get("parent"), dict) and parents is not None:
            parent = parents(params["parent"]["of"])
        self.calls.append({"request": request, "snapshots": snapshots, "settings": dict(settings), "context": context,
                           "parent": parent, "parents": parents, "refuse_data_errors": refuse_data_errors,
                           "visible": {n: parents is not None and parents(n) is not None for n in self.peek}})
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        status = self.statuses.pop(0) if self.statuses else "ok"
        verdict = self.verdicts.pop(0) if self.verdicts else VERDICT[request["op"]]
        roles = {"query": "data", "base_query": "base", "target_query": "target"}
        inputs = [{"ref": name, "artifact": snap["artifact"], "role": roles[key], "columns": snap["columns"],
                   "rows": "all"}
                  for key in ("query", "base_query", "target_query")
                  if (name := params.get(key)) and (snap := snapshots.get(name)) is not None]
        if parent is not None:
            from app.core.artifact_store import canonical_json, content_hash

            inputs.append({"ref": params["parent"]["of"], "artifact": content_hash(canonical_json(parent)),
                           "role": "parent"})
        return fake_result(op=request["op"], mode=request.get("mode"), verdict=verdict, status=status,
                           reason_code="history_short" if status == "refused" else None, inputs=inputs,
                           params=dict(params), settings=dict(settings), hypothesis=request.get("hypothesis"),
                           **self.overrides)
