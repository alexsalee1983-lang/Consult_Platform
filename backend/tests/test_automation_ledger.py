"""四期 F5：自动化账本与自适应抽检（只读审计记录，不建表）。"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from app.core import audit
from app.core.db import SessionLocal
from app.main import app
from app.models import Project
from app.services import automation_ledger as al

from .conftest import create_user, logged_in_client


def _project(service: str = "SOC 运营体系设计") -> str:
    code = f"F5-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        db.add(Project(code=code, service=service, industry="银行", tier="standard"))
        db.commit()
    return code


def _log(code: str, action: str, result: str = "ok", actor: str = "pm-f5", **detail) -> None:
    with SessionLocal() as db:
        audit.record(db, actor=actor, action=action, result=result, project_code=code, **detail)


def _window():
    end = datetime.now(timezone.utc) + timedelta(seconds=1)
    return end - timedelta(days=7), end


def test_ledger_counts_auto_manual_and_spot_errors():
    code = _project()
    _log(code, "survey.auto_confirm", answer_ids=[1, 2, 3], qids=["Q1", "Q2", "Q3"], sampled=1)
    _log(code, "project.phase_auto", actor="system", stage="调研")
    _log(code, "survey.confirm", answer_id=4)
    _log(code, "survey.spot_check", result="failed", answer_id=1)
    _log(code, "survey.spot_check", answer_id=2)
    _log(code, "llm.chat", length=10)                         # 不是人动手的事
    _log(code, "deliverable.sign", actor="cli")               # 命令行不算人工
    with SessionLocal() as db:
        rep = al.ledger(db, *_window(), [code])
    assert (rep["auto"], rep["manual"], rep["spot_checks"], rep["spot_errors"]) == (4, 3, 2, 1)
    first, second = rep["entries"]                             # 新的在前
    assert first["rule"] == "阶段自动推进" and first["actor"] == "system" and first["count"] == 1
    assert second["rule"] == "事实自动确认" and second["count"] == 3 and second["basis"]["qids"] == ["Q1", "Q2", "Q3"]
    assert "ip" not in second["basis"]


def test_ledger_empty_codes_counts_nothing():
    with SessionLocal() as db:
        rep = al.ledger(db, *_window(), [])
    assert (rep["auto"], rep["manual"], rep["entries"]) == (0, 0, [])


def test_spot_state_tightens_then_recovers_then_relaxes():
    base = 0.1
    assert al.spot_state([], base) == {"rate": base, "state": "normal", "streak": 0}
    assert al.spot_state([True] * 10 + [False], base)["state"] == "tight"
    assert al.spot_state([True] * 10 + [False], base)["rate"] == 1.0           # 发现差错：每条都抽，即退回人工
    assert al.spot_state([False] + [True] * (al.RECOVER - 1), base)["state"] == "tight"
    assert al.spot_state([False] + [True] * al.RECOVER, base) == {"rate": base, "state": "normal", "streak": al.RECOVER}
    relaxed = al.spot_state([True] * al.RELAX, base)
    assert relaxed["state"] == "relaxed" and relaxed["rate"] == 0.05             # 连续无差错再放开
    assert al.spot_state([True] * al.RELAX, 0.02)["rate"] == al.MIN_RATE         # 不低于下限


def test_adaptive_rate_reads_latest_audit_verdicts():
    code = _project()
    action = f"f5test.spot_{secrets.token_hex(4)}"            # 独立动作名，不受其他测试的抽检记录影响
    _log(code, action)
    _log(code, action, result="failed")
    with SessionLocal() as db:
        assert al.adaptive_spot_rate(db, 0.1, action)["rate"] == 1.0
    for _ in range(al.RECOVER):
        _log(code, action)
    with SessionLocal() as db:
        assert al.adaptive_spot_rate(db, 0.1, action)["state"] == "normal"


def test_acceptance_three_indicators():
    soc, ire = _project(), _project("IRE 应急响应")
    _log(soc, "survey.auto_confirm", answer_ids=list(range(8)))
    _log(soc, "survey.confirm", answer_id=99)
    _log(soc, "survey.spot_check", answer_id=1)
    for _ in range(5):
        _log(soc, "deliverable.sign")
    for _ in range(20):                                        # IRE 项目人工多也不算进 SOC 指标
        _log(ire, "confirmation.resolve")
    with SessionLocal() as db:
        rep = al.acceptance(db, *_window(), [soc, ire])
    assert rep["soc_manual"]["value"] == 7 and rep["soc_manual"]["projects"] == 1
    assert rep["soc_manual"]["status"] == "ok"
    assert rep["fact_auto"]["value"] == round(8 / 9, 4) and rep["fact_auto"]["status"] == "ok"
    assert rep["spot_error"]["value"] == 0 and rep["spot_error"]["status"] == "ok"
    assert rep["daily_manual"]["value"] == 20 and rep["daily_manual"]["status"] == "breach"


def test_acceptance_without_data_is_na():
    with SessionLocal() as db:
        rep = al.acceptance(db, *_window(), [])
    assert {v["status"] for v in rep.values()} == {"na"}


def test_project_api_member_only_and_indicators_include_automation():
    code = _project()
    _log(code, "survey.auto_confirm", answer_ids=[1])
    with logged_in_client(app, create_user(("consultant",), {code: "manager"})) as c:
        body = c.get(f"/api/projects/{code}/automation").json()
        assert body["days"] == 7 and body["auto"] == 1 and body["entries"][0]["rule"] == "事实自动确认"
    with logged_in_client(app, create_user(("consultant",), {})) as c:
        assert c.get(f"/api/projects/{code}/automation").status_code == 404
    with logged_in_client(app, create_user(("consultant", "project_lead"), {})) as c:
        auto = c.get("/api/indicators?days=7").json()["automation"]
        assert set(auto) == {"soc_manual", "fact_auto", "spot_error", "daily_manual", "spot"}
        assert auto["spot"]["state"] in ("normal", "tight", "relaxed")
