from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, current_owner, TimestampMixin, UTCDateTime, new_id, utcnow

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
    # 期 3：作废接受（界面叫「撤回这一期」）的记录 {at, reason, signed_by, signed_by_verified: false}。
    # 导入记录本身不删、接受内容不改（H4、H5），只在这里记下作废；含作废导入的快照不能再启用
    revoked: Mapped[dict[str, Any] | None] = mapped_column(default=None)


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
    # 期 3：已应用的修改（修复、框选）。每条含撤销用的 before（answers_base、recipe_origin）、base_sha256_after、
    # superseded；接口不给 before。结束时瘦身清空，提交前先抄进导入清单（含已被覆盖的，作为历史）
    edits: Mapped[list[Any] | None] = mapped_column(default=list)
    # 期 3：改配方后丢弃的回答 [{id, text, reason: changed | gone}]（遗留项 2）。结束时清空
    answers_dropped: Mapped[list[Any] | None] = mapped_column(default=list)
    # 期 3：最近一次「按规则重新起草」给出的对齐配方的哈希。PUT recipe 带 origin=rules_redraft 时核对它；结束时清空
    redraft_sha256: Mapped[str | None] = mapped_column(String(64), default=None)


class SourceSnapshot(Base):
    """数据源的一个可查询版本：若干期导入合成的一个库，加上冻结的表结构。

    「每期替换」：一个快照就是一期导入，db_path 直接用那次构建的库。「按期累积」（期 3）：imports 是
    各期导入记录、按统计期排序，两期及以上（或一期但那一期的配方不是目标配方）时 db_path 指向物化出的
    并集库（tables/<源>/snapshots/<并集 id>.db，并集也登记成一个构建）。
    运行固定的是快照 id（runs.data_versions），表结构和数据出自同一个版本。
    """

    __tablename__ = "source_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(32), index=True)
    imports: Mapped[list[Any]] = mapped_column(default=list)  # import id 列表（按统计期排序）
    db_path: Mapped[str] = mapped_column(Text, default="")
    db_sha256: Mapped[str] = mapped_column(String(64), default="")
    schema_cache: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # 没有运行、也不是当前版本时由回收标上；之后钉着它的请求明确报「版本不存在」
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # 以下是期 3 的新列，存量行一律 NULL，含义是「期 3 之前的」：
    # replace / accumulate；NULL 按 replace 处理
    mode: Mapped[str | None] = mapped_column(String(20), default=None)
    # 快照库按哪一版配方的表结构建的；NULL 时取 imports 最后一期的 recipe_id，简单导入为 NULL
    recipe_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 最近一次成为当前快照的时刻；NULL 按 created_at。回收按它保留最近 SNAPSHOT_KEEP 个
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # 快照清单的工件 id（物化过的快照才有）。只作索引：证据链经 schema_cache 的 snapshot_manifest 承诺
    manifest_artifact: Mapped[str | None] = mapped_column(String(64), default=None)


class SnapshotActivation(Base):
    """数据源的当前快照每切换一次记一行：提交、启用旧版本（回滚）、移除某一期、作废接受。

    快照记录只有「最近一次启用」的时刻；谁在什么时候、为什么把当前版本切到哪里，要留完整的历史
    （署名自报、未认证，照实标注）。不挂外键：数据源删掉以后照样留作审计。
    """

    __tablename__ = "snapshot_activations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(String(32), index=True)
    snapshot_id: Mapped[str] = mapped_column(String(64))
    previous_snapshot_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # commit / activate / remove_period / revoke_acceptance
    kind: Mapped[str] = mapped_column(String(20), default="commit")
    import_id: Mapped[str | None] = mapped_column(String(32), default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    signed_by: Mapped[str | None] = mapped_column(String(100), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --------------------------------------------------------------------------
# 数据目录（data/catalog.py）
# --------------------------------------------------------------------------


class CatalogNote(Base, TimestampMixin):
    """一张表的业务目录：中文名、粒度、业务主键、业务日期、列的含义 / 单位 / 度量类型 / 码值、表间关系。

    只记数据事实，不放计算公式（公式在口径卡里）。notes 的结构和每一项的来源、状态见 data/catalog.py。

    一表一行而不是一项一行：起草、审阅、冻结进表结构快照都以表为单位，版本号也按表走——两个人改
    同一个源的不同表互不冲突，改同一张表时后提交的被乐观锁拦下。

    挂外键、级联删除：目录是数据源的附属说明，源删掉了留着没有意义（和导入记录不同，后者要留作审计）。
    运行用过的那一版目录已经随表结构快照冻结进证据，不靠这张表回溯。
    """

    __tablename__ = "catalog_notes"
    __table_args__ = (UniqueConstraint("source_id", "table_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), index=True)
    # 与 schema_cache["tables"] 的键一致（不带 schema 前缀）
    table_name: Mapped[str] = mapped_column(String(255))
    notes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 从 1 起，每次内容有变化的写入加 1；写入方必须带上读到的版本（data/catalog.write_entry）
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 最近一次写入的署名（请求头 X-Actor，自报、未认证）；助手起草时可为空
    updated_by: Mapped[str | None] = mapped_column(String(100), default=None)


class CatalogDraftJob(Base, TimestampMixin):
    """数据目录的一次后台起草任务（data/catalog_jobs.py）。逐表的状态在 catalog_draft_tasks。

    以前起草是一次同步长请求，出错整个对话框就停、关掉之后没有任何记录哪些表失败、为什么；现在建任务后在后台起草，
    进度（每张表到第几段、计数、用量）随时落库，接口读的是库里的状态。

    同一个数据源同时只有一个排队或进行中的任务：接口先查一遍，部分唯一索引兜底（两个请求同时建，后提交的撞上）。
    服务重启时还没结束的标成 interrupted，不自动续跑。挂外键、级联删除：任务是数据源的附属记录，源删掉了留着没有意义。
    """

    __tablename__ = "catalog_draft_jobs"
    __table_args__ = (
        Index("ix_catalog_draft_jobs_source_created", "source_id", "created_at"),
        Index("ux_catalog_draft_jobs_active", "source_id", unique=True,
              sqlite_where=text("status IN ('queued', 'running')"),
              postgresql_where=text("status IN ('queued', 'running')")),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    # queued | running | succeeded | partial | failed | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(20), default="queued")
    #: 认领这一行的工作进程（app/cluster；缺省是建它的进程）。多进程时启动整理、接手只碰属主不在了的行
    owner: Mapped[str | None] = mapped_column(String(120), default=current_owner)
    use_model: Mapped[bool] = mapped_column(default=False)
    # 实际用的模型 id（用模型时开始起草后写上）
    model: Mapped[str | None] = mapped_column(String(200), default=None)
    # 建任务时的快照：数据源级的起草要求、这一次的补充说明
    guidance: Mapped[str | None] = mapped_column(Text, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    # 请求头 X-Actor 的署名（自报、未认证）
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # 用量：调用次数、输入和输出 token（拿不到用量的调用按 0 算，超时的调用也是 0）、超时几次、没写出来几次（额度用完、
    # 只给空壳、超时、出错）
    calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    timeouts: Mapped[int] = mapped_column(Integer, default=0)
    failed_calls: Mapped[int] = mapped_column(Integer, default=0)
    # 新开始的段每段最多几列（额度用完、超时多了会减半；用模型时开始起草后写上）
    part_columns: Mapped[int | None] = mapped_column(Integer, default=None)
    # 提醒：最近几次调用多数没写出来时的一句话（建议换起草模型、减少表或停止），少了清掉
    notice: Mapped[str | None] = mapped_column(Text, default=None)
    # 任务整体出错的原因（逐表的原因在 catalog_draft_tasks）
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # 重试建的任务记下是重试哪一个
    retry_of: Mapped[str | None] = mapped_column(String(32), default=None)


class CatalogDraftTask(Base):
    """起草任务里的一张表。按 seq（起草顺序）排。状态：pending 排队、running 起草中、ok、partial 模型只写出一部分、
    failed、skipped 表结构里没有、cancelled（停止或服务重启）。"""

    __tablename__ = "catalog_draft_tasks"
    __table_args__ = (UniqueConstraint("job_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("catalog_draft_jobs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    # 表结构里的键；表结构里没有的（skipped）是请求里的原样名字
    table_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    column_count: Mapped[int] = mapped_column(Integer, default=0)
    # 模型分几段起草（不用模型时为 NULL）、已经结束了几段、其中写出了内容并进了目录的段数（宽表每写出一段就并入目录，
    # 停止、服务重启时已经写进去的留着）
    parts_total: Mapped[int | None] = mapped_column(Integer, default=None)
    parts_done: Mapped[int] = mapped_column(Integer, default=0)
    parts_written: Mapped[int] = mapped_column(Integer, default=0)
    # 块进度：这张表实际分成几块来问、已经结束了几块（段被缩小、拆成两半后会变多）；不用模型时为 NULL
    chunks_total: Mapped[int | None] = mapped_column(Integer, default=None)
    chunks_done: Mapped[int | None] = mapped_column(Integer, default=None)
    added: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    model_items: Mapped[int | None] = mapped_column(Integer, default=None)
    # 模型拿不准、留空了的列（中文名和含义都没给）：几列，最多记 5 个列名。表的状态不因此变
    blank_columns: Mapped[int] = mapped_column(Integer, default=0)
    blank_sample: Mapped[list[Any] | None] = mapped_column(default=None)
    # 起草时记下的几句话，不算失败（模型提议的有效记录条件没有收下的原因），最多 10 句
    log: Mapped[list[Any] | None] = mapped_column(default=None)
    # error：整张表没起草（表结构里没有、写入一直冲突、服务重启中断）；model_error：模型那部分没写出来的原因和办法
    error: Mapped[str | None] = mapped_column(Text, default=None)
    model_error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class CatalogProfileJob(Base, TimestampMixin):
    """数据目录的一次后台剖析任务（data/catalog_profile_jobs.py）。逐表的结果在 catalog_profile_tasks。

    以前剖析是一次同步长请求：预算按整次剖析算（选 50 张表只做得完两三张）、不能停、关掉页面报告就没了，对业务库查了什么
    也无从查。现在建任务后在后台逐表剖析，每张表各有自己的查询次数和时长，进度、发过的查询（字面量去掉）随时落库。生命周期
    和起草任务是同一套（data/catalog_job_runtime.py），表各建各的：进度字段完全不同。

    同一个数据源同时只有一个排队或进行中的剖析任务：接口先查一遍，部分唯一索引兜底。和同源的起草任务按建立的先后排队。
    服务重启时还没结束的标成 interrupted，不自动续跑。挂外键、级联删除：任务是数据源的附属记录。
    """

    __tablename__ = "catalog_profile_jobs"
    __table_args__ = (
        Index("ix_catalog_profile_jobs_source_created", "source_id", "created_at"),
        Index("ux_catalog_profile_jobs_active", "source_id", unique=True,
              sqlite_where=text("status IN ('queued', 'running')"),
              postgresql_where=text("status IN ('queued', 'running')")),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    # queued | running | succeeded | partial | failed | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(20), default="queued")
    #: 认领这一行的工作进程（app/cluster；缺省是建它的进程）。多进程时启动整理、接手只碰属主不在了的行
    owner: Mapped[str | None] = mapped_column(String(120), default=current_owner)
    # selected 指定的表 | needed 需要剖析的表 | all 全部表
    scope: Mapped[str] = mapped_column(String(20), default="selected")
    # 建任务时的剖析设置快照（catalog_profile.ProfileSettings.to_dict）
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 署名（开了登录是认证过的用户名，否则请求头 X-Actor 的自报）
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # 整个任务发出的查询数、其中超时的、失败的（守卫拒掉的没发出去，不算）
    queries: Mapped[int] = mapped_column(Integer, default=0)
    timeouts: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    # 任务级止损停下的原因：failed 连续失败、slow 超时比例；没有为 NULL
    stop_reason: Mapped[str | None] = mapped_column(String(20), default=None)
    # 提醒：最近的查询多数超时时的一句话，好转后清掉
    notice: Mapped[str | None] = mapped_column(Text, default=None)
    # 任务整体出错、止损、数据源中途停用或关掉剖析时的整句原因
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # 重试建的任务记下是重试哪一个
    retry_of: Mapped[str | None] = mapped_column(String(32), default=None)


class CatalogProfileTask(Base):
    """剖析任务里的一张表。按 seq（剖析顺序）排。状态：pending 排队、running 剖析中、ok 每项都有结论、partial 有项因为
    运行原因没查成、failed 写入一直冲突或整张表出错、skipped 视图 / 表结构里没有 / 空表 / 没有可剖析的项、cancelled
    （停止、止损或服务重启）。

    检查项、发现、跳过的项、日期范围、发过的查询都是 JSON：条数受每张表的查询上限约束，大小有界。发过的查询里的 SQL
    字面量都换成了「?」（sql_semantics.normalize_sql）：抽样到的键值不进 AgentLab 自己的库。"""

    __tablename__ = "catalog_profile_tasks"
    __table_args__ = (UniqueConstraint("job_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("catalog_profile_jobs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    # 表结构里的键；表结构里没有的（skipped）是请求里的原样名字
    table_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # 检查项：[{kind, target, path, state, reason, detail, queries}]。建任务时按当时的目录列一遍，开始剖析这张表时按最新的
    # 目录重列
    checks: Mapped[list[Any] | None] = mapped_column(default=None)
    queries: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    # 进行中时正在做哪一项的整句（「正在核对 orders.customer_id → customers.id」）
    current: Mapped[str | None] = mapped_column(Text, default=None)
    # 行数估算 {rows, method, at_least}
    row_estimate: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    findings: Mapped[list[Any] | None] = mapped_column(default=None)
    skipped: Mapped[list[Any] | None] = mapped_column(default=None)
    date_ranges: Mapped[list[Any] | None] = mapped_column(default=None)
    # 发过的查询：[{at, kind, target, sql, ms, rows, outcome, detail}]
    query_log: Mapped[list[Any] | None] = mapped_column(default=None)
    # 发现的合计 {relations_checked, relations_verified, codes, codes_pending, business_dates}；跳过的项数、其中因为运行
    # 原因没查成的（表清单的「上次剖析」读这几列，不必读整份发现）
    summary: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    unfinished: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    # 整张表的原因：表结构里没有、视图、空表、没有可剖析的项、写入一直冲突、停止 / 中断 / 止损
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # 剖析上传表格源时那一刻的快照（数据库源为空）：上传表格换了版本之后，旧版本的剖析事实不再交给 look（next-round 遗留 e）
    snapshot_id: Mapped[str | None] = mapped_column(String(32), default=None)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


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
    # http 传输的认证头（值经 core/crypto 加密，接口只回掩码）；或者绑定一个登记的端点（app/capabilities）
    headers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    endpoint_id: Mapped[str | None] = mapped_column(String(32), default=None)


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
    # 从运行提取来的工作流记下来源：{"run_id", "conversation_id"?, "turn_id"?}（/copilot/from-run）。手建的为空
    origin: Mapped[dict[str, Any] | None] = mapped_column(default=None)

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
    # 发布时这一版 SQL 用到的表的数据目录版本：{源名: {表名: 版本}}，还没有目录的表记 0（data/catalog_impact.py）。
    # 从这一版发起正式运行时和当前的比，有变化只提醒、不拦。没发布过、或者是加这一列之前发布的为空——不知道
    # 当时是哪一版，不提醒
    catalog_versions: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 这一版是谁、什么时候发布的（工作流上的 published_by 只记最近一次）。存量为空
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    published_by: Mapped[str | None] = mapped_column(String(100), default=None)
    # 退役：不再接受新的正式运行，进行中的实例照常走完。当前发布版不能退役（app/apps/versions.py）
    retired_at: Mapped[datetime | None] = mapped_column(default=None)
    retired_by: Mapped[str | None] = mapped_column(String(100), default=None)
    retire_note: Mapped[str | None] = mapped_column(Text, default=None)
    # 发布时这一版用到的能力的治理版本：{能力名: version}。正式运行发起时有漂移只提醒、不拦
    capability_versions: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 发布时这一版直接引用的表的结构版本 {源名: {表名: 结构版本}}（next-round 03 B1）：正式运行发起时比对，变了只提醒、
    # 不拦（schema.drift），写法照 catalog_versions。加这一列之前发布的版本为空
    schema_versions: Mapped[dict[str, Any] | None] = mapped_column(default=None)

    workflow: Mapped[Workflow] = relationship(back_populates="versions")


# --------------------------------------------------------------------------
# 运行、事件、审批
# --------------------------------------------------------------------------


class Run(Base, TimestampMixin):
    __tablename__ = "runs"
    # 找钉住版本最近一次封存的运行（应用的视图、口径对比）。存量库由 db/base.py 的 _INDEX_MIGRATIONS 补建
    __table_args__ = (Index("ix_runs_wf_ver_status", "workflow_id", "version", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), default=None
    )
    workflow_name: Mapped[str] = mapped_column(String(200), default="")
    # LangGraph checkpointer 的 thread_id —— 持久化与恢复的锚点
    thread_id: Mapped[str] = mapped_column(String(64), index=True, default=new_id)
    # queued | running | interrupted | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    #: 认领这一行的工作进程（app/cluster；缺省是建它的进程）。多进程时启动整理、接手只碰属主不在了的行
    owner: Mapped[str | None] = mapped_column(String(120), default=current_owner)
    graph: Mapped[dict[str, Any]] = mapped_column(default=dict)  # 执行时的图快照
    input: Mapped[dict[str, Any]] = mapped_column(default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # 运行为什么结束在 cancelled（docs/design/next-round/00-总方案.md X9）：rejected 人工驳回、stopped 手动停止。
    # 其余终态、加这一列之前的运行为空
    end_reason: Mapped[str | None] = mapped_column(String(20), default=None)
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
    # 发起时定下、整次运行沿用的运行选项（比如核查设置的快照）。审批停下期间改了设置，恢复和重放
    # 仍按发起时的这份——同 approval_default 的道理。非空时写进 run.started，进封存范围。存量运行是 {}
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 发起它的定时任务，手动发起的为空。不设外键：定时任务删掉之后，运行记录照旧是追溯凭证，来源 id 留着
    schedule_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    # 运行来源（engine/origin.py 的 ORIGIN_KINDS）：chat / canvas / launcher / schedule / app_entry / app_view /
    # app_backfill / hook / api。发起时由 launch_run 写，同时记一条 run.origin 事件进封存范围。存量为空：来源未记录
    origin_kind: Mapped[str | None] = mapped_column(String(20), default=None, index=True)
    origin: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    app_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    app_version: Mapped[int | None] = mapped_column(Integer, default=None)

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
    #: 认领这一行的工作进程（app/cluster；缺省是建它的进程）。多进程时启动整理、接手只碰属主不在了的行
    owner: Mapped[str | None] = mapped_column(String(120), default=current_owner)
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
    # 一个人在一个应用里只有一段（未归档的）助手对话（D-e）。存量库由 db/base.py 的 _UNIQUE_INDEX_MIGRATIONS 补建。
    # created_by 为空（关登录又没填署名）的不受约束：SQLite 的唯一索引里 NULL 互不相等，那种情况靠接口里的进程内锁
    __table_args__ = (Index("ux_conversations_app_owner", "app_id", "created_by", unique=True,
                            sqlite_where=text("kind = 'app' AND archived = 0"),
                            postgresql_where=text("kind = 'app' AND archived = false")),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(20), default="chat", index=True)
    workflow_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), default=None, index=True
    )
    archived: Mapped[bool] = mapped_column(default=False)
    # 应用编辑时助手对话归属的应用（kind=app）。其余为空
    app_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    # 谁建的这段对话（api/deps.actor_of：开登录时是认证过的用户名）。开登录后 user 角色只看得到、只改得动自己的对话；
    # 存量为空：关登录时建的，开登录后只有 builder 以上看得到
    created_by: Mapped[str | None] = mapped_column(String(100), default=None, index=True)
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


# --------------------------------------------------------------------------
# 定时任务：按 cron 到点发起运行，结束、失败、停在审批时推送 Webhook（app/scheduler）
# --------------------------------------------------------------------------


class Schedule(Base, TimestampMixin):
    """一个定时任务。webhook_url、webhook_secret 落库前经 app.core.crypto 加密，接口只返回主机和末 4 位。

    不给 workflow_id 设外键：工作流删掉之后任务留着，到点照常发起、照实记一次未能发起（「工作流不存在」），
    订阅了失败通知的人会收到——悄悄跟着删掉，每周的报告就无声无息地没了。
    """

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), default="")
    # 定时的目标（next-round X4，定时只有一套）：workflow 发起工作流运行；eval_suite 跑一次评测题集；
    # source_maintenance 维护一个数据源（探查、剖析、取值观察）。加这一列之前的任务都是 workflow
    target_kind: Mapped[str] = mapped_column(String(20), default="workflow")
    # 目标的 id：workflow 时同 workflow_id；其余两种是题集 id、数据源 id
    target_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    # 怎么触发（docs/design/orchestration-round/00-总方案.md §4）：cron 按 cron 表达式到点触发（加这一列之前的都是）；
    # event 由事件触发，trigger 写事件种类和对象（table.version / workflow.finished / source.changed，见 scheduler 的说明）。
    # event 时 cron 存空串（老表的非空约束），下一次触发时间为空
    trigger_kind: Mapped[str] = mapped_column(String(20), default="cron")
    trigger: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 非空（老表的约束，只加不改的迁移放宽不了）：目标不是工作流时存空串，读写都经 scheduler/service 里的一处封装
    workflow_id: Mapped[str] = mapped_column(String(32), index=True)
    # 保存时的工作流名。工作流还在时接口给它现在的名字，删掉之后才用这一份
    workflow_name: Mapped[str] = mapped_column(String(200), default="")
    cron: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64))
    # formal：每次都从当前发布版本发起正式运行；exploratory：用工作流现在的画布
    run_class: Mapped[str] = mapped_column(String(20), default="formal")
    # {输入字段名: 模板字符串}，到点按触发时间渲染（scheduler/service.py）
    input_template: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    # 上一次运行还没结束时怎么办。目前只有 skip：记一次跳过
    overlap: Mapped[str] = mapped_column(String(20), default="skip")
    # 错过的触发（服务停机等）在这么多秒内补一次，超出就记跳过
    misfire_grace_s: Mapped[int] = mapped_column(Integer, default=3600)
    # generic | feishu | wecom | dingtalk。没配 Webhook 时 url 为空
    webhook_format: Mapped[str | None] = mapped_column(String(20), default=None)
    webhook_url: Mapped[str | None] = mapped_column(Text, default=None)  # 密文
    webhook_secret: Mapped[str | None] = mapped_column(Text, default=None)  # 密文
    webhook_events: Mapped[list[Any]] = mapped_column(default=list)
    # 下一次触发的时刻（UTC）。停用时为空；调度循环按它认领，认领时推进到再下一次
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None, index=True)
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)
    # 乐观锁：PATCH 带上读到的版本，对不上回 409。调度循环推进 next_run_at 不改它
    version: Mapped[int] = mapped_column(Integer, default=1)


class ScheduleFire(Base):
    """一次触发：到点、立即运行、或者错过的补跑 / 跳过。(schedule_id, fire_at, kind) 唯一：两拍撞在一起时，
    后插入的那条撞上约束就知道这一次已经有人处理了，不会发起第二次。"""

    __tablename__ = "schedule_fires"
    __table_args__ = (
        UniqueConstraint("schedule_id", "fire_at", "kind"),
        Index("ix_schedule_fires_schedule_created", "schedule_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedules.id", ondelete="CASCADE"))
    # 计划触发的时刻（UTC）。立即运行是点下去的那一秒；输入模板的日期变量都按它算
    fire_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # scheduled | manual | misfire
    kind: Mapped[str] = mapped_column(String(20), default="scheduled")
    # started | skipped_overlap | skipped_misfire | failed_to_start
    status: Mapped[str] = mapped_column(String(30), default="started")
    run_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    # 目标是评测题集时，这一次触发建的那次评测（target_kind=eval_suite，next-round X4）
    eval_run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    # 渲染后的输入，就是交给运行的那一份
    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 已经按哪个运行状态处理过通知：succeeded / failed / cancelled / failed_to_start；停在断点上的另带第几段执行，
    # 停在审批是 interrupted:approval:<段>、挂起是 interrupted:held:<段>（见 scheduler/webhook.py 的 _state_of）。
    # 和现在算出来的不同才再看要不要推送，同一状态只处理一次
    notified_status: Mapped[str | None] = mapped_column(String(30), default=None)
    # 和同一任务上一次运行的比较结果（工件 id），后续的运行对比工作包写
    compare_artifact: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class WebhookDelivery(Base):
    """一次 Webhook 推送（含重试）的结果。error 只写主机名和平台返回的错误码，不含地址、key、密钥。
    「测试 Webhook」也记一条：fire_id、run_id 为空，event 是 test。"""

    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedules.id", ondelete="CASCADE"), index=True)
    fire_id: Mapped[str | None] = mapped_column(
        ForeignKey("schedule_fires.id", ondelete="CASCADE"), default=None, index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # finished | failed | awaiting_approval | test
    event: Mapped[str] = mapped_column(String(30))
    # ok | failed
    status: Mapped[str] = mapped_column(String(20))
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --------------------------------------------------------------------------
# 应用平台（docs/design/app-platform-and-look.md §3.1）：按领域分文件，这里统一导入，
# 让 init_db 的 create_all 和其他模块 `from app.db.models import ...` 都认得到
# --------------------------------------------------------------------------

from app.db.models_apps import (  # noqa: E402,F401
    App, AppGrant, AppHook, AppVersion, AppViewCache, HookReceipt,
)
from app.db.models_capabilities import Capability, Endpoint  # noqa: E402,F401
from app.db.models_identity import AuditEvent, AuthSession, Group, GroupMember, User  # noqa: E402,F401
from app.db.models_process import (  # noqa: E402,F401
    ActionRecord, NotifyChannel, NotifyDelivery, Task, TaskAssignment,
)
from app.db.models_blueprints import (  # noqa: E402,F401
    Blueprint, BlueprintEvent, PublishPlan, RecordsTable, StoredFile,
)
# 下一轮第 2 波（docs/design/next-round）：数据源对象索引与后台探查（models_sources）、评测（models_eval）。整模块导入，
# 各自的模型类由那两个文件自己登记，这里不逐个列名
from app.db import models_chat, models_eval, models_sources  # noqa: E402,F401
# 编排完善第 1 波（docs/design/orchestration-round/00-总方案.md §4）：数据事件触发的待派发事件和去抖等待
from app.db import models_triggers  # noqa: E402,F401
# 编排完善第 2 波 W2-b（docs/design/orchestration-round/00-总方案.md §5）：多进程协调——工作进程的登记和租约、调度当班、
# 跨进程的请求、运行的并发名额、同源队、按时间窗的计数
from app.db import models_cluster  # noqa: E402,F401
