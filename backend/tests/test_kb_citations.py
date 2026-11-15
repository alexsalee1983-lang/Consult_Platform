"""起草引用校验（二期 B8）：解读、映射引用的“文件 + 条款号”必须有依据原文，找不到的待核、不能提交。"""
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import citations, importer, sources
from app.kb import review as rv
from app.kb.files import parse_entry
from app.models import KbItem, User

from .conftest import create_user

LAW = "第一条 为了保障示范安全，制定本法。\n第二条 运营者应当履行安全保护义务。\n"
STD = "8.1.4.1 身份鉴别\n应对登录的用户进行身份标识和鉴别。\n8.1.4.2 访问控制\n应对用户分配权限。\n"


def _people():
    names = [create_user(r)[0] for r in (("knowledge_expert",), ("knowledge_expert",), ("knowledge_admin",))]
    with SessionLocal() as db:
        return [db.scalar(select(User).where(User.username == n)) for n in names]


def _publish(db, people, entries):
    author, expert, admin = (db.merge(p) for p in people)
    for e in entries:
        r = importer.import_entry(db, author, e)
        if r.action == "unchanged":          # 前面的用例已发布过（用完下线了），恢复上线
            db.scalar(select(KbItem).where(KbItem.import_key == e.key)).retired = False
            db.commit()
            continue
        assert r.action == "created", r.message
        v = db.scalar(select(KbItem).where(KbItem.import_key == e.key)).versions[-1]
        rv.submit(db, author, v)
        rv.review(db, expert, v, "content", "approve")
        rv.review(db, admin, v, "compliance", "approve")


def _ref(key, body, **extra):
    head = {"key": key, "title": "示范解读", "layer": "L4", "domain": "CMP", "product": "GEN", "doc_type": "REF",
            "stage": "通用", "capabilities": "[管理]", "source_type": "public", "source_detail": "公开资料：示范",
            "ref_document": "中华人民共和国示范引用法", "ref_version": "2029", "ref_status": "current", **extra}
    return parse_entry("---\n" + "\n".join(f"{k}: {v}" for k, v in head.items()) + f"\n---\n\n{body}\n")


def test_find_patterns():
    body = ("## 第二条 运营者义务\n\n依照《网络安全法》（2016）第二十一条之一，结合 GB/T 22239-2019 8.1.4.1 与"
            "JR/T 0071 第 6.2 条，另见第一条。版本 1.2 不是条款。")
    got = [(d, p) for d, _, p in citations.find(body, "中华人民共和国示范引用法")]
    assert got == [("中华人民共和国示范引用法", "2"), ("网络安全法", "21-1"), ("GB/T 22239-2019", "8-1-4-1"),
                   ("JR/T 0071", "6-2"), ("中华人民共和国示范引用法", "1")]
    std = citations.find("## 8.1.4.1 身份鉴别\n\n正文 3.5 倍", "示范基本要求", "GB/T 99998-2029")
    assert [(d, p) for d, _, p in std] == [("GB/T 99998-2029", "8-1-4-1")]       # 正文里的数字不算


@pytest.fixture()
def library():
    people = _people()
    law = sources.Doc(file="law.txt", title="中华人民共和国示范引用法", code="主席令第1号", version="2029")
    std = sources.Doc(file="std.txt", title="示范引用技术 基本要求", code="GB/T 99998-2029", version="2029",
                      mode="catalog")
    entries = sources.split_doc(law, LAW.encode())[0] + sources.split_doc(std, STD.encode())[0]
    with SessionLocal() as db:
        _publish(db, people, entries)
    yield people
    with SessionLocal() as db:       # 下线，免得干扰同一测试库里的依据检索用例
        for e in entries:
            item = db.scalar(select(KbItem).where(KbItem.import_key == e.key))
            if item is not None:
                item.retired = True
        db.commit()


def test_check_found_and_pending(library):
    with SessionLocal() as db:
        cs = citations.check(db, "《示范引用法》第二条；GB/T 99998 8.1.4.2；《示范引用法》第九条；GB/T 99998-2029 9.9")
    assert [(c.found, c.clause) for c in cs] == [(True, "第二条"), (True, "8.1.4.2"), (False, "第九条"), (False, "9.9")]
    assert cs[0].excerpt == "运营者应当履行安全保护义务。" and cs[0].ref_status == "current"
    assert cs[1].catalog and cs[1].excerpt == ""
    assert citations.pending(cs) == ["示范引用法 第九条", "GB/T 99998-2029 9.9"]


def test_submit_blocked_until_source_exists(library, monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_citation_gate", True)
    author = library[0]
    with SessionLocal() as db:
        u = db.merge(author)
        ok = importer.import_entry(db, u, _ref("demo-ok", "## 第一条 立法目的\n\n解读……"), submit=True)
        assert ok.action == "created"
        bad = _ref("demo-bad", "## 第九条 处罚\n\n解读……")
        r = importer.import_entry(db, u, bad)
        v = db.scalar(select(KbItem).where(KbItem.import_key == "demo-bad")).versions[-1]
        with pytest.raises(rv.ReviewError, match="引用待核 1 处：中华人民共和国示范引用法 第九条"):
            rv.submit(db, u, v)
        assert v.status == "draft" and r.action == "created"
        monkeypatch.setattr(get_settings(), "kb_citation_gate", False)       # 关掉只显示、不拦
        assert rv.submit(db, u, v).status == "in_review"


def test_api_shows_citations(library):
    from app.main import app

    from .conftest import logged_in_client

    acct = create_user(("knowledge_expert",))
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == acct[0]))
        r = importer.import_entry(db, u, _ref("demo-api", "## 第二条 义务\n\n见 GB/T 99998-2029 8.1.4.1。"))
    c = logged_in_client(app, acct)
    got = c.get(f"/api/kb/versions/{r.version_id}").json()["citations"]
    assert [(x["clause"], x["found"], x["catalog"]) for x in got] == [("第二条", True, False), ("8.1.4.1", True, True)]
    assert got[0]["excerpt"] == "运营者应当履行安全保护义务。"
    consultant = logged_in_client(app, create_user(("consultant",)))
    assert consultant.get(f"/api/kb/versions/{r.version_id}").json().get("citations") in ([], None)
