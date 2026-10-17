"""助手的数据上下文：system 里只给概览，要看什么由模型自己 look（look 文档第二部分，总方案第五部分）。

**以前。** 每一轮开始前服务端按库的大小定怎么给：小库塞详细摘要，大库先用模型挑表、挑不出就退回全部表名。这一步
完全不看这一轮要做什么——用户说一句「你好」，助手也先花几秒挑表，再带着上万字的表结构去生成；挑表模型照提示说
「不需要」，反而被记成失败、退回列出全部表名（look 文档 §1.3、§1.4）。

**现在。** system 里每个库一行概览（app/look/tables.py），工具清单不再列表名。模型要看表时输出一行 look，生成过程
（api/copilot_gen.py）读到就停下，由查看服务（app/look/service.py）一次给齐：点名直取；只给需求时候选不超过 12 张
全给，否则挑选模型按需求挑；补两跳以内的中间表；全部字段、数据目录、连接条件；遮罩列只给列名和类型。闲聊只调一次
模型，数据类请求一次查看到位。

**各入口看什么**（总方案 §5.3）：画布、问数据看表和能力（问数据只列只读能力）；发布前修复、升级证据看表，由系统按要修
的 SQL 代为查看（look 文档 D5）；应用润色看工作流版本和能力（在 copilot.py 里组装）。问数据另看已发布口径（next-round 02
做法 4：搭建者及以上；使用者发起的图里看得到拷进来的取数 SQL，这一波不给）：助手可以 use_caliber 选用一份口径做骨架，
「什么时候该看」换成 LOOK_WHEN_ANSWER_PROMPT，输出协议多 USE_CALIBER_PROMPT 一段。运行时写 SQL 的 Agent 不看口径（拍板
L2：保持独立解读，「等价已证明」才有意义）。

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

#: 问数据里（能看已发布口径时）的「什么时候该看」：问数据那一条按口径账本改写（next-round 02 做法 4 ④），别的照旧
LOOK_WHEN_ANSWER_PROMPT = LOOK_WHEN_PROMPT.replace(
    """  · 问数据：查库交给 agent 节点，它运行时会自己查表结构。只有从概览看不出该用哪个库、用户点了表名、用户说了口径时
    才看一次，把看到的表名写进 agent 的提示词，运行时少走弯路""",
    """  · 问数据：先看已发布口径（kind 写 caliber）。问法和口径的定义完全对得上时用 use_caliber 选用它；口径只覆盖问题的
    一部分时 use_caliber 选用那一部分，其余交给 agent 补；问法多了口径没有的条件（比如「含退款」「只看新客」）时不要套
    口径，交给 agent 去查，系统会自动拿它的 SQL 和口径对账。查库交给 agent 时它运行时会自己查表结构：只有从概览看不出
    该用哪个库、用户点了表名时才看表，把看到的表名写进 agent 的提示词""")

#: 问数据里输出协议多的一段：选用口径（ledger/compose.py）。只在这一轮能看已发布口径时接在协议后面。
#: 两个 id 写清楚（真实验收里模型第一次常把 card 写成自己起的 c1，被拒之后才改对）：card 是那一版工作流图里口径卡的节点 id，
#: 照抄查看结果；id 是这次选用自己起的名字，生成的口径卡用它当节点 id
USE_CALIBER_PROMPT = """- 选用已发布口径（只在问数据里、look 过 caliber 之后）：
  {"op":"use_caliber","id":"c1","workflow_id":"…","version":3,"card":"card","metrics":["指标id"],"inputs":{"字段":"值"}}
  照抄查看结果里给的那一行写法：workflow_id、version、card 原样照抄，只改 id、metrics、inputs。两个 id 别弄混：
  · card 填那一版工作流图里口径卡的节点 id（查看结果的写法里已经写好，例如 "card"），指明选哪一张卡，不是你起的名字；
  · id 是你给这次选用起的名字（简短英文小写，例如 c1），系统生成的口径卡就用它当节点 id，取数节点是 c1_<原 id>。
  系统把入口连到取数、取数连到口径卡；你**不要**自己写这几个节点，也不要改它们。接下来只需把口径卡连到报告
  （add_edge c1 → report，这里的 c1 是你起的 id），报告里用 [[m:指标id]] 引用。inputs 按口径的入口字段填（日期、门店这类
  问题里给出的条件），填不对会被拒收、原因会交回给你改；口径对不上问法时不要选用"""

#: 系统代为查看时写进记录的需求（界面上显示）
SYSTEM_LOOK_FIX_NEED = "要修的工作流里，查库节点的 SQL 用到的表"
SYSTEM_LOOK_UPGRADE_NEED = "要升级的工作流里，查库节点的 SQL 用到的表"


async def look_service(sources: list[Any], *, spec: ModelSpec | None, graph: Mapping[str, Any] | None = None,
                       kinds: tuple[str, ...] = ("table", "capability"), answer: bool = False,
                       catalog_view: Any = None, trail: Mapping[str, Any] | None = None,
                       session: Any = None) -> LookService:
    """按入口组装这一轮的查看服务。读目录、读能力登记都用各自的短会话，开流前后都能调。

    spec 是助手的模型规格：挑选模型第一次要用时才按它拿（关思考、额度按 look/pick.PICK_MAX_TOKENS）。graph 是现有工作流，概览里列出它查过的表。
    answer：问数据（意图为回答），能力只列只读的。catalog_view：表详情里给哪一份数据目录（data/catalog_view.py），不给是当前目录。
    trail：按需加入探查范围时的留痕（哪一轮对话：{conversation_id}；谁取这次请求的身份）。
    kinds 里有 caliber 时用 session（调用方开流之前的会话）读已发布口径，问数据里另判能不能组合（look/calibers 的 compose）。
    """
    from app.look.calibers import CaliberKind

    picker = Picker(spec)
    made: list[Any] = []
    if "table" in kinds:
        made.append(await TableKind.load(sources, graph=graph, picker=picker, catalog_view=catalog_view, trail=trail))
    if "capability" in kinds:
        made.append(await CapabilityKind.load(picker=picker, read_only=answer))
    if "caliber" in kinds and session is not None:
        made.append(await CaliberKind.load(session, picker=picker, compose=answer))
    return LookService(made)


def composing(looks: LookService) -> bool:
    """这一轮能不能选用已发布口径（问数据里读好了能组合的口径卡）。"""
    kind = looks.kinds.get("caliber")
    return kind is not None and getattr(kind, "options", None) is not None


def look_when(looks: LookService) -> str:
    """「什么时候该看」：能选用已发布口径的问数据用改写过的那一份。"""
    return LOOK_WHEN_ANSWER_PROMPT if composing(looks) else LOOK_WHEN_PROMPT


def composer(looks: LookService) -> Any | None:
    """GraphApplier 用的组合器（ledger/compose.Composer）；这一轮不能选用口径时为 None。"""
    if not composing(looks):
        return None
    from app.ledger.compose import Composer

    return Composer(list(looks.kinds["caliber"].options))


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
    "LOOK_WHEN_ANSWER_PROMPT", "LOOK_WHEN_PROMPT", "SQL_RULES_PROMPT", "SYSTEM_LOOK_FIX_NEED", "SYSTEM_LOOK_UPGRADE_NEED",
    "USE_CALIBER_PROMPT", "composer", "composing", "graph_tables", "look_service", "look_when", "section", "system_look",
]
