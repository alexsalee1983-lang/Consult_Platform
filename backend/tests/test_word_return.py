"""四期 A32①：Word 回传。顾问把本平台导出的 Word 改完传回，平台按导出编号找到导出时各章版本，
逐章比对，改过的章新建版本（修订痕迹 = 与导出时版本的差异），并自动发起质量评审（增量评审只看改过的章）；
非本平台导出的文件、别的交付件导出的文件拒收，机密级命中不保存，导出后平台里和 Word 里都改了同一章的报冲突，
非项目成员 404，审计不记正文。"""
import io
import json

import pytest
from docx import Document
from sqlalchemy import select

from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, QualityReview

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project

BODY1 = ("## 人员与排班\n\nSOC 现有 6 人，全部白班 [推断]。\n\n- 交接班记录为纸质 **需要整改**\n- 无升级流程\n\n"
         "| 项目 | 现状 |\n|---|---|\n| 值班 | 白班 |")
BODY2 = "夜间无人值守 [推断]。\n\n建议设立夜班。"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture()
def proj():
    code = _new_project()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    assert c.put(f"{_base(code)}/outline", json={"titles": ["现状梳理", "差距分析", "整改建议"]}).status_code == 200
    assert c.put(f"{_base(code)}/sections/1", json={"body": BODY1}).status_code == 201
    assert c.put(f"{_base(code)}/sections/2", json={"body": BODY2}).status_code == 201
    yield code, c, user[0]
    c.__exit__(None, None, None)


def _export(c, code: str, d: str = "D4") -> Document:
    r = c.get(f"{_base(code, d)}/export")
    assert r.status_code == 200, r.text
    return Document(io.BytesIO(r.content))


def _bytes(doc: Document) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _upload(c, code: str, data: bytes, d: str = "D4", name: str = "回传.docx"):
    return c.post(f"{_base(code, d)}/word-return", files={"file": (name, data, DOCX)})


def _para(doc: Document, text: str):
    return next(p for p in doc.paragraphs if p.text == text)


def _replace(doc: Document, old: str, new: str) -> None:
    p = _para(doc, old)
    for r in p.runs[1:]:
        r._r.getparent().remove(r._r)
    p.runs[0].text = new


def test_round_trip_creates_new_version_with_trace_and_rereview(proj):
    code, c, name = proj
    doc = _export(c, code)
    _replace(doc, "建议设立夜班。", "建议设立夜班，并建立升级流程。")
    _para(doc, "夜间无人值守 [推断]。").insert_paragraph_before("夜间告警无人响应。")
    r = _upload(c, code, _bytes(doc))
    assert r.status_code == 200, r.text
    out = r.json()
    assert [x["seq"] for x in out["changed"]] == [2]
    ch = out["changed"][0]
    assert ch["from_version"] == 1 and ch["version"] == 2
    assert any(line.startswith("+") and "升级流程" in line for line in ch["diff"])   # 修订痕迹
    assert any(line.startswith("-") and "建议设立夜班。" in line for line in ch["diff"])
    assert 1 in out["unchanged"] and 3 in out["unchanged"]

    s2 = c.get(f"{_base(code)}/sections/2").json()
    assert s2["current_version"] == 2 and "夜间告警无人响应。" in s2["body"] and "并建立升级流程" in s2["body"]
    assert "Word 回传" in s2["note"] and s2["author"] == name
    s1 = c.get(f"{_base(code)}/sections/1").json()
    assert s1["current_version"] == 1                                    # 没改的章不新建版本（加粗、表格原样保留）

    assert out["review"] and out["review"]["status"] == "queued"          # 改动处重新过门禁
    with SessionLocal() as db:
        rv = db.get(QualityReview, out["review"]["id"])
        assert rv.versions["2"] == 2
        a = db.scalars(select(AuditLog).where(AuditLog.action == "deliverable.word_return",
                                              AuditLog.project_code == code)).all()[-1]
        assert a.result == "allowed" and "升级流程" not in a.detail and json.loads(a.detail)["changed"] == [2]


def test_bold_and_unchanged_text_kept_when_other_lines_change(proj):
    code, c, _ = proj
    doc = _export(c, code)
    _para(doc, "无升级流程").runs[0].text = "无升级流程，事件靠电话通知"
    assert _upload(c, code, _bytes(doc)).status_code == 200
    body = c.get(f"{_base(code)}/sections/1").json()["body"]
    assert "**需要整改**" in body and "- 无升级流程，事件靠电话通知" in body and "| 值班 | 白班 |" in body


def test_no_change_and_wrong_file_rejected(proj):
    code, c, _ = proj
    doc = _export(c, code)
    r = _upload(c, code, _bytes(doc))
    assert r.status_code == 409 and "没有改动" in r.json()["detail"]

    plain = Document()                                                    # 不是本平台导出的
    plain.add_paragraph("1　现状梳理")
    r = _upload(c, code, _bytes(plain))
    assert r.status_code == 422 and "本平台导出" in r.json()["detail"]

    forged = _export(c, code)                                             # 编号改成不存在的
    forged.core_properties.identifier = "CP-EXPORT-00000000"
    for s in forged.sections:
        for p in s.footer.paragraphs:
            for run in p.runs:
                run.text = run.text.replace("导出编号", "编号")
    r = _upload(c, code, _bytes(forged))
    assert r.status_code == 422

    r = _upload(c, code, b"not a docx", name="x.docx")
    assert r.status_code == 422

    other = _export(c, code)                                              # 别的交付件导出的文件
    c.put(f"{_base(code, 'D5')}/outline", json={"titles": ["甲"]})
    r = _upload(c, code, _bytes(other), d="D5")
    assert r.status_code == 422 and "本平台导出" in r.json()["detail"]


def test_footer_export_id_is_enough_when_properties_lost(proj):
    code, c, _ = proj
    doc = _export(c, code)
    doc.core_properties.identifier = ""
    _replace(doc, "建议设立夜班。", "建议设立双人夜班。")
    assert _upload(c, code, _bytes(doc)).status_code == 200


def test_guard_hit_rejected_and_nothing_saved(proj):
    code, c, _ = proj
    doc = _export(c, code)
    _replace(doc, "建议设立夜班。", "核心交换机管理口令 Abc@12345")
    r = _upload(c, code, _bytes(doc))
    assert r.status_code == 422 and r.json()["detail"]["blocked"] is True
    assert c.get(f"{_base(code)}/sections/2").json()["current_version"] == 1
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "deliverable.word_return",
                                              AuditLog.project_code == code)).all()[-1]
        assert a.result == "blocked" and "Abc@12345" not in a.detail


def test_conflict_when_platform_changed_same_section_after_export(proj):
    code, c, _ = proj
    doc = _export(c, code)
    assert c.put(f"{_base(code)}/sections/2", json={"body": "平台里改过。"}).status_code == 201
    _replace(doc, "建议设立夜班。", "Word 里也改过。")
    r = _upload(c, code, _bytes(doc))
    assert r.status_code == 409 and "第 2 章" in r.json()["detail"]
    assert c.get(f"{_base(code)}/sections/2").json()["current_version"] == 2


def test_empty_section_written_in_word(proj):
    code, c, _ = proj
    doc = _export(c, code)
    _replace(doc, "（本章还没有正文）", "第三章在 Word 里补写的整改建议。")
    r = _upload(c, code, _bytes(doc))
    assert r.status_code == 200 and [x["seq"] for x in r.json()["changed"]] == [3]
    assert c.get(f"{_base(code)}/sections/3").json()["body"] == "第三章在 Word 里补写的整改建议。"


def test_non_member_404_and_strict_refused(proj):
    code, c, _ = proj
    data = _bytes(_export(c, code))
    outsider = logged_in_client(app, create_user(("consultant",)))
    try:
        assert _upload(outsider, code, data).status_code == 404
    finally:
        outsider.__exit__(None, None, None)

    strict = _new_project("strict")
    sc = logged_in_client(app, create_user(("consultant",), {strict: "member"}))
    try:
        r = _upload(sc, strict, data)
        assert r.status_code == 422 and "严格档" in r.json()["detail"]
    finally:
        sc.__exit__(None, None, None)
