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
    SheetsOut,
    TableOut,
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
#: trial 里只给服务端自己用的键：StagingOut 不带它们（整份 Extraction 很大，界面也不需要）
TRIAL_INTERNAL = ("extraction", "null_counts", "inputs", "recipe_sha256", "build_id")
#: TrialOut.receipt 给界面看的回执字段（7.4）。完整的回执在 trial["extraction"] 和导入清单里
_RECEIPT_VIEW = (
    "ledger", "tables", "period", "axes", "placeholders", "labels", "outside_text", "hidden", "sheets",
    "derived_form", "block_order", "ignored_columns", "full_calc_on_load", "formula_cells_accepted",
    "blank_rows_skipped", "table_hashes", "timings",
)
#: 暂存区的 recipe_origin → 静态校验的 origin。manual / mixed 一律按 manual 校验
_VALIDATE_ORIGIN = {"rules": "rules", "ai": "ai"}
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


def default_pipeline() -> Pipeline:
    """接上真模块。函数体里惰性导入：这些模块（openpyxl、执行器、起草器）导入不便宜，接口模块加载时不必付。"""
    from app.data import (
        recipe, recipe_ai, recipe_checks, recipe_confirm, recipe_diff, recipe_engine, recipe_notes,
        recipe_suggest, xlsx_cells, xlsx_scan,
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


def _snap(staging: ImportStaging, *, base_sha: str | None = None, recipe_origin: str | None = None) -> _Snap:
    return _Snap(id=staging.id, kind=staging.kind, raw_sha256=staging.raw_sha256,
                 scan=copy.deepcopy(staging.scan), file_name=staging.file_name or "",
                 recipe_origin=recipe_origin if recipe_origin is not None else staging.recipe_origin,
                 base_sha=base_sha, context=copy.deepcopy(staging.context_inputs or {}))


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
    """静态校验。上传新一期、工作配方仍是现行配方时按重放校验（不查认领）；其余带系统发现查认领。"""
    if not isinstance(work, dict):
        return None, []
    if snap.kind == "reupload" and snap.base_sha:
        parsed, problems = p.validate(work, facts=None, origin="replay")
        if parsed is not None and p.recipe_sha256(parsed) == snap.base_sha:
            return parsed, problems
    return p.validate(work, facts=facts, origin=_VALIDATE_ORIGIN.get(snap.recipe_origin or "", "manual"))


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
    row = await _require_recipe_source(session, source)
    full = _full_form(row.recipe)
    staging = _new_staging(source_id=source.id, name=source.name, description=source.description or "",
                           kind="reupload", filename=filename, raw=raw, signed_by=signed_by, base_recipe_id=row.id)
    staging.recipe_origin = row.origin or "manual"
    snap = _snap(staging, base_sha=row.recipe_sha256)
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
    staging.recipe_origin = row.origin or "manual"
    snap = _snap(staging)
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


async def _base_sha(session: AsyncSession, staging: ImportStaging) -> str | None:
    if staging.kind != "reupload" or not staging.base_recipe_id:
        return None
    row = await session.get(TableRecipe, staging.base_recipe_id)
    return row.recipe_sha256 if row is not None else None


def _reeval_work(p: Pipeline, snap: _Snap, work: Any) -> _Eval:
    return _evaluate(p, _book_of(p, snap), snap, work)


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
    try:
        work = replay_answers(p, staging.answers_base, questions, clean)
    except ValueError as e:
        raise ImportRefused(422, "answer_invalid", f"回答没能应用到配方上：{e}") from e
    snap = _snap(staging, base_sha=await _base_sha(session, staging))
    ev = await _in_slot(_reeval_work, p, snap, work)
    await _still_open(session, staging)
    old = _apply_eval(staging, ev, questions=False)
    staging.answers = clean
    await session.commit()
    await _remove_trial(old)
    return staging


async def put_recipe(session: AsyncSession, staging: ImportStaging, recipe: dict[str, Any], *,
                     origin: str = "manual") -> ImportStaging:
    """PUT imports/{id}/recipe：整份配方作为新的起点（answers_base 换成它、回答清空），照存；静态校验的问题
    写进 recipe_problems（有问题也存，只是不能试运行）。"""
    p = pipeline()
    require_open(staging)
    base = _full_form(recipe)
    snap = _snap(staging, base_sha=await _base_sha(session, staging), recipe_origin=origin)
    ev = await _in_slot(_reeval_work, p, snap, base)
    await _still_open(session, staging)
    staging.recipe_origin = origin
    staging.answers_base = copy.deepcopy(base)
    staging.answers = {}
    old = _apply_eval(staging, ev, questions=True)
    await session.commit()
    await _remove_trial(old)
    return staging


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


def _confirm_context(staging: ImportStaging, cx: _Context, recipe: Recipe, ex_dict: dict[str, Any],
                     checks: list[Any], diff: list[Any] | None) -> ConfirmContext:
    from app.data.engine import masked_columns

    old = cx.recipe.recipe if cx.recipe is not None and staging.kind in ("reupload", "redraft") else None
    return ConfirmContext(
        kind=staging.kind, recipe=recipe, old_recipe=old,
        old_schema_cache=(cx.snapshot.schema_cache or {}) if staging.kind == "switch" and cx.snapshot else None,
        extraction=ex_dict, checks=checks, diff=(diff or []) if cx.prev is not None else None, prev=cx.prev,
        recipe_origin=effective_origin(staging, cx.recipe.origin if cx.recipe is not None else None),
        facts=_facts_from(staging.facts),
        mask_columns=masked_columns(cx.source.options) if cx.source is not None else None,
    )


async def run_trial(session: AsyncSession, staging: ImportStaging, *,
                    context_inputs: dict[str, PeriodInput] | None, signed_by: str | None) -> ImportStaging:
    """POST imports/{id}/trial：执行 → 核对 → 说明预览 → 确认项 →（源有当前版本时）差异卡。拒收也照常返回，
    看 trial.status。试运行库写好后删掉同一暂存区旧的试运行库。"""
    p = pipeline()
    require_open(staging)
    if not isinstance(staging.recipe, dict):
        raise ImportRefused(409, "recipe_invalid", "还没有可用的配方，请先在配方面板中填写")
    ctx = copy.deepcopy(staging.context_inputs or {})
    if context_inputs:
        if not _context_allowed(staging):
            raise ImportRefused(422, "context_not_needed", "这份表格里能解析出统计期，不需要人工录入")
        ctx = {key: {"start": v.start.isoformat(), "end": v.end.isoformat(),
                     "signed_by": (v.signed_by or signed_by or None)} for key, v in context_inputs.items()}
    cx = await _context(session, staging)
    base_sha = cx.recipe.recipe_sha256 if cx.recipe is not None and staging.kind == "reupload" else None
    snap = _snap(staging, base_sha=base_sha)
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
    # 指着，当场删掉。路径与旧的相同（同一个键重跑）时不删，那是暂存区记着的库
    try:
        await _still_open(session, staging)
        if run.extraction is None:
            raise ImportRefused(409, "recipe_invalid", "配方没有通过检查，请先按配方面板里的提示修改")
        ex = run.extraction
        notes_problems = list(run.notes.problems) if run.notes is not None else []
        problems_out = [_jsonable(x) for x in ex.problems]
        problems_out += [{"code": "notes_invalid", "category": "recipe", "message": f"说明没有通过检查：{m}",
                          "cells": [], "model_message": "", "fix": None} for m in notes_problems]
        checks_json = [_jsonable(c) for c in run.checks]
        status = trial_status(ex.problems, run.checks, notes_problems)
        full = _receipt_full(ex)
        ex_dict = _jsonable(ex)
        diff: list[Any] | None = None
        items: list[ConfirmItem] = []
        # 差异卡和确认项只在能提交的试运行（passed / needs_decision）上算。拒收时执行器在结构问题上停下，
        # 表、格子账可能只写了一半，拿它比会冒出「行数 31 → 0」这类假变化，结构上的变化已经作为拒收问题
        # 报了（7.6）；needs_input 缺统计期，录入后再试运行就有。这两种状态 diff 为 null
        if status in _ACTIVE_TRIAL:
            if cx.prev is not None:
                prev_name = cx.imp.file_name if cx.imp is not None else None
                cur = {"receipt": full, "checks": checks_json, "period": full.get("period"),
                       "recipe_sha256": recipe_sha}
                changed = cx.recipe is None or cx.recipe.recipe_sha256 != recipe_sha
                diff = list(p.diff_reports(cx.prev, cur, prev_file_name=prev_name,
                                           cur_file_name=staging.file_name or "", recipe=parsed,
                                           recipe_changed=changed))
            items = list(p.confirm_items(_confirm_context(staging, cx, parsed, ex_dict, run.checks, diff)))
        inputs = _build_inputs(ex)
        build_id = table_versions.recipe_build_id(staging.source_id, staging.raw_sha256, recipe_sha, inputs)
        same = None
        if cx.imp is not None and cx.imp.build_id == build_id:
            same = {"id": cx.imp.id, "seq": cx.imp.seq}
        notes_view = ({t: {"comment": n.comment, "columns": dict(n.columns)} for t, n in run.notes.tables.items()}
                      if run.notes is not None else {})
        base_snapshot = cx.source.current_snapshot_id if cx.source is not None else None
        staging.trial = {
            "trial_id": key[:16], "status": status, "receipt": _receipt_view(full, run.db_sha256),
            "problems": problems_out, "checks": checks_json, "confirm_items": [_jsonable(i) for i in items],
            "acceptable": acceptable_ids(run.checks), "notes": notes_view,
            "diff": [_jsonable(d) for d in diff] if diff is not None else None,
            "same_as_import": same, "base_snapshot_id": base_snapshot,
            # 以下只给服务端：提交时还原 Extraction、重算必勾项、生成说明
            "extraction": ex_dict, "null_counts": run.null_counts, "inputs": inputs, "recipe_sha256": recipe_sha,
            "build_id": build_id,
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
              notes: SchemaNotes, ai: dict[str, Any], signed_by: str | None) -> dict[str, Any]:
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
        "receipt": receipt,
        "notes": {"templates": dict(notes.templates),
                  "rendered": {t: {"comment": n.comment, "columns": dict(n.columns)} for t, n in notes.tables.items()}},
        "ai": ai,
        "signed_by": {"name": signed_by, "verified": False},
        "staging_id": staging_id, "kind": kind,
        "created_at": _now_iso(),
    }


async def commit_staging(session: AsyncSession, staging: ImportStaging, *, trial_id: str, confirmations: list[str],
                         acceptances: list[Acceptance], signed_by: str | None) -> CommitResult:
    """POST imports/{id}/commit（7.8）：预检 → （与现行导入相同时）不新建记录 → 发布构建、写配方、导入记录、
    快照、清单，切指针。失败时指针不动，暂存区退回 drafting（试运行库已被发布流程消耗）。"""
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

    cx = await _context(session, staging)
    source = cx.source
    base = staging.base_snapshot_id
    # 先看当前版本有没有被换掉：换了的话上一期回执也换了，按它重算的确认项没有意义（这里只是快速预检，
    # 最终判定在 publish_build 的锁内）
    if source is not None and source.current_snapshot_id != base:
        raise ImportRefused(409, "base_changed", _BASE_CHANGED)
    if source is None:
        taken = (await session.execute(
            select(DataSource.id).where(DataSource.name == staging.source_name))).first()
        if taken is not None:
            raise ImportRefused(409, "name_taken", f"名称「{staging.source_name}」已被占用，请放弃这次导入后换一个名称")
    ex_dict = trial.get("extraction") or {}
    checks = [check_from_dict(c) for c in trial.get("checks") or []]
    items = list(p.confirm_items(_confirm_context(staging, cx, parsed, ex_dict, checks, trial.get("diff"))))
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

    human = bool(_build_inputs(ex_dict))
    same = trial.get("same_as_import") or None
    if (same and not given and not human and cx.imp is not None and cx.imp.id == same.get("id")
            and cx.imp.file_name == staging.file_name and source is not None):
        # 与现行导入完全相同：不新建任何记录（final.md 2.6 第 5 条）。结束暂存区也在锁内、重读指针之后：
        # 预检之后别的提交切走了指针的话，这里返回的「现行导入」已经不是现行的了
        imp_id, imp_build, source_id = cx.imp.id, cx.imp.build_id, source.id
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
                            recipe_id=cur[1] or "", build_reused=True, unchanged=True, source_id=source_id)

    ex = extraction_from_dict(ex_dict)
    who = signed_by or staging.signed_by
    acc = [Acceptance(check_id=a.check_id, reason=a.reason.strip(), signed_by=who) for a in given.values()]
    notes = p.build_notes(parsed, ex, checks, acc, null_counts=trial.get("null_counts"))
    if notes.problems:
        raise ImportRefused(422, "notes_invalid", "说明没有通过检查：" + "；".join(notes.problems[:5]))

    # ---- 以下到 publish_build 之前，不改会话里的任何对象（7.2 第 1 条最后一项）
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
    origin = effective_origin(staging, base_origin)
    recipe_changed = cx.recipe is None or cx.recipe.recipe_sha256 != recipe_sha
    recipe_id = new_id() if recipe_changed else cx.recipe.id
    canonical = p.canonical(parsed)
    build_id = table_versions.recipe_build_id(source_id, raw_sha, recipe_sha, inputs)
    import_id = new_id()
    ai = {"usage": copy.deepcopy(staging.ai_usage or []), "consents": copy.deepcopy(staging.ai_consents or [])}
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

    async def schema_cache_for(view: table_versions.SourceView, info: table_versions.PublishInfo) -> dict[str, Any]:
        from app.core import artifact_store

        cache = await table_versions.introspect_view(view, info.report)
        cache = p.apply_notes(cache, notes)
        if notes.problems:
            raise table_versions.PublishError("导入失败：说明没能写进表结构（" + "；".join(notes.problems[:3])
                                              + "），当前版本未受影响")
        table_versions.check_probe(cache, info.report, source_id)
        manifest = _manifest(
            staging_id=staging_id, kind=kind, source_id=source_id, info=info, trial_sha=trial_sha, ex_dict=ex_dict,
            file={"name": file_name, "size": file_size, "raw_sha256": raw_sha},
            recipe_meta={"id": recipe_id, "sha256": recipe_sha, "origin": origin, "canonical": canonical},
            checks=checks_full, overrides=overrides, waivers=waivers, confirmed=confirmed, notes=notes, ai=ai,
            signed_by=who)
        mid = await artifact_store.put_json(manifest, kind="import_manifest",
                                            meta={"source_id": source_id, "import_id": info.import_id})
        manifest_ids["id"] = mid
        cache["import_manifests"] = [mid]
        return cache

    async def before_commit(imp_id: str, snap_id: str) -> None:
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
        imp = await session.get(TableImport, imp_id)
        if imp is not None:
            imp.manifest_artifact = manifest_ids.get("id")
        staging.committed_import_id = imp_id
        table_versions.close_staging(staging, "committed", now=now)

    try:
        result = await table_versions.publish_build(
            session, target, tmp_db=path, build_id=build_id,
            build_options=table_versions.recipe_build_options(recipe_sha, inputs),
            engine_ver=recipe_types.RECIPE_ENGINE_VER, report=report, raw=None, raw_sha=raw_sha,
            file_name=file_name, file_size=file_size, schema_cache_for=schema_cache_for,
            import_fields=import_fields, expected_db_sha256=trial_sha, before_commit=before_commit,
            import_id=import_id)
    except ImportRefused:
        await _back_to_drafting(session, staging_id)
        raise
    except table_versions.PublishError as e:
        await _back_to_drafting(session, staging_id)
        if e.code == "trial_missing":
            raise ImportRefused(409, "trial_required", str(e)) from e
        if e.code == "trial_tampered":
            raise ImportRefused(409, "trial_tampered", str(e)) from e
        raise ImportRefused(500, "publish_failed", str(e)) from e
    except IntegrityError as e:
        await _back_to_drafting(session, staging_id)
        if "data_sources.name" in str(e.orig if e.orig is not None else e):
            raise ImportRefused(409, "name_taken", f"名称「{name}」已被占用，请放弃这次导入后换一个名称") from e
        raise ImportRefused(500, "publish_failed", "导入失败：写入版本记录时出错，当前版本未受影响") from e
    except BaseException:
        await _back_to_drafting(session, staging_id)
        raise
    return CommitResult(import_id=result.import_id, snapshot_id=result.snapshot_id, build_id=result.build_id,
                        recipe_id=recipe_id, build_reused=result.build_reused, unchanged=False, source_id=source_id)


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
    discarded（SE-8）。"""
    require_open(staging)
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
        "recipe_problems": list(staging.recipe_problems or []),
        "answers": dict(staging.answers or {}),
        "cards": list(staging.cards or []), "questions": list(staging.questions or []),
        "draft_problems": list(staging.draft_problems or []), "draft_partial": bool(staging.draft_partial),
        "candidates": dict(facts.get("candidates") or {}),
        "units": list(units),
        "marks": marks,
        "trial": trial,
        "context_inputs": dict(staging.context_inputs or {}),
        "base_snapshot_id": staging.base_snapshot_id,
        "committed_import_id": staging.committed_import_id,
        "created_at": _iso(staging.created_at), "updated_at": _iso(staging.updated_at),
        "expires_at": _iso(staging.expires_at),
    }


__all__ = [
    "ACCEPT_REASON_MAX", "ANSWER_REASON_MAX", "CommitConflict", "CommitResult", "ImportRefused", "MANIFEST_FORMAT",
    "Pipeline", "acceptable_ids", "ai_offered", "ai_preview", "answer", "check_from_dict", "commit_staging",
    "consent_token",
    "current_import", "current_recipe", "default_pipeline", "discard_staging", "effective_origin",
    "extraction_from_dict", "import_mode", "pipeline", "put_recipe", "redraft", "replay_answers", "reupload",
    "run_ai_draft", "run_trial", "stage_upload", "staging_out", "trial_key", "trial_status",
]
