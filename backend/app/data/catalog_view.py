"""目录视图：运行和助手读数据目录统一经这一个接缝（docs/design/next-round/01-效果度量与回归评测.md 做法 5，总方案 X7）。

**为什么要接缝。** 目录在四处被读：运行时的 db_schema / db_query 工具（目录同时冻结进表结构快照）、助手查看（look 表详情）、
自查用的 SQL 检查器、双盲复算（交给盲算的说明、比较两条 SQL 用的中文名）。以前各读各的、没有统一的开关，「开目录和关
目录」「只用已核实的项」「起草模型 A 的目录和 B 的」这些对照都做不了，评测期间目录被起草或剖析改了也不知道。现在四处都
经这里取，一处定读哪一份；lint 测试（tests/test_catalog_view.py）不许这几个模块绕过它直接 read_catalog、直接查 CatalogNote。

**视图**（CatalogView）：
- mode：live 当前目录（缺省，行为和以前一样）；snapshot 一份冻结的目录快照（工件 id 在 snapshot，内容见 SNAPSHOT_KIND）；
  off 不给目录。
- statuses：all 全部（驳回的照旧不给模型看）；reviewed 只要已核实（verified）和已确认（confirmed）的项，推断的
  （proposed）去掉——量的是模型起草、还没审过的那些项是帮忙还是添乱。

**视图从哪来。** 运行从 Run.options['catalog_view'] 读（launch_run 收 options，发起时定下、随 run.started 封存，审批恢复、
重启续跑都沿用——用上下文变量传的话恢复之后就丢了）；生成过程（问数据的 answer_ops、评测的作答）从参数读。都不给就是 live。
冻结进表结构快照时，不是缺省视图的在工件引用的 meta 里记下视图（describe），证据面板据此写得出「本次运行目录：关」。

**快照的内容**（第 2 波的评测题集包写，这里只定读法）：kind=eval_catalog_snapshot 的内容寻址工件，
{"sources": {源 id: {"tables": {表名: {"version": 版本, "notes": 目录}}}}}。快照里没有这个源就是这个源没有目录；
快照工件不在了照实报错（CatalogSnapshotMissing），不悄悄退回当前目录——那样对照就白做了。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.data import catalog
from app.data.catalog import CatalogEntry

logger = logging.getLogger(__name__)

#: Run.options 里放视图的键
OPTION_KEY = "catalog_view"
MODES = ("live", "snapshot", "off")
#: 各状态集合留下哪些项的状态（all 不筛：驳回的由各处照旧经 visible_notes 去掉）
STATUS_SETS: dict[str, tuple[str, ...] | None] = {"all": None, "reviewed": ("verified", "confirmed")}
#: 目录快照工件的 kind（细案 01 做法 6）
SNAPSHOT_KIND = "eval_catalog_snapshot"


class CatalogViewInvalid(ValueError):
    """视图写得不对（服务端代码写错了才会有：客户端写不了 Run.options）。"""


class CatalogSnapshotMissing(RuntimeError):
    """视图指定的目录快照工件不在了、或者内容认不出。"""


@dataclass(frozen=True)
class CatalogView:
    mode: str = "live"
    statuses: str = "all"
    #: mode=snapshot 时快照工件的 id
    snapshot: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise CatalogViewInvalid(f"目录视图的 mode 只能是 {'、'.join(MODES)}，当前为「{self.mode}」")
        if self.statuses not in STATUS_SETS:
            raise CatalogViewInvalid(f"目录视图的 statuses 只能是 {'、'.join(STATUS_SETS)}，当前为「{self.statuses}」")
        if (self.mode == "snapshot") != bool(self.snapshot):
            raise CatalogViewInvalid("目录视图只有 mode=snapshot 时带快照工件 id，而且必须带")

    @property
    def is_default(self) -> bool:
        return self.mode == "live" and self.statuses == "all"

    def as_option(self) -> dict[str, Any]:
        """写进 Run.options['catalog_view'] 的形状。"""
        return {"mode": self.mode, "statuses": self.statuses, **({"snapshot": self.snapshot} if self.snapshot else {})}

    @classmethod
    def parse(cls, raw: Any) -> CatalogView:
        """Run.options['catalog_view']、生成参数 → 视图。None 是缺省（live）；写得不对抛 CatalogViewInvalid。"""
        if raw is None:
            return LIVE
        if isinstance(raw, CatalogView):
            return raw
        if not isinstance(raw, Mapping):
            raise CatalogViewInvalid(f"目录视图需要是一个对象，当前为「{raw}」")
        unknown = set(raw) - {"mode", "statuses", "snapshot"}
        if unknown:
            raise CatalogViewInvalid(f"目录视图里不认识的键：{'、'.join(sorted(unknown))}")
        return cls(mode=str(raw.get("mode") or "live"), statuses=str(raw.get("statuses") or "all"),
                   snapshot=str(raw["snapshot"]) if raw.get("snapshot") else None)


LIVE = CatalogView()


def of_options(options: Mapping[str, Any] | None) -> CatalogView:
    """一次运行的视图（Run.options 里的 catalog_view）。没有就是 live。"""
    return CatalogView.parse((options or {}).get(OPTION_KEY))


def of_run(run_ctx: Any) -> CatalogView:
    """运行里的节点构造 ToolContext 时用：RunContext.extra['run_options'] 里的视图（runner 每段执行都放这份）。"""
    from app.engine.runner import RUN_OPTIONS_KEY

    return of_options((getattr(run_ctx, "extra", None) or {}).get(RUN_OPTIONS_KEY))


def describe(view: CatalogView | None) -> dict[str, Any] | None:
    """记进冻结快照 meta 的说法：缺省视图（live、全部）不记，别的照 as_option 记下。"""
    view = view or LIVE
    return None if view.is_default else view.as_option()


def restrict(view: CatalogView | None, notes: Mapping[str, Any] | None) -> dict[str, Any]:
    """一张表的目录在这个视图下留下什么（状态集合；驳回的项原样留着，见 catalog.restrict_notes）。"""
    allowed = STATUS_SETS[(view or LIVE).statuses]
    if allowed is None:
        return dict(notes or {})
    return catalog.restrict_notes(notes, allowed)


def _restricted(view: CatalogView, entries: Mapping[str, CatalogEntry]) -> dict[str, CatalogEntry]:
    if STATUS_SETS[view.statuses] is None:
        return dict(entries)
    out: dict[str, CatalogEntry] = {}
    for table, entry in entries.items():
        if notes := restrict(view, entry.notes):
            out[table] = CatalogEntry(table=entry.table, notes=notes, version=entry.version,
                                      updated_by=entry.updated_by, updated_at=entry.updated_at)
    return out


def _snapshot_tables(view: CatalogView, source_id: str) -> dict[str, CatalogEntry]:
    from app.core.artifact_store import load

    try:
        content = load(view.snapshot or "")
    except ValueError as e:     # 内容和哈希对不上
        raise CatalogSnapshotMissing(f"目录快照 {str(view.snapshot)[:12]}… 读不出：{e}") from e
    if not isinstance(content, dict) or not isinstance(content.get("sources"), dict):
        raise CatalogSnapshotMissing(f"目录快照 {str(view.snapshot)[:12]}… 不在了或内容认不出，这次不能按它读目录")
    tables = ((content["sources"].get(source_id) or {}).get("tables")) or {}
    out: dict[str, CatalogEntry] = {}
    for table, body in tables.items():
        if isinstance(body, Mapping) and isinstance(body.get("notes"), Mapping):
            out[str(table)] = CatalogEntry(table=str(table), notes=dict(body["notes"]),
                                           version=int(body.get("version") or 0))
    return out


async def read_many(session: Any, view: CatalogView | None, source_ids: Iterable[str]) -> dict[str, dict[str, CatalogEntry]]:
    """几个数据源在这个视图下的目录：{源 id: {表名: CatalogEntry}}，没有目录的源不出现。live 一次查询读出。"""
    view = view or LIVE
    ids = [i for i in dict.fromkeys(source_ids) if i]
    if view.mode == "off" or not ids:
        return {}
    if view.mode == "snapshot":
        raw = {sid: found for sid in ids if (found := _snapshot_tables(view, sid))}
    else:
        raw = await catalog.read_catalogs(session, ids)
    return {sid: kept for sid, entries in raw.items() if (kept := _restricted(view, entries))}


async def read(view: CatalogView | None, source: Any) -> dict[str, CatalogEntry]:
    """一个数据源在这个视图下的目录：{表名: CatalogEntry}。用自己的短会话读（调用方的会话里已经加载的对象不会因为回滚
    过期）。live 读不到（库出错）当没有目录、记日志：目录是附加说明，不能挡住查询；快照不在了照实抛 CatalogSnapshotMissing。"""
    from app.db.base import SessionLocal

    view = view or LIVE
    source_id = getattr(source, "id", None)
    if view.mode == "off" or not source_id:
        return {}
    if view.mode == "snapshot":
        return _restricted(view, _snapshot_tables(view, source_id))
    try:
        async with SessionLocal() as own:
            return (await read_many(own, view, [source_id])).get(source_id, {})
    except Exception:  # noqa: BLE001
        logger.exception("读取数据源 %s 的数据目录失败，本次不附目录", getattr(source, "name", source_id))
        return {}


__all__ = ["CatalogSnapshotMissing", "CatalogView", "CatalogViewInvalid", "LIVE", "MODES", "OPTION_KEY", "SNAPSHOT_KIND",
           "STATUS_SETS", "describe", "of_options", "of_run", "read", "read_many", "restrict"]
