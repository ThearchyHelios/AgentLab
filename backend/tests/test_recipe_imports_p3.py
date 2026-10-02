"""按配方导入期 3 的状态机（WP-5，P3-SPEC 3.1、3.3、第 8 节遗留项 2、12.6）：用假 Pipeline 测。

假管线沿用 test_recipe_imports 的（合成的扫描、网格、Extraction），只换上期 3 的几个接缝：propose_fixes、fix_ops
用桩，补丁照真的 recipe_fixes.finish 算哈希（与暂存区同一口径）。真管线的流程在 test_table_imports_api_p3.py。
"""
from __future__ import annotations

import copy
import dataclasses

from app.data import recipe, recipe_imports, table_versions
from app.data.recipe_fixes import EditRequestError, finish
from app.data.recipe_imports import Pipeline
from app.data.recipe_types import FixAnchor, FixOption, FixProposal, Problem, Question
from app.db.models import ImportStaging, TableImport
from tests.test_recipe_imports import (  # noqa: F401 - client、fake、store 是夹具
    FLOW_FULL, FakeState, client, committed, fake, fake_pipeline, get, recipe_source, staged, store, trialed,
)

API = "/api/datasources"


# ==========================================================================
# 接缝
# ==========================================================================


def test_pipeline_new_fields_default_to_none_and_default_pipeline_wires_them():
    """期 2 测试里全关键字构造的假 Pipeline 照旧成立：期 3 的接缝都有默认值 None（12.6）。"""
    new = ("propose_fixes", "fix_ops", "selection_edit", "replay_compare", "align_to_contract", "compare_recipes",
           "plan_accumulate", "classify_changes", "materialize_union", "build_union_notes")
    fields = {f.name: f for f in dataclasses.fields(Pipeline)}
    assert all(fields[n].default is None for n in new)
    real = recipe_imports.default_pipeline()
    assert all(callable(getattr(real, n)) for n in new)
    assert fake_pipeline(FakeState()).propose_fixes is None


# ==========================================================================
# 遗留项 2：carry_answers
# ==========================================================================


def test_carry_answers_keeps_embodied_answers_and_drops_changed_and_gone():
    p = recipe_imports.default_pipeline()
    base = copy.deepcopy(FLOW_FULL)
    old_q = [Question("q_a", "问题甲", [{"value": "x"}, {"value": "y"}], None,
                      {"x": [], "y": [{"op": "add", "path": "/tables/0/note", "value": "乙"}]}),
             Question("q_gone", "已经没有的问题", [{"value": "x"}], None, {"x": []})]
    new_q = [old_q[0], Question("q_b", "问题乙", [{"value": "z"}], None,
                                {"z": [{"op": "add", "path": "/tables/0/note", "value": "丙"}]})]
    kept, dropped = recipe_imports.carry_answers(
        p, base, new_q, old_q, {"q_a": {"value": "x"}, "q_gone": {"value": "x"}, "q_b": {"value": "z"}})
    assert kept == {"q_a": {"value": "x", "reason": None}}
    assert {d["id"]: (d["reason"], d["text"]) for d in dropped} == {"q_gone": ("gone", "已经没有的问题"),
                                                                    "q_b": ("changed", "问题乙")}
    # 回答的效果已经体现在新起点上：保留
    embodied = copy.deepcopy(base)
    embodied["tables"][0]["note"] = "乙"
    kept, dropped = recipe_imports.carry_answers(p, embodied, new_q, old_q, {"q_a": {"value": "y"}})
    assert kept == {"q_a": {"value": "y", "reason": None}} and dropped == []
    # 选项值在新问题里不存在：changed
    kept, dropped = recipe_imports.carry_answers(p, base, new_q, old_q, {"q_a": {"value": "w"}})
    assert kept == {} and dropped[0]["reason"] == "changed"
    # reapply（撤销修改时，修改之后才做的回答）：恢复的起点没体现它，effects 能干净地应用就保留
    kept, dropped = recipe_imports.carry_answers(p, base, new_q, old_q, {"q_b": {"value": "z"}}, reapply={"q_b"})
    assert kept == {"q_b": {"value": "z", "reason": None}} and dropped == []
    bad = [Question("q_c", "问题丙", [{"value": "z"}], None,
                    {"z": [{"op": "replace", "path": "/no_such_field/0", "value": 1}]})]
    kept, dropped = recipe_imports.carry_answers(p, base, bad, bad, {"q_c": {"value": "z"}}, reapply={"q_c"})
    assert kept == {} and dropped == [{"id": "q_c", "text": "问题丙", "reason": "changed"}]


def test_stored_origin_keeps_rules_redraft_out_of_the_recipe_record():
    """配方记录、导入清单只记 rules / ai / manual / mixed（界面按 RECIPE_ORIGIN_LABEL 显示）。rules_redraft 只管本暂存区
    的确认清单：没再改过记 rules，采用之后又有未被覆盖的修改记 manual；给暂存区赋初值（没有暂存区）按没改过算。"""
    st = ImportStaging(edits=[])
    assert recipe_imports.stored_origin("rules_redraft", st) == "rules"
    st.edits = [{"seq": 1, "superseded": True}]
    assert recipe_imports.stored_origin("rules_redraft", st) == "rules", "采用之前的修改已被覆盖"
    st.edits = [{"seq": 1, "superseded": True}, {"seq": 2, "superseded": False}]
    assert recipe_imports.stored_origin("rules_redraft", st) == "manual"
    assert recipe_imports.stored_origin("rules_redraft") == "rules"
    for origin in recipe_imports.STORED_ORIGINS:
        assert recipe_imports.stored_origin(origin, st) == origin
    assert recipe_imports.stored_origin(None) == "manual"


# ==========================================================================
# 3.3：base 一律取数据源当前的现行配方
# ==========================================================================


async def test_validate_gets_the_current_recipe_as_base_only_for_reupload(client, fake):
    await staged(client)
    assert all("base" not in c for c in fake.calls["validate"]), "首次导入没有现行配方，不传 base（12.0）"
    done = await recipe_source(client)
    source_id = done["source"]["id"]
    st = (await client.post(f"{API}/{source_id}/reupload", files={"file": ("x.xlsx", b"PK p3 base", "x")})).json()
    # 现行配方原样重放按 replay 校验（不查认领，也就用不着 base）；工作配方改过之后带系统发现校验，base 是现行配方
    fake.calls["validate"].clear()
    changed = copy.deepcopy(FLOW_FULL)
    changed["tables"][0]["note"] = "改过的说明"
    resp = await client.put(f"{API}/imports/{st['id']}/recipe", json={"recipe": changed})
    assert resp.status_code == 200, resp.text
    with_base = [c for c in fake.calls["validate"] if "base" in c]
    ref = recipe.recipe_sha256(recipe.Recipe.model_validate(FLOW_FULL))
    assert with_base and all(recipe.recipe_sha256(recipe.Recipe.model_validate(c["base"])) == ref for c in with_base)


async def test_base_follows_the_current_recipe_not_the_one_at_creation(client, fake):
    """评审一-m7：暂存区 A 创建时现行配方是 r1；之后别的提交把现行换成 r2。A 再评估时 base 取 r2（三处一致）。"""
    done = await recipe_source(client)
    source_id = done["source"]["id"]
    a = (await client.post(f"{API}/{source_id}/reupload", files={"file": ("a.xlsx", b"PK p3 a", "x")})).json()
    b = (await client.post(f"{API}/{source_id}/reupload", files={"file": ("b.xlsx", b"PK p3 b", "x")})).json()
    changed = copy.deepcopy(FLOW_FULL)
    changed["tables"][0]["note"] = "第二版的说明"
    resp = await client.put(f"{API}/imports/{b['id']}/recipe", json={"recipe": changed})
    assert resp.status_code == 200, resp.text
    b = await trialed(client, b["id"])
    await committed(client, b)
    fake.calls["validate"].clear()
    resp = await client.post(f"{API}/imports/{a['id']}/answers", json={"answers": {}})
    assert resp.status_code == 200, resp.text
    bases = [c["base"] for c in fake.calls["validate"] if "base" in c]
    assert bases and all(x["tables"][0].get("note") == "第二版的说明" for x in bases)
    row = await get(ImportStaging, a["id"])
    assert row.base_recipe_id == done["recipe_id"], "base_recipe_id 只是创建时的记录，不改"


# ==========================================================================
# 3.1：提议、fix_ids、修改与撤销
# ==========================================================================


def _note_fix(state: FakeState, *, index: int = 0, needs_reason: bool = False) -> None:
    """给假管线装上修复接缝：任何问题都出一个「给第一张表加说明」的提议，锚在 problems 的第 index 条。"""

    def propose(rec, problems, recipe_problems, facts, renamed=None):
        state.calls["propose"].append({"problems": problems, "recipe_problems": recipe_problems, "renamed": renamed})
        if len(problems) <= index:
            return []
        return [FixProposal(id="fx-aaaaaaaaaaaa", kind="ignore_cells", problem_code=problems[index].get("code"),
                            title="合成提议", cells=[], target={"table": 0},
                            options=[FixOption("note", "加说明", needs_reason=needs_reason)],
                            anchor=FixAnchor("problem", index))]

    def fix_ops(rec, proposal, option, reason):
        if option != "note":
            raise EditRequestError("edit_invalid", "选项不在提议里")
        if needs_reason and not reason:
            raise EditRequestError("reason_required", "这个选项需要写明理由")
        text = f"合成说明：{reason}" if reason else "合成说明"
        return finish(rec, [{"op": "add", "path": "/tables/0/note", "value": text}], kind="fix",
                      key="ignore_cells:合成", title="合成修改", summary=["给第一张表加说明"])

    pipe = recipe_imports.pipeline()
    pipe.propose_fixes = propose
    pipe.fix_ops = fix_ops


async def test_fix_ids_follow_the_anchor_into_the_problem_they_belong_to(client, fake):
    fake.draft_problems = [Problem("label_unexpected", "structure", "第一条"), Problem("label_missing", "structure", "第二条")]
    _note_fix(fake, index=1)
    st = await staged(client)
    assert [p["fix_ids"] for p in st["draft_problems"]] == [[], ["fx-aaaaaaaaaaaa"]]
    assert st["fixes"][0]["anchor"] == {"kind": "problem", "index": 1, "sheet": None}
    assert all(p["fix_ids"] == [] for p in st["recipe_problems"])
    # 试运行拒收之后，提议按试运行的问题现算，fix_ids 写到 trial.problems 上
    fake.trial_problems = [Problem("x1", "structure", "甲"), Problem("x2", "structure", "乙")]
    st = await trialed(client, st["id"])
    assert st["trial"]["status"] == "rejected"
    assert [p["fix_ids"] for p in st["trial"]["problems"]] == [[], ["fx-aaaaaaaaaaaa"]]
    assert [p["fix_ids"] for p in st["draft_problems"]] == [[], []]


async def _apply_note(client, sid: str, body: dict) -> dict:
    pv = (await client.post(f"{API}/imports/{sid}/edits/preview", json=body)).json()
    assert pv["ok"], pv
    resp = await client.post(f"{API}/imports/{sid}/edits/apply",
                             json={"fix": body["fix"], "expected_sha256": pv["recipe_sha256_after"]})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_preview_apply_undo_and_put_supersede_the_edit_stack(client, fake):
    fake.draft_problems = [Problem("label_missing", "structure", "合成问题")]
    _note_fix(fake)
    st = await staged(client)
    resp = await client.post(f"{API}/imports/{st['id']}/answers",
                             json={"answers": {"q_relation:F1": {"value": "register"}}})
    st = resp.json()
    before = st["recipe_sha256"]
    body = {"fix": {"id": "fx-aaaaaaaaaaaa", "option": "note"}, "seq": 7}
    pv = (await client.post(f"{API}/imports/{st['id']}/edits/preview", json=body)).json()
    assert pv["ok"] and pv["seq"] == 7 and pv["recipe_sha256_before"] == before
    assert pv["breaking"] == {} and pv["compare"] is None and pv["accumulate_change"] is None
    assert (await get(ImportStaging, st["id"])).recipe_sha256 == before, "预览不改暂存区"
    resp = await client.post(f"{API}/imports/{st['id']}/edits/apply",
                             json={"fix": body["fix"], "expected_sha256": pv["recipe_sha256_after"]})
    assert resp.status_code == 200, resp.text
    st = resp.json()
    assert st["recipe"]["tables"][0]["note"] == "合成说明" and st["recipe_sha256"] == pv["recipe_sha256_after"]
    assert st["answers"] == {"q_relation:F1": {"value": "register", "reason": None}}, "修改之后回答照样延续"
    assert st["recipe_origin"] == "manual"
    row = await get(ImportStaging, st["id"])
    assert row.edits[0]["before"]["recipe_origin"] == "rules" and row.edits[0]["base_sha256_after"]
    # 修改记录带补丁和人选的选项、理由（3.1：进导入清单 edits）；StagingOut 不给补丁
    assert row.edits[0]["ops"] == [{"op": "add", "path": "/tables/0/note", "value": "合成说明"}]
    assert row.edits[0]["fix"] == {"id": "fx-aaaaaaaaaaaa", "option": "note", "reason": None}
    assert "ops" not in st["edits"][0] and "before" not in st["edits"][0]
    # 修改之后又回答过（改成不登记）：撤销只撤这次修改，修改之后的回答不跟着撤掉（3.1），工作配方 = 修改前的配方
    # 加上这次回答，与「不做修改、直接这样回答」的配方逐字相同
    resp = await client.post(f"{API}/imports/{st['id']}/answers",
                             json={"answers": {"q_relation:F1": {"value": "dismiss", "reason": "合成理由"}}})
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"{API}/imports/{st['id']}/edits/undo", json={})
    assert resp.status_code == 200, resp.text
    st = resp.json()
    assert st["edits"] == [] and st["recipe_origin"] == "rules"
    assert "note" not in st["recipe"]["tables"][0] or not st["recipe"]["tables"][0]["note"]
    assert st["answers"] == {"q_relation:F1": {"value": "dismiss", "reason": "合成理由"}}
    assert st["answers_dropped"] == []
    assert st["recipe"]["relations"][0]["kind"] == "dismissed"
    other = await staged(client)
    resp = await client.post(f"{API}/imports/{other['id']}/answers",
                             json={"answers": {"q_relation:F1": {"value": "dismiss", "reason": "合成理由"}}})
    dismissed_sha = resp.json()["recipe_sha256"]
    assert dismissed_sha != before and st["recipe_sha256"] == dismissed_sha
    # 先回答（有效果的「不登记」）、再修改、再撤销：修改前的回答原样保留，配方回到修改前（评审意见：撤销丢回答）
    st = await _apply_note(client, st["id"], body)
    assert st["answers"] == {"q_relation:F1": {"value": "dismiss", "reason": "合成理由"}}
    resp = await client.post(f"{API}/imports/{st['id']}/edits/undo", json={})
    assert resp.status_code == 200, resp.text
    st = resp.json()
    assert st["recipe_sha256"] == dismissed_sha and st["edits"] == []
    assert st["answers"] == {"q_relation:F1": {"value": "dismiss", "reason": "合成理由"}}
    assert st["answers_dropped"] == []
    # 再改一次，然后 PUT：之前的修改标「已被覆盖」，不能撤销
    pv = (await client.post(f"{API}/imports/{st['id']}/edits/preview", json=body)).json()
    resp = await client.post(f"{API}/imports/{st['id']}/edits/apply",
                             json={"fix": body["fix"], "expected_sha256": pv["recipe_sha256_after"]})
    st = resp.json()
    resp = await client.put(f"{API}/imports/{st['id']}/recipe", json={"recipe": st["recipe"]})
    st = resp.json()
    assert [(e["superseded"], e["undoable"]) for e in st["edits"]] == [(True, False)]
    resp = await client.post(f"{API}/imports/{st['id']}/edits/undo", json={})
    assert resp.status_code == 409 and resp.json()["code"] == "nothing_to_undo"
    # 被覆盖的修改不进确认项：确认清单只拿到未被覆盖的
    st = await trialed(client, st["id"])
    assert fake.calls["confirm_items"][-1].edits == []
    out = await committed(client, st)
    imp = await get(TableImport, out["import_id"])
    from app.core import artifact_store

    manifest = artifact_store.load(imp.manifest_artifact)
    assert [e["superseded"] for e in manifest["edits"]] == [True] and "before" not in manifest["edits"][0]


async def test_reason_changes_after_preview_are_edit_stale(client, fake):
    fake.draft_problems = [Problem("row_unclaimed", "structure", "合成问题")]
    _note_fix(fake, needs_reason=True)
    st = await staged(client)
    url = f"{API}/imports/{st['id']}/edits"
    resp = await client.post(f"{url}/preview", json={"fix": {"id": "fx-aaaaaaaaaaaa", "option": "note"}})
    assert resp.status_code == 422 and resp.json()["code"] == "reason_required"
    body = {"fix": {"id": "fx-aaaaaaaaaaaa", "option": "note", "reason": "甲"}}
    pv = (await client.post(f"{url}/preview", json=body)).json()
    resp = await client.post(f"{url}/apply", json={"fix": {**body["fix"], "reason": "乙"},
                                                   "expected_sha256": pv["recipe_sha256_after"]})
    assert resp.status_code == 409 and resp.json()["code"] == "edit_stale"
    resp = await client.post(f"{url}/apply", json={**body, "expected_sha256": pv["recipe_sha256_after"]})
    assert resp.status_code == 200 and resp.json()["recipe"]["tables"][0]["note"] == "合成说明：甲"


async def test_apply_on_a_closed_staging_and_unknown_fix(client, fake):
    fake.draft_problems = [Problem("label_missing", "structure", "合成问题")]
    _note_fix(fake)
    st = await staged(client)
    resp = await client.post(f"{API}/imports/{st['id']}/edits/preview",
                             json={"fix": {"id": "fx-bbbbbbbbbbbb", "option": "note"}})
    assert resp.status_code == 409 and resp.json()["code"] == "fix_stale"
    assert (await client.delete(f"{API}/imports/{st['id']}")).status_code == 204
    resp = await client.post(f"{API}/imports/{st['id']}/edits/preview",
                             json={"fix": {"id": "fx-aaaaaaaaaaaa", "option": "note"}})
    assert resp.status_code == 409 and resp.json()["code"] == "staging_closed"
    got = (await client.get(f"{API}/imports/{st['id']}")).json()
    assert got["fixes"] == [], "已结束的暂存区不再给提议"


async def test_trial_and_staging_out_carry_the_new_fields_and_hide_internal_ones(client, fake):
    """TrialOut 的新字段（没有累积计划时为 null）、StagingOut 的新字段；只给服务端的键（并集、上一期）不出去。
    修改记录、丢弃的回答、重新起草的哈希在暂存区结束时一并瘦身（STAGING_SLIM）。"""
    st = await trialed(client, (await staged(client))["id"])
    t = st["trial"]
    assert not set(recipe_imports.TRIAL_INTERNAL) & set(t)
    assert t["accumulate"] is None and t["union_checks"] is None and t["recipe_compare"] is None
    assert t["prior_acceptances"] == []
    assert st["fixes"] == [] and st["recipe_sha256"] and st["edits"] == [] and st["answers_dropped"] == []
    assert {"edits", "answers_dropped", "redraft_sha256"} <= set(table_versions.STAGING_SLIM)
