"""方言容器集成测试的执行端（由 scripts/dialect-it.sh 起好容器后调用，不进 CI）。

对每种库：
1. 用管理账号建虚构夹具（见 _ddl / _rows）；
2. 用 AgentLab 自己的探查（app.data.introspect）读结构，核对类型写法、ENUM 取值、Oracle 小写带引号的名字、索引首列；
3. 用 AgentLab 自己的剖析（catalog_profile.profile_table，目录读写换成内存里的）逐表剖析，经只读连接、守卫、单条时限，
   和线上走的是同一条路；
4. 直接跑取值观察要用的新查询（按日行数、时间窗口里的取值分布、块采样、最新业务日期）；
5. 对象索引的字典查询（第 2 波，data/source_objects.list_objects：MySQL information_schema.TABLES、PostgreSQL pg_class 加
   obj_description、Oracle ALL_OBJECTS 加 ALL_TAB_COMMENTS）：走字典查询（不退回 Inspector）、类型、注释、行数估算对得上；
6. 取值观察的四类检查（第 3 波，data/catalog_profile_observe）经剖析引擎跑一遍（只观察，目录读写换成内存里的）：行数、
   码值观察（小表整表、大表 PG / Oracle 块采样、MySQL 按存储顺序的前 N 行）、最新业务日期（大表业务日期列打头有索引）；
7. 断言：所有查询零报错（留痕里没有超时、失败、守卫拒），每个结论和下面按夹具定义手算的一致。

夹具的数（手算的依据）：orders 40 行，id 1–40。
- store_code = id % 5 + 1（整数子列，指向文字主键 stores.code，'1'–'5' 都在）；
- status CHAR(2)：id 是 3 的倍数为 'A'，否则 'B'（13 / 27）；type：奇数 'web'、偶数 'shop'（20 / 20）；
  level = id % 3 + 1（1: 13、2: 14、3: 13）；pay_way（只 MySQL / MariaDB，ENUM('cash','card','coupon')）：奇数 cash、偶数 card；
- paid_at = 2026-09-01 10:00 加 (id % 10) 天，id 39、40 是无效日期（MySQL 零日期、PG ±infinity、Oracle 0001-01-01 和
  9999-12-31 23:59:59）；date（日期列）同 paid_at 的日子，id 39、40 为空；
- events 先插 10 行、收集统计信息，再插 1500 行（统计信息做旧：说 10 行，其实 1510 行，超过整表统计上限 1000）；
- Oracle 另有小写带引号的表 "visits"（"id"、"level"），3 行，level 1、1、2。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import sys
import tempfile
import time
from collections import Counter
from types import SimpleNamespace
from typing import Any

# AgentLab 自己的库放到临时目录：不碰开发数据目录（必须在导入 app 之前设）
os.environ["AGENTLAB_DATA_DIR"] = tempfile.mkdtemp(prefix="agentlab-dialect-it-")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core.crypto import encrypt  # noqa: E402
from app.data import catalog, catalog_profile, introspect, source_objects  # noqa: E402
from app.data import engine as data_engine  # noqa: E402
from app.data.catalog_profile import ProfileSettings, SqlDialect  # noqa: E402

PASSWORD = os.environ.get("DIALECT_IT_PASSWORD", "itpass")
SETTINGS = ProfileSettings(enabled=True, table_max_queries=40, table_max_s=60, query_timeout_s=10, sample_size=100,
                           max_scan_rows=1000)
N_ORDERS, N_EVENTS_OLD, N_EVENTS_NEW = 40, 10, 1500
BASE = _dt.datetime(2026, 9, 1, 10, 0, 0)
WINDOW_FROM = "2026-09-05"
#: 按日行数的上界（观察当天，含）：WINDOW_UNTIL 在全部夹具日期之后；WINDOW_CUT 落在中间，之后的日子不进窗口
WINDOW_UNTIL = "2026-09-30"
WINDOW_CUT = "2026-09-07"

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ✓ " if ok else "  ✗ ") + what)
    if not ok:
        failures.append(what)


# ---------------------------------------------------------------- 手算


def _order(i: int) -> dict[str, Any]:
    return {"id": i, "store_code": i % 5 + 1, "status": "A" if i % 3 == 0 else "B",
            "type": "web" if i % 2 else "shop", "level": i % 3 + 1, "pay_way": "cash" if i % 2 else "card",
            "paid_at": None if i >= 39 else BASE + _dt.timedelta(days=i % 10),
            "date": None if i >= 39 else (BASE + _dt.timedelta(days=i % 10)).date()}


ORDERS = [_order(i) for i in range(1, N_ORDERS + 1)]
VALID = [o for o in ORDERS if o["paid_at"] is not None]
IN_WINDOW = [o for o in VALID if o["paid_at"].date().isoformat() >= WINDOW_FROM]
EXPECT = {
    "status": Counter(o["status"] for o in ORDERS),
    "type": Counter(o["type"] for o in ORDERS),
    "level": Counter(str(o["level"]) for o in ORDERS),
    "pay_way": Counter(o["pay_way"] for o in ORDERS),
    "paid_at": (min(o["paid_at"] for o in VALID), max(o["paid_at"] for o in VALID)),
    "day_volume": Counter(o["paid_at"].date().isoformat() for o in IN_WINDOW),
    "day_volume_cut": Counter(o["paid_at"].date().isoformat() for o in IN_WINDOW
                              if o["paid_at"].date().isoformat() <= WINDOW_CUT),
    "window_status": Counter(o["status"] for o in IN_WINDOW),
}


# ---------------------------------------------------------------- 各库的夹具


class Target:
    def __init__(self, key: str, kind: str, port: int) -> None:
        self.key, self.kind, self.port = key, kind, port
        self.oracle = kind == "oracle"
        self.mysql = kind in ("mysql", "mariadb")
        self.pg = kind == "postgresql"

    def admin_url(self) -> str:
        if self.pg:
            return f"postgresql+asyncpg://postgres:{PASSWORD}@127.0.0.1:{self.port}/it"
        if self.mysql:
            return f"mysql+aiomysql://root:{PASSWORD}@127.0.0.1:{self.port}/it"
        return f"oracle+oracledb://it:{PASSWORD}@127.0.0.1:{self.port}/?service_name=FREEPDB1"

    def source(self) -> SimpleNamespace:
        user = {"postgresql": "postgres", "mysql": "root", "mariadb": "root", "oracle": "it"}[self.kind]
        return SimpleNamespace(id=f"it-{self.key}", name=f"it_{self.key}", kind=self.kind, host="127.0.0.1",
                               port=self.port, database="FREEPDB1" if self.oracle else "it", username=user,
                               password=encrypt(PASSWORD), options={}, readonly=True, description="",
                               schema_cache={}, origin="manual", enabled=True)


def _q(t: Target, name: str) -> str:
    """建表时的列名：Oracle 上 TYPE、LEVEL、DATE 是保留字，按大写加引号建（库里就是大写的普通名字）。"""
    if t.oracle and name in ("type", "level", "date"):
        return f'"{name.upper()}"'
    if t.mysql:
        return f"`{name}`"
    return f'"{name}"' if name in ("type", "level", "date") else name


def _ddl(t: Target) -> list[str]:
    ts = "TIMESTAMP" if not t.mysql else "DATETIME"
    intg = "NUMBER(10)" if t.oracle else "INTEGER"
    vc = "VARCHAR2" if t.oracle else "VARCHAR"
    cols = [f"id {intg} PRIMARY KEY", f"store_code {intg}", "status CHAR(2)", f"{_q(t, 'type')} {vc}(10)",
            f"{_q(t, 'level')} {intg}", f"paid_at {ts}", f"{_q(t, 'date')} DATE"]
    if t.mysql:
        cols.append("pay_way ENUM('cash','card','coupon')")
    orders = f"CREATE TABLE orders ({', '.join(cols)})"
    if t.mysql:
        orders += " COMMENT='订单'"
    out = [f"CREATE TABLE stores (code {vc}(10) PRIMARY KEY, name {vc}(20))", orders,
           "CREATE INDEX ix_orders_paid_at ON orders (paid_at)"]
    if not t.mysql:
        out.append("COMMENT ON TABLE orders IS '订单'")
    events = f"CREATE TABLE events (id {intg} PRIMARY KEY, kind {vc}(10))"
    if t.pg:
        events += " WITH (autovacuum_enabled = false)"
    if t.mysql:
        events += " STATS_PERSISTENT=1 STATS_AUTO_RECALC=0"
    out.append(events)
    if t.oracle:
        out.append('CREATE TABLE "visits" ("id" NUMBER(10) PRIMARY KEY, "level" NUMBER(10))')
    return out


def _snapshot(t: Target) -> list[str]:
    """覆盖检查（data/coverage.py）的夹具：只存当天的滚动表（每天整表重灌的那种）。日期按库自己的当前日期写，避开容器
    和本机时区不一致；daily_snapshot 的日期按文字 'YYYYMMDD' 存（部署上撞过的写法，只在 MySQL / MariaDB 上判），
    daily_snapshot_d 按 DATE 存。"""
    vc = "VARCHAR2" if t.oracle else "VARCHAR"
    today_text = ("TO_CHAR(SYSDATE, 'YYYYMMDD')" if t.oracle
                  else "REPLACE(CAST(CURRENT_DATE AS CHAR), '-', '')" if t.mysql
                  else "REPLACE(CAST(CURRENT_DATE AS VARCHAR), '-', '')")
    today = "TRUNC(SYSDATE)" if t.oracle else "CURRENT_DATE"
    out = [f"CREATE TABLE daily_snapshot (event_day {vc}(8), visitor {vc}(16))",
           f"CREATE TABLE daily_snapshot_d (event_day DATE, visitor {vc}(16))"]
    for who in ("v1", "v2", "v3"):
        out.append(f"INSERT INTO daily_snapshot (event_day, visitor) VALUES ({today_text}, '{who}')")
        out.append(f"INSERT INTO daily_snapshot_d (event_day, visitor) VALUES ({today}, '{who}')")
    return out


def _paid(t: Target, o: dict[str, Any]) -> str:
    if o["paid_at"] is not None:
        stamp = o["paid_at"].strftime("%Y-%m-%d %H:%M:%S")
        return f"TIMESTAMP '{stamp}'" if t.oracle else f"'{stamp}'"
    # 无效日期：各家能存进去的那种
    if t.mysql:
        return "'0000-00-00 00:00:00'"
    if t.pg:
        return "'infinity'" if o["id"] == 39 else "'-infinity'"
    return "TIMESTAMP '9999-12-31 23:59:59'" if o["id"] == 39 else "TIMESTAMP '0001-01-01 00:00:00'"


def _rows(t: Target) -> list[str]:
    out = [f"INSERT INTO stores (code, name) VALUES ('{c}', 'store {c}')" for c in ("1", "2", "3", "4", "5", "A1")]
    for o in ORDERS:
        day = f"DATE '{o['date'].isoformat()}'" if o["date"] is not None else "NULL"
        cols = ["id", "store_code", "status", _q(t, "type"), _q(t, "level"), "paid_at", _q(t, "date")]
        vals = [str(o["id"]), str(o["store_code"]), f"'{o['status']}'", f"'{o['type']}'", str(o["level"]),
                _paid(t, o), day]
        if t.mysql:
            cols.append("pay_way")
            vals.append(f"'{o['pay_way']}'")
        out.append(f"INSERT INTO orders ({', '.join(cols)}) VALUES ({', '.join(vals)})")
    if t.oracle:
        out += ['INSERT INTO "visits" ("id", "level") VALUES (1, 1)', 'INSERT INTO "visits" ("id", "level") VALUES (2, 1)',
                'INSERT INTO "visits" ("id", "level") VALUES (3, 2)']
    return out


def _events(start: int, n: int) -> list[str]:
    return [f"INSERT INTO events (id, kind) VALUES ({i}, '{'open' if i % 4 else 'close'}')"
            for i in range(start, start + n)]


def _analyze(t: Target, table: str) -> list[str]:
    if t.pg:
        return [f"ANALYZE {table}"]
    if t.mysql:
        return [f"ANALYZE TABLE {table}"]
    return [f"BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, '{table.upper()}'); END;"]


async def _wait(t: Target, deadline_s: float = 360) -> Any:
    engine = create_async_engine(t.admin_url())
    started = time.monotonic()
    while True:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1 FROM DUAL" if t.oracle else "SELECT 1"))
            return engine
        except Exception as e:  # noqa: BLE001 - 容器还没起来：等
            if time.monotonic() - started > deadline_s:
                raise RuntimeError(f"{t.key} 在 {deadline_s:.0f} 秒内没起来：{e}") from e
            await asyncio.sleep(3)


async def _setup(t: Target) -> None:
    engine = await _wait(t)
    try:
        async with engine.begin() as conn:
            if t.mysql:
                # 关掉严格模式才写得进零日期（老库里常见的情形）
                await conn.execute(text("SET SESSION sql_mode = ''"))
            for sql in _ddl(t) + _rows(t) + _events(1, N_EVENTS_OLD) + _snapshot(t):
                await conn.exec_driver_sql(sql)
        async with engine.begin() as conn:
            for sql in _analyze(t, "events") + _analyze(t, "orders") + _analyze(t, "stores"):
                await conn.exec_driver_sql(sql)
            if t.oracle:
                await conn.exec_driver_sql("""BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, '"visits"'); END;""")
        async with engine.begin() as conn:
            if t.mysql:
                await conn.execute(text("SET SESSION sql_mode = ''"))
            for sql in _events(N_EVENTS_OLD + 1, N_EVENTS_NEW):
                await conn.exec_driver_sql(sql)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- 剖析：目录换成内存里的


async def _profile(source: Any, tables: list[str], notes: dict[str, dict[str, Any]], *,
                   observe: bool = False) -> dict[str, Any]:
    cx = catalog_profile.make_context(source, SETTINGS, options={})
    captured: dict[str, Any] = {}

    async def db(fn: Any, *args: Any) -> Any:
        if fn is catalog_profile._read_notes:
            return notes.get(args[1], {})
        if fn is catalog_profile._record:
            _sid, table, out, result, _actor, _rec = args
            catalog_profile._settle_status(result)
            captured[table] = (out, result)
            return None
        raise AssertionError(f"剖析要了没预料的库操作：{fn}")

    for table in tables:
        await catalog_profile.profile_table(cx, table, db=db, regular=not observe, observe=observe)
    return captured


def _key(meta: dict[str, Any], name: str) -> str:
    """表结构里这个名字的写法（Oracle 上普通名字是小写，TYPE 这类保留字是大写）。"""
    return next(str(c["name"]) for c in meta["columns"] if str(c["name"]).lower() == name)


def _day(v: Any) -> str:
    return str(v)[:10]


async def run_target(t: Target) -> None:
    print(f"\n== {t.key}（{t.kind}，端口 {t.port}）")
    await _setup(t)
    source = t.source()
    try:
        cache = await introspect.introspect(source)
        source.schema_cache = cache
        tables = cache["tables"]
        orders, events, stores = tables["orders"], tables["events"], tables["stores"]
        types = {c["name"].lower(): c for c in orders["columns"]}
        check(not cache.get("failed") and {"orders", "events", "stores"} <= set(tables), "探查读到三张夹具表")
        check("paid_at" in [c.lower() for c in orders.get("index_first_columns") or []],
              f"索引首列记进了表结构：{orders.get('index_first_columns')}")
        if t.oracle:
            check(stores["columns"][0]["type"] == "VARCHAR2(10 CHAR)", f"Oracle 文字类型写全了长度和单位："
                                                                         f"{stores['columns'][0]['type']}")
            check(types["id"]["type"] == "NUMBER(10, 0)", f"Oracle 数字类型写全了精度：{types['id']['type']}")
            check("visits" in tables and tables["visits"].get("quote_name") is True,
                  "Oracle 小写带引号的表记了 quote_name")
            check(all(c.get("quote_name") is True for c in tables["visits"]["columns"]),
                  "Oracle 小写带引号的列记了 quote_name")
            check(_key(orders, "level") == "LEVEL" and "quote_name" not in types["level"],
                  "Oracle 的保留字列 LEVEL 是大写的普通名字，不记 quote_name")
        if t.mysql:
            check(types["pay_way"]["type"].startswith("ENUM(") and
                  types["pay_way"].get("enum_values") == ["cash", "card", "coupon"],
                  f"MySQL ENUM 记下了取值列表：{types['pay_way']}")
        if t.pg:
            check(types["paid_at"]["type"] == "TIMESTAMP WITHOUT TIME ZONE",
                  f"PostgreSQL 时间类型写全了时区：{types['paid_at']['type']}")

        store_col, code_col = _key(orders, "store_code"), _key(stores, "code")
        rel = catalog.relation_id("orders", [store_col], "stores", [code_col])
        notes = {"orders": {"relations": [{"id": rel, "columns": [store_col], "to_table": "stores",
                                           "to_columns": [code_col], "cardinality": "many_to_one", "source": "name",
                                           "status": "proposed"}]}}
        names = ["orders", "events", "stores"] + (["visits"] if t.oracle else [])
        got = await _profile(source, names, notes)

        logs = [q for _, r in got.values() for q in r.query_log]
        bad = [f"{q['kind']} {q['target']}：{q['outcome']} {q['detail']}" for q in logs if q["outcome"] != "ok"]
        check(not bad, f"剖析的 {len(logs)} 条查询零报错" + (f"：{bad[:3]}" if bad else ""))

        out, result = got["orders"]
        check(result.status == "ok", f"orders 剖析完成：{result.status} {result.error or ''}")
        [r] = out.relations or [None]
        check(r is not None and r.coverage == 1.0 and r.sample == 5 and r.matched == 5 and r.holds,
              f"整数子列对文字主键：取 5 个键值全对上、升为有确证（{r and r.note}）")
        codes = {c.column.lower(): c for c in out.codes}
        for col in ("status", "type", "level") + (("pay_way",) if t.mysql else ()):
            c = codes.get(col)
            check(c is not None and Counter(dict(c.values)) == EXPECT[col],
                  f"{col} 的取值分布和手算一致：{c and c.values}")
        if t.oracle or t.pg:
            check(codes["status"].values and all(not k.endswith(" ") for k, _ in codes["status"].values),
                  "CHAR(2) 码值去掉了补齐的空格")
        if t.mysql:
            check(codes["pay_way"].declared == ["coupon"] and "coupon" in codes["pay_way"].note,
                  "ENUM 列定义里没出现的取值也进了码值")
            check(catalog_profile.MYSQL_CODES_COLLATION_NOTE in codes["status"].note, "MySQL 码值说明写了排序规则")
            check(catalog_profile.MYSQL_COLLATION_NOTE in r.note, "MySQL 关系说明写了排序规则")
        ranges = {d["column"].lower(): d for d in result.date_ranges}
        paid = ranges.get("paid_at")
        lo, hi = EXPECT["paid_at"]
        check(paid is not None and paid["min"][:19].replace("T", " ") == str(lo) and
              paid["max"][:19].replace("T", " ") == str(hi) and paid.get("invalid") == 2,
              f"paid_at 的范围排除了 2 个无效日期：{paid}")
        day = ranges.get("date")
        check(day is not None and _day(day["min"]) == lo.date().isoformat() and _day(day["max"]) ==
              hi.date().isoformat() and not day.get("invalid"), f"date 列（保留字）的范围：{day}")

        out_e, result_e = got["events"]
        size = result_e.row_estimate
        d = SqlDialect(t.kind)
        stats = await data_engine.run_query(source, d.stats_sql(events, "events"))
        estimated = int(float(stats.rows[0][0] or 0)) if stats.rows else 0
        if estimated <= SETTINGS.max_scan_rows:
            check(size is not None and size.rows is None and size.at_least == SETTINGS.max_scan_rows + 1,
                  f"统计信息做旧（说 {estimated} 行，其实 {N_EVENTS_OLD + N_EVENTS_NEW} 行）也认出了大表：{size}")
        else:
            # 有的库插入时自己就更新了行数估算，夹具做不旧：照实说，只核对按统计信息认出了大表
            print(f"  · 统计信息没能做旧（库自己更新成了 {estimated} 行），只核对按统计信息认出大表")
            check(size is not None and size.method == "stats" and (size.rows or 0) > SETTINGS.max_scan_rows,
                  f"按统计信息认出了大表：{size}")
        kind_codes = next((c for c in out_e.codes if c.column.lower() == "kind"), None)
        check(kind_codes is not None and not kind_codes.complete and "按存储顺序取前" in kind_codes.note,
              f"大表的码值按存储顺序的前若干行，不标已列全：{kind_codes and kind_codes.note}")

        if t.oracle:
            out_v, result_v = got["visits"]
            lv = next((c for c in out_v.codes if c.column == "level"), None)
            check(result_v.status == "ok" and lv is not None and Counter(dict(lv.values)) == Counter({"1": 2, "2": 1}),
                  f"小写带引号的表和列照样剖析：{result_v.status} {lv and lv.values}")
            vstats = await data_engine.run_query(source, d.stats_sql(tables["visits"], "visits"))
            check(bool(vstats.rows) and vstats.rows[0][0] is not None, f"小写表的统计信息按原名查得到：{vstats.rows}")

        # ---- 取值观察的新方法
        tbl = d.table(orders, "orders")
        paid_col = d.column(orders, _key(orders, "paid_at"))
        res = await data_engine.run_query(source, d.day_volume_sql(tbl, paid_col, WINDOW_FROM, WINDOW_UNTIL))
        got_days = Counter({_day(row[0]): int(row[1]) for row in res.rows})
        check(got_days == EXPECT["day_volume"], f"按日行数和手算一致（无效日期不算进窗口）：{dict(got_days)}")
        res = await data_engine.run_query(source, d.day_volume_sql(tbl, paid_col, WINDOW_FROM, WINDOW_CUT))
        got_cut = Counter({_day(row[0]): int(row[1]) for row in res.rows})
        check(got_cut == EXPECT["day_volume_cut"],
              f"按日行数只数到观察当天（含当天的时分秒，之后的不算）：{dict(got_cut)}")
        status_col = d.column(orders, _key(orders, "status"))
        res = await data_engine.run_query(source, d.window_value_counts_sql(tbl, status_col, paid_col, WINDOW_FROM, 21))
        got_window = Counter({str(row[0]).rstrip(" "): int(row[1]) for row in res.rows})
        check(got_window == EXPECT["window_status"], f"时间窗口里的取值分布和手算一致：{dict(got_window)}")
        sampled = d.sample_value_counts_sql(d.table(events, "events"), d.column(events, _key(events, "kind")), 50, 21)
        if t.pg or t.oracle:
            res = await data_engine.run_query(source, sampled)
            total = sum(int(row[1]) for row in res.rows)
            check(total <= N_EVENTS_OLD + N_EVENTS_NEW, f"块采样跑得通（取到 {total} 行）")
        else:
            check(sampled is None, "MySQL / MariaDB 没有块采样，照实返回 None")
        # ---- 对象索引的字典查询（第 2 波）
        index = await source_objects.list_objects(source, None)
        found = {o.name: o for o in index.objects}
        check(index.method == "dictionary" and index.note is None,
              f"对象索引走字典查询，没有退回 Inspector：{index.method} {index.note or ''}")
        wanted = {"orders", "events", "stores"} | ({"visits"} if t.oracle else set())
        check(wanted <= set(found), f"对象索引列出了夹具表：{sorted(found)}")
        check(found["orders"].kind == "table" and found["orders"].comment == "订单",
              f"对象索引的类型、表注释：{found.get('orders')}")
        if t.oracle:
            check(found["visits"].quote is True, "Oracle 小写带引号的表在对象索引里记了 quote")
        est = found["events"].est_rows
        check(est is None or est > 0, f"对象索引的行数估算（做过统计的表）：{est}")

        # ---- 取值观察（第 3 波）：只观察，经剖析引擎、只读守卫、单条时限
        paid = _key(orders, "paid_at")
        obs_notes = {
            "orders": {"business_date": {"value": {"column": paid}, "source": "human", "status": "confirmed"},
                       "columns": {_key(orders, "status"): {"codes": {"value": {"A": "", "B": ""}, "source": "human",
                                                                      "status": "confirmed", "complete": True}}}},
            "events": {"columns": {_key(events, "kind"): {"codes": {"value": {"open": "", "close": ""},
                                                                    "source": "profile", "status": "proposed"}}}},
        }
        seen = await _profile(source, ["orders", "events"], obs_notes, observe=True)
        logs = [q for _, r in seen.values() for q in r.query_log]
        bad = [f"{q['kind']} {q['target']}：{q['outcome']} {q['detail']}" for q in logs if q["outcome"] != "ok"]
        check(not bad, f"取值观察的 {len(logs)} 条查询零报错" + (f"：{bad[:3]}" if bad else ""))
        got_o = {(o["kind"], o["column"].lower()): o for o in seen["orders"][1].observations}
        got_e = {(o["kind"], o["column"].lower()): o for o in seen["events"][1].observations}
        check(got_o.get(("rows", ""), {}).get("data", {}).get("rows") == N_ORDERS, f"orders 的行数观察：{got_o.get(('rows', ''))}")
        codes = got_o.get(("codes_observe", "status"))
        check(codes is not None and codes["basis_key"] == "full"
              and Counter(dict(map(tuple, codes["data"]["values"]))) == EXPECT["status"],
              f"小表的码值观察看整表、和手算一致：{codes and codes['data']}")
        fresh = got_o.get(("freshness", "paid_at"))
        check(fresh is not None and fresh["data"]["max"][:19].replace("T", " ") == str(EXPECT["paid_at"][1]),
              f"最新业务日期排除了无效日期、和手算一致：{fresh and fresh['data']}")
        ev = got_e.get(("codes_observe", "kind"))
        method = "sample" if (t.pg or t.oracle) else "first_rows"
        check(ev is not None and ev["basis"]["method"] == method,
              f"大表、没有业务日期：{'块采样' if method == 'sample' else '按存储顺序的前 N 行'}（{ev and ev['basis_key']}）")
        res = await data_engine.run_query(source, d.max_date_sql(tbl, paid_col))
        check(bool(res.rows) and str(res.rows[0][0])[:19].replace("T", " ") == str(EXPECT["paid_at"][1]),
              f"max_date_sql 直接跑：{res.rows}")
        await _coverage(t, source)
    finally:
        await data_engine.engines.invalidate(source.id)


async def _coverage(t: Target, source: Any) -> None:
    """覆盖检查（data/coverage.py）在真库上：按运行当天现算的上周区间，表里只有当天的数据 → 判成没有数据；区间包含当天、
    只是别的条件筛空的 → 不报。界在库里算（各方言自己的写法），表的范围现查 MIN / MAX。"""
    from app.data import coverage

    last_week = {"mysql": ("event_day >= DATE_SUB(CURRENT_DATE, INTERVAL (WEEKDAY(CURRENT_DATE) + 7) DAY) "
                           "AND event_day < DATE_SUB(CURRENT_DATE, INTERVAL WEEKDAY(CURRENT_DATE) DAY)"),
                 "pg": ("event_day >= date_trunc('week', CURRENT_DATE) - INTERVAL '7 days' "
                        "AND event_day < date_trunc('week', CURRENT_DATE)"),
                 "oracle": "event_day >= TRUNC(SYSDATE, 'IW') - 7 AND event_day < TRUNC(SYSDATE, 'IW')"}
    recent = {"mysql": "event_day >= DATE_SUB(CURRENT_DATE, INTERVAL 1 DAY)",
              "pg": "event_day >= CURRENT_DATE - INTERVAL '1 day'",
              "oracle": "event_day >= TRUNC(SYSDATE) - 1"}
    flavor = "mysql" if t.mysql else "pg" if t.pg else "oracle"
    today_sql = "SELECT TRUNC(SYSDATE) FROM DUAL" if t.oracle else "SELECT CURRENT_DATE"
    today = str((await data_engine.run_query(source, today_sql)).rows[0][0])[:10]
    for table in ["daily_snapshot_d"] + (["daily_snapshot"] if t.mysql else []):
        sql = f"SELECT COUNT(DISTINCT visitor) AS n FROM {table} WHERE {last_week[flavor]}"
        gaps = await coverage.result_gaps(source, sql, (await data_engine.run_query(source, sql)).to_payload())
        check(len(gaps) == 1 and gaps[0]["data_range"] == [today, today]
              and [c.lower() for c in gaps[0]["columns"]] == ["n"],
              f"覆盖检查：{table} 只有当天的数据，上周的区间判成没有数据：{gaps}")
        pre = await coverage.sql_gaps(source, sql)
        check(len(pre) == 1, f"助手自查：{table} 上周的区间判成没有数据：{pre}")
        fine = f"SELECT COUNT(DISTINCT visitor) AS n FROM {table} WHERE visitor = 'nobody' AND {recent[flavor]}"
        quiet = await coverage.result_gaps(source, fine, (await data_engine.run_query(source, fine)).to_payload())
        check(quiet == [], f"覆盖检查：{table} 区间里有数据、只是别的条件筛空的不报：{quiet}")
        check(await coverage.sql_gaps(source, fine) == [], f"助手自查：{table} 区间包含当天的不报")


async def main() -> int:
    only = set((os.environ.get("DIALECT_IT_ONLY") or "pg,mysql,mariadb,oracle").split(","))
    plan = [("pg", "postgresql", "DIALECT_IT_PG_PORT"), ("mysql", "mysql", "DIALECT_IT_MYSQL_PORT"),
            ("mariadb", "mariadb", "DIALECT_IT_MARIADB_PORT"), ("oracle", "oracle", "DIALECT_IT_ORACLE_PORT")]
    for key, kind, env in plan:
        if key not in only:
            continue
        port = os.environ.get(env)
        if not port:
            failures.append(f"{key}：没有拿到容器端口（{env}）")
            continue
        try:
            await run_target(Target(key, kind, int(port)))
        except Exception as e:  # noqa: BLE001 - 一种库出错不挡别的库，照实记下
            print(f"  ✗ {key} 中途出错：{type(e).__name__}: {e}")
            failures.append(f"{key}：{type(e).__name__}: {e}")
    await data_engine.engines.close()
    print("\n" + ("全部通过" if not failures else f"{len(failures)} 项不通过：\n- " + "\n- ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
