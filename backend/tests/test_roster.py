"""三期 C6-P：Agent 名册按 9 个（迁移 0045）、旧名兼容、采集机区硬规则。

旧名兼容的切换方式：C6 合并步骤停用旧代号后，调用方仍用旧代号的自动转到新代号；旧代号启用时照旧。
这里临时停用 consultant-lead 验证切换，测完恢复。
"""
import json
from contextlib import contextmanager

import pytest
from sqlalchemy import select

from app.agents import registry, roster, runtime, tools
from app.core.db import SessionLocal
from app.main import app
from app.models import Agent, AuditLog, Conversation, User

from .conftest import create_user, logged_in_client


@contextmanager
def _disabled(code: str):
    with SessionLocal() as db:
        db.get(Agent, code).enabled = False
        db.commit()
    try:
        yield
    finally:
        with SessionLocal() as db:
            db.get(Agent, code).enabled = True
            db.commit()


def _member():
    cred = create_user(("consultant",), {"BANK-A": "member"})
    with SessionLocal() as db:
        return cred, db.scalar(select(User).where(User.username == cred[0]))


def test_nine_agents_registered_with_md_and_zone():
    assert len(roster.NINE) == 9 and len(set(roster.NINE_CODES)) == 9
    with SessionLocal() as db:
        for code, _name, side in roster.NINE:
            a = db.get(Agent, code)
            assert a is not None and a.enabled, code
            assert a.side == side, (code, a.side)
            assert registry.load(code) is not None, f"{code} 缺 md 或 name 不一致"


def test_aliases_point_into_nine_and_match_md_replaces():
    assert set(roster.ALIASES.values()) <= set(roster.NINE_CODES)
    assert not set(roster.ALIASES) & set(roster.NINE_CODES)
    with SessionLocal() as db:
        assert all(db.get(Agent, old) is not None for old in roster.ALIASES)   # 旧代号仍登记，已有对话可追溯
    for code in roster.NINE_CODES:
        for old in registry.load(code).replaces:
            assert roster.ALIASES.get(old) == code, f"{code}.md 写了 replaces {old}，名册里没有对应别名"
    assert set(registry.load("consult-coordinator").replaces) == {"consultant-lead", "delivery-pm"}
    assert not set(roster.TOOL_LAYER) & set(roster.ALIASES) and not set(roster.RESERVED) & set(roster.ALIASES)


def test_resolve_keeps_old_code_while_enabled():
    with SessionLocal() as db:
        assert roster.resolve(db, "consultant-lead") == "consultant-lead"
        assert roster.resolve(db, "research-interview") == "research-interview"
        assert roster.resolve(db, "nope") == "nope"
    with _disabled("consultant-lead"), SessionLocal() as db:
        assert roster.resolve(db, "consultant-lead") == "consult-coordinator"
        assert roster.resolve(db, "delivery-pm") == "delivery-pm"              # 只停了一个旧代号
    with _disabled("consultant-lead"), _disabled("consult-coordinator"), SessionLocal() as db:
        assert roster.resolve(db, "consultant-lead") == "consultant-lead"      # 新代号也不可用时不转，照旧报不可用


def test_retired_old_code_runs_as_new_agent_in_tools_and_conversation():
    cred, user = _member()
    with _disabled("consultant-lead"):
        with SessionLocal() as db:
            r = tools.invoke(db, db.merge(user), "consultant-lead", "project.read", {}, "BANK-A")
            assert r["project"]["code"] == "BANK-A"
            log = db.scalars(select(AuditLog).where(AuditLog.action == "tool.call", AuditLog.actor == user.username)
                             .order_by(AuditLog.id.desc())).first()
            detail = json.loads(log.detail)
            assert detail["agent"] == "consult-coordinator" and detail["alias_of"] == "consultant-lead"
            assert runtime.ready_doc(db, "consultant-lead").md_version.startswith("0.1")
        c = logged_in_client(app, cred)
        try:
            r = c.post("/api/projects/BANK-A/conversations", json={"agent_code": "consultant-lead"})
            assert r.status_code == 201 and r.json()["agent_code"] == "consult-coordinator"
            agents = {a["code"]: a for a in c.get("/api/agents").json()}
            assert "consultant-lead" not in agents and agents["consult-coordinator"]["ready"]
        finally:
            c.__exit__(None, None, None)
        with SessionLocal() as db:
            assert db.get(Conversation, r.json()["id"]).agent_code == "consult-coordinator"


def test_api_marks_old_codes_replaced_by():
    cred, _ = _member()
    c = logged_in_client(app, cred)
    try:
        agents = {a["code"]: a for a in c.get("/api/agents").json()}
    finally:
        c.__exit__(None, None, None)
    assert agents["consultant-lead"]["replaced_by"] == "consult-coordinator"
    assert agents["policy-drafter"]["replaced_by"] == "solution-designer"
    assert agents["consult-coordinator"]["replaced_by"] is None and agents["consult-coordinator"]["ready"]
    assert agents["kb-collector"]["side"] == "collector" and not agents["kb-collector"]["ready"]
    assert agents["kb-collector"]["tools"] == []


def test_collector_zone_reason_and_hard_rule():
    with SessionLocal() as db:
        info = {i.code: i for i in runtime.list_agents(db)}["kb-collector"]
    assert not info.ready and "采集机区" in info.reason
    for zone, access in (("none", "read"), ("knowledge", "read"), ("project", "read"), ("staging", "write")):
        assert tools.violates_hard_rule("collector", zone, access) == "采集机区 Agent 不能使用平台工具"
    assert tools.violates_hard_rule("something", "none", "read") == "Agent 所在区未登记"


def test_collector_blocked_even_if_grant_table_is_edited():
    from sqlalchemy import delete

    from app.models import AgentToolGrant

    _, user = _member()
    with SessionLocal() as db:
        db.add(AgentToolGrant(agent_code="kb-collector", tool_code="kb.search", granted_by="tamper"))
        db.commit()
    try:
        with SessionLocal() as db, pytest.raises(tools.ToolDenied, match="采集机区"):
            tools.invoke(db, db.merge(user), "kb-collector", "kb.search", {"query": "值班"})
    finally:
        with SessionLocal() as db:
            db.execute(delete(AgentToolGrant).where(AgentToolGrant.granted_by == "tamper"))
            db.commit()
