"""交付件 Word 导出（M3 ⑥）：内置模板与配置模板、占位符、草稿页眉与页脚留痕、来源脚注、Markdown 排版、
严格档与无大纲拒绝、模板缺失 503、成员隔离、审计不记正文。"""
import io
import json
import zipfile

import pytest
from docx import Document
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Deliverable, Project

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project, _publish_kb


@pytest.fixture()
def proj():
    code = _new_project()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    yield code, c, user[0]
    c.__exit__(None, None, None)


def _docx(r) -> tuple[Document, zipfile.ZipFile]:
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml")
    return Document(io.BytesIO(r.content)), zipfile.ZipFile(io.BytesIO(r.content))


def _text(doc: Document) -> str:
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        parts += [c.text for row in t.rows for c in row.cells]
    return "\n".join(parts)


def _d(a: AuditLog) -> dict:
    return json.loads(a.detail)


def _export_audits(code: str) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == "deliverable.export",
                                                      AuditLog.project_code == code).order_by(AuditLog.id)))


def test_export_builtin_template_with_footnotes_and_markdown(proj):
    code, c, name = proj
    c.post(f"/api/projects/{code}/notes", json={"kind": "interview", "title": "值班访谈",
                                                "interviewee_role": "SOC 负责人", "content": "全部白班。"})
    doc_id = _publish_kb("导出脚注测试依据")
    c.put(f"{_base(code)}/outline", json={"titles": ["现状梳理", "差距分析", "整改建议"]})
    body1 = ("## 人员与排班\n\nSOC 现有 6 人，全部白班 [I-01-02]，夜间无人值守 [I-01-02]。\n"
             "- 交接班记录为纸质 **需要整改**\n- 无升级流程 [D-09]\n\n"
             "| 项目 | 现状 | 来源 |\n|---|---|---|\n| 值班 | 白班 | [I-01] |\n\n"
             f"依据【依据：GB/T 22239 2019 8.1.5｜{doc_id}】。二期扩编 [推断]。")
    assert c.put(f"{_base(code)}/sections/1", json={"body": body1}).status_code == 201
    assert c.put(f"{_base(code)}/sections/2", json={"body": "1. 缺少夜间值守\n2. 缺少升级 [I-01-02]"}).status_code == 201

    r = c.get(f"{_base(code)}/export")
    doc, z = _docx(r)
    assert "%E8%8D%89%E7%A8%BF" in r.headers["content-disposition"]          # 文件名带“草稿”（UTF-8 编码）
    text = _text(doc)
    assert "1　现状梳理" in text and "2　差距分析" in text and "3　整改建议" in text
    assert "（本章还没有正文）" in text                                       # 第 3 章没写
    assert "SOC 现有 6 人，全部白班 [I-01-02]" in text and "需要整改" in text and "**" not in text
    styles = {p.text: p.style.name for p in doc.paragraphs}
    assert styles["人员与排班"] == "Heading 3" and styles["1　现状梳理"] == "Heading 1"
    assert styles["交接班记录为纸质 需要整改"] == "List Bullet" and styles["缺少夜间值守"] == "List Number"
    assert len(doc.tables) == 1 and doc.tables[0].cell(1, 1).text == "白班"

    notes = z.read("word/footnotes.xml").decode("utf-8")
    assert "I-01-02：访谈纪要《值班访谈》（受访者：SOC 负责人）" in notes
    assert notes.count("I-01-02：") == 2                                      # 每章第一次出现各一次，第 1 章重复的不再加
    assert "D-09：未核对到" in notes and f"{doc_id}：知识库 L4 依据《导出脚注测试依据》" in notes
    assert "推断" not in notes
    assert "footnotes" in z.read("word/_rels/document.xml.rels").decode("utf-8")
    assert "footnotes+xml" in z.read("[Content_Types].xml").decode("utf-8")

    header = "\n".join(p.text for p in doc.sections[0].header.paragraphs)
    footer = "\n".join(p.text for p in doc.sections[0].footer.paragraphs)
    assert "草稿" in header
    export_id = r.headers["x-export-id"]
    assert f"导出：{name}" in footer and f"导出编号 {export_id}" in footer

    (a,) = _export_audits(code)
    assert a.result == "allowed" and _d(a)["export_id"] == export_id and _d(a)["draft"] is True
    assert _d(a)["template"] == "builtin" and _d(a)["versions"] == {"1": 1, "2": 1, "3": 0}
    assert _d(a)["footnotes"] == 5 and _d(a)["size"] == len(r.content)
    assert "人员与排班" not in a.detail and "现状梳理" not in a.detail


def test_signed_has_no_draft_mark(proj):
    code, c, _ = proj
    c.put(f"{_base(code)}/outline", json={"titles": ["现状"]})
    c.put(f"{_base(code)}/sections/1", json={"body": "正文。"})
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        d = db.scalar(select(Deliverable).where(Deliverable.project_id == p.id, Deliverable.code == "D4"))
        d.status = "signed"
        db.commit()
    r = c.get(f"{_base(code)}/export")
    doc, _ = _docx(r)
    assert all("草稿" not in p.text for p in doc.sections[0].header.paragraphs)
    assert "%E8%8D%89%E7%A8%BF" not in r.headers["content-disposition"]
    assert _d(_export_audits(code)[-1])["draft"] is False


def test_configured_template_placeholders_and_body_anchor(proj, tmp_path, monkeypatch):
    code, c, _ = proj
    tpl = Document()
    tpl.add_paragraph("公司封面：{{项目代号}} {{交付件编号}} {{交付件名称}} {{修订}}")
    tpl.add_paragraph("{{正文}}")
    tpl.add_paragraph("公司封底")
    tpl.sections[0].header.paragraphs[0].text = "公司页眉 {{项目代号}}"
    path = tmp_path / "company.docx"
    tpl.save(path)
    monkeypatch.setattr(get_settings(), "export_template", str(path))

    c.put(f"{_base(code)}/outline", json={"titles": ["现状"]})
    c.put(f"{_base(code)}/sections/1", json={"body": "正文段落。\n\n| 甲 | 乙 |\n|---|---|\n| 1 | 2 |"})
    doc, _ = _docx(c.get(f"{_base(code)}/export"))
    paras = [p.text for p in doc.paragraphs]
    assert paras[0] == f"公司封面：{code} D4 现状评估报告 v2"
    assert paras[-1] == "公司封底" and "{{正文}}" not in paras
    assert paras.index("1　现状") < paras.index("正文段落。") < paras.index("公司封底")
    body = doc.element.body                                              # 表格也插在封底之前
    tags = [el.tag.split("}")[1] for el in body]
    assert tags.index("tbl") < len(tags) - 2
    header = [p.text for p in doc.sections[0].header.paragraphs]
    assert header[0] == f"公司页眉 {code}" and "草稿" in "".join(header)
    assert _d(_export_audits(code)[-1])["template"] == "configured"
    assert str(path) not in _export_audits(code)[-1].detail


def test_missing_template_refused(proj, tmp_path, monkeypatch):
    code, c, _ = proj
    c.put(f"{_base(code)}/outline", json={"titles": ["现状"]})
    monkeypatch.setattr(get_settings(), "export_template", str(tmp_path / "nope.docx"))
    r = c.get(f"{_base(code)}/export")
    assert r.status_code == 503 and "EXPORT_TEMPLATE" in r.json()["detail"]
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a docx")
    monkeypatch.setattr(get_settings(), "export_template", str(bad))
    assert c.get(f"{_base(code)}/export").status_code == 503
    # 缺页面设置（sectPr）的模板：拒绝导出（503），而不是 500
    from docx import Document as _Doc
    d = _Doc()
    body = d.element.body
    for sp in body.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}sectPr"):
        body.remove(sp)
    nosect = tmp_path / "nosect.docx"
    d.save(str(nosect))
    monkeypatch.setattr(get_settings(), "export_template", str(nosect))
    r = c.get(f"{_base(code)}/export")
    assert r.status_code == 503 and "页面设置" in r.json()["detail"]


def test_no_outline_strict_and_outsider(proj):
    code, c, _ = proj
    assert c.get(f"{_base(code)}/export").status_code == 409
    strict = _new_project("strict")
    user = create_user(("consultant",), {strict: "member"})
    with logged_in_client(app, user) as c2:
        r = c2.get(f"{_base(strict)}/export")
        assert r.status_code == 422
        assert c2.get(f"{_base(code)}/export").status_code == 404           # 非成员 404
    (a,) = _export_audits(strict)
    assert a.result == "blocked" and _d(a)["reason"] == "strict_tier"
