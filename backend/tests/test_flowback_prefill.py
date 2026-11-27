"""二期 P16 经验回流预填（人工环节复查，2026-11-15 负责人同意排 v2.0）。

投料时可选填来源项目（只能是自己所在的项目，否则 404），记在 kb_uploads.source_project（知识区迁移 k0009）；
删除确认页按来源项目列出去标识已通过的投料编号作为预填（flowback_suggested），负责人只需确认；
项目标了“客户不同意回流”的不预填。投料列表、详情不返回来源项目（知识侧不看项目代号）。
"""
import json
import secrets
from datetime import date

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT, KNOWLEDGE_EXPERT, PROJECT_LEAD
from app.main import app
from app.models import AuditLog, KbUpload
from app.services import storage

from .conftest import create_user, logged_in_client
from .kb_samples import SOC_MD


def _project(manager: str) -> str:
    code = f"FB-{secrets.token_hex(3).upper()}"
    assert cli_main(["create-project", "--code", code, "--tier", "standard", "--manager", manager]) == 0
    return code


def _upload_row(code: str, status: str, who: str) -> int:
    with SessionLocal() as db:
        up = KbUpload(object_key=f"fbtest/{secrets.token_hex(6)}", filename="x.md", sha256=secrets.token_hex(32),
                      uploaded_by=who, guard_categories=[], status=status, source_project=code)
        db.add(up)
        db.commit()
        return up.id


@pytest.fixture()
def no_storage_io(monkeypatch):
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "put", lambda *a, **k: None)


def test_intake_records_source_project_for_members_only(no_storage_io):
    pm, other, expert = create_user((CONSULTANT,)), create_user((CONSULTANT,)), create_user((KNOWLEDGE_EXPERT,))
    code = _project(pm[0])
    c, o, e = logged_in_client(app, pm), logged_in_client(app, other), logged_in_client(app, expert)
    try:
        data = (SOC_MD + f"\n\n回流测试 {secrets.token_hex(4)}\n").encode()
        files = {"file": ("回流.md", data, "text/markdown")}
        assert o.post("/api/kb/intake", files=files, data={"project": code}).status_code == 404   # 不是成员
        assert c.post("/api/kb/intake", files=files, data={"project": "NO-SUCH"}).status_code == 404
        r = c.post("/api/kb/intake", files=files, data={"project": code})
        assert r.status_code == 201, r.text
        up_id = r.json()["id"]
        assert code not in r.text
        assert code not in e.get("/api/kb/intake").text and code not in e.get(f"/api/kb/intake/{up_id}").text
        r2 = c.post("/api/kb/intake", files={"file": ("无项目.md", (SOC_MD + secrets.token_hex(4)).encode(),
                                                     "text/markdown")})
        assert r2.status_code == 201                                                  # 不填照旧
    finally:
        for x in (c, o, e):
            x.__exit__(None, None, None)
    with SessionLocal() as db:
        assert db.get(KbUpload, up_id).source_project == code
        assert db.get(KbUpload, r2.json()["id"]).source_project == ""
        log = db.scalars(select(AuditLog).where(AuditLog.action == "kb.intake", AuditLog.result == "allowed")
                         .order_by(AuditLog.id.desc())).all()
    assert any(json.loads(a.detail).get("upload_id") == up_id and a.project_code == code for a in log)


def test_deletion_detail_prefills_flowback_refs(monkeypatch):
    monkeypatch.setattr(storage, "enabled", lambda: False)
    pm, lead = create_user((CONSULTANT,)), create_user((PROJECT_LEAD,))
    code, other = _project(pm[0]), _project(pm[0])
    passed = [_upload_row(code, "deid_passed", pm[0]) for _ in range(2)]
    _upload_row(code, "deid_pending", pm[0])
    _upload_row(code, "deid_rejected", pm[0])
    _upload_row(other, "deid_passed", pm[0])
    c, ld = logged_in_client(app, pm), logged_in_client(app, lead)
    try:
        r = c.post(f"/api/projects/{code}/deletions", json={"request_ref": "删函-FB-1",
                                                           "received_on": date.today().isoformat()})
        assert r.status_code == 201, r.text
        d = r.json()
        assert d["flowback_suggested"] == sorted(passed)
        assert ld.get(f"/api/deletions/{d['id']}").json()["flowback_suggested"] == sorted(passed)
        # 标了客户不同意回流就不预填
        assert c.put(f"/api/projects/{code}/flowback", json={"allowed": False}).status_code == 200
        assert ld.get(f"/api/deletions/{d['id']}").json()["flowback_suggested"] == []
        assert c.put(f"/api/projects/{code}/flowback", json={"allowed": True}).status_code == 200
        r = ld.post(f"/api/deletions/{d['id']}/confirm", json={"flowback": "done", "flowback_refs": passed})
        assert r.status_code == 200 and r.json()["flowback_refs"] == sorted(passed)
    finally:
        for x in (c, ld):
            x.__exit__(None, None, None)
