from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime, new_id, utcnow

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
    # 最近一次测连接：{at, ok, latency_ms, error}。记在对象上，换台浏览器也看得到
    last_check: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # upload：上传表格建的源，连接信息由系统维护（data/table_versions.py），不许手改；
    # manual：手工登记的连接。按这个字段判断，而不是猜 database 的路径
    origin: Mapped[str] = mapped_column(String(20), default="manual")
    # 上传源当前启用的快照（source_snapshots.id）。查询、探查都从快照解析，
    # database 只作显示；手工源恒为 None
    current_snapshot_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # 按配方导入的源当前启用的配方（table_recipes.id）。非空即「按配方导入」：重传走「上传新一期」，
    # 旧的简单上传对它拒收。简单导入的上传源和手工源恒为 None
    current_recipe_id: Mapped[str | None] = mapped_column(String(32), default=None)


# --------------------------------------------------------------------------
# 上传表格的版本：构建 → 导入记录 → 快照（data/table_versions.py）
# --------------------------------------------------------------------------


class TableBuild(Base):
    """一次解析的产物：同一个源、同一份原件、同一组解析选项、同一版解析器，只建一次库。

    id 是这四样的 sha256 全长，source_id 算在里面：两个源传同一个文件得到两个构建，
    互不引用——一个源删掉、回收，不会连带另一个源的库。库文件写好后是只读的（0444），
    之后不再改动；回收时删掉文件、记录留着（retired_at）备查。
    """

    __tablename__ = "table_builds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(32), index=True)
    raw_sha256: Mapped[str] = mapped_column(String(64), default="")
    options_sha256: Mapped[str] = mapped_column(String(64), default="")
    engine_ver: Mapped[str] = mapped_column(String(40), default="")
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    db_path: Mapped[str] = mapped_column(Text, default="")
    db_sha256: Mapped[str] = mapped_column(String(64), default="")
    # 解析回执（tabular.LoadReport）：区域、跳过的工作表、类型转换、警告
    report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # 库文件被回收的时刻。为空表示文件还在
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class TableImport(Base):
    """一次上传。重传同一个文件也是一条新记录（指向同一个构建）：谁、何时、传了什么，都要留痕。

    不挂外键：数据源删掉以后，导入记录置 retired 留作审计，不跟着删。
    """

    __tablename__ = "table_imports"
    __table_args__ = (Index("ix_table_imports_source_seq", "source_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    build_id: Mapped[str] = mapped_column(String(64), default="")
    file_name: Mapped[str] = mapped_column(String(500), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    raw_sha256: Mapped[str] = mapped_column(String(64), default="")
    # kept：原件在 uploads/raw 里；purged：清除过（purged 里记时间、署名、理由）；
    # absent：从来没有原件（迁移前的老上传只剩 .db）
    raw_state: Mapped[str] = mapped_column(String(20), default="kept")
    # active：当前快照里的那一期；superseded：被新上传替换、文件还在；retired：已回收或源已删除
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    purged: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)

    # 以下是按配方导入（期 2）才有的列，简单导入一律为空。状态仍只有 active / superseded / retired：
    # 没发布的尝试记在 import_stagings 上，不进这张表——回收的保护集合、seq 的递增、导入列表都按
    # 这张表算，塞进未发布的尝试会同时扰动这三处
    recipe_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 统计期（YYYY-MM-DD）。期 3 按期累积时按它排序
    period_start: Mapped[str | None] = mapped_column(String(10), default=None)
    period_end: Mapped[str | None] = mapped_column(String(10), default=None)
    # 统计期的来源与原文（PeriodOut）
    context: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 核对摘要（id、状态、核对数、不一致数、无法核对数、原因）。含 C1、C2：它们取决于文件名，
    # 而构建按内容复用、不含文件名，所以不能放在构建回执里
    checks: Mapped[list[Any] | None] = mapped_column(default=None)
    # 数据质量类的接受、无法核对的接受（各带理由）
    overrides: Mapped[list[Any] | None] = mapped_column(default=None)
    waivers: Mapped[list[Any] | None] = mapped_column(default=None)
    # 本次提交时勾过的确认项（id、label、at）
    confirmations: Mapped[list[Any] | None] = mapped_column(default=None)
    # 导入清单的工件 id。只作索引：证据链经快照 schema_cache 里的 import_manifests 承诺（内容寻址），
    # 不经过这个可改的列
    manifest_artifact: Mapped[str | None] = mapped_column(String(64), default=None)
    # 署名（自报，未认证）
    signed_by: Mapped[str | None] = mapped_column(String(100), default=None)
    staging_id: Mapped[str | None] = mapped_column(String(32), default=None)


class TableRecipe(Base):
    """按配方导入的源的一版配方。同一个源从 1 起编号，只增不改：改配方就是新的一版。

    recipe 存去掉默认值的紧凑形式（recipe.canonical_recipe），recipe_sha256 由它算，进构建 id。
    不挂外键：数据源删掉以后配方置 retired 留作审计，导入清单里引用的配方 id 仍查得到。
    """

    __tablename__ = "table_recipes"
    # 同源的 seq 不重复：发布在版本存储的锁里取「最大 seq + 1」，这条约束兜住锁外误用
    __table_args__ = (UniqueConstraint("source_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(String(32), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    recipe: Mapped[dict[str, Any]] = mapped_column(default=dict)
    recipe_sha256: Mapped[str] = mapped_column(String(64), default="")
    # 配方格式版本，与 recipe_types.RECIPE_FORMAT 一致（这里不 import 数据层，测试核对两边相等）
    recipe_format: Mapped[str] = mapped_column(String(40), default="agentlab-recipe/2")
    # rules：规则起草；ai：AI 起草；manual：手工；mixed：起草后又改过
    origin: Mapped[str] = mapped_column(String(20), default="manual")
    # active：现行；superseded：被新的一版替换；retired：数据源已删除
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    # 确认时勾过的确认项（id、label、at）
    confirmations: Mapped[list[Any]] = mapped_column(default=list)
    # 署名（自报，未认证）
    signed_by: Mapped[str | None] = mapped_column(String(100), default=None)
    # {"calls": [AiUsage], "consents": [同意记录]}；不是 AI 起草的为空
    ai_usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    staging_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class ImportStaging(Base, TimestampMixin):
    """一次还没发布的配方导入：暂存、起草、试运行都在这里，提交成功时才影响数据源。

    「拒收、放弃、过期都不动当前快照」由此天然成立：在提交那个事务之前，这里的任何东西都不碰
    数据源、导入记录和快照。新源在暂存时就分配 source_id，提交时用它建 DataSource。

    **结束即瘦身**（table_versions.close_staging）：进入 committed / discarded / expired 的同一个事务里，
    网格预览（有数字格的值、隐藏行列）、试运行回执（有区域外文字全文）这些大字段一律清空、试运行库
    删掉，只留 id、来源、文件哈希、状态、工作配方及其哈希、用量、同意记录、署名和时间。接口没有认证，
    这些内容不该在用完之后还留在库里。结束超过 90 天的行由回收删除。
    """

    __tablename__ = "import_stagings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(String(32), index=True)
    source_name: Mapped[str] = mapped_column(String(100), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # first：首次导入；reupload：上传新一期；redraft：修改配方（不换文件）；switch：从简单导入切换
    kind: Mapped[str] = mapped_column(String(20), default="first")
    # 未结束：drafting / trialed / rejected；已结束：committed / discarded / expired
    status: Mapped[str] = mapped_column(String(20), default="drafting", index=True)
    # 原件按内容存档（raw_store）。未结束的暂存区保护它不被回收；清除原件时按它找到要一并放弃的暂存区
    raw_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    file_name: Mapped[str] = mapped_column(String(500), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    # 以下几项结束即清空
    scan: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    grid_preview: Mapped[list[Any] | None] = mapped_column(default=None)
    facts: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # {"rules": Draft 或 null, "ai": AI 起草结果的摘要或 null}
    drafts: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 工作配方
    recipe: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    recipe_origin: Mapped[str | None] = mapped_column(String(20), default=None)
    recipe_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    # 回答问题的起点配方。工作配方 = 它按问题顺序重放全部回答；PUT 配方、AI 草稿会换掉它并清空回答
    answers_base: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # {question_id: {"value": 选项值, "reason": 理由或 null}}
    answers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    cards: Mapped[list[Any] | None] = mapped_column(default=list)
    questions: Mapped[list[Any] | None] = mapped_column(default=list)
    # 最近一次静态校验
    recipe_problems: Mapped[list[Any]] = mapped_column(default=list)
    # 最近一次干跑的问题、是否只在前若干行上干跑
    draft_problems: Mapped[list[Any] | None] = mapped_column(default=list)
    draft_partial: Mapped[bool] = mapped_column(default=False)
    # 起点配方（上传新一期、修改配方时是现行配方）
    base_recipe_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 试运行时源的当前快照；提交时在版本存储的锁里核对它没变
    base_snapshot_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # {"统计期": {"start", "end", "signed_by"}}：人工录入
    context_inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 最近一次试运行的回执（含 extraction、checks 的整份 JSON，提交时据此还原）
    trial: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 试运行库的键和路径。库被发布消耗或删除后两者都清空
    trial_key: Mapped[str | None] = mapped_column(String(64), default=None)
    trial_path: Mapped[str | None] = mapped_column(Text, default=None)
    # 每次调用模型的用量；用户同意发送的记录 {signed_by, at, model, provider, compressed_sha256, chars}
    ai_usage: Mapped[list[Any]] = mapped_column(default=list)
    ai_consents: Mapped[list[Any]] = mapped_column(default=list)
    # 署名（自报，未认证）
    signed_by: Mapped[str | None] = mapped_column(String(100), default=None)
    committed_import_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 过期时刻（创建后 table_versions.STAGING_TTL）。为空时按 created_at 加 TTL 算
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # 进入 committed / discarded / expired 的时刻
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class SourceSnapshot(Base):
    """数据源的一个可查询版本：若干期导入合成的一个库，加上冻结的表结构。

    期 1 只有「每期替换」：一个快照就是一期导入，db_path 直接用那次构建的库。
    运行钉的是快照 id（runs.data_versions），表结构和数据出自同一个版本。
    """

    __tablename__ = "source_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(32), index=True)
    imports: Mapped[list[Any]] = mapped_column(default=list)  # import id 列表
    db_path: Mapped[str] = mapped_column(Text, default="")
    db_sha256: Mapped[str] = mapped_column(String(64), default="")
    schema_cache: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # 没有运行、也不是当前版本时由回收标上；之后钉着它的请求明确报「版本不存在」
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


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
    # 最近一次连接的时刻和耗时（status / last_error 只有结论，没有「何时、多快」）
    last_check: Mapped[dict[str, Any] | None] = mapped_column(default=None)


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
    # 这一版按哪一档发布的：published | governed。没发布过、或者是加这一列之前发布的为空。
    # 正式运行按不按受管出具看它，而不是工作流的 status——status 说的是当前画布，改一笔就退回 draft
    level: Mapped[str | None] = mapped_column(String(20), default=None)

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
    # 清单封到了哪一条事件（含）。之后追加的事件（复核批注之类）不在封存范围内；
    # 为空的是记录这一列之前封存的老运行，核对时按当时的口径算
    manifest_seq: Mapped[int | None] = mapped_column(Integer, default=None)
    started_by: Mapped[str | None] = mapped_column(String(100), default=None)
    # 第一次发起时实际生效的记忆域、知识库、工具审批默认值。恢复和接着跑沿用它们，
    # 而不是回落到 "default"——同一次运行前后两段必须查同一个库、守同一道门
    memory_scope: Mapped[str | None] = mapped_column(String(100), default=None)
    collection: Mapped[str | None] = mapped_column(String(100), default=None)
    approval_default: Mapped[str | None] = mapped_column(String(20), default=None)
    # MCP / 自定义工具信任三档在发起时的快照（tools/trust.py）。None 是升级前发起的运行：
    # 那时这两类工具不问人，恢复时也不能开始问，否则审批的答复会对错号
    tool_trust: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # agent 护栏的上限在发起时的快照（engine/guards.py）。None 是升级前发起的运行，走旧逻辑
    agent_limits: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 失败时能定位到的节点。报错要能落到画布上的那张卡片，而不只是一句话
    error_node_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # 发起时钉住的上传表格版本：{source_id: {"snapshot": 快照 id, "name": 源名}}。续跑、恢复沿用，
    # 回收（table_versions.gc）不删任何运行引用的快照。None 是升级前发起的运行
    data_versions: Mapped[dict[str, Any] | None] = mapped_column(default=None)

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
    # 这条向量是谁产出的。没有它就无法判断存量向量还能不能和当前查询比对——
    # 换 embedder 之后维度对不上，cosine 直接 ValueError（512 vs 1536），
    # 而在此之前代码里没有任何地方能看出"这条是旧模型建的"
    embed_model: Mapped[str] = mapped_column(String(100), default="")
    embed_dim: Mapped[int] = mapped_column(Integer, default=0)
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
    # 切块+算向量是在后台跑的：一个 10MB 文档配上远端 embedding 要几十分钟，
    # 压在 HTTP 请求里必然超时——而后端其实还在跑，界面显示失败、数据其实成功，
    # 比直接拒绝还糟。ready | processing | failed
    status: Mapped[str] = mapped_column(String(20), default="ready")
    error: Mapped[str] = mapped_column(Text, default="")

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
    # 这条向量是谁产出的。没有它就无法判断存量向量还能不能和当前查询比对——
    # 换 embedder 之后维度对不上，cosine 直接 ValueError（512 vs 1536），
    # 而在此之前代码里没有任何地方能看出"这条是旧模型建的"
    embed_model: Mapped[str] = mapped_column(String(100), default="")
    embed_dim: Mapped[int] = mapped_column(Integer, default=0)
    #: 分词后的 token 数。BM25 要按文档长度归一，存下来省得为算 avg_len 再扫一遍
    token_len: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)

    document: Mapped[Document] = relationship(back_populates="chunks")


class ChunkTerm(Base):
    """倒排表：词 → 包含它的片段。

    在此之前每次检索都 `select(Chunk)` 全量载入，再在 Python 里**重建**一遍
    BM25 倒排——几百段无感，上万段就是秒级，而那正是知识库开始有用的规模。

    没用 SQLite 的 FTS5：它的 trigram 分词器要求查询至少三个字符，中文里
    「商家」「订单」「权限」这些两字词一个都召不回（实测）。而现有的分词器
    本来就做中文单字 + 二元组，自己存一张倒排表既保住召回，也不把知识库
    锁死在 SQLite 上。
    """

    __tablename__ = "chunk_terms"
    __table_args__ = (Index("ix_chunk_terms_lookup", "collection", "term"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), index=True
    )
    collection: Mapped[str] = mapped_column(String(100), default="default")
    term: Mapped[str] = mapped_column(String(64))
    tf: Mapped[int] = mapped_column(Integer, default=1)


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
    last_active_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

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
    # 跑完之后的复核结论（engine/review.py）：判定档位、给用户的异常说明、
    # 命中的信号清单。落库是因为它是答案的一部分——刷新页面后只剩一个
    # 看起来很完整的答案、而"它哪里不可靠"没了，比不复核更糟
    review: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 这一轮的可信度元数据（出具档位、运行类别、未查库、结局、耗时、查库次数…），
    # 前端写、前端读。单独一列：塞在 review 里的话，没复核过的轮次 review 也不为空
    meta: Mapped[dict[str, Any] | None] = mapped_column(default=None)

    conversation: Mapped[Conversation] = relationship(back_populates="turns")
