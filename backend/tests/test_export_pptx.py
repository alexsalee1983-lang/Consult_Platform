"""交付件 PowerPoint 导出（二期 A9）：与 Word 导出同一套规则——模板从配置读（配了打不开 503）、未签署每页标草稿、
页脚留痕导出编号、来源标注保留原样并在末页列出来源说明、严格档导出占位符版本、无大纲 409、非成员 404、审计不记正文。"""
import io
import json

import pytest
from pptx import Presentation
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Deliverable, Project

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project, _publish_kb

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture()
def proj():
    code = _new_project()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    yield code, c, user[0]
    c.__exit__(None, None, None)


def _pptx(r):
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == PPTX
    return Presentation(io.BytesIO(r.content))


def _slide_text(s) -> str:
    parts = []
    for sh in s.shapes:
        if sh.has_text_frame:
            parts.append(sh.text_frame.text)
        if sh.has_table:
            parts += [c.text for row in sh.table.rows for c in row.cells]
    return "\n".join(parts)


def _audits(code: str) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == "deliverable.export",
                                                      AuditLog.project_code == code).order_by(AuditLog.id)))


def _url(code: str, d: str = "D4") -> str:
    return f"{_base(code, d)}/export?format=pptx"


def test_pptx_builtin_slides_notes_sources_and_audit(proj):
    code, c, name = proj
    c.post(f"/api/projects/{code}/notes", json={"kind": "interview", "title": "值班访谈",
                                                "interviewee_role": "SOC 负责人", "content": "全部白班。"})
    doc_id = _publish_kb("导出 PPT 测试依据")
    c.put(f"{_base(code)}/outline", json={"titles": ["一句话结论", "现状", "空章"]})
    body1 = ("**要点**\n- 夜里没人看 [I-01]\n- 覆盖 38% **需要整改**\n\n**讲稿**：攻防演练 20 小时后才发现。")
    body2 = ("## 四个数字\n\n| 数字 | 含义 |\n|---|---|\n| 38% | 接入比例 |\n| 0 | 夜间研判人员 |\n\n"
             f"## 依据\n\n依据【依据：GB/T 22239 2019 8.1.5｜{doc_id}】。二期扩编 [推断]。")
    assert c.put(f"{_base(code)}/sections/1", json={"body": body1}).status_code == 201
    assert c.put(f"{_base(code)}/sections/2", json={"body": body2}).status_code == 201

    r = c.get(_url(code))
    prs = _pptx(r)
    assert "%E8%8D%89%E7%A8%BF" in r.headers["content-disposition"] and ".pptx" in r.headers["content-disposition"]
    slides = list(prs.slides)
    texts = [_slide_text(s) for s in slides]
    assert code in texts[0] and "D4" in texts[0]                                   # 封面
    one = next(i for i, t in enumerate(texts) if "1　一句话结论" in t)
    assert "夜里没人看 [I-01]" in texts[one] and "需要整改" in texts[one] and "**" not in texts[one]
    assert "攻防演练" not in texts[one]                                             # 讲稿进备注，不上屏
    assert "攻防演练 20 小时后才发现" in slides[one].notes_slide.notes_text_frame.text
    tbl = next(s for s in slides if any(sh.has_table for sh in s.shapes))
    assert "四个数字" in _slide_text(tbl) and "夜间研判人员" in _slide_text(tbl)
    assert any("（本章还没有正文）" in t for t in texts)
    src = texts[-1]                                                                 # 末页来源说明
    assert "来源说明" in src and "I-01：访谈纪要《值班访谈》（受访者：SOC 负责人）" in src
    assert f"{doc_id}：知识库 L4 依据《导出 PPT 测试依据》" in src and "推断：" not in src
    export_id = r.headers["x-export-id"]
    assert all("草稿" in t for t in texts)                                          # 每页标草稿
    assert all(f"导出编号 {export_id}" in t and f"导出：{name}" in t for t in texts)

    (a,) = _audits(code)
    d = json.loads(a.detail)
    assert d["format"] == "pptx" and d["export_id"] == export_id and d["draft"] is True
    assert d["template"] == "builtin" and d["versions"] == {"1": 1, "2": 1, "3": 0}
    assert d["slides"] == len(slides) and d["size"] == len(r.content)
    assert "夜里没人看" not in a.detail and "一句话结论" not in a.detail


def test_pptx_long_section_split_and_signed(proj):
    code, c, _ = proj
    c.put(f"{_base(code)}/outline", json={"titles": ["长章"]})
    body = "\n".join(f"- 第 {i} 条要点" for i in range(1, 31))
    c.put(f"{_base(code)}/sections/1", json={"body": body})
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        dv = db.scalar(select(Deliverable).where(Deliverable.project_id == p.id, Deliverable.code == "D4"))
        dv.status = "signed"
        db.commit()
    r = c.get(_url(code))
    texts = [_slide_text(s) for s in _pptx(r).slides]
    long = [t for t in texts if "长章" in t]
    assert len(long) >= 2 and "（续）" in long[1]                                   # 太长自动分页
    assert "第 30 条要点" in "\n".join(long)
    assert all("草稿" not in t for t in texts) and "%E8%8D%89%E7%A8%BF" not in r.headers["content-disposition"]


def test_pptx_template_configured_and_refused(proj, tmp_path, monkeypatch):
    code, c, _ = proj
    c.put(f"{_base(code)}/outline", json={"titles": ["现状"]})
    c.put(f"{_base(code)}/sections/1", json={"body": "正文。"})
    monkeypatch.setattr(get_settings(), "export_template_pptx", str(tmp_path / "nope.pptx"))
    r = c.get(_url(code))
    assert r.status_code == 503 and "EXPORT_TEMPLATE_PPTX" in r.json()["detail"]
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"not a pptx")
    monkeypatch.setattr(get_settings(), "export_template_pptx", str(bad))
    assert c.get(_url(code)).status_code == 503
    tpl = Presentation()
    tpl.slide_width, tpl.slide_height = 12192000, 6858000
    good = tmp_path / "company.pptx"
    tpl.save(str(good))
    monkeypatch.setattr(get_settings(), "export_template_pptx", str(good))
    prs = _pptx(c.get(_url(code)))
    assert prs.slide_width == 12192000
    a = _audits(code)[-1]
    assert json.loads(a.detail)["template"] == "configured" and str(good) not in a.detail


def test_pptx_no_outline_outsider_bad_format(proj):
    code, c, _ = proj
    assert c.get(_url(code)).status_code == 409
    assert c.get(f"{_base(code)}/export?format=pdf").status_code == 422
    other = _new_project()
    user = create_user(("consultant",), {other: "member"})
    with logged_in_client(app, user) as c2:
        assert c2.get(_url(code)).status_code == 404
