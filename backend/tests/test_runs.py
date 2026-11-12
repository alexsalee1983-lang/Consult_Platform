"""交付链编排（M3 ④）：3→4→7 跑通（D3、差距分析、D4 初稿），没有证据时发卡停下、补证处理卡后继续，
模型提出的确认卡让运行停在下一步之前、顾问意见交给后续步骤，严格档和重复发起被拒，取消后任务不再执行，
模型输出命中机密级时不保存、运行失败，模型暂不可用时重试、到上限失败，成员隔离，审计不记原文。"""
import json
import secrets

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Confirmation, Deliverable, Job, LlmUsage, Project
from app.project import deliverables, runs
from app.seed import DELIVERABLES
from app.services import gateway, jobs

from .conftest import create_user, logged_in_client

FACTS = "## 组织与人员\nSOC 现有 6 人，全部白班 [O-01]。\n\n## 待补证据与矛盾点\n夜间值守安排未见书面材料 [推断]。"
GAP = "## 差距分析表\n夜间无人值守，与 7×24 监测要求存在差距 [O-01]。\n\n## 业务影响 TOP 5\n夜间事件发现滞后 [推断]。"
DRAFT = "## 现状概述\n值守仅覆盖白班 [O-01]。\n\n## 主要差距与业务影响\n夜间监测缺失 [推断]。"
CARD_BLOCK = ('\n\n```确认卡\n[{"kind": "方案取舍", "title": "夜间值守采用哪种模式？", '
              '"options": ["A 托管夜间初筛", "B 自建 7×24"], "recommendation": "A", '
              '"reason": "本期不扩编", "impact": "影响 D5 人力测算"}]\n```')


class _Model:
    """按提示里的步骤号返回对应产出；replies 可覆盖某一步的输出，status 可模拟模型不可用。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.replies = {"交付链第 3 步": FACTS, "交付链第 4 步": GAP, "交付链第 7 步": DRAFT}
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "x"})
        user_msg = body["messages"][-1]["content"]
        text = next(v for k, v in self.replies.items() if k in user_msg)
        return httpx.Response(200, json={"model": "mock-model", "choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 50}})

    def prompt(self, step: str) -> str:
        return next(r["messages"][-1]["content"] for r in self.requests if step in r["messages"][-1]["content"])


@pytest.fixture()
def model(monkeypatch):
    m = _Model()
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
    gateway.set_transport(httpx.MockTransport(m.handler))
    yield m
    gateway.set_transport(None)


def _new_project(tier: str = "standard") -> str:
    code = f"RUN-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier=tier)
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    return code


@pytest.fixture()
def proj():
    code = _new_project()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    yield code, c
    c.__exit__(None, None, None)


def _note(c, code: str) -> None:
    r = c.post(f"/api/projects/{code}/notes",
               json={"kind": "observation", "title": "值班室巡查", "content": "值班台交接记录为纸质，夜间无人值守。"})
    assert r.status_code == 201


def _work() -> None:
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=[runs.JOB_KIND]):
            pass


def _run(c, code: str, rid: int) -> dict:
    r = c.get(f"/api/projects/{code}/runs/{rid}")
    assert r.status_code == 200
    return r.json()


def _bodies(code: str, dcode: str) -> list[str]:
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        d = deliverables.get_deliverable(db, p, dcode)
        return [deliverables.get_version(s).body for s in d.sections if s.current_version]


def test_chain_runs_3_4_7_end_to_end(proj, model):
    code, c = proj
    _note(c, code)
    r = c.post(f"/api/projects/{code}/runs")
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["next_step"] == "facts"
    rid = r.json()["id"]
    _work()
    run = _run(c, code, rid)
    assert run["status"] == "done" and run["next_step"] == "end" and run["finished_at"]
    assert [s["step"] for s in run["steps"]] == ["facts", "gap", "draft"]
    assert all(s["status"] == "done" and s["prompt_version"] and s["model"] == "mock-model" for s in run["steps"])
    assert run["steps"][0]["refs"]["deliverable"] == "D3" and run["steps"][2]["refs"]["deliverable"] == "D4"
    assert "差距分析表" in run["steps"][1]["output"]               # 第 4 步产出存在运行记录里
    assert _bodies(code, "D3") == ["SOC 现有 6 人，全部白班 [O-01]。", "夜间值守安排未见书面材料 [推断]。"]
    assert len(_bodies(code, "D4")) == 2
    # 每一步拿到上一步的产出：证据 → D3 → 差距分析
    assert "夜间无人值守" in model.prompt("交付链第 3 步")
    assert "SOC 现有 6 人" in model.prompt("交付链第 4 步")
    assert "夜间无人值守，与 7×24" in model.prompt("交付链第 7 步")
    with SessionLocal() as db:
        used = db.scalars(select(LlmUsage).where(LlmUsage.project_code == code)).all()
        assert {u.agent_code for u in used} == {"research-interview", "compliance-basis", "report-writer"}
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action.like("run.%"))).all()
        assert [a.action for a in logs].count("run.step") == 3
        assert all("SOC 现有" not in (a.detail or "") and "差距分析表" not in (a.detail or "") for a in logs)


def test_no_evidence_asks_then_resumes_after_card(proj, model):
    code, c = proj
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    run = _run(c, code, rid)
    assert run["status"] == "waiting" and run["next_step"] == "facts" and len(run["waiting_cards"]) == 1
    assert run["steps"][0]["status"] == "waiting" and not model.requests          # 没有证据，不调模型
    card = run["waiting_cards"][0]
    with SessionLocal() as db:
        assert db.get(Confirmation, card).kind == "证据不足"
    _note(c, code)
    assert c.post(f"/api/confirmations/{card}/resolve", json={"choice": "A"}).status_code == 200
    assert _run(c, code, rid)["status"] == "queued"
    _work()
    assert _run(c, code, rid)["status"] == "done"


def test_model_card_pauses_before_next_step_and_resolution_is_passed_on(proj, model):
    code, c = proj
    _note(c, code)
    model.replies["交付链第 4 步"] = GAP + CARD_BLOCK
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    run = _run(c, code, rid)
    assert run["status"] == "waiting" and run["next_step"] == "draft" and len(run["waiting_cards"]) == 1
    assert run["steps"][1]["refs"]["cards"] == run["waiting_cards"]
    assert "确认卡" not in run["steps"][1]["output"]                     # 卡片块不进产出正文
    card = run["waiting_cards"][0]
    assert c.post(f"/api/confirmations/{card}/resolve", json={"resolution": "按托管夜间初筛写"}).status_code == 200
    _work()
    assert _run(c, code, rid)["status"] == "done"
    assert "顾问已确认事项" in model.prompt("交付链第 7 步") and "按托管夜间初筛写" in model.prompt("交付链第 7 步")


def test_resolved_question_is_not_asked_again_in_same_run(proj, model):
    code, c = proj
    _note(c, code)
    model.replies["交付链第 4 步"] = GAP + CARD_BLOCK
    model.replies["交付链第 7 步"] = DRAFT + CARD_BLOCK                  # 模型在下一步又问同一个问题
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    card = _run(c, code, rid)["waiting_cards"][0]
    c.post(f"/api/confirmations/{card}/resolve", json={"choice": "A"})
    _work()
    run = _run(c, code, rid)
    assert run["status"] == "done" and run["card_ids"] == [card]          # 不再发卡、不再停


def test_strict_tier_duplicate_start_and_cancel(model):
    strict = _new_project("strict")
    code = _new_project()
    user = create_user(("consultant",), {strict: "member", code: "member"})
    with logged_in_client(app, user) as c:
        assert c.post(f"/api/projects/{strict}/runs").status_code == 422
        rid = c.post(f"/api/projects/{code}/runs").json()["id"]
        assert c.post(f"/api/projects/{code}/runs").status_code == 409            # 已有未结束的运行
        r = c.post(f"/api/projects/{code}/runs/{rid}/cancel")
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        assert c.post(f"/api/projects/{code}/runs/{rid}/cancel").status_code == 409
        _work()                                                                    # 已排队的任务不再执行
        assert _run(c, code, rid)["status"] == "cancelled" and not model.requests
        assert c.post(f"/api/projects/{code}/runs").status_code == 202            # 结束后可以再发起


def test_blocked_model_output_is_not_saved(proj, model):
    code, c = proj
    _note(c, code)
    model.replies["交付链第 3 步"] = "## 组织与人员\n堡垒机 admin/Abc@12345 [O-01]"
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    run = _run(c, code, rid)
    assert run["status"] == "failed" and "机密级" in run["error"] and "Abc@12345" not in json.dumps(run)
    assert _bodies(code, "D3") == []


def test_model_unavailable_retries_then_fails(proj, model):
    code, c = proj
    _note(c, code)
    model.status = 503
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    ctx = jobs.JobContext(job_id=0, kind=runs.JOB_KIND, attempt=1, project_code=code, created_by="")
    with pytest.raises(jobs.JobError) as e:
        runs._step_job({"run_id": rid}, ctx)
    assert e.value.retry and _run(c, code, rid)["status"] == "queued"
    ctx.attempt = runs.MAX_ATTEMPTS
    with pytest.raises(jobs.JobError) as e:
        runs._step_job({"run_id": rid}, ctx)
    assert not e.value.retry
    run = _run(c, code, rid)
    assert run["status"] == "failed" and "模型暂不可用" in run["error"]


def test_non_member_gets_404(proj, model):
    code, c = proj
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    outsider = create_user(("consultant",), {})
    with logged_in_client(app, outsider) as o:
        assert o.get(f"/api/projects/{code}/runs").status_code == 404
        assert o.get(f"/api/projects/{code}/runs/{rid}").status_code == 404
        assert o.post(f"/api/projects/{code}/runs").status_code == 404
        assert o.post(f"/api/projects/{code}/runs/{rid}/cancel").status_code == 404
    other = _new_project()
    assert c.get(f"/api/projects/{other}/runs/{rid}").status_code == 404


def test_parse_output():
    sections, cards = runs.parse_output(GAP + CARD_BLOCK, "差距分析")
    assert [t for t, _ in sections] == ["差距分析表", "业务影响 TOP 5"] and cards[0]["kind"] == "方案取舍"
    sections, cards = runs.parse_output("没有标题的一段正文", "事实清单")
    assert sections == [("事实清单", "没有标题的一段正文")] and cards == []
    with pytest.raises(runs.StepRetry):
        runs.parse_output("```确认卡\n不是 JSON\n```", "x")
    with pytest.raises(runs.StepRetry):
        runs.parse_output("   ", "x")


def test_card_on_last_step_finishes_run(proj, model):
    """测试 #41 缺陷 1：最后一步发的卡处理完后运行应结束（done），不再排任务。"""
    code, c = proj
    _note(c, code)
    model.replies["交付链第 7 步"] = DRAFT + CARD_BLOCK
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    _work()
    run = _run(c, code, rid)
    assert run["status"] == "waiting" and run["next_step"] == "end"
    assert c.post(f"/api/confirmations/{run['waiting_cards'][0]}/resolve", json={"choice": "A"}).status_code == 200
    run = _run(c, code, rid)
    assert run["status"] == "done" and run["finished_at"] and run["waiting_cards"] == []
    with SessionLocal() as db:
        assert not db.scalars(select(Job).where(Job.kind == runs.JOB_KIND, Job.status == "queued",
                                                     Job.project_code == code)).all()
    _work()
    assert _run(c, code, rid)["status"] == "done"
    ctx = jobs.JobContext(job_id=0, kind=runs.JOB_KIND, attempt=1, project_code=code, created_by="")
    runs._step_job({"run_id": rid}, ctx)                                     # 多余的任务也不出错、不改状态
    assert _run(c, code, rid)["status"] == "done"


def test_cancel_during_model_call_writes_nothing(proj, model):
    """测试 #41 缺陷 2：模型调用期间取消，本步不写交付件、不发卡，运行保持已取消。"""
    code, c = proj
    _note(c, code)
    model.replies["交付链第 3 步"] = FACTS + CARD_BLOCK
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    inner = model.handler

    def cancel_then_reply(request):
        assert c.post(f"/api/projects/{code}/runs/{rid}/cancel").status_code == 200
        return inner(request)

    gateway.set_transport(httpx.MockTransport(cancel_then_reply))
    _work()
    run = _run(c, code, rid)
    assert run["status"] == "cancelled" and run["waiting_cards"] == [] and run["card_ids"] == []
    assert run["steps"] == [] and _bodies(code, "D3") == []
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        assert not db.scalars(select(Confirmation).where(Confirmation.project_id == p.id)).all()


def test_cancel_wins_over_worker_state_update(proj, model, monkeypatch):
    """取消发生在写入期间（模型返回之后）：本步写完即停，运行不被改回 waiting / queued。"""
    code, c = proj
    _note(c, code)
    rid = c.post(f"/api/projects/{code}/runs").json()["id"]
    real = runs._send_cards

    def send_then_cancel(*a, **kw):
        ids = real(*a, **kw)
        assert c.post(f"/api/projects/{code}/runs/{rid}/cancel").status_code == 200
        return ids

    monkeypatch.setattr(runs, "_send_cards", send_then_cancel)
    _work()
    run = _run(c, code, rid)
    assert run["status"] == "cancelled" and run["next_step"] == "facts"
    assert [s["status"] for s in run["steps"]] == ["cancelled"] and len(model.requests) == 1


def test_signed_deliverable_refuses_start(proj, model):
    """测试 #41 缺陷 3：要写入的 D3 / D4 已签署时发起即 422，说明原因。"""
    code, c = proj
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        deliverables.get_deliverable(db, p, "D4").status = "signed"
        db.commit()
    r = c.post(f"/api/projects/{code}/runs")
    assert r.status_code == 422 and "D4 已签署" in r.json()["detail"]
    assert not c.get(f"/api/projects/{code}/runs").json()
