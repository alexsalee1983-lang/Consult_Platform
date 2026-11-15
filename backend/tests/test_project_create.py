"""立项与项目配置快照（二期 A3，docs/接口约定.md 第四节）。"""
from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT, PROJECT_LEAD
from app.main import app
from app.models import AuditLog, Project, ProjectConfig
from app.products import config
from app.project import configs, runs

from .conftest import create_user, logged_in_client
from .test_product_config import AGENTS, MODULE, PRODUCT, _write


def _code() -> str:
    return f"QA-{secrets.token_hex(2).upper()}"


@pytest.fixture
def lead():
    c = logged_in_client(app, create_user((PROJECT_LEAD,)))
    yield c
    c.__exit__(None, None, None)


def test_catalog_options_list_soc(lead) -> None:
    r = lead.get("/api/catalog/products")
    assert r.status_code == 200
    soc = next(p for p in r.json() if p["code"] == "SOC")
    assert soc["name"] == "SOC 运营体系设计" and len(soc["deliverables"]) == 8


def test_lead_creates_project_via_web(lead) -> None:
    code = _code()
    mgr, *_ = create_user((CONSULTANT,))
    r = lead.post("/api/projects", json={"code": code.lower(), "tier": "strict", "product": "SOC", "manager": mgr,
                                         "industry": "电力"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == code and body["tier"] == "strict" and body["product"] == "SOC"
    assert body["service"] == "SOC 运营体系设计" and body["modules"] == [] and len(body["product_version"]) == 12
    assert lead.post("/api/projects", json={"code": code, "tier": "standard", "product": "SOC"}).status_code == 409
    assert lead.get(f"/api/projects/{code}").status_code == 404          # 立项人不是成员：看不到
    c = logged_in_client(app, (mgr, *_))
    try:
        d = c.get(f"/api/projects/{code}").json()
        assert [x["code"] for x in d["deliverables"]] == [f"D{i}" for i in range(1, 9)]
        assert d["product"] == "SOC" and d["product_name"] == "SOC 运营体系设计"
    finally:
        c.__exit__(None, None, None)
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        row = db.scalar(select(ProjectConfig).where(ProjectConfig.project_id == p.id))
        assert row.product == "SOC" and row.snapshot["chain"][0]["key"] == "facts"
        audit = db.scalars(select(AuditLog).where(AuditLog.action == "project.create", AuditLog.result == "allowed")
                           .order_by(AuditLog.id.desc())).first()
        assert audit.project_code == code and "电力" not in (audit.detail or "")


@pytest.mark.parametrize(("body", "status"), [
    ({"code": "某银行", "tier": "standard", "product": "SOC"}, 422),
    ({"code": "QA-BAD1", "tier": "both", "product": "SOC"}, 422),
    ({"code": "QA-BAD2", "tier": "standard", "product": "ISO"}, 422),
    ({"code": "QA-BAD3", "tier": "standard", "product": "SOC", "modules": ["DJBH"]}, 422),
    ({"code": "QA-BAD4", "tier": "standard", "product": "SOC", "manager": "no-such-user"}, 422),
    ({"code": "QA-BAD5", "tier": "standard", "product": "SOC", "weeks": 0}, 422),
])
def test_create_rejects(lead, body: dict, status: int) -> None:
    assert lead.post("/api/projects", json=body).status_code == status
    with SessionLocal() as db:
        assert db.scalar(select(Project.id).where(Project.code == body["code"].upper())) is None


def test_admin_cannot_be_manager(lead) -> None:
    admin, *_ = create_user(("sys_admin",))
    r = lead.post("/api/projects", json={"code": _code(), "tier": "standard", "product": "SOC", "manager": admin})
    assert r.status_code == 422


def test_snapshot_frozen_and_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """立项后改配置文件不影响项目；勾模块的项目，交付链读到模块追加的说明。"""
    _write(tmp_path, PRODUCT, MODULE)
    monkeypatch.setattr(get_settings(), "catalog_dir", str(tmp_path))
    monkeypatch.setattr(config, "_known_agents", lambda: AGENTS)
    monkeypatch.setattr(config.meta, "PRODUCTS", {**config.meta.PRODUCTS})
    mgr, *_ = create_user((CONSULTANT,))
    code = _code()
    assert cli_main(["create-project", "--code", code, "--tier", "standard", "--manager", mgr,
                     "--product", "SOC", "--module", "DJBH"]) == 0
    changed = dict(PRODUCT, name="SOC 改版")
    _write(tmp_path, changed, MODULE)
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        assert [d.code for d in p.deliverables] == ["D1", "D2", "D3"]
        snap = configs.snapshot(db, p)
        assert snap["product_name"] == "SOC 运营体系设计" and [m["code"] for m in snap["modules"]] == ["DJBH"]
        gap = runs.steps(db, p)[1]
        assert gap.instruction.endswith("逐项对照第三级管理要求。")
        assert config.compose_codes("SOC")["product_name"] == "SOC 改版"


def test_backfill_old_projects() -> None:
    """一期项目（没有快照行）读快照按 SOC 现算，启动时补建并进审计。"""
    with SessionLocal() as db:
        p = Project(code=_code(), service="SOC 运营体系设计", industry="", tier="standard")
        db.add(p)
        db.commit()
        assert configs.snapshot(db, p)["product"] == "SOC"
        assert configs.backfill(db) >= 1
        row = db.scalar(select(ProjectConfig).where(ProjectConfig.project_id == p.id))
        assert row.product == "SOC" and row.created_by == "platform" and row.modules == []
        assert configs.backfill(db) == 0
