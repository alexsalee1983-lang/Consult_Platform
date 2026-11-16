"""知识迁移（kb-export / kb-import-export）：导出带审核记录和哈希，导入先校验、再自动核验，通过的保留原审核人发布。"""
import json

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import autopublish, transfer
from app.kb import review as rv
from app.kb.files import format_entry, parse_entry
from app.kb.importer import import_entry
from app.models import AuditLog, KbItem, User

from .conftest import create_user
from .test_kb_autopublish import LAW, _batch


@pytest.fixture(autouse=True)
def _never_suspended(monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_sample_fail_max", 1.0)


def _user(*roles):
    name = create_user(roles)[0]
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


def _rekey(src, suffix="-p"):
    """把导出目录改成“另一个环境”的样子：key 换新、重算文件和清单哈希（测试里生产和测试共用一个库）。"""
    m = json.loads((src / transfer.MANIFEST).read_text(encoding="utf-8"))
    for x in m["entries"]:
        e = parse_entry((src / x["file"]).read_text(encoding="utf-8"))
        e.key = (e.key[:60] + suffix)
        data = format_entry(e).encode()
        (src / x["file"]).write_bytes(data)
        x["key"], x["sha256"] = e.key, transfer.sha256(data)
    raw = json.dumps(m, ensure_ascii=False).encode()
    (src / transfer.MANIFEST).write_bytes(raw)
    return m, transfer.sha256(raw)


def test_export_verify_and_import(tmp_path, monkeypatch):
    op, author = _user("knowledge_expert"), _user("knowledge_expert")
    expert, admin = _user("knowledge_expert"), _user("knowledge_admin")
    manifest, src, titles = _batch(tmp_path, [("ok.txt", LAW, {})])
    law = titles["ok.txt"]
    with SessionLocal() as db:
        autopublish.run(db, db.merge(op), manifest, src)
        ref = parse_entry("---\n" + "\n".join(f"{k}: {v}" for k, v in {
            "key": f"tr-ref-{law[-6:]}", "title": "示范解读", "layer": "L4", "domain": "CMP", "product": "GEN",
            "doc_type": "REF", "stage": "通用", "capabilities": "[管理]", "source_type": "public",
            "source_detail": "公开资料：示范", "ref_document": law, "ref_version": "2029",
            "ref_status": "current"}.items()) + "\n---\n\n## 第二条 解读\n\n依照第二条，运营者应当建立管理制度。\n")
        r = import_entry(db, db.merge(author), ref)
        v = db.get(KbItem, db.scalar(select(KbItem.id).where(KbItem.import_key == ref.key))).versions[-1]
        rv.submit(db, db.merge(author), v)
        rv.review(db, db.merge(expert), v, "content", "approve", "意见原文不导出")
        rv.review(db, db.merge(admin), v, "compliance", "approve")
        ids = {r.doc_id} | {i.doc_id for i in db.scalars(select(KbItem)) if i.versions
                            and i.versions[-1].ref_document == law}
    orig = transfer.exportable
    monkeypatch.setattr(transfer, "exportable", lambda db: [(i, v) for i, v in orig(db) if i.doc_id in ids])

    out = tmp_path / "out"
    with SessionLocal() as db:
        rep = transfer.export(db, db.merge(op), out)
        with pytest.raises(transfer.TransferError, match="不是空的"):
            transfer.export(db, db.merge(op), out)
    assert rep.count == 4
    m = json.loads((out / transfer.MANIFEST).read_text(encoding="utf-8"))
    assert [x["doc_id"].split("-")[3] for x in m["entries"]] == ["SRC", "SRC", "SRC", "REF"]   # 依据原文在前
    assert "意见原文不导出" not in (out / transfer.MANIFEST).read_text(encoding="utf-8")
    assert {(r["reviewer"], r["review_role"]) for r in m["entries"][3]["reviews"]} == {
        (expert.username, "content"), (admin.username, "compliance")}

    with SessionLocal() as db:                       # 清单哈希不对、文件被改：整批不导入
        with pytest.raises(transfer.TransferError, match="清单哈希"):
            transfer.import_export(db, db.merge(op), out, "0" * 64)
    m2, sha = _rekey(out)
    f = out / m2["entries"][0]["file"]
    good = f.read_bytes()
    f.write_bytes(good + b"x")
    with SessionLocal() as db:
        with pytest.raises(transfer.TransferError, match="文件哈希不一致"):
            transfer.import_export(db, db.merge(op), out, sha)
    f.write_bytes(good)

    m2["entries"][2]["reviews"] = []                 # 审核记录不全的转人工审核
    raw = json.dumps(m2, ensure_ascii=False).encode()
    (out / transfer.MANIFEST).write_bytes(raw)
    sha = transfer.sha256(raw)
    with SessionLocal() as db:
        res = transfer.import_export(db, db.merge(op), out, sha)
        assert [(o.action, o.failed) for o in res] == [("published", []), ("published", []), ("review", ["reviews"]),
                                                       ("published", [])]
        new = db.scalar(select(KbItem).where(KbItem.import_key == m2["entries"][3]["key"]))
        cur = db.get(type(new.versions[-1]), new.current_version_id)
        assert cur.status == "published" and cur.created_by == author.username
        assert {(x.reviewer, x.review_role) for x in cur.reviews} == {
            (expert.username, "content"), (admin.username, "compliance"), (transfer.REVIEWER, transfer.ROLE)}
        src0 = db.scalar(select(KbItem).where(KbItem.import_key == m2["entries"][0]["key"]))
        assert db.get(type(cur), src0.current_version_id).auto_review == "platform"
        again = transfer.import_export(db, db.merge(op), out, sha)
        assert [o.action for o in again] == ["unchanged", "unchanged", "skipped", "unchanged"]
        logs = db.scalars(select(AuditLog).where(AuditLog.action.in_(("kb.export", "kb.transfer_import")))
                          .order_by(AuditLog.id.desc()).limit(8)).all()
        assert logs and all("运营者" not in str(x.detail) and law not in str(x.detail) for x in logs)
        for i in db.scalars(select(KbItem).where(KbItem.doc_id.in_(ids) | KbItem.import_key.like("%-p"))):
            i.retired = True                         # 下线，免得干扰同一测试库里的检索用例
        db.commit()


def test_rule_failures_metadata():
    x = {"auto_review": "agent", "reviews": [{"reviewer": "agent:kb-reviewer", "review_role": "agent",
                                              "decision": "approve"}]}
    assert transfer.reviews_complete(x)
    assert not transfer.reviews_complete({"auto_review": "platform", "reviews": []})
    assert transfer.reviews_complete({"reviews": [{"reviewer": "a", "review_role": "content", "decision": "approve"},
                                                  {"reviewer": "b", "review_role": "compliance",
                                                   "decision": "approve"}]})
    assert not transfer.reviews_complete({"reviews": [{"reviewer": "a", "review_role": "content",
                                                       "decision": "reject"}]})
