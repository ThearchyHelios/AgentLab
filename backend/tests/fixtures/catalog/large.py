"""虚构的大库：3,000 张表（next-round 03 的大库验收夹具）。名字、注释、需求一律虚构。

- 2,990 张「同一种写法」的表：20 个业务域 × 15 种表 × 10 个分区（华东、华南……），表名 {域}_{表}_{分区}，注释
  「{域中文}{表中文}（{分区中文}）」。它们彼此只差几个字，正好是挑表时最容易混淆的那一类。
- 10 张目标表（TARGETS）：名字和注释各不相同，每张配一句脚本化的需求。前 5 张给数据目录（中文名），有目录的表一律在探查
  范围里（pinned）；后 5 张在探查范围外，只有对象索引里的注释——SQLite 没有表注释，测试里直接写进对象索引的 comment，
  模拟 MySQL 的 TABLE_COMMENT、PostgreSQL 的 obj_description。

每张表 5 列（id 主键、业务日期、金额、状态、所属域的编号），没有数据：大库的验收看的是对象索引、范围、挑表，不查数。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

DOMAINS = [
    ("ord", "订单"), ("inv", "库存"), ("pur", "采购"), ("sal", "销售"), ("fin", "财务"), ("hrm", "人事"),
    ("crm", "客户"), ("lgs", "物流"), ("prd", "商品"), ("mkt", "营销"), ("svc", "客服"), ("whs", "仓储"),
    ("pay", "支付"), ("rtn", "退货"), ("sto", "门店"), ("emp", "员工"), ("sup", "供应商"), ("prm", "促销"),
    ("mbr", "会员"), ("aud", "审计"),
]
ENTITIES = [
    ("hdr", "表头"), ("line", "明细"), ("log", "日志"), ("snap", "快照"), ("daily", "日汇总"), ("monthly", "月汇总"),
    ("cfg", "配置"), ("map", "映射"), ("hist", "历史"), ("tmp", "临时表"), ("bak", "备份"), ("stat", "统计"),
    ("dim", "维度"), ("fact", "事实"), ("queue", "队列"),
]
REGIONS = [
    ("east", "华东"), ("south", "华南"), ("north", "华北"), ("west", "西部"), ("central", "华中"),
    ("ne", "东北"), ("arch", "历史归档"), ("test", "测试"), ("y2019", "2019年"), ("y2020", "2020年"),
]


@dataclass(frozen=True)
class Target:
    name: str
    comment: str
    need: str
    #: 有数据目录（中文名）、一律在探查范围里
    cataloged: bool


TARGETS = [
    Target("member_points_ledger", "会员积分流水", "会员积分每个月发放了多少、消耗了多少", True),
    Target("store_traffic_counter", "门店每日客流计数", "各门店上周每天的客流量", True),
    Target("refund_reason_dict", "退货原因字典", "按退货原因统计退货单数", True),
    Target("coupon_redemption", "优惠券核销记录", "优惠券的核销率", True),
    Target("supplier_delivery_sla", "供应商到货时效", "供应商的到货准时率", True),
    Target("warehouse_turnover_snap", "仓库库存周转快照", "各仓库的库存周转天数", False),
    Target("shift_roster", "员工排班表", "下周员工的排班情况", False),
    Target("complaint_ticket", "客户投诉工单", "客户投诉工单的平均处理时长", False),
    Target("online_payment_flow", "线上支付流水", "线上支付的成功率", False),
    Target("waybill_track", "物流运单签收轨迹", "物流运单从发货到签收用了多久", False),
]

TOTAL = len(DOMAINS) * len(ENTITIES) * len(REGIONS) - len(TARGETS) + len(TARGETS)


def generic_names() -> list[tuple[str, str]]:
    """2,990 张同一种写法的表 (表名, 注释)：按域、表、分区的顺序，最后 10 个组合让给目标表。"""
    out: list[tuple[str, str]] = []
    for code, cn in DOMAINS:
        for ent, ent_cn in ENTITIES:
            for reg, reg_cn in REGIONS:
                out.append((f"{code}_{ent}_{reg}", f"{cn}{ent_cn}（{reg_cn}）"))
    return out[: len(out) - len(TARGETS)]


def comments() -> dict[str, str]:
    """全部 3,000 张表的注释（写进对象索引，模拟有表注释的数据库）。"""
    out = dict(generic_names())
    out.update({t.name: t.comment for t in TARGETS})
    return out


def build(path: Path | str) -> Path:
    path = Path(path)
    db = sqlite3.connect(path)
    for name, _comment in generic_names():
        code = name.split("_", 1)[0]
        db.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, biz_date TEXT, amount REAL, status TEXT, "
                   f"{code}_id INTEGER)")
    for t in TARGETS:
        db.execute(f"CREATE TABLE {t.name} (id INTEGER PRIMARY KEY, biz_date TEXT, amount REAL, status TEXT, "
                   "ref_id INTEGER)")
    db.commit()
    db.close()
    return path


__all__ = ["DOMAINS", "ENTITIES", "REGIONS", "TARGETS", "TOTAL", "Target", "build", "comments", "generic_names"]
