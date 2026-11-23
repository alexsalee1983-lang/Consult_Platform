"""三期 F2②：就绪度看板（章节 × 槽位；按类型的专家分钟 / 条、采纳率、修改率；修改率长期接近 0 提醒）。"""
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.core.db import SessionLocal, utcnow
from app.kb import readiness as rd
from app.main import app
from app.models import KbItem, KbReview, KbVersion

from .conftest import create_user, logged_in_client

PREFIX = "rd-"


def _item(db, key: str, doc_type: str = "MTD", layer: str = "L2", status: str | None = "published",
          retired: bool = False, recheck: str = "", reviews: list[dict] | None = None) -> KbItem:
    n = db.scalar(select(KbItem.id).order_by(KbItem.id.desc())) or 0
    item = KbItem(doc_id=f"{layer}-TST-RDX-{doc_type}-{900000 + n + 1}", layer=layer, domain="TST", product="RDX",
                  doc_type=doc_type, stage="通用", import_key=PREFIX + key, retired=retired)
    db.add(item)
    db.flush()
    if status:
        v = KbVersion(item_id=item.id, version=1, status=status, title="虚拟条目", body="正文", source_type="expert",
                      recheck_state=recheck)
        db.add(v)
        db.flush()
        if status == "published":
            item.current_version_id = v.id
        for r in reviews or []:
            db.add(KbReview(version_id=v.id, reviewer="rd_expert", review_role=r.get("role", "content"),
                            decision=r["decision"], findings=r.get("findings", {}),
                            created_at=r.get("at", utcnow() - timedelta(days=1))))
    db.commit()
    return item


@pytest.fixture(scope="module")
def evalset_file(tmp_path_factory) -> Iterator[Path]:
    """两章：第一章 4 槽各种状态，第二章 3 槽全就绪（其中一条依据）。"""
    with SessionLocal() as db:
        _item(db, "ok-1")
        _item(db, "cand", status="in_review")
        _item(db, "recheck", recheck="pending")
        _item(db, "gone", retired=True)
        _item(db, "ok-2", doc_type="TPL")
        _item(db, "ok-3", doc_type="PIT")
        _item(db, "basis", doc_type="REG", layer="L4")
    p = tmp_path_factory.mktemp("rd") / "soc.yaml"
    p.write_text(yaml.safe_dump({"product": "SOC", "chapters": [
        {"id": "SOC-D5-01", "step": "design_doc", "title": "组织与人员",
         "expect": [PREFIX + k for k in ("ok-1", "cand", "recheck", "nothing")]},
        {"id": "SOC-D5-02", "step": "design_doc", "title": "告警分级",
         "expect": [PREFIX + k for k in ("ok-2", "ok-3", "basis")]},
    ]}, allow_unicode=True), encoding="utf-8")
    yield p
    with SessionLocal() as db:
        for item in db.scalars(select(KbItem).where(KbItem.import_key.like(f"{PREFIX}%"))):
            item.retired = True
        db.commit()


def test_slot_states_and_chapter_readiness(evalset_file):
    with SessionLocal() as db:
        out = rd.matrix(db, [evalset_file])
    assert out["errors"] == []
    (es,) = out["evalsets"]
    ch1, ch2 = es["chapters"]
    assert [s["state"] for s in ch1["slots"]] == ["ready", "candidate", "recheck", "missing"]
    assert ch1["readiness"] == 0.25 and ch1["missing"] == [PREFIX + k for k in ("cand", "recheck", "nothing")]
    assert ch1["slots"][0]["doc_type"] == "MTD" and ch1["slots"][3]["doc_type"] == ""
    assert ch2["readiness"] == 1.0
    assert es["readiness"] == 0.625                       # 章节平均
    assert es["lowest"] == {"id": "SOC-D5-01", "title": "组织与人员", "readiness": 0.25}
    assert es["basis_readiness"] == 1.0                   # 依据类槽位（L4）单列
    assert out["readiness"] == 0.625


def test_retired_is_not_ready(evalset_file, tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text(yaml.safe_dump({"product": "SOC", "chapters": [
        {"id": "X", "step": "facts", "title": "t", "expect": [PREFIX + k for k in ("gone", "ok-1", "ok-2")]}]}),
        encoding="utf-8")
    with SessionLocal() as db:
        (ch,) = rd.matrix(db, [p])["evalsets"][0]["chapters"]
    assert ch["slots"][0]["state"] == "retired" and ch["readiness"] == round(2 / 3, 4)


def test_bad_evalset_reported_not_raised(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("chapters: []", encoding="utf-8")
    with SessionLocal() as db:
        out = rd.matrix(db, [bad, tmp_path / "nope.yaml"])
    assert out["evalsets"] == [] and len(out["errors"]) == 2 and out["readiness"] is None
    assert all(str(tmp_path) not in e["file"] for e in out["errors"])      # 只给文件名，不暴露部署路径


def test_paths_from_dir_and_default(tmp_path, monkeypatch):
    (tmp_path / "a.yaml").write_text("x: 1", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv("KB_READINESS_EVALSETS", str(tmp_path))
    from app.core.config import get_settings
    get_settings.cache_clear()
    try:
        assert rd.evalset_paths() == [tmp_path / "a.yaml"]
        monkeypatch.setenv("KB_READINESS_EVALSETS", "")
        get_settings.cache_clear()
        assert [p.name for p in rd.evalset_paths()] == ["assembly_samples.yaml"]     # 默认用平台回归样例
    finally:
        get_settings.cache_clear()


def _expert_rows(db, doc_type: str, n: int, edited: int, minutes: float | None, rejects: int = 0,
                 days_ago: int = 1) -> None:
    for i in range(n + rejects):
        approve = i < n
        f = {}
        if approve and minutes is not None:
            f = {"minutes": minutes, "edited": i < edited}
        _item(db, f"{doc_type}-{days_ago}-{i}", doc_type=doc_type, status="published" if approve else "rejected",
              reviews=[{"decision": "approve" if approve else "reject", "findings": f,
                        "at": utcnow() - timedelta(days=days_ago)},
                       {"role": "agent", "decision": "flag", "at": utcnow() - timedelta(days=days_ago)}])


def test_expert_metrics_by_type_and_low_edit_alert():
    with SessionLocal() as db:
        _expert_rows(db, "ZZA", 20, 0, 3.0, rejects=5)      # 20 条采纳全没改：修改率 0，触发提醒
        _expert_rows(db, "ZZB", 4, 2, 6.0)                 # 条数不够，不提醒
        _expert_rows(db, "ZZC", 3, 0, None)                # 没有耗时与修改记录（B17 前）
        _expert_rows(db, "ZZD", 5, 0, 1.0, days_ago=200)   # 窗外
        end = utcnow()
        rows = {r["doc_type"]: r for r in rd.expert_metrics(db, end - timedelta(days=90), end)["by_type"]}
        alerts = rd.expert_metrics(db, end - timedelta(days=90), end)["alerts"]
    zza = rows["ZZA"]
    assert (zza["reviews"], zza["approved"], zza["adopt_rate"]) == (25, 20, 0.8)
    assert zza["minutes_per_item"] == 3.0 and zza["edit_rate"] == 0.0 and zza["low_edit"] is True
    assert rows["ZZB"]["edit_rate"] == 0.5 and rows["ZZB"]["low_edit"] is False
    assert rows["ZZC"]["minutes_per_item"] is None and rows["ZZC"]["edit_rate"] is None
    assert "ZZD" not in rows
    mine = [a for a in alerts if "ZZ" in a]
    assert len(mine) == 1 and "ZZA" in mine[0] and "抽查" in mine[0]


def test_api_roles():
    lead = logged_in_client(app, create_user(("project_lead",)))
    expert = logged_in_client(app, create_user(("knowledge_expert",)))
    consultant = logged_in_client(app, create_user(("consultant",)))
    try:
        r = expert.get("/api/kb/readiness?days=30")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["days"] == 30 and {"evalsets", "errors", "readiness", "experts"} <= set(body)
        assert body["experts"]["edit_min_reviews"] == rd.EDIT_MIN_REVIEWS
        assert lead.get("/api/kb/readiness").status_code == 200
        assert consultant.get("/api/kb/readiness").status_code == 403
        assert expert.get("/api/kb/readiness?days=0").status_code == 422
    finally:
        for c in (lead, expert, consultant):
            c.__exit__(None, None, None)
