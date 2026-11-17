"""门槛校准（kb-eval --sweep）：按评测题算各个重排门槛下的准确率，推荐一个值；不记检索词。"""
import json
import shutil
from pathlib import Path

import httpx
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import importer, sweep
from app.kb import review as rv
from app.models import AuditLog, KbItem, User
from app.services import gateway

from .conftest import create_user

SAMPLES = Path(__file__).resolve().parents[2] / "knowledge" / "samples"


def _user(roles):
    name, *_ = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


def _publish(tmp_path):
    d = tmp_path / "kb"
    shutil.copytree(SAMPLES, d)
    for f in d.glob("*.md"):
        f.write_text(f.read_text(encoding="utf-8").replace("key: ", "key: sw-", 1), encoding="utf-8")
    expert, expert2, admin = _user(("knowledge_expert",)), _user(("knowledge_expert",)), _user(("knowledge_admin",))
    with SessionLocal() as db:
        for r in importer.import_dir(db, db.merge(expert), d, submit=True):
            v = db.get(KbItem, db.scalar(select(KbItem.id).where(KbItem.import_key == r.key))).versions[-1]
            if v.status != "published":
                rv.review(db, db.merge(expert2), v, "content", "approve")
                rv.review(db, db.merge(admin), v, "compliance", "approve")


def _reranker(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        docs = json.loads(request.content)["documents"]
        return httpx.Response(200, json={"results": [{"index": i, "relevance_score": 0.9 if "日志" in d else 0.02}
                                                     for i, d in enumerate(docs)]})
    monkeypatch.setattr(get_settings(), "rerank_base_url", "http://rerank.internal/v1")
    gateway.set_transport(httpx.MockTransport(handler))


QS = [{"id": "W1", "mode": "basis", "question": "网络日志 留存", "expect": ["sw-csl-art21-mlps"]},
      {"id": "W2", "mode": "basis", "question": "应急预案 外汇 交接班", "expect": []}]


def test_sweep_accuracy_by_threshold(tmp_path, monkeypatch):
    _publish(tmp_path)
    _reranker(monkeypatch)
    try:
        with SessionLocal() as db:
            rep = sweep.run(db, db.merge(_user(("consultant",))), QS, basis=[0.0, 0.01, 0.05, 0.5, 0.95])
    finally:
        gateway.set_transport(None)
    rows = {r["threshold"]: r for r in rep.basis}
    # 门槛 0 / 0.01：负例返回了 0.02 分的条目（误命中）；0.05、0.5：两题都对；0.95：正例也被挡掉
    assert rows[0.0]["accuracy"] == 0.5 and rows[0.0]["false_hits"] == 1
    assert rows[0.05]["accuracy"] == 1.0 and rows[0.5]["accuracy"] == 1.0
    assert rows[0.95]["accuracy"] == 0.5 and rows[0.95]["missed"] == 1
    assert rep.recommend["basis"] in (0.05, 0.5)
    assert rep.current["basis"] == get_settings().kb_basis_min_rerank
    text = sweep.format_report(rep)
    assert "推荐" in text and "网络日志" not in text            # 报告不出现检索词


def test_sweep_needs_rerank(tmp_path):
    with SessionLocal() as db:
        rep = sweep.run(db, db.merge(_user(("consultant",))), QS)
    assert rep.skipped == 2 and rep.recommend == {}
    assert "重排不可用" in sweep.format_report(rep)


def test_cli_sweep_audit_has_no_query(tmp_path, monkeypatch, capsys):
    _publish(tmp_path)
    qf = tmp_path / "q.yaml"
    qf.write_text("\n".join(f"- id: {q['id']}\n  mode: basis\n  question: {q['question']}\n  expect: {q['expect']}"
                            for q in QS), encoding="utf-8")
    user = _user(("consultant",))
    _reranker(monkeypatch)
    try:
        rc = cli_main(["kb-eval", str(qf), "--as", user.username, "--sweep", "--out", str(tmp_path / "s.json")])
    finally:
        gateway.set_transport(None)
    assert rc == 0
    out = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert "网络日志" not in json.dumps(out, ensure_ascii=False) and out["basis"]
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "kb.eval_sweep")).all()[-1]
    assert "网络日志" not in a.detail and json.loads(a.detail)["questions"] == 2
    assert "推荐" in capsys.readouterr().out
