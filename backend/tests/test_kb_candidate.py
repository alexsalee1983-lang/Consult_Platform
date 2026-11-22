"""候选影子期（三期 B24①）：自动发布先落候选，交付检索查不到、评测可带候选；满期且抽检未判不通过晋级，抽检不通过退回人工。"""
import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, inspect, select, text

from app.core.config import get_settings
from app.core.db import SessionLocal, utcnow
from app.core.migrate import alembic_config, upgrade_to_head
from app.kb import agent_review, autopublish, candidate, evaluate, importer, search
from app.kb import review as rv
from app.kb.files import parse_entry
from app.models import AuditLog, Job, KbChunk, KbVersion, User

from .conftest import create_user

LAW = "示范条例\n第一条 为了规范示范管理，制定本条例。\n第二条 运营者应当建立管理制度。\n第三条 本条例自公布之日起施行。\n"
HEAD = "文件名,名称,文号/标准号,现行状态,原网址,获取方式,sha256\n"
LATER = timedelta(days=8)


@pytest.fixture(autouse=True)
def _shadow(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "kb_sample_fail_max", 1.0)       # 其他用例留下的抽检结果不影响
    monkeypatch.setattr(s, "kb_candidate_shadow", True)
    monkeypatch.setattr(s, "kb_candidate_days", 7)
    monkeypatch.setattr(s, "kb_sample_rate_src", 0.0)        # 仍至少抽 1 条


def _people():
    names = [create_user(r)[0] for r in (("knowledge_expert",), ("knowledge_expert",), ("consultant",))]
    with SessionLocal() as db:
        return [db.scalar(select(User).where(User.username == n)) for n in names]


def _write(tmp_path: Path, title: str, body: str) -> tuple[Path, Path]:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    data = body.replace("第一条 ", f"第一条 （{title}）", 1).encode()
    (src / "law.txt").write_bytes(data)
    manifest = tmp_path / "list.csv"
    manifest.write_text(HEAD + f"law.txt,{title},示范令第1号,现行,https://www.example.gov.cn/1,已下载,"
                               f"{hashlib.sha256(data).hexdigest()}\n", encoding="utf-8")
    return manifest, src


def _versions(db, title):
    return list(db.scalars(select(KbVersion).where(KbVersion.ref_document == title).order_by(KbVersion.id)))


def _doc_ids(out: dict) -> set[str]:
    return {h["doc_id"] for h in out["items"]}


def _pass_samples(db, checker, vs):
    for v in vs:
        if v.sample_state == "pending":
            autopublish.sample_check(db, db.merge(checker), v, "pass")


def test_source_publish_lands_candidate_and_shadow_search(tmp_path):
    op, checker, consultant = _people()
    title = f"示范条例{uuid.uuid4().hex[:6]}"
    manifest, src = _write(tmp_path, title, LAW)
    with SessionLocal() as db:
        r = autopublish.run(db, db.merge(op), manifest, src)
        assert r.docs[0].published == 3 and len(r.sampled) == 1
        vs = _versions(db, title)
        assert {(v.status, v.auto_review, v.candidate_result) for v in vs} == {("candidate", "platform", "")}
        assert all(v.candidate_since is not None and v.item.current_version_id is None for v in vs)
        assert all(db.scalar(select(func.count()).where(KbChunk.version_id == v.id)) for v in vs)
        doc_ids = {v.item.doc_id for v in vs}
        q = f"{title} 运营者应当建立管理制度"
        # 交付侧检索（默认）查不到候选，知识侧用户也一样
        assert not doc_ids & _doc_ids(search.search(db, db.merge(consultant), q, layer="L4"))
        assert not doc_ids & _doc_ids(search.search(db, db.merge(checker), q, layer="L4"))
        # 影子检索 / 评测带候选能查到
        assert doc_ids & _doc_ids(search.search(db, db.merge(checker), q, layer="L4", include_candidates=True))
        key = vs[0].item.import_key          # 第一条带唯一标题，不受其他用例留下的同文条款干扰
        questions = [{"id": "c1", "mode": "search", "question": f"{title} 规范示范管理", "expect": [key]}]
        assert evaluate.run(db, db.merge(checker), questions).results[0].rank is None
        assert evaluate.run(db, db.merge(checker), questions, include_candidates=True).results[0].rank is not None
        # 抽检队列能抽到候选
        pending = next(v for v in vs if v.sample_state == "pending")
        assert pending in autopublish.sample_queue(db)
        # 原件没变再跑一次：与候选相同，跳过
        again = autopublish.run(db, db.merge(op), manifest, src)
        assert again.docs[0].unchanged == 3 and again.docs[0].published == 0
        log = db.scalar(select(AuditLog).where(AuditLog.action == "kb.candidate").order_by(AuditLog.id.desc()))
        assert "制定本条例" not in log.detail and title not in log.detail


def test_promote_dry_run_then_promote(tmp_path):
    op, checker, consultant = _people()
    title = f"示范条例{uuid.uuid4().hex[:6]}"
    manifest, src = _write(tmp_path, title, LAW)
    with SessionLocal() as db:
        autopublish.run(db, db.merge(op), manifest, src)
        vs = _versions(db, title)
        labels = {f"{v.item.doc_id} v{v.version}" for v in vs}
        pending = next(v for v in vs if v.sample_state == "pending")
        # 天数未满：继续候选
        r = candidate.promote_due(db, now=utcnow())
        assert labels <= set(r.waiting) and not labels & set(r.promoted)
        # 满期，但抽检还没判的那条继续等
        r = candidate.promote_due(db, dry_run=True, now=utcnow() + LATER)
        assert f"{pending.item.doc_id} v{pending.version}" in r.waiting
        assert labels - {f"{pending.item.doc_id} v{pending.version}"} <= set(r.promoted)
        db.expire_all()
        assert {v.status for v in _versions(db, title)} == {"candidate"}           # dry-run 不改
        _pass_samples(db, checker, vs)
        r = candidate.promote_due(db, now=utcnow() + LATER)
        assert labels <= set(r.promoted)
        db.expire_all()
        vs = _versions(db, title)
        assert {(v.status, v.candidate_result) for v in vs} == {("published", "promoted")}
        assert all(v.item.current_version_id == v.id for v in vs)
        q = f"{title} 运营者应当建立管理制度"
        assert {v.item.doc_id for v in vs} & _doc_ids(search.search(db, db.merge(consultant), q, layer="L4"))
        log = db.scalar(select(AuditLog).where(AuditLog.action == "kb.candidate_promote").order_by(AuditLog.id.desc()))
        assert "运营者" not in log.detail and title not in log.detail
        scan = db.scalar(select(AuditLog).where(AuditLog.action == "kb.candidate_scan").order_by(AuditLog.id.desc()))
        assert json.loads(scan.detail)["promoted"] >= 3


def test_revision_keeps_old_published_until_promoted(tmp_path, monkeypatch):
    op, checker, consultant = _people()
    title = f"示范条例{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(get_settings(), "kb_candidate_shadow", False)
    manifest, src = _write(tmp_path, title, LAW)
    with SessionLocal() as db:
        autopublish.run(db, db.merge(op), manifest, src)
        assert {v.status for v in _versions(db, title)} == {"published"}       # 开关关：照旧直接发布
    monkeypatch.setattr(get_settings(), "kb_candidate_shadow", True)
    manifest, src = _write(tmp_path, title, LAW.replace("建立管理制度", "建立管理制度和应急预案"))
    with SessionLocal() as db:
        r = autopublish.run(db, db.merge(op), manifest, src)
        assert r.docs[0].published == 1 and r.docs[0].unchanged == 2
        v2 = next(v for v in _versions(db, title) if v.version == 2)
        item = v2.item
        v1 = next(v for v in item.versions if v.version == 1)
        assert v2.status == "candidate" and item.current_version_id == v1.id and v1.status == "published"
        q = f"{title} 运营者应当建立管理制度"
        live = search.search(db, db.merge(consultant), q, layer="L4")["items"]
        assert (item.doc_id, 1) in {(h["doc_id"], h["version"]) for h in live}
        shadow = search.search(db, db.merge(checker), q, layer="L4", include_candidates=True)["items"]
        got = {(h["doc_id"], h["version"]) for h in shadow}
        assert (item.doc_id, 2) in got and (item.doc_id, 1) not in got               # 候选替换旧现行版本
        if v2.sample_state == "pending":
            autopublish.sample_check(db, db.merge(checker), v2, "pass")
        candidate.promote_due(db, now=utcnow() + LATER)
        db.expire_all()
        v1, v2 = db.get(KbVersion, v1.id), db.get(KbVersion, v2.id)
        assert (v1.status, v2.status, v2.item.current_version_id) == ("superseded", "published", v2.id)
        assert db.scalar(select(func.count()).where(KbChunk.version_id == v2.id)) == 1   # 不重复切片


def test_sample_fail_returns_to_manual(tmp_path):
    op, checker, _ = _people()
    title = f"示范条例{uuid.uuid4().hex[:6]}"
    manifest, src = _write(tmp_path, title, LAW)
    with SessionLocal() as db:
        autopublish.run(db, db.merge(op), manifest, src)
        v = next(x for x in _versions(db, title) if x.sample_state == "pending")
        autopublish.sample_check(db, db.merge(checker), v, "fail", "条文与原件不一致")
        db.refresh(v)
        assert (v.status, v.candidate_result, v.sample_state) == ("in_review", "returned", "failed")
        assert db.scalar(select(func.count()).where(KbChunk.version_id == v.id)) == 0
        assert v not in autopublish.sample_queue(db)
        assert f"{v.item.doc_id} v{v.version}" not in candidate.promote_due(db, now=utcnow() + LATER).promoted
        # 人工两项审核后照常发布，重新切片
        admin = db.scalar(select(User).where(User.username == create_user(("knowledge_admin",))[0]))
        rv.review(db, db.merge(checker), v, "content", "approve")
        rv.review(db, admin, v, "compliance", "approve")
        db.refresh(v)
        assert v.status == "published" and v.item.current_version_id == v.id
        assert db.scalar(select(func.count()).where(KbChunk.version_id == v.id)) >= 1
        log = db.scalar(select(AuditLog).where(AuditLog.action == "kb.candidate_return").order_by(AuditLog.id.desc()))
        assert "不一致" not in log.detail and "sample_failed" in log.detail


def test_promote_catches_failed_candidate(tmp_path):
    """抽检结果在别处已记成不通过（例如旧数据）：晋级扫描退回人工，dry-run 只列出。"""
    op, _, _ = _people()
    title = f"示范条例{uuid.uuid4().hex[:6]}"
    manifest, src = _write(tmp_path, title, LAW)
    with SessionLocal() as db:
        autopublish.run(db, db.merge(op), manifest, src)
        v = _versions(db, title)[0]
        v.sample_state = "failed"
        db.commit()
        label = f"{v.item.doc_id} v{v.version}"
        assert label in candidate.promote_due(db, dry_run=True).returned
        db.refresh(v)
        assert v.status == "candidate"
        assert label in candidate.promote_due(db).returned
        db.refresh(v)
        assert (v.status, v.candidate_result) == ("in_review", "returned")


def test_agent_review_lands_candidate(monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_sample_rate_agent", 0.0)
    findings = {"checks": {k: {"pass": True, "reason": "与原文一致"} for k in agent_review.CHECKS}, "issues": [],
                "conclusion": "通过"}
    monkeypatch.setattr(agent_review, "rule_failures", lambda db, v: ([], []))
    monkeypatch.setattr(agent_review, "_agent", lambda db, v, cites: ("ok", "", dict(findings), "t1", "mock"))
    author = _people()[0]
    head = {"key": f"t-{uuid.uuid4().hex[:8]}", "title": "示范审核法第二条解读", "layer": "L4", "domain": "CMP",
            "product": "GEN", "doc_type": "REF", "stage": "通用", "capabilities": "[管理]", "source_type": "public",
            "source_detail": "公开资料：示范", "ref_document": "示范审核法", "ref_version": "2029",
            "ref_status": "current", "summary": "第二条要求每年评估", "applicability": "适用于示范行业运营者"}
    e = parse_entry("---\n" + "\n".join(f"{k}: {v}" for k, v in head.items())
                    + "\n---\n\n## 第二条 风险评估\n\n运营者应当每年开展一次风险评估。\n")
    with SessionLocal() as db:
        r = importer.import_entry(db, db.merge(author), e)
        v = db.get(KbVersion, r.version_id)
        rv.submit(db, db.merge(author), v)
        assert agent_review.run(db, v).status == "published"
        db.refresh(v)
        assert (v.status, v.auto_review, v.item.current_version_id) == ("candidate", "agent", None)
        assert f"{v.item.doc_id} v{v.version}" in candidate.promote_due(db, now=utcnow() + LATER).promoted
        db.refresh(v)
        assert v.status == "published" and v.item.current_version_id == v.id


def test_switch_off_publishes_directly(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_candidate_shadow", False)
    op = _people()[0]
    title = f"示范条例{uuid.uuid4().hex[:6]}"
    manifest, src = _write(tmp_path, title, LAW)
    with SessionLocal() as db:
        autopublish.run(db, db.merge(op), manifest, src)
        vs = _versions(db, title)
        assert {(v.status, v.candidate_since, v.candidate_result) for v in vs} == {("published", None, "")}
        assert all(v.item.current_version_id == v.id for v in vs)
        assert not {f"{v.item.doc_id} v{v.version}" for v in vs} & set(candidate.promote_due(db, dry_run=True).waiting)


def test_retire_covers_candidate(tmp_path):
    op = _people()[0]
    title = f"示范条例{uuid.uuid4().hex[:6]}"
    manifest, src = _write(tmp_path, title, LAW)
    admin_name = create_user(("knowledge_admin",))[0]
    with SessionLocal() as db:
        autopublish.run(db, db.merge(op), manifest, src)
        v = _versions(db, title)[0]
        rv.retire(db, db.scalar(select(User).where(User.username == admin_name)), v.item, "测试下线")
        db.refresh(v)
        assert v.status == "retired" and v not in candidate.candidates(db)


def test_daily_job_enqueued_once():
    with SessionLocal() as db:
        for j in db.scalars(select(Job).where(Job.kind == candidate.JOB, Job.status.in_(("queued", "running")))):
            j.status = "done"
        db.commit()
        assert candidate.enqueue(db) is not None
        assert candidate.enqueue(db) is None


def test_k0015_roundtrip(tmp_path):
    main = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    kb = create_engine(f"sqlite:///{tmp_path / 'kb.db'}")
    upgrade_to_head(main, kb)
    down = ScriptDirectory.from_config(alembic_config(chain="kb")).get_revision("k0015_kb_candidate").down_revision
    with kb.begin() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("kb_versions")}
        assert {"candidate_since", "candidate_result"} <= cols
        conn.execute(text("INSERT INTO kb_items (id, doc_id, layer, domain, product, doc_type, stage, capabilities, "
                          "industries, tags, control_ids, classification, visible_roles, owner, retired, created_by, "
                          "created_at, updated_at) VALUES (1, 'L4-CMP-GEN-SRC-001', 'L4', 'CMP', 'GEN', 'SRC', '通用', "
                          "'[]', '[]', '[]', '[]', 'internal', '[]', '', 0, 't', '2026-11-22', '2026-11-22')"))
        conn.execute(text("INSERT INTO kb_versions (id, item_id, version, status, title, summary, body, applicability, "
                          "change_note, source_type, source_detail, ref_document, ref_version, ref_status, ref_code, "
                          "ref_replaced_by, deid_status, created_by, created_at, submitted_by, single_review, "
                          "auto_review, sample_state, recheck_state, recheck_reason, candidate_result) VALUES "
                          "(1, 1, 1, 'candidate', 't', '', 'b', '', '', 'public', '', '', '', '', '', '', "
                          "'not_needed', 't', '2026-11-22', 't', 0, 'platform', '', '', '', '')"))
        conn.execute(text("INSERT INTO kb_chunks (id, version_id, seq, heading, text, embedding_model) "
                          "VALUES (1, 1, 1, '', 'b', '')"))
        cfg = alembic_config(conn, "kb")
        command.downgrade(cfg, down)
        cols = {c["name"] for c in inspect(conn).get_columns("kb_versions")}
        assert not {"candidate_since", "candidate_result"} & cols
        assert conn.execute(text("SELECT status FROM kb_versions WHERE id = 1")).scalar() == "in_review"
        assert conn.execute(text("SELECT count(*) FROM kb_chunks")).scalar() == 0
        command.upgrade(cfg, "head")
        cols = {c["name"] for c in inspect(conn).get_columns("kb_versions")}
        assert {"candidate_since", "candidate_result"} <= cols
    main.dispose()
    kb.dispose()
