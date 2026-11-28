"""四期 A33：输出包组件清单与输出页、阶段包导出、未签草稿水印（docs/交付链输入输出方案.md 第六节、8.1）。"""
import io
import json
import zipfile

import pytest
from docx import Document
from sqlalchemy import select

from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Deliverable, Project

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project


@pytest.fixture()
def proj():
    code = _new_project()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    yield code, c
    c.__exit__(None, None, None)


def test_outputs_lists_component_packs_by_stage(proj):
    code, c = proj
    r = c.get(f"/api/projects/{code}/outputs")
    assert r.status_code == 200, r.text
    data = r.json()
    packs = {d["code"]: d for st in data["stages"] for d in st["deliverables"]}
    assert set(packs) == {f"D{i}" for i in range(1, 9)}
    d1 = packs["D1"]
    assert d1["stage"] == "启动" and d1["status"] == "not_started" and d1["signed"] is False
    c01 = next(x for x in d1["components"] if x["id"] == "D1-C01")
    assert c01["name"] == "范围确认单" and c01["format"] == "docx" and c01["client_signs"] is True
    assert any(x["truth"] for x in packs["D4"]["components"])
    assert any(not x["default"] for x in packs["D5"]["components"])        # 可选组件也列出，标 default=false
    assert [s["stage"] for s in data["stages"]][0] == "启动"
    assert data["closeout"] and data["closeout"][0]["id"] == "R-C01"


def test_outputs_marks_signed_readonly(proj):
    code, c = proj
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        d = db.scalar(select(Deliverable).where(Deliverable.project_id == p.id, Deliverable.code == "D3"))
        d.status = "signed"
        db.commit()
    packs = {d["code"]: d for st in c.get(f"/api/projects/{code}/outputs").json()["stages"] for d in st["deliverables"]}
    assert packs["D3"]["signed"] is True and packs["D4"]["signed"] is False


def test_stage_export_zip_drafts_and_skips(proj):
    code, c = proj
    c.put(f"{_base(code, 'D1')}/outline", json={"titles": ["项目概况"]})
    assert c.put(f"{_base(code, 'D1')}/sections/1", json={"body": "范围为 SOC 运营体系。"}).status_code == 201
    r = c.get(f"/api/projects/{code}/outputs/export", params={"stage": "启动"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    docx_name = next(n for n in names if n.endswith(".docx"))
    assert "D1" in docx_name and "草稿" in docx_name
    doc = Document(io.BytesIO(z.read(docx_name)))
    assert "草稿" in "".join(p.text for p in doc.sections[0].header.paragraphs)              # 未签草稿水印沿用单件导出
    manifest = z.read("清单.txt").decode("utf-8")
    assert "D1" in manifest and "D-" not in manifest
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "outputs.stage_export",
                                              AuditLog.project_code == code)).all()
    assert len(a) == 1
    detail = json.loads(a[0].detail)
    assert detail["stage"] == "启动" and detail["exported"] >= 1 and "范围为" not in a[0].detail


def test_stage_export_unknown_stage_and_empty(proj):
    code, c = proj
    assert c.get(f"/api/projects/{code}/outputs/export", params={"stage": "不存在"}).status_code == 404
    r = c.get(f"/api/projects/{code}/outputs/export", params={"stage": "分析"})   # 没有大纲：全部跳过
    assert r.status_code == 409


def test_outputs_non_member_404():
    code = _new_project()
    user = create_user(("consultant",), {})
    with logged_in_client(app, user) as c:
        assert c.get(f"/api/projects/{code}/outputs").status_code == 404
        assert c.get(f"/api/projects/{code}/outputs/export", params={"stage": "启动"}).status_code == 404
