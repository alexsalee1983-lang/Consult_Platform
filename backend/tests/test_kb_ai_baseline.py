"""AI 基线草稿导入（负责人 2026-11-27 选 B）：虚拟客户的黄金样例 / 公司做法基线稿，
非生产环境经 kb-import --ai-baseline 直接成草稿（去标识状态 virtual），走第三级（一位非作者知识专家拍板）；生产拒绝。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import importer
from app.kb import review as rv
from app.models import AuditLog, KbItem, KbVersion

from .test_kb_import_eval import _entry, _user


def _golden(key: str, **extra) -> str:
    return _entry(key=key, doc_type="GLD", knowledge_kind="golden", source_type="expert",
                  source_detail="AI 基线草稿，expert_review pending", **extra).replace("source_type: public\n", "")


def test_baseline_import_creates_level3_draft_and_submits(tmp_path):
    expert = _user(("knowledge_expert",))
    (tmp_path / "g.md").write_text(_golden("t-gld-base"), encoding="utf-8")
    with SessionLocal() as db:
        r = importer.import_dir(db, db.merge(expert), tmp_path, submit=True, ai_baseline=True)[0]
        assert r.action == "created", r.message
        v = db.get(KbVersion, r.version_id)
        assert v.deid_status == "virtual" and v.status == "in_review" and rv.level3(v)
        assert rv.required_roles(v) == ("content",)
        logs = db.scalars(select(AuditLog).where(AuditLog.action == "kb.ai_baseline_import")).all()
        assert any(v.item.doc_id in str(x.detail) for x in logs)


def test_without_flag_expert_source_still_rejected(tmp_path):
    expert = _user(("knowledge_expert",))
    (tmp_path / "g.md").write_text(_golden("t-gld-noflag"), encoding="utf-8")
    with SessionLocal() as db:
        r = importer.import_dir(db, db.merge(expert), tmp_path)[0]
    assert r.action == "error" and "公开资料" in r.message


def test_baseline_only_for_expert_source(tmp_path):
    expert = _user(("knowledge_expert",))
    (tmp_path / "p.md").write_text(_entry(key="t-base-public"), encoding="utf-8")
    (tmp_path / "q.md").write_text(_entry(key="t-base-proj", source_detail="x").replace(
        "source_type: public", "source_type: project"), encoding="utf-8")
    with SessionLocal() as db:
        rs = {r.file: r for r in importer.import_dir(db, db.merge(expert), tmp_path, ai_baseline=True)}
    assert rs["p.md"].action == "error" and "专家萃取" in rs["p.md"].message
    assert rs["q.md"].action == "error" and "专家萃取" in rs["q.md"].message


def test_baseline_still_runs_guard(tmp_path):
    expert = _user(("knowledge_expert",))
    (tmp_path / "s.md").write_text(_golden("t-gld-secret", body="## 配置\n\n管理口令 Abc@12345"), encoding="utf-8")
    with SessionLocal() as db:
        r = importer.import_dir(db, db.merge(expert), tmp_path, ai_baseline=True)[0]
    assert r.action == "error" and "机密级" in r.message


def test_baseline_refused_in_production(tmp_path, monkeypatch):
    expert = _user(("knowledge_expert",))
    (tmp_path / "g.md").write_text(_golden("t-gld-prod"), encoding="utf-8")
    monkeypatch.setattr(get_settings(), "app_env", "prod")
    with SessionLocal() as db, pytest.raises(rv.ReviewError):
        importer.import_dir(db, db.merge(expert), tmp_path, ai_baseline=True)
    with SessionLocal() as db:
        assert db.scalar(select(KbItem).where(KbItem.import_key == "t-gld-prod")) is None


def test_virtual_version_cannot_be_submitted_in_production(tmp_path, monkeypatch):
    expert = _user(("knowledge_expert",))
    (tmp_path / "g.md").write_text(_golden("t-gld-prod-submit"), encoding="utf-8")
    with SessionLocal() as db:
        r = importer.import_dir(db, db.merge(expert), tmp_path, ai_baseline=True)[0]
        monkeypatch.setattr(get_settings(), "app_env", "prod")
        with pytest.raises(rv.ReviewError):
            rv.submit(db, db.merge(expert), db.get(KbVersion, r.version_id))
        assert not rv.level3(db.get(KbVersion, r.version_id))


def test_non_author_expert_publishes_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_base_url", "")          # 未接模型：Agent 记一条说明后即可拍板
    author, reviewer = _user(("knowledge_expert",)), _user(("knowledge_expert",))
    (tmp_path / "g.md").write_text(_golden("t-gld-publish"), encoding="utf-8")
    with SessionLocal() as db:
        r = importer.import_dir(db, db.merge(author), tmp_path, submit=True, ai_baseline=True)[0]
        v = db.get(KbVersion, r.version_id)
        from app.kb import agent_review
        agent_review.run(db, v)
        with pytest.raises(rv.ReviewError):
            rv.review(db, db.merge(author), v, "content", "approve")          # 作者不能自批
        rv.review(db, db.merge(reviewer), v, "content", "approve")
        db.refresh(v)
        assert v.status == "published"


def test_cli_flag(tmp_path, capsys):
    expert = _user(("knowledge_expert",))
    (tmp_path / "g.md").write_text(_golden("t-gld-cli"), encoding="utf-8")
    assert cli_main(["kb-import", str(tmp_path), "--as", expert.username, "--submit", "--ai-baseline"]) == 0
    assert "AI 基线" in capsys.readouterr().out
