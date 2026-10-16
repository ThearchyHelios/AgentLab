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
    # 审批质询：运行停在审批时，审批人就待审的报告问的一个问题和得到的回答（engine/run_append.py 追加，
    # node_id 为空）。这次问答的用量写在事件数据的 usage 里，计进运行用量（runner._usage_of_events）。
    # 停在审批时追加的随运行结束一起封存；封存之后追加的带 post_seal: true，是批注不是证据
    APPROVAL_INQUIRY = "approval.inquiry"

    # 出具与口径治理
    ISSUANCE = "issuance"  # 出具契约的判定结果（档位 / 缺数据 / 未回指数字）
    CALIBER_UPGRADE = "caliber.upgrade"  # 钉住的方法卡有新版本，按声明的策略处置
    # 从发布版本发起的正式运行：发布之后这一版用到的表的数据目录有变化（data/catalog_impact.catalog_drift）。
    # 每张表带 impact：direct 写死的 SQL 用到的，possible Agent 可能查询的。
    # 只提醒、不拦运行；落在封存范围内，出具物上看得到「当时的目录和发布时不一样」
    CATALOG_DRIFT = "catalog.drift"
    # 从发布版本发起的正式运行：发布之后这一版直接引用的表的结构版本变了（data/catalog_impact.schema_drift，next-round
    # 03 B1）。每张表带发布时和现在的结构版本、是否已删除、还没处理的结构变化。和 catalog.drift 分开记（老的渲染只认整数的
    # 目录版本），界面上合到同一块横幅里。只提醒、不拦运行；落在封存范围内
    SCHEMA_DRIFT = "schema.drift"
    # 报告撰写节点的自查结果：报告文档工件 id、统计、违规清单。落在封存范围内，
    # 证据接口靠它找到文档——不去翻不受封存保护的 artifacts 表
    REPORT_CHECKED = "report.checked"
    # 探索运行里按需裁判的结论句判定：点开哪句才判哪句，追加在封存之后（post_seal），是批注不是证据。
    # 封存核对只覆盖 manifest_seq 之前的事件，追加它不影响 verify；封存的报告文档一个字都不改
    EVIDENCE_JUDGED = "evidence.judged"
    # 口径账本（docs/design/next-round/02-口径账本.md 做法 2）：探索运行封存之后，后台拿这次执行的 SQL 和已发布口径的取数比对，
    # 结果存成内容寻址工件（kind=caliber_match，格式 agentlab.caliber_match/1），再追加这一条：{artifact, manifest_hash,
    # algo, counts, trigger}。追加在封存之后（post_seal），是批注不是证据：比对依据的已发布口径在运行之外，不能混进运行
    # 自身的封存事实；封存核对照旧一致
    LEDGER_MATCHED = "ledger.matched"
    # 口径召回（02 做法 7）：口径发布新版本后，在同一份数据上把新旧两版各算一次（召回运行，origin_kind=recall）。召回运行
    # 结束时在它自己身上记一条结论 {role: recall, changed, workflow_id, from_version, to_version, metrics, answers, tasks}；
    # 数没变的那几个原答案的运行上各记一条 {role: answer, changed: false, recall_run, workflow_id, from_version,
    # to_version}，界面上写「已按 vN 核对，不影响」（数变了的开 recall 待办，不在原答案上记）。同 ledger.matched：追加在
    # 封存之后（post_seal），是批注不是证据，不进封存范围
    LEDGER_RECALLED = "ledger.recalled"
    # 答案依据的东西变了（02 做法 7 二、三）：kind=catalog 目录里影响数字的一项被驳回或改了（冻结的目录里看得到这一项）；
    # structure 用到的表或列被删、列的类型变了；values 触及有效记录条件的新码值；catalog_edit 处理变化时改了目录。带数据源、
    # 表、哪几项（路径和叫法），按新目录跑 SQL 检查新出现的错误级问题条数。同上，封存之后追加，不进封存范围
    LEDGER_BASIS_CHANGED = "ledger.basis_changed"
    # 对账结果上的「说明有用 / 看不懂」（02 做法 10 的 E2，ledger/feedback）：追加在对账运行（origin_kind=reconcile）上，
    # {verdict: useful | unclear, by, verified, source_run}。同一个人改主意再记一条，统计只算他最后一次。同上，封存之后
    # 追加，是批注不是证据
    LEDGER_FEEDBACK = "ledger.feedback"

    # 应用平台（docs/design/app-platform-and-look.md §3.11）
    # 运行来源：发起时写进 launch_run 的 notes，在封存范围内（engine/origin.py）
    RUN_ORIGIN = "run.origin"
    # 待办：一次中断面向人的那一面（app/process/tasks.py）
    TASK_OPENED = "task.opened"
    TASK_RESPONDED = "task.responded"  # 谁、怎么决定的、第几人 / 共几人
    TASK_REASSIGNED = "task.reassigned"
    TASK_REMINDED = "task.reminded"
    TASK_ESCALATED = "task.escalated"
    TASK_EXPIRED = "task.expired"
    TASK_RESOLVED = "task.resolved"
    TASK_CANCELLED = "task.cancelled"
    # 动作：写外部系统（app/process/actions.py）。prepared 带 payload_hash、环境、遮罩后的预览
    ACTION_PREPARED = "action.prepared"
    ACTION_SENT = "action.sent"
    ACTION_CONFIRMED = "action.confirmed"
    ACTION_FAILED = "action.failed"
    ACTION_UNKNOWN = "action.unknown"
    ACTION_RECONCILED = "action.reconciled"
    ACTION_SIMULATED = "action.simulated"

    # 普通用户建应用（docs/design/normal-user-apps.md N9、§3.8）
    # 读入一个附件的文字（模板函数 file_text，app/files/text.py）：哪个文件、读入多少字、全文多少字、是否截断，
    # 读入的那一段存成工件（artifact）。抽不出时 ok=false、reason 写原因
    FILE_EXTRACTED = "file.extracted"

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
