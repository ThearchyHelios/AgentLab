"""bp_* 操作的确定性落地（N2、§3.2）。模型（BlueprintApplier）和用户（PATCH /api/blueprints/{id}）用同一组操作，
只是来源记成 assistant 或 user。每张卡片有白名单（例如 people[].policy.kind 只能是 any / all / quorum），越界的拒绝并照实交回。

包 B1 实现。
"""
from __future__ import annotations

from typing import Any


class BpRefused(Exception):
    """操作被拒：message 是写给人看的一句话，code 是 bp.* 机器码。"""

    def __init__(self, message: str, code: str = "bp.refused") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def apply_bp_op(spec: dict[str, Any], op: dict[str, Any], *, source: str) -> dict[str, Any]:
    """把一个 bp_* 操作落到蓝图上，交回新的蓝图（不改入参）。source 是 user / assistant。不合规时抛 BpRefused。"""
    raise NotImplementedError


def blueprint_issues(spec: dict[str, Any], *, for_: str) -> list[dict[str, Any]]:
    """蓝图检查（§3.13 bp.*）：for_ = trial（缺项挡试运行）/ publish（待确认项挡发布）/ personal。
    每条 {code, level, path, message}。"""
    raise NotImplementedError
