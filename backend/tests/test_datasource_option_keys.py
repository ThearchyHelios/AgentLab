"""数据源 options 里 AgentLab 自己的配置项一律不交给驱动（data/engine._NON_DRIVER_OPTIONS）。

2026-10 部署上撞过：探查范围（introspect_scope，大库那一波加的，运行时按需加入表时也会自动写）、定时维护的步骤
（catalog_maintenance）没进摘除名单，被当成驱动参数拼进连接串，MySQL 驱动 connect() 收到不认识的关键字参数，这个数据源上
每一条查询都失败（「connect() got an unexpected keyword argument 'introspect_scope'」）。名单是手写的，以后再加配置项照样会
漏，所以这里把 app/data 下所有 *_OPTION 常量（数据源 options 的键都这样声明）扫出来逐个核对：新加一项忘了进名单，这条测试先报。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.data.engine import _NON_DRIVER_OPTIONS, build_url

DATA_DIR = Path(__file__).resolve().parents[1] / "app" / "data"
_DECL = re.compile(r'^([A-Z_]*_OPTION)\s*(?::\s*\w+\s*)?=\s*"([^"]+)"', re.M)


def _declared() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(DATA_DIR.glob("*.py")):
        for name, key in _DECL.findall(path.read_text(encoding="utf-8")):
            out[f"{path.stem}.{name}"] = key
    return out


def test_every_declared_option_key_is_kept_away_from_the_driver():
    declared = _declared()
    assert {"source_objects.SCOPE_OPTION", "source_maintenance.MAINTENANCE_OPTION"} <= set(declared), declared
    missing = {where: key for where, key in declared.items() if key not in _NON_DRIVER_OPTIONS}
    assert not missing, f"这些数据源配置项会被拼进连接串交给驱动：{missing}"


@pytest.mark.parametrize("kind", ["mysql", "postgresql", "oracle"])
def test_app_options_never_reach_the_connection_url(kind):
    keys = set(_declared().values()) | set(_NON_DRIVER_OPTIONS)
    app_options = {key: {"mode": "rules"} if key in ("introspect_scope", "catalog_maintenance", "catalog_profile") else "x"
                   for key in keys}
    src = SimpleNamespace(kind=kind, host="db.example", port=None, database="shop", username="reader", password=None,
                          options={"charset": "utf8mb4", **app_options})
    url = build_url(src)
    assert "charset=utf8mb4" in url, "驱动参数照旧拼进去"
    for key in keys:
        assert f"{key}=" not in url, (key, url)
