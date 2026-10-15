"""按模板实例化蓝图（N3、§3.3–§3.4）：确定性关节按卡片填，生成关节留给模型（TemplateApplier 的 fill_joint），
实例化后的图和骨架逐节点比对，只许关节位置不同。模型失败时生成关节留空、标「待填」（P3）。

包 B1 实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Instantiated:
    #: 实例化出来的图（GraphSpec 的 JSON）。生成关节没填的位置是占位
    graph: dict[str, Any]
    #: {joint_id: {"status": filled|pending|refused, "by": template|assistant|user, "note"?: …}}
    joints: dict[str, Any]
    #: 按模板排布的应用草稿（agentlab.app/1），钉住项的 version 为空（还没发布）
    app_spec: dict[str, Any]
    #: 定时任务参数（看板类）或 None
    schedule: dict[str, Any] | None
    problems: list[dict[str, Any]] = field(default_factory=list)


def instantiate(blueprint: dict[str, Any], *, filled: dict[str, Any] | None = None) -> Instantiated:
    """蓝图 + 已填的生成关节（{joint_id: value}）→ 图、关节状态、应用草稿、定时任务参数。纯函数，不落库。"""
    raise NotImplementedError


def skeleton_changes(template_graph: dict[str, Any], graph: dict[str, Any], joints: list[Any]) -> list[dict[str, Any]]:
    """骨架比对：除关节位置外的任何差异都交回（tpl.skeleton_changed）。"""
    raise NotImplementedError
