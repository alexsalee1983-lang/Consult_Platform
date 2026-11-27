"""四期 F2③：v4.0 验收口径（经验萃取跳过率、四格分列、四格关键章节就绪度）。"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import Project
from app.services import indicators_v4

from .test_indicators import NOW, _sign
from .test_kb_citations import library  # noqa: F401


def _p(db, industry: str, service: str = "SOC 运营体系设计") -> Project:
    p = Project(code=f"V4-{secrets.token_hex(3).upper()}", service=service, industry=industry, tier="standard")
    db.add(p)
    db.flush()
    return p


def test_grids_fixed_four():
    assert [g["key"] for g in indicators_v4.grid_rows()] == ["SOC·银行", "SOC·电力", "IRE·银行", "IRE·电力"]


def test_grid_of_project():
    with SessionLocal() as db:
        assert indicators_v4.grid_of(db, _p(db, "银行")) == "SOC·银行"
        assert indicators_v4.grid_of(db, _p(db, "电力", "IRE 应急响应")) == "IRE·电力"
        assert indicators_v4.grid_of(db, _p(db, "证券")) == indicators_v4.OTHER
        db.rollback()


def test_fabricated_by_grid(library):  # noqa: F811
    with SessionLocal() as db:
        bank, grid = _p(db, "银行"), _p(db, "电力", "IRE 应急响应")
        _sign(db, bank, "D5", [("现状", "依据《示范引用法》第九条。")], ["submit", "sign"])
        _sign(db, grid, "D5", [("现状", "依据《示范引用法》第一条。")], ["submit", "sign"])
        rep = indicators_v4.by_grid(db, NOW - timedelta(days=1), NOW, project_ids=[bank.id, grid.id])
        db.rollback()
    rows = {r["key"]: r for r in rep}
    assert rows["SOC·银行"]["fabricated_clauses"] == 1 and rows["SOC·银行"]["signed"] == 1
    assert rows["IRE·电力"]["fabricated_clauses"] == 0 and rows["IRE·电力"]["signed"] == 1
    assert rows["SOC·电力"]["signed"] == 0 and rows["SOC·电力"]["auto_publish_rate"] is None


def test_item_applies():
    f = indicators_v4.item_applies
    assert f("SOC", ["银行"], "SOC·银行") and not f("SOC", ["银行"], "SOC·电力")
    assert f("GEN", ["通用"], "IRE·电力") and f("IRE", [], "IRE·银行")
    assert not f("SOC", ["通用"], "IRE·银行")


def test_skip_rate_threshold():
    s = indicators_v4.skip_status({"answered": 6, "skipped": 4})
    assert s["value"] == 0.4 and s["status"] == "breach" and s["threshold"] == 0.3
    assert indicators_v4.skip_status({"answered": 7, "skipped": 3})["status"] == "ok"
    none = indicators_v4.skip_status({"answered": 0, "skipped": 0})
    assert none["value"] is None and none["status"] == "na"


def test_readiness_grids_uncalibrated_and_target(monkeypatch):
    evalsets = [{"product": "SOC", "industry": "", "chapters": [{"readiness": 0.5}, {"readiness": 0.7}]},
                {"product": "IRE", "industry": "电力", "chapters": [{"readiness": 0.2}]}]
    monkeypatch.setattr(get_settings(), "kb_readiness_targets", "")
    rows = {r["key"]: r for r in indicators_v4.readiness_grids(evalsets)}
    assert rows["SOC·银行"]["readiness"] == 0.6 and rows["SOC·电力"]["readiness"] == 0.6   # 不分行业的评测集两格都算
    assert rows["SOC·银行"]["target"] is None and rows["SOC·银行"]["status"] == "uncalibrated"
    assert rows["SOC·银行"]["label"] == "未校准"
    assert rows["IRE·银行"]["readiness"] is None and rows["IRE·银行"]["status"] == "na"
    monkeypatch.setattr(get_settings(), "kb_readiness_targets", "SOC·银行=0.7, IRE-电力=0.1,坏=x")
    rows = {r["key"]: r for r in indicators_v4.readiness_grids(evalsets)}
    assert rows["SOC·银行"]["target"] == 0.7 and rows["SOC·银行"]["gap"] == -0.1
    assert rows["SOC·银行"]["status"] == "below"
    assert rows["IRE·电力"]["target"] == 0.1 and rows["IRE·电力"]["status"] == "ok"


def test_summary_shape_and_api(library):  # noqa: F811
    with SessionLocal() as db:
        rep = indicators_v4.summary(db, datetime(2001, 1, 1, tzinfo=timezone.utc),
                                    datetime(2001, 2, 1, tzinfo=timezone.utc), project_ids=[])
    assert set(rep) == {"extraction_skip", "grids", "readiness_grids"}
    assert len(rep["grids"]) == 4 and len(rep["readiness_grids"]) == 4
    from .conftest import create_user, logged_in_client
    from app.main import app
    lead = create_user(("consultant", "project_lead"), {})
    with logged_in_client(app, lead) as c:
        body = c.get("/api/indicators?days=30").json()
        assert set(body["v4"]) == {"extraction_skip", "grids", "readiness_grids"}
        assert len(c.get("/api/kb/readiness").json()["grids"]) == 4


def test_cli_v4(capsys):
    from app import cli
    assert cli.main(["indicators", "--days", "30", "--v4"]) == 0
    out = capsys.readouterr().out
    assert "经验萃取跳过率" in out and "SOC·银行" in out and "IRE·电力" in out
