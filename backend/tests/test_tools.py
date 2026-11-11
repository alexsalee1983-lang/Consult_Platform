"""Agent × 工具授权。对应 M1 验收“知识侧 Agent 读不到项目区”。

EXPECTED 是《架构设计》第五节授权表的独立抄写（不从迁移导入），迁移写错时这里会失败。
调整授权时：新迁移 + 改这里 + PR 里写明依据。
"""
import json

import pytest
from sqlalchemy import delete, select

from app.agents import tools
from app.core.db import SessionLocal
from app.main import app
from app.models import Agent, AgentToolGrant, AuditLog, Tool, User

from .conftest import create_user, logged_in_client

ALL_TOOLS = ["kb.search", "basis.lookup", "project.read", "doc.generate", "quality.check", "kb.stage_write",
             "project.evidence"]
#                      知识检索 依据查询 读项目区 文档生成 质量检查 写待入库区 查证据（M3，迁移 0017，与读项目区同列）
EXPECTED = {
    "consultant-lead":    (1, 0, 1, 0, 0, 0, 1),
    "delivery-pm":        (1, 0, 1, 1, 0, 0, 1),
    "research-interview": (1, 0, 1, 1, 0, 0, 1),
    "compliance-basis":   (1, 1, 1, 0, 0, 0, 1),
    "solution-designer":  (1, 1, 1, 0, 0, 0, 1),
    "policy-drafter":     (1, 1, 1, 1, 0, 0, 1),
    "report-writer":      (1, 0, 1, 1, 0, 0, 1),
    "qa-reviewer":        (1, 1, 1, 0, 1, 0, 1),
    "kb-builder":         (1, 1, 0, 0, 0, 1, 0),
    "sanitizer":          (0, 0, 0, 0, 0, 1, 0),
}
KNOWLEDGE_SIDE = {"kb-builder", "sanitizer"}


@pytest.fixture(scope="module")
def member():
    """顾问，BANK-A（标准档）和 GRID-B（严格档）的成员。"""
    name, *_ = create_user(("consultant",), {"BANK-A": "member", "GRID-B": "member"})
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


def _invoke(user, agent, tool, project="BANK-A", args=None):
    with SessionLocal() as db:
        return tools.invoke(db, db.merge(user), agent, tool, args or {"text": "问题", "query": "问题"}, project)


@pytest.mark.parametrize("agent", list(EXPECTED))
def test_grant_matrix(member, agent):
    for tool, allowed in zip(ALL_TOOLS, EXPECTED[agent]):
        if allowed:
            assert isinstance(_invoke(member, agent, tool), dict), f"{agent} 应能用 {tool}"
        else:
            with pytest.raises(tools.ToolDenied):
                _invoke(member, agent, tool)


def test_registered_grants_equal_expected():
    with SessionLocal() as db:
        got = {(g.agent_code, g.tool_code) for g in db.scalars(select(AgentToolGrant))}
    want = {(a, t) for a, flags in EXPECTED.items() for t, f in zip(ALL_TOOLS, flags) if f}
    assert got == want


@pytest.mark.parametrize("agent", sorted(KNOWLEDGE_SIDE))
def test_knowledge_agents_cannot_read_project_area(member, agent):
    for tool in ("project.read", "project.evidence"):
        with pytest.raises(tools.ToolDenied):
            _invoke(member, agent, tool)


def test_hard_rule_holds_even_if_grant_table_is_edited(member):
    """有人直接往授权表里加了“知识库建设 → 读项目区”“咨询师 → 写待入库区”，调用仍被拒绝。"""
    with SessionLocal() as db:
        db.add_all([AgentToolGrant(agent_code="kb-builder", tool_code="project.read", granted_by="tamper"),
                    AgentToolGrant(agent_code="consultant-lead", tool_code="kb.stage_write", granted_by="tamper")])
        db.commit()
    try:
        with pytest.raises(tools.ToolDenied, match="项目区"):
            _invoke(member, "kb-builder", "project.read")
        with pytest.raises(tools.ToolDenied, match="知识区"):
            _invoke(member, "consultant-lead", "kb.stage_write")
    finally:
        with SessionLocal() as db:
            db.execute(delete(AgentToolGrant).where(AgentToolGrant.granted_by == "tamper"))
            db.commit()


def test_seeded_registry_respects_hard_rules():
    with SessionLocal() as db:
        for g in db.scalars(select(AgentToolGrant)):
            a, t = db.get(Agent, g.agent_code), db.get(Tool, g.tool_code)
            assert tools.violates_hard_rule(a.side, t.zone, t.access) is None, (a.code, t.code)


def test_reserved_agents_are_disabled(member):
    for agent in ("coach", "presales"):
        with pytest.raises(tools.ToolNotFound):
            _invoke(member, agent, "kb.search")


def test_unknown_agent_and_tool(member):
    with pytest.raises(tools.ToolNotFound):
        _invoke(member, "nobody", "kb.search")
    with pytest.raises(tools.ToolNotFound):
        _invoke(member, "consultant-lead", "db.drop")


def test_project_tools_follow_project_membership(member):
    with pytest.raises(tools.ToolNotFound):
        _invoke(member, "consultant-lead", "project.read", project="MFG-C")   # 不是成员
    with pytest.raises(tools.ToolNotFound):
        _invoke(member, "consultant-lead", "project.read", project="NOPE")
    with pytest.raises(tools.ToolError):
        _invoke(member, "consultant-lead", "project.read", project=None)


def test_project_read_standard_vs_strict(member):
    std = _invoke(member, "consultant-lead", "project.read", project="BANK-A")
    assert std["project"]["code"] == "BANK-A" and len(std["deliverables"]) == 8
    strict = _invoke(member, "consultant-lead", "project.read", project="GRID-B")
    assert strict["project"]["tier"] == "strict" and "deliverables" not in strict and "摘要卡" in strict["note"]


def test_quality_check_tool(member):
    r = _invoke(member, "qa-reviewer", "quality.check", args={"text": "交付件里写了管理口令：Abc@12345"})
    assert r["blocked"] and any(f["kind"] == "credential" for f in r["findings"])


def test_placeholders_say_so(member):
    r = _invoke(member, "kb-builder", "kb.stage_write", project=None)
    assert r["placeholder"] is True and r["staged"] is False
    r = _invoke(member, "report-writer", "doc.generate")                    # 已是真实实现：不带参数返回交付件清单
    assert "placeholder" not in r and [d["deliverable"] for d in r["deliverables"]][:2] == ["D1", "D2"]


def test_kb_search_tool_is_real(member):
    r = _invoke(member, "consultant-lead", "kb.search", args={"query": "值班交接"})
    assert "placeholder" not in r and isinstance(r["items"], list)
    with pytest.raises(tools.ToolError):
        _invoke(member, "consultant-lead", "kb.search", args={"query": ""})          # 空检索词
    with pytest.raises(tools.ToolError):
        _invoke(member, "consultant-lead", "kb.search", args={"query": "口令 Abc@12345"})   # 机密级


def test_tool_calls_are_audited_without_content(member):
    secret = "绝不进审计的内容"
    with pytest.raises(tools.ToolDenied):
        _invoke(member, "kb-builder", "project.read", args={"text": secret})
    _invoke(member, "qa-reviewer", "quality.check", args={"text": secret})
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "tool.call").order_by(AuditLog.id.desc())
                          .limit(2)).all()
    allowed, blocked = rows
    assert blocked.result == "blocked" and json.loads(blocked.detail)["reason"] == "not_granted"
    assert allowed.result == "allowed" and json.loads(allowed.detail)["tool"] == "quality.check"
    assert all(secret not in r.detail for r in rows)


# ---- 接口 ----

def test_api_lists_agents_and_calls_tools():
    c = logged_in_client(app, create_user(("consultant",), {"BANK-A": "member"}))
    try:
        agents = {a["code"]: a for a in c.get("/api/agents").json()}
        assert set(agents) == set(EXPECTED)
        assert agents["kb-builder"]["side"] == "knowledge" and "project.read" not in agents["kb-builder"]["tools"]
        ok = c.post("/api/agents/consultant-lead/tools/project.read", json={"project_code": "BANK-A"})
        assert ok.status_code == 200 and ok.json()["project"]["code"] == "BANK-A"
        denied = c.post("/api/agents/kb-builder/tools/project.read", json={"project_code": "BANK-A"})
        assert denied.status_code == 403
        assert c.post("/api/agents/consultant-lead/tools/project.read",
                      json={"project_code": "GRID-B"}).status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_admin_sees_grant_matrix_read_only():
    c = logged_in_client(app, create_user(("sys_admin",)))
    try:
        rows = {r["agent"]: r for r in c.get("/api/admin/agent-grants").json()}
        assert rows["sanitizer"]["tools"] == ["kb.stage_write"] and rows["coach"]["enabled"] is False
        assert c.post("/api/agents/consultant-lead/tools/kb.search", json={}).status_code == 403
    finally:
        c.__exit__(None, None, None)
