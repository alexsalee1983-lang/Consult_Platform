"""对话转交（二期 C2）：咨询师 Agent 回复里的【交接】行 → 顾问点转交 → 平台为专业 Agent 新建对话。
只有咨询师 Agent 有 agent.handoff 授权；只有对话发起人能转交；交接说明过机密级检测；审计不记说明内容。"""
from contextlib import contextmanager

from sqlalchemy import select

from app.agents import handoff
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Conversation, Message, Project, User

from .conftest import create_user, logged_in_client


@contextmanager
def _client(user):
    c = logged_in_client(app, user)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)

BRIEF = "【交接】目标 Agent：调研访谈 Agent ｜ 项目卡：（附） ｜ 任务：夜间值守访谈提纲 ｜ 已有材料：D-01 ｜ 期望输出：提纲"
REPLY = f"【项目：BANK-A｜SOC｜调研】【本次任务：访谈提纲｜处理方式：转交 调研访谈 Agent】\n\n说明……\n\n{BRIEF}\n"


def _conversation(username: str, agent: str, reply: str) -> tuple[int, int]:
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == username))
        p = db.scalar(select(Project).where(Project.code == "BANK-A"))
        c = Conversation(project_id=p.id, user_id=u.id, agent_code=agent, title="t", prompt_version="t")
        db.add(c)
        db.flush()
        db.add(Message(conversation_id=c.id, role="user", content="帮我出访谈提纲", prompt_version="t"))
        m = Message(conversation_id=c.id, role="assistant", content=reply, prompt_version="t")
        db.add(m)
        db.commit()
        return c.id, m.id


def test_parse_names_codes_and_skips():
    text = "\n".join([BRIEF, "【交接】目标 Agent：调研访谈 ｜ 任务：重复的同一目标",
                      "`【交接】目标 Agent：report-writer ｜ 任务：写报告`", "【交接】目标 Agent：咨询师 ｜ 任务：给自己",
                      "【交接】目标 Agent：质量评审与异议应答 Agent ｜ 任务：评审", "【交接】目标 Agent：不存在的 Agent ｜ 任务：x", "普通一行 目标 Agent：方案设计"])
    with SessionLocal() as db:
        got = handoff.parse(db, text, "consultant-lead")
    assert [h.agent_code for h in got] == ["research-interview", "report-writer", "qa-reviewer"]
    assert got[0].brief == BRIEF


def test_handoff_flow_creates_conversation_for_specialist():
    name_u = create_user(("consultant",), {"BANK-A": "member"})
    name = name_u[0]
    cid, mid = _conversation(name, "consultant-lead", REPLY)
    with _client(name_u) as c:
        detail = c.get(f"/api/conversations/{cid}").json()
        assert detail["messages"][1]["handoffs"] == [{"agent_code": "research-interview", "agent_name": "调研访谈"}]
        assert detail["messages"][0]["handoffs"] == []

        r = c.post(f"/api/conversations/{cid}/handoff", json={"message_id": mid, "agent_code": "research-interview"})
        assert r.status_code == 201, r.text
        out = r.json()
        new = out["conversation"]
        assert new["agent_code"] == "research-interview" and new["handoff_from"] == cid
        assert new["title"] == "转交：夜间值守访谈提纲"
        assert BRIEF in out["draft"] and "咨询师" in out["draft"]
        assert c.get(f"/api/conversations/{new['id']}").json()["messages"] == []      # 说明不自动发给模型

        assert c.post(f"/api/conversations/{cid}/handoff",
                      json={"message_id": mid, "agent_code": "report-writer"}).status_code == 422
        assert c.post(f"/api/conversations/{cid}/handoff",
                      json={"message_id": mid - 1, "agent_code": "research-interview"}).status_code == 404

    with SessionLocal() as db:
        logs = list(db.scalars(select(AuditLog).where(AuditLog.actor == name)))
    create_log = next(a for a in logs if a.action == "conversation.create")
    assert f'"handoff_from": {cid}' in create_log.detail
    assert all("夜间值守" not in a.detail for a in logs)


def test_only_owner_and_only_consultant_lead():
    owner_u = create_user(("consultant",), {"BANK-A": "member"})
    owner = owner_u[0]
    other_u = create_user(("consultant",), {"BANK-A": "member"})
    cid, mid = _conversation(owner, "consultant-lead", REPLY)
    with _client(other_u) as c:          # 同项目的其他成员也看不到别人的对话
        assert c.post(f"/api/conversations/{cid}/handoff",
                      json={"message_id": mid, "agent_code": "research-interview"}).status_code == 404

    rid, rmid = _conversation(owner, "solution-designer", REPLY.replace("调研访谈 Agent ｜", "policy-drafter ｜"))
    with _client(owner_u) as c:          # 专业 Agent 没有转交授权：不显示按钮，调用 403
        assert c.get(f"/api/conversations/{rid}").json()["messages"][1]["handoffs"] == []
        assert c.post(f"/api/conversations/{rid}/handoff",
                      json={"message_id": rmid, "agent_code": "policy-drafter"}).status_code == 403


def test_brief_with_secret_is_blocked():
    name_u = create_user(("consultant",), {"BANK-A": "member"})
    name = name_u[0]
    cid, mid = _conversation(name, "consultant-lead", BRIEF + " ｜ 口令：Abc@12345678x")
    with _client(name_u) as c:
        r = c.post(f"/api/conversations/{cid}/handoff", json={"message_id": mid, "agent_code": "research-interview"})
    assert r.status_code == 422 and "机密级" in r.text
    with SessionLocal() as db:
        assert db.scalar(select(Conversation).where(Conversation.handoff_from == cid)) is None
