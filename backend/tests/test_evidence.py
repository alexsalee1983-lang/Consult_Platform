"""项目区资料与证据编号（M3）：编号规则、只写角色、机密级拦截、严格档拒收、成员隔离、上传与解析、工具 project.evidence。"""
import json
import secrets

import pytest
from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Evidence, Project, ProjectDocument, ProjectMember, User
from app.services import jobs

from .conftest import create_user, logged_in_client, on_s3
from .kb_samples import SOC_MD, blank_pdf_bytes, docx_bytes

INTERVIEW = {"kind": "interview", "title": "SOC 值班访谈", "interviewee_role": "信息科技部 SOC 值班负责人",
             "occurred_on": "2026-11-05",
             "content": "目前 7×8 小时值班，夜间告警由短信通知二线；交接班记录为纸质，未纳入工单系统。"}


def _new_project(tier: str = "standard") -> str:
    """每个测试用一个新的虚拟项目，编号从 01 起，不受其他测试影响。"""
    code = f"EVT-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        db.add(Project(code=code, service="SOC 运营体系设计", industry="银行", tier=tier))
        db.commit()
    return code


def _member_client(*codes: str):
    user = create_user(("consultant",), {c: "member" for c in codes})
    return logged_in_client(app, user), user[0]


@pytest.fixture()
def proj():
    code = _new_project()
    c, name = _member_client(code)
    yield code, c, name
    c.__exit__(None, None, None)


def _last_audit(action: str) -> AuditLog:
    with SessionLocal() as db:
        return db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.desc())).first()


def test_notes_get_sequential_codes_per_kind(proj):
    code, c, name = proj
    r1 = c.post(f"/api/projects/{code}/notes", json=INTERVIEW)
    assert r1.status_code == 201, r1.text
    assert r1.json()["code"] == "I-01" and r1.json()["kind_name"] == "访谈纪要"
    assert c.post(f"/api/projects/{code}/notes", json={**INTERVIEW, "title": "二线访谈"}).json()["code"] == "I-02"
    obs = c.post(f"/api/projects/{code}/notes", json={"kind": "observation", "title": "值班室巡查",
                                                     "content": "值班台有两块大屏。"})
    assert obs.json()["code"] == "O-01" and obs.json()["status"] == "ready"

    listed = c.get(f"/api/projects/{code}/evidences").json()
    assert [e["code"] for e in listed] == ["I-01", "I-02", "O-01"]
    d = c.get(f"/api/projects/{code}/evidences/i-01").json()          # 编号不区分大小写
    assert d["content"] == INTERVIEW["content"] and d["interviewee_role"] == INTERVIEW["interviewee_role"]
    assert d["occurred_on"] == "2026-11-05" and d["created_by"] == name
    assert c.get(f"/api/projects/{code}/evidences/I-09").status_code == 404

    a = _last_audit("project.note")
    assert a.result == "allowed" and a.project_code == code and json.loads(a.detail)["evidence"] == "O-01"
    assert "大屏" not in a.detail                                      # 审计不记正文


def test_interview_requires_role_not_name(proj):
    code, c, _ = proj
    r = c.post(f"/api/projects/{code}/notes", json={**INTERVIEW, "interviewee_role": ""})
    assert r.status_code == 422 and "角色" in r.json()["detail"]
    assert c.post(f"/api/projects/{code}/notes", json={**INTERVIEW, "kind": "other"}).status_code == 422
    assert c.post(f"/api/projects/{code}/notes", json={**INTERVIEW, "content": "   "}).status_code == 422


def test_guard_blocks_note_without_saving(proj):
    code, c, _ = proj
    secret = "防火墙管理口令 Abc@12345，核心交换机 10.1.20.1"
    r = c.post(f"/api/projects/{code}/notes", json={**INTERVIEW, "content": secret})
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["blocked"] is True and "没有保存" in body["summary"]
    assert "Abc@12345" not in r.text and "10.1.20.1" not in r.text
    assert c.get(f"/api/projects/{code}/evidences").json() == []
    a = _last_audit("project.note")
    assert a.result == "blocked" and json.loads(a.detail)["reason"] == "guard" and "Abc@12345" not in a.detail


def test_strict_project_refuses_materials():
    code = _new_project("strict")
    c, _ = _member_client(code)
    try:
        r = c.post(f"/api/projects/{code}/notes", json=INTERVIEW)
        assert r.status_code == 422 and "严格档" in r.json()["detail"]
        r = c.post(f"/api/projects/{code}/documents", files={"file": ("值班.md", SOC_MD.encode(), "text/markdown")})
        assert r.status_code == 422 and "严格档" in r.json()["detail"]
        assert c.get(f"/api/projects/{code}/evidences").json() == []
        assert json.loads(_last_audit("project.document").detail)["reason"] == "strict_tier"
    finally:
        c.__exit__(None, None, None)


def test_non_member_sees_404(proj):
    code, c, _ = proj
    assert c.post(f"/api/projects/{code}/notes", json=INTERVIEW).status_code == 201
    other, _ = _member_client("MFG-C")
    try:
        assert other.get(f"/api/projects/{code}/evidences").status_code == 404
        assert other.get(f"/api/projects/{code}/evidences/I-01").status_code == 404
        assert other.post(f"/api/projects/{code}/notes", json=INTERVIEW).status_code == 404
        assert other.post(f"/api/projects/{code}/documents",
                          files={"file": ("a.md", b"# x\n\ny", "text/markdown")}).status_code == 404
    finally:
        other.__exit__(None, None, None)


def test_document_checks_before_storage(proj, monkeypatch):
    code, c, _ = proj

    def up(name, data):
        return c.post(f"/api/projects/{code}/documents", files={"file": (name, data, "application/octet-stream")})

    assert up("a.exe", b"MZ").status_code == 422
    r = up("scan.pdf", blank_pdf_bytes())
    assert r.status_code == 422 and "扫描件" in r.json()["detail"]
    r = up("改造方案.md", "# 网络改造\n\n核心交换机 10.1.20.1 管理口令 Abc@12345".encode())
    assert r.status_code == 422 and r.json()["detail"]["blocked"] is True
    a = _last_audit("project.document")
    assert a.result == "blocked" and "改造方案" not in a.detail                # 不记文件名
    from app.services import storage

    monkeypatch.setattr(storage, "enabled", lambda: False)
    assert up("值班.md", SOC_MD.encode()).status_code == 503
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        assert db.scalar(select(ProjectDocument).where(ProjectDocument.project_id == p.id)) is None


@on_s3
def test_document_upload_parse_and_read(proj):
    code, c, _ = proj
    data = docx_bytes([("Heading 1", "SOC 值班制度（证据测试）"), ("Normal", "实行 7×8 小时值班。"),
                       ("Heading 1", "告警分级"), ("Normal", "按业务影响分为四级。")],
                      table=[["级别", "响应时限"], ["一级", "15 分钟"]])
    r = c.post(f"/api/projects/{code}/documents", data={"title": "客户现行值班制度"},
               files={"file": ("BANK-A 值班制度.docx", data, "application/octet-stream")})
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["code"] == "D-01" and ev["status"] == "received" and ev["title"] == "客户现行值班制度"
    assert ev["filename"] == "BANK-A 值班制度.docx"
    dup = c.post(f"/api/projects/{code}/documents", files={"file": ("副本.docx", data, "application/octet-stream")})
    assert dup.status_code == 409 and "D-01" in dup.json()["detail"]

    with SessionLocal() as db:
        d = db.scalar(select(ProjectDocument).join(Project).where(Project.code == code))
        assert d.object_key.startswith(f"{code}/documents/") and "值班" not in d.object_key
        while jobs.run_once(db, "test-worker", kinds=["project.parse"]):
            pass
    detail = c.get(f"/api/projects/{code}/evidences/D-01").json()
    assert detail["status"] == "parsed" and detail["chars"] > 0
    assert any("一级 | 15 分钟" in s["text"] for s in detail["preview"])

    with SessionLocal() as db:
        user = db.scalar(select(User).join(ProjectMember).join(Project).where(Project.code == code))
        listed = tools.invoke(db, user, "research-interview", "project.evidence", {}, code)
        assert [e["code"] for e in listed["evidences"]] == ["D-01"]
        part = tools.invoke(db, user, "research-interview", "project.evidence",
                            {"code": "D-01", "limit": 10}, code)
        assert len(part["text"]) == 10 and part["next_offset"] == 10 and part["total_chars"] > 10
        rest = tools.invoke(db, user, "research-interview", "project.evidence",
                            {"code": "D-01", "offset": part["next_offset"]}, code)
        assert "next_offset" not in rest and "告警分级" in part["text"] + rest["text"]


def test_evidence_tool_for_agents(proj):
    code, c, name = proj
    c.post(f"/api/projects/{code}/notes", json=INTERVIEW)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        listed = tools.invoke(db, user, "compliance-basis", "project.evidence", {}, code)
        assert listed["evidences"][0]["code"] == "I-01" and "text" not in listed["evidences"][0]
        one = tools.invoke(db, user, "compliance-basis", "project.evidence", {"code": "I-01"}, code)
        assert one["text"] == INTERVIEW["content"] and one["interviewee_role"].endswith("值班负责人")
        assert "error" in tools.invoke(db, user, "compliance-basis", "project.evidence", {"code": "D-99"}, code)
        with pytest.raises(tools.ToolError):
            tools.invoke(db, user, "compliance-basis", "project.evidence", {"code": "I-01", "offset": "x"}, code)
        with pytest.raises(tools.ToolDenied):
            tools.invoke(db, user, "kb-builder", "project.evidence", {}, code)        # 知识侧 Agent 读不到项目区
        with pytest.raises(tools.ToolNotFound):
            tools.invoke(db, user, "compliance-basis", "project.evidence", {}, "MFG-C")  # 不是成员


def test_evidence_tool_strict_project_returns_nothing():
    code = _new_project("strict")
    name, *_ = create_user(("consultant",), {code: "member"})
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        r = tools.invoke(db, user, "report-writer", "project.evidence", {}, code)
    assert r["evidences"] == [] and "摘要卡" in r["note"]


def test_codes_are_never_reused():
    """编号按已有最大序号递增：中间缺号（以后删除管理会删记录）也不会复用旧编号。"""
    code = _new_project()
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        db.add(Evidence(project_id=p.id, code="O-07", kind="observation", seq=7, title="t", created_by="t"))
        db.commit()
    c, _ = _member_client(code)
    try:
        r = c.post(f"/api/projects/{code}/notes", json={"kind": "observation", "title": "巡查", "content": "x"})
        assert r.json()["code"] == "O-08"
    finally:
        c.__exit__(None, None, None)
