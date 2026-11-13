"""质量门禁与签署（M3 ⑤）：评审跑通（Agent 问题表 + 平台硬规则，结论按规则重算）、阻断时门禁不放行、
评审后改了正文门禁要求重评、提交签署 → 项目经理签署（非项目经理 403、项目经理可签自己提交的）、签署后只读且导出不带草稿页眉、
退回要写理由、待签署时改正文回到起草中、输出格式不对重试到上限失败、严格档拒绝、成员隔离、审计不记原文；
项目经理写理由驳回评审 Agent 的阻断（平台规则的阻断不能驳回、过期评审不能驳回、统计多于清单的不能放行）。"""
import io
import json
import secrets

import httpx
import pytest
from docx import Document
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Deliverable, Project
from app.project import quality
from app.seed import DELIVERABLES, seed_signed_sample
from app.services import gateway, jobs

from .conftest import create_user, logged_in_client

REVIEW = ("# 评审意见\n- 交付物：D4 ｜ 评审结论：通过\n- 统计：阻断 0 项，重要 1 项，建议 1 项\n\n"
          "| # | 位置 | 维度 | 严重度 | 问题 | 修改建议 |\n|---|---|---|---|---|---|\n"
          "| 1 | 第 1 章 | 建议质量 | 重要 | 建议缺少验收标准 | 补充验收标准 |\n"
          "| 2 | 第 2 章 | 表达 | 建议 | 语句冗长 | 精简 |\n\n"
          "## 挑战预判\n| 挑战者角色 | 可能的问题 | 当前稿件能否应对 | 建议补充 |\n|---|---|---|---|\n"
          "| 审计 | 证据是否充分 | 部分 | 补值班记录 |\n")
BODY1 = "SOC 现有 6 人，全部白班，交接记录为纸质 [O-01]。"
BODY2 = "夜间无人值守，与 7×24 监测要求存在差距 [O-01]；建议引入托管夜间初筛 [推断]。"


class _Model:
    def __init__(self):
        self.requests: list[dict] = []
        self.reply = REVIEW

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "mock-model", "choices": [{"message": {"content": self.reply}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 50}})


@pytest.fixture()
def model(monkeypatch):
    m = _Model()
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
    gateway.set_transport(httpx.MockTransport(m.handler))
    yield m
    gateway.set_transport(None)


def _new_project(tier: str = "standard") -> str:
    code = f"QA-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier=tier)
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    return code


@pytest.fixture()
def team():
    """项目成员（顾问，起草和提交）、项目经理 A、项目经理 B。"""
    code = _new_project()
    clients = [logged_in_client(app, create_user(("consultant",), {code: role}))
               for role in ("member", "manager", "manager")]
    yield code, *clients
    for c in clients:
        c.__exit__(None, None, None)


def _work() -> None:
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=[quality.JOB_KIND]):
            pass


def _draft(c, code: str, bodies=(BODY1, BODY2)) -> None:
    r = c.post(f"/api/projects/{code}/notes",
               json={"kind": "observation", "title": "值班室巡查", "content": "值班台交接记录为纸质，夜间无人值守。"})
    assert r.status_code == 201
    assert c.put(f"/api/projects/{code}/deliverables/D4/outline",
                 json={"titles": ["现状概述", "主要差距"]}).status_code == 200
    for i, b in enumerate(bodies, 1):
        assert c.put(f"/api/projects/{code}/deliverables/D4/sections/{i}", json={"body": b}).status_code == 201


def _review(c, code: str) -> dict:
    r = c.post(f"/api/projects/{code}/deliverables/D4/reviews")
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    _work()
    r = c.get(f"/api/projects/{code}/deliverables/D4/reviews/{r.json()['id']}")
    assert r.status_code == 200
    return r.json()


def _url(code: str, tail: str) -> str:
    return f"/api/projects/{code}/deliverables/D4/{tail}"


def test_review_submit_sign_then_readonly_and_export_without_draft(team, model):
    code, member, mgr_a, mgr_b = team
    _draft(member, code)
    rv = _review(member, code)
    assert rv["status"] == "done" and (rv["blocking"], rv["major"], rv["minor"]) == (0, 1, 1)
    assert rv["conclusion"] == "pass" and rv["conclusion_name"] == "通过" and not rv["stale"]
    assert {i["severity"] for i in rv["issues"]} == {"重要", "建议"} and "挑战预判" in rv["report"]
    # 评审的提示里有交付件正文，系统提示是质量评审 Agent
    sent = model.requests[-1]["messages"]
    assert BODY1 in sent[-1]["content"] and "质量评审" in sent[0]["content"]

    st = member.get(_url(code, "signoff")).json()
    assert st["gate"] and st["reasons"] == [] and st["latest_review"]["id"] == rv["id"]
    # 项目成员（非项目经理）能提交、不能签署
    r = member.post(_url(code, "signoff/submit"), json={"comment": "已逐章核对"})
    assert r.status_code == 200 and r.json()["status"] == "review"
    assert member.post(_url(code, "signoff/sign"), json={}).status_code == 403
    assert member.post(_url(code, "signoff/submit"), json={}).status_code == 409    # 已提交
    r = mgr_a.post(_url(code, "signoff/sign"), json={"comment": "同意"})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["status"] == "signed" and not st["gate"]
    assert [h["action"] for h in st["history"]] == ["submit", "sign"]
    assert st["history"][1]["review_id"] == rv["id"]

    # 签署后只读：写章节、改大纲 409，不再评审，不能再签
    assert member.put(_url(code, "sections/1"), json={"body": "改一句"}).status_code == 409
    assert member.put(_url(code, "outline"), json={"titles": ["x"]}).status_code == 409
    assert member.post(_url(code, "reviews")).status_code == 409
    assert mgr_b.post(_url(code, "signoff/sign"), json={}).status_code == 409
    # 导出不带草稿页眉
    r = member.get(_url(code, "export"))
    assert r.status_code == 200
    doc = Document(io.BytesIO(r.content))
    assert all("草稿" not in p.text for p in doc.sections[0].header.paragraphs)
    assert "%E8%8D%89%E7%A8%BF" not in r.headers["content-disposition"]

    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action.like("deliverable.s%"))).all()
        assert {x.action for x in logs} >= {"deliverable.submit", "deliverable.sign"}
        dump = json.dumps([x.detail for x in db.scalars(select(AuditLog).where(AuditLog.project_code == code))],
                          ensure_ascii=False)
        for secret in ("已逐章核对", "建议缺少验收标准", "白班", "同意"):
            assert secret not in dump


def test_manager_can_sign_own_submission(team, model):
    """负责人 2026-11-12 定：不设四眼限制，只有一位项目经理的项目也能走完签署。"""
    code, member, mgr_a, _ = team
    _draft(mgr_a, code)
    _review(mgr_a, code)
    assert mgr_a.post(_url(code, "signoff/submit"), json={}).status_code == 200
    r = mgr_a.post(_url(code, "signoff/sign"), json={})
    assert r.status_code == 200 and r.json()["status"] == "signed"


def test_rule_blockers_fail_gate(team, model):
    code, member, *_ = team
    _draft(member, code, bodies=("夜间值守安排未见书面材料 [推断]。", "经评估，本系统已通过等保三级测评 [D-99]。"))
    rv = _review(member, code)
    assert rv["conclusion"] == "fail" and rv["blocking"] >= 2
    rule = [i for i in rv["issues"] if i["by"] == "rule"]
    assert {i["dimension"] for i in rule} >= {"范围与资质边界", "证据链 / 依据"}
    assert any("D-99" in i["problem"] for i in rule)
    assert any(i["severity"] == "重要" and i["location"].startswith("第 1 章") for i in rule)   # 整章只有推断
    assert all("通过等保三级测评" not in i["problem"] for i in rule)    # 硬规则只记类别，不摘正文
    r = member.post(_url(code, "signoff/submit"), json={})
    assert r.status_code == 409 and any("阻断" in x for x in r.json()["detail"]["reasons"])


def test_agent_blocker_and_stats_take_max(team, model):
    code, member, *_ = team
    model.reply = "# 评审意见\n评审结论：不通过\n统计：阻断 1 项，重要 3 项，建议 0 项\n（表格略）"
    _draft(member, code)
    rv = _review(member, code)
    assert (rv["blocking"], rv["major"], rv["conclusion"]) == (1, 3, "fail")


def test_parse_conclude_and_qualification_rules():
    issues = quality.parse_review("评审结论：通过\n统计：阻断 0 项，重要 3 项，建议 2 项")
    assert issues is not None and issues[1] == {"阻断": 0, "重要": 3, "建议": 2}
    assert quality.conclude(0, 3) == "revise" and quality.conclude(1, 0) == "fail" and quality.conclude(0, 2) == "pass"
    assert quality.parse_review("好的，我看过了") is None
    assert quality.qualification_hits("本报告不构成测评结论，仅为差距分析。") == 0
    assert quality.qualification_hits("该系统已通过等保测评；认证结论为合格。") == 2
    for text in ("项目已通过三级等保测评 [D-99]。", "本系统通过第三级等级保护测评。", "三级等保测评合格。",
                 "已达到等保三级达标要求。", "密评通过。"):
        assert quality.qualification_hits(text) >= 1, text
    assert quality.qualification_hits("按等保三级要求做差距分析，提出整改建议。") == 0


def test_edit_after_review_makes_it_stale_and_edit_in_review_returns_to_drafting(team, model):
    code, member, mgr_a, _ = team
    _draft(member, code)
    _review(member, code)
    assert member.put(_url(code, "sections/2"), json={"body": BODY2 + "补充一句 [O-01]。"}).status_code == 201
    st = member.get(_url(code, "signoff")).json()
    assert not st["gate"] and any("重新评审" in x for x in st["reasons"]) and st["latest_review"]["stale"]
    assert member.post(_url(code, "signoff/submit"), json={}).status_code == 409
    _review(member, code)
    assert member.post(_url(code, "signoff/submit"), json={}).status_code == 200
    # 待签署时改了正文：回到起草中，签署 409
    assert member.put(_url(code, "sections/1"), json={"body": BODY1 + "（复核）"}).status_code == 201
    assert member.get(_url(code, "signoff")).json()["status"] == "drafting"
    assert mgr_a.post(_url(code, "signoff/sign"), json={}).status_code == 409


def test_return_requires_reason_and_manager(team, model):
    code, member, mgr_a, _ = team
    _draft(member, code)
    _review(member, code)
    member.post(_url(code, "signoff/submit"), json={})
    assert member.post(_url(code, "signoff/return"), json={"comment": "x"}).status_code == 403
    assert mgr_a.post(_url(code, "signoff/return"), json={}).status_code == 422
    r = mgr_a.post(_url(code, "signoff/return"), json={"comment": "第 2 章建议缺验收标准"})
    assert r.status_code == 200 and r.json()["status"] == "drafting"
    assert [h["action"] for h in r.json()["history"]] == ["submit", "return"]


def test_bad_format_retries_then_fails_and_duplicate_refused(team, model):
    code, member, *_ = team
    _draft(member, code)
    model.reply = "好的，我看过了。"
    rid = member.post(_url(code, "reviews")).json()["id"]
    assert member.post(_url(code, "reviews")).status_code == 409          # 已有进行中的评审
    ctx = jobs.JobContext(job_id=0, kind=quality.JOB_KIND, attempt=1, project_code=code, created_by="")
    with pytest.raises(jobs.JobError) as e:
        quality._review_job({"review_id": rid}, ctx)
    assert e.value.retry and member.get(_url(code, f"reviews/{rid}")).json()["status"] == "queued"
    ctx.attempt = quality.MAX_ATTEMPTS
    with pytest.raises(jobs.JobError) as e:
        quality._review_job({"review_id": rid}, ctx)
    assert not e.value.retry
    rv = member.get(_url(code, f"reviews/{rid}")).json()
    assert rv["status"] == "failed" and "格式" in rv["error"]


def test_empty_and_strict_refused(model):
    code = _new_project()
    c = logged_in_client(app, create_user(("consultant",), {code: "member"}))
    assert c.post(_url(code, "reviews")).status_code == 409
    c.__exit__(None, None, None)
    code = _new_project("strict")
    c = logged_in_client(app, create_user(("consultant",), {code: "manager"}))
    assert c.post(_url(code, "reviews")).status_code == 422
    c.__exit__(None, None, None)


def test_non_member_gets_404(team, model):
    code, member, *_ = team
    _draft(member, code)
    other = logged_in_client(app, create_user(("consultant",), {"MFG-C": "member"}))
    for method, tail in (("POST", "reviews"), ("GET", "reviews"), ("GET", "signoff"), ("POST", "signoff/submit")):
        assert other.request(method, _url(code, tail), json={}).status_code == 404
    other.__exit__(None, None, None)


def test_seed_signed_sample_is_idempotent():
    with SessionLocal() as db:
        seed_signed_sample(db)
        seed_signed_sample(db)
        p = db.scalar(select(Project).where(Project.code == "BANK-A"))
        d = next(x for x in p.deliverables if x.code == "D1")
        assert d.status == "signed" and len(d.sections) == 2 and all(s.current_version == 1 for s in d.sections)
        assert [s.action for s in quality.list_signoffs(db, d)] == ["submit", "sign"]


AGENT_BLOCK = ("# 评审意见\n评审结论：不通过\n统计：阻断 1 项，重要 1 项，建议 0 项\n\n"
               "| # | 位置 | 维度 | 严重度 | 问题 | 修改建议 |\n|---|---|---|---|---|---|\n"
               "| 1 | 第 2 章 | 业务关联 | 阻断 | 托管初筛的成本未论证 | 补充成本比较 |\n"
               "| 2 | 第 1 章 | 建议质量 | 重要 | 建议缺少验收标准 | 补充验收标准 |\n")


def _waive(c, code: str, rid: int, issue: int, reason: str = "成本比较放在 D6 方案里，本报告不展开"):
    return c.post(_url(code, f"reviews/{rid}/waivers"), json={"issue": issue, "reason": reason})


def test_manager_waives_agent_blocker_then_sign(team, model):
    code, member, mgr_a, _ = team
    model.reply = AGENT_BLOCK
    _draft(member, code)
    rv = _review(member, code)
    assert (rv["blocking"], rv["open_blocking"], rv["waived"]) == (1, 1, 0)
    idx = next(n for n, i in enumerate(rv["issues"]) if i["severity"] == "阻断")
    assert rv["issues"][idx]["waivable"] and not any(i["waivable"] for i in rv["issues"] if i["severity"] != "阻断")
    r = member.post(_url(code, "signoff/submit"), json={})
    assert r.status_code == 409 and any("项目经理写理由驳回" in x for x in r.json()["detail"]["reasons"])

    assert _waive(member, code, rv["id"], idx).status_code == 403                    # 只有项目经理
    assert _waive(mgr_a, code, rv["id"], idx, reason="  ").status_code == 422        # 必须写理由
    assert _waive(mgr_a, code, rv["id"], idx, reason="o k").status_code == 422       # 理由太短（去空白不足 10 字）
    assert _waive(mgr_a, code, rv["id"], idx + 1).status_code == 422                 # 不是阻断
    assert _waive(mgr_a, code, rv["id"], 99).status_code == 404
    r = _waive(mgr_a, code, rv["id"], idx)
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["blocking"], d["open_blocking"], d["waived"]) == (1, 0, 1)
    assert d["waivers"][0]["issue"] == idx and d["waivers"][0]["actor"]
    assert _waive(mgr_a, code, rv["id"], idx).status_code == 409                     # 不重复驳回

    st = member.get(_url(code, "signoff")).json()
    assert st["gate"] and st["reasons"] == [] and st["latest_review"]["open_blocking"] == 0
    assert member.post(_url(code, "signoff/submit"), json={}).status_code == 200
    r = mgr_a.post(_url(code, "signoff/sign"), json={})
    assert r.status_code == 200 and r.json()["status"] == "signed"
    assert [h["waived"] for h in r.json()["history"]] == [1, 1]

    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code)).all()
        waive = [x for x in logs if x.action == "quality.waive" and x.result == "allowed"]
        assert len(waive) == 1 and json.loads(waive[0].detail)["issue"] == idx
        signs = [json.loads(x.detail) for x in logs
                 if x.action in ("deliverable.submit", "deliverable.sign") and x.result == "allowed"]
        assert [x["waived"] for x in signs] == [1, 1]
        dump = json.dumps([x.detail for x in logs], ensure_ascii=False)
        assert "成本比较放在" not in dump and "托管初筛的成本" not in dump


def test_rule_blocker_and_stale_review_cannot_be_waived(team, model):
    code, member, mgr_a, _ = team
    model.reply = AGENT_BLOCK
    _draft(member, code, bodies=(BODY1, "夜间无人值守 [D-99]。"))
    rv = _review(member, code)
    rule = next(n for n, i in enumerate(rv["issues"]) if i["by"] == "rule" and i["severity"] == "阻断")
    agent = next(n for n, i in enumerate(rv["issues"]) if i["by"] == "agent" and i["severity"] == "阻断")
    r = _waive(mgr_a, code, rv["id"], rule)
    assert r.status_code == 422 and "平台规则" in r.json()["detail"]
    assert _waive(mgr_a, code, rv["id"], agent).status_code == 200
    r = member.get(_url(code, "signoff")).json()
    assert not r["gate"] and any("平台规则判出 1 项" in x for x in r["reasons"])
    assert r["latest_review"]["open_blocking"] == 1

    # 改了正文，旧评审过期，不能再驳回；新评审的阻断要重新处理
    assert member.put(_url(code, "sections/2"), json={"body": BODY2}).status_code == 201
    assert _waive(mgr_a, code, rv["id"], agent, reason="再驳一次，理由写够十个字").status_code == 409
    rv2 = _review(member, code)
    assert rv2["open_blocking"] == 1 and rv2["waived"] == 0


def test_unlisted_agent_blockers_keep_gate_closed(team, model):
    code, member, mgr_a, _ = team
    model.reply = AGENT_BLOCK.replace("阻断 1 项", "阻断 2 项")
    _draft(member, code)
    rv = _review(member, code)
    assert rv["blocking"] == 2
    idx = next(n for n, i in enumerate(rv["issues"]) if i["severity"] == "阻断")
    assert _waive(mgr_a, code, rv["id"], idx).status_code == 200
    st = member.get(_url(code, "signoff")).json()
    assert not st["gate"] and st["latest_review"]["open_blocking"] == 1
    assert any("无法逐条驳回" in x for x in st["reasons"])
