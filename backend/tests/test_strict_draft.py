"""M4 ④b：严格档交付件占位符版本。

写入时查占位符（机构名、人名、联系方式、网络标识、系统名、原话 → 422，不保存、审计只记类别）；
来源标注 [S-xx] 按有效摘要卡核对，撤下的卡核对不上；交付链读摘要卡起草、提示里带严格档要求；
没有摘要卡时发“证据不足”卡；质量评审、导出对严格档放开。"""
import json
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AgentRun, AuditLog, Confirmation, Deliverable, Project, QualityReview
from app.project import cards, runs
from app.services import gateway, jobs

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project

TODAY = date.today().isoformat()
CONSENT = {"doc_ref": "数据处理计划-11", "signed_on": (date.today() - timedelta(days=2)).isoformat()}
CARDS = [
    {"item": "夜间值守", "finding": "夜间无人值守，告警由【人员1】次日处理。", "magnitude": "每晚约 200 条",
     "onsite_refs": ["O-03"], "client_reviewed_on": TODAY},
    {"item": "特权账号复核", "finding": "【系统A】特权账号近 12 个月未复核。", "client_reviewed_on": TODAY},
]
GOOD = "【客户】夜间无人值守 [S-01]；【系统A】特权账号未复核 [S-02]（现场 O-03）。依据 GB/T 22239 第 8.1 条。"
DRAFT = "## 现状概述\n【客户】夜间无人值守 [S-01]。\n\n## 主要差距\n特权账号未复核 [S-02]。"


@pytest.fixture()
def strict():
    code = _new_project("strict")
    mgr = create_user(("consultant",), {code: "manager"})
    c = logged_in_client(app, mgr)
    yield code, c
    c.__exit__(None, None, None)


def _consent(c, code: str) -> int:
    r = c.post(f"/api/projects/{code}/consents", json=CONSENT)
    assert r.status_code == 201
    return r.json()["id"]


def _with_cards(c, code: str) -> None:
    _consent(c, code)
    assert c.post(f"/api/projects/{code}/summary-cards", json={"cards": CARDS}).status_code == 201


@pytest.mark.parametrize("text", [
    GOOD, "建议【客户】建立 7×24 值守 [推断]。【依据：GB/T 22239-2019 8.1.4｜L4-REG-GBT-22239-00001】",
    "【人员1】负责 SOC 的 L1 分析，SIEM 告警没有工单闭环 [S-01]。",
])
def test_placeholder_text_passes(text):
    assert cards.placeholder_problems(text) == []


def test_write_checks_placeholders_and_resolves_cards(strict):
    code, c = strict
    _with_cards(c, code)
    assert c.put(f"{_base(code)}/outline", json={"titles": ["江北农商银行现状"]}).status_code == 422
    assert c.put(f"{_base(code)}/outline", json={"titles": ["现状概述", "主要差距"]}).status_code == 200
    for body, kind in (("由李处长负责审批 [S-01]。", "人员姓名"), ("日志服务器 10.20.30.40 未备份 [S-01]。", "网络标识"),
                       ("使用 QRadar 做关联分析 [S-02]。", "系统名称")):
        r = c.put(f"{_base(code)}/sections/1", json={"body": body})
        assert r.status_code == 422, r.text
        assert kind in r.json()["detail"].get("kinds", []) or kind == "网络标识"   # IP 先被机密级检测拦下
    r = c.put(f"{_base(code)}/sections/1", json={"body": GOOD + " 另见 [S-09]。"})
    assert r.status_code == 201, r.text
    assert r.json()["sources"]["unresolved"] == ["S-09"]

    assert c.post(f"/api/projects/{code}/summary-cards/S-02/withdraw").status_code == 200
    r = c.put(f"{_base(code)}/sections/2", json={"body": "特权账号未复核 [S-02]。"})
    assert r.status_code == 201 and r.json()["sources"]["unresolved"] == ["S-02"]        # 撤下的卡核对不上

    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action.like("deliverable.%"))).all()
    text = " ".join(a.detail or "" for a in logs)
    assert "李处长" not in text and "10.20.30.40" not in text and "QRadar" not in text
    assert sum(1 for a in logs if a.result == "blocked" and json.loads(a.detail).get("reason") == "placeholder") >= 3

    assert c.post(f"{_base(code)}/reviews").status_code == 202                 # 质量评审放开
    r = c.get(f"{_base(code)}/export")
    assert r.status_code == 200 and len(r.content) > 1000                     # 导出放开（占位符版本）


class _Model:
    def __init__(self):
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)["messages"][-1]["content"]
        self.requests.append(msg)
        text = DRAFT.replace("现状概述", "事实") if "交付链第 3 步" in msg else DRAFT
        return httpx.Response(200, json={"model": "mock-model", "choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 50}})


@pytest.fixture()
def model(monkeypatch):
    m = _Model()
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
    gateway.set_transport(httpx.MockTransport(m.handler))
    yield m
    gateway.set_transport(None)


def _work() -> None:
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=[runs.JOB_KIND]):
            pass


def test_run_without_cards_asks_then_drafts_from_cards(strict, model):
    code, c = strict
    assert c.post(f"/api/projects/{code}/runs").status_code == 422             # 没有客户同意
    consent = _consent(c, code)
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    run = c.get(f"/api/projects/{code}/runs/{rid}").json()
    assert run["status"] == "waiting" and not model.requests
    assert run["chain"] == "strict"                                            # 按项目档位记
    with SessionLocal() as db:
        card = db.get(Confirmation, run["card_ids"][0])
        assert "摘要卡" in card.title
    c.post(f"/api/projects/{code}/runs/{rid}/cancel")

    assert c.post(f"/api/projects/{code}/summary-cards", json={"cards": CARDS}).status_code == 201
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    assert c.get(f"/api/projects/{code}/runs/{rid}").json()["status"] == "done"
    first = model.requests[0]
    assert "[S-01] 夜间值守（数量级：每晚约 200 条；现场 O-03）" in first and "占位符" in first
    with SessionLocal() as db:
        d = db.scalar(select(Deliverable).join(Project).where(Project.code == code, Deliverable.code == "D4"))
        assert d.sections and d.sections[0].current_version

    # 客户撤回同意：未签署交付件正文、评审意见、运行产出清空；之后不能写入、不能运行
    assert c.post(f"{_base(code)}/reviews").status_code == 202
    r = c.post(f"/api/projects/{code}/consents/{consent}/revoke", json={"revoke_ref": "撤回-01", "received_on": TODAY})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        ds = db.scalars(select(Deliverable).where(Deliverable.project_id == p.id)).all()
        assert all(v.body == "" for d in ds for s in d.sections for v in s.versions)
        assert all(not q.report and not q.issues for q in db.scalars(select(QualityReview).where(
            QualityReview.project_id == p.id)))
        assert all(s.output == "" for run in db.scalars(select(AgentRun).where(AgentRun.project_id == p.id))
                   for s in run.steps)
        a = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                              AuditLog.action == "project.consent.revoke")).all()[-1]
        assert json.loads(a.detail)["versions_cleared"] >= 2 and "撤回-01" not in a.detail
    assert c.put(f"{_base(code)}/sections/1", json={"body": "【客户】值守 [推断]。"}).status_code == 422
    assert c.post(f"/api/projects/{code}/runs").status_code == 422

def test_run_output_with_real_names_fails(strict, model, monkeypatch):
    code, c = strict
    _with_cards(c, code)
    monkeypatch.setattr(model, "handler", None)
    gateway.set_transport(httpx.MockTransport(lambda r: httpx.Response(200, json={
        "model": "m", "choices": [{"message": {"content": "## 事实\n由张经理负责 [S-01]。"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}})))
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    assert c.get(f"/api/projects/{code}/runs/{rid}").json()["status"] == "failed"
    with SessionLocal() as db:
        d = db.scalar(select(Deliverable).join(Project).where(Project.code == code, Deliverable.code == "D3"))
        assert not any(s.current_version for s in d.sections)                 # 不保存

