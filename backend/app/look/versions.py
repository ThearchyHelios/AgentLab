"""查看的对象之一：工作流版本（总方案 L3、§5.3）。应用润色时看钉住的版本，模型也可以再看别的。

只列「已发布且未退役」的版本（app/apps/versions.published_versions，P5 的唯一说法），详情由 app/apps/describe.py 的
describe_version 给：入口字段、人工节点、口径卡指标、报告、取数节点、写入动作，以及版本、级别、哈希，不含任何密钥。

点名写法：source 写工作流名（或 id），name 写 "v3"（或 3）；不写 name 取最新的已发布版本。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.look.base import PICK_LIMIT, LookTarget
from app.look.pick import Picker, PickFailed

# ==========================================================================
# 写给模型的文字
# ==========================================================================

OVERVIEW_HEAD_PROMPT = "已发布的工作流（要看某一版的入口字段、节点、口径卡时用 look，kind 写 workflow_version）："
OVERVIEW_LINE_PROMPT = "- {name}：已发布 {versions}{level}"
OVERVIEW_MORE_PROMPT = "- …另有 {n} 个工作流"
LEVEL_PROMPT = {"governed": "受管级", "published": "已发布级"}
DETAIL_HEAD_PROMPT = "· {name} v{version}（{level}，哈希 {hash}）"
BRIEF_PROMPT = "· {name} v{version}：字数超过上限，只列名字，要看详情时点名再 look"
MISSING_PROMPT = "没找到工作流版本：{items}（只能查看已发布且未退役的版本；别拿别的版本顶替）"
INDEX_LINE_PROMPT = "{name}｜v{version}｜{description}"
PICK_WHAT_PROMPT = "工作流版本"
PICK_LINE_PROMPT = "每行一个工作流的最新已发布版本：工作流名｜版本｜说明"

#: 概览最多列几个工作流
OVERVIEW_WORKFLOWS = 40


@dataclass(frozen=True)
class VersionView:
    workflow_id: str
    workflow_name: str
    description: str
    version: Any  # WorkflowVersion：describe_version 要它

    @property
    def number(self) -> int:
        return int(self.version.version)

    @property
    def label(self) -> str:
        return f"{self.workflow_name} v{self.number}"


def _number(name: Any) -> int | None:
    text = str(name or "").strip().lower().lstrip("v")
    return int(text) if text.isdigit() else None


class VersionKind:
    kind = "workflow_version"
    label = "工作流版本"

    def __init__(self, items: list[VersionView], *, picker: Picker | None = None) -> None:
        self.items = items
        self.picker = picker

    @classmethod
    async def load(cls, session: Any, *, picker: Picker | None = None) -> VersionKind:
        """读全部工作流的已发布且未退役的版本。用调用方的会话，开流之前读好。"""
        from sqlalchemy import select

        from app.apps import versions as app_versions
        from app.db.models import Workflow

        items: list[VersionView] = []
        for wf in (await session.execute(select(Workflow).order_by(Workflow.name))).scalars().all():
            for v in await app_versions.published_versions(session, wf.id):
                items.append(VersionView(workflow_id=wf.id, workflow_name=wf.name,
                                         description=wf.description or "", version=v))
        return cls(items, picker=picker)

    def _workflows(self) -> dict[str, list[VersionView]]:
        out: dict[str, list[VersionView]] = {}
        for item in self.items:
            out.setdefault(item.workflow_id, []).append(item)
        return out

    # ---------------------------------------------------------------- 概览、目录

    async def overview(self) -> list[str]:
        flows = self._workflows()
        if not flows:
            return []
        lines = [OVERVIEW_HEAD_PROMPT]
        for items in list(flows.values())[:OVERVIEW_WORKFLOWS]:
            latest = max(items, key=lambda i: i.number)
            level = LEVEL_PROMPT.get(getattr(latest.version, "level", None) or "", "")
            lines.append(OVERVIEW_LINE_PROMPT.format(
                name=latest.workflow_name, versions="、".join(f"v{i.number}" for i in sorted(items, key=lambda i: i.number)),
                level=f"（最新 v{latest.number}，{level}）" if level else f"（最新 v{latest.number}）"))
        if len(flows) > OVERVIEW_WORKFLOWS:
            lines.append(OVERVIEW_MORE_PROMPT.format(n=len(flows) - OVERVIEW_WORKFLOWS))
        return lines

    def _latest(self, sources: list[str] | None) -> list[VersionView]:
        """每个工作流最新的已发布版本，sources（工作流名或 id）给了就只在这几个里。"""
        out = []
        for items in self._workflows().values():
            latest = max(items, key=lambda i: i.number)
            if not sources or latest.workflow_name in sources or latest.workflow_id in sources:
                out.append(latest)
        return out

    async def index(self, sources: list[str] | None) -> str:
        return "\n".join(INDEX_LINE_PROMPT.format(name=i.workflow_name, version=i.number,
                                                  description=i.description[:60] or "未填说明")
                         for i in self._latest(sources))

    # ---------------------------------------------------------------- 点名、挑选

    async def resolve(self, targets: list[LookTarget]) -> tuple[list[tuple[VersionView, str]], list[LookTarget]]:
        found: list[tuple[VersionView, str]] = []
        missing: list[LookTarget] = []
        for t in targets:
            source = str(t.get("source") or "")
            flow = [i for i in self.items if source in (i.workflow_name, i.workflow_id)]
            wanted = _number(t.get("name"))
            if t.get("name") and wanted is None:
                flow = []
            hit = (next((i for i in flow if i.number == wanted), None) if wanted is not None
                   else max(flow, key=lambda i: i.number, default=None))
            if hit is None:
                missing.append(t)
            elif all(i is not hit for i, _ in found):
                found.append((hit, "named"))
        return found, missing

    async def pick(self, need: str, sources: list[str] | None) -> tuple[list[tuple[VersionView, str]], str | None]:
        pool = self._latest(sources)
        if len(pool) <= PICK_LIMIT:
            return [(i, "picked") for i in pool], None
        if self.picker is None:
            return [], "没有可用的挑选模型"
        try:
            picks = await self.picker.pick(need=need, what=PICK_WHAT_PROMPT, line=PICK_LINE_PROMPT,
                                           index=await self.index(sources))
        except PickFailed as e:
            return [], str(e)
        by_name = {i.workflow_name: i for i in pool}
        out = list({by_name[n].workflow_id: (by_name[n], "picked") for n in
                    (s or name for s, name in picks) if n in by_name}.values())
        if picks and not out:
            return [], "挑出的工作流不存在或没有已发布的版本"
        return out[:PICK_LIMIT], None

    def key(self, obj: tuple[VersionView, str]) -> str:
        return f"workflow_version:{obj[0].workflow_id}:{obj[0].number}"

    # ---------------------------------------------------------------- 详情

    async def detail(self, objects: list[tuple[VersionView, str]], budget: int, *,
                     missing: list[LookTarget] = ()) -> tuple[str, list[dict[str, Any]]]:
        from app.apps import describe

        lines: list[str] = []
        used = 0
        for item, _how in objects:
            v = item.version
            head = DETAIL_HEAD_PROMPT.format(name=item.workflow_name, version=item.number,
                                             level=LEVEL_PROMPT.get(getattr(v, "level", None) or "", "未定级"),
                                             hash=str(getattr(v, "graph_hash", "") or "")[:8])
            body = describe.describe_version(v, workflow_name=item.workflow_name)
            block = head + "\n" + "\n".join(f"    {line}" for line in body.split("\n"))
            if used + len(block) > budget:
                block = BRIEF_PROMPT.format(name=item.workflow_name, version=item.number)
            used += len(block) + 1
            lines.append(block)
        if missing:
            lines.append(MISSING_PROMPT.format(items="、".join(
                f"{t.get('source') or ''} {t.get('name') or ''}".strip() for t in missing)))
        record = {"kind": "workflow_version", "named": [i.label for i, how in objects if how == "named"],
                  "picked": [i.label for i, how in objects if how == "picked"],
                  "missing": [f"{t.get('source') or ''} {t.get('name') or ''}".strip() for t in missing]}
        return "\n".join(lines), [record]
