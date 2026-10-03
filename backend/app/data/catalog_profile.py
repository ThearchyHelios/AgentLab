"""数据剖析：对业务库发少量只读查询，验证数据目录里推断出来的关联关系，补上基数、覆盖率和码值候选。

**为什么要有它。** 命名推断只看列名，可能推错，也给不出基数和覆盖率；而只有 verified、confirmed 的关系才会
触发错误级的 SQL 检查。剖析拿数据说话：从子表抽一批不同的键值去父表里对，对得上、父键唯一的升为 verified
（来源记为 profile，和外键约束的 fk 分开），对不上的保持 proposed 并在备注里写明覆盖率。顺带给状态类的列
取一次取值分布（码值候选，含义留给人填），给只有一个日期列的表提议业务日期。

**这是数据目录里唯一会对业务库发查询的地方**，约束都是硬的：

- 默认关闭，按数据源在 options.catalog_profile 里开启（profile_settings）；
- 每条查询先过守卫的只读判定（不论数据源本身可写与否），再交给查询层 run_query：同一套连接、守卫和
  时限，不另开连接；
- 每条查询有时限（不超过数据源自己的查询时限），每次剖析有查询次数上限和总时长上限；
- 表有多大先看数据库的统计信息，没有统计信息就数到上限为止，不做全表 COUNT；超过上限的表不做
  COUNT(DISTINCT)、MIN/MAX 这类整表统计，抽样和取值分布也只看前若干行；
- 数据源设置里遮罩的列不取样，也不出现在任何一条剖析查询里；
- 只取低基数列的取值分布（最多 21 个取值），不取明细行。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.data.engine import CATALOG_PROFILE_OPTION

#: 数据源 options 里剖析设置的键
PROFILE_OPTION = CATALOG_PROFILE_OPTION


# ==========================================================================
# 设置
# ==========================================================================


@dataclass(frozen=True)
class ProfileSettings:
    """一个数据源的剖析设置。缺省即「关闭」：剖析会对业务库发查询，必须有人按源明确打开。"""

    enabled: bool = False
    #: 一次剖析最多发几条查询（读统计信息、抽样、核对都算）。用完就停，剩下的记为「查询次数用完」
    max_queries: int = 60
    #: 单条查询的时限（秒）。实际取它和数据源自己的查询时限中较小的一个
    query_timeout_s: float = 10
    #: 每条关系从子表抽多少个不同的键值去父表里对
    sample_size: int = 2000
    #: 行数不超过它的表才做 COUNT(DISTINCT)、MIN/MAX 这类整表统计；更大的、或者不知道多大的表只看前若干行。
    #: 填 0 表示一律不做整表统计
    max_scan_rows: int = 100_000
    #: 一次剖析的总时长上限（秒）。接口是同步的，不能让一个请求无限地挂着
    max_total_s: float = 120

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: 数值项：(中文叫法, 下限, 上限, 是否必须是整数)。叫法和将来设置界面上的标签一致，报错里只写它
_NUMBER_FIELDS: dict[str, tuple[str, float, float, bool]] = {
    "max_queries": ("查询次数上限", 1, 500, True),
    "query_timeout_s": ("单条查询时限（秒）", 1, 60, False),
    "sample_size": ("抽样键值数", 10, 10_000, True),
    "max_scan_rows": ("整表统计行数上限", 0, 10_000_000, True),
    "max_total_s": ("总时长上限（秒）", 10, 600, False),
}
_ENABLED_LABEL = "开启数据剖析"
_KNOWN_KEYS = frozenset({"enabled", *_NUMBER_FIELDS})


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _number(value: Any, low: float, high: float, integer: bool) -> int | float | None:
    """设置里的一个数：数字和数字写成的文字都认（表单存的常是文字）。不合规返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    if number != number or not low <= number <= high:       # NaN 也在这里挡掉
        return None
    if number.is_integer():
        return int(number)
    return None if integer else number


def profile_settings(options: dict[str, Any] | None) -> ProfileSettings:
    """options → 剖析设置。读的时候宽松：库里存着的某一项不对（老数据、手改过），那一项用缺省，不报错；
    开关只认 true，写成别的一律当作没开。"""
    raw = (options or {}).get(PROFILE_OPTION)
    if not isinstance(raw, dict):
        return ProfileSettings()
    values: dict[str, Any] = {"enabled": raw.get("enabled") is True}
    for key, (_, low, high, integer) in _NUMBER_FIELDS.items():
        number = _number(raw.get(key), low, high, integer)
        if number is not None:
            values[key] = number
    return ProfileSettings(**values)


def profile_settings_problem(options: dict[str, Any] | None) -> str | None:
    """保存数据源时查一下剖析设置；没填、或者填对了返回 None。写的时候严格：不认识的项、超出范围的值都拒。"""
    raw = (options or {}).get(PROFILE_OPTION)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "数据剖析设置的格式不正确，请在数据源设置中重新填写"
    if unknown := sorted(set(raw) - _KNOWN_KEYS):
        return f"数据剖析设置中有无法识别的项「{'、'.join(map(str, unknown))}」，请删除后重试"
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        return f"数据剖析的「{_ENABLED_LABEL}」只能是开启或关闭"
    for key, (label, low, high, integer) in _NUMBER_FIELDS.items():
        if key in raw and _number(raw[key], low, high, integer) is None:
            kind = "整数" if integer else "数"
            return f"数据剖析的「{label}」需要填写 {_fmt(low)} 到 {_fmt(high)} 之间的{kind}；当前为「{raw[key]}」"
    return None


__all__ = ["PROFILE_OPTION", "ProfileSettings", "profile_settings", "profile_settings_problem"]
