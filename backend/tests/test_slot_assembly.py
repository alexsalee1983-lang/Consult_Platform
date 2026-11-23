"""三期 A12 最小版：交付链按知识槽位装配。

- 产品配置的步骤可写 slots（槽位 id、条目类型、检索词、所需条数、是否必需），配置校验拦不合规的写法；
- 每个槽位按类型检索，结果按槽位分组附进系统提示，摘录按条目类型分额度截断，槽位取用情况记进运行记录（只记编号）；
- 必需槽位库里没有时发“知识不足”卡停在本步，处理卡后重跑本步、不再问，按现有知识起草；
- 没写 slots 的步骤与原来完全一样（快照不多出字段）。
"""
import secrets
from dataclasses import replace

from sqlalchemy import select

from app.agents import slots as slot_asm
from app.core.db import SessionLocal
from app.models.kb import KbItem
from app.models import AgentRunStep, AuditLog, Confirmation, Project, User
from app.products import config
from app.project import runs

from .conftest import create_user
from .test_chat_kb import _publish
from .test_runs import _note, _run, _work, model, proj  # noqa: F401（pytest 夹具）


def _check(raw):
    out: list[str] = []
    got = config.parse_slots(config._P("t", out), raw)
    return got, out


def test_slot_spec_validation():
    ok, errs = _check([{"id": "night-duty", "type": "MTD", "query": "夜间值守 做法", "need": 2, "required": True},
                       {"id": "tpl", "type": "TPL", "query": "SOC 设计说明 模板"}])
    assert not errs
    assert ok == ({"id": "night-duty", "type": "MTD", "query": "夜间值守 做法", "need": 2, "required": True},
                  {"id": "tpl", "type": "TPL", "query": "SOC 设计说明 模板", "need": 1, "required": False})
    assert _check(None) == ((), [])
    for bad in ([{"id": "a", "type": "XXX", "query": "q"}],                       # 类型不在 DOC_TYPES
                [{"id": "a", "type": "MTD", "query": "q", "need": 9}],            # 条数超上限
                [{"id": "a", "type": "MTD", "query": "q"}, {"id": "a", "type": "TPL", "query": "q"}],  # id 重复
                [{"id": "a b", "type": "MTD", "query": "q"}],                     # id 格式
                [{"id": "a", "type": "MTD"}],                                     # 缺检索词
                [{"id": "a", "type": "MTD", "query": "q", "required": "yes"}],
                "MTD"):
        assert _check(bad)[1], bad
    assert _check([{"id": f"s{i}", "type": "MTD", "query": "q"} for i in range(slot_asm.MAX_SLOTS + 1)])[1]


def test_snapshot_without_slots_is_unchanged():
    snap = config.compose_codes("SOC")
    assert all("slots" not in s for s in snap["chain"])
    assert all(s.slots == () for s in runs.chain_of(snap))


def _doc_type(db, doc_id: str) -> str:
    """按条目类型字段取类型（B15 起编号为 KB-000123，不再含类型段）。"""
    return db.scalar(select(KbItem.doc_type).where(KbItem.doc_id == doc_id))


def _ids():
    return {"tpl": _publish("槽位测试-设计说明模板", "## 模板\n\n" + "槽位模板正文。" * 120, doc_type="TPL"),
            "mtd": _publish("槽位测试-夜间值守做法", "## 夜间值守\n\n槽位夜间值守由托管方初筛。" * 40, doc_type="MTD")}


def test_assemble_groups_by_slot_and_budgets_by_type():
    _ids()
    spec = ({"id": "tpl", "type": "TPL", "query": "槽位测试 设计说明模板", "need": 1, "required": True},
            {"id": "night", "type": "MTD", "query": "槽位测试 夜间值守做法", "need": 1, "required": False},
            {"id": "pit", "type": "PIT", "query": "槽位测试 不存在的常见坑", "need": 1, "required": True})
    code = f"SLOT-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        db.add(Project(code=code, service="SOC 运营体系设计", industry="银行"))
        db.commit()
    name = create_user(("consultant",), {code: "member"})[0]
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        user = db.scalar(select(User).where(User.username == name))
        asm = slot_asm.assemble(db, user, "research-interview", p, spec)
    assert asm is not None
    by = {r["id"]: r for r in asm.report}
    for sid, t in (("tpl", "TPL"), ("night", "MTD")):     # 全量测试库里同类条目多，只核类型和条数
        assert len(by[sid]["got"]) == 1 and by[sid]["ok"]
        with SessionLocal() as kdb:
            assert _doc_type(kdb, by[sid]["got"][0]) == t
    ids = {"tpl": by["tpl"]["got"][0], "mtd": by["night"]["got"][0]}
    assert by["pit"]["got"] == [] and not by["pit"]["ok"]
    assert asm.missing_required == ["pit"]
    assert "## 槽位 tpl" in asm.block and ids["tpl"] in asm.block and "【依据待核实】" in asm.block
    lines = asm.block.splitlines()
    for doc_type, did in (("TPL", ids["tpl"]), ("MTD", ids["mtd"])):     # 每条摘录不超过该类型的额度
        i = next(n for n, x in enumerate(lines) if f"[{did}]" in x)
        assert lines[i + 1].startswith("  摘录：")
        assert len(lines[i + 1]) - len("  摘录：") <= slot_asm.budget(doc_type, 1)
    assert slot_asm.budget("TPL", 4) > slot_asm.budget("PIT", 4)          # 模板给得多，常见坑给得少
    assert slot_asm.budget("MTD", 6) < slot_asm.budget("MTD", 1)          # 同类型条目平分额度
    assert {r["doc_id"] for r in asm.refs} == set(ids.values())


def _with_slots(monkeypatch, spec):
    orig = runs.chain_of

    def patched(snap):
        return tuple(replace(s, slots=spec) if s.key == "facts" else s for s in orig(snap))
    monkeypatch.setattr(runs, "chain_of", patched)


def test_missing_required_slot_sends_card_then_resumes(proj, model, monkeypatch):  # noqa: F811
    _ids()
    _with_slots(monkeypatch, ({"id": "tpl", "type": "TPL", "query": "槽位测试 设计说明模板", "need": 1, "required": True},
                              {"id": "pit", "type": "PIT", "query": "槽位测试 不存在的常见坑", "need": 1,
                               "required": True}))
    code, c = proj
    _note(c, code)
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    run = _run(c, code, rid)
    assert run["status"] == "waiting" and run["next_step"] == "facts" and not model.requests
    card = run["waiting_cards"][0]
    with SessionLocal() as db:
        cf = db.get(Confirmation, card)
        assert cf.kind == "知识不足" and "pit" in cf.title
        rec = db.scalars(select(AgentRunStep).where(AgentRunStep.run_id == rid)).one()
        assert rec.status == "waiting" and {r["id"]: r["ok"] for r in rec.refs["slots"]} == {"tpl": True, "pit": False}
    assert c.post(f"/api/confirmations/{card}/resolve", json={"choice": "A"}).status_code == 200
    _work()
    run = _run(c, code, rid)
    assert run["status"] == "done", run
    system = model.requests[0]["messages"][0]["content"]
    assert "## 槽位 tpl" in system and "槽位 pit" in system
    with SessionLocal() as db:
        done = db.scalars(select(AgentRunStep).where(AgentRunStep.run_id == rid, AgentRunStep.step == "facts",
                                                     AgentRunStep.status == "done")).one()
        got = [r["got"] for r in done.refs["slots"]]
        assert len(got[0]) == 1 and got[1] == [] and got[0][0] in system        # 只记编号
        assert _doc_type(db, got[0][0]) == "TPL"
        n = db.scalar(select(Confirmation.id).where(Confirmation.kind == "知识不足",
                                                    Confirmation.project_id == db.scalar(
                                                        select(Project.id).where(Project.code == code))))
        assert n == card                                       # 只问一次
        logs = db.scalars(select(AuditLog).where(AuditLog.action == "run.step", AuditLog.project_code == code)).all()
        assert any("knowledge_missing" in lg.detail for lg in logs)
        assert not any("槽位模板正文" in str(lg.detail) for lg in logs)


def test_all_slots_present_runs_without_card(proj, model, monkeypatch):  # noqa: F811
    _ids()
    _with_slots(monkeypatch, ({"id": "tpl", "type": "TPL", "query": "槽位测试 设计说明模板", "need": 1,
                               "required": True},))
    code, c = proj
    _note(c, code)
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    assert _run(c, code, rid)["status"] == "done"
