"""能力登记与登记的端点（应用平台 A6，docs/design/app-platform-and-look.md §2.6、§3.9）。

capabilities 一行 = 图里一个可调用的工具（主键就是图里写的工具名），分两层：
- 系统同步：内置工具、数据源、自定义工具、MCP 启动时和各自增删改时同步进来（effect_system、params_schema、
  source_hash、synced_at、missing）；
- 人工治理：effect、approval、unattended_write、returns_schema、tabular、fields、masked、credential_mode、lookup、
  endpoint_id。每改一次 version 加一，运行发起时把用到的能力的治理快照固定下来（Run.options["capabilities"]）。

endpoints 是管理员登记的外部系统：按环境（test / prod）存 base_url 和认证头（值加密）。只有登记的端点能开内网访问；
模型给出的任意地址照旧走 tools/net.py 的完整 SSRF 检查。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_id

#: 能力对外部世界的效果。dynamic：同一个工具看这一次的参数定（可写数据源上的 db_query__：查询只读、DML 是写）
EFFECTS = ("read", "write", "dynamic")
#: 审批：inherit 跟三档信任；confirm 总要确认；none 只读能力在正式运行里免审批
APPROVALS = ("inherit", "confirm", "none")
#: 凭据：service 服务凭据；service_on_behalf 服务凭据 + AgentLab 签发的身份断言（不存用户自己的 token）
CREDENTIAL_MODES = ("service", "service_on_behalf")


class Capability(Base, TimestampMixin):
    __tablename__ = "capabilities"

    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    # 所属系统：数据源名、端点名、MCP 服务名、builtin、custom
    system: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    params_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    returns_schema: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 端点返回落成表格：{"rows": "/data/items", "columns": {"单号": "/id", "金额": "/amount"}}。声明了才能作证
    tabular: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # 同步时得出的效果（数据源可写为 dynamic，内置工具按 ToolSpec.dangerous，MCP 的 readOnlyHint 只当建议）
    effect_system: Mapped[str] = mapped_column(String(20), default="dynamic")
    # 治理后的效果；为空表示还没人确认过，按 effect_system 算
    effect: Mapped[str | None] = mapped_column(String(20), default=None)
    approval: Mapped[str] = mapped_column(String(20), default="inherit")
    unattended_write: Mapped[bool] = mapped_column(default=False)
    # 返回字段的语义，按 JSON 指针：{"/amount": {"label": 项, "unit": 项, ...}}，「项」的形状同数据目录
    # （data/catalog.py：{value, source, status, note, reason, updated_at}）
    fields: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 遮罩字段（JSON 指针）；查看、快照、视图都按它遮
    masked: Mapped[list[Any]] = mapped_column(default=list)
    credential_mode: Mapped[str] = mapped_column(String(30), default="service")
    endpoint_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 按幂等键查写入结果用的能力名（结果未知时自动核实）
    lookup: Mapped[str | None] = mapped_column(String(200), default=None)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    synced_at: Mapped[datetime | None] = mapped_column(default=None)
    # 来源已经删掉了（数据源、自定义工具、MCP 工具不在了）。不删行：运行和发布记录还引用它的版本
    missing: Mapped[bool] = mapped_column(default=False)
    updated_by: Mapped[str | None] = mapped_column(String(100), default=None)


class Endpoint(Base, TimestampMixin):
    """登记的外部系统。

    environments = {"test": {...}, "prod": {...}}，每个环境：
      base_url、headers（值经 core/crypto 加密，接口只回掩码）、allow_intranet（只有管理员能开）、verify_tls。
    正式运行走 prod；探索运行走 test，没有 test 时演练（不发送，记 simulated）。
    """

    __tablename__ = "endpoints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    environments: Mapped[dict[str, Any]] = mapped_column(default=dict)
    idempotency_header: Mapped[str] = mapped_column(String(100), default="Idempotency-Key")
    timeout_s: Mapped[int] = mapped_column(Integer, default=20)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str | None] = mapped_column(String(100), default=None)
