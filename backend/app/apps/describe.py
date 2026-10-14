"""给助手查看（look 的 workflow_version 类型）用的工作流版本详情：入口字段、人工节点、口径卡指标、报告、
取数节点、写入动作，以及版本、级别、哈希。不含任何密钥（L3）。"""
from __future__ import annotations

from typing import Any


def describe_version(version: Any, *, workflow_name: str) -> str:
    raise NotImplementedError("BE-3")
