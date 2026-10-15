"""助手的数据上下文：system 里只给概览，要看什么由模型自己 look（look 文档第二部分，总方案第五部分）。

**以前。** 每一轮开始前服务端按库的大小定怎么给：小库塞详细摘要，大库先用模型挑表、挑不出就退回全部表名。这一步
完全不看这一轮要做什么——用户说一句「你好」，助手也先花几秒挑表，再带着上万字的表结构去生成；挑表模型照提示说
「不需要」，反而被记成失败、退回列出全部表名（look 文档 §1.3、§1.4）。

**现在。** system 里每个库一行概览（app/look/tables.py），工具清单不再列表名。模型要看表时输出一行 look，生成过程
（api/copilot_gen.py）读到就停下，由查看服务（app/look/service.py）一次给齐：点名直取；只给需求时候选不超过 12 张
全给，否则挑选模型按需求挑；补两跳以内的中间表；全部字段、数据目录、连接条件；遮罩列只给列名和类型。闲聊只调一次
模型，数据类请求一次查看到位。

**各入口看什么**（总方案 §5.3）：画布、问数据看表和能力（问数据只列只读能力）；发布前修复、升级证据看表，由系统按要修
的 SQL 代为查看（look 文档 D5）；应用润色看工作流版本和能力（在 copilot.py 里组装）。

这里提供：look_service（按入口组装查看服务，开流之前调：读目录、读能力登记要用会话）、section（system 里数据源那一节）、
system_look（系统代为查看）、写 SQL 的硬性要求和「什么时候该看」的判断标准。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.look.base import LookResult
from app.look.capabilities import CapabilityKind
from app.look.pick import PICK_MAX_TOKENS, Picker  # noqa: F401 - copilot.py 按这个额度取挑选模型的规格
from app.look.service import LookService
from app.look.tables import TableKind, graph_tables
from app.providers.factory import ModelSpec

# ==========================================================================
# 写给模型的文字（不上界面）
# ==========================================================================

SQL_RULES_PROMPT = (
    "\n\n写 SQL 的硬性要求：\n"
    # 这三条都是真实踩过的：Oracle 上漏 schema 前缀直接 ORA-00942，
    # 写惯 MySQL 的模型会顺手写 LIMIT，而一条没有行数限制的查询能拖垮生产库
    "- 表名照抄查看结果里的全名（含 schema 前缀），漏掉前缀在 Oracle 上会直接报 ORA-00942\n"
    "- 认准方言：Oracle 用 FETCH FIRST n ROWS ONLY，不是 LIMIT\n"
    "- 一次只写一条语句；分号拼接会被拒\n"
    # 真实踩过：列名是照着业务说法猜的，跑到查库那一步才「no such column」
    "- 列名只照查看结果里列出来的写；没看过的表，动笔前先 look，别猜字段名。搭完自查会拿 SQL 里的列名去对结构，"
    "对不上会打回来让你改\n"
    "- 比率、增幅、占比这类派生计算进口径卡（metrics 节点）；查询结果里原样的数，报告撰写节点（report）"
    "可以直接用 [[v:Q1.r0.列名]] 引用。别让 llm 节点直接对数字做算术\n"
    # 真实运行里报告表格的表头全是英文缩写的原始列名：[[table:]] 原样用查询结果的列名
    "- 结果列用中文别名（AS 月份、AS 曝光量），报告里的表格表头、引用直接可读。按上面标的方言加标识符引号："
    "MySQL / MariaDB 用反引号（AS `月份`），PostgreSQL、Oracle、SQLite 用双引号（AS \"月份\"），不要用单引号"
    "（那是字符串）；只给最外层 SELECT 的结果列起别名，WHERE、JOIN、GROUP BY 里照旧写原列名\n"
)

#: 什么时候该看（look 文档 D4）：画布、问数据共用
LOOK_WHEN_PROMPT = """\
什么时候该看：
  · 闲聊、解释、改提示词、改流程结构、改不碰 SQL 的节点：不看
  · 问数据：查库交给 agent 节点，它运行时会自己查表结构。只有从概览看不出该用哪个库、用户点了表名、用户说了口径时
    才看一次，把看到的表名写进 agent 的提示词，运行时少走弯路
  · 直接写 SQL 的取数节点、修改已有的 SQL：动笔前看，一次把要用的表都点上
  · 用户说了一条数据事实：先看那张表的现有目录，再提目录修改
  · 要看就先看，看完再规划，不要先搭节点再回头看；这次对话里已经看过的表直接点名看，不要只给需求"""

#: 系统代为查看时写进记录的需求（界面上显示）
SYSTEM_LOOK_FIX_NEED = "要修的工作流里，查库节点的 SQL 用到的表"
SYSTEM_LOOK_UPGRADE_NEED = "要升级的工作流里，查库节点的 SQL 用到的表"


async def look_service(sources: list[Any], *, spec: ModelSpec | None, graph: Mapping[str, Any] | None = None,
                       kinds: tuple[str, ...] = ("table", "capability"), answer: bool = False) -> LookService:
    """按入口组装这一轮的查看服务。读目录、读能力登记都用各自的短会话，开流前后都能调。

    spec 是助手的模型规格：挑选模型第一次要用时才按它拿（关思考、额度按 look/pick.PICK_MAX_TOKENS）。graph 是现有工作流，概览里列出它查过的表。
    answer：问数据（意图为回答），能力只列只读的。
    """
    picker = Picker(spec)
    made: list[Any] = []
    if "table" in kinds:
        made.append(await TableKind.load(sources, graph=graph, picker=picker))
    if "capability" in kinds:
        made.append(await CapabilityKind.load(picker=picker, read_only=answer))
    return LookService(made)


async def section(looks: LookService) -> str:
    """system 里数据源那一节：各类对象的概览，有数据源时再加写 SQL 的硬性要求。什么都没有时是空串，不占提示词。"""
    overview = await looks.overview()
    if not overview:
        return ""
    tables = looks.kinds.get("table")
    return "\n\n" + overview + (SQL_RULES_PROMPT if tables is not None and tables.plans else "")


async def system_look(looks: LookService, graph: Mapping[str, Any] | None, sources: list[Any], *,
                      need: str) -> LookResult | None:
    """系统代为查看（look 文档 D5）：图里调用工具节点的 SQL 用到的表，点名查看，不调挑选模型。没有就是 None。

    记录、文字和模型主动查看的完全一样，只是 by=system、不占模型的次数；模型仍然可以再看。
    """
    targets = [{"kind": "table", "source": source, "name": table}
               for source, tables in graph_tables(graph, sources).items() for table in tables]
    if not targets or "table" not in looks.kinds:
        return None
    return await looks.look({"op": "look", "need": need, "targets": targets}, by="system")


__all__ = [
    "LOOK_WHEN_PROMPT", "SQL_RULES_PROMPT", "SYSTEM_LOOK_FIX_NEED", "SYSTEM_LOOK_UPGRADE_NEED", "graph_tables",
    "look_service", "section", "system_look",
]
