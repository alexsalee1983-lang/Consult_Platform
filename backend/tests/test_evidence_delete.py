"""证据改名、删除（二期一期待办，app/project/evidence.py rename_evidence / delete_evidence）。

录入人或项目经理可以改名、删除；删除后编号保留不复用，标题、正文、文件和解析结果清空，同一文件可以重新上传
（分配新编号）；Agent 读不到；交付件写入引用 422；已有引用在签署门禁和质量评审平台规则里判阻断；
已签署交付件引用的不能删；审计不记标题和正文。"""
import json

from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Deliverable, Project, User
from app.project import quality
from app.services import jobs

from .conftest import create_user, logged_in_client
from .test_local_only import DOC, _parse, team  # noqa: F401  （复用资料和项目 fixture）
from .test_quality import model  # noqa: F401

TITLE = "值班制度（改名测试）"


def _logs(code: str, action: str) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.project_code == code, AuditLog.action == action)
                               .order_by(AuditLog.id)))


def _upload(c, code: str, name: str = "值班制度.docx"):
    return c.post(f"/api/projects/{code}/documents", files={"file": (name, DOC, "application/octet-stream")})


def test_rename_by_owner_or_manager(team):  # noqa: F811
    code, c, pm, member, _ = team
    assert c.post(f"/api/projects/{code}/notes", json={"kind": "interview", "title": "访谈 SOC 负责人",
                                                       "interviewee_role": "SOC 负责人",
                                                       "content": "夜间值班一人。"}).status_code == 201
    other = logged_in_client(app, create_user(("consultant",), {code: "member"}))
    try:
        url = f"/api/projects/{code}/evidences/I-01"
        assert other.patch(url, json={"title": "别人改"}).status_code == 403     # 不是录入人也不是项目经理
        assert c.patch(url, json={"title": "   "}).status_code == 422
        assert c.patch(url, json={"title": "口令 Passw0rd!2026"}).status_code == 422   # 机密级
        r = c.patch(url, json={"title": TITLE})
        assert r.status_code == 200 and r.json()["title"] == TITLE and r.json()["code"] == "I-01"
        assert pm.patch(url, json={"title": "经理改的标题"}).status_code == 200
        assert c.patch(f"/api/projects/{code}/evidences/I-09", json={"title": "x"}).status_code == 404
    finally:
        other.__exit__(None, None, None)
    logs = _logs(code, "project.evidence.rename")
    assert {a.result for a in logs} == {"allowed", "denied", "blocked"}
    assert all(TITLE not in (a.detail or "") and "Passw0rd" not in (a.detail or "") for a in logs)


def test_delete_document_keeps_code_and_allows_reupload(team):  # noqa: F811
    code, c, pm, member, store = team
    assert _upload(c, code).status_code == 201
    _parse()
    assert len(store.objects) == 2
    other = logged_in_client(app, create_user(("consultant",), {code: "member"}))
    try:
        assert other.delete(f"/api/projects/{code}/evidences/D-01").status_code == 403
    finally:
        other.__exit__(None, None, None)
    r = c.delete(f"/api/projects/{code}/evidences/D-01")
    assert r.status_code == 200, r.text
    ev = r.json()
    assert ev["code"] == "D-01" and ev["status"] == "deleted" and ev["title"] == "" and ev["filename"] == ""
    assert ev["deleted_by"] == member and ev["deleted_at"]
    assert store.objects == {}                                          # 原件和解析结果已删
    assert c.delete(f"/api/projects/{code}/evidences/D-01").status_code == 409
    assert c.patch(f"/api/projects/{code}/evidences/D-01", json={"title": "x"}).status_code == 409
    detail = c.get(f"/api/projects/{code}/evidences/D-01").json()
    assert detail["preview"] == [] and detail["content"] == ""

    again = _upload(c, code)                                            # 同一文件可以重新上传，分配新编号
    assert again.status_code == 201 and again.json()["code"] == "D-02"

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == member))
        listed = tools.invoke(db, user, "research-interview", "project.evidence", {}, code)
        d1 = next(e for e in listed["evidences"] if e["code"] == "D-01")
        assert d1["status"] == "deleted" and "已删除" in d1["note"]
        one = tools.invoke(db, user, "research-interview", "project.evidence", {"code": "D-01"}, code)
        assert one["text"] == "" and "已删除" in one["error"]
    logs = _logs(code, "project.evidence.delete")
    assert {a.result for a in logs} == {"allowed", "denied"}
    assert all("值班制度" not in (a.detail or "") for a in logs)
    allowed = json.loads(next(a for a in logs if a.result == "allowed").detail)
    assert allowed["files"] == 2 and allowed["evidence"] == "D-01"


def test_delete_before_parse_writes_nothing(team):  # noqa: F811
    code, c, _, _, store = team
    assert _upload(c, code).status_code == 201
    assert c.delete(f"/api/projects/{code}/evidences/D-01").status_code == 200
    _parse()                                                            # 排队中的解析任务：不再解析、不写结果
    assert store.objects == {}


def test_delete_note_and_local_only_refused(team):  # noqa: F811
    code, c, pm, _, _ = team
    assert c.post(f"/api/projects/{code}/notes", json={"kind": "observation", "title": "巡查",
                                                       "content": "交接记录为纸质。"}).status_code == 201
    r = pm.delete(f"/api/projects/{code}/evidences/O-01")              # 项目经理可以删别人录的
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert c.get(f"/api/projects/{code}/evidences/O-01").json()["content"] == ""
    assert c.post(f"/api/projects/{code}/notes", json={"kind": "observation", "title": "巡查二",
                                                       "content": "另一条观察。"}).json()["code"] == "O-02"
    assert _upload(c, code).status_code == 201
    assert pm.post(f"/api/projects/{code}/evidences/D-01/local-only", json={"request_ref": "现场函-1"}).status_code == 200
    assert pm.delete(f"/api/projects/{code}/evidences/D-01").status_code == 422   # 仅现场使用的不需要删


def test_citations_after_delete(team, model):  # noqa: F811
    code, c, pm, _, _ = team
    assert c.post(f"/api/projects/{code}/notes", json={"kind": "observation", "title": "巡查",
                                                       "content": "交接记录为纸质。"}).status_code == 201
    base = f"/api/projects/{code}/deliverables/D4"
    assert c.put(f"{base}/outline", json={"titles": ["现状"]}).status_code == 200
    assert c.put(f"{base}/sections/1", json={"body": "交接记录为纸质 [O-01]。"}).status_code == 201
    assert c.delete(f"/api/projects/{code}/evidences/O-01").status_code == 200   # 未签署的引用不挡删除

    r = c.put(f"{base}/sections/1", json={"body": "交接记录为纸质 [O-01]，夜间无人。"})
    assert r.status_code == 422 and "O-01（已删除）" in r.json()["detail"]
    with SessionLocal() as db:
        d = db.scalar(select(Deliverable).join(Project).where(Project.code == code, Deliverable.code == "D4"))
        assert any("O-01（已删除）" in x for x in quality.gate(db, d).reasons)
    rv = c.post(f"{base}/reviews")
    assert rv.status_code == 202
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=[quality.JOB_KIND]):
            pass
    out = c.get(f"{base}/reviews/{rv.json()['id']}").json()
    rule = [i for i in out["issues"] if i["by"] == "rule" and "已删除" in i["problem"]]
    assert rule and rule[0]["severity"] == "阻断" and rule[0]["dimension"] == "证据链"


def test_signed_citation_blocks_delete(team):  # noqa: F811
    code, c, pm, _, _ = team
    assert c.post(f"/api/projects/{code}/notes", json={"kind": "observation", "title": "巡查",
                                                       "content": "交接记录为纸质。"}).status_code == 201
    base = f"/api/projects/{code}/deliverables/D4"
    assert c.put(f"{base}/outline", json={"titles": ["现状"]}).status_code == 200
    assert c.put(f"{base}/sections/1", json={"body": "交接记录为纸质 [O-01]。"}).status_code == 201
    with SessionLocal() as db:                                          # 签署流程在 test_quality 里测，这里直接置状态
        d = db.scalar(select(Deliverable).join(Project).where(Project.code == code, Deliverable.code == "D4"))
        d.status = "signed"
        db.commit()
    r = pm.delete(f"/api/projects/{code}/evidences/O-01")
    assert r.status_code == 409 and "D4" in r.json()["detail"]
    assert c.get(f"/api/projects/{code}/evidences/O-01").json()["content"] == "交接记录为纸质。"
    assert any(a.result == "blocked" for a in _logs(code, "project.evidence.delete"))
