"""口径调用审计（app/ledger/usage.py）：谁在什么时候用了哪一版、带什么参数。

- 参数只记指纹和字段名，不记值；
- composed（问数据里 use_caliber）和 issued（按口径正式出具）各记一条 caliber.use；
- 同一组参数写法不同（键顺序、空值）指纹相同；参数不同指纹不同。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import AuditEvent
from app.ledger.usage import audit_composed_uses, params_fingerprint, record_use


def test_params_fingerprint_ignores_order_and_blanks_but_not_values():
    first, fields = params_fingerprint({"start": "2026-09-01", "end": "2026-09-08", "store": ""})
    second, _ = params_fingerprint({"end": "2026-09-08", "start": "2026-09-01", "store": None})
    assert first == second and fields == ["end", "start"]
    other, _ = params_fingerprint({"start": "2026-09-01", "end": "2026-09-07"})
    assert other != first


async def test_record_use_keeps_a_fingerprint_not_the_values():
    wf = f"usage{uuid.uuid4().hex[:10]}"
    async with SessionLocal() as session:
        await record_use(session, workflow_id=wf, version=3, how="issued", card="card", metrics=["gmv", "gmv"],
                         params={"start": "2026-09-01", "end": "2026-09-08"}, run_id="run-1", auto=True,
                         actor="王敏")
        await session.commit()
        [event] = (await session.execute(select(AuditEvent).where(
            AuditEvent.action == "caliber.use", AuditEvent.subject_id == f"{wf}@3"))).scalars().all()
    assert event.actor == "王敏" and event.subject_kind == "caliber"
    assert event.data["how"] == "issued" and event.data["metrics"] == ["gmv"]
    assert event.data["run_id"] == "run-1" and event.data["auto"] is True
    assert event.data["param_fields"] == ["end", "start"] and event.data["params_hash"]
    assert "2026-09-01" not in str(event.data) and "2026-09-08" not in str(event.data)


async def test_composed_uses_are_written_as_one_audit_each():
    wf = f"usage{uuid.uuid4().hex[:10]}"
    count = await audit_composed_uses([
        {"workflow_id": wf, "version": 1, "card": "card", "metrics": ["gmv"],
         "inputs": {"start": "2026-09-01", "end": "2026-09-08"}},
        {"workflow_id": wf, "version": 2, "card": "card2", "metrics": ["orders"], "inputs": {}},
    ], conversation_id="conv-1")
    assert count == 2
    async with SessionLocal() as session:
        rows = list((await session.execute(select(AuditEvent).where(
            AuditEvent.action == "caliber.use", AuditEvent.subject_kind == "caliber")
            .where(AuditEvent.subject_id.in_([f"{wf}@1", f"{wf}@2"])))).scalars())
    assert {r.subject_id for r in rows} == {f"{wf}@1", f"{wf}@2"}
    assert all(r.data["how"] == "composed" and r.data["conversation_id"] == "conv-1" for r in rows)
