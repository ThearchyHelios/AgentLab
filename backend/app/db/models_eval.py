"""评测：题集、题目、评测、结果、目录快照（docs/design/next-round/01-效果度量与回归评测.md 做法 6、7）。

由 app/db/models.py 末尾整模块导入，create_all 建表（只加不改：都是新表，总方案 X11）。这里不导入 app.db.models：
models.py 导入本模块时自己还没加载完，外键一律按表名写。

**为什么一次定全。** 第 2 波只做题集、题目和期望答案的来源；第 3 波的执行、评分、对比、清理直接用 EvalRun、EvalResult
的这些列。列一次定好，第 3 波不再改表（SQLite 上 ADD COLUMN 式的迁移改不了约束，只加列也要进迁移列表）。

**为什么是强类型的几张表，不是「kind + JSON」的通用任务表。** 评测的进度（每题正在做什么、作答路径、结论、分段耗时、
按角色的用量）要能按列筛、按列汇总；通用任务表丢掉这些列，后台任务运行时的说明里已经否决过（core/job_runtime.py）。

- EvalSuite 题集：名称、说明、时区（题目里 {{ schedule.* }} 变量按它算）、默认变体、预算、是否归档、乐观锁 version。
- EvalCase 题目：kind（ask 问数据题 / workflow 钉版本的工作流题）、问法、数据源、期望（expectations）和期望答案的来源
  （gold：sql / caliber / from_run，见 app/eval/cases.py）、标签、乐观锁 version、case_hash（题目改过 hash 就变，和
  上一次比时标「题目改过，不可比」）。history 是给多轮追问留的位置（评测-D12：本轮只做单轮，必须为空）。
- EvalRun 一次评测（第 3 波起用）：状态用后台任务的状态词，变体原样存一份、另存实际解析出的模型 id，指纹、配对（A/B）、
  基准、预算快照、计数、按角色的用量、停下的原因。
- EvalResult 每题 × 重复 × 臂一项（第 3 波起用）：状态用逐项状态词，作答路径、结论、评分、复核、出具档位、分段耗时……
  job_id 是 eval_run_id 的别名：后台任务运行时的收尾（close_job、cancel_queued）按 task_model.job_id 认逐项。
- EvalCatalogSnapshot 目录快照的登记（做法 6）：快照内容是内容寻址工件（kind=eval_catalog_snapshot，读法见
  data/catalog_view.py），这里记谁、何时、来源（当前目录 / 影子起草：起草模型、起草要求、是否先去掉模型起草的项）、影子
  起草的进度和用量。工件一旦写好就不变，这一行写好 artifact_id 之后也不再改。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.db.base import Base, TimestampMixin, UTCDateTime, new_id

#: 题目的种类：ask 问数据题（走问数据同一条路作答）、workflow 钉版本的工作流题
CASE_KINDS = ("ask", "workflow")
#: 一次评测怎么发起的：手动、定时、A/B 一次发起两臂
RUN_TRIGGERS = ("manual", "schedule", "pair")
#: 逐题的作答路径（第 3 波）：直接回答、反问、只建不跑、建图失败、运行失败、跑完了、被驳回（总方案 X9：驳回记成路径，
#: 不算失败）
RESULT_PATHS = ("reply", "asked", "ready", "build_failed", "run_failed", "ran", "rejected")
#: 逐题的结论（第 3 波）：通过、不通过、不能评分（遮罩、期望没算出来、作答期间数据变了）、跳过（口径升版待处置、到了上限）
VERDICTS = ("pass", "fail", "ungradable", "skipped")
#: 目录快照的来源：当前目录、影子起草
SNAPSHOT_ORIGINS = ("live", "shadow")

#: 预算的缺省（评测-D9，用户拍板）：同时 2 题、每题 900 秒、单次评测 2 美元、评测每日 5 美元（评测单独一本账，不挤占
#: 裁判、核查的额度）。题集的 budget 只存改过的项，生效的是缺省加上它（app/eval/suites.effective_budget）；发起评测时
#: 整份快照进 EvalRun.budget。这一波只定字段，执行、预留与找补在第 3 波。首次真实验收后按实际花费调
DEFAULT_BUDGET: dict[str, Any] = {
    "max_parallel": 2,
    "per_case_timeout_s": 900,
    "per_run_max_usd": 2.0,
    "daily_max_usd": 5.0,
}


class EvalSuite(Base, TimestampMixin):
    """一个题集。归档的不出现在缺省列表里、不能发起评测（第 3 波），题目和历次评测都留着。"""

    __tablename__ = "eval_suites"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # 题目、期望 SQL、口径输入里的 {{ schedule.* }} 按评测的截至时间在这个时区算（scheduler/service.template_values）
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    # 发起评测时缺省用的变体（助手模型、写作模型、裁判、目录视图、重复次数……，第 3 波解析）
    default_variant: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 只存改过的预算项，缺省见 DEFAULT_BUDGET
    budget: Mapped[dict[str, Any]] = mapped_column(default=dict)
    archived: Mapped[bool] = mapped_column(default=False)
    # 乐观锁：改题集带上读到的 version，对不上回 409
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)


class EvalCase(Base, TimestampMixin):
    """题集里的一道题。字段的取值和校验见 app/eval/cases.py（normalize_case）。"""

    __tablename__ = "eval_cases"
    __table_args__ = (UniqueConstraint("suite_id", "seq"), Index("ix_eval_cases_suite", "suite_id"))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    suite_id: Mapped[str] = mapped_column(ForeignKey("eval_suites.id", ondelete="CASCADE"))
    # 题号：题集里从 1 起，删了的题号不复用（运行名「评测：题集名·第 n 题」按它写）
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="ask")
    # 问法。可以带 {{ schedule.* }} 变量（按评测的截至时间渲染成明确的日期）
    question: Mapped[str] = mapped_column(Text, default="")
    # 问数据题限定的数据源（空 = 不限）。导出导入按名字对上（app/eval/transfer.py）
    datasource_ids: Mapped[list[Any]] = mapped_column(default=list)
    # workflow 题：钉住的工作流版本和输入模板（输入模板同定时任务，可以带 {{ schedule.* }}）
    workflow_id: Mapped[str | None] = mapped_column(String(32), default=None)
    workflow_version: Mapped[int | None] = mapped_column(Integer, default=None)
    input_template: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 多轮追问的前几轮（评测-D12：本轮只做单轮，这里必须为空；留位置是为了以后不改表）
    history: Mapped[list[Any] | None] = mapped_column(default=None)
    # 期望：[{id, kind: scalar|table|set|none|ask|text, required, 取哪一格/列/表……}]。数值期望不许人填（评测-D4）
    expectations: Mapped[list[Any]] = mapped_column(default=list)
    # 期望答案的来源：{kind: sql|caliber|from_run, …}；只有反问、文字要点的题为空
    gold: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 标签：口径陷阱、拒答、清单、相对日期……（自由填写，统计时分组用）
    tags: Mapped[list[Any]] = mapped_column(default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    # 口径升版选了「不可比」：旧题归档（不再评），superseded_by 指向按新版本另建的那道题
    archived: Mapped[bool] = mapped_column(default=False)
    superseded_by: Mapped[str | None] = mapped_column(String(32), default=None)
    # 乐观锁
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 题目内容的指纹（app/eval/cases.case_hash）：问法、数据源、期望、期望来源变了它就变
    case_hash: Mapped[str] = mapped_column(String(64), default="")
    # 最近一次试算：{status, code, reason, at, by, as_of, run_ids}。不存期望值：期望每次评测都重算，不冻结数值
    trial: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)


class EvalCatalogSnapshot(Base, TimestampMixin):
    """一份目录快照的登记。内容在工件里（artifact_id，kind=eval_catalog_snapshot）；变体用工件 id 指定目录
    （data/catalog_view.CatalogView(mode="snapshot", snapshot=工件 id)），同一份快照可以被多次评测引用。

    当前目录的快照建出来就是 succeeded；影子起草是后台任务（app/eval/snapshots.py），状态用后台任务的状态词，起草完、
    工件写好才有 artifact_id。"""

    __tablename__ = "eval_catalog_snapshots"
    __table_args__ = (Index("ix_eval_catalog_snapshots_artifact", "artifact_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    origin: Mapped[str] = mapped_column(String(20), default="live")
    # queued | running | succeeded | partial | failed | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(20), default="succeeded")
    artifact_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # 拍了哪些数据源：[{id, name}]（名字是拍的那一刻的）
    sources: Mapped[list[Any]] = mapped_column(default=list)
    # 影子起草起草了哪些表：{源 id: [表名]}；当前目录的快照为空（整个源的目录照拍）
    tables: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 影子起草的设定：{provider, model, model_id, guidance, note, strip_llm, suite_id}
    draft: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 开始前的估算：{tables, columns, calls, input_tokens, output_tokens, cost_usd, unpriced, note}
    estimate: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 实际用量：{calls, input_tokens, output_tokens, timeouts, failed_calls, cost_usd, unpriced}
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 进度：{tables_total, tables_done, tables_failed, current, notice, failures: {表名: 原因}}
    progress: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class EvalRun(Base, TimestampMixin):
    """一次评测（第 3 波的 app/eval/runner.py 写）。一次评测是一个后台任务，每题 × 重复次数 × 臂是一项（EvalResult）。

    题集有评测时不能删除（只能归档）：每次评测的总表（summary）永久保留，明细可以按保留规则清理（cleaned_at）。"""

    __tablename__ = "eval_runs"
    __table_args__ = (Index("ix_eval_runs_suite_created", "suite_id", "created_at"),
                      Index("ix_eval_runs_pair", "pair_id"))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    suite_id: Mapped[str] = mapped_column(ForeignKey("eval_suites.id", ondelete="CASCADE"))
    # queued | running | succeeded | partial | failed | cancelled | interrupted（core/job_runtime 的状态词）
    status: Mapped[str] = mapped_column(String(20), default="queued")
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    # 定时发起的那一条（ScheduleFire.eval_run_id 反过来指向这里）
    schedule_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 变体原样存一份；resolved_models 是实际解析出的模型 id（助手、写作、裁判……）
    variant: Mapped[dict[str, Any]] = mapped_column(default=dict)
    resolved_models: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 这次用的目录快照（工件 id）：开始时拍一份（缺省「当前目录」也拍），起草、剖析照常进行也不影响可比性
    catalog_snapshot: Mapped[str | None] = mapped_column(String(64), default=None)
    # 截至时间：题目里的 {{ schedule.* }} 按它渲染
    as_of: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    repeats: Mapped[int] = mapped_column(Integer, default=1)
    # 指纹：提示词指纹、目录快照 id 和哈希、各数据源 schema 哈希、题目哈希、评分规则版本
    fingerprint: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # A/B：两臂共用 pair_id，arm 是这一臂的名字
    pair_id: Mapped[str | None] = mapped_column(String(32), default=None)
    arm: Mapped[str | None] = mapped_column(String(40), default=None)
    # 和哪一次比；pinned 是用户钉住的基线（清理时保留）
    baseline_id: Mapped[str | None] = mapped_column(String(32), default=None)
    pinned: Mapped[bool] = mapped_column(default=False)
    # 发起时的预算快照（DEFAULT_BUDGET 加题集改过的项）
    budget: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 计数：{total, done, passed, failed, ungradable, skipped, …}；按角色的用量；总表（永久保留）
    counts: Mapped[dict[str, Any]] = mapped_column(default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 停下的原因（止损、到了上限、手动停止），整体出错的原因
    stop_reason: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # 明细（逐题结果、底下的运行）按保留规则清理掉的时间；总表还在
    cleaned_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class EvalResult(Base, TimestampMixin):
    """一次评测里的一项：一道题 × 第几次重复 × 哪一臂（第 3 波写）。题目删了结果照留（case_id 不设外键）。"""

    __tablename__ = "eval_results"
    __table_args__ = (Index("ix_eval_results_run_case", "eval_run_id", "case_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    eval_run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    #: 后台任务运行时按 task_model.job_id 认一个任务的逐项（close_job、cancel_queued）
    job_id = synonym("eval_run_id")
    # 排序用：同一次评测里从 1 起
    seq: Mapped[int] = mapped_column(Integer, default=0)
    case_id: Mapped[str | None] = mapped_column(String(32), default=None)
    case_seq: Mapped[int | None] = mapped_column(Integer, default=None)
    case_hash: Mapped[str] = mapped_column(String(64), default="")
    repeat: Mapped[int] = mapped_column(Integer, default=0)
    arm: Mapped[str | None] = mapped_column(String(40), default=None)
    # pending | running | ok | partial | failed | skipped | cancelled（core/job_runtime 的逐项状态词）
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # 作答的那次运行、算期望的那几次运行（作答前后各一次，口径题两版各一次）
    run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    gold_run_ids: Mapped[list[Any]] = mapped_column(default=list)
    # 期望：作答前后两次的结果和是否一致（app/eval/gold.settle 的形状）
    gold: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    path: Mapped[str | None] = mapped_column(String(20), default=None)
    verdict: Mapped[str | None] = mapped_column(String(20), default=None)
    # 每个期望的结论（对且可追溯 / 对但没引用 / 错 / 量纲不同 / 不能评分）
    scores: Mapped[list[Any]] = mapped_column(default=list)
    review: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    tier: Mapped[str | None] = mapped_column(String(20), default=None)
    judge: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    looks: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    # 分段耗时（建图、运行、复核、整题）、按角色的用量
    timings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 答错的题：引用那格所在查询的 SQL 和期望 SQL 的比对（逐题的 E1）
    sql_match: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    details_artifact: Mapped[str | None] = mapped_column(String(64), default=None)
    # 正在做什么（正在算期望 / 正在建图 / 运行中 / 正在复核 / 正在评分）
    current: Mapped[str | None] = mapped_column(String(200), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


__all__ = ["CASE_KINDS", "DEFAULT_BUDGET", "EvalCase", "EvalCatalogSnapshot", "EvalResult", "EvalRun", "EvalSuite",
           "RESULT_PATHS", "RUN_TRIGGERS", "SNAPSHOT_ORIGINS", "VERDICTS"]
