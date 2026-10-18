"""问数据的运行怎么发起：开登录后 user 角色的那几条规则，HTTP 发起（api/runs.start_run）和服务端跑的一轮（chat/job.py）共用。

以前只有 start_run 一处（浏览器发 POST /api/runs）。一轮问数据搬到服务端之后，发起运行的是这一轮的后台任务，规则不能另抄一份——
使用者的问数据运行只许白名单里的节点和工具、记忆固定为自己的范围、查询结果在工具边界遮、数据源一律只读，这几条一处说清。
请求体能不能带记忆范围、知识库，能不能发正式运行，这些是 HTTP 请求本身的事，仍在 start_run 里判。
"""
from __future__ import annotations

from typing import Any


class ChatRunRefused(Exception):
    """这张图使用者在问数据里不能运行：str(e) 就是给人看的原因（HTTP 发起时是 403 的 detail）。"""


def user_chat_run(graph: Any, actor: str | None) -> tuple[str, dict[str, Any]]:
    """开登录后 user 角色发起问数据运行：交回 (记忆范围, 运行选项)。图里有白名单之外的节点、工具抛 ChatRunRefused。

    记忆固定为这位使用者自己的范围、知识库取设置里的默认值（记忆、知识库的内容不经遮罩，见 auth/chat_policy 开头）；查询结果
    在工具边界就遮（事件、回答里只有遮过的值——它读得到自己这次运行的事件，api/deps.py 的 _run_scope）；数据源查询一律只读，
    写语句不交给审批。"""
    from app.auth.chat_policy import chat_graph_problem, chat_memory_scope
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    if problem := chat_graph_problem(graph):
        raise ChatRunRefused(problem)
    return chat_memory_scope(actor), {MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True}


__all__ = ["ChatRunRefused", "user_chat_run"]
