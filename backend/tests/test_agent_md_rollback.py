"""四期 F3②e：Agent md 版本库与质量事件“回滚 Agent 版本”（负责人 2026-11-24 同意）。

- 版本库：平台每取用一次 md 就把（公共规范 + 角色 md）按内容哈希存一份，同一哈希只存一次；
  部署机只部署 main，所以库里只有已合入的版本。咨询业务负责人、系统管理员可查历史与内容。
- 回滚：质量事件遏制选“回滚 Agent 版本”并给出 Agent 与目标版本，当场写回滚指针，运行时按指针取 md；
  目标必须在版本库里（不能回滚到没见过的版本）；处置人须同时是咨询业务负责人（系统角色）——
  系统管理员与其他角色互斥（三权分立），不能兼任处置人，故不要求兼系统管理员。
- 撤销：咨询业务负责人或系统管理员撤销回滚，恢复仓库当前版本。审计只记哈希，不记 md 原文。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents import call, registry, runtime
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AgentMdPin, AgentMdVersion, AuditLog

from .conftest import create_user, logged_in_client

REPO_AGENTS = Path(__file__).resolve().parents[2] / "agents"
CODE = "report-writer"


@pytest.fixture()
def agents_copy(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    shutil.copytree(REPO_AGENTS, d)
    monkeypatch.setattr(get_settings(), "agents_dir", str(d))
    registry.clear_cache()
    yield d
    registry.clear_cache()
    with SessionLocal() as db:
        pin = db.get(AgentMdPin, CODE)
        if pin is not None:
            db.delete(pin)
            db.commit()


def _edit(d: Path, extra: str) -> None:
    md = d / "roles" / f"{CODE}.md"
    time.sleep(0.01)
    md.write_text(md.read_text(encoding="utf-8") + f"\n{extra}\n", encoding="utf-8")


def _two_versions(d: Path) -> tuple[str, str]:
    with SessionLocal() as db:
        v1 = call.load(db, CODE).version
        _edit(d, "F3②e 新版补充一句。")
        v2 = call.load(db, CODE).version
    assert v1 != v2
    return v1, v2


def _incident(c, **kw) -> int:
    body = {"category": "improper_advice", "project": "", "ref_kind": "run", "ref_id": "1", **kw}
    r = c.post("/api/quality-incidents", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _rollback(c, iid: int, agent: str, version: str):
    return c.post(f"/api/quality-incidents/{iid}/events",
                  json={"step": "contain", "action": "rollback_agent", "agent": agent, "version": version})


def test_versions_captured_once_per_hash(agents_copy):
    v1, v2 = _two_versions(agents_copy)
    with SessionLocal() as db:
        call.load(db, CODE)
        rows = db.scalars(select(AgentMdVersion).where(AgentMdVersion.agent_code == CODE)).all()
        got = [r.version for r in rows]
        assert got.count(v1) == 1 and got.count(v2) == 1
        row = next(r for r in rows if r.version == v1)
        assert "公共规范" in row.common_text and f"name: {CODE}" in row.role_text
        assert "F3②e 新版补充一句" not in row.role_text


def test_rollback_via_incident_pins_runtime_and_undo(agents_copy):
    v1, v2 = _two_versions(agents_copy)
    lead = logged_in_client(app, create_user(("project_lead",)))
    iid = _incident(lead)
    r = _rollback(lead, iid, CODE, v1)
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        doc = call.load(db, CODE)
        assert doc.version == v1 and "F3②e 新版补充一句" not in doc.body
        assert runtime.agent_info(db, db.get(runtime.Agent, CODE)).prompt_version == v1
        pin = db.get(AgentMdPin, CODE)
        assert pin.version == v1 and pin.incident_id == iid
        a = db.scalars(select(AuditLog).where(AuditLog.action == "agent.md_rollback")
                       .order_by(AuditLog.id.desc())).first()
        assert a is not None and v1 in a.detail and v2 in a.detail and "公共规范" not in a.detail

    r = lead.get(f"/api/agent-md/{CODE}/versions")
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == v2 and body["pinned"] == v1
    assert {v["version"] for v in body["items"]} >= {v1, v2}
    r = lead.get(f"/api/agent-md/{CODE}/versions/{v1}")
    assert r.status_code == 200 and f"name: {CODE}" in r.json()["role_text"]

    admin = logged_in_client(app, create_user(("sys_admin",)))
    r = admin.post(f"/api/agent-md/{CODE}/unpin", json={"reason": "已修复 md"})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.get(AgentMdPin, CODE) is None
        assert call.load(db, CODE).version == v2
        assert db.scalar(select(AuditLog).where(AuditLog.action == "agent.md_unpin")) is not None
    assert admin.post(f"/api/agent-md/{CODE}/unpin", json={"reason": "再撤一次"}).status_code == 404


def test_rollback_rules(agents_copy):
    v1, v2 = _two_versions(agents_copy)
    lead = logged_in_client(app, create_user(("project_lead",)))
    iid = _incident(lead)
    assert _rollback(lead, iid, CODE, "0" * 12).status_code == 422          # 版本库里没有：不能回滚
    assert _rollback(lead, iid, CODE, v2).status_code == 422                # 已是当前版本
    assert _rollback(lead, iid, "no-such-agent", v1).status_code == 422
    assert _rollback(lead, iid, "", "").status_code == 422
    manager = logged_in_client(app, create_user(("consultant",), {"BANK-A": "manager"}))
    iid2 = _incident(manager, project="BANK-A")
    assert _rollback(manager, iid2, CODE, v1).status_code == 403            # 项目经理可处置，但不能回滚 Agent
    with SessionLocal() as db:
        assert db.get(AgentMdPin, CODE) is None


def test_versions_api_permissions(agents_copy):
    for roles, projects, want in ((("consultant",), {"BANK-A": "manager"}, 403), (("knowledge_admin",), {}, 403),
                                  (("sys_admin",), {}, 200), (("project_lead",), {}, 200)):
        c = logged_in_client(app, create_user(roles, projects))
        assert c.get(f"/api/agent-md/{CODE}/versions").status_code == want
    lead = logged_in_client(app, create_user(("project_lead",)))
    assert lead.get("/api/agent-md/no-such-agent/versions").status_code == 404
    assert lead.get(f"/api/agent-md/{CODE}/versions/{'0' * 12}").status_code == 404


def test_registry_method_layer_follows_pin(agents_copy):
    """F3②f：有回滚指针时，配置登记方法层按指针版本计哈希，撤销后恢复。"""
    from app.services import config_registry

    v1, v2 = _two_versions(agents_copy)

    def agent_item() -> dict:
        reg = config_registry.build()
        method = next(x for x in reg["layers"] if x["key"] == "method")
        return next(i for i in method["items"] if i["key"] == f"agent:{CODE}")

    before = config_registry.effective_sha()
    assert agent_item()["value"] == v2 and not agent_item().get("pinned")
    with SessionLocal() as db:
        db.add(AgentMdPin(agent_code=CODE, version=v1, pinned_by="tester"))
        db.commit()
        assert config_registry.effective_sha(db) != before
    item = agent_item()
    assert item["value"] == v1 and item["pinned"] is True and item["repo_version"] == v2
    with SessionLocal() as db:
        db.delete(db.get(AgentMdPin, CODE))
        db.commit()
    assert config_registry.effective_sha() == before
