"""M4 ④：严格档客户同意记录与去标识摘要卡（app/project/cards.py）。

没有有效同意不能上传；规则检查（机构名、人名、联系方式、网络标识、系统名、原话、机密级）任何一张不过整批拒收；
内容相同不重复；Agent 经 project.evidence 读摘要卡；撤下 / 撤回同意后内容清空、编号不复用；审计不记内容和文号。"""
import json
import secrets
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Project, User
from app.models.consent import SummaryCard
from app.project import cards, deletion

from .conftest import create_user, logged_in_client

TODAY = date.today().isoformat()
CONSENT = {"doc_ref": "数据处理计划-07", "signed_on": (date.today() - timedelta(days=3)).isoformat()}
GOOD = [
    {"item": "特权账号复核", "finding": "特权账号近 12 个月未复核，由【信息科技部负责人】口头确认。",
     "magnitude": "约 30 个", "onsite_refs": ["d-03", "I-02"], "client_reviewed_on": TODAY},
    {"item": "夜间告警响应", "finding": "夜间告警依赖值班电话通知，【系统A】的 SIEM 告警没有工单闭环。",
     "client_reviewed_on": TODAY},
]


@pytest.mark.parametrize("text", [
    "特权账号近 12 个月未复核。", "【客户】的 SOC 7x24 值守，L1 分析员 3 人。", "【系统A】与【系统 B】之间无 SLA。",
    "依据 GB/T 22239 第 8.1 条做差距分析。", "夜间告警无人处理（现场 O-03、D-12）。",
])
def test_clean_text_passes(text):
    assert cards.card_problems(text) == []


@pytest.mark.parametrize("text,kind", [
    ("江北农商银行的值班制度不完善。", "机构名称"),
    ("由张经理负责审批。", "人员姓名"),
    ("联系 13812345678 确认。", "联系方式"),
    ("日志服务器 10.20.30.40 未做备份。", "网络标识"),
    ("使用 QRadar 做关联分析。", "系统名称"),
    ("核心系统版本 3.2.1 未升级。", "系统名称"),
    ("受访者表示“我们从来没有做过演练这件事”。", "原话"),
    ("管理员口令为 Passw0rd!2026。", "机密级"),
])
def test_rule_hits(text, kind):
    assert kind in cards.card_problems(text)


def _project(tier: str) -> str:
    code = f"SC-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        db.add(Project(code=code, service="SOC 运营体系设计", industry="银行", tier=tier))
        db.commit()
    return code


@pytest.fixture()
def strict():
    code = _project("strict")
    member, manager = create_user(("consultant",), {code: "member"}), create_user(("consultant",), {code: "manager"})
    other = create_user(("consultant",), {code: "member"})
    clients = [logged_in_client(app, u) for u in (member, manager, other)]
    yield code, *clients, member[0]
    for c in clients:
        c.__exit__(None, None, None)


def _agent_read(code: str, username: str, args: dict) -> dict:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        return tools.invoke(db, user, "research-interview", "project.evidence", args, code)


def test_consent_upload_read_withdraw_revoke(strict):
    code, c, pm, other, member = strict
    base = f"/api/projects/{code}"
    assert c.post(f"{base}/summary-cards", json={"cards": GOOD}).status_code == 422          # 没有同意
    assert c.post(f"{base}/consents", json=CONSENT).status_code == 403                        # 只有项目经理
    assert pm.post(f"{base}/consents", json={**CONSENT, "signed_on": "2999-01-01"}).status_code == 422
    assert pm.post(f"{base}/consents", json={**CONSENT, "doc_ref": "张经理同意函"}).status_code == 422
    r = pm.post(f"{base}/consents", json=CONSENT)
    assert r.status_code == 201, r.text
    consent_id = r.json()["id"]
    assert pm.post(f"{base}/consents", json=CONSENT).status_code == 409
    assert c.get(f"{base}/summary-cards/template").json()["consent_active"] is True

    bad = [GOOD[0], {**GOOD[1], "finding": "由李处长负责，日志在 10.1.1.8。"},
           {"item": "演练", "finding": "无", "client_reviewed_on": "2000-01-01", "onsite_refs": ["文件三"]}]
    r = c.post(f"{base}/summary-cards", json={"cards": bad})
    assert r.status_code == 422
    probs = r.json()["detail"]["problems"]
    assert {(p["card"], p["field"]) for p in probs} >= {(2, "finding"), (3, "client_reviewed_on"), (3, "onsite_refs")}
    assert {"人员姓名", "网络标识"} <= {k for p in probs if p["card"] == 2 for k in p["kinds"]}
    assert c.get(f"{base}/summary-cards").json() == []                                       # 整批没保存

    r = c.post(f"{base}/summary-cards", json={"cards": GOOD})
    assert r.status_code == 201, r.text
    assert [x["code"] for x in r.json()] == ["S-01", "S-02"] and r.json()[0]["onsite_refs"] == ["D-03", "I-02"]
    dup = c.post(f"{base}/summary-cards", json={"cards": [{**GOOD[0], "finding": GOOD[0]["finding"] + "  "}]})
    assert dup.status_code == 409 and dup.json()["detail"]["duplicates"][0]["same_as"] == "S-01"
    assert c.post(f"{base}/summary-cards", json={"cards": [GOOD[1], GOOD[1]]}).status_code == 409

    listed = _agent_read(code, member, {})
    assert [e["code"] for e in listed["evidences"]] == ["S-01", "S-02"] and "占位符" in listed["note"]
    one = _agent_read(code, member, {"code": "s-02"})
    assert "工单闭环" in one["text"]
    assert "error" in _agent_read(code, member, {"code": "D-01"})

    assert other.post(f"{base}/summary-cards/S-01/withdraw").status_code == 403              # 不是上传者也不是经理
    assert c.post(f"{base}/summary-cards/S-01/withdraw").status_code == 200
    assert c.post(f"{base}/summary-cards/S-01/withdraw").status_code == 409
    assert _agent_read(code, member, {"code": "S-01"})["text"] == ""
    assert [e["code"] for e in _agent_read(code, member, {})["evidences"]] == ["S-02"]

    rv = {"revoke_ref": "撤回函-01", "received_on": TODAY}
    assert c.post(f"{base}/consents/{consent_id}/revoke", json=rv).status_code == 403
    r = pm.post(f"{base}/consents/{consent_id}/revoke", json=rv)
    assert r.status_code == 200 and r.json()["revoked"]
    assert pm.post(f"{base}/consents/{consent_id}/revoke", json=rv).status_code == 409
    assert all(x["withdrawn"] and x["finding"] == "" for x in c.get(f"{base}/summary-cards").json())
    assert _agent_read(code, member, {})["evidences"] == []
    assert c.post(f"{base}/summary-cards", json={"cards": GOOD}).status_code == 422          # 撤回后不能上传

    assert pm.post(f"{base}/consents", json={**CONSENT, "doc_ref": "数据处理计划-08"}).status_code == 201
    r = c.post(f"{base}/summary-cards", json={"cards": GOOD})
    assert [x["code"] for x in r.json()] == ["S-03", "S-04"]                                 # 编号不复用

    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code)).all()
    text = " ".join((a.detail or "") for a in logs)
    for s in ("特权账号", "工单闭环", "李处长", "10.1.1.8", "数据处理计划", "撤回函"):
        assert s not in text
    blocked = [json.loads(a.detail) for a in logs if a.action == "project.summary_card" and a.result == "blocked"]
    assert any(b.get("reason") == "card_rules" and "人员姓名" in b["categories"] for b in blocked)


def test_standard_tier_refused_and_deletion_purges(strict):
    code, c, pm, _, _ = strict
    std = _project("standard")
    mgr = create_user(("consultant",), {std: "manager"})
    with logged_in_client(app, mgr) as s:
        assert s.post(f"/api/projects/{std}/consents", json=CONSENT).status_code == 422
        assert s.get(f"/api/projects/{std}/summary-cards/template").status_code == 422
        assert s.post(f"/api/projects/{std}/summary-cards", json={"cards": GOOD}).status_code == 422
        assert "evidences" in _agent_read(std, mgr[0], {})          # 标准档照旧返回证据

    assert pm.post(f"/api/projects/{code}/consents", json=CONSENT).status_code == 201
    assert c.post(f"/api/projects/{code}/summary-cards", json={"cards": GOOD}).status_code == 201
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        assert deletion.pending_counts(db, p)["summary_cards"] == 2
        counts = deletion.purge(db, p)
        db.commit()
        assert counts["summary_cards"] == 2 and counts["consents"] == 1
        assert db.scalar(select(SummaryCard).where(SummaryCard.project_id == p.id)) is None
