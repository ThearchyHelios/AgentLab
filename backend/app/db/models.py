from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, new_id, utcnow

# --------------------------------------------------------------------------
# 设置：模型供应商 / 用户偏好 / MCP / 自定义工具
# --------------------------------------------------------------------------


class Provider(Base, TimestampMixin):
    """一个模型接入点。api_key 落库前经 app.core.crypto 加密。"""

    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(32))  # anthropic | openai | openai_compatible | mock
    base_url: Mapped[str | None] = mapped_column(String(500), default=None)
    api_key: Mapped[str | None] = mapped_column(Text, default=None)  # 密文
    models: Mapped[list[Any]] = mapped_column(default=list)  # [{id, label, context, pricing}]
    default_model: Mapped[str | None] = mapped_column(String(200), default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)


class DataSource(Base, TimestampMixin):
    """一个数据库接入点。password 落库前经 app.core.crypto 加密，和 Provider 同一套。

    readonly 默认为真，是刻意的安全基线：这里的 SQL 由模型生成，不是人写的。
    要写库就另建一个显式关掉只读的源，并且写操作会走人工审批——把"能写"这件事
    变成一个需要两次明确动作的决定，而不是默认状态。
    """

    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(32))  # mysql | postgres | oracle | sqlite
    host: Mapped[str | None] = mapped_column(String(255), default=None)
    port: Mapped[int | None] = mapped_column(Integer, default=None)
    database: Mapped[str | None] = mapped_column(String(255), default=None)
    username: Mapped[str | None] = mapped_column(String(128), default=None)
    password: Mapped[str | None] = mapped_column(Text, default=None)  # 密文
    # 驱动差异塞这里：Oracle 的 service_name/sid、MySQL 的 charset、SSL 参数…
    # 不给每种数据库加一列，否则表会长成各家方言的并集
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    readonly: Mapped[bool] = mapped_column(default=True)
    # 给 Copilot 看的一句话："销售库，含订单/客户/产品"。它据此判断该查哪个源
    description: Mapped[str] = mapped_column(Text, default="")
    # 探查到的表结构。缓存下来，否则每次建图都要连一次生产库
    schema_cache: Mapped[dict[str, Any]] = mapped_column(default=dict)
    schema_synced_at: Mapped[datetime | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(default=True)


class Setting(Base, TimestampMixin):
    """键值形式的用户设置，前端 Settings 页直接读写。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)


class McpServer(Base, TimestampMixin):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    transport: Mapped[str] = mapped_column(String(20), default="stdio")  # stdio | http
    command: Mapped[str | None] = mapped_column(String(500), default=None)
    args: Mapped[list[Any]] = mapped_column(default=list)
    env: Mapped[dict[str, Any]] = mapped_column(default=dict)
    url: Mapped[str | None] = mapped_column(String(500), default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    # 最近一次探活的结果与工具清单缓存
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    tools_cache: Mapped[list[Any]] = mapped_column(default=list)


class CustomTool(Base, TimestampMixin):
    """用户自定义工具：HTTP 调用模板，或跑在沙箱里的一段 Python。"""

    __tablename__ = "custom_tools"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="http")  # http | python
    parameters: Mapped[dict[str, Any]] = mapped_column(default=dict)  # JSON Schema
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)


# --------------------------------------------------------------------------
# 工作流与版本
# --------------------------------------------------------------------------


class Workflow(Base, TimestampMixin):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    graph: Mapped[dict[str, Any]] = mapped_column(default=dict)  # {nodes, edges, viewport}
    tags: Mapped[list[Any]] = mapped_column(default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_template: Mapped[bool] = mapped_column(default=False)
    # 治理状态机：draft（随便改）→ published（有正式版本可跑）→ governed（发布需过治理 lint）
    status: Mapped[str] = mapped_column(String(20), default="draft")
    # 正式运行默认解析到这个版本；为空表示还没发布过
    published_version: Mapped[int | None] = mapped_column(Integer, default=None)
    published_by: Mapped[str | None] = mapped_column(String(100), default=None)

    versions: Mapped[list["WorkflowVersion"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", lazy="selectin"
    )


class WorkflowVersion(Base, TimestampMixin):
    """每次保存留一份快照，便于回滚和对比不同编排的效果。"""

    __tablename__ = "workflow_versions"
    __table_args__ = (UniqueConstraint("workflow_id", "version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    graph: Mapped[dict[str, Any]] = mapped_column(default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    # 图内容的 sha256 指纹（剔除 viewport）。formal run 钉住的就是它。
    graph_hash: Mapped[str | None] = mapped_column(String(64), default=None)

    workflow: Mapped[Workflow] = relationship(back_populates="versions")


# --------------------------------------------------------------------------
# 运行、事件、审批
# --------------------------------------------------------------------------


class Run(Base, TimestampMixin):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), default=None
    )
    workflow_name: Mapped[str] = mapped_column(String(200), default="")
    # LangGraph checkpointer 的 thread_id —— 持久化与恢复的锚点
    thread_id: Mapped[str] = mapped_column(String(64), index=True, default=new_id)
    # queued | running | interrupted | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    graph: Mapped[dict[str, Any]] = mapped_column(default=dict)  # 执行时的图快照
    input: Mapped[dict[str, Any]] = mapped_column(default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)  # tokens / cost / 各节点耗时
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    # formal：从不可变的 WorkflowVersion 发起，可复现、可出具
    # exploratory：画布试跑 / 裸 graph / 追问，结果只标探索性
    run_class: Mapped[str] = mapped_column(String(20), default="exploratory")
    version: Mapped[int | None] = mapped_column(Integer, default=None)
    version_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    # 结束时对全部事件计算的清单哈希，事后改动事件流会被它戳穿
    manifest_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    started_by: Mapped[str | None] = mapped_column(String(100), default=None)

    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class RunEvent(Base):
    """完整事件流落库 —— 这既是 trace，也是页面刷新后重建时间线的依据。"""

    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_seq", "run_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[str] = mapped_column(String(40))
    node_id: Mapped[str | None] = mapped_column(String(64), default=None)
    ts: Mapped[float] = mapped_column(Float)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)

    run: Mapped[Run] = relationship(back_populates="events")


class Approval(Base, TimestampMixin):
    """一次人工介入请求。对应 LangGraph 的 interrupt。"""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(String(64))
    interrupt_id: Mapped[str | None] = mapped_column(String(64), default=None)
    mode: Mapped[str] = mapped_column(String(20), default="approve")  # approve | input | edit
    title: Mapped[str] = mapped_column(String(300), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)  # 给人看的上下文
    schema_: Mapped[dict[str, Any]] = mapped_column("schema", default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    response: Mapped[dict[str, Any]] = mapped_column(default=dict)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
    # 责任归属：谁批的。没有 actor 的治理是空转。
    resolved_by: Mapped[str | None] = mapped_column(String(100), default=None)


class Artifact(Base, TimestampMixin):
    """工件引用表。

    文件本体按内容哈希存在 data/artifacts/ 下（同内容只一份）；
    这张表记录"哪个 run 的哪个节点产出了它"——溯源需要的正是这层归属。
    复合主键：同一工件被多个 run 产出时各记一行引用。
    """

    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_run", "run_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), primary_key=True, default="")
    kind: Mapped[str] = mapped_column(String(32), default="node_output")  # node_output | tool_snapshot | metric_set
    node_id: Mapped[str | None] = mapped_column(String(64), default=None)
    size: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)


# --------------------------------------------------------------------------
# 记忆、知识库、Skill
# --------------------------------------------------------------------------


class MemoryItem(Base, TimestampMixin):
    """长期记忆。scope 用来隔离不同 agent / 会话的记忆空间。"""

    __tablename__ = "memories"
    __table_args__ = (Index("ix_memories_scope_kind", "scope", "kind"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(100), default="default")
    kind: Mapped[str] = mapped_column(String(32), default="fact")  # fact | preference | episode
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    use_count: Mapped[int] = mapped_column(Integer, default=0)


class Document(Base, TimestampMixin):
    """知识库里的一份原始文档。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    collection: Mapped[str] = mapped_column(String(100), default="default", index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    source: Mapped[str] = mapped_column(String(500), default="")  # 文件名 / URL
    mime: Mapped[str] = mapped_column(String(100), default="text/plain")
    content: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunks_collection", "collection"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    collection: Mapped[str] = mapped_column(String(100), default="default")
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)

    document: Mapped[Document] = relationship(back_populates="chunks")


class Skill(Base, TimestampMixin):
    """可复用的方法论：一段指令 + 可选的示例和推荐工具。

    节点可以挂载 skill，运行时把内容注入 system prompt —— 把"怎么做事"
    从工作流结构里抽出来单独管理和迭代。
    """

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    examples: Mapped[list[Any]] = mapped_column(default=list)
    suggested_tools: Mapped[list[Any]] = mapped_column(default=list)
    tags: Mapped[list[Any]] = mapped_column(default=list)
    enabled: Mapped[bool] = mapped_column(default=True)


# --------------------------------------------------------------------------
# 会话：把一串问答串成一次对话
# --------------------------------------------------------------------------


class Conversation(Base, TimestampMixin):
    """一次对话。

    在此之前"对话"只是前端内存里的一个数组：刷新页面就没了，而每一轮又各自
    从零建图、各起一条 checkpoint 线程，所以"上一轮问过什么"在系统里根本
    无处可查。追问「再按月份拆一下」会重新找一遍数据源，可能接到别的表。

    落成一张表之后，它同时承担两件事：给用户看的历史列表，和给模型看的上下文。

    kind 分两种，因为这两件事像但不是一回事：
      chat   —— 问数据页，一轮 = 一次「建图 → 跑图 → 出答案」，进左侧列表
      canvas —— 画布右栏的 Copilot，依附某张图，一轮 = 一次改图，不进列表
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(20), default="chat", index=True)
    workflow_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), default=None, index=True
    )
    archived: Mapped[bool] = mapped_column(default=False)
    # 列表按活跃度排。不能用 updated_at —— 改个标题就会把这条顶到最前面，
    # 而用户对"最近聊过的"的预期是按说话时间排，不是按编辑时间
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    turns: Mapped[list["ConversationTurn"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationTurn.seq",
    )


class ConversationTurn(Base, TimestampMixin):
    """一轮问答。

    run_id 可以为空：建图阶段就失败时压根没有 run，但这一轮仍然发生过，
    用户也该在历史里看到它失败了——只记成功的轮次，历史就成了一份美化过的
    记录，而人再回来时最想搞清楚的恰恰是上次卡在哪。
    """

    __tablename__ = "conversation_turns"
    __table_args__ = (Index("ix_conversation_turns_conv_seq", "conversation_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    # Copilot 对这张图的说明。画布那条路径上只有它，没有 answer
    explanation: Mapped[str] = mapped_column(Text, default="")
    graph: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), default=None
    )
    # running | done | error
    status: Mapped[str] = mapped_column(String(20), default="running")
    error: Mapped[str] = mapped_column(Text, default="")

    conversation: Mapped[Conversation] = relationship(back_populates="turns")
