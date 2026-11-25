"""四期 B25：来源批量登记（kb-source-import）。资料仓已批准来源清单 → 候选/试用，走 source_registry 规则与审计；不能批量转正式。"""
import csv

from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.core.permissions import KNOWLEDGE_EXPERT
from app.models import AuditLog, KbSource

from .conftest import create_user

COLS = ["name", "url", "license", "authority_kind", "publisher", "note", "status"]


def _csv(tmp_path, rows):
    p = tmp_path / "sources.csv"
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLS})
    return p


def _row(i, **kw):
    return {"name": f"测试来源 {i}", "url": f"https://imp.example.gov.cn/b25-{i}", "license": "公开发布，可内部引用并注明出处",
            "authority_kind": "法律", "publisher": "测试机关", **kw}


def test_import_trial_and_candidate_idempotent(tmp_path, capsys):
    user = create_user((KNOWLEDGE_EXPERT,))[0]
    p = _csv(tmp_path, [_row(1, status="trial"), _row(2), _row(3, authority_kind="国家标准", status="candidate")])
    assert cli_main(["kb-source-import", str(p), "--as", user]) == 0
    out = capsys.readouterr().out
    assert "新登记 3" in out
    with SessionLocal() as db:
        got = {s.url: s for s in db.scalars(select(KbSource).where(KbSource.url.like("https://imp.example.gov.cn/b25-%")))}
        assert got["https://imp.example.gov.cn/b25-1"].status == "trial"
        assert got["https://imp.example.gov.cn/b25-2"].status == "candidate"
        assert got["https://imp.example.gov.cn/b25-3"].authority_level == 2
        assert all(s.created_by == user for s in got.values())
        assert db.scalar(select(AuditLog).where(AuditLog.action == "kb.source.create", AuditLog.actor == user))
    # 再跑一遍：已登记的跳过，不报错
    assert cli_main(["kb-source-import", str(p), "--as", user]) == 0
    assert "新登记 0" in capsys.readouterr().out


def test_import_rejects_active_and_bad_rows(tmp_path, capsys):
    user = create_user((KNOWLEDGE_EXPERT,))[0]
    p = _csv(tmp_path, [_row(10, status="active"), _row(11, license=""), _row(12, authority_kind="论坛帖子"),
                        _row(13, status="trial")])
    assert cli_main(["kb-source-import", str(p), "--as", user]) == 1   # 有错行退出码 1
    out = capsys.readouterr().out
    assert "第 2 行" in out and "第 3 行" in out and "第 4 行" in out
    with SessionLocal() as db:
        urls = set(db.scalars(select(KbSource.url).where(KbSource.url.like("https://imp.example.gov.cn/b25-1_"))))
    assert urls == {"https://imp.example.gov.cn/b25-13"}                 # 好行照常登记，正式只能页面双人批准


def test_import_requires_known_user(tmp_path, capsys):
    p = _csv(tmp_path, [_row(20)])
    assert cli_main(["kb-source-import", str(p), "--as", "no-such-user"]) == 1
    with SessionLocal() as db:
        assert db.scalar(select(KbSource).where(KbSource.url == "https://imp.example.gov.cn/b25-20")) is None
