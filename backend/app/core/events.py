from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """前端与后端之间的实时协议。画布高亮、时间线、token 流全部基于这套事件。"""

    # run 级
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_INTERRUPTED = "run.interrupted"  # 等待人工介入
    RUN_RESUMED = "run.resumed"

    # 节点级
    NODE_STARTED = "node.started"
    NODE_FINISHED = "node.finished"
    NODE_FAILED = "node.failed"
    NODE_SKIPPED = "node.skipped"
    EDGE_TAKEN = "edge.taken"  # 条件分支实际走了哪条边

    # 模型级
    LLM_START = "llm.start"
    LLM_TOKEN = "llm.token"
    # 思考的流式增量：只推给画布做实时显示，不落库
    LLM_THINKING_DELTA = "llm.thinking.delta"
    # 一次完整思考：调用结束时落一条，这才是进轨迹的事件
    LLM_THINKING = "llm.thinking"
    LLM_END = "llm.end"

    # 工具 / 沙箱
    TOOL_START = "tool.start"
    TOOL_END = "tool.end"
    TOOL_ERROR = "tool.error"
    # 信任档是「始终允许 · 门控把关」的工具，门控模型对这一次调用的判定（tools/trust.py）。
    # allow 之后是正常的 tool.start；escalate 之后是人工审批，协作团队里则是不执行
    TOOL_GATED = "tool.gated"
    # 知识检索。以前只发一条 info 日志，于是"这句结论依据的是哪份文档的哪一段"
    # 在轨迹里查不到——而 SQL 取数那条路早就有工件快照可以下钻
    RETRIEVE_END = "retrieve.end"
    # 记忆读写。以前发的是 info 日志，而解码器丢弃所有 info——于是往长期记忆里
    # 写东西这件事在界面上是隐形的。让 Copilot 主动记之后，这条不可接受
    MEMORY_END = "memory.end"
    # 合并查询做完了：哪几个输入（别名、节点、行数、数据源）、哪条合并 SQL、得到几行、有哪些警告。
    # 运行详情据此展示这一步；完整结果在 query_artifact 指向的查询快照里
    MERGE_END = "merge.end"
    SANDBOX_START = "sandbox.start"
    SANDBOX_END = "sandbox.end"

    # 多 agent 协作中的单个 agent 步骤
    AGENT_STEP_START = "agent.step.start"
    AGENT_STEP_END = "agent.step.end"
    # 调度者的一次决策。它是一次结构化模型调用，常常占掉团队节点大半的时间；
    # 以前前后都没有事件，界面上那几十秒是黑盒
    AGENT_ROUTE_START = "agent.route.start"
    AGENT_ROUTE_END = "agent.route.end"

    # 人工介入
    HUMAN_REQUESTED = "human.requested"
    HUMAN_RESOLVED = "human.resolved"

    # 出具与口径治理
    ISSUANCE = "issuance"  # 出具契约的判定结果（档位 / 缺数据 / 未回指数字）
    CALIBER_UPGRADE = "caliber.upgrade"  # 钉住的方法卡有新版本，按声明的策略处置
    # 从发布版本发起的正式运行：发布之后这一版 SQL 用到的表的数据目录有变化（data/catalog_impact.catalog_drift）。
    # 只提醒、不拦运行；落在封存范围内，出具物上看得到「当时的目录和发布时不一样」
    CATALOG_DRIFT = "catalog.drift"
    # 报告撰写节点的自查结果：报告文档工件 id、统计、违规清单。落在封存范围内，
    # 证据接口靠它找到文档——不去翻不受封存保护的 artifacts 表
    REPORT_CHECKED = "report.checked"
    # 探索运行里按需裁判的结论句判定：点开哪句才判哪句，追加在封存之后（post_seal），是批注不是证据。
    # 封存核对只覆盖 manifest_seq 之前的事件，追加它不影响 verify；封存的报告文档一个字都不改
    EVIDENCE_JUDGED = "evidence.judged"

    # 其他
    LOG = "log"
    USAGE = "usage"  # token / 成本累计


class RunEventModel(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    seq: int = 0
    type: EventType
    node_id: str | None = None
    ts: float = Field(default_factory=time.time)
    data: dict[str, Any] = Field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
