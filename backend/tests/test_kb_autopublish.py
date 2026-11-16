"""依据原文自动核验发布（二期 B10）：核验通过的平台自动发布并抽检，没通过的转人工；抽检问题率超标暂停。"""
import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import autopublish, sources
from app.kb import review as rv
from app.models import AuditLog, KbItem, KbVersion, User

from .conftest import create_user, logged_in_client

LAW = "示范条例\n第一条 为了规范示范管理，制定本条例。\n第二条 运营者应当建立管理制度。\n第三条 本条例自公布之日起施行。\n"
SKIP = "示范条例\n第一条 为了规范示范管理，制定本条例。\n第二条 运营者应当建立管理制度。\n第四条 跳号的条文。\n"
LEAK = ("示范条例\n第一条 为了规范示范管理，制定本条例。\n第二条 默认管理口令：Zx9#kq2Lm8Q，应当在上线前修改。\n"
        "第三条 本条例自公布之日起施行。\n")
HEAD = "文件名,名称,文号/标准号,现行状态,原网址,获取方式,sha256\n"


@pytest.fixture(autouse=True)
def _never_suspended(monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_sample_fail_max", 1.0)     # 其他用例留下的抽检结果不影响


def _users():
    names = [create_user(("knowledge_expert",))[0] for _ in range(2)]
    with SessionLocal() as db:
        return [db.scalar(select(User).where(User.username == n)) for n in names]


def _batch(tmp_path, rows):
    """rows: [(文件名, 正文, 改动)]；返回 (清单, 资料目录, {文件名: 名称})。"""
    src = tmp_path / "src"
    src.mkdir()
    lines, titles = [HEAD], {}
    for name, text, change in rows:
        title = f"示范条例{uuid.uuid4().hex[:6]}"
        titles[name] = title
        data = text.encode()
        (src / name).write_bytes(data)
        row = {"code": "示范令第1号", "url": "https://www.example.gov.cn/1", "sha": hashlib.sha256(data).hexdigest(),
               **change}
        lines.append(f"{name},{title},{row['code']},现行,{row['url']},已下载,{row['sha']}\n")
    manifest = tmp_path / "list.csv"
    manifest.write_text("".join(lines), encoding="utf-8")
    return manifest, src, titles


def _versions(db, title):
    return list(db.scalars(select(KbVersion).where(KbVersion.ref_document == title).order_by(KbVersion.id)))


def test_verify_rules():
    d = sources.Doc(file="a.txt", title="示范条例", code="示范令第1号", url="https://flk.npc.gov.cn/x",
                    sha256=hashlib.sha256(LAW.encode()).hexdigest())
    entries, rejected, _ = sources.split_doc(d, LAW.encode())
    assert sources.verify(d, LAW.encode(), entries, rejected) == []
    bad = sources.Doc(file="a.txt", title="示范条例", url="https://example.com.cn.evil.com/gov.cn", sha256="00")
    assert sources.verify(bad, LAW.encode(), entries, rejected) == ["official_source", "sha256", "code"]
    assert not sources.is_official("ftp://www.gov.cn/x") and sources.is_official("http://csrc.nist.gov/pubs")
    assert sources.is_official("https://www.gov.za/a") and sources.is_official("https://lom.agc.gov.my/x")
    assert not sources.is_official("https://gov.za.evil.com/a") and not sources.is_official("https://196.188.1.2/x")
    gap = sources.Doc(file="b.txt", title="示范条例", code="示范令第1号", url="https://www.gov.cn/1",
                      sha256=hashlib.sha256(SKIP.encode()).hexdigest())
    e2, r2, _ = sources.split_doc(gap, SKIP.encode())
    assert sources.verify(gap, SKIP.encode(), e2, r2) == ["continuous"]       # 第四条跳号，没切出来


def test_publish_sample_and_manual(tmp_path):
    op, checker = _users()
    manifest, src, titles = _batch(tmp_path, [("ok.txt", LAW, {}), ("bad.txt", LAW, {"sha": "0" * 64}),
                                              ("gap.txt", SKIP, {})])
    with SessionLocal() as db:
        r = autopublish.run(db, db.merge(op), manifest, src)
        assert [(d.published, d.manual, d.failed) for d in r.docs] == [(3, 0, []), (0, 3, ["sha256"]),
                                                                         (0, 2, ["continuous"])]
        ok = _versions(db, titles["ok.txt"])
        assert {(v.status, v.auto_review) for v in ok} == {("published", "platform")}
        assert [(x.reviewer, x.review_role) for x in ok[0].reviews] == [("platform", "platform")]
        assert {v.status for v in _versions(db, titles["bad.txt"])} == {"in_review"}
        assert len(r.sampled) == 1 and sum(v.sample_state == "pending" for v in ok) == 1
        logs = db.scalars(select(AuditLog).where(AuditLog.action == "kb.auto_publish")
                          .order_by(AuditLog.id.desc()).limit(3)).all()
        assert all(titles["ok.txt"] not in str(x.detail) and "制定本条例" not in str(x.detail) for x in logs)
        again = autopublish.run(db, db.merge(op), manifest, src)          # 原件没变：跳过
        assert again.docs[0].unchanged == 3 and again.docs[0].published == 0 and again.docs[1].manual == 3

        target = next(v for v in ok if v.sample_state == "pending")
        with pytest.raises(rv.ReviewError, match="不能抽检自己导入的版本"):
            autopublish.sample_check(db, db.merge(op), target, "pass")
        with pytest.raises(rv.ReviewError, match="写明问题"):
            autopublish.sample_check(db, db.merge(checker), target, "fail", "")
        assert autopublish.sample_check(db, db.merge(checker), target, "pass").sample_state == "passed"
        with pytest.raises(rv.ReviewError, match="已经抽检过"):
            autopublish.sample_check(db, db.merge(checker), target, "fail", "条文与原件不一致")


def test_suspended_when_fail_rate_high(tmp_path, monkeypatch):
    op, checker = _users()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, src1, t1 = _batch(tmp_path / "a", [("ok.txt", LAW, {})])
    with SessionLocal() as db:
        autopublish.run(db, db.merge(op), first, src1)
        v = _versions(db, t1["ok.txt"])[0]
        autopublish.sample_check(db, db.merge(checker), v, "fail", "条文与原件不一致")
        assert v.sample_state == "failed" and v in autopublish.sample_queue(db)
        monkeypatch.setattr(get_settings(), "kb_sample_fail_max", 0.0)
        second, src2, t2 = _batch(tmp_path / "b", [("ok.txt", LAW, {})])
        r = autopublish.run(db, db.merge(op), second, src2)
        assert r.suspended and r.docs[0].manual == 3 and r.docs[0].published == 0
        assert {x.status for x in _versions(db, t2["ok.txt"])} == {"in_review"}
        monkeypatch.setattr(get_settings(), "kb_sample_fail_max", 1.0)
        monkeypatch.setattr(get_settings(), "kb_src_auto_publish", False)
        assert autopublish.suspended(db) == "KB_SRC_AUTO_PUBLISH=false"


def test_rejected_clause_does_not_block_rest(tmp_path, monkeypatch):
    """负责人 2026-11-16：机密级检测退回的条款不入库，同一份其余条款照常自动发布，按更高比例抽检。"""
    op = _users()[0]
    monkeypatch.setattr(get_settings(), "kb_sample_rate_src", 0.0)
    monkeypatch.setattr(get_settings(), "kb_sample_rate_src_partial", 1.0)
    manifest, src, titles = _batch(tmp_path, [("leak.txt", LEAK, {})])
    with SessionLocal() as db:
        r = autopublish.run(db, db.merge(op), manifest, src)
        d = r.docs[0]
        assert (d.published, d.manual, d.rejected, d.failed) == (2, 0, 1, [])
        vs = _versions(db, titles["leak.txt"])
        assert len(vs) == 2 and {v.status for v in vs} == {"published"}
        assert all("Zx9#kq2Lm8Q" not in v.body for v in vs)
        assert len(r.sampled) == 2 and all(v.sample_state == "pending" for v in vs)


def test_samples_api(tmp_path):
    from app.main import app

    acct = create_user(("knowledge_expert",))
    checker = logged_in_client(app, acct)
    op = _users()[0]
    manifest, src, titles = _batch(tmp_path, [("ok.txt", LAW, {})])
    with SessionLocal() as db:
        r = autopublish.run(db, db.merge(op), manifest, src)
        vid = db.scalar(select(KbItem).where(KbItem.doc_id == r.sampled[0])).current_version_id
    got = checker.get("/api/kb/samples").json()
    assert vid in [x["id"] for x in got["items"]] and "suspended" in got
    item = next(x for x in got["items"] if x["id"] == vid)
    assert item["auto_review"] == "platform" and item["sample_state"] == "pending"
    res = checker.post(f"/api/kb/versions/{vid}/sample", json={"decision": "pass"})
    assert res.status_code == 200 and res.json()["sample_state"] == "passed"
    consultant = logged_in_client(app, create_user(("consultant",)))
    assert consultant.get("/api/kb/samples").status_code == 403
