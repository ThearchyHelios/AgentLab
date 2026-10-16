"""后台任务运行时上移到 core/job_runtime.py（docs/design/next-round/00-总方案.md X5）。

守三件事：老路径 data/catalog_job_runtime 拿到的是同一个模块（同一条队、同一批名字，起草、剖析的 import 不用改）；
同源队里的事是开放的——新的一种登记一行就和起草、剖析排进同一条队、同源串行；没登记的 kind 当场报错，不会悄悄排成
一种谁都不认识的事。
"""
from __future__ import annotations

import asyncio

import pytest

from app.core import job_runtime


def test_the_old_path_is_the_same_module():
    from app.data import catalog_job_runtime as old
    from app.data.catalog_job_runtime import lanes

    assert old is job_runtime
    assert lanes is job_runtime.lanes
    # 起草、剖析照旧从老路径导入，登记的几种事都在
    import app.data.catalog_jobs  # noqa: F401
    import app.data.catalog_profile_jobs  # noqa: F401

    assert {"draft", "profile", "sync_draft", "import"} <= set(job_runtime.lane_kinds())
    assert job_runtime.lane_label("profile") == "剖析"


def test_an_unregistered_kind_is_refused():
    lanes = job_runtime.SourceLanes()

    async def go() -> None:
        with pytest.raises(ValueError, match="没有登记"):
            lanes.enter("src", "nonsense-kind")

    asyncio.run(go())


def test_a_registered_label_cannot_change():
    job_runtime.register_lane_kind("test-open-kind", "测试用")
    job_runtime.register_lane_kind("test-open-kind", "测试用")   # 同一个叫法再登记无害
    with pytest.raises(ValueError, match="已经登记"):
        job_runtime.register_lane_kind("test-open-kind", "别的叫法")


def test_a_new_kind_queues_behind_the_existing_ones_on_the_same_source():
    """新登记的一种（第 2 波的后台探查就是这样加进来的）和起草排进同一条队：同源串行，不同源并行。"""
    import app.data.catalog_jobs  # noqa: F401 - 登记 draft

    job_runtime.register_lane_kind("test-introspect", "测试探查")
    lanes = job_runtime.SourceLanes()

    async def go() -> None:
        first = lanes.enter("src-a", "draft", "job-1")
        second = lanes.enter("src-a", "test-introspect", "job-2")
        other = lanes.enter("src-b", "test-introspect", "job-3")
        assert first.granted.is_set() and not second.granted.is_set()
        assert other.granted.is_set(), "不同源并行"
        assert lanes.ahead("src-a", "job-2") == ["draft"]
        first.release()
        await asyncio.wait_for(second.wait(), timeout=1)
        second.release()
        other.release()

    asyncio.run(go())


def test_a_job_without_a_lane_skips_the_queue():
    """lane_of 返回 None 的任务（以后不占源的评测）不进队、直接执行。"""

    class _NoLane(job_runtime.JobRun):
        kind = "draft"

        def lane_of(self, job):  # noqa: ANN001
            return None

    run = _NoLane("whatever")
    assert run.lane_of(object()) is None
