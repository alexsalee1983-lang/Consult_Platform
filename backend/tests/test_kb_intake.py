"""投料与解析：格式识别、章节与表格、扫描件、切片；上传入口的机密级拦截、去重、可见范围、后台解析。"""
import json

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import parse
from app.main import app
from app.models import AuditLog, KbUpload
from app.services import jobs

from .conftest import create_user, logged_in_client, on_s3
from .kb_samples import SOC_MD, blank_pdf_bytes, docx_bytes, text_pdf_bytes

# ---- 解析 ----


def test_markdown_sections_keep_heading_path():
    doc = parse.parse("值班.md", SOC_MD.encode())
    heads = [s.heading for s in doc.sections]
    assert heads == ["值班管理规程", "值班管理规程 / 交接班", "值班管理规程 / 夜间"]
    assert "一级事件" in doc.sections[1].text


def test_docx_headings_and_table():
    data = docx_bytes([("Heading 1", "告警分级方法"), ("Normal", "按业务影响分为四级。"),
                       ("Heading 2", "一级告警"), ("Normal", "15 分钟内响应。")],
                      table=[["级别", "响应时限"], ["一级", "15 分钟"]])
    doc = parse.parse("分级.docx", data)
    assert doc.kind == "docx"
    assert doc.sections[0].heading == "告警分级方法"
    assert doc.sections[-1].heading == "告警分级方法 / 一级告警"
    assert "一级 | 15 分钟" in doc.sections[-1].text


def test_text_pdf_and_page_headings():
    doc = parse.parse("guide.pdf", text_pdf_bytes(["Shift handover must review open incidents and escalations",
                                                   "Night shift handled by the managed provider first"]))
    assert doc.kind == "pdf" and doc.pages == 2
    assert [s.heading for s in doc.sections] == ["第 1 页", "第 2 页"]


def test_scanned_pdf_rejected():
    with pytest.raises(parse.ParseError, match="扫描件"):
        parse.parse("scan.pdf", blank_pdf_bytes())


def test_gb18030_text_decoded():
    doc = parse.parse("notes.txt", "值班制度\n\n交接班核对".encode("gb18030"))
    assert doc.sections[0].text == "值班制度"


@pytest.mark.parametrize(("name", "data", "msg"), [
    ("a.doc", b"\xd0\xcf\x11\xe0", "另存为 .docx"),
    ("a.exe", b"MZ", "只支持"),
    ("a.docx", b"not a zip", "不是 .docx"),
    ("a.pdf", b"hello", "不是 PDF"),
    ("a.txt", b"", "空的"),
    ("a.md", b"   \n\n  ", "没有解析出文字"),
])
def test_bad_files_rejected(name, data, msg):
    with pytest.raises(parse.ParseError, match=msg):
        parse.parse(name, data)


def test_oversize_rejected():
    with pytest.raises(parse.ParseError, match="超过"):
        parse.parse("big.txt", b"x" * (parse.MAX_BYTES + 1))


def test_chunk_splits_long_sections_with_overlap():
    long = "\n".join(f"第{i}段：" + "交接班核对未闭环事件。" * 8 for i in range(20))
    chunks = parse.chunk([parse.Section("规程 / 交接班", long), parse.Section("规程 / 夜间", "短章节")])
    assert all(len(c.text) <= parse.CHUNK_CHARS + parse.CHUNK_OVERLAP + 1 for c in chunks)
    assert chunks[-1].text == "短章节" and chunks[0].heading == "规程 / 交接班"
    assert len(chunks) > 3
    assert chunks[1].text[:20] in chunks[0].text          # 相邻片段有重叠


def test_filename_cleaned():
    from app.kb.intake import clean_filename

    assert clean_filename("C:\\Users\\x\\值班 规程.docx") == "值班 规程.docx"
    assert clean_filename("../../etc/passwd") == "passwd"
    assert clean_filename("a\x00b.md") == "ab.md"


# ---- 上传入口 ----

@pytest.fixture()
def consultant():
    c = logged_in_client(app, create_user(("consultant",)))
    yield c
    c.__exit__(None, None, None)


def _upload(client, name, data):
    return client.post("/api/kb/intake", files={"file": (name, data, "application/octet-stream")})


def test_guard_blocks_without_storing_and_audit_has_no_content(consultant):
    secret_doc = "# 网络改造\n\n核心交换机 10.1.20.1 管理口令 Abc@12345".encode()
    r = _upload(consultant, "改造.md", secret_doc)
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["blocked"] is True and "没有保存" in body["summary"]
    assert {f["kind"] for f in body["findings"]} >= {"ip", "credential"}
    assert "Abc@12345" not in r.text and "10.1.20.1" not in r.text
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "kb.intake").order_by(AuditLog.id.desc())).first()
        assert a.result == "blocked" and json.loads(a.detail)["reason"] == "guard"
        assert "Abc@12345" not in a.detail and "改造" not in a.detail       # 不记内容，也不记文件名
        assert db.scalar(select(KbUpload).where(KbUpload.filename == "改造.md")) is None


def test_unsupported_and_scanned_rejected(consultant):
    assert _upload(consultant, "a.exe", b"MZ").status_code == 422
    r = _upload(consultant, "scan.pdf", blank_pdf_bytes())
    assert r.status_code == 422 and "扫描件" in r.json()["detail"]


def test_storage_disabled_returns_503(consultant, monkeypatch):
    from app.services import storage

    monkeypatch.setattr(storage, "enabled", lambda: False)
    assert _upload(consultant, "值班.md", SOC_MD.encode()).status_code == 503


@on_s3
def test_full_intake_flow_parse_job_and_visibility(consultant):
    data = SOC_MD.replace("值班管理规程", "值班管理规程（流程测试）").encode()
    r = _upload(consultant, "值班规程.md", data)
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["status"] == "received" and up["kind"] == "markdown"
    assert _upload(consultant, "副本.md", data).status_code == 409                     # 同一文件不重复投料

    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=["kb.parse"]):
            pass
    detail = consultant.get(f"/api/kb/intake/{up['id']}").json()
    assert detail["status"] == "parsed" and detail["section_count"] == 3 and detail["chunk_count"] >= 3
    assert detail["preview"][1]["heading"].endswith("交接班")

    with SessionLocal() as db:
        row = db.get(KbUpload, up["id"])
        assert "值班" not in row.object_key and row.object_key.startswith("intake/")   # 键里不用原文件名

    other = logged_in_client(app, create_user(("consultant",)))
    expert = logged_in_client(app, create_user(("knowledge_expert",)))
    try:
        assert other.get(f"/api/kb/intake/{up['id']}").status_code == 404           # 别人的投料看不到
        assert up["id"] not in [u["id"] for u in other.get("/api/kb/intake").json()]
        assert expert.get(f"/api/kb/intake/{up['id']}").status_code == 200          # 知识专家看全部
        assert up["id"] in [u["id"] for u in expert.get("/api/kb/intake").json()]
    finally:
        other.__exit__(None, None, None)
        expert.__exit__(None, None, None)


@on_s3
def test_docx_intake_parsed_by_worker(consultant):
    data = docx_bytes([("Heading 1", "告警分级方法（流程测试）"), ("Normal", "按业务影响分为四级。")],
                      table=[["级别", "响应时限"], ["一级", "15 分钟"]])
    up = _upload(consultant, "分级.docx", data).json()
    with SessionLocal() as db:
        assert jobs.run_once(db, "test-worker", kinds=["kb.parse"]) == jobs.DONE
    d = consultant.get(f"/api/kb/intake/{up['id']}").json()
    assert d["status"] == "parsed" and any("一级 | 15 分钟" in c["text"] for c in d["preview"])
