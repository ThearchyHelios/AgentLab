"""9.5 的性能（WP-7）：合成 20 万行 × 10 列的列表（同 p2-probe/perf.py 的结构），量 4.9 的各项目标。

默认跳过；`RECIPE_PERF=1` 时跑（仓库里没有现成的慢测试标记，按规格用环境变量开关）。量到的数字打印出来
（加 -s 看得到），也经 record_property 进 junit 报告。判定照 9.5：超过目标 1.5 倍算不通过（断言失败），
在 1–1.5 倍之间记为「待优化」（只打印，不失败）。

量的都是真管线（default_pipeline）经接口走一遍的耗时，和用户点按钮时服务端做的事一样：
- 暂存（POST imports/stage：扫描 + 预览 + 规则起草 + 部分干跑）≤ 扫描时间 + 3 s；
- 单次改配方 / 回答问题（静态校验 + 部分干跑）≤ 1 s；
- 暂存后的试运行（复用 scan_from_json，不再扫描）≤ 18 s，进程内存增量 ≤ 200 MB；
- 上传新一期（扫描 + 试运行）≤ 25 s；整列有公式（有缓存值）的同样大小 ≤ 40 s（整列公式的表暂存后试运行
  规格没给目标，只记录）；
- 结构仿照客流表的夹具：暂存 ≤ 1 s，单次回答 ≤ 0.5 s，试运行 ≤ 1 s。
20 万行的夹具与 p2-probe/perf.py 一样用 openpyxl 的 write_only 写出，它不写 `<dimension>`；Excel 存盘的文件都有
这个元素。openpyxl 只读模式打开工作簿时，缺 `<dimension>` 的工作表会被整张解析一遍（parse_dimensions 一直读到
</sheetData>），所以每项都分「无 dimension（同 perf.py）」和「有 dimension（同 Excel 存盘）」两种各量一次，
目标对两种一视同仁。
夹具全部合成（假名、随机数），不调用任何模型。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import random
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.data import xlsx_cells, xlsx_scan
from app.main import app
from tests.fixtures.xlsx import patch_parts, sheet_part
from tests.fixtures.xlsx.flow import flow_workbook

pytestmark = pytest.mark.skipif(os.environ.get("RECIPE_PERF") != "1",
                                reason="性能测试默认跳过：RECIPE_PERF=1 时运行（20 万行的夹具要生成十几秒）")

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ROWS = 200_000
SHEET = "明细"
HEADERS = ["地区", "产品", "渠道", "门店", "日期", "销量", "单价", "金额", "成本", "备注"]
TYPES = ["TEXT", "TEXT", "TEXT", "TEXT", "DATE", "INTEGER", "REAL", "INTEGER", "INTEGER", "TEXT"]
FLOW_RECIPE = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text("utf-8"))

#: 超过目标的这个倍数算不通过（9.5）
FAIL_FACTOR = 1.5

RESULTS: list[dict[str, Any]] = []


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def big_list(seed: int, *, formulas: bool = False) -> bytes:
    """20 万行 × 10 列的列表：一列日期，无公式；formulas=True 时「金额」整列是公式（=销量+成本），并在
    压缩包层面补上缓存值、去掉 fullCalcOnLoad（模拟 Excel 保存过）。"""
    from openpyxl import Workbook

    rnd = random.Random(seed)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(SHEET)
    ws.append(HEADERS)
    cached: dict[int, int] = {}
    base = dt.date(2026, 8, 1)
    for i in range(ROWS):
        r = i + 2
        qty, cost = rnd.randint(1, 999), rnd.randint(1, 9999)
        amount: Any = rnd.randint(1, 99999)
        if formulas:
            amount = f"=F{r}+I{r}"
            cached[r] = qty + cost
        ws.append([f"区{i % 7}", f"品{i % 50}", "线上" if i % 2 else "线下", f"店{i % 300}",
                   base + dt.timedelta(days=i % 31), qty, round(rnd.random() * 100, 2), amount, cost, "无"])
    buf = io.BytesIO()
    wb.save(buf)
    raw = buf.getvalue()
    edits = {"xl/workbook.xml": lambda t: t.replace(' fullCalcOnLoad="1"', "")}
    if formulas:
        pat = re.compile(r'<c r="H(\d+)"><f>([^<]*)</f><v\s*/?>(?:</v>)?</c>')

        def fill(xml: str) -> str:
            out, n = pat.subn(lambda m: f'<c r="H{m.group(1)}"><f>{m.group(2)}</f><v>{cached[int(m.group(1))]}</v></c>',
                              xml)
            assert n == ROWS, n
            return out
        edits[sheet_part(raw, SHEET)] = fill
    return patch_parts(raw, edits)


def with_dimension(raw: bytes) -> bytes:
    """在 <sheetData> 前补上 <dimension ref="A1:J200001"/>（Excel 存盘的文件都有它）。"""
    def add(xml: str) -> str:
        assert "<dimension " not in xml
        return xml.replace("<sheetViews>", f'<dimension ref="A1:J{ROWS + 1}"/><sheetViews>', 1)
    return patch_parts(raw, {sheet_part(raw, SHEET): add})


def list_recipe() -> dict[str, Any]:
    return {
        "recipe_format": "agentlab-recipe/2",
        "sheets": [{"id": "s1", "match": {"name": SHEET}, "blocks": [{
            "id": "明细", "layout": "list", "table": "销售明细",
            "columns": [{"header": h, "name": h, "type": t} for h, t in zip(HEADERS, TYPES)]}]}],
        "tables": [{"name": "销售明细"}],
    }


@pytest.fixture(scope="module")
def plain_files() -> tuple[bytes, bytes]:
    return big_list(1), big_list(2)


@pytest.fixture(scope="module")
def formula_files() -> tuple[bytes, bytes]:
    return big_list(3, formulas=True), big_list(4, formulas=True)


def _variant(files: tuple[bytes, bytes], dimension: bool) -> tuple[bytes, bytes]:
    return (with_dimension(files[0]), with_dimension(files[1])) if dimension else files


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=600) as c:
        yield c


# --------------------------------------------------------------------------
# 计时与内存
# --------------------------------------------------------------------------


def _rss_mb() -> float:
    """当前进程的常驻内存（MB）。没有 psutil，用 ps 读（macOS、Linux 都有）。"""
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True).stdout
    return int(out.strip() or 0) / 1024


class _Peak:
    """后台线程每 50 ms 采一次 RSS，记下这一段里的峰值。"""

    def __init__(self) -> None:
        self.base = _rss_mb()
        self.peak = self.base
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            self.peak = max(self.peak, _rss_mb())

    def __enter__(self) -> _Peak:
        self._t.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._t.join()
        self.peak = max(self.peak, _rss_mb())

    @property
    def delta(self) -> float:
        return self.peak - self.base


def judge(record_property, name: str, value: float, target: float | None, unit: str = "s") -> dict[str, Any]:
    """记录一项实测；超过目标 1.5 倍判「不通过」，1–1.5 倍记「待优化」。target=None 是规格没给目标的项，
    只记录。返回这一行，由测试末尾的 assert_all 一并断言（量全了再判，前一项不通过不挡住后面各项的实测）。"""
    if target is None:
        row = {"item": name, "value": round(value, 2), "target": None, "unit": unit, "ratio": 0.0,
               "verdict": "仅记录（规格未给目标）"}
        RESULTS.append(row)
        record_property(name, json.dumps(row, ensure_ascii=False))
        print(f"\n[perf] {name}: {value:.2f} {unit}（规格未给目标，仅记录）")
        return row
    ratio = value / target
    verdict = "通过" if ratio <= 1 else ("待优化" if ratio <= FAIL_FACTOR else "不通过")
    row = {"item": name, "value": round(value, 2), "target": round(target, 2), "unit": unit,
           "ratio": round(ratio, 2), "verdict": verdict}
    RESULTS.append(row)
    record_property(name, json.dumps(row, ensure_ascii=False))
    print(f"\n[perf] {name}: {value:.2f} {unit}（目标 {target:.2f} {unit}，{ratio:.2f} 倍）{verdict}")
    return row


def assert_all(rows: list[dict[str, Any]]) -> None:
    bad = [r for r in rows if r["ratio"] > FAIL_FACTOR]
    assert not bad, "超过目标 1.5 倍：" + "；".join(
        f"{r['item']} {r['value']} {r['unit']}（目标 {r['target']}，{r['ratio']} 倍）" for r in bad)


async def _timed(coro) -> tuple[Any, float]:
    t0 = time.perf_counter()
    out = await coro
    return out, time.perf_counter() - t0


def _confirms(st: dict[str, Any]) -> list[str]:
    return [i["id"] for i in st["trial"]["confirm_items"]]


# --------------------------------------------------------------------------
# 20 万行 × 10 列
# --------------------------------------------------------------------------


async def _big_flow(client, record_property, files: tuple[bytes, bytes], *, label: str,
                    reupload_target: float, trial_target: float | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    first, second = files
    fn1, fn2 = f"明细_{label}_甲.xlsx", f"明细_{label}_乙.xlsx"

    # 扫描单独量一次：暂存的目标是「扫描时间 + 3 s」
    t0 = time.perf_counter()
    scan = xlsx_scan.scan(first)
    scan_s = time.perf_counter() - t0
    sheet = scan.sheets[0]
    assert sheet.nonempty == ROWS * 10 + 10 and sheet.nonempty > xlsx_cells.GRID_MAX_CELLS
    print(f"\n[perf] {label} 文件 {len(first) / 1e6:.1f} MB，扫描 {scan_s:.2f} s")
    record_property(f"{label}：扫描", round(scan_s, 2))

    # 暂存：扫描 + 预览 + 规则起草 + 部分干跑
    name = f"perf_{uuid.uuid4().hex[:8]}"
    resp, stage_s = await _timed(client.post("/api/datasources/imports/stage",
                                             files={"file": (fn1, first, XLSX)}, data={"name": name}))
    assert resp.status_code == 201, resp.text
    st = resp.json()
    sid = st["id"]
    assert st["draft_partial"] is True, "大表的交互干跑只看前 500 行（4.9）"
    rows.append(judge(record_property, f"{label}：暂存（扫描 + 预览 + 规则起草 + 部分干跑）", stage_s, scan_s + 3))

    # 单次回答问题（有问题时）、单次改配方：静态校验 + 部分干跑
    if st["questions"]:
        answers = {q["id"]: {"value": q["options"][0]["value"], "reason": "合成理由"} for q in st["questions"][:1]}
        resp, ans_s = await _timed(client.post(f"/api/datasources/imports/{sid}/answers", json={"answers": answers}))
        assert resp.status_code == 200, resp.text
        rows.append(judge(record_property, f"{label}：单次回答问题", ans_s, 1.0))
    resp, put_s = await _timed(client.put(f"/api/datasources/imports/{sid}/recipe", json={"recipe": list_recipe()}))
    assert resp.status_code == 200, resp.text
    st = resp.json()
    assert st["recipe_problems"] == [], st["recipe_problems"]
    assert st["draft_partial"] is True, "大表的交互干跑只看前 500 行（4.9）"
    rows.append(judge(record_property, f"{label}：单次改配方（PUT recipe）", put_s, 1.0))

    # 暂存后的试运行：复用 scan_from_json，不再扫描；量内存增量
    with _Peak() as peak:
        resp, trial_s = await _timed(client.post(f"/api/datasources/imports/{sid}/trial", json={}))
    assert resp.status_code == 200, resp.text
    st = resp.json()
    trial = st["trial"]
    assert trial["status"] == "passed", trial["problems"][:3]
    assert {t["name"]: t["rows"] for t in trial["receipt"]["tables"]} == {"销售明细": ROWS}
    rows.append(judge(record_property, f"{label}：暂存后的试运行", trial_s, trial_target))
    rows.append(judge(record_property, f"{label}：试运行的进程内存增量", peak.delta, 200.0, "MB"))
    resp = await client.post(f"/api/datasources/imports/{sid}/commit",
                             json={"trial_id": trial["trial_id"], "confirmations": _confirms(st)})
    assert resp.status_code == 201, resp.text
    source_id = resp.json()["source"]["id"]

    # 上传新一期：扫描 + 按现行配方自动试运行
    with _Peak() as peak:
        resp, re_s = await _timed(client.post(f"/api/datasources/{source_id}/reupload",
                                              files={"file": (fn2, second, XLSX)}))
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert st["trial"]["status"] == "passed", st["trial"]["problems"][:3]
    rows.append(judge(record_property, f"{label}：上传新一期（扫描 + 试运行）", re_s, reupload_target))
    rows.append(judge(record_property, f"{label}：上传新一期的进程内存增量", peak.delta, 200.0, "MB"))
    return rows


@pytest.mark.parametrize("dimension", [False, True], ids=["no_dimension", "with_dimension"])
async def test_perf_big_list_without_formulas(client, record_property, plain_files, dimension):
    label = f"20万行无公式（{'有' if dimension else '无'} dimension）"
    assert_all(await _big_flow(client, record_property, _variant(plain_files, dimension), label=label,
                               reupload_target=25.0, trial_target=18.0))


@pytest.mark.parametrize("dimension", [False, True], ids=["no_dimension", "with_dimension"])
async def test_perf_big_list_with_formula_column(client, record_property, formula_files, dimension):
    label = f"20万行整列公式（{'有' if dimension else '无'} dimension）"
    # 4.9 对整列公式的表只给了「上传新一期 ≤ 40 s」；暂存后的试运行没有单独的目标，只记录
    assert_all(await _big_flow(client, record_property, _variant(formula_files, dimension), label=label,
                               reupload_target=40.0, trial_target=None))


# --------------------------------------------------------------------------
# 结构仿照客流表的夹具
# --------------------------------------------------------------------------


async def test_perf_flow_fixture(client, record_property):
    raw, fn = flow_workbook(dt.date(2026, 8, 1), 31, seed=5)
    name = f"perf_{uuid.uuid4().hex[:8]}"
    resp, stage_s = await _timed(client.post("/api/datasources/imports/stage",
                                             files={"file": (fn, raw, XLSX)}, data={"name": name}))
    assert resp.status_code == 201, resp.text
    st = resp.json()
    sid = st["id"]
    rows = [judge(record_property, "客流夹具：暂存", stage_s, 1.0)]
    q = st["questions"][0]
    resp, ans_s = await _timed(client.post(f"/api/datasources/imports/{sid}/answers", json={"answers": {
        q["id"]: {"value": q["options"][0]["value"], "reason": "合成理由"}}}))
    assert resp.status_code == 200, resp.text
    rows.append(judge(record_property, "客流夹具：单次回答", ans_s, 0.5))
    resp, put_s = await _timed(client.put(f"/api/datasources/imports/{sid}/recipe", json={"recipe": FLOW_RECIPE}))
    assert resp.status_code == 200, resp.text
    rows.append(judge(record_property, "客流夹具：单次改配方", put_s, 0.5))
    resp, trial_s = await _timed(client.post(f"/api/datasources/imports/{sid}/trial", json={}))
    assert resp.status_code == 200, resp.text
    assert resp.json()["trial"]["status"] == "passed"
    rows.append(judge(record_property, "客流夹具：试运行", trial_s, 1.0))
    assert_all(rows)


def teardown_module(_module) -> None:
    if RESULTS:
        print("\n[perf] 汇总：")
        for row in RESULTS:
            target = f"目标 {row['target']} {row['unit']}，{row['ratio']} 倍" if row["target"] is not None else "无目标"
            print(f"[perf]   {row['item']}: {row['value']} {row['unit']}（{target}，{row['verdict']}）")


# --------------------------------------------------------------------------
# 期 3（WP-8，P3-SPEC 11.5）：按期累积的物化、累积试运行、修复与框选的预览
# --------------------------------------------------------------------------

#: 12 期 × 20 万行 × 10 列的合成表（同 p3-probe/probe_perf.py 的结构：每期 30 天 × 6667 个项目，主键 (日期, 项目)）
UNION_PERIODS = 12
UNION_COLS = ["日期", "项目", "类别"] + [f"值{i}" for i in range(7)]


def _union_target() -> Any:
    """物化的目标配方：一张 10 列的表，主键 (日期, 项目)。用列表块写出这个表结构（交叉表块一段只有一个值列，造不出
    10 列 × 20 万行）；物化只看 derive_tables 推出的表结构，与块的形态无关。列表表没有日期轴，U3 不跑（U3 每期每表
    一条带 rowid 区间的 COUNT，量级可以忽略）。"""
    from app.data.recipe_types import Recipe

    return Recipe.model_validate({
        "recipe_format": "agentlab-recipe/2",
        "sheets": [{"id": "s1", "match": {"name": SHEET}, "blocks": [{
            "id": "列表1", "layout": "list", "table": "明细",
            "columns": [{"header": c, "name": c, "type": "DATE" if c == "日期" else "TEXT" if i < 3 else "INTEGER"}
                        for i, c in enumerate(UNION_COLS)]}]}],
        "tables": [{"name": "明细", "grain": ["日期", "项目"]}],
    })


def _union_part_db(path: Path, start: dt.date, seed: int) -> None:
    """一期的构建库：执行器建的那种 STRICT 表，rowid 从 1 连续（物化前要断言）。"""
    import sqlite3

    rnd = random.Random(seed)
    per = ROWS // 30
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute('CREATE TABLE "明细" ("日期" TEXT NOT NULL, "项目" TEXT NOT NULL, "类别" TEXT, '
                 + ", ".join(f'"值{i}" INTEGER' for i in range(7)) + ', PRIMARY KEY ("日期", "项目")) STRICT')
    rows = []
    for d in range(30):
        day = (start + dt.timedelta(days=d)).isoformat()
        for k in range(per):
            rows.append((day, f"项目{k:05d}", "甲" if k % 2 else "乙", *[rnd.randint(0, 99999) for _ in range(7)]))
    conn.executemany(f'INSERT INTO "明细" VALUES ({", ".join("?" * 10)})', rows)
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def union_parts(tmp_path_factory) -> list[Any]:
    from app.data import raw_store
    from app.data.recipe_types import UnionPart

    target = _union_target()
    root = tmp_path_factory.mktemp("union_parts")
    parts = []
    for m in range(UNION_PERIODS):
        start = dt.date(2025, 1, 1) + dt.timedelta(days=31 * m)
        path = root / f"p{m:02d}.db"
        _union_part_db(path, start, seed=700 + m)
        end = start + dt.timedelta(days=29)
        parts.append(UnionPart(import_id=f"imp{m:02d}", start=start.isoformat(), end=end.isoformat(),
                               db_path=str(path), recipe=target, db_sha256=raw_store.sha256_file(path),
                               table_hashes={"明细": f"{m:064x}"}))
    return parts


def test_perf_materialize_twelve_periods_of_two_hundred_thousand_rows(record_property, union_parts, tmp_path,
                                                                      monkeypatch):
    """11.5：12 期 × 20 万行的累积物化（各期哈希核对 + INSERT…SELECT + U1/U2 + 空值数 + 组合表哈希 + 库哈希）≤ 8 s；
    其中表哈希这一步（union/1 的组合定义，不逐行重算）≤ 0.2 s。进程内存增量只记录。"""
    from app.data import recipe_accumulate as ra

    hashing = {"s": 0.0}
    real_sha = ra.sha_json

    def timed_sha(obj):
        t0 = time.perf_counter()
        try:
            return real_sha(obj)
        finally:
            hashing["s"] += time.perf_counter() - t0

    monkeypatch.setattr(ra, "sha_json", timed_sha)
    out = tmp_path / "union.db"
    with _Peak() as peak:
        t0 = time.perf_counter()
        report = ra.materialize_union(out, target=_union_target(), parts=union_parts)
        elapsed = time.perf_counter() - t0
    assert report.ok, [c for c in report.checks if c.status != "passed"]
    assert report.rows == {"明细": UNION_PERIODS * (ROWS // 30) * 30}
    rows = [judge(record_property, "期 3：12 期 × 20 万行累积物化（含核对、哈希）", elapsed, 8.0),
            judge(record_property, "期 3：其中组合表哈希", hashing["s"], 0.2),
            judge(record_property, "期 3：物化的进程内存增量", peak.delta, None, "MB"),
            judge(record_property, "期 3：并集库大小", out.stat().st_size / 1e6, None, "MB")]
    assert_all(rows)


async def _accumulate_first(client, seed: int) -> str:
    """参考夹具 8 月按期累积首次导入（回答问题、宽表改名、提交），返回数据源 id。"""
    from tests.test_recipe_acceptance_p3 import answers_for, rename_wide

    raw, fn = flow_workbook(dt.date(2026, 8, 1), 31, seed=seed)
    resp = await client.post("/api/datasources/imports/stage", files={"file": (fn, raw, XLSX)},
                             data={"name": f"perf_{uuid.uuid4().hex[:8]}"})
    st = resp.json()
    st = (await client.post(f"/api/datasources/imports/{st['id']}/answers",
                            json={"answers": answers_for(st["questions"], "accumulate")})).json()
    wide = next(t["name"] for t in st["recipe"]["tables"] if t["name"].endswith("_按日"))
    st = (await client.put(f"/api/datasources/imports/{st['id']}/recipe",
                           json={"recipe": rename_wide(st["recipe"], wide, "日客流")})).json()
    st = (await client.post(f"/api/datasources/imports/{st['id']}/trial", json={})).json()
    resp = await client.post(f"/api/datasources/imports/{st['id']}/commit",
                             json={"trial_id": st["trial"]["trial_id"], "confirmations": _confirms(st)})
    assert resp.status_code == 201, resp.text
    return resp.json()["source"]["id"]


async def test_perf_flow_fixture_accumulate_trial_and_previews(client, record_property):
    """11.5：参考夹具 8 月加 9 月，试运行（含物化）≤ 1.5 s；交互路径：修复预览（D04 去掉「7-8」）、框选预览（c01 框为
    列表；参考夹具框为交叉表整块，要对整张表跑规则起草）≤ 1 s。上传新一期（扫描 + 试运行 + 物化）只记录。"""
    from tests.fixtures.xlsx import lab
    from tests.fixtures.xlsx.drift import DRIFT_CASES

    source_id = await _accumulate_first(client, seed=31)
    raw, fn = flow_workbook(dt.date(2026, 9, 1), 30, seed=32)
    resp, up_s = await _timed(client.post(f"/api/datasources/{source_id}/reupload",
                                          files={"file": (fn, raw, XLSX)}))
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert st["trial"]["status"] == "passed" and st["trial"]["accumulate"]["action"] == "append"
    rows = [judge(record_property, "期 3：客流夹具 8+9 月上传新一期（扫描 + 试运行 + 物化）", up_s, None)]
    resp, trial_s = await _timed(client.post(f"/api/datasources/imports/{st['id']}/trial", json={}))
    assert resp.status_code == 200, resp.text
    assert resp.json()["trial"]["accumulate"]["union"]["rows"] == {"日客流": 61, "时段客流": 1037,
                                                                   "时段客流_表内合计": 183}
    rows.append(judge(record_property, "期 3：客流夹具 8+9 月试运行（含物化）", trial_s, 1.5))
    await client.delete(f"/api/datasources/imports/{st['id']}")

    raw, fn = DRIFT_CASES["D04"].build(33)
    st = (await client.post(f"/api/datasources/{source_id}/reupload", files={"file": (fn, raw, XLSX)})).json()
    fx = next(f for f in st["fixes"] if f["kind"] == "remove_label")
    resp, fix_s = await _timed(client.post(f"/api/datasources/imports/{st['id']}/edits/preview",
                                           json={"fix": {"id": fx["id"], "option": "remove"}}))
    assert resp.status_code == 200 and resp.json()["ok"], resp.text
    rows.append(judge(record_property, "期 3：修复预览（D04 去掉标签）", fix_s, 1.0))
    sel = {"sheet": "客流汇总", "ref": "B4:AG30", "as": "crosstab", "options": {}}
    resp, cross_s = await _timed(client.post(f"/api/datasources/imports/{st['id']}/edits/preview",
                                             json={"selection": sel}))
    assert resp.status_code == 200, resp.text
    rows.append(judge(record_property, "期 3：框选预览（客流夹具框为交叉表整块）", cross_s, 1.0))

    raw, fn = lab.c01("literal")
    st = (await client.post("/api/datasources/imports/stage", files={"file": (fn, raw, XLSX)},
                            data={"name": f"perf_{uuid.uuid4().hex[:8]}"})).json()
    sel = {"sheet": "月报", "ref": "C5:F8", "as": "list", "options": {"header_rows": 1, "bottom": "box"}}
    resp, list_s = await _timed(client.post(f"/api/datasources/imports/{st['id']}/edits/preview",
                                            json={"selection": sel}))
    assert resp.status_code == 200 and resp.json()["ok"], resp.text
    rows.append(judge(record_property, "期 3：框选预览（c01 框为列表）", list_s, 1.0))
    assert_all(rows)
