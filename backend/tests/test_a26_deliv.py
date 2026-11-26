"""四期 A26 交付件页：写签分离（对客户交付件起草人、最后修改人不能签，D3 豁免）、D3 核对与签署合并与批量签、
机械问题一键修订（最多 2 轮，超出转人工）、客户意见回路（录入 → 采纳 → 重开 → 自动修订稿）。虚拟客户，模型打桩。"""
import json
import secrets

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Confirmation, Deliverable, Project
from app.project import quality, revise
from app.seed import DELIVERABLES
from app.services import gateway, jobs

from .conftest import create_user, logged_in_client


def _review_text(rows: list[tuple[str, str, str, str]]) -> str:
    n = {s: sum(1 for r in rows if r[2] == s) for s in ("阻断", "重要", "建议")}
    table = "".join(f"| {i} | {loc} | {dim} | {sev} | {prob} | 修改 |\n" for i, (loc, dim, sev, prob) in enumerate(rows, 1))
    return ("# 评审意见\n- 评审结论：通过\n"
            f"- 统计：阻断 {n['阻断']} 项，重要 {n['重要']} 项，建议 {n['建议']} 项\n\n"
            "| # | 位置 | 维度 | 严重度 | 问题 | 修改建议 |\n|---|---|---|---|---|---|\n" + table)


CLEAN = _review_text([])
MECH = _review_text([("第 1 章 现状概述", "表达口径", "重要", "术语前后不统一")])
BODY1 = "SOC 现有 6 人，全部白班，交接记录为纸质 [O-01]。"
BODY2 = "夜间无人值守，与 7×24 监测要求存在差距 [O-01]；建议引入托管夜间初筛 [推断]。"


class _Model:
    """评审请求回 review（可按次序给列表），修订请求（带“修改清单”）回改过的正文。"""

    def __init__(self):
        self.review: list[str] = [CLEAN]
        self.revise_prompts: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)["messages"][-1]["content"]
        if "# 修改清单" in msg:
            self.revise_prompts.append(msg)
            text = f"SOC 现有 6 人，全部白班，交接记录为纸质（修订 {len(self.revise_prompts)}）[O-01]。"
        else:
            text = self.review[0] if len(self.review) == 1 else self.review.pop(0)
        return httpx.Response(200, json={"model": "mock-model", "choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


@pytest.fixture()
def model(monkeypatch):
    m = _Model()
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
    monkeypatch.setattr(get_settings(), "auto_flow", False)
    gateway.set_transport(httpx.MockTransport(m.handler))
    yield m
    gateway.set_transport(None)


def _new_project() -> str:
    code = f"A26-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    return code


@pytest.fixture()
def team():
    """顾问（成员）、项目经理 A、项目经理 B。"""
    code = _new_project()
    users = [create_user(("consultant",), {code: role}) for role in ("member", "manager", "manager")]
    clients = [logged_in_client(app, u) for u in users]
    yield code, [u[0] for u in users], *clients
    for c in clients:
        c.__exit__(None, None, None)


def _url(code: str, dcode: str, tail: str) -> str:
    return f"/api/projects/{code}/deliverables/{dcode}/{tail}"


def _work() -> None:
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=[quality.JOB_KIND, revise.JOB_KIND]):
            pass


def _draft(c, code: str, dcode: str = "D4") -> None:
    assert c.post(f"/api/projects/{code}/notes", json={"kind": "observation", "title": "值班室巡查",
                                                       "content": "值班台交接记录为纸质。"}).status_code == 201
    assert c.put(_url(code, dcode, "outline"), json={"titles": ["现状概述", "主要差距"]}).status_code == 200
    for i, b in enumerate((BODY1, BODY2), 1):
        assert c.put(_url(code, dcode, f"sections/{i}"), json={"body": b}).status_code == 201


def _review(c, code: str, dcode: str = "D4") -> None:
    assert c.post(_url(code, dcode, "reviews")).status_code == 202
    _work()


def _audit(code: str, action: str) -> list[dict]:
    with SessionLocal() as db:
        return [json.loads(x.detail) if isinstance(x.detail, str) else x.detail for x in db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                                     AuditLog.action == action))]


# ---- 写签分离 ----

def test_drafter_cannot_sign_client_deliverable(team, model):
    code, (_, name_a, _), member, mgr_a, mgr_b = team
    _draft(mgr_a, code)
    _review(mgr_a, code)
    assert mgr_a.post(_url(code, "D4", "signoff/submit"), json={}).status_code == 200
    st = mgr_a.get(_url(code, "D4", "signoff")).json()
    assert st["drafters"] == [name_a] and st["last_modifiers"] == [name_a] and not st["separation_exempt"]
    assert "写签分离" in st["sign_block"] and "起草人和最后修改人" in st["sign_block"]
    assert mgr_b.get(_url(code, "D4", "signoff")).json()["sign_block"] == ""
    r = mgr_a.post(_url(code, "D4", "signoff/sign"), json={})
    assert r.status_code == 403 and "写签分离" in r.json()["detail"]
    assert _audit(code, "deliverable.sign")[-1]["reason"] == "write_sign_separation"
    r = mgr_b.post(_url(code, "D4", "signoff/sign"), json={"comment": "同意"})
    assert r.status_code == 200 and r.json()["status"] == "signed"


def test_last_modifier_cannot_sign(team, model):
    code, _, member, mgr_a, mgr_b = team
    _draft(member, code)
    assert mgr_b.put(_url(code, "D4", "sections/2"), json={"body": BODY2 + "（经理补一句）"}).status_code == 201
    _review(member, code)
    assert member.post(_url(code, "D4", "signoff/submit"), json={}).status_code == 200
    r = mgr_b.post(_url(code, "D4", "signoff/sign"), json={})
    assert r.status_code == 403 and "最后修改人" in r.json()["detail"]
    assert mgr_a.post(_url(code, "D4", "signoff/sign"), json={}).status_code == 200


# ---- D3：写签分离豁免、核对与签署合并、批量签 ----

def test_d3_merged_and_batch_sign(team, model):
    code, _, member, mgr_a, mgr_b = team
    other = _new_project()
    _draft(mgr_a, code, "D3")
    _review(mgr_a, code, "D3")
    st = mgr_a.get(_url(code, "D3", "signoff")).json()
    assert st["separation_exempt"] and st["sign_block"] == "" and st["merged"] and st["merged_ready"]
    assert member.post(_url(code, "D3", "signoff/merged")).status_code == 403          # 只有项目经理
    assert mgr_a.post(_url(code, "D4", "signoff/merged")).status_code == 409           # D4 不走合并
    cands = mgr_a.get("/api/deliverables/batch-sign").json()
    assert [(x["project"], x["deliverable"], x["ready"]) for x in cands] == [(code, "D3", True)]
    r = mgr_a.post("/api/deliverables/batch-sign", json={"items": [{"project": code}, {"project": other}]})
    assert r.status_code == 200
    res = r.json()
    assert res[0]["ok"] and not res[1]["ok"] and "不存在" in res[1]["reason"]           # 非本人项目不泄露
    st = mgr_a.get(_url(code, "D3", "signoff")).json()
    assert st["status"] == "signed" and [h["action"] for h in st["history"]] == ["submit", "sign"]
    assert {a.get("trigger") for a in _audit(code, "deliverable.sign") if "reason" not in a} == {quality.BATCH}
    assert {a.get("trigger") for a in _audit(code, "deliverable.submit")} == {quality.BATCH}
    assert mgr_a.get("/api/deliverables/batch-sign").json() == []


def test_d3_with_issues_goes_back_to_itemized_check(team, model):
    code, _, member, mgr_a, _ = team
    model.review = [MECH]
    _draft(mgr_a, code, "D3")
    _review(mgr_a, code, "D3")
    st = mgr_a.get(_url(code, "D3", "signoff")).json()
    assert st["gate"] and not st["merged_ready"]
    r = mgr_a.post(_url(code, "D3", "signoff/merged"))
    assert r.status_code == 409 and any("逐条核对" in x for x in r.json()["detail"]["reasons"])
    assert mgr_a.get("/api/deliverables/batch-sign").json()[0]["ready"] is False


# ---- 一键修订 ----

def test_revise_two_rounds_then_handoff(team, model):
    code, _, member, *_ = team
    model.review = [MECH]                                   # 每轮复查都还有机械问题
    _draft(member, code)
    _review(member, code)
    st = member.get(_url(code, "D4", "revise")).json()
    assert st["mechanical_open"] == 1 and st["max_rounds"] == 2 and st["status"] == ""
    r = member.post(_url(code, "D4", "revise"))
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert member.post(_url(code, "D4", "revise")).status_code == 409                    # 不重复排
    _work()
    assert len(model.revise_prompts) == 2 and "术语前后不统一" in model.revise_prompts[0]
    st = member.get(_url(code, "D4", "revise")).json()
    assert st["status"] == "done" and st["handoff"] and st["mechanical_open"] == 1
    s1 = member.get(_url(code, "D4", "sections/1")).json()
    assert s1["version"] == 3 and "一键修订第 2 轮" in s1["note"]
    done = _audit(code, "deliverable.revise")[-1]
    assert (done["rounds"], done["written"], done["handoff"]) == (2, 2, True)
    assert "术语" not in json.dumps(_audit(code, "deliverable.revise"), ensure_ascii=False)


def test_revise_stops_when_clean_and_round_limit_config(team, model, monkeypatch):
    code, _, member, *_ = team
    model.review = [MECH, CLEAN]                             # 首评有问题，修一轮后复查通过
    _draft(member, code)
    _review(member, code)
    member.post(_url(code, "D4", "revise"))
    _work()
    st = member.get(_url(code, "D4", "revise")).json()
    assert len(model.revise_prompts) == 1 and not st["handoff"] and st["mechanical_open"] == 0
    monkeypatch.setattr(get_settings(), "revise_max_rounds", 0)                         # 轮数是配置
    model.review = [MECH]
    _review(member, code)
    member.post(_url(code, "D4", "revise"))
    _work()
    assert len(model.revise_prompts) == 1 and member.get(_url(code, "D4", "revise")).json()["handoff"]


def test_mechanical_excludes_human_decisions():
    assert revise.mechanical({"severity": "重要", "dimension": "表达", "problem": "口径不一"})
    assert revise.mechanical({"severity": "阻断", "dimension": "范围与资质边界", "problem": "出现测评 / 认证结论用语 1 处"})
    assert not revise.mechanical({"severity": "阻断", "dimension": "保密", "problem": "命中机密级检测（口令）"})
    assert not revise.mechanical({"severity": "阻断", "dimension": "与已确认事项矛盾", "problem": "表达与确认卡矛盾"})
    assert not revise.mechanical({"severity": "建议", "dimension": "表达", "problem": "语句冗长"})
    assert not revise.mechanical({"severity": "重要", "dimension": "表达", "problem": "x", "status": "已驳回"})


def test_revise_refused_when_signed_without_feedback(team, model):
    code, _, member, mgr_a, _ = team
    _draft(member, code)
    _review(member, code)
    member.post(_url(code, "D4", "signoff/submit"), json={})
    assert mgr_a.post(_url(code, "D4", "signoff/sign"), json={}).status_code == 200
    r = member.post(_url(code, "D4", "revise"))
    assert r.status_code == 409 and "录入客户意见" in r.json()["detail"]


# ---- 客户意见回路 ----

def test_client_feedback_reopens_and_revises(team, model):
    code, _, member, mgr_a, _ = team
    _draft(member, code)
    _review(member, code)
    member.post(_url(code, "D4", "signoff/submit"), json={})
    assert mgr_a.post(_url(code, "D4", "signoff/sign"), json={}).status_code == 200

    r = member.post(_url(code, "D4", "client-feedback"), json={"items": [{"section": 1, "text": "口令 Abc@12345"}]})
    assert r.status_code == 422 and r.json()["detail"]["blocked"]                       # 机密级不收
    assert member.post(_url(code, "D4", "client-feedback"),
                       json={"items": [{"section": 9, "text": "第九章"}]}).status_code == 422
    r = member.post(_url(code, "D4", "client-feedback"),
                    json={"items": [{"section": 1, "text": "BANK-A 希望写明白班人数口径"}, {"text": "整体语气再平和些"}]})
    assert r.status_code == 201
    c1, c2 = r.json()["confirmations"]
    with SessionLocal() as db:
        card = db.get(Confirmation, c1)
        assert (card.kind, card.deliverable_code, card.section_seq, card.recommendation) == ("客户意见", "D4", 1, "A")

    assert member.post(f"/api/confirmations/{c1}/resolve", json={"choice": "A"}).status_code == 200
    assert member.get(_url(code, "D4", "signoff")).json()["status"] == "signed"          # 还有未处理的意见，先不动
    assert member.post(f"/api/confirmations/{c2}/resolve", json={"choice": "B"}).status_code == 200
    st = member.get(_url(code, "D4", "signoff")).json()
    assert st["status"] == "drafting" and st["history"][-1]["action"] == "reopen"
    _work()
    assert len(model.revise_prompts) == 1 and "白班人数口径" in model.revise_prompts[0]
    assert "整体语气" not in model.revise_prompts[0]                                    # 不采纳的不改
    assert member.get(_url(code, "D4", "sections/1")).json()["version"] == 2
    rv = member.get(_url(code, "D4", "revise")).json()
    assert rv["pending_feedback"] == 0 and rv["trigger"] == revise.TRIGGER_FEEDBACK
    dump = json.dumps([*_audit(code, "confirmation.create"), *_audit(code, "deliverable.revise")], ensure_ascii=False)
    assert "白班人数" not in dump and "语气" not in dump
    # 重新评审后重签（签署人不是起草人）
    assert member.post(_url(code, "D4", "signoff/submit"), json={}).status_code == 200
    r = mgr_a.post(_url(code, "D4", "signoff/sign"), json={})
    assert r.status_code == 200 and [h["action"] for h in r.json()["history"]] == ["submit", "sign", "reopen",
                                                                                   "submit", "sign"]
