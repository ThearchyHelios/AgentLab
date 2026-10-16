"""查看的接口与形状（L2、L3），以及生成过程的注入接口（L1）。前端解码（run/decode.ts）按这里的形状认 look 事件。

look 行（模型输出）：
  {"op":"look","need":"…","targets":[{"kind":"table","source":"库 B","name":"refunds"}],"sources":["库 B"]}
  旧写法 tables:[{source, table}] 也认，读进来换成 targets（kind=table）。

look 事件（发给前端、存进 meta.looks）：
  {"op":"look","id":"L1","by":"model"|"system","need":"…","status":"ok"|"index"|"failed","reason"?,
   "elapsed_ms":…,"objects":[{"kind":"table","source":…,"source_id":…,"named":[…],"picked":[…],"joined":[…],
   "missing":[…],"total":…,"explored"?:…,"catalog_tables":…,"schema_synced_at":…,"catalog_versions":{…},
   "added_to_scope"?:[…],"prefiltered"?:…},
   {"kind":"capability","named":[…],"missing":[…],"versions":{…}},
   {"kind":"workflow_version","named":[…],"missing":[…]}]}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

#: 实现的对象类型。口径（caliber）是已发布口径卡上的指标，蓝图助手复用口径时看（look/calibers.py，normal-user-apps N2）
LOOK_KINDS = ("table", "capability", "workflow_version", "caliber")

LOOK_LIMIT = 3              # 起草阶段每轮最多查看几次
REPAIR_LOOK_LIMIT = 1       # 自查修正阶段另给几次
LOOK_RESULT_CHARS = 12_000  # 一次查看结果的字数上限
PICK_LIMIT = 12             # 挑选最多挑几个；候选不超过这个数时不挑、全给（沿用）
CONTEXT_TABLE_LIMIT = 16    # 一次查看最多给几张表的详情（含中间表，沿用）
#: 挑选时限。经 OpenAI 兼容接入的推理模型关不掉思考（look/pick.PICK_MAX_TOKENS），先想再写：原来的 20 秒常常想到一半就
#: 掐了，交回目录让主模型自己再看一遍反而更慢
PICK_TIMEOUT_S = 45
#: 大库的检索预筛（next-round 03 A2）：候选超过这么多张时，挑选模型只看检索（schema_view.search_tables）的前这么多个命中、
#: 工作流已用到的表、命中表的一跳关联表（最多 NEIGHBOR_LIMIT 张），挑选提示词从几万字降到几千字；不超过时照旧给整份目录
PREFILTER_LIMIT = 120
NEIGHBOR_LIMIT = 40
#: 按需加入（目录-D4）：点名或挑中探查范围外的表，当场只读反射这一张、落盘、留痕。每次查看最多几张，每次运行最多几张
ON_DEMAND_PER_LOOK = 4
ON_DEMAND_PER_RUN = 8


class LookTarget(TypedDict, total=False):
    kind: str      # LOOK_KINDS 之一
    source: str    # 表：库名；能力：所属系统；工作流版本：工作流名
    name: str      # 表名 / 能力名 / "v3"


@dataclass
class LookResult:
    """一次查看交回给模型的文字，和发给前端、存进 meta.looks 的记录。

    记录为 None 的是没真正查看的那几种回话（need 没写、次数用完、重复查看），不发事件。counted：占不占这一轮的次数。
    """

    text: str
    record: dict[str, Any] | None
    counted: bool = True


class LookKind(Protocol):
    """每种可查看的对象都实现同一组方法（L3）。遮罩按类型各自处理。

    detail 交回的记录块是 objects[] 里的若干项（表按库各一项，能力、工作流版本各一项）；missing 是 resolve 认不出的
    点名，由 detail 写进文字和记录。key 是一个对象在这一轮里的身份，重复查看靠它短路。
    """

    kind: str
    #: 写给模型的类别名：表、能力、工作流版本
    label: str

    async def overview(self) -> list[str]:
        """概览：进 system，每个来源一行。"""
        ...

    async def index(self, sources: list[str] | None) -> str:
        """目录：挑选失败时交回，每个对象一行。"""
        ...

    async def resolve(self, targets: list[LookTarget]) -> tuple[list[Any], list[LookTarget]]:
        """按点名解析：(认出的对象, 没找到的点名)。"""
        ...

    async def pick(self, need: str, sources: list[str] | None) -> tuple[list[Any], str | None]:
        """按需求挑选：(挑出的对象, 失败原因)。返回空列表且原因为 None 表示「按这个需求没有相关的」。"""
        ...

    async def detail(self, objects: list[Any], budget: int, *,
                     missing: list[LookTarget] = ()) -> tuple[str, list[dict[str, Any]]]:
        """详情文字（受字数上限约束）和这一类的记录块（objects[] 里的项）。"""
        ...

    def key(self, obj: Any) -> str:
        ...


# ---- 生成过程的注入接口（L1）：循环只管「读到 look 停下、执行、接着写、计次数」，落地和自查由调用方注入 ----


@dataclass
class Applied:
    changed: bool
    forward: bool = True
    problem: str | None = None


class Applier(Protocol):
    def apply(self, op: dict[str, Any]) -> Applied: ...

    def landed(self) -> list[str]:
        """已经落地的对象 id（接着写的提示里列出来，提醒模型不要重复）。"""
        ...

    def continuing(self) -> None:
        """进入「接着写」：之后和已落地完全相同的操作忽略、不转发（look §2.5.5）。"""
        ...

    def repairing(self) -> None:
        """进入自查修正：plan、done 由循环吃掉；回答、目录修改提案这类只在起草时有意义的不再收。"""
        ...


class Checker(Protocol):
    async def issues(self) -> list[dict[str, Any]]: ...

    def repair_request(self, issues: list[dict[str, Any]], looks_text: str) -> str: ...


@dataclass
class LookBudget:
    limit: int = LOOK_LIMIT
    used: int = 0
    seen: set[str] = field(default_factory=set)
