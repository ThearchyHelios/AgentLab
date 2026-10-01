"""按配方导入的流程（期 2，WP-5c；P2-SPEC 第 7 节）：暂存 → 起草 → 回答问题 / 改配方 → 试运行 → 确认并启用，
以及「上传新一期」（按已确认的配方重放）和「修改配方」（不换文件）。

**暂存区是唯一的工作台。** 起草、回答、改配方、试运行都只改 import_stagings 里的一行，提交成功的那个事务
（table_versions.publish_build）才碰数据源、导入记录和快照。所以任何拒收、放弃、过期都不动当前快照，
暂存区结束即瘦身（table_versions.close_staging）。

**各模块经 Pipeline 接缝调用。** 静态校验、读取、执行、核对、说明、起草、差异卡、确认项都来自别的工作包，
这里只编排：default_pipeline() 在函数体里惰性导入真模块，测试替换 pipeline() 返回假实现，状态机的每条
分支（错误码、提交事务、并发）都能在不依赖真表格的情况下测到。

**几条贯穿全文件的规矩：**
- CPU 密集的步骤（扫描、读网格、干跑、试运行、核对、压缩表示）在线程池里，受 table_versions.parse_slot()
  限并发；等模型回复期间不占名额（三次调用、每次 90 秒，占着名额会堵住所有上传和试运行）。
- 工作配方 = answers_base 按问题顺序重放全部回答。问题（含 effects）按 answers_base 生成，只在 answers_base
  换掉（暂存、PUT、AI 草稿、上传新一期、修改配方）时重算；每次回答只重算卡片。这样先选 A 再改选 B 等于
  直接选 B，effects 的路径也不会打在已经改过的配方上。
- 每次回答、PUT、修改配方之前用 detect_facts(grids, scan, recipe=工作配方) 取系统发现，静态校验带上它
  （认领检查）；上传新一期的现行配方原样重放时按 origin="replay" 校验、不查认领（配方已经确认过，本期的
  数据变化由试运行报，不该在静态校验这一步就把 D26 这类数据质量问题挡成「配方不合法」）。
- 试运行把 asdict(Extraction) 和核对结果整份存进 trial，提交时据此还原、重算必勾确认项；差异卡的本期
  一侧用这份完整回执，不用只截了前 50 条规范写法的 TrialOut.receipt。
- 发布前不改会话里的任何对象：对暂存区的修改一律放进 publish_build 的 before_commit（锁内、同一事务）。

**期 3（WP-5）补的几条：**
- 修复按钮与框选（edits/preview、apply、undo）：补丁由服务端按封闭选项算，客户端只发提议 id、选项和理由；
  应用时在同一个工作配方上重新换算，哈希对不上就是预览之后配方或理由变了（edit_stale）。每次修改的修改前起点
  记在暂存区的 edits 里，撤销只撤最近一次，PUT 整份替换会清空撤销栈（之前的修改标 superseded）；
- 改配方之后，回答在新起点上仍然成立的保留（carry_answers），不成立的进 answers_dropped；
- 静态校验的 base 一律取数据源**当前的**现行配方（3.3）：启用旧版本之后，未结束暂存区的三处判断要一致；
- 按期累积：试运行里做累积计划、物化并集（提交时只 link），单期提交走 publish_build，多期走 publish_snapshot；
- 只读进程（没拿到版本存储的守卫）里，会删、写试运行库和原件的入口一律先 require_store_writable（503）。
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import raw_store, recipe_parsers, recipe_types, table_versions
from app.data.recipe_confirm import ConfirmContext
from app.data.recipe_types import (
    REASON_SLOT,
    Acceptance,
    AiAvailability,
    AxisOut,
    CanonEntry,
    Card,
    CheckResult,
    ColumnOut,
    ConfirmItem,
    DerivedItem,
    DiffItem,
    Draft,
    DraftFacts,
    EditResult,
    ExcludedRows,
    Extraction,
    Fact,
    Grid,
    LedgerSheet,
    OutsideText,
    PeriodInput,
    PeriodOut,
    Problem,
    Question,
    Recipe,
    RecipeProblem,
    RegionMark,
    SchemaNotes,
    SegmentLabels,
    Selection,
    SheetsOut,
    TableOut,
    UnionPart,
)
from app.data.xlsx_scan import WorkbookScan
from app.db.base import new_id, utcnow
from app.db.models import DataSource, ImportStaging, SourceSnapshot, TableBuild, TableImport, TableRecipe

logger = logging.getLogger(__name__)

#: 导入清单的格式版本（7.7）
MANIFEST_FORMAT = "agentlab-import-manifest/1"
#: 回答里理由的字数上限（Question.options 的 needs_reason）
ANSWER_REASON_MAX = 200
#: 接受（Acceptance）理由的字数上限（5.3）
ACCEPT_REASON_MAX = 500
#: 试运行回执里规范写法对照给界面看几条（全部在导入清单里）
TRIAL_CANON_MAX = 50
#: trial 里只给服务端自己用的键：StagingOut 不带它们（整份 Extraction 很大，界面也不需要）。union 是物化好的
#: 并集（试运行文件路径、并集 id、回执），提交时用
TRIAL_INTERNAL = ("extraction", "null_counts", "inputs", "recipe_sha256", "build_id", "union", "prev_import_id")
#: TrialOut.receipt 给界面看的回执字段（7.4）。完整的回执在 trial["extraction"] 和导入清单里
_RECEIPT_VIEW = (
    "ledger", "tables", "period", "axes", "placeholders", "labels", "outside_text", "hidden", "sheets",
    "derived_form", "block_order", "ignored_columns", "full_calc_on_load", "formula_cells_accepted",
    "blank_rows_skipped", "table_hashes", "timings", "rows_excluded",
)
#: 暂存区的 recipe_origin → 静态校验的 origin。manual / mixed 一律按 manual 校验；采用按规则重新起草的结果
#: （rules_redraft，6.3）按规则草稿的口径校验
_VALIDATE_ORIGIN = {"rules": "rules", "ai": "ai", "rules_redraft": "rules"}
#: 修改记录里只给服务端的键：撤销用的修改前起点，StagingOut、导入清单都不带（整份旧配方很大，界面也不需要）
EDIT_INTERNAL = ("before", "base_sha256_after")
#: 修改记录里只进导入清单、不给界面的键（StagingOut 的 edits 不带补丁：界面用不上，也免得回包随补丁变大）
EDIT_MANIFEST_ONLY = ("ops",)
#: 统计期上下文的 id（契约 ContextSpec.id 只能是它）
PERIOD_KEY = "统计期"
_ACTIVE_TRIAL = ("passed", "needs_decision")


# ==========================================================================
# 错误
# ==========================================================================


class ImportRefused(Exception):
    """业务上拒绝这次操作。status、code 给接口层原样转成 CodedHTTPException；message 给人看。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class CommitConflict(ImportRefused):
    """提交时在版本存储锁内发现的冲突（base_changed / name_taken）：409。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


_CLOSED = "这次导入已经结束（已提交、已放弃或已过期），不能再修改。请重新开始导入"
_NOT_RECIPE = "这个数据源不是按配方导入的。请用「上传表格」更新，或通过按配方导入重新开始"
_RAW_GONE = "这次导入的原件已不在服务端（可能已被清除），请重新上传文件"
_TRIAL_STALE = "试运行之后配方或录入有变化，请重新试运行"
_BASE_CHANGED = "试运行之后当前版本已被更新，请重新试运行后再提交"
#: AI 起草失败（502 ai_failed、413 ai_too_large）之后能做什么，取决于有没有工作配方：有的话配方面板里是表单，
#: 可以接着改；没有的话面板里只能粘贴一份完整的配方（没有表单可填）。recipe_ai 的失败原因只说出了什么事
_AI_NEXT = {
    (True, True): "可以重试，或在配方面板中修改现有配方",
    (True, False): "可以重试，或在配方面板中粘贴一份完整的配方",
    (False, True): "请在配方面板中修改现有配方",
    (False, False): "请在配方面板中粘贴一份完整的配方",
}


# ==========================================================================
# 接缝
# ==========================================================================


@dataclass
class Pipeline:
    """各工作包的函数。default_pipeline() 接真模块；测试替换 pipeline() 返回假实现。

    在 P2-SPEC 10 节的清单上多了几项：scan（暂存、上传新一期要扫描）、column_null_counts（说明只给本期真有
    空值的列写空值说明，WP-3 的要求）、first_call_text（同意框展示首次调用要发出的全文）、grid_max_cells /
    draft_max_rows（大表部分干跑的阈值）、units（单位词表，StagingOut 给表单用）。
    """

    validate: Callable[..., tuple[Recipe | None, list[RecipeProblem]]]
    recipe_sha256: Callable[[Recipe], str]
    canonical: Callable[[Recipe], dict[str, Any]]
    apply_patch: Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]
    scan: Callable[[bytes], WorkbookScan]
    scan_to_json: Callable[[WorkbookScan], dict[str, Any]]
    scan_from_json: Callable[[dict[str, Any]], WorkbookScan]
    read_grid: Callable[..., Grid]
    preview: Callable[..., dict[str, Any]]
    execute: Callable[..., Extraction]
    run_checks: Callable[[str, Extraction, Recipe], list[CheckResult]]
    column_null_counts: Callable[[str, Recipe], dict[str, dict[str, int]]]
    build_notes: Callable[..., SchemaNotes]
    apply_notes: Callable[[dict[str, Any], SchemaNotes], dict[str, Any]]
    detect_facts: Callable[..., DraftFacts]
    draft: Callable[..., Draft]
    questions_for: Callable[..., tuple[list[Card], list[Question]]]
    ai_availability: Callable[..., Awaitable[AiAvailability]]
    ai_draft: Callable[..., Awaitable[Any]]
    compress: Callable[..., Any]
    first_call_text: Callable[..., str]
    diff_reports: Callable[..., list[DiffItem]]
    confirm_items: Callable[[ConfirmContext], list[ConfirmItem]]
    breaking_changes: Callable[..., dict[str, list[str]]]
    grid_max_cells: int = 200_000
    draft_max_rows: int = 500
    units: tuple[str, ...] = ()
    #: (模型对象, 模型 id, 可用性)：同意与发送只解析一次模型（AU-7）。None 时退回 ai_availability，ai_draft 自己解析
    resolve_model: Callable[..., Awaitable[tuple[Any, str, AiAvailability]]] | None = None
    # ---- 期 3（P3-SPEC 12.6）。都给默认值 None：期 2 测试里全关键字构造的假 Pipeline 照旧成立，没接上的功能
    # 按「没有」处理（没有修复提议、不做累积计划），状态机的其余分支照常测
    #: WP-2 recipe_fixes：问题 → 修复提议；选中的选项 → 补丁
    propose_fixes: Callable[..., list[Any]] | None = None
    fix_ops: Callable[..., EditResult] | None = None
    #: WP-2 recipe_select：框选 → 补丁；重放比对
    selection_edit: Callable[..., EditResult] | None = None
    replay_compare: Callable[..., Any] | None = None
    #: WP-2 recipe_suggest：按规则重新起草的结果按来源对齐到现行配方的名字（6.3）
    align_to_contract: Callable[..., tuple[dict[str, Any], list[str]]] | None = None
    #: WP-3 recipe_compare：新旧配方对照
    compare_recipes: Callable[..., dict[str, Any]] | None = None
    #: WP-4 recipe_accumulate：累积计划、配方演进分档、并集物化
    plan_accumulate: Callable[..., Any] | None = None
    classify_changes: Callable[..., Any] | None = None
    materialize_union: Callable[..., Any] | None = None
    #: WP-3 recipe_notes：物化快照的说明
    build_union_notes: Callable[..., SchemaNotes] | None = None


def default_pipeline() -> Pipeline:
    """接上真模块。函数体里惰性导入：这些模块（openpyxl、执行器、起草器）导入不便宜，接口模块加载时不必付。"""
    from app.data import (
        recipe, recipe_accumulate, recipe_ai, recipe_checks, recipe_compare, recipe_confirm, recipe_diff,
        recipe_engine, recipe_fixes, recipe_notes, recipe_select, recipe_suggest, xlsx_cells, xlsx_scan,
    )

    return Pipeline(
        validate=recipe.validate_recipe,
        recipe_sha256=recipe.recipe_sha256,
        canonical=recipe.canonical_recipe,
        apply_patch=recipe.apply_patch,
        scan=xlsx_scan.scan,
        scan_to_json=xlsx_scan.scan_to_json,
        scan_from_json=xlsx_scan.scan_from_json,
        read_grid=xlsx_cells.read_grid,
        preview=xlsx_cells.preview,
        execute=recipe_engine.execute,
        run_checks=recipe_checks.run_checks,
        column_null_counts=recipe_checks.column_null_counts,
        build_notes=recipe_notes.build_notes,
        apply_notes=recipe_notes.apply_notes,
        detect_facts=recipe_suggest.detect_facts,
        draft=recipe_suggest.draft,
        questions_for=recipe_suggest.questions_for,
        ai_availability=recipe_ai.ai_availability,
        ai_draft=recipe_ai.ai_draft,
        compress=recipe_ai.compress,
        first_call_text=recipe_ai.first_call_text,
        diff_reports=recipe_diff.diff_reports,
        confirm_items=recipe_confirm.confirm_items,
        breaking_changes=recipe_confirm.breaking_changes,
        grid_max_cells=xlsx_cells.GRID_MAX_CELLS,
        draft_max_rows=recipe_suggest.DRAFT_MAX_ROWS,
        units=tuple(recipe_parsers.UNITS),
        resolve_model=recipe_ai.resolve_draft_model,
        propose_fixes=recipe_fixes.propose_fixes,
        fix_ops=recipe_fixes.fix_ops,
        selection_edit=recipe_select.selection_edit,
        replay_compare=recipe_select.replay_compare,
        align_to_contract=recipe_suggest.align_to_contract,
        compare_recipes=recipe_compare.compare_recipes,
        plan_accumulate=recipe_accumulate.plan_accumulate,
        classify_changes=recipe_accumulate.classify_changes,
        materialize_union=recipe_accumulate.materialize_union,
        build_union_notes=recipe_notes.build_union_notes,
    )


def pipeline() -> Pipeline:
    """本模块实际用的接缝。测试 monkeypatch 它。"""
    return default_pipeline()


@dataclass
class CommitResult:
    """提交的结果（CommitOut 除 source 以外的字段，接口层补上 source）。"""

    import_id: str
    snapshot_id: str
    build_id: str
    recipe_id: str
    build_reused: bool
    unchanged: bool
    source_id: str = ""
    #: 服务端已有的同版本数据文件被改过，这次用试运行的文件恢复了它（第 8 节遗留项 1）
    build_restored: bool = False
    #: 启用后当前版本含几期
    parts: int = 1
    #: 与此前某个版本内容相同，直接启用了那个版本（publish_snapshot 的复用、复活）
    snapshot_reused: bool = False


# ==========================================================================
# JSON 往返
# ==========================================================================


def _jsonable(obj: Any) -> Any:
    """dataclass（含嵌套）、日期 → 能存进 JSON 列的形状。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def _known(cls: type, data: Any) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in (data or {}).items() if k in names}


def check_from_dict(data: dict[str, Any]) -> CheckResult:
    """试运行存下的核对结果 → CheckResult（提交时还原用，7.8 第 3 步）。"""
    return CheckResult(**_known(CheckResult, data))


def _table_from(data: dict[str, Any]) -> TableOut:
    d = _known(TableOut, data)
    d["columns"] = [ColumnOut(**_known(ColumnOut, c)) for c in d.get("columns") or []]
    return TableOut(**d)


#: Extraction 里需要还原成 dataclass 的字段
_EXTRACTION_PARTS: dict[str, Callable[[Any], Any]] = {
    "problems": lambda v: [Problem(**_known(Problem, x)) for x in v or []],
    "tables": lambda v: [_table_from(x) for x in v or []],
    "ledger": lambda v: [LedgerSheet(**_known(LedgerSheet, x)) for x in v or []],
    "regions": lambda v: [RegionMark(**_known(RegionMark, x)) for x in v or []],
    "period": lambda v: PeriodOut(**_known(PeriodOut, v)) if v else None,
    "context_checks": lambda v: [check_from_dict(x) for x in v or []],
    "derived": lambda v: [DerivedItem(**_known(DerivedItem, x)) for x in v or []],
    "canonicalized": lambda v: [CanonEntry(**_known(CanonEntry, x)) for x in v or []],
    "labels": lambda v: [SegmentLabels(**_known(SegmentLabels, x)) for x in v or []],
    "outside_text": lambda v: [OutsideText(**_known(OutsideText, x)) for x in v or []],
    "sheets": lambda v: SheetsOut(**_known(SheetsOut, v)),
    "axes": lambda v: [AxisOut(**_known(AxisOut, x)) for x in v or []],
    # 期 3：排除的行（契约 ExcludedRows，第 8 节遗留项 4）。期 3 之前的回执没有这个键，按默认值（空列表）
    "rows_excluded": lambda v: [ExcludedRows(**_known(ExcludedRows, x)) for x in v or [] if isinstance(x, dict)],
}


def extraction_from_dict(data: dict[str, Any]) -> Extraction:
    """试运行存下的 asdict(Extraction) → Extraction（提交时还原，说明生成要用 dataclass）。缺的字段取默认值。"""
    kw: dict[str, Any] = {}
    for f in fields(Extraction):
        if f.name not in data:
            continue
        conv = _EXTRACTION_PARTS.get(f.name)
        kw[f.name] = conv(data[f.name]) if conv is not None else copy.deepcopy(data[f.name])
    kw.setdefault("ok", bool(data.get("ok")))
    return Extraction(**kw)


def _facts_from(data: Any) -> DraftFacts:
    data = data or {}
    return DraftFacts(facts=[Fact(**_known(Fact, f)) for f in data.get("facts") or []],
                      candidates=dict(data.get("candidates") or {}),
                      nonnumeric=dict(data.get("nonnumeric") or {}))


def _question_from(data: dict[str, Any]) -> Question:
    return Question(**_known(Question, data))


def _card_from(data: dict[str, Any]) -> Card:
    return Card(**_known(Card, data))


def _draft_from(data: Any) -> Draft | None:
    if not data:
        return None
    return Draft(recipe=data.get("recipe"), complete=bool(data.get("complete")), origin=data.get("origin") or "rules",
                 cards=[_card_from(c) for c in data.get("cards") or []],
                 questions=[_question_from(q) for q in data.get("questions") or []],
                 facts=_facts_from(data.get("facts")), failures=list(data.get("failures") or []),
                 failures_for_model=list(data.get("failures_for_model") or []))


def _draft_out(data: Any) -> dict[str, Any] | None:
    """Draft 给界面的形状：不含给模型看的那份原因（failures_for_model），也不重复带系统发现。"""
    if not data:
        return None
    return {k: v for k, v in data.items() if k not in ("failures_for_model", "facts")}


def _full_form(recipe: Any) -> Any:
    """补全默认值的完整形式：问题的 effects 按它写路径、界面表单按它显示。schema 不过时原样返回。"""
    if not isinstance(recipe, dict):
        return recipe
    try:
        return Recipe.model_validate(recipe).model_dump(mode="json")
    except Exception:  # noqa: BLE001 - 不合法的配方照存，由静态校验报问题
        return copy.deepcopy(recipe)


def _period_inputs(ctx: dict[str, Any] | None) -> dict[str, PeriodInput] | None:
    """暂存区存的人工录入 → 执行器要的 PeriodInput。"""
    out: dict[str, PeriodInput] = {}
    for key, v in (ctx or {}).items():
        try:
            out[key] = PeriodInput(start=_dt.date.fromisoformat(str(v["start"])),
                                   end=_dt.date.fromisoformat(str(v["end"])), signed_by=v.get("signed_by"))
        except (KeyError, TypeError, ValueError):
            continue
    return out or None


def _now_iso() -> str:
    return utcnow().isoformat()


# ==========================================================================
# 读原件、扫描、网格（线程里跑）
# ==========================================================================


@dataclass
class _Book:
    raw: bytes
    scan: WorkbookScan
    #: 每个有内容的可见工作表的前 draft_max_rows 行（起草、系统发现、压缩表示用）
    grids: dict[str, Grid]


@dataclass
class _Snap:
    """暂存区在线程里要用的几项。ORM 对象不进线程：属性过期时会回库加载，线程里没有事件循环。"""

    id: str
    kind: str
    raw_sha256: str
    scan: dict[str, Any] | None
    file_name: str
    recipe_origin: str | None
    #: 起点配方（上传新一期的现行配方）的哈希：工作配方与它相同时按重放校验
    base_sha: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    #: 现行配方（canonical 形式）：数据源**当前的** current_recipe_id 对应的那一版（3.3，评审一-m7）。kind 为
    #: reupload / redraft 时给，静态校验据此放宽常量沿用、只查改动过的部分的认领；first / switch 为 None
    base_recipe: dict[str, Any] | None = None


def _snap(staging: ImportStaging, *, base_sha: str | None = None, recipe_origin: str | None = None,
          base_recipe: dict[str, Any] | None = None) -> _Snap:
    return _Snap(id=staging.id, kind=staging.kind, raw_sha256=staging.raw_sha256,
                 scan=copy.deepcopy(staging.scan), file_name=staging.file_name or "",
                 recipe_origin=recipe_origin if recipe_origin is not None else staging.recipe_origin,
                 base_sha=base_sha, context=copy.deepcopy(staging.context_inputs or {}),
                 base_recipe=copy.deepcopy(base_recipe) if isinstance(base_recipe, dict) else None)


def _read_raw(sha: str) -> bytes:
    try:
        path = raw_store.raw_path(sha)
    except ValueError:
        raise ImportRefused(409, "raw_missing", _RAW_GONE) from None
    if not path.is_file():
        raise ImportRefused(409, "raw_missing", _RAW_GONE)
    return path.read_bytes()


def _open_book(p: Pipeline, raw: bytes, scan: WorkbookScan) -> _Book:
    grids: dict[str, Grid] = {}
    for s in scan.sheets:
        if s.state != "visible" or s.bounds is None or not s.nonempty:
            continue
        grids[s.name] = p.read_grid(raw, scan, s.name, max_rows=p.draft_max_rows)
    return _Book(raw=raw, scan=scan, grids=grids)


def _book_of(p: Pipeline, snap: _Snap) -> _Book:
    raw = _read_raw(snap.raw_sha256)
    scan = p.scan_from_json(snap.scan) if snap.scan else p.scan(raw)
    return _open_book(p, raw, scan)


async def _in_slot(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    """CPU 密集的一步：占一个解析名额、放进线程池。只包这一步，用完即还。"""
    async with table_versions.parse_slot():
        return await asyncio.to_thread(fn, *args, **kw)


class _Capture:
    """包一层干跑：记下最后一次的 Extraction（起草器内部干跑过，不必再跑一遍）。"""

    def __init__(self, fn: Callable[[Recipe], Extraction]) -> None:
        self.fn = fn
        self.last: Extraction | None = None

    def __call__(self, recipe: Recipe) -> Extraction:
        self.last = self.fn(recipe)
        return self.last


def _dry_fn(p: Pipeline, book: _Book, filename: str, ctx: dict[str, Any] | None) -> Callable[[Recipe], Extraction]:
    """交互时的干跑（不建库）。有工作表的非空格超过 GRID_MAX_CELLS 时只在前 draft_max_rows 行上部分干跑（4.9）。"""
    big = any(s.state == "visible" and s.nonempty > p.grid_max_cells for s in book.scan.sheets)
    inputs = _period_inputs(ctx)

    def run(recipe: Recipe) -> Extraction:
        return p.execute(recipe, book.raw, filename, book.scan, None, context_inputs=inputs,
                         max_rows=p.draft_max_rows if big else None)
    return run


def _validate(p: Pipeline, snap: _Snap, work: Any, facts: DraftFacts | None) -> tuple[Recipe | None, list[Any]]:
    """静态校验。上传新一期、工作配方仍是现行配方时按重放校验（不查认领）；其余带系统发现查认领。

    有现行配方（kind 为 reupload / redraft）时把它作为 base 传下去（3.3）：常量可以沿用已确认的 pick，认领检查
    只查与现行配方相比改动过的部分。base 只在有值时传：期 2 测试里的假 validate 没有这个关键字（12.0）。"""
    if not isinstance(work, dict):
        return None, []
    if snap.kind == "reupload" and snap.base_sha:
        parsed, problems = p.validate(work, facts=None, origin="replay")
        if parsed is not None and p.recipe_sha256(parsed) == snap.base_sha:
            return parsed, problems
    extra = {"base": snap.base_recipe} if snap.base_recipe is not None else {}
    return p.validate(work, facts=facts, origin=_VALIDATE_ORIGIN.get(snap.recipe_origin or "", "manual"), **extra)


def _cards(p: Pipeline, recipe: Any, facts: DraftFacts, grids: dict[str, Grid],
           ext: Extraction | None) -> tuple[list[Card], list[Question]]:
    if not isinstance(recipe, dict):
        return [], []
    try:
        return p.questions_for(recipe, facts, grids, extraction=ext)
    except Exception as e:  # noqa: BLE001 - 卡片和问题是辅助信息，出错不能拖垮回答、改配方
        logger.warning("questions_for failed: %s", type(e).__name__)
        return [], []


#: 交互时的干跑自己出错时给界面的问题（异常只进日志：消息里可能带着格子的值）
_DRY_FAILED = "按配方检查表格未能完成：检查时出错。请修改配方后重试，或直接试运行查看完整结果"


def _safe_dry(run: Callable[[Recipe], Extraction], recipe: Recipe) -> Extraction:
    """交互时的干跑只给着色和问题列表用：出错时变成一条问题，不让回答、改配方整个失败。试运行不走这里。"""
    from app.data.xlsx_scan import UnsupportedTable

    try:
        return run(recipe)
    except UnsupportedTable as e:
        message = str(e) or _DRY_FAILED
    except Exception as e:  # noqa: BLE001
        logger.warning("interactive dry run failed: %s", type(e).__name__)
        message = _DRY_FAILED
    return Extraction(ok=False, problems=[Problem("dry_run_failed", "structure", message,
                                                  model_message="dry_run_failed")])


@dataclass
class _Eval:
    """一次「静态校验 + 干跑 + 卡片」的结果。"""

    recipe: Any
    sha: str | None
    parsed: Recipe | None
    problems: list[Any]
    facts: DraftFacts
    extraction: Extraction | None
    cards: list[Card]
    questions: list[Question]


def _evaluate(p: Pipeline, book: _Book, snap: _Snap, work: Any, *, facts: DraftFacts | None = None) -> _Eval:
    """工作配方 → 系统发现（按这份配方的写法）、静态校验、干跑、卡片和问题（同步，线程里跑）。"""
    if facts is None:
        facts = p.detect_facts(book.grids, book.scan, recipe=work if isinstance(work, dict) else None)
    parsed, problems = _validate(p, snap, work, facts)
    ext = None
    if parsed is not None and not problems:
        ext = _safe_dry(_dry_fn(p, book, snap.file_name, snap.context), parsed)
    cards, questions = _cards(p, work, facts, book.grids, ext)
    stored = p.canonical(parsed) if parsed is not None else (copy.deepcopy(work) if isinstance(work, dict) else None)
    sha = p.recipe_sha256(parsed) if parsed is not None else None
    return _Eval(recipe=stored, sha=sha, parsed=parsed, problems=list(problems), facts=facts, extraction=ext,
                 cards=cards, questions=questions)


def _problem_json(p: Any) -> dict[str, Any]:
    if isinstance(p, RecipeProblem):
        return {"path": p.path, "code": p.code, "message": p.message}
    return _jsonable(p)


#: 静态校验的问题存库、回显最多几条（SE-5：病态配方能产生几万条，整份存两份再回显）。多出的合成一条
RECIPE_PROBLEMS_MAX = 200


def _problems_json(problems: list[Any]) -> list[dict[str, Any]]:
    out = [_problem_json(x) for x in problems[:RECIPE_PROBLEMS_MAX]]
    if len(problems) > RECIPE_PROBLEMS_MAX:
        out.append({"path": "", "code": "too_many_problems",
                    "message": f"另有 {len(problems) - RECIPE_PROBLEMS_MAX} 个问题没有列出（共 {len(problems)} 个），"
                               "请先按上面的提示修改"})
    return out


def _regions(ext: Extraction | dict[str, Any] | None) -> list[dict[str, Any]]:
    if ext is None:
        return []
    if isinstance(ext, dict):
        return [dict(r) for r in ext.get("regions") or [] if isinstance(r, dict)]
    return [_jsonable(r) for r in ext.regions]


def _ai_card(staging: ImportStaging) -> dict[str, Any] | None:
    """AI 草稿那张「请逐项核对」的卡片（ai_draft 生成，存在 drafts.ai 里）。工作配方仍出自 AI 时一直显示。"""
    if staging.recipe_origin != "ai":
        return None
    draft = ((staging.drafts or {}).get("ai_adopted") or {}).get("draft") or {}
    return next((c for c in draft.get("cards") or [] if c.get("id") == "ai_origin"), None)


def _invalidate_trial(staging: ImportStaging) -> str | None:
    """工作配方或录入变了：试运行作废（键和库清空、状态退回 drafting），回执留着给界面标「已失效」。

    返回旧的试运行库路径：提交之后再删（remove_trial_file）。
    """
    old = staging.trial_path
    staging.trial_key = None
    staging.trial_path = None
    if staging.status in ("trialed", "rejected"):
        staging.status = "drafting"
    return old


def _apply_eval(staging: ImportStaging, ev: _Eval, *, questions: bool) -> str | None:
    """把一次评估写回暂存区（不提交）。配方变了就让试运行作废，返回旧的试运行库路径。"""
    old_sha = staging.recipe_sha256
    staging.recipe = ev.recipe
    staging.recipe_sha256 = ev.sha
    staging.recipe_problems = _problems_json(ev.problems)
    staging.facts = _jsonable(ev.facts)
    staging.draft_problems = [_jsonable(x) for x in ev.extraction.problems] if ev.extraction is not None else []
    staging.draft_partial = bool(ev.extraction is not None and ev.extraction.partial)
    cards = [_jsonable(c) for c in ev.cards]
    card = _ai_card(staging)
    if card is not None and not any(c.get("id") == "ai_origin" for c in cards):
        cards.insert(0, card)
    staging.cards = cards
    if questions:
        staging.questions = [_jsonable(q) for q in ev.questions]
    drafts = dict(staging.drafts or {})
    drafts["marks"] = _regions(ev.extraction)
    staging.drafts = drafts
    staging.updated_at = utcnow()
    if ev.sha is None or ev.sha != old_sha:
        return _invalidate_trial(staging)
    return None


async def _remove_trial(path: str | None) -> None:
    if path:
        await asyncio.to_thread(table_versions.remove_trial_file, path)


def require_open(staging: ImportStaging) -> None:
    if not table_versions.staging_is_open(staging):
        raise ImportRefused(409, "staging_closed", _CLOSED)


async def _still_open(session: AsyncSession, staging: ImportStaging) -> None:
    """慢步骤之后再看一眼：期间被放弃、过期或提交了的，不再往上写。"""
    await session.refresh(staging, ["status"])
    require_open(staging)


# ==========================================================================
# 现行版本：配方、导入、上一期回执
# ==========================================================================


async def current_recipe(session: AsyncSession, source: DataSource | None) -> TableRecipe | None:
    if source is None or not source.current_recipe_id:
        return None
    return await session.get(TableRecipe, source.current_recipe_id)


async def _require_recipe_source(session: AsyncSession, source: DataSource) -> TableRecipe:
    row = await current_recipe(session, source)
    if row is None:
        raise ImportRefused(409, "not_recipe_source", _NOT_RECIPE)
    return row


async def current_import(session: AsyncSession, source: DataSource | None) -> tuple[SourceSnapshot | None,
                                                                                    TableImport | None]:
    """源的当前快照和其中最近的那一期导入（期 2 替换模式下一个快照只有一期）。"""
    if source is None or not source.current_snapshot_id:
        return None, None
    snap = await session.get(SourceSnapshot, source.current_snapshot_id)
    if snap is None or not snap.imports:
        return snap, None
    return snap, await session.get(TableImport, str(snap.imports[-1]))


async def _prev_doc(session: AsyncSession, source: DataSource | None,
                    imp: TableImport | None) -> dict[str, Any] | None:
    """差异卡、确认项的「上一期导入回执」。

    按配方导入的源取现行导入的导入清单（7.7：回执、核对含 C1 C2、统计期、配方哈希都在里面），不取
    TableBuild.report——构建可能被复用，回执里没有 C1、C2，文件名也不是那一次的。简单导入的源（切换）
    取期 1 的构建回执，没有格子账，差异卡只给 rows、period。
    """
    if source is None or imp is None:
        return None
    if source.current_recipe_id:
        from app.core import artifact_store

        manifest = artifact_store.load(imp.manifest_artifact) if imp.manifest_artifact else None
        if not isinstance(manifest, dict):
            raise ImportRefused(500, "manifest_missing",
                                "现行版本的导入清单不在了，无法与上一期比较。请联系管理员检查数据目录")
        return manifest
    build = await session.get(TableBuild, imp.build_id) if imp.build_id else None
    report = dict(build.report or {}) if build is not None else {}
    report.setdefault("tables", [])
    return {"receipt": report}


def effective_origin(staging: ImportStaging, base_origin: str | None) -> str:
    """提交时配方记的 origin，也是确认项的 recipe_origin。

    人手改过（recipe_origin=manual）而起点出自 AI（这个暂存区采用过 AI 草稿，或现行配方本来就是 ai / mixed）
    时记 mixed：AI 类确认项（ai_origin、rename、columns）照出。规则草稿改过的记 manual，不出 AI 类。
    """
    origin = staging.recipe_origin or "manual"
    if origin != "manual":
        return origin
    ai_used = bool(((staging.drafts or {}).get("ai_adopted") or {}).get("draft"))
    return "mixed" if ai_used or base_origin in ("ai", "mixed") else "manual"


#: 配方记录（TableRecipe.origin）、导入清单只记这四种来源：界面按 RECIPE_ORIGIN_LABEL 显示，没有别的键
STORED_ORIGINS = ("rules", "ai", "manual", "mixed")


def stored_origin(origin: str | None, staging: ImportStaging | None = None) -> str:
    """配方入库（TableRecipe.origin、导入清单的 recipe.origin）时记的来源，只取 STORED_ORIGINS 之一。

    rules_redraft 是**这个暂存区**采用了按规则重新起草的结果（6.3）：只用来让本次的确认清单列出全部配方类项并出
    redraft_adopted。它不能原样进配方记录：下个月上传新一期时，reupload 拿现行配方的 origin 给暂存区赋值，
    recipe_confirm 见到 rules_redraft 就又出 redraft_adopted、又列全部配方类项，每个月都要再勾一遍并没有发生的
    「采用重新起草」，违背「每月重传不打扰人」；卡片上还会露出英文键名（评审意见）。所以：没再改过的记 rules（内容
    就是规则起草器给的，名字按来源对齐过）；采用之后又用修复按钮、框选改过（还有未被覆盖的修改）的记 manual，
    与首次导入时规则草稿加修复记 manual 一致。staging 为 None 时（给暂存区赋初值，防住已经入库的旧值）按没改过算。"""
    if origin == "rules_redraft":
        live = staging is not None and any(isinstance(e, dict) and not e.get("superseded")
                                           for e in staging.edits or [])
        return "manual" if live else "rules"
    return origin if origin in STORED_ORIGINS else "manual"


# ==========================================================================
# 暂存、上传新一期、修改配方
# ==========================================================================


@dataclass
class _Staged:
    scan_json: dict[str, Any]
    previews: list[dict[str, Any]]
    facts: DraftFacts
    draft: Draft | None
    ev: _Eval | None


def _stage_work(p: Pipeline, raw: bytes, filename: str) -> _Staged:
    """首次导入（或切换）的暂存：扫描、网格预览、规则起草（带干跑）、对草稿做一次静态校验。"""
    scan = p.scan(raw)
    book = _open_book(p, raw, scan)
    dry = _Capture(_dry_fn(p, book, filename, {}))
    d = p.draft(scan, book.grids, filename, validate=lambda data, f, o: p.validate(data, facts=f, origin=o),
                dry_run=dry)
    previews = [p.preview(g) for g in book.grids.values()]
    ev = None
    if d.recipe is not None:
        snap = _Snap(id="", kind="first", raw_sha256="", scan=None, file_name=filename, recipe_origin="rules")
        parsed, problems = _validate(p, snap, d.recipe, d.facts)
        ext = dry.last if parsed is not None and not problems else None
        if parsed is not None and not problems and ext is None:
            ext = _safe_dry(dry, parsed)
        ev = _Eval(recipe=p.canonical(parsed) if parsed is not None else copy.deepcopy(d.recipe),
                   sha=p.recipe_sha256(parsed) if parsed is not None else None, parsed=parsed,
                   problems=list(problems), facts=d.facts, extraction=ext, cards=list(d.cards),
                   questions=list(d.questions))
    return _Staged(scan_json=p.scan_to_json(scan), previews=previews, facts=d.facts, draft=d, ev=ev)


def _new_staging(*, source_id: str, name: str, description: str, kind: str, filename: str, raw: bytes,
                 signed_by: str | None, base_recipe_id: str | None = None) -> ImportStaging:
    now = utcnow()
    return ImportStaging(
        id=new_id(), source_id=source_id, source_name=name, description=description or "", kind=kind,
        status="drafting", raw_sha256=hashlib.sha256(raw).hexdigest(), file_name=(filename or "")[:500],
        file_size=len(raw), answers={}, cards=[], questions=[], recipe_problems=[], draft_problems=[],
        draft_partial=False, context_inputs={}, ai_usage=[], ai_consents=[], base_recipe_id=base_recipe_id,
        signed_by=(signed_by or None), created_at=now, updated_at=now,
    )


def _fill(staging: ImportStaging, staged: _Staged, ev: _Eval | None, *, answers_base: Any) -> None:
    staging.scan = staged.scan_json
    staging.grid_preview = staged.previews
    staging.facts = _jsonable(staged.facts)
    staging.answers_base = copy.deepcopy(answers_base)
    if ev is not None:
        staging.recipe = ev.recipe
        staging.recipe_sha256 = ev.sha
        staging.recipe_problems = _problems_json(ev.problems)
        staging.facts = _jsonable(ev.facts)
        staging.draft_problems = [_jsonable(x) for x in ev.extraction.problems] if ev.extraction else []
        staging.draft_partial = bool(ev.extraction is not None and ev.extraction.partial)
        staging.cards = [_jsonable(c) for c in ev.cards]
        staging.questions = [_jsonable(q) for q in ev.questions]
    drafts = dict(staging.drafts or {})
    drafts["marks"] = _regions(ev.extraction if ev is not None else None)
    staging.drafts = drafts


async def stage_upload(session: AsyncSession, *, raw: bytes, filename: str, name: str, description: str,
                       signed_by: str | None) -> ImportStaging:
    """POST imports/stage：原件存档、扫描、网格预览、系统发现、规则起草（加干跑）。

    同名的简单导入源 → kind=switch（提交时原地切成按配方导入，源 id 和工具名不变）；没有同名源 → first，
    这时就分配源 id，提交时用它建 DataSource。名字格式由接口层查。扫描认不出时 UnsupportedTable 原样抛出。
    """
    p = pipeline()
    await table_versions.expire_stagings(session)
    # 只读进程（没拿到版本存储的守卫）存不了原件：别先花时间扫描、起草（第 8 节遗留项 5）
    table_versions.require_store_writable()
    existing = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
    if existing is None:
        kind, source_id = "first", new_id()
    elif existing.current_recipe_id:
        raise ImportRefused(409, "recipe_source_exists",
                            f"「{name}」已经按配方导入。更新数据请用「上传新一期」，调整取数规则请用「修改配方」")
    elif (existing.origin or "manual") == "upload":
        kind, source_id = "switch", existing.id
    else:
        raise ImportRefused(409, "name_taken", f"已存在名为「{name}」的数据源（非上传表格），请换一个名称")
    staged = await _in_slot(_stage_work, p, raw, filename)
    staging = _new_staging(source_id=source_id, name=name, description=description, kind=kind,
                           filename=filename, raw=raw, signed_by=signed_by)
    staging.recipe_origin = "rules"
    d = staged.draft
    _fill(staging, staged, staged.ev, answers_base=_full_form(d.recipe) if d and d.recipe is not None else None)
    drafts = dict(staging.drafts or {})
    drafts.update({"rules": _jsonable(d) if d is not None else None, "ai": None})
    staging.drafts = drafts
    await table_versions.store_staging(session, staging, raw)
    return staging


def _replay_work(p: Pipeline, raw: bytes, current: dict[str, Any], filename: str, snap: _Snap,
                 *, evaluate: bool) -> tuple[_Staged, _Eval | None]:
    """上传新一期 / 修改配方：扫描（或取存下的扫描）、按现行配方的写法取系统发现，修改配方时再评估一遍。"""
    scan = p.scan_from_json(snap.scan) if snap.scan else p.scan(raw)
    book = _open_book(p, raw, scan)
    facts = p.detect_facts(book.grids, scan, recipe=current)
    staged = _Staged(scan_json=p.scan_to_json(scan), previews=[p.preview(g) for g in book.grids.values()],
                     facts=facts, draft=None, ev=None)
    if not evaluate:
        # 上传新一期：现行配方原样重放（不查认领），试运行紧接着做，这里不另干跑
        parsed, problems = _validate(p, snap, current, facts)
        cards, questions = _cards(p, current, facts, book.grids, None)
        return staged, _Eval(recipe=p.canonical(parsed) if parsed is not None else copy.deepcopy(current),
                             sha=p.recipe_sha256(parsed) if parsed is not None else None, parsed=parsed,
                             problems=list(problems), facts=facts, extraction=None, cards=cards,
                             questions=questions)
    return staged, _evaluate(p, book, snap, current, facts=facts)


async def reupload(session: AsyncSession, source: DataSource, *, raw: bytes, filename: str,
                   signed_by: str | None) -> ImportStaging:
    """POST {source_id}/reupload：原件存档、扫描、系统发现，按当前配方自动试运行（不调模型）→ 回执 + 差异卡。

    系统发现按现行配方的写法存下，用户随后改配方时静态校验用。现行配方原样重放不查认领（见模块 docstring）。
    """
    p = pipeline()
    await table_versions.expire_stagings(session)
    table_versions.require_store_writable()
    row = await _require_recipe_source(session, source)
    full = _full_form(row.recipe)
    staging = _new_staging(source_id=source.id, name=source.name, description=source.description or "",
                           kind="reupload", filename=filename, raw=raw, signed_by=signed_by, base_recipe_id=row.id)
    staging.recipe_origin = stored_origin(row.origin or "manual")
    snap = _snap(staging, base_sha=row.recipe_sha256, base_recipe=row.recipe)
    staged, ev = await _in_slot(_replay_work, p, raw, full, filename, snap, evaluate=False)
    _fill(staging, staged, ev, answers_base=full)
    drafts = dict(staging.drafts or {})
    drafts.update({"rules": None, "ai": None})
    staging.drafts = drafts
    await table_versions.store_staging(session, staging, raw)
    if staging.recipe_sha256 and not staging.recipe_problems:
        staging_id = staging.id
        try:
            staging = await run_trial(session, staging, context_inputs=None, signed_by=signed_by)
        except Exception:
            # 自动试运行没跑完（全量执行时文件读不了、上一期的清单不在了……）：接口回的是错误，前端拿不到
            # 这个暂存区的 id，留着它只会占着原件直到过期。放弃它（瘦身、原件没有别的引用就删），再抛原错误
            await _discard_quietly(session, staging_id)
            raise
    return staging


async def _discard_quietly(session: AsyncSession, staging_id: str) -> None:
    """出错之后放弃刚建的暂存区。收拾本身出错只记日志，不盖住原来的错误。"""
    try:
        await session.rollback()
        row = await session.get(ImportStaging, staging_id, populate_existing=True)
        if row is not None and table_versions.staging_is_open(row):
            await discard_staging(session, row)
    except Exception:  # noqa: BLE001
        logger.exception("failed to discard staging %s after an error", staging_id)


async def redraft(session: AsyncSession, source: DataSource, *, signed_by: str | None) -> ImportStaging:
    """POST {source_id}/redraft：取当前导入的原件、以当前配方为工作配方，重做系统发现和干跑 → drafting。"""
    p = pipeline()
    await table_versions.expire_stagings(session)
    table_versions.require_store_writable()
    row = await _require_recipe_source(session, source)
    _snapshot, imp = await current_import(session, source)
    if imp is None or imp.raw_state != "kept" or not raw_store.raw_exists(imp.raw_sha256):
        raise ImportRefused(409, "raw_missing",
                            "当前版本的原件已清除或没有保存，无法在原文件上修改配方。请用「上传新一期」带上文件进入修改")
    raw = await asyncio.to_thread(_read_raw, imp.raw_sha256)
    full = _full_form(row.recipe)
    staging = _new_staging(source_id=source.id, name=source.name, description=source.description or "",
                           kind="redraft", filename=imp.file_name, raw=raw, signed_by=signed_by,
                           base_recipe_id=row.id)
    staging.recipe_origin = stored_origin(row.origin or "manual")
    snap = _snap(staging, base_recipe=row.recipe)
    staged, ev = await _in_slot(_replay_work, p, raw, full, imp.file_name, snap, evaluate=True)
    _fill(staging, staged, ev, answers_base=full)
    drafts = dict(staging.drafts or {})
    drafts.update({"rules": None, "ai": None})
    staging.drafts = drafts
    await table_versions.store_staging(session, staging, raw)
    return staging


# ==========================================================================
# 回答问题、改配方
# ==========================================================================


def _fill_reason(node: Any, reason: str | None) -> Any:
    """effects 里整个字符串等于 REASON_SLOT 的值换成理由（不做子串替换，契约 Question.effects）。"""
    if isinstance(node, str):
        return (reason or "") if node == REASON_SLOT else node
    if isinstance(node, list):
        return [_fill_reason(x, reason) for x in node]
    if isinstance(node, dict):
        return {k: _fill_reason(v, reason) for k, v in node.items()}
    return node


def replay_answers(p: Pipeline, base: Any, questions: list[Question], answers: dict[str, dict[str, Any]]) -> Any:
    """工作配方 = answers_base 按问题顺序重放全部回答的 effects（7.2）。effects 用不上时抛 ValueError。"""
    doc = copy.deepcopy(base)
    for q in questions:
        a = answers.get(q.id)
        if a is None:
            continue
        ops = q.effects.get(str(a.get("value")), [])
        if ops:
            doc = p.apply_patch(doc, _fill_reason(ops, a.get("reason")))
    return doc


def _check_answers(questions: list[Question], answers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """回答必须指向存在的问题和选项；needs_reason 的选项要有 1–200 字的理由。返回规范后的回答。"""
    by_id = {q.id: q for q in questions}
    clean: dict[str, dict[str, Any]] = {}
    for qid, a in answers.items():
        q = by_id.get(qid)
        if q is None:
            raise ImportRefused(422, "answer_invalid", f"问题「{qid}」不存在，请刷新后重新回答")
        if not isinstance(a, dict):
            raise ImportRefused(422, "answer_invalid", f"问题「{q.text}」的回答格式不对，请重新选择")
        value = a.get("value")
        option = next((o for o in q.options if str(o.get("value")) == str(value)), None)
        if option is None:
            raise ImportRefused(422, "answer_invalid", f"问题「{q.text}」没有「{value}」这个选项，请重新选择")
        reason = a.get("reason")
        reason = str(reason).strip() if reason is not None else ""
        if len(reason) > ANSWER_REASON_MAX:
            raise ImportRefused(422, "answer_invalid",
                                f"问题「{q.text}」的理由超过 {ANSWER_REASON_MAX} 字，请精简后重新提交")
        if option.get("needs_reason") and not reason:
            raise ImportRefused(422, "answer_invalid",
                                f"问题「{q.text}」选择「{option.get('label') or value}」时需要写明理由")
        clean[qid] = {"value": str(option.get("value")), "reason": reason or None}
    return clean


async def _base_of(session: AsyncSession, staging: ImportStaging) -> tuple[str | None, dict[str, Any] | None]:
    """(按重放校验时比的现行配方哈希, 静态校验的 base)。一律取数据源**当前的** current_recipe_id（3.3，评审一-m7）：
    启用旧版本、移除、作废都会改它，静态校验、只列有变化的确认项、试运行三处取的「现行配方」必须一致。暂存区的
    base_recipe_id 只是创建时的记录。哈希只给上传新一期（修改配方期 2 起就一律带系统发现校验）。"""
    if staging.kind not in ("reupload", "redraft"):
        return None, None
    source = await session.get(DataSource, staging.source_id)
    row = await current_recipe(session, source)
    if row is None:
        return None, None
    return (row.recipe_sha256 if staging.kind == "reupload" else None), copy.deepcopy(row.recipe)


async def _snap_of(session: AsyncSession, staging: ImportStaging, *, recipe_origin: str | None = None) -> _Snap:
    sha, base = await _base_of(session, staging)
    return _snap(staging, base_sha=sha, recipe_origin=recipe_origin, base_recipe=base)


def _reeval_work(p: Pipeline, snap: _Snap, work: Any) -> _Eval:
    return _evaluate(p, _book_of(p, snap), snap, work)


def _doc_sha(doc: Any) -> str:
    """一份起点配方的哈希（canonical 口径，与 recipe_sha256 同一种写法：去掉等于默认值的字段）。合 schema 的按
    canonical 算，不合的（PUT 照存的半成品）按原样算。延续回答、撤销时比对「是不是同一份起点」用。"""
    try:
        return table_versions.sha_json(Recipe.model_validate(doc).model_dump(mode="json", exclude_defaults=True))
    except Exception:  # noqa: BLE001 - 不合 schema 的照样要能比
        return table_versions.sha_json(doc)


def carry_answers(p: Pipeline, base: Any, questions: list[Question], old_questions: list[Question],
                  answers: dict[str, dict[str, Any]], *,
                  reapply: frozenset[str] | set[str] = frozenset()) -> tuple[dict[str, dict[str, Any]],
                                                                         list[dict[str, Any]]]:
    """起点换了之后（PUT、应用修复或框选、撤销修改），旧回答逐个判断还成不成立（第 8 节遗留项 2）。

    - 新问题里有同 id、同选项值的：把它的 effects（理由照旧填）应用到新起点上，结果与新起点的 canonical 形式
      相同（新起点已经体现了这个回答），保留为已选；否则丢弃，reason=changed；
    - 新问题里已经没有这个 id：丢弃，reason=gone。
    返回 (保留的回答, answers_dropped)。answers_dropped 每项 {id, text, reason}：text 取自旧问题（gone 的问题在新
    问题里已经没有了，界面只拿到 id 的话要么露出 id，要么什么都显示不了）。

    为什么不清空（期 2 的写法）：PUT 进来的配方本来就体现了之前的回答（占位符已经在列表里），清空之后看起来像没
    答过，用户会以为要重答，或者以为自己的选择丢了。

    reapply（只有撤销修改用）：这几个回答是在那次修改**之后**才做的，撤销恢复的起点当然还没有体现它们。按上面的
    规则它们一律判 changed，用户在修改之后做的选择就被撤销连带撤掉了（3.1「修改之后又回答过的问题不应该被撤
    掉」）。所以对它们放宽成「effects 能干净地应用到恢复的起点上就保留」，等于在恢复的起点上把这道题重新答一遍。
    """
    by_id = {q.id: q for q in questions}
    old_by = {q.id: q for q in old_questions}
    target = _doc_sha(base)
    kept: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    for qid, a in (answers or {}).items():
        old_q = old_by.get(qid)
        text = old_q.text if old_q is not None else (by_id[qid].text if qid in by_id else qid)
        q = by_id.get(qid)
        if q is None:
            dropped.append({"id": qid, "text": text, "reason": "gone"})
            continue
        value = str((a or {}).get("value"))
        if not any(str(o.get("value")) == value for o in q.options):
            dropped.append({"id": qid, "text": text, "reason": "changed"})
            continue
        ops = q.effects.get(value, [])
        try:
            doc = p.apply_patch(copy.deepcopy(base), _fill_reason(ops, (a or {}).get("reason"))) if ops else base
        except (ValueError, TypeError, KeyError, IndexError):
            doc = None
        if doc is not None and (_doc_sha(doc) == target or qid in reapply):
            kept[qid] = {"value": value, "reason": (a or {}).get("reason") or None}
        else:
            dropped.append({"id": qid, "text": text, "reason": "changed"})
    return kept, dropped


@dataclass
class _Rebased:
    """起点换掉之后的一次评估：按新起点重出的问题、延续下来的回答、丢掉的回答。"""

    ev: _Eval
    answers: dict[str, dict[str, Any]]
    dropped: list[dict[str, Any]]


def _rebase_on(p: Pipeline, book: _Book, snap: _Snap, base: Any, old_questions: list[Question],
               answers: dict[str, dict[str, Any]], ev: _Eval | None = None, *,
               reapply: frozenset[str] | set[str] = frozenset()) -> _Rebased:
    """新起点 → 评估（问题按新起点重出）→ 延续回答。保留下来的回答在新起点上一般都是无效果的，工作配方仍是新起点；
    撤销修改时 reapply 的那几条（修改之后才做的回答）有效果，几条回答叠在一起也可能有效果：这两种情况按工作配方
    重新评估，问题仍按起点出。重放失败时先丢掉 reapply 的那几条再试（其余各条单独看都是无效果的），还不行才全丢。"""
    ev = ev or _evaluate(p, book, snap, base)
    kept, dropped = carry_answers(p, base, ev.questions, old_questions, answers, reapply=reapply)

    def drop(ids: list[str]) -> None:
        for qid in ids:
            dropped.append({"id": qid, "text": next((q.text for q in ev.questions if q.id == qid), qid),
                            "reason": "changed"})
            kept.pop(qid, None)

    if kept:
        try:
            work = replay_answers(p, base, ev.questions, kept)
        except ValueError:
            work = None
        if work is None and any(qid in reapply for qid in kept):
            drop([qid for qid in kept if qid in reapply])
            try:
                work = replay_answers(p, base, ev.questions, kept) if kept else base
            except ValueError:
                work = None
        if work is None:
            drop(list(kept))
            work = base
        if _doc_sha(work) != _doc_sha(base):
            again = _evaluate(p, book, snap, work)
            again.questions = ev.questions
            ev = again
    return _Rebased(ev=ev, answers=kept, dropped=dropped)


def _rebase_work(p: Pipeline, snap: _Snap, base: Any, old_questions: list[Question],
                 answers: dict[str, dict[str, Any]], *,
                 reapply: frozenset[str] | set[str] = frozenset()) -> _Rebased:
    return _rebase_on(p, _book_of(p, snap), snap, base, old_questions, answers, reapply=reapply)


def _apply_rebase(staging: ImportStaging, base: Any, rebased: _Rebased, *, origin: str | None) -> str | None:
    """把换起点的结果写回暂存区（不提交）。返回旧的试运行库路径（配方变了才有）。"""
    if origin is not None:
        staging.recipe_origin = origin
    staging.answers_base = copy.deepcopy(base)
    staging.answers = dict(rebased.answers)
    staging.answers_dropped = list(rebased.dropped)
    return _apply_eval(staging, rebased.ev, questions=True)


def _supersede_edits(staging: ImportStaging) -> None:
    """整份替换工作配方（PUT、采用 AI 草稿）：之前的修改标 superseded，不能再撤销，也不再出 fix:* / select:* 确认项
    （它们的效果可能已经不在新配方里），只作为历史进导入清单（3.1，评审丙-M4）。"""
    edits = [dict(e) for e in staging.edits or [] if isinstance(e, dict)]
    if edits:
        staging.edits = [{**e, "superseded": True} for e in edits]


async def answer(session: AsyncSession, staging: ImportStaging, answers: dict[str, dict[str, Any]]) -> ImportStaging:
    """POST imports/{id}/answers：工作配方 = answers_base 重放这一组回答（整组替换已存的回答），
    重做系统发现、静态校验、干跑和卡片。问题集合不变（它按 answers_base 生成）。"""
    p = pipeline()
    require_open(staging)
    questions = [_question_from(q) for q in staging.questions or []]
    clean = _check_answers(questions, answers or {})
    if staging.answers_base is None:
        if clean:
            raise ImportRefused(422, "answer_invalid", "还没有可回答的配方草稿")
        return staging
    # 回答会让旧的试运行库作废、被删掉：只读进程不写版本存储（第 8 节遗留项 5）
    table_versions.require_store_writable()
    try:
        work = replay_answers(p, staging.answers_base, questions, clean)
    except ValueError as e:
        raise ImportRefused(422, "answer_invalid", f"回答没能应用到配方上：{e}") from e
    snap = await _snap_of(session, staging)
    ev = await _in_slot(_reeval_work, p, snap, work)
    await _still_open(session, staging)
    old = _apply_eval(staging, ev, questions=False)
    staging.answers = clean
    # 用户重新回答过了：「需要重新回答」的提示到此为止
    staging.answers_dropped = []
    await session.commit()
    await _remove_trial(old)
    return staging


_REDRAFT_MISMATCH = "采用的配方与重新起草的结果不同，请重新起草后再采用"


def _schema_sha(p: Pipeline, doc: Any) -> str | None:
    """配方的哈希（只看 schema，不做语义校验），与 EditResult.recipe_sha256_after、暂存区的哈希同一口径。"""
    try:
        return p.recipe_sha256(Recipe.model_validate(doc))
    except Exception:  # noqa: BLE001
        return None


async def put_recipe(session: AsyncSession, staging: ImportStaging, recipe: dict[str, Any], *,
                     origin: str = "manual") -> ImportStaging:
    """PUT imports/{id}/recipe：整份配方作为新的起点（answers_base 换成它），照存；静态校验的问题写进
    recipe_problems（有问题也存，只是不能试运行）。

    期 3：回答在新起点上仍然成立的保留（carry_answers），不成立的进 answers_dropped；撤销栈清空，之前的修改标
    superseded（3.1）。origin="rules_redraft"（采用按规则重新起草的结果，6.3）时配方的哈希必须等于最近一次
    redraft-rules 给出的那份（redraft_mismatch）：服务端据此记 recipe_origin=rules_redraft，确认清单按首次导入
    列出全部配方类项并出 redraft_adopted，起草器识别出的定位规则才会逐项经过人的确认（H3）。"""
    p = pipeline()
    require_open(staging)
    base = _full_form(recipe)
    if origin == "rules_redraft":
        sha = _schema_sha(p, base)
        if not staging.redraft_sha256 or sha != staging.redraft_sha256:
            raise ImportRefused(409, "redraft_mismatch", _REDRAFT_MISMATCH)
    table_versions.require_store_writable()
    old_questions = [_question_from(q) for q in staging.questions or []]
    answers = dict(staging.answers or {})
    snap = await _snap_of(session, staging, recipe_origin=origin)
    rebased = await _in_slot(_rebase_work, p, snap, base, old_questions, answers)
    await _still_open(session, staging)
    _supersede_edits(staging)
    old = _apply_rebase(staging, base, rebased, origin=origin)
    await session.commit()
    await _remove_trial(old)
    return staging


# ==========================================================================
# 期 3：修复按钮、框选（edits/preview、apply、undo），按规则重新起草
# ==========================================================================


@dataclass
class EditRequest:
    """edits/preview、edits/apply 的请求（接口层解析好）：修复给 fix_id、option、reason；框选给 selection。"""

    kind: str
    fix_id: str = ""
    option: str = ""
    reason: str | None = None
    selection: Selection | None = None


def _fix_inputs(staging: ImportStaging) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, str] | None]:
    """提议按哪些问题现算（3.1）：当前试运行没失效且拒收时用 trial.problems，否则用 draft_problems；再加上
    recipe_problems，以及当前试运行的 extraction.sheets.renamed。返回 (问题出自哪里, 问题, 配方问题, 改名)。"""
    trial = staging.trial if isinstance(staging.trial, dict) else {}
    if staging.trial_key and trial.get("status") == "rejected":
        where, problems = "trial", [x for x in trial.get("problems") or [] if isinstance(x, dict)]
    else:
        where, problems = "draft", [x for x in staging.draft_problems or [] if isinstance(x, dict)]
    renamed = None
    if staging.trial_key:
        sheets = ((trial.get("extraction") or {}).get("sheets") or {}) if isinstance(trial.get("extraction"), dict) else {}
        got = sheets.get("renamed") if isinstance(sheets, dict) else None
        renamed = {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) and got else None
    recipe_problems = [x for x in staging.recipe_problems or [] if isinstance(x, dict)]
    return where, problems, recipe_problems, renamed


def proposals_for(p: Pipeline, staging: ImportStaging) -> tuple[list[Any], str]:
    """当前的修复提议（不存库，每次按暂存区里存的问题现算；同样的问题得到同样的 id）。没接上 propose_fixes、
    暂存区已结束或还没有配方时为空。出错只记日志：提议是辅助信息，不能拖垮 StagingOut。"""
    where, problems, recipe_problems, renamed = _fix_inputs(staging)
    if p.propose_fixes is None or not table_versions.staging_is_open(staging) or not isinstance(staging.recipe, dict):
        return [], where
    try:
        return list(p.propose_fixes(_full_form(staging.recipe), problems, recipe_problems, staging.facts or {},
                                    renamed)), where
    except Exception as e:  # noqa: BLE001
        logger.warning("propose_fixes failed: %s", type(e).__name__)
        return [], where


@dataclass
class _EditRun:
    edit: EditResult
    new: Any = None
    rebased: _Rebased | None = None
    replay: Any = None


def _dry_before(p: Pipeline, book: _Book, snap: _Snap, full: Any) -> Extraction | None:
    """框选要的「当前工作配方最近一次干跑」：工作配方合 schema 才干跑（判断选区和已有的块是否重叠）。"""
    if not isinstance(full, dict):
        return None
    try:
        parsed = Recipe.model_validate(full)
    except Exception:  # noqa: BLE001
        return None
    return _safe_dry(_dry_fn(p, book, snap.file_name, snap.context), parsed)


def _edit_work(p: Pipeline, snap: _Snap, full: Any, req: EditRequest, proposal: Any,
               retired: dict[str, list[str]] | None, old_questions: list[Question],
               answers: dict[str, dict[str, Any]]) -> _EditRun:
    """（线程里跑）换算一次修改，ok 时把补丁应用到工作配方上、静态校验（带系统发现和 base）、干跑、重放比对，
    再按新起点重出问题、延续回答（4.4）。预览和应用走同一段，应用时才把结果写回暂存区。"""
    from app.data.recipe_fixes import EditRequestError

    book = _book_of(p, snap)
    if req.kind == "fix":
        edit = p.fix_ops(full, proposal, req.option, req.reason)
    else:
        sel = req.selection
        grid = book.grids.get(sel.sheet) if sel is not None else None
        if grid is None:
            raise EditRequestError("edit_invalid", f"这份文件里没有可以框选的工作表「{sel.sheet if sel else ''}」")
        before = _dry_before(p, book, snap, full)

        def draft_fn() -> Draft:
            return p.draft(book.scan, book.grids, snap.file_name,
                           validate=lambda data, f, o: p.validate(data, facts=f, origin=o),
                           dry_run=_Capture(_dry_fn(p, book, snap.file_name, snap.context)))
        edit = p.selection_edit(full if isinstance(full, dict) else None, grid, sel, extraction=before,
                                draft_fn=draft_fn, retired_names=retired or None)
    if not edit.ok:
        return _EditRun(edit=edit)
    new = p.apply_patch(copy.deepcopy(full) if isinstance(full, dict) else {}, edit.ops)
    rebased = _rebase_on(p, book, snap, new, old_questions, answers)
    replay = None
    ext = rebased.ev.extraction
    if req.kind == "selection" and ext is not None and p.replay_compare is not None:
        window = p.draft_max_rows if ext.partial else None
        replay = p.replay_compare(edit, ext, window_rows=window)
    return _EditRun(edit=edit, new=new, rebased=rebased, replay=replay)


async def _edit_context(session: AsyncSession, staging: ImportStaging, req: EditRequest
                        ) -> tuple[Pipeline, _Snap, Any, Any, dict[str, list[str]] | None]:
    """预览、应用共用的准备：工作配方的完整形式、要用的提议（fix_stale）、退役名字（框选撞名检查用）。"""
    p = pipeline()
    require_open(staging)
    full = _full_form(staging.recipe) if isinstance(staging.recipe, dict) else None
    proposal = None
    if req.kind == "fix":
        if p.fix_ops is None:
            raise ImportRefused(409, "fix_stale", "这条修复建议已不在当前的列表里，请刷新后重新查看")
        proposal = next((x for x in proposals_for(p, staging)[0] if x.id == req.fix_id), None)
        if proposal is None:
            raise ImportRefused(409, "fix_stale", "这条修复建议已不在当前的列表里：问题在打开建议之后有了变化。"
                                                  "请重新查看问题和修复建议")
    elif p.selection_edit is None:
        raise ImportRefused(422, "edit_invalid", "这个服务端没有提供框选")
    retired: dict[str, list[str]] | None = None
    if req.kind == "selection":
        from app.data import source_versions

        source = await session.get(DataSource, staging.source_id)
        snapshot = (await session.get(SourceSnapshot, source.current_snapshot_id)
                    if source is not None and source.current_snapshot_id else None)
        try:
            retired = source_versions.retired_names(source_versions.snapshot_manifest(snapshot))
        except source_versions.PartProblem:
            retired = None
    snap = await _snap_of(session, staging)
    return p, snap, full, proposal, retired


async def _run_edit(session: AsyncSession, staging: ImportStaging, req: EditRequest) -> tuple[Pipeline, Any, _EditRun]:
    from app.data.recipe_fixes import EditRequestError

    p, snap, full, proposal, retired = await _edit_context(session, staging, req)
    old_questions = [_question_from(q) for q in staging.questions or []]
    try:
        run = await _in_slot(_edit_work, p, snap, full, req, proposal, retired, old_questions,
                             dict(staging.answers or {}))
    except EditRequestError as e:
        raise ImportRefused(422, e.code or "edit_invalid", str(e)) from e
    except ValueError as e:
        raise ImportRefused(422, "edit_invalid", str(e) or "修改请求的格式不对") from e
    return p, full, run


async def _accumulate_change(session: AsyncSession, p: Pipeline, staging: ImportStaging, new: Any) -> str | None:
    """预览里的 accumulate_change：当前版本是按期累积时，修改后的配方对当前各期的分档（2.5）；否则 None。"""
    if p.classify_changes is None:
        return None
    from app.data import source_versions

    source = await session.get(DataSource, staging.source_id)
    snapshot = (await session.get(SourceSnapshot, source.current_snapshot_id)
                if source is not None and source.current_snapshot_id else None)
    if snapshot is None or (snapshot.mode or "replace") != "accumulate" or not source.current_recipe_id:
        return None
    try:
        parts = await source_versions.load_parts(session, snapshot)
        tables = await source_versions.current_tables(session, snapshot)
        status = source_versions.retired_status(source_versions.snapshot_manifest(snapshot))
        cc = p.classify_changes([x.info for x in parts], Recipe.model_validate(new), current_tables=tables,
                                retired_status=status)
    except Exception as e:  # noqa: BLE001 - 分档只是提示
        logger.warning("classify_changes for preview failed: %s", type(e).__name__)
        return None
    return str(cc.change)


async def edit_preview(session: AsyncSession, staging: ImportStaging, req: EditRequest, *,
                       seq: Any = None) -> dict[str, Any]:
    """POST imports/{id}/edits/preview（4.4、9.4 EditPreview）：换算、应用到工作配方的副本上、静态校验、干跑、重放
    比对、对现行配方的破坏性变化、新旧对照。不改暂存区。换算不了时照样 200，ok=false、problems 写原因。"""
    p, full, run = await _run_edit(session, staging, req)
    edit = run.edit
    out = _jsonable(edit)
    out.update({"recipe_sha256_before": staging.recipe_sha256, "recipe_problems": [], "dry_run": None,
                "replay": None, "breaking": {}, "accumulate_change": None, "compare": None, "seq": seq})
    if not edit.ok:
        return out
    ev = run.rebased.ev
    out["recipe_problems"] = _problems_json(ev.problems)
    if ev.extraction is not None:
        out["dry_run"] = {"problems": [_jsonable(x) for x in ev.extraction.problems],
                          "partial": bool(ev.extraction.partial), "marks": _regions(ev.extraction)}
    out["replay"] = _jsonable(run.replay) if run.replay is not None else None
    source = await session.get(DataSource, staging.source_id)
    row = await current_recipe(session, source)
    if row is not None and isinstance(run.new, dict):
        # 9.4：breaking 是 table_changes 算出的「表名 → 人话列表」，不是 EditResult.breaking 那个布尔（WP-0 交接）
        try:
            out["breaking"] = dict(p.breaking_changes(row.recipe, run.new))
        except Exception:  # noqa: BLE001 - 修改后的配方不合 schema 时没有可比的表结构
            out["breaking"] = {}
    if p.compare_recipes is not None and isinstance(full, dict) and isinstance(run.new, dict):
        try:
            out["compare"] = p.compare_recipes(full, run.new)
        except Exception:  # noqa: BLE001
            out["compare"] = None
    out["accumulate_change"] = await _accumulate_change(session, p, staging, run.new)
    return out


def _edit_out(entry: dict[str, Any], *, last: bool, open_: bool) -> dict[str, Any]:
    """修改记录给界面的形状：不带撤销用的修改前起点；undoable 只给最后一条、未被覆盖的。"""
    out = {k: copy.deepcopy(v) for k, v in entry.items() if k not in EDIT_INTERNAL and k not in EDIT_MANIFEST_ONLY}
    out["superseded"] = bool(entry.get("superseded"))
    out["undoable"] = bool(last and open_ and not entry.get("superseded"))
    return out


async def edit_apply(session: AsyncSession, staging: ImportStaging, req: EditRequest, *, expected_sha256: str | None,
                     signed_by: str | None) -> ImportStaging:
    """POST imports/{id}/edits/apply：在同一个工作配方上重新换算，哈希必须等于预览时的 expected_sha256（edit_stale：
    预览之后配方、选项或理由变了；理由进配方哈希，界面必须原样提交预览时的理由）。之后新配方作为新的起点，回答按
    carry_answers 延续，试运行作废。修改记录存修改前的工作配方和回答（撤销用）、修改后起点的哈希（undo_stale 的兜底），
    以及补丁和所选的选项、理由或选区（进导入清单的 edits）。"""
    p, full, run = await _run_edit(session, staging, req)
    edit = run.edit
    if not edit.ok:
        why = "；".join(x.message for x in edit.problems[:3]) or "这次修改已无法应用到当前的配方上"
        raise ImportRefused(422, "edit_not_applicable", why)
    if not expected_sha256 or edit.recipe_sha256_after != expected_sha256:
        raise ImportRefused(409, "edit_stale", "配方在预览之后有变化（或选项、理由与预览时不同），请重新预览")
    table_versions.require_store_writable()
    await _still_open(session, staging)
    edits = [dict(e) for e in staging.edits or [] if isinstance(e, dict)]
    # 撤销用的「修改前的起点」存修改前的**工作配方**（完整形式），不存 answers_base：补丁打在工作配方上（新起点 =
    # 工作配方 + 补丁，已经体现了此前的回答），撤销时恢复的起点必须与它对称，此前的回答在恢复的起点里也都已体现，
    # carry_answers 才会照常保留它们。存 answers_base 的话，有效果的回答（例如导入模式选了每期替换）在撤销时一律
    # 判 changed 丢掉，配方悄悄退回草稿的默认值（评审意见：撤销丢回答）。修改前的回答另存一份，撤销时据此分出
    # 哪些是修改之后才做的（见 edit_undo）
    before_base = full if isinstance(full, dict) else copy.deepcopy(staging.answers_base)
    entry = {
        "seq": max((int(e.get("seq") or 0) for e in edits), default=0) + 1,
        "kind": req.kind, "key": edit.key, "title": edit.title, "summary": list(edit.summary),
        "at": _now_iso(), "signed_by": (signed_by or staging.signed_by or None),
        "recipe_sha256_before": staging.recipe_sha256, "recipe_sha256_after": edit.recipe_sha256_after,
        "superseded": False,
        # 修改的内容本身（3.1「修改记录（谁、何时、补丁）……只进导入清单 edits」）：补丁，以及人选的是什么——修复
        # 记提议 id、选项和理由，框选记选区（Selection.to_json，键是 as，不用 asdict）。导入清单据此能复原这次
        # 改了什么，不只剩人话摘要。StagingOut 不给 ops（_edit_out 去掉），界面用不上
        "ops": copy.deepcopy(list(edit.ops)),
        **({"fix": {"id": req.fix_id, "option": req.option, "reason": req.reason}} if req.kind == "fix" else {}),
        **({"selection": req.selection.to_json()} if req.kind == "selection" and req.selection is not None else {}),
        "before": {"answers_base": copy.deepcopy(before_base), "answers": copy.deepcopy(dict(staging.answers or {})),
                   "recipe_origin": staging.recipe_origin},
        "base_sha256_after": _doc_sha(run.new),
    }
    # 修改是人做的：照 PUT 记 manual（采用过 AI 草稿的由 effective_origin 记 mixed）。采用按规则重新起草的结果之后
    # 再用修复按钮，仍记 rules_redraft：确认清单照旧列出全部配方类项和 redraft_adopted，起草器识别出的定位规则不能
    # 因为补了一个封闭补丁就免于逐项确认（H3）
    origin = "rules_redraft" if staging.recipe_origin == "rules_redraft" else "manual"
    staging.edits = [*edits, entry]
    old = _apply_rebase(staging, run.new, run.rebased, origin=origin)
    await session.commit()
    await _remove_trial(old)
    return staging


async def edit_undo(session: AsyncSession, staging: ImportStaging) -> ImportStaging:
    """POST imports/{id}/edits/undo：撤销最近一次修改（3.1）。先比对当前起点的哈希等于最后一条修改的
    base_sha256_after（否则 undo_stale：之后又被改过；正常流程不会出现，PUT 会清空撤销栈，这是兜底），再恢复修改前的
    起点（修改前的工作配方，见 edit_apply）和 recipe_origin。回答按 carry_answers 延续，不是整份恢复成修改前的回答：
    修改前的回答已体现在恢复的起点里，照常保留；修改之后又回答过的问题不该被撤掉，在恢复的起点上重新应用（reapply）。"""
    p = pipeline()
    require_open(staging)
    edits = [dict(e) for e in staging.edits or [] if isinstance(e, dict)]
    last = edits[-1] if edits else None
    if last is None or last.get("superseded") or not isinstance(last.get("before"), dict):
        raise ImportRefused(409, "nothing_to_undo", "没有可以撤销的修改（整份替换配方之后，之前的修改不能再撤销）")
    if _doc_sha(staging.answers_base) != last.get("base_sha256_after"):
        raise ImportRefused(409, "undo_stale", "这次修改之后配方又被改过，无法直接撤销；请在配方面板中手工改回")
    table_versions.require_store_writable()
    before = last["before"]
    base = copy.deepcopy(before.get("answers_base"))
    origin = before.get("recipe_origin")
    old_questions = [_question_from(q) for q in staging.questions or []]
    # 恢复的起点是修改前的工作配方，修改前的回答都已体现在里面。要判断的回答 = 修改前的回答，再叠上现在的回答：
    # 修改时因为补丁而丢掉的回答，撤销后又成立了，照常恢复为已选；修改之后才做（或改过）的回答，恢复的起点还没有
    # 体现，按 reapply 在恢复的起点上重新应用，不跟着撤销一起撤掉（3.1）
    prior = {str(k): v for k, v in (before.get("answers") or {}).items() if isinstance(v, dict)}
    current = {str(k): v for k, v in (staging.answers or {}).items() if isinstance(v, dict)}

    def same(a: Any, b: Any) -> bool:
        return (isinstance(a, dict) and isinstance(b, dict) and str(a.get("value")) == str(b.get("value"))
                and (a.get("reason") or None) == (b.get("reason") or None))

    later = {qid for qid, a in current.items() if not same(prior.get(qid), a)}
    snap = await _snap_of(session, staging, recipe_origin=origin)
    rebased = await _in_slot(_rebase_work, p, snap, base, old_questions, {**prior, **current}, reapply=later)
    # 只对现在的回答报「需要重新回答」：修改前的回答若在修改时已经丢掉、撤销后仍不成立，修改那一刻已经提示过
    rebased.dropped = [d for d in rebased.dropped if d.get("id") in current]
    await _still_open(session, staging)
    staging.edits = edits[:-1]
    old = _apply_rebase(staging, base, rebased, origin=origin)
    await session.commit()
    await _remove_trial(old)
    return staging


@dataclass
class _Redrafted:
    draft: Draft
    aligned: dict[str, Any] | None
    alignment: list[str]
    compare: dict[str, Any] | None
    sha: str | None


def _redraft_rules_work(p: Pipeline, snap: _Snap, current: dict[str, Any]) -> _Redrafted:
    """（线程里跑）对暂存区的文件跑规则起草（不调用模型），名字按来源对齐到现行配方，再和现行配方对照（6.3）。"""
    book = _book_of(p, snap)
    dry = _Capture(_dry_fn(p, book, snap.file_name, snap.context))
    d = p.draft(book.scan, book.grids, snap.file_name, validate=lambda data, f, o: p.validate(data, facts=f, origin=o),
                dry_run=dry)
    if d.recipe is None:
        # 失败原因只留在 draft.failures：alignment 是「名字对齐」的报告，界面把它放在那个标题下面；抄一份过去，同一句话
        # 就在错误的标题下再出现一次（评审意见；check-manage 对这种情形伪造的也是 alignment: []）
        return _Redrafted(draft=d, aligned=None, alignment=[], compare=None, sha=None)
    candidate = _full_form(d.recipe)
    if p.align_to_contract is not None:
        aligned, report = p.align_to_contract(candidate, current)
    else:
        aligned, report = candidate, []
    try:
        parsed = Recipe.model_validate(aligned)
    except Exception:  # noqa: BLE001
        return _Redrafted(draft=d, aligned=None, compare=None, sha=None,
                          alignment=[*report, "对齐后的配方格式不对，无法采用；请在配方面板中修改现行配方"])
    full = parsed.model_dump(mode="json")
    cmp = p.compare_recipes(current, full) if p.compare_recipes is not None else None
    return _Redrafted(draft=d, aligned=full, alignment=list(report), compare=cmp, sha=p.recipe_sha256(parsed))


async def redraft_rules(session: AsyncSession, staging: ImportStaging) -> dict[str, Any]:
    """POST imports/{id}/redraft-rules（6.3）：不改工作配方，只把对齐后配方的哈希记在 redraft_sha256，「采用」时
    PUT recipe（origin=rules_redraft）核对它。首次导入、切换那里已有规则草稿，不提供（redraft_not_offered）。"""
    p = pipeline()
    require_open(staging)
    if staging.kind not in ("reupload", "redraft"):
        raise ImportRefused(409, "redraft_not_offered", "首次导入已有规则起草的结果，不需要重新起草")
    table_versions.require_store_writable()
    source = await session.get(DataSource, staging.source_id)
    row = await _require_recipe_source(session, source) if source is not None else None
    if row is None:
        raise ImportRefused(409, "not_recipe_source", _NOT_RECIPE)
    current = _full_form(row.recipe)
    snap = await _snap_of(session, staging)
    out = await _in_slot(_redraft_rules_work, p, snap, current)
    await _still_open(session, staging)
    staging.redraft_sha256 = out.sha
    staging.updated_at = utcnow()
    await session.commit()
    return {"draft": _draft_out(_jsonable(out.draft)), "aligned_recipe": out.aligned, "alignment": out.alignment,
            "compare": out.compare}


# ==========================================================================
# AI 起草
# ==========================================================================


def ai_offered(staging: ImportStaging) -> bool:
    """首次导入（含切换）、规则草稿不完整时才提供 AI 起草（6.2）。"""
    if staging.kind not in ("first", "switch"):
        return False
    rules = (staging.drafts or {}).get("rules") or {}
    return not rules.get("complete")


@dataclass
class _Gate:
    avail: AiAvailability
    #: 解析出的模型对象和 id（pipeline 没有 resolve_model 时为 None / ""，由 ai_draft 自己解析）
    model: Any = None
    model_id: str = ""


async def _ai_gate(session: AsyncSession, staging: ImportStaging) -> _Gate:
    require_open(staging)
    if staging.kind not in ("first", "switch"):
        raise ImportRefused(409, "ai_not_offered",
                            "上传新一期和修改配方时不提供 AI 起草：工作配方是已确认的现行配方。请在配方面板中修改")
    if not ai_offered(staging):
        raise ImportRefused(409, "ai_not_needed", "规则起草已经完整，不需要 AI 起草。请检查配方后试运行")
    p = pipeline()
    if p.resolve_model is not None:
        model, model_id, avail = await p.resolve_model(session)
    else:
        model, model_id, avail = None, "", await p.ai_availability(session)
    if not avail.available or (p.resolve_model is not None and model is None):
        raise ImportRefused(409, "ai_unavailable", avail.reason or "AI 起草不可用。请到「设置 → 模型接入」检查")
    return _Gate(avail=avail, model=model, model_id=model_id or avail.model or "")


def consent_token(text_sha256: str, avail: AiAvailability) -> str:
    """同意框绑定的令牌：发送的全文 + 接入名 + 模型 id（AU-7、SE-4）。

    只按全文算的话，看完预览之后把助手的模型从本机换成外部服务，全文不变、令牌不变，内容就发给了同意框里
    没写的那个接入。三者任一变了，POST draft-ai 就回 409 ai_preview_stale，要重新查看。
    """
    return table_versions.sha_json({"text_sha256": text_sha256, "provider": avail.provider or "",
                                    "model": avail.model or ""})


async def _mask_headers(session: AsyncSession, staging: ImportStaging) -> set[str]:
    """切换时，原数据源遮罩的列在原表里的表头（压缩表示里这些列只给画像）。首次导入没有遮罩列。"""
    if staging.kind != "switch":
        return set()
    from app.data.engine import masked_columns

    source = await session.get(DataSource, staging.source_id)
    # 遮罩列名不区分大小写（与证据面板、裁判的遮罩一致，SE-9）
    masked = {m.lower() for m in masked_columns(source.options if source is not None else None)}
    if not masked:
        return set()
    _snapshot, imp = await current_import(session, source)
    build = await session.get(TableBuild, imp.build_id) if imp is not None and imp.build_id else None
    heads: set[str] = set()
    for t in ((build.report or {}).get("tables") or []) if build is not None else []:
        for c in t.get("columns") or [] if isinstance(t, dict) else []:
            if isinstance(c, dict) and str(c.get("name") or "").lower() in masked and c.get("header"):
                heads.add(str(c["header"]))
    return heads


def _ai_failure(staging: ImportStaging, message: str, *, retry: bool = True) -> str:
    """AI 起草失败的原话补上下一步（见 _AI_NEXT）。"""
    has_work = isinstance(staging.recipe, dict) and bool(staging.recipe)
    return f"{message.rstrip('。')}。{_AI_NEXT[(retry, has_work)]}"


@dataclass
class _AiPrep:
    book: _Book
    facts: DraftFacts
    compressed: Any
    text: str
    sha256: str


def _ai_prep_work(p: Pipeline, snap: _Snap, rules: Draft | None, mask: set[str]) -> _AiPrep:
    """读原件、系统发现（规则起草的分段 id，与半成品配方一致）、压缩表示、首次调用的全文。"""
    book = _book_of(p, snap)
    facts = p.detect_facts(book.grids, book.scan)
    compressed = p.compress(book.grids, book.scan, facts, mask_headers=frozenset(mask))
    text = p.first_call_text(compressed, grids=book.grids, scan=book.scan, rules_draft=rules)
    return _AiPrep(book=book, facts=facts, compressed=compressed, text=text,
                   sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


async def _ai_prep(session: AsyncSession, staging: ImportStaging) -> _AiPrep:
    from app.data.recipe_ai import AiTooLarge

    p = pipeline()
    rules = _draft_from((staging.drafts or {}).get("rules"))
    mask = await _mask_headers(session, staging)
    try:
        return await _in_slot(_ai_prep_work, p, _snap(staging), rules, mask)
    except AiTooLarge as e:
        raise ImportRefused(413, "ai_too_large", _ai_failure(staging, str(e), retry=False)) from e


async def ai_preview(session: AsyncSession, staging: ImportStaging) -> dict[str, Any]:
    """GET imports/{id}/draft-ai/preview：首次调用要发给模型的全文（系统提示 + 首次提示词，含单位词表、
    规则半成品、格式说明）。sha256 是同意令牌（全文 + 接入名 + 模型 id，见 consent_token），POST draft-ai 时
    带回来比对；text_sha256 是全文本身的哈希，留痕用。"""
    gate = await _ai_gate(session, staging)
    prep = await _ai_prep(session, staging)
    return {"text": prep.text, "chars": len(prep.text), "sha256": consent_token(prep.sha256, gate.avail),
            "text_sha256": prep.sha256, "model": gate.avail.model, "provider": gate.avail.provider}


def _usage_total(usage: list[dict[str, Any]]) -> tuple[int, float]:
    tokens = sum(int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0) for u in usage)
    return tokens, round(sum(float(u.get("cost_usd") or 0) for u in usage), 6)


def _adopt_ai_work(p: Pipeline, book: _Book, snap: _Snap, work: Any) -> _Eval:
    return _evaluate(p, book, snap, work)


#: 正在 AI 起草的暂存区（进程内）：同一暂存区不并发起草，否则两次发送各花一份钱，同意记录、用量还会互相覆盖
#: （SE-11）。事件循环单线程，查和加之间没有 await，不会有两个请求同时通过；进程重启后自然清空
_AI_RUNNING: set[str] = set()


async def run_ai_draft(session: AsyncSession, staging: ImportStaging, *, consent: bool,
                       preview_sha256: str | None, signed_by: str | None) -> ImportStaging:
    """POST imports/{id}/draft-ai（流程见 _run_ai_draft）。同一暂存区同时只允许一次 AI 起草：已有一次在跑时回
    409 ai_in_progress，不记同意、不调模型。"""
    if consent is not True:
        raise ImportRefused(422, "ai_consent_required", "需要先查看并同意发送的内容，才能让 AI 起草")
    if staging.id in _AI_RUNNING:
        raise ImportRefused(409, "ai_in_progress", "这次导入正在由 AI 起草，请等它完成后再试")
    _AI_RUNNING.add(staging.id)
    try:
        return await _run_ai_draft(session, staging, preview_sha256=preview_sha256, signed_by=signed_by)
    finally:
        _AI_RUNNING.discard(staging.id)


async def _run_ai_draft(session: AsyncSession, staging: ImportStaging, *,
                        preview_sha256: str | None, signed_by: str | None) -> ImportStaging:
    """POST imports/{id}/draft-ai：用户同意后才发送。

    发送的全文必须就是预览给用户看的那份：服务端重算的 sha256 与 preview_sha256 不同 → 409 ai_preview_stale。
    压缩表示在解析名额内先算好、经 compressed= 交给 ai_draft；等模型回复期间不占名额。同意记录在发送前落库；
    用量（AiDraftResult.usage）拿到就单独提交，之后才看暂存区是否还开着（期间结束了 → 409 staging_closed，
    用量照留）、草稿能不能用（不能 → 502 ai_failed）。成功时工作配方换成 AI 草稿（answers_base 换掉、
    回答清空、recipe_origin=ai）。
    """
    from app.data.recipe_ai import AiTooLarge, AiUnavailable

    p = pipeline()
    gate = await _ai_gate(session, staging)
    avail = gate.avail
    prep = await _ai_prep(session, staging)
    token = consent_token(prep.sha256, avail)
    if not preview_sha256 or preview_sha256 != token:
        raise ImportRefused(409, "ai_preview_stale",
                            "要发送的内容或接收它的模型与查看时不同（原件、设置或助手的模型有变化），请重新查看后再同意")
    await _still_open(session, staging)
    consent_rec = {"signed_by": signed_by or None, "at": _now_iso(), "model": avail.model,
                   "provider": avail.provider, "compressed_sha256": prep.compressed.sha256,
                   "preview_sha256": token, "text_sha256": prep.sha256, "chars": len(prep.text)}
    staging.ai_consents = [*(staging.ai_consents or []), consent_rec]
    staging.updated_at = utcnow()
    await session.commit()

    rules = _draft_from((staging.drafts or {}).get("rules"))
    # AI 每次尝试之后的干跑：协程函数，占一个解析名额、在线程池里执行（ai_draft 经 run_dry_async await 它）。
    # 等模型回复期间不占名额，只有这一步占
    dry_sync = _dry_fn(p, prep.book, staging.file_name or "", staging.context_inputs or {})

    async def dry(recipe: Recipe) -> Extraction:
        return await _in_slot(dry_sync, recipe)
    # 同意记录里的模型就是实际调用的模型：把同意前解析出的那个交给 ai_draft，不让它再解析一次（AU-7）
    model_kw: dict[str, Any] = {"model": gate.model, "model_id": gate.model_id} if gate.model is not None else {}
    try:
        result = await p.ai_draft(
            session, grids=prep.book.grids, scan=prep.book.scan, facts=prep.facts, rules_draft=rules,
            filename=staging.file_name or "", validate=lambda d, f, o: p.validate(d, facts=f, origin=o),
            dry_run=dry, compressed=prep.compressed, **model_kw)
    except AiUnavailable as e:
        raise ImportRefused(409, "ai_unavailable", str(e) or "AI 起草不可用。请到「设置 → 模型接入」检查") from e
    except AiTooLarge as e:
        raise ImportRefused(413, "ai_too_large", _ai_failure(staging, str(e), retry=False)) from e

    # 用量先落库，再看暂存区还开不开、草稿能不能用：等模型期间暂存区可能已被放弃、过期或提交了（瘦身后的
    # 行也留着用量），采用草稿时也可能出错，这几次调用花的钱都要记下。已结束的暂存区只追加用量，不写草稿
    await session.refresh(staging)
    usage = [_jsonable(u) for u in result.usage or []]
    tokens, cost = _usage_total(usage)
    draft = result.draft
    summary = {"draft": _jsonable(draft) if draft is not None else None, "usage": usage,
               "attempts": int(result.attempts or 0), "total_tokens": tokens, "cost_usd": cost,
               "error": result.error, "compressed_chars": result.compressed_chars,
               "compressed_sha256": result.compressed_sha256}
    staging.ai_usage = [*(staging.ai_usage or []), *usage]
    if table_versions.staging_is_open(staging):
        # ai：最近一次起草的摘要（成败都记，界面显示它）；ai_adopted：被采用为工作配方的那一次
        staging.drafts = {**(staging.drafts or {}), "ai": summary}
        staging.updated_at = utcnow()
    await session.commit()
    require_open(staging)
    if draft is None or draft.recipe is None:
        # 工作配方不变
        raise ImportRefused(502, "ai_failed", _ai_failure(staging, result.error or "AI 未能起草出合法的配方"))
    base = _full_form(draft.recipe)
    snap = _snap(staging, recipe_origin="ai")
    ev = await _in_slot(_adopt_ai_work, p, prep.book, snap, base)
    await _still_open(session, staging)
    was_ai = effective_origin(staging, None) in ("ai", "mixed")
    staging.drafts = {**(staging.drafts or {}), "ai_adopted": summary}
    staging.recipe_origin = "ai"
    staging.answers_base = copy.deepcopy(base)
    staging.answers = {}
    staging.answers_dropped = []
    # 整份换成 AI 草稿：同 PUT，之前的修改不能再撤销、不再出确认项
    _supersede_edits(staging)
    old = _apply_eval(staging, ev, questions=True)
    if old is None and not was_ai:
        # AI 草稿与工作配方逐字相同（哈希没变）时 _apply_eval 不作废试运行，但来源从规则 / 手改变成了 AI，
        # 提交时要多勾 ai_origin、rename、columns：存下的确认清单已经不全，试运行一并作废（AU-4）
        old = _invalidate_trial(staging)
    await session.commit()
    await _remove_trial(old)
    return staging


# ==========================================================================
# 试运行
# ==========================================================================


def trial_key(staging: ImportStaging, recipe_sha: str, ctx: dict[str, Any]) -> str:
    """试运行的键：工作配方、人工录入、文件名、解析器与执行器版本（外加原件和暂存区）。任何一项变了就要重新试运行。"""
    return table_versions.sha_json({
        "staging_id": staging.id, "raw_sha256": staging.raw_sha256, "recipe_sha256": recipe_sha,
        "context": ctx or {}, "file_name": staging.file_name or "",
        "parser_ver": recipe_parsers.PARSER_VER, "engine_ver": recipe_types.RECIPE_ENGINE_VER,
    })


def _context_allowed(staging: ImportStaging) -> bool:
    """录入统计期只在需要时收：上一次试运行要求录入（needs_input），或者上一次用的就是人工录入。"""
    trial = staging.trial or {}
    if trial.get("status") == "needs_input":
        return True
    period = (trial.get("receipt") or {}).get("period") or {}
    return period.get("source") == "human"


def _build_inputs(ex: Extraction | dict[str, Any]) -> dict[str, Any]:
    """进构建 id 的人工录入：统计期是人工录入时才有（署名不进）。"""
    period = ex.get("period") if isinstance(ex, dict) else (dataclasses.asdict(ex.period) if ex.period else None)
    if isinstance(period, dict) and period.get("source") == "human":
        return {"context": {PERIOD_KEY: {"start": period.get("start"), "end": period.get("end")}}}
    return {}


@dataclass
class _TrialRun:
    parsed: Recipe | None
    problems: list[Any]
    facts: DraftFacts | None = None
    extraction: Extraction | None = None
    db: Path | None = None
    db_sha256: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    null_counts: dict[str, dict[str, int]] | None = None
    notes: SchemaNotes | None = None


def _trial_work(p: Pipeline, snap: _Snap, work: dict[str, Any], facts: DraftFacts | None, path: Path) -> _TrialRun:
    """执行（写临时库）→ settle → 改名 → 算库哈希 → 核对 → 各列空值数 → 说明预览（同步，线程里跑）。"""
    parsed, problems = _validate(p, snap, work, facts)
    if parsed is None or problems:
        return _TrialRun(parsed=parsed, problems=list(problems))
    raw = _read_raw(snap.raw_sha256)
    scan = p.scan_from_json(snap.scan) if snap.scan else p.scan(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先写 .tmp- 再改名：半成品归启动清理（_sweep_tmp）管，发布、回收不会把半个库当成试运行库
    tmp = path.parent / f"{path.name}{raw_store.TMP_MARK}{secrets.token_hex(6)}"
    ex = p.execute(parsed, raw, snap.file_name, scan, str(tmp), context_inputs=_period_inputs(snap.context))
    if not tmp.is_file():
        return _TrialRun(parsed=parsed, problems=[], extraction=ex, checks=list(ex.context_checks))
    try:
        # 先 settle 再算哈希：发布时 _publish_file 还会 settle 一次，库已是回滚日志模式，文件头不会再变
        table_versions.settle_journal(tmp)
        os.replace(tmp, path)
    except BaseException:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{tmp}{suffix}").unlink(missing_ok=True)
        raise
    db_sha = raw_store.sha256_file(path)
    checks = p.run_checks(str(path), ex, parsed)
    nulls = p.column_null_counts(str(path), parsed)
    notes = p.build_notes(parsed, ex, checks, [], null_counts=nulls)
    return _TrialRun(parsed=parsed, problems=[], extraction=ex, db=path, db_sha256=db_sha, checks=checks,
                     null_counts=nulls, notes=notes)


def trial_status(problems: list[Problem], checks: list[CheckResult], notes_problems: list[str]) -> str:
    """5.3：recipe / structure 类问题、不可接受的结构类核对没过 → rejected；input 类问题 → needs_input；
    有可接受、没通过的核对 → needs_decision；否则 passed。confirm 类问题不影响状态。"""
    if notes_problems or any(p.category in ("recipe", "structure") for p in problems):
        return "rejected"
    if any(c.status != "passed" and c.category == "structure" and not c.acceptable for c in checks):
        return "rejected"
    if any(p.category == "input" for p in problems):
        return "needs_input"
    if acceptable_ids(checks):
        return "needs_decision"
    return "passed"


def acceptable_ids(checks: list[CheckResult]) -> list[str]:
    """可以写理由接受、本次没通过的核对。"""
    return [c.id for c in checks if c.acceptable and c.status in ("mismatch", "unverifiable")]


def _receipt_full(ex: Extraction) -> dict[str, Any]:
    """导入回执 = asdict(Extraction) 去掉 problems（7.7、7.6）：axes、tables、ledger、period……一样不少。"""
    data = _jsonable(ex)
    data.pop("problems", None)
    return data


def _receipt_view(full: dict[str, Any], db_sha: str | None) -> dict[str, Any]:
    out = {k: copy.deepcopy(full.get(k)) for k in _RECEIPT_VIEW if k in full}
    canon = list(full.get("canonicalized") or [])
    out["canonicalized"] = canon[:TRIAL_CANON_MAX]
    out["canonicalized_total"] = len(canon)
    out["db_sha256"] = db_sha
    return out


def _checks_summary(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("id", "kind", "title", "status", "category", "checked", "failed", "unverifiable", "reasons", "acceptable")
    return [{k: c.get(k) for k in keys} for c in checks]


@dataclass
class _Context:
    """试运行、提交都要的「现行版本」上下文。"""

    source: DataSource | None
    recipe: TableRecipe | None
    snapshot: SourceSnapshot | None
    imp: TableImport | None
    prev: dict[str, Any] | None


async def _context(session: AsyncSession, staging: ImportStaging) -> _Context:
    source = await session.get(DataSource, staging.source_id)
    row = await current_recipe(session, source)
    snapshot, imp = await current_import(session, source)
    prev = await _prev_doc(session, source, imp)
    if staging.kind in ("reupload", "redraft") and (row is None or prev is None):
        raise ImportRefused(409, "not_recipe_source", _NOT_RECIPE)
    return _Context(source=source, recipe=row, snapshot=snapshot, imp=imp, prev=prev)


def _active_edits(staging: ImportStaging) -> list[dict[str, Any]]:
    """确认项要的修改记录：只含未被覆盖的（3.1），不带撤销用的修改前起点。"""
    return [{k: v for k, v in e.items() if k not in EDIT_INTERNAL}
            for e in staging.edits or [] if isinstance(e, dict) and not e.get("superseded")]


def _confirm_context(staging: ImportStaging, cx: _Context, recipe: Recipe, ex_dict: dict[str, Any],
                     checks: list[Any], diff: list[Any] | None, *, prev: dict[str, Any] | None = None,
                     accumulate: dict[str, Any] | None = None) -> ConfirmContext:
    """确认项的输入。prev 给了就用它（按期累积时上一期是统计期最晚的那一期，替换该期时是被替换的那一期，9.5），
    否则用现行导入的清单。"""
    from app.data.engine import masked_columns

    prev = prev if prev is not None else cx.prev
    old = cx.recipe.recipe if cx.recipe is not None and staging.kind in ("reupload", "redraft") else None
    return ConfirmContext(
        kind=staging.kind, recipe=recipe, old_recipe=old,
        old_schema_cache=(cx.snapshot.schema_cache or {}) if staging.kind == "switch" and cx.snapshot else None,
        extraction=ex_dict, checks=checks, diff=(diff or []) if prev is not None else None, prev=prev,
        recipe_origin=effective_origin(staging, cx.recipe.origin if cx.recipe is not None else None),
        facts=_facts_from(staging.facts),
        mask_columns=masked_columns(cx.source.options) if cx.source is not None else None,
        accumulate=accumulate, edits=_active_edits(staging),
    )


def _structure_problem(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "category": "structure", "message": message, "cells": [], "model_message": code,
            "fix": None}


@dataclass
class _Acc:
    """试运行里按期累积的那一步（2.4、2.6、2.8）：计划、物化好的并集、说明、额外的拒收问题。"""

    plan: dict[str, Any] | None = None
    #: 提交时用的并集（只给服务端，TRIAL_INTERNAL）：试运行文件、并集 id、哈希、登记的选项和回执、各期顺序
    union: dict[str, Any] | None = None
    union_checks: list[dict[str, Any]] | None = None
    notes: SchemaNotes | None = None
    problems: list[dict[str, Any]] = field(default_factory=list)
    #: 差异卡的上一期（替换该期时是被替换的那一期，9.5）；None = 用现行导入
    prev_import_id: str | None = None


def _union_note_new(parsed: Recipe, ex: Extraction, run: _TrialRun, start: str, end: str, acc: list[Acceptance]) -> Any:
    from app.data.recipe_types import UnionNotePart

    return UnionNotePart(recipe=parsed, extraction=ex, checks=list(run.checks), acceptances=list(acc),
                         null_counts=run.null_counts, start=start, end=end)


async def _accumulate_step(session: AsyncSession, p: Pipeline, staging: ImportStaging, cx: _Context, parsed: Recipe,
                           recipe_sha: str, run: _TrialRun, build_id: str) -> _Acc | None:
    """新配方是按期累积、或者当前版本是按期累积时做累积计划；结果有两期及以上时在这里物化并集（提交时只 link），
    并按各期最弱的结论生成说明预览（本期按「全部未接受」）。不需要累积计划时返回 None。

    当前快照某一期的导入清单被改过、统计期与库列对不上（part_tampered）：拒收，当前快照不动。某一期的构建库不在了
    （part_missing）：409，消息指向数据源卡片的「版本」（可以先移除那一期，7.4）。"""
    from app.data import recipe_accumulate as ra
    from app.data import source_versions as sv

    if p.plan_accumulate is None or run.extraction is None or run.db is None:
        return None
    ex = run.extraction
    snapshot = cx.snapshot if cx.source is not None else None
    cur_kind = None if snapshot is None else ("recipe" if cx.source.current_recipe_id else "simple")
    cur_mode = (snapshot.mode or "replace") if cur_kind == "recipe" else None
    if parsed.mode != "accumulate" and cur_mode != "accumulate":
        return None
    period = ex.period
    new_period = (period.start, period.end) if period is not None and period.start and period.end else None
    parts: list[sv.Part] = []
    tables = status = None
    try:
        if cur_kind == "recipe":
            parts = await sv.load_parts(session, snapshot)
            # 退役只按结果各期有数据的列判定（WP-8 修补）：当前版本的表结构里，只留结果各期（当前各期去掉本期要替换的
            # 那一期）的配方里有、或者目标配方里有的列。否则两种情况都会出一条任何一期都没有数据的必勾 retire:*，
            # 文案还写「早期各期保留原值」：移除唯一含某列的那一期之后（留下的全空目标列），以及替换唯一含某列的那一期
            # （被替换掉的旧数据不在新版本里）。替换该期时这一列就是从当前配方里消失了，由 breaking:<表> 要人确认
            gone = _replaced_part(parts, new_period, staging.kind)
            tables = sv.tables_in_parts(await sv.current_tables(session, snapshot),
                                        [x for x in parts if x is not gone], also=parsed)
            status = sv.retired_status(sv.snapshot_manifest(snapshot))
    except sv.PartProblem as e:
        if e.code == "part_missing":
            raise ImportRefused(409, "part_missing", e.message) from e
        return _Acc(problems=[_structure_problem(e.code, e.message)])
    try:
        plan = p.plan_accumulate(new_recipe=parsed, new_period=new_period,
                                 period_source=(period.source if period is not None else "cells"),
                                 staging_kind=staging.kind, current_mode=cur_mode, current_snapshot_kind=cur_kind,
                                 parts=[x.info for x in parts], current_tables=tables, retired_status=status)
    except ValueError as e:
        return _Acc(problems=[_structure_problem("accumulate_rejected", str(e))])
    plan_json = _jsonable(plan)
    rows = {t.name: int(t.rows) for t in ex.tables}
    for entry in plan_json.get("parts") or []:
        if entry.get("new"):
            entry["file_name"], entry["rows"] = staging.file_name or "", dict(rows)
    acc = _Acc(plan=plan_json)
    if plan.action == "rejected":
        if plan.overlaps:
            acc.problems.append(_structure_problem("period_overlap", ra.overlap_message(plan.period, plan.overlaps)))
        else:
            acc.problems.append(_structure_problem("accumulate_rejected", plan.reason or "这份配方不能按期累积"))
        return acc
    if plan.action == "replace_period" and plan.replaces:
        acc.prev_import_id = plan.replaces.get("import_id")
    if len(plan.parts) < 2:
        if plan.dropped:
            # restart、模式切换后只剩本期：快照库就是本期的构建库，说明照单期写，另加「此前各期不在当前版本中」
            # （2.8 开头，评审一-m10）
            acc.notes = _add_single_after_drop(run.notes) if run.notes is not None else None
        return acc
    by_id = {x.info.import_id: x for x in parts}
    start, end = new_period or ("", "")
    union_parts, triples, order = [], [], []
    try:
        for entry in plan.parts:
            if entry.get("new"):
                union_parts.append(UnionPart(import_id=None, start=start, end=end, db_path=str(run.db), recipe=parsed,
                                             db_sha256=run.db_sha256 or "", table_hashes=dict(ex.table_hashes)))
                triples.append((start, end, build_id))
                order.append(None)
            else:
                x = by_id[entry["import_id"]]
                union_parts.append(x.union_part())
                triples.append((x.info.start, x.info.end, x.info.build_id))
                order.append(x.info.import_id)
    except sv.PartProblem as e:
        acc.problems.append(_structure_problem(e.code, e.message))
        return acc
    retired = ra.retired_columns(plan, tables)
    uid = ra.union_id(staging.source_id, triples, recipe_sha, retired=retired)
    out = table_versions.union_trial_path(run.db)
    try:
        report = await _in_slot(p.materialize_union, out, target=parsed, parts=union_parts, retired=retired)
    except ra.UnionError as e:
        if e.code == "part_missing":
            raise ImportRefused(409, "part_missing", str(e)) from e
        acc.problems.append(_structure_problem(e.code, str(e)))
        return acc
    acc.union_checks = [_jsonable(c) for c in report.checks]
    plan_json["union"] = {"union_id": uid, "db_sha256": report.db_sha256, "rows": dict(report.rows)}
    if not report.ok:
        details = [d for c in report.checks if c.status != "passed" for d in (c.details or [c.title])]
        acc.problems.append(_structure_problem("union_failed", "累积后的数据没有通过结构核对："
                                               + "；".join(details[:3])))
        return acc
    note_parts = []
    for entry in plan.parts:
        if entry.get("new"):
            note_parts.append(_union_note_new(parsed, ex, run, start, end, []))
        else:
            note_parts.append(await _in_slot(sv.note_part, by_id[entry["import_id"]]))
    acc.notes = p.build_union_notes(
        parsed, note_parts, union_tables=report.tables, null_counts=report.null_counts,
        part_null_counts=report.part_null_counts, added=report.added, retired=report.retired,
        label_sets=plan.label_sets, gaps=bool(plan.gaps), dropped=False)
    acc.union = {"path": str(out), "union_id": uid, "db_sha256": report.db_sha256,
                 "options": ra.union_options(triples, recipe_sha, retired=retired), "report": _jsonable(report),
                 "imports": order}
    return acc


def _replaced_part(parts: list[Any], new_period: tuple[str, str] | None, staging_kind: str) -> Any | None:
    """当前各期里本期要替换的那一期（与 recipe_accumulate.plan_accumulate 的判定一致）：修改配方、不换文件（redraft）
    时是最晚一期；否则是统计期起止与本期都相同的那一期。没有就 None（追加、部分重叠拒收）。"""
    if not parts:
        return None
    if staging_kind == "redraft":
        return max(parts, key=lambda x: (x.info.start or "", x.info.end or "", x.info.seq or 0))
    if new_period is None:
        return None
    return next((x for x in parts if (x.info.start, x.info.end) == tuple(new_period)), None)


def _add_single_after_drop(notes: SchemaNotes) -> SchemaNotes:
    from app.data.recipe_notes import add_single_after_drop

    try:
        return add_single_after_drop(notes)
    except Exception:  # noqa: BLE001 - 假说明（测试）没有片段时照原样
        return notes


async def _prev_of(session: AsyncSession, cx: _Context, import_id: str | None) -> tuple[dict[str, Any] | None,
                                                                                         str | None]:
    """差异卡、确认项的上一期：给了导入 id（替换该期）就取那一期的导入清单，否则取现行导入的。"""
    if not import_id or cx.imp is None or import_id == cx.imp.id:
        return cx.prev, (cx.imp.file_name if cx.imp is not None else None)
    imp = await session.get(TableImport, import_id)
    doc = await _prev_doc(session, cx.source, imp)
    return doc, (imp.file_name if imp is not None else None)


async def _prior_acceptances(session: AsyncSession, source_id: str, build_id: str) -> list[dict[str, Any]]:
    """同源、build_id 相同的导入记录以前的接受（第 5 节）：只读显示「上次接受的理由」，不预填。

    已作废（revoked 非空）的导入不算：那条理由已经被人判定为不成立，再摆在回执旁边当「上次接受的理由」，等于引导
    用户照着再接受一次（H3）；界面的 PriorAcceptance 也没有「已作废」的标记，用户分辨不出来（评审意见）。"""
    rows = (await session.execute(
        select(TableImport).where(TableImport.source_id == source_id, TableImport.build_id == build_id)
        .order_by(TableImport.seq.desc()))).scalars()
    out: list[dict[str, Any]] = []
    for imp in rows:
        if imp.revoked:
            continue
        for rec in [*(imp.overrides or []), *(imp.waivers or [])]:
            if isinstance(rec, dict) and rec.get("check_id"):
                out.append({"check_id": rec.get("check_id"), "reason": rec.get("reason"),
                            "signed_by": rec.get("signed_by"), "at": rec.get("at"), "import_seq": imp.seq})
    return out


def _same_candidate(cx: _Context, acc: _Acc | None) -> str | None:
    """「与某次导入完全相同」看哪一期：替换该期时是被替换的那一期；当前版本只有一期时是它（期 2 的写法）。"""
    if acc is not None and acc.plan is not None and acc.plan.get("action") == "replace_period":
        return (acc.plan.get("replaces") or {}).get("import_id")
    if cx.snapshot is not None and len(cx.snapshot.imports or []) == 1 and cx.imp is not None:
        return cx.imp.id
    return None


async def run_trial(session: AsyncSession, staging: ImportStaging, *,
                    context_inputs: dict[str, PeriodInput] | None, signed_by: str | None) -> ImportStaging:
    """POST imports/{id}/trial：执行 → 核对 → 说明预览 →（按期累积时）累积计划、物化并集 → 确认项 →（源有当前
    版本时）差异卡。拒收也照常返回，看 trial.status。试运行库写好后删掉同一暂存区旧的试运行库。"""
    p = pipeline()
    require_open(staging)
    if not isinstance(staging.recipe, dict):
        raise ImportRefused(409, "recipe_invalid", "还没有可用的配方，请先在配方面板中填写")
    # 试运行要写试运行库（和并集文件）：只读进程不写版本存储（第 8 节遗留项 5）
    table_versions.require_store_writable()
    ctx = copy.deepcopy(staging.context_inputs or {})
    if context_inputs:
        if not _context_allowed(staging):
            raise ImportRefused(422, "context_not_needed", "这份表格里能解析出统计期，不需要人工录入")
        ctx = {key: {"start": v.start.isoformat(), "end": v.end.isoformat(),
                     "signed_by": (v.signed_by or signed_by or None)} for key, v in context_inputs.items()}
    cx = await _context(session, staging)
    base_sha = cx.recipe.recipe_sha256 if cx.recipe is not None and staging.kind == "reupload" else None
    base_recipe = cx.recipe.recipe if cx.recipe is not None and staging.kind in ("reupload", "redraft") else None
    snap = _snap(staging, base_sha=base_sha, base_recipe=base_recipe)
    snap.context = ctx
    work = _full_form(staging.recipe)
    facts = _facts_from(staging.facts)
    parsed, problems = _validate(p, snap, work, facts)
    if parsed is None or problems:
        raise ImportRefused(409, "recipe_invalid", "配方没有通过检查，请先按配方面板里的提示修改")
    recipe_sha = p.recipe_sha256(parsed)
    key = trial_key(staging, recipe_sha, ctx)
    path = table_versions.trial_db_path(staging.source_id, staging.id, key)
    from app.data.xlsx_scan import UnsupportedTable

    prev_path = staging.trial_path
    try:
        run = await _in_slot(_trial_work, p, snap, work, facts, path)
    except UnsupportedTable as e:
        raise ImportRefused(400, "file_unreadable", str(e) or "无法读取这个文件，请确认是未加密的 Excel 文件") from e
    # 从这里到提交之间出错（期间暂存区结束、差异卡或确认项报错、提交失败）：这次新写的试运行库没有记录
    # 指着，当场删掉（连同附属的并集文件）。路径与旧的相同（同一个键重跑）时不删，那是暂存区记着的库
    try:
        await _still_open(session, staging)
        if run.extraction is None:
            raise ImportRefused(409, "recipe_invalid", "配方没有通过检查，请先按配方面板里的提示修改")
        ex = run.extraction
        notes_problems = list(run.notes.problems) if run.notes is not None else []
        problems_out = [_jsonable(x) for x in ex.problems]
        status = trial_status(ex.problems, run.checks, notes_problems)
        inputs = _build_inputs(ex)
        # 执行器语义标记（table_versions.recipe_engine_semantics）：只有受修补 3 影响的配方（列表跳过空行）才有，
        # 这类配方的构建因此换 id，不会复用修补前的执行器建的旧库；提交时按同一份配方重算，与这里一致
        build_id = table_versions.recipe_build_id(staging.source_id, staging.raw_sha256, recipe_sha, inputs,
                                                  semantics=table_versions.recipe_engine_semantics(parsed))
        acc: _Acc | None = None
        notes = run.notes
        if status in _ACTIVE_TRIAL:
            acc = await _accumulate_step(session, p, staging, cx, parsed, recipe_sha, run, build_id)
            if acc is not None:
                if acc.notes is not None:
                    notes = acc.notes
                    notes_problems = list(acc.notes.problems)
                if acc.problems:
                    problems_out += acc.problems
                    status = "rejected"
                elif notes_problems:
                    status = "rejected"
        problems_out += [{"code": "notes_invalid", "category": "recipe", "message": f"说明没有通过检查：{m}",
                          "cells": [], "model_message": "", "fix": None} for m in notes_problems]
        checks_json = [_jsonable(c) for c in run.checks]
        full = _receipt_full(ex)
        ex_dict = _jsonable(ex)
        plan_json = acc.plan if acc is not None else None
        prev, prev_name = await _prev_of(session, cx, acc.prev_import_id if acc is not None else None)
        diff: list[Any] | None = None
        items: list[ConfirmItem] = []
        # 差异卡和确认项只在能提交的试运行（passed / needs_decision）上算。拒收时执行器在结构问题上停下，
        # 表、格子账可能只写了一半，拿它比会冒出「行数 31 → 0」这类假变化，结构上的变化已经作为拒收问题
        # 报了（7.6）；needs_input 缺统计期，录入后再试运行就有。这两种状态 diff 为 null
        if status in _ACTIVE_TRIAL:
            if prev is not None:
                cur = {"receipt": full, "checks": checks_json, "period": full.get("period"),
                       "recipe_sha256": recipe_sha}
                # 配方变没变按上一期自己的配方比：按期累积时上一期的配方不一定是现行配方（移除之后剩下的期）
                prev_sha = ((prev.get("recipe") or {}).get("sha256") if isinstance(prev.get("recipe"), dict)
                            else None) or (cx.recipe.recipe_sha256 if cx.recipe is not None else None)
                kw: dict[str, Any] = {"accumulate": plan_json} if plan_json is not None else {}
                diff = list(p.diff_reports(prev, cur, prev_file_name=prev_name,
                                           cur_file_name=staging.file_name or "", recipe=parsed,
                                           recipe_changed=prev_sha != recipe_sha, **kw))
            items = list(p.confirm_items(_confirm_context(staging, cx, parsed, ex_dict, run.checks, diff, prev=prev,
                                                          accumulate=plan_json)))
        same = None
        cand = _same_candidate(cx, acc)
        if cand is not None:
            cand_imp = cx.imp if cx.imp is not None and cx.imp.id == cand else await session.get(TableImport, cand)
            if cand_imp is not None and cand_imp.build_id == build_id:
                same = {"id": cand_imp.id, "seq": cand_imp.seq}
        notes_view = ({t: {"comment": n.comment, "columns": dict(n.columns)} for t, n in notes.tables.items()}
                      if notes is not None else {})
        compare = None
        if (p.compare_recipes is not None and cx.recipe is not None and staging.kind in ("reupload", "redraft")
                and cx.recipe.recipe_sha256 != recipe_sha):
            try:
                compare = p.compare_recipes(cx.recipe.recipe, parsed, plan=plan_json)
            except Exception as e:  # noqa: BLE001 - 对照只是辅助信息
                logger.warning("compare_recipes failed: %s", type(e).__name__)
        base_snapshot = cx.source.current_snapshot_id if cx.source is not None else None
        staging.trial = {
            "trial_id": key[:16], "status": status, "receipt": _receipt_view(full, run.db_sha256),
            "problems": problems_out, "checks": checks_json, "confirm_items": [_jsonable(i) for i in items],
            "acceptable": acceptable_ids(run.checks), "notes": notes_view,
            "diff": [_jsonable(d) for d in diff] if diff is not None else None,
            "same_as_import": same, "base_snapshot_id": base_snapshot,
            # 期 3（9.4 TrialOut）
            "accumulate": plan_json, "recipe_compare": compare,
            "union_checks": acc.union_checks if acc is not None else None,
            "prior_acceptances": await _prior_acceptances(session, staging.source_id, build_id),
            # 以下只给服务端：提交时还原 Extraction、重算必勾项、生成说明、发布并集
            "extraction": ex_dict, "null_counts": run.null_counts, "inputs": inputs, "recipe_sha256": recipe_sha,
            "build_id": build_id, "union": acc.union if acc is not None and status in _ACTIVE_TRIAL else None,
            "prev_import_id": acc.prev_import_id if acc is not None else None,
        }
        old = staging.trial_path
        staging.context_inputs = ctx
        staging.trial_key = key
        staging.trial_path = str(run.db) if run.db is not None else None
        staging.base_snapshot_id = base_snapshot
        staging.status = "rejected" if status == "rejected" else "trialed"
        drafts = dict(staging.drafts or {})
        drafts["marks"] = _regions(ex)
        staging.drafts = drafts
        if signed_by:
            staging.signed_by = signed_by
        staging.updated_at = utcnow()
        await session.commit()
    except Exception:
        if run.db is not None and str(run.db) != prev_path:
            await _remove_trial(str(run.db))
        raise
    if old and old != staging.trial_path:
        await _remove_trial(old)
    return staging


# ==========================================================================
# 提交
# ==========================================================================


async def _reopen_drafting(session: AsyncSession, staging: ImportStaging) -> None:
    """提交预检发现试运行用不了：试运行作废、退回 drafting，删掉试运行库。

    用带状态条件的 UPDATE：预检期间（几次 await）暂存区可能已被放弃或过期，不能拿会话里过期的对象把它改回
    drafting（SE-8）。已结束的回 409 staging_closed。"""
    old = staging.trial_path
    done = await session.execute(
        update(ImportStaging)
        .where(ImportStaging.id == staging.id, ImportStaging.status.in_(table_versions.STAGING_OPEN))
        .values(trial_path=None, trial_key=None, status="drafting", updated_at=utcnow())
        .execution_options(synchronize_session=False))
    await session.commit()
    if not done.rowcount:
        raise ImportRefused(409, "staging_closed", _CLOSED)
    await _remove_trial(old)


def _missing_text(items: list[ConfirmItem]) -> str:
    shown = "；".join(f"{i.id}「{i.label}」" for i in items[:20])
    more = f"等 {len(items)} 项" if len(items) > 20 else ""
    return f"还有确认项没有勾选{more}：{shown}"


async def _back_to_drafting(session: AsyncSession, staging_id: str) -> None:
    """发布失败：试运行库已被发布流程消耗（或本来就不在），另开一个事务把暂存区退回 drafting。回执留着。"""
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001
        pass
    row = await session.get(ImportStaging, staging_id, populate_existing=True)
    if row is None or not table_versions.staging_is_open(row):
        return
    old = row.trial_path
    row.trial_path = None
    row.trial_key = None
    row.status = "drafting"
    row.updated_at = utcnow()
    await session.commit()
    await _remove_trial(old)


def _manifest(*, staging_id: str, kind: str, source_id: str, info: table_versions.PublishInfo, trial_sha: str | None,
              ex_dict: dict[str, Any], file: dict[str, Any], recipe_meta: dict[str, Any], checks: list[dict[str, Any]],
              overrides: list[dict[str, Any]], waivers: list[dict[str, Any]], confirmed: list[dict[str, Any]],
              notes: SchemaNotes, ai: dict[str, Any], signed_by: str | None,
              edits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    receipt = copy.deepcopy(ex_dict)
    receipt.pop("problems", None)
    return {
        "format": MANIFEST_FORMAT,
        "source_id": source_id, "import_id": info.import_id, "seq": info.seq, "build_id": info.build_id,
        "build_reused": info.build_reused, "snapshot_id": info.snapshot_id,
        "db_sha256": info.db_sha256, "trial_db_sha256": trial_sha,
        "table_hashes": copy.deepcopy(ex_dict.get("table_hashes") or {}),
        "file": file, "recipe": recipe_meta,
        "engine_ver": recipe_types.RECIPE_ENGINE_VER, "parser_ver": recipe_parsers.PARSER_VER,
        "period": copy.deepcopy(ex_dict.get("period")),
        "checks": checks,
        "acceptances": {"overrides": overrides, "waivers": waivers}, "confirmations": confirmed,
        # receipt 照 asdict(Extraction) 去掉 problems：期 3 的 rows_excluded（排除的行，H2）随之写进来，只取本次
        # 试运行的 Extraction，不取复用构建登记的回执（那里没有这个字段，第 8 节遗留项 4）
        "receipt": receipt,
        "notes": {"templates": dict(notes.templates),
                  "rendered": {t: {"comment": n.comment, "columns": dict(n.columns)} for t, n in notes.tables.items()}},
        "ai": ai,
        # 期 3：修复、框选的修改记录（含已被整份替换覆盖的，作为历史；不带撤销用的修改前起点）
        "edits": copy.deepcopy(list(edits or [])),
        "signed_by": {"name": signed_by, "verified": False},
        "staging_id": staging_id, "kind": kind,
        "created_at": _now_iso(),
    }


#: 发布原语的错误码 → 接口的状态和机读码（10.2 的错误映射表）
_PUBLISH_STATUS = {
    "trial_missing": (409, "trial_required"), "trial_tampered": (409, "trial_tampered"),
    "build_conflict": (409, "build_conflict"), "base_changed": (409, "base_changed"),
    "part_missing": (409, "part_missing"), "part_tampered": (409, "part_tampered"),
    "raw_missing": (409, "raw_missing"), "store_unavailable": (503, "store_unavailable"),
}


def _publish_refused(e: table_versions.PublishError) -> ImportRefused:
    status, code = _PUBLISH_STATUS.get(e.code or "", (500, "publish_failed"))
    return ImportRefused(status, code, str(e) or "导入失败，当前版本未受影响")


def _file_sha(path: Path) -> str | None:
    try:
        return raw_store.sha256_file(path) if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


async def commit_staging(session: AsyncSession, staging: ImportStaging, *, trial_id: str, confirmations: list[str],
                         acceptances: list[Acceptance], signed_by: str | None) -> CommitResult:
    """POST imports/{id}/commit（7.8）：预检 → （与现行导入相同时）不新建记录 → 发布构建、写配方、导入记录、
    快照、清单，切指针。失败时指针不动，暂存区退回 drafting（试运行库已被发布流程消耗）。

    期 3 的三条路径（12.6）：
    - 结果只有一期、快照库就是本期的构建库（替换模式，按期累积的 first / restart / 替换唯一的一期）：照期 2 走
      publish_build，带 snapshot_fields（mode、目标配方：累积快照的 id 要用）；
    - 结果有两期及以上：试运行时已物化好并集，这里走 publish_snapshot（本期构建、并集都 link，写导入清单和快照清单）；
    - 与当前版本里的某一期完全相同（同一构建、同一配方、同一文件名、没有接受、统计期不是人工录入）：不新建任何记录。
      按期累积时这一期是被替换的那一期（替换该期而构建相同）。"""
    p = pipeline()
    require_open(staging)
    trial = staging.trial or {}
    if not isinstance(staging.recipe, dict) or not trial:
        raise ImportRefused(409, "trial_required", "还没有试运行，请先试运行")
    # 试运行作废时回执留着（界面标「已失效」），键清空；这时工作配方可能已改成 schema 不过的样子
    # （PUT 照存），先看键，别让 model_validate 的英文原文经全局 ValueError 处理器漏成 400
    if not staging.trial_key:
        raise ImportRefused(409, "trial_required", _TRIAL_STALE)
    try:
        parsed = Recipe.model_validate(staging.recipe)
    except ValueError:
        raise ImportRefused(409, "trial_required", _TRIAL_STALE) from None
    recipe_sha = p.recipe_sha256(parsed)
    ctx = staging.context_inputs or {}
    key = trial_key(staging, recipe_sha, ctx)
    if staging.trial_key != key or trial.get("trial_id") != key[:16] or trial_id != key[:16]:
        raise ImportRefused(409, "trial_required", _TRIAL_STALE)
    if trial.get("status") not in _ACTIVE_TRIAL:
        raise ImportRefused(409, "trial_not_passed", "试运行没有通过（拒收或需要录入统计期），不能提交")
    # 发布会消耗试运行库：只读进程在动任何东西之前就停下（503），别把用户的试运行白白删掉
    table_versions.require_store_writable()

    cx = await _context(session, staging)
    source = cx.source
    base = staging.base_snapshot_id
    # 先看当前版本有没有被换掉：换了的话上一期回执也换了，按它重算的确认项没有意义（这里只是快速预检，
    # 最终判定在 publish_build / publish_snapshot 的锁内）
    if source is not None and source.current_snapshot_id != base:
        raise ImportRefused(409, "base_changed", _BASE_CHANGED)
    if source is None:
        taken = (await session.execute(
            select(DataSource.id).where(DataSource.name == staging.source_name))).first()
        if taken is not None:
            raise ImportRefused(409, "name_taken", f"名称「{staging.source_name}」已被占用，请放弃这次导入后换一个名称")
    ex_dict = trial.get("extraction") or {}
    checks = [check_from_dict(c) for c in trial.get("checks") or []]
    plan = trial.get("accumulate") if isinstance(trial.get("accumulate"), dict) else None
    union_info = trial.get("union") if isinstance(trial.get("union"), dict) else None
    prev, _prev_name = await _prev_of(session, cx, trial.get("prev_import_id"))
    items = list(p.confirm_items(_confirm_context(staging, cx, parsed, ex_dict, checks, trial.get("diff"), prev=prev,
                                                  accumulate=plan)))
    shown = {str(i.get("id")) for i in trial.get("confirm_items") or [] if isinstance(i, dict)}
    if any(i.required and i.id not in shown for i in items):
        # 服务端此刻要求的确认项比试运行时给界面的多（配方来源、数据源的遮罩设置等在试运行之后变了）：界面上的
        # 清单已经不全，用户勾不到缺的那几项。试运行作废，请重新试运行，而不是回一个对不上清单的 422（AU-4）
        await _reopen_drafting(session, staging)
        raise ImportRefused(409, "trial_required", "试运行之后确认项有变化（配方来源或数据源设置变了），请重新试运行")
    ticked = set(confirmations or [])
    missing = [i for i in items if i.required and i.id not in ticked]
    if missing:
        raise ImportRefused(422, "confirm_required", _missing_text(missing))
    acceptable = list(trial.get("acceptable") or [])
    given: dict[str, Acceptance] = {}
    for a in acceptances or []:
        if a.check_id in acceptable and (a.reason or "").strip():
            given[a.check_id] = a
    too_long = [cid for cid, a in given.items() if len(a.reason.strip()) > ACCEPT_REASON_MAX]
    if too_long:
        raise ImportRefused(422, "acceptance_required",
                            f"接受理由不能超过 {ACCEPT_REASON_MAX} 字：{'、'.join(too_long)}")
    lacking = [cid for cid in acceptable if cid not in given]
    if lacking:
        raise ImportRefused(422, "acceptance_required",
                            f"以下核对没有通过，接受前需要写明理由：{'、'.join(lacking)}")

    path = Path(staging.trial_path) if staging.trial_path else None
    try:
        usable = path is not None and path.is_file() and path.stat().st_size > 0
    except OSError:
        usable = False
    if not usable:
        await _reopen_drafting(session, staging)
        raise ImportRefused(409, "trial_required", "试运行的数据文件已不在，请重新试运行")
    union_path: Path | None = None
    if union_info is not None:
        union_path = Path(str(union_info.get("path") or ""))
        got = await asyncio.to_thread(_file_sha, union_path)
        if got is None:
            await _reopen_drafting(session, staging)
            raise ImportRefused(409, "trial_required", "试运行时生成的累积数据文件已不在，请重新试运行")
        if got != union_info.get("db_sha256"):
            raise ImportRefused(409, "union_tampered", "试运行之后累积数据文件被改动过，请重新试运行")

    human = bool(_build_inputs(ex_dict))
    same = trial.get("same_as_import") or None
    same_imp = await session.get(TableImport, str(same.get("id"))) if same and same.get("id") else None
    current_ids = {str(x) for x in (cx.snapshot.imports or [])} if cx.snapshot is not None else set()
    if (same_imp is not None and not given and not human and same_imp.id in current_ids
            and same_imp.file_name == staging.file_name and source is not None and cx.recipe is not None
            and cx.recipe.recipe_sha256 == recipe_sha):
        # 与现行导入完全相同：不新建任何记录（final.md 2.6 第 5 条）。结束暂存区也在锁内、重读指针之后：
        # 预检之后别的提交切走了指针的话，这里返回的「现行导入」已经不是现行的了
        imp_id, imp_build, source_id = same_imp.id, same_imp.build_id, source.id
        async with table_versions.store_lock():
            cur = (await session.execute(
                select(DataSource.current_snapshot_id, DataSource.current_recipe_id)
                .where(DataSource.id == source_id))).first()
            if cur is None or cur[0] != base:
                raise ImportRefused(409, "base_changed", _BASE_CHANGED)
            await _require_open_locked(session, staging.id)
            old = table_versions.close_staging(staging, "committed")
            staging.committed_import_id = imp_id
            await session.commit()
        await _remove_trial(old)
        return CommitResult(import_id=imp_id, snapshot_id=cur[0] or "", build_id=imp_build,
                            recipe_id=cur[1] or "", build_reused=True, unchanged=True, source_id=source_id,
                            parts=max(len(current_ids), 1))

    ex = extraction_from_dict(ex_dict)
    who = signed_by or staging.signed_by
    acc = [Acceptance(check_id=a.check_id, reason=a.reason.strip(), signed_by=who) for a in given.values()]
    # 这一期自己的说明（带实际的接受）进它的导入清单：版本页逐期显示「改写后的那一句」从这里取（第 5 节）。
    # 快照冻结的说明在多期时另按各期最弱的结论合并（2.8），只剩本期时就是它
    period_notes = p.build_notes(parsed, ex, checks, acc, null_counts=trial.get("null_counts"))
    parts: list[Any] = []
    if union_info is not None:
        from app.data import source_versions as sv

        try:
            parts = await sv.load_parts(session, cx.snapshot)
        except sv.PartProblem as e:
            raise ImportRefused(409, e.code, e.message) from e
        notes = await _union_notes_for_commit(p, parsed, ex, checks, acc, trial, union_info, plan or {}, parts)
    else:
        notes = period_notes
        if plan is not None and plan.get("dropped"):
            notes = _add_single_after_drop(notes)
    problems = list(notes.problems) + [m for m in period_notes.problems if m not in notes.problems]
    if problems:
        raise ImportRefused(422, "notes_invalid", "说明没有通过检查：" + "；".join(problems[:5]))

    # ---- 以下到 publish_build / publish_snapshot 之前，不改会话里的任何对象（7.2 第 1 条最后一项）
    at = _now_iso()
    by_id = {c.id: c for c in checks}
    overrides, waivers = [], []
    for a in acc:
        rec = {"check_id": a.check_id, "reason": a.reason, "signed_by": a.signed_by, "at": at}
        (overrides if by_id[a.check_id].category == "data_quality" else waivers).append(rec)
    confirmed = [{"id": i.id, "label": i.label, "at": at} for i in items if i.id in ticked]
    checks_full = [_jsonable(c) for c in checks]
    period = ex_dict.get("period") or {}
    inputs = trial.get("inputs") or {}
    source_id, staging_id, kind = staging.source_id, staging.id, staging.kind
    name, file_name, file_size, raw_sha = (staging.source_name, staging.file_name or "", int(staging.file_size or 0),
                                           staging.raw_sha256)
    base_origin = cx.recipe.origin if cx.recipe is not None else None
    # 确认项按 effective_origin 算（上面已经算过）；入库只记四种来源之一（rules_redraft → rules / manual）
    origin = stored_origin(effective_origin(staging, base_origin), staging)
    recipe_changed = cx.recipe is None or cx.recipe.recipe_sha256 != recipe_sha
    recipe_id = new_id() if recipe_changed else cx.recipe.id
    recipe_seq_now = cx.recipe.seq if cx.recipe is not None and not recipe_changed else None
    canonical = p.canonical(parsed)
    semantics = table_versions.recipe_engine_semantics(parsed)
    build_id = table_versions.recipe_build_id(source_id, raw_sha, recipe_sha, inputs, semantics=semantics)
    import_id = new_id()
    ai = {"usage": copy.deepcopy(staging.ai_usage or []), "consents": copy.deepcopy(staging.ai_consents or [])}
    edits = [{k: v for k, v in e.items() if k not in EDIT_INTERNAL} for e in staging.edits or [] if isinstance(e, dict)]
    trial_sha = (trial.get("receipt") or {}).get("db_sha256")
    report = copy.deepcopy(ex_dict)
    report.pop("problems", None)
    report.pop("context_checks", None)
    import_fields = {
        "recipe_id": recipe_id, "period_start": period.get("start"), "period_end": period.get("end"),
        "context": copy.deepcopy(period) or None, "checks": _checks_summary(checks_full),
        "overrides": overrides, "waivers": waivers, "confirmations": confirmed, "signed_by": who,
        "staging_id": staging_id,
    }
    if source is None:
        target = DataSource(id=source_id, name=name, kind="sqlite", readonly=True,
                            description=staging.description or "", origin="upload")
    else:
        target = source
    manifest_ids: dict[str, str] = {}
    activation = {"kind": "commit", "import_id": import_id, "signed_by": who}

    def import_manifest(info: table_versions.PublishInfo) -> dict[str, Any]:
        return _manifest(
            staging_id=staging_id, kind=kind, source_id=source_id, info=info, trial_sha=trial_sha, ex_dict=ex_dict,
            file={"name": file_name, "size": file_size, "raw_sha256": raw_sha},
            recipe_meta={"id": recipe_id, "sha256": recipe_sha, "origin": origin, "canonical": canonical},
            checks=checks_full, overrides=overrides, waivers=waivers, confirmed=confirmed,
            notes=notes if union_info is None else period_notes, ai=ai, signed_by=who, edits=edits)

    async def schema_cache_for(view: table_versions.SourceView, info: table_versions.PublishInfo) -> dict[str, Any]:
        from app.core import artifact_store

        cache = await table_versions.introspect_view(view, info.report)
        cache = p.apply_notes(cache, notes)
        if notes.problems:
            raise table_versions.PublishError("导入失败：说明没能写进表结构（" + "；".join(notes.problems[:3])
                                              + "），当前版本未受影响")
        table_versions.check_probe(cache, info.report, source_id)
        mid = await artifact_store.put_json(import_manifest(info), kind="import_manifest",
                                            meta={"source_id": source_id, "import_id": info.import_id})
        manifest_ids["id"] = mid
        cache["import_manifests"] = [mid]
        return cache

    async def union_cache_for(view: table_versions.SourceView, info: table_versions.SnapshotInfo) -> dict[str, Any]:
        """多期快照冻结的表结构：探查并集库、写说明，再写本期的导入清单和快照清单（2.9）。"""
        from app.core import artifact_store
        from app.data import source_versions as sv

        union_report = info.union_report or union_info.get("report") or {}
        probe = sv.union_table_report(union_report)
        cache = await table_versions.introspect_view(view, probe)
        cache = p.apply_notes(cache, notes)
        if notes.problems:
            raise table_versions.PublishError("导入失败：说明没能写进表结构（" + "；".join(notes.problems[:3])
                                              + "），当前版本未受影响")
        table_versions.check_probe(cache, probe, source_id)
        pinfo = table_versions.PublishInfo(
            import_id=info.import_id or import_id, snapshot_id=info.snapshot_id, seq=int(info.seq or 0),
            build_id=info.build_id or build_id, db_sha256=info.db_sha256 or "", build_reused=info.build_reused,
            report=info.report, build_restored=info.build_restored)
        mid = await artifact_store.put_json(import_manifest(pinfo), kind="import_manifest",
                                            meta={"source_id": source_id, "import_id": pinfo.import_id})
        manifest_ids["id"] = mid
        by_imp = {x.info.import_id: x for x in parts}
        ordered: list[Any] = []
        for iid in union_info.get("imports") or []:
            if iid is None:
                ordered.append({"import_id": pinfo.import_id, "seq": pinfo.seq,
                                "period": {"start": period.get("start"), "end": period.get("end")},
                                "build_id": pinfo.build_id, "db_sha256": pinfo.db_sha256, "recipe_id": recipe_id,
                                "recipe_sha256": recipe_sha, "manifest_artifact": mid, "file_name": file_name,
                                "raw_sha256": raw_sha})
            else:
                ordered.append(by_imp[iid])
        seq = recipe_seq_now
        if seq is None:
            last = (await session.execute(
                select(func.max(TableRecipe.seq)).where(TableRecipe.source_id == source_id))).scalar() or 0
            seq = last + 1
        doc = sv.manifest_doc(
            source_id=source_id, snapshot_id=info.snapshot_id, action=str((plan or {}).get("action") or "append"),
            recipe={"id": recipe_id, "seq": seq, "sha256": recipe_sha},
            union={"build_id": info.union_id, "db_sha256": info.union_db_sha256,
                   "table_hashes": dict(union_report.get("table_hashes") or {}), "union_ver": recipe_types.UNION_VER},
            parts=ordered, report=union_report, label_sets=list((plan or {}).get("label_sets") or []),
            gaps=list((plan or {}).get("gaps") or []), replaced=(plan or {}).get("replaces"), removed=None,
            notes=notes, signed_by=who)
        smid = await artifact_store.put_json(doc, kind="snapshot_manifest",
                                             meta={"source_id": source_id, "snapshot_id": info.snapshot_id})
        cache["import_manifests"] = [mid if iid is None else by_imp[iid].info.manifest_artifact
                                     for iid in union_info.get("imports") or []]
        cache["snapshot_manifest"] = smid
        return cache

    async def before_commit(imp_id: str | None, snap_id: str) -> None:
        # 锁内、同一事务：用 select 重读，不信会话里缓存的对象
        now = utcnow()
        cur = (await session.execute(
            select(DataSource.current_snapshot_id).where(DataSource.id == source_id))).first()
        if (cur[0] if cur is not None else None) != base:
            raise CommitConflict("base_changed", _BASE_CHANGED)
        # 暂存区本身也要在锁内重读：预检之后它可能已被放弃（放弃的请求先拿到了锁），已放弃的不能再发布（SE-8）
        await _require_open_locked(session, staging_id)
        if kind == "first" or source is None:
            taken = (await session.execute(
                select(DataSource.id).where(DataSource.name == name, DataSource.id != source_id))).first()
            if taken is not None:
                raise CommitConflict("name_taken", f"名称「{name}」已被占用，请放弃这次导入后换一个名称")
        if recipe_changed:
            last = (await session.execute(
                select(func.max(TableRecipe.seq)).where(TableRecipe.source_id == source_id))).scalar() or 0
            await session.execute(
                update(TableRecipe).where(TableRecipe.source_id == source_id, TableRecipe.status == "active")
                .values(status="superseded"))
            session.add(TableRecipe(
                id=recipe_id, source_id=source_id, seq=last + 1, recipe=canonical, recipe_sha256=recipe_sha,
                recipe_format=recipe_types.RECIPE_FORMAT, origin=origin, status="active", confirmations=confirmed,
                signed_by=who, ai_usage={"calls": ai["usage"], "consents": ai["consents"]}, staging_id=staging_id,
                created_at=now, activated_at=now))
            target.current_recipe_id = recipe_id
        imp = await session.get(TableImport, imp_id) if imp_id else None
        if imp is not None:
            imp.manifest_artifact = manifest_ids.get("id")
        staging.committed_import_id = imp_id
        table_versions.close_staging(staging, "committed", now=now)

    try:
        if union_info is not None:
            imports = [import_id if iid is None else str(iid) for iid in union_info.get("imports") or []]
            pub = await table_versions.publish_snapshot(
                session, target, imports=imports, mode="accumulate", recipe_id=recipe_id, recipe_sha256=recipe_sha,
                expected_current=base,
                period_build=table_versions.PeriodBuild(
                    tmp_db=path, build_id=build_id,
                    options=table_versions.recipe_build_options(recipe_sha, inputs, semantics=semantics),
                    report=report, expected_sha256=trial_sha or ""),
                new_import=table_versions.NewImport(import_id=import_id, raw_sha=raw_sha, file_name=file_name,
                                                    file_size=file_size, fields=import_fields),
                union=table_versions.UnionBuild(
                    tmp_db=union_path, union_id=str(union_info["union_id"]), options=dict(union_info.get("options") or {}),
                    report=dict(union_info.get("report") or {}), expected_sha256=str(union_info.get("db_sha256") or "")),
                schema_cache_for=union_cache_for, before_commit=before_commit, activation=activation)
            return CommitResult(import_id=pub.import_id or import_id, snapshot_id=pub.snapshot_id,
                                build_id=pub.build_id or build_id, recipe_id=recipe_id, build_reused=pub.build_reused,
                                unchanged=False, source_id=source_id, build_restored=pub.build_restored,
                                parts=len(imports), snapshot_reused=bool(pub.snapshot_reused or pub.snapshot_revived))
        result = await table_versions.publish_build(
            session, target, tmp_db=path, build_id=build_id,
            build_options=table_versions.recipe_build_options(recipe_sha, inputs, semantics=semantics),
            engine_ver=recipe_types.RECIPE_ENGINE_VER, report=report, raw=None, raw_sha=raw_sha,
            file_name=file_name, file_size=file_size, schema_cache_for=schema_cache_for,
            import_fields=import_fields, expected_db_sha256=trial_sha, before_commit=before_commit,
            import_id=import_id,
            snapshot_fields={"mode": parsed.mode, "recipe_id": recipe_id, "recipe_sha256": recipe_sha},
            activation=activation)
    except ImportRefused:
        await _back_to_drafting(session, staging_id)
        raise
    except table_versions.PublishError as e:
        await _back_to_drafting(session, staging_id)
        raise _publish_refused(e) from e
    except IntegrityError as e:
        await _back_to_drafting(session, staging_id)
        if "data_sources.name" in str(e.orig if e.orig is not None else e):
            raise ImportRefused(409, "name_taken", f"名称「{name}」已被占用，请放弃这次导入后换一个名称") from e
        raise ImportRefused(500, "publish_failed", "导入失败：写入版本记录时出错，当前版本未受影响") from e
    except BaseException:
        await _back_to_drafting(session, staging_id)
        raise
    finally:
        if union_path is not None:
            # publish_snapshot 无论成败都消耗并集的试运行文件；走 publish_build 的路径用不上它，也一并删掉
            await asyncio.to_thread(_drop_file, union_path)
    return CommitResult(import_id=result.import_id, snapshot_id=result.snapshot_id, build_id=result.build_id,
                        recipe_id=recipe_id, build_reused=result.build_reused, unchanged=False, source_id=source_id,
                        build_restored=result.build_restored, parts=1)


def _drop_file(path: Path) -> None:
    if path.parent.name != table_versions.TRIALS_DIR:
        return
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


async def _union_notes_for_commit(p: Pipeline, parsed: Recipe, ex: Extraction, checks: list[CheckResult],
                                  acc: list[Acceptance], trial: dict[str, Any], union_info: dict[str, Any],
                                  plan: dict[str, Any], parts: list[Any]) -> SchemaNotes:
    """提交时冻结的并集说明：与试运行预览同一套合并规则，只是本期带上实际的接受（2.8）。"""
    from app.data import source_versions as sv

    report = union_info.get("report") or {}
    by_imp = {x.info.import_id: x for x in parts}
    period = (trial.get("extraction") or {}).get("period") or {}
    start, end = str(period.get("start") or ""), str(period.get("end") or "")
    run = _TrialRun(parsed=parsed, problems=[], extraction=ex, checks=checks, null_counts=trial.get("null_counts"))
    note_parts = []
    for iid in union_info.get("imports") or []:
        if iid is None:
            note_parts.append(_union_note_new(parsed, ex, run, start, end, acc))
        else:
            x = by_imp.get(iid)
            if x is None:
                raise ImportRefused(409, "base_changed", _BASE_CHANGED)
            note_parts.append(await _in_slot(sv.note_part, x))
    tables = {str(k): [ColumnOut(**_known(ColumnOut, c)) for c in v or [] if isinstance(c, dict)]
              for k, v in (report.get("tables") or {}).items()}
    return p.build_union_notes(
        parsed, note_parts, union_tables=tables, null_counts=report.get("null_counts"),
        part_null_counts=report.get("part_null_counts"), added=list(report.get("added") or []),
        retired=list(report.get("retired") or []), label_sets=list(plan.get("label_sets") or []),
        gaps=bool(plan.get("gaps")), dropped=False)


# ==========================================================================
# 放弃
# ==========================================================================


async def _require_open_locked(session: AsyncSession, staging_id: str) -> None:
    """锁内按库里的状态判断暂存区是否还开着（select 重读，不信会话里缓存的对象）。"""
    status = (await session.execute(
        select(ImportStaging.status).where(ImportStaging.id == staging_id))).scalar_one_or_none()
    if status not in table_versions.STAGING_OPEN:
        raise CommitConflict("staging_closed", _CLOSED)


async def discard_staging(session: AsyncSession, staging: ImportStaging) -> None:
    """DELETE imports/{id}：status → discarded、瘦身、删试运行库；原件已没有别的引用时当场删除。

    进锁之后重读状态：排在提交后面等锁的放弃，拿到锁时暂存区可能已经提交了，不能把 committed 改写成
    discarded（SE-8）。只读进程不放弃：放弃要删试运行库和原件（第 8 节遗留项 5）。"""
    require_open(staging)
    table_versions.require_store_writable()
    async with table_versions.store_lock():
        await session.refresh(staging, ["status"])
        require_open(staging)
        old = table_versions.close_staging(staging, "discarded")
        await session.commit()
    await _remove_trial(old)
    await table_versions.release_staging_raw(session, staging)


# ==========================================================================
# 响应形状（7.4 StagingOut）
# ==========================================================================


def _span_count(spans: Any) -> int:
    total = 0
    for s in spans or []:
        try:
            total += int(s[1]) - int(s[0]) + 1
        except (TypeError, ValueError, IndexError):
            continue
    return total


def _bounds_a1(b: Any) -> str | None:
    if not isinstance(b, dict):
        return None
    from app.data.xlsx_scan import cell_ref

    try:
        return f"{cell_ref(int(b['min_row']), int(b['min_col']))}:{cell_ref(int(b['max_row']), int(b['max_col']))}"
    except (KeyError, TypeError, ValueError):
        return None


def import_mode(source: DataSource | None) -> str | None:
    if source is None:
        return None
    if source.current_recipe_id:
        return "recipe"
    return "simple" if (source.origin or "manual") == "upload" else None


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def staging_out(staging: ImportStaging, source: DataSource | None, *,
                ai: AiAvailability | None = None, units: tuple[str, ...] | None = None) -> dict[str, Any]:
    """7.4 的 StagingOut。ai 是模型接入的可用性（只在提供 AI 起草时由接口层查好传进来）。结束（瘦身）后的
    暂存区照样能给出：大字段都是空的。"""
    scan = staging.scan or {}
    sheets, skipped = [], []
    for s in scan.get("sheets") or []:
        if not isinstance(s, dict):
            continue
        if s.get("state") != "visible":
            skipped.append({"sheet": s.get("name"), "state": s.get("state")})
            continue
        sheets.append({
            "name": s.get("name"), "state": s.get("state"), "bounds": _bounds_a1(s.get("bounds")),
            "nonempty": int(s.get("nonempty") or 0),
            "merged": int(s.get("merged_total") or len(s.get("merged") or [])),
            "formulas": int(s.get("formulas") or 0),
            "formulas_uncached": int((s.get("formulas_uncached") or {}).get("total") or 0),
            "hidden_rows": _span_count(s.get("hidden_rows")), "hidden_cols": _span_count(s.get("hidden_cols")),
        })
    drafts = staging.drafts or {}
    trial = staging.trial
    if isinstance(trial, dict):
        trial = {k: copy.deepcopy(v) for k, v in trial.items() if k not in TRIAL_INTERNAL}
    ai_sum = drafts.get("ai")
    if isinstance(ai_sum, dict):
        ai_sum = {**ai_sum, "draft": _draft_out(ai_sum.get("draft"))}
    offered = table_versions.staging_is_open(staging) and ai_offered(staging)
    recipe = _full_form(staging.recipe) if isinstance(staging.recipe, dict) else None
    facts = staging.facts or {}
    marks = drafts.get("marks") or []
    if not marks and isinstance(staging.trial, dict):
        marks = _regions(staging.trial.get("extraction"))
    if units is None:
        units = tuple(recipe_parsers.UNITS)
    fixes, fix_where = proposals_for(pipeline(), staging)
    trial, draft_problems, recipe_problems = _with_fix_ids(fixes, fix_where, trial, staging)
    edits = [e for e in staging.edits or [] if isinstance(e, dict)]
    open_ = table_versions.staging_is_open(staging)
    return {
        "id": staging.id, "kind": staging.kind, "status": staging.status,
        "source": {"id": staging.source_id, "name": source.name if source is not None else staging.source_name,
                   "exists": source is not None, "import_mode": import_mode(source)},
        "file": {"name": staging.file_name, "size": int(staging.file_size or 0),
                 "sha256_prefix": (staging.raw_sha256 or "")[:8]},
        "sheets": sheets, "skipped_sheets": skipped, "full_calc_on_load": bool(scan.get("full_calc_on_load")),
        "grids": list(staging.grid_preview or []),
        "draft": _draft_out(drafts.get("rules")),
        "ai_draft": ai_sum,
        "ai": {"offered": offered, "available": bool(ai.available) if ai is not None else False,
               "reason": ai.reason if ai is not None else "", "model": ai.model if ai is not None else "",
               "provider": ai.provider if ai is not None else ""},
        "ai_consents": list(staging.ai_consents or []),
        "recipe": recipe, "recipe_origin": staging.recipe_origin,
        "recipe_problems": recipe_problems,
        "answers": dict(staging.answers or {}),
        "cards": list(staging.cards or []), "questions": list(staging.questions or []),
        "draft_problems": draft_problems, "draft_partial": bool(staging.draft_partial),
        "candidates": dict(facts.get("candidates") or {}),
        "units": list(units),
        "marks": marks,
        "trial": trial,
        "context_inputs": dict(staging.context_inputs or {}),
        "base_snapshot_id": staging.base_snapshot_id,
        "committed_import_id": staging.committed_import_id,
        "created_at": _iso(staging.created_at), "updated_at": _iso(staging.updated_at),
        "expires_at": _iso(staging.expires_at),
        # 期 3（9.4 StagingOut）
        "fixes": [_jsonable(f) for f in fixes],
        "recipe_sha256": staging.recipe_sha256,
        "edits": [_edit_out(e, last=i == len(edits) - 1, open_=open_) for i, e in enumerate(edits)],
        "answers_dropped": list(staging.answers_dropped or []),
    }


def _with_fix_ids(fixes: list[Any], where: str, trial: Any, staging: ImportStaging
                  ) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """按提议的 anchor 把 fix_ids 写到对应的问题上（3.1，评审丙-B2）：界面只按 fix_ids 放按钮，不靠 code 加坐标去猜
    （title_not_found 没有坐标，两个分段同时找不到标题时只有这样才分得清）。没有提议的问题 fix_ids 为 []。
    返回 (trial, draft_problems, recipe_problems)，都是新的副本。"""
    by_problem: dict[int, list[str]] = {}
    by_recipe: dict[int, list[str]] = {}
    for f in fixes:
        anchor = getattr(f, "anchor", None)
        index = getattr(anchor, "index", None)
        if index is None:
            continue
        if getattr(anchor, "kind", None) == "problem":
            by_problem.setdefault(int(index), []).append(f.id)
        elif getattr(anchor, "kind", None) == "recipe_problem":
            by_recipe.setdefault(int(index), []).append(f.id)

    def tag(items: Any, ids: dict[int, list[str]]) -> list[dict[str, Any]]:
        return [{**x, "fix_ids": list(ids.get(i, []))} for i, x in enumerate(items or []) if isinstance(x, dict)]

    draft_problems = tag(staging.draft_problems, by_problem if where == "draft" else {})
    recipe_problems = tag(staging.recipe_problems, by_recipe)
    if isinstance(trial, dict) and isinstance(trial.get("problems"), list):
        trial = {**trial, "problems": tag(trial["problems"], by_problem if where == "trial" else {})}
    return trial, draft_problems, recipe_problems


__all__ = [
    "ACCEPT_REASON_MAX", "ANSWER_REASON_MAX", "CommitConflict", "CommitResult", "EditRequest", "ImportRefused",
    "MANIFEST_FORMAT", "Pipeline", "acceptable_ids", "ai_offered", "ai_preview", "answer", "carry_answers",
    "check_from_dict", "commit_staging", "consent_token", "edit_apply", "edit_preview", "edit_undo", "proposals_for",
    "redraft_rules",
    "current_import", "current_recipe", "default_pipeline", "discard_staging", "effective_origin",
    "extraction_from_dict", "import_mode", "pipeline", "put_recipe", "redraft", "replay_answers", "reupload",
    "run_ai_draft", "run_trial", "stage_upload", "staging_out", "stored_origin", "trial_key", "trial_status",
]
