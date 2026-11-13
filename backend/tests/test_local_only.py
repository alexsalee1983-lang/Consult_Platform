"""M4 ③：单个资料“仅现场使用”（app/project/evidence.py mark_local_only）。

项目经理标注 → 原件和解析结果从对象存储删除、文件名清空、编号保留；同一文件不能再上传；Agent 读不到；
交付件写入引用被拒；评审前已有的引用：签署门禁和质量评审平台规则判阻断；审计不记文号。"""
import json
import secrets

import pytest
from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Deliverable, Project, ProjectDocument, User
from app.project import quality
from app.seed import DELIVERABLES
from app.services import jobs, storage

from .conftest import create_user, logged_in_client
from .kb_samples import docx_bytes
from .test_quality import model  # noqa: F401  （复用模拟模型 fixture）

REF = "现场函-2026-03"
DOC = docx_bytes([("Heading 1", "值班制度（仅现场测试）"), ("Normal", "实行 7×8 小时值班。")])


class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def install(self, mp):
        mp.setattr(storage, "enabled", lambda: True)
        mp.setattr(storage, "put", lambda area, key, data, ct="": self.objects.__setitem__(f"{area}:{key}", data))
        mp.setattr(storage, "get", lambda area, key: self.objects[f"{area}:{key}"])
        mp.setattr(storage, "delete", lambda area, key: self.objects.pop(f"{area}:{key}", None))


@pytest.fixture()
def team(monkeypatch):
    store = _Store()
    store.install(monkeypatch)
    code = f"LO-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    member, manager = create_user(("consultant",), {code: "member"}), create_user(("consultant",), {code: "manager"})
    c, pm = logged_in_client(app, member), logged_in_client(app, manager)
    yield code, c, pm, member[0], store
    c.__exit__(None, None, None)
    pm.__exit__(None, None, None)


def _parse() -> None:
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=["project.parse"]):
            pass


def test_mark_local_only_removes_file_and_blocks_reupload_and_reading(team):
    code, c, pm, member, store = team
    r = c.post(f"/api/projects/{code}/documents", files={"file": ("值班制度.docx", DOC, "application/octet-stream")})
    assert r.status_code == 201 and r.json()["code"] == "D-01"
    _parse()
    assert len(store.objects) == 2                                      # 原件 + 解析结果
    assert c.post(f"/api/projects/{code}/notes", json={"kind": "observation", "title": "巡查",
                                                       "content": "交接记录为纸质。"}).status_code == 201

    url = f"/api/projects/{code}/evidences/D-01/local-only"
    assert c.post(url, json={"request_ref": REF}).status_code == 403   # 只有项目经理
    assert pm.post(url, json={"request_ref": " "}).status_code == 422
    assert pm.post(url, json={"request_ref": "口令 Passw0rd!2026"}).status_code == 422
    assert pm.post(f"/api/projects/{code}/evidences/O-01/local-only", json={"request_ref": REF}).status_code == 422
    assert pm.post(f"/api/projects/{code}/evidences/D-09/local-only", json={"request_ref": REF}).status_code == 404
    r = pm.post(url, json={"request_ref": REF})
    assert r.status_code == 200, r.text
    ev = r.json()
    assert ev["local_only"] and ev["status"] == "local_only" and ev["filename"] == "" and ev["local_only_ref"] == REF
    assert store.objects == {}                                          # 文件已删
    assert pm.post(url, json={"request_ref": REF}).status_code == 409
    assert c.get(f"/api/projects/{code}/evidences/D-01").json()["preview"] == []

    again = c.post(f"/api/projects/{code}/documents", files={"file": ("改名.docx", DOC, "application/octet-stream")})
    assert again.status_code == 422 and "D-01" in again.json()["detail"]
    assert store.objects == {}

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == member))
        listed = tools.invoke(db, user, "research-interview", "project.evidence", {}, code)
        d1 = next(e for e in listed["evidences"] if e["code"] == "D-01")
        assert d1["status"] == "local_only" and "仅现场使用" in d1["note"]
        one = tools.invoke(db, user, "research-interview", "project.evidence", {"code": "D-01"}, code)
        assert one["text"] == "" and "仅现场使用" in one["error"]
        d = db.scalar(select(ProjectDocument).join(Project).where(Project.code == code))
        assert d.parsed_key == "" and d.local_only_by and d.sha256
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action == "project.document.local_only")).all()
    assert {a.result for a in logs} >= {"allowed", "denied", "blocked"}
    assert all(REF not in (a.detail or "") for a in logs)


def test_parse_after_mark_writes_nothing(team):
    code, c, pm, _, store = team
    assert c.post(f"/api/projects/{code}/documents",
                  files={"file": ("值班制度.docx", DOC, "application/octet-stream")}).status_code == 201
    assert pm.post(f"/api/projects/{code}/evidences/D-01/local-only", json={"request_ref": REF}).status_code == 200
    _parse()                                                            # 排队中的解析任务：不再解析、不写结果
    assert store.objects == {}
    with SessionLocal() as db:
        d = db.scalar(select(ProjectDocument).join(Project).where(Project.code == code))
        assert d.status == "local_only" and d.parsed_key == ""


def test_citations_refused_and_gate_blocks(team, model):  # noqa: F811
    code, c, pm, _, _ = team
    assert c.post(f"/api/projects/{code}/documents",
                  files={"file": ("值班制度.docx", DOC, "application/octet-stream")}).status_code == 201
    _parse()
    base = f"/api/projects/{code}/deliverables/D4"
    assert c.put(f"{base}/outline", json={"titles": ["现状"]}).status_code == 200
    assert c.put(f"{base}/sections/1", json={"body": "现行值班为 7×8 小时 [D-01]。"}).status_code == 201

    assert pm.post(f"/api/projects/{code}/evidences/D-01/local-only", json={"request_ref": REF}).status_code == 200
    r = c.put(f"{base}/sections/1", json={"body": "现行值班为 7×8 小时 [D-01-2]，夜间无人。"})
    assert r.status_code == 422 and "D-01" in r.json()["detail"]

    with SessionLocal() as db:                                          # 已有引用：签署门禁实时拦
        d = db.scalar(select(Deliverable).join(Project).where(Project.code == code, Deliverable.code == "D4"))
        assert any("仅现场使用" in x for x in quality.gate(db, d).reasons)

    rv = c.post(f"{base}/reviews")
    assert rv.status_code == 202
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=[quality.JOB_KIND]):
            pass
    out = c.get(f"{base}/reviews/{rv.json()['id']}").json()
    rule = [i for i in out["issues"] if i["by"] == "rule" and "仅现场使用" in i["problem"]]
    assert rule and rule[0]["severity"] == "阻断"
    with SessionLocal() as db:
        blocked = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                    AuditLog.action == "deliverable.write",
                                                    AuditLog.result == "blocked")).all()
    assert any(json.loads(a.detail).get("reason") == "local_only" for a in blocked)
