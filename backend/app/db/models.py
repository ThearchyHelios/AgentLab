from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, new_id

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
