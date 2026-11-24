"""三期 F4 最小版：改稿差异转候选。差异分删除、改写、新增；删改归“候选常见坑”、新增归“候选评审项”；
初稿取该章第一个 Agent 版本、签署稿取签署记录里的版本；开关默认关、生产不开；非成员 404、严格档 422、
未签署和不同意回流 409；摘录换掉项目代号、机密级命中整条丢弃；审计不记正文；四期 F4①候选以项目沉淀经投料箱投料，重复拒收、无存储只返回。"""
import json
import secrets

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, KbUpload, Deliverable, DeliverableSection, Project, SectionVersion, SignOff
from app.project import revision_mining as rm
from app.services import storage

from .conftest import create_user, logged_in_client

DRAFT = "SOC 现有 6 人，全部白班。\n建议立即采购 SIEM 平台。\n值班记录为纸质。"
SIGNED = "SOC 现有 6 人，全部白班，夜间无人值守。\n值班记录为纸质。\n补充：整改需列明验收标准与责任人。"


def test_diff_lines_kinds():
    changes = rm.diff_lines(DRAFT, SIGNED)
    kinds = sorted(c.kind for c in changes)
    assert kinds == ["add", "delete", "rewrite"]
    rw = next(c for c in changes if c.kind == "rewrite")
    assert rw.before == "SOC 现有 6 人，全部白班。" and "夜间无人值守" in rw.after and rw.ratio >= rm.REWRITE_MIN
    assert next(c for c in changes if c.kind == "delete").before == "建议立即采购 SIEM 平台。"
    assert rm.diff_lines("同一行", "同一行") == []


def _project(tier: str = "standard", status: str = "signed", flowback: bool = True, body2: str = SIGNED,
             agent_first: bool = True) -> str:
    code = f"F4-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier=tier, flowback_allowed=flowback)
        d = Deliverable(code="D4", title="差距分析报告", status=status)
        p.deliverables = [d]
        db.add(p)
        db.flush()
        s = DeliverableSection(deliverable_id=d.id, seq=1, title=f"{code} 现状概述", current_version=3, created_by="u")
        s.versions = [
            SectionVersion(version=1, body="顾问手写的提纲", char_count=1, body_sha="x", author="u", agent_code=""),
            SectionVersion(version=2, body=DRAFT, char_count=1, body_sha="x", author="u",
                           agent_code="writer" if agent_first else ""),
            SectionVersion(version=3, body=body2, char_count=1, body_sha="x", author="u", agent_code=""),
        ]
        db.add(s)
        db.flush()
        if status == "signed":
            db.add(SignOff(project_id=p.id, deliverable_id=d.id, action="sign", revision=3, versions={"1": 3},
                           actor="pm"))
        db.commit()
    return code


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(get_settings(), "f4_revision_mining", True)
    monkeypatch.setattr(storage, "enabled", lambda: False)        # 默认无对象存储：只返回候选


@pytest.fixture()
def s3(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "put", lambda area, key, data, ct="": saved.__setitem__((area, key), data))
    return saved


def _client(code: str):
    return logged_in_client(app, create_user(("consultant",), {code: "member"}))


def _url(code: str) -> str:
    return f"/api/projects/{code}/deliverables/D4/revision-candidates"


def test_member_gets_pitfall_and_review_item_candidates(on):
    code = _project()
    with _client(code) as c:
        r = c.post(_url(code))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["available"] is True and data["queued"] == 0 and data["reason"] == "storage"
    assert data["counts"] == {"sections": 1, "delete": 1, "rewrite": 1, "add": 1, "blocked": 0}
    kinds = {x["kind"]: x for x in data["candidates"]}
    assert set(kinds) == {"pitfall", "review_item"}
    assert kinds["pitfall"]["change_count"] == 2 and kinds["review_item"]["change_count"] == 1
    assert code not in json.dumps(data, ensure_ascii=False)          # 项目代号换成【客户】
    assert kinds["pitfall"]["section_title"] == "【客户】 现状概述"
    with SessionLocal() as db:
        log = db.scalar(select(AuditLog).where(AuditLog.action == "f4.revision_mining")
                        .order_by(AuditLog.id.desc()))
        assert log is not None and log.project_code == code
        assert "SIEM" not in log.detail and "夜间" not in log.detail


def test_switch_off_or_prod_is_404(monkeypatch):
    code = _project()
    with _client(code) as c:
        assert c.post(_url(code)).status_code == 404
        monkeypatch.setattr(get_settings(), "f4_revision_mining", True)
        monkeypatch.setattr(get_settings(), "app_env", "prod")
        assert c.post(_url(code)).status_code == 404


def test_non_member_404_strict_422_unsigned_and_no_flowback_409(on):
    code = _project()
    with logged_in_client(app, create_user(("consultant",), {})) as c:
        assert c.post(_url(code)).status_code == 404
    for kwargs, status in (({"tier": "strict"}, 422), ({"status": "review"}, 409), ({"flowback": False}, 409)):
        code = _project(**kwargs)
        with _client(code) as c:
            assert c.post(_url(code)).status_code == status, kwargs


def test_no_agent_draft_gives_no_candidates(on):
    code = _project(agent_first=False)
    with _client(code) as c:
        data = c.post(_url(code)).json()
    assert data["candidates"] == [] and data["counts"]["sections"] == 0


def test_guard_hit_drops_candidate(on):
    code = _project(body2=DRAFT + "\n管理员口令 password=Xk9#mP2$vL7q")
    with _client(code) as c:
        data = c.post(_url(code)).json()
    assert data["counts"]["blocked"] == 1
    assert all(x["kind"] != "review_item" for x in data["candidates"])


def test_candidates_go_to_intake_as_project_sediment(on, s3):
    code = _project()
    with _client(code) as c:
        r = c.post(_url(code))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["queued"] == 2 and data["reason"] == "" and data["upload_id"]
        again = c.post(_url(code)).json()                              # 内容相同：投料箱按哈希拒收
        assert again["queued"] == 0 and again["reason"] == "duplicate"
    with SessionLocal() as db:
        up = db.get(KbUpload, data["upload_id"])
        assert up.source_project == code and up.kind == "markdown" and code not in up.filename
        assert up.status == "received" and up.job_id                    # 照常排解析，之后去标识与审核
    body = next(iter(s3.values())).decode("utf-8")
    assert "候选常见坑" in body and "候选评审项" in body and "夜间无人值守" in body
    assert code not in body                                            # 项目代号已换成【客户】
    with SessionLocal() as db:
        log = db.scalar(select(AuditLog).where(AuditLog.action == "f4.revision_mining", AuditLog.project_code == code)
                        .order_by(AuditLog.id.desc()))
        assert "夜间" not in log.detail and "duplicate" in log.detail


def test_no_candidates_not_submitted(on, s3):
    code = _project(body2=DRAFT)
    with _client(code) as c:
        data = c.post(_url(code)).json()
    assert data["candidates"] == [] and data["queued"] == 0 and data["reason"] == "no_candidates" and not s3
