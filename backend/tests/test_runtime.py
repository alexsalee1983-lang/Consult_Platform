"""Agent 运行时：md 加载与版本、热加载、就绪判断、系统提示组装、项目卡、对话存储与隔离、多轮历史。"""
import json
import shutil
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.agents import registry, runtime
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Conversation, LlmUsage, Message, User
from app.services import gateway

from .conftest import create_user, logged_in_client

REPO_AGENTS = Path(__file__).resolve().parents[2] / "agents"


# ---- md 注册表 ----

@pytest.fixture()
def agents_copy(tmp_path, monkeypatch):
    """复制一份 agents/ 到临时目录，测试里可以随意改。"""
    d = tmp_path / "agents"
    shutil.copytree(REPO_AGENTS, d)
    monkeypatch.setattr(get_settings(), "agents_dir", str(d))
    registry.clear_cache()
    yield d
    registry.clear_cache()


def test_loads_repo_md():
    doc = registry.load("solution-designer")
    assert doc is not None and doc.display_name == "方案设计 Agent" and doc.level == "reasoning"
    assert not doc.body.startswith("---") and "# 角色" in doc.body
    assert "公共规范" in doc.common and len(doc.version) == 12
    assert "SOC 运营体系设计" in doc.service_modules


def test_version_changes_and_hot_reload(agents_copy):
    v1 = registry.load("consultant-lead").version
    md = agents_copy / "roles" / "consultant-lead.md"
    time.sleep(0.01)
    md.write_text(md.read_text(encoding="utf-8") + "\n补充一句。\n", encoding="utf-8")
    v2 = registry.load("consultant-lead").version
    assert v1 != v2
    common = agents_copy / registry.COMMON_FILE
    common.write_text(common.read_text(encoding="utf-8") + "\n公共规范补充。\n", encoding="utf-8")
    assert registry.load("consultant-lead").version not in {v1, v2}


def test_missing_or_mismatched_md_is_not_ready(agents_copy):
    (agents_copy / "roles" / "report-writer.md").unlink()
    assert registry.load("report-writer") is None
    md = agents_copy / "roles" / "policy-drafter.md"
    md.write_text(md.read_text(encoding="utf-8").replace("name: policy-drafter", "name: something-else"),
                  encoding="utf-8")
    assert registry.load("policy-drafter") is None
    with SessionLocal() as db:
        infos = {i.code: i for i in runtime.list_agents(db)}
    assert not infos["report-writer"].ready and "md" in infos["report-writer"].reason
    assert not infos["policy-drafter"].ready


def test_readiness_rules():
    with SessionLocal() as db:
        infos = {i.code: i for i in runtime.list_agents(db)}
    assert infos["consultant-lead"].ready and infos["solution-designer"].ready
    assert not infos["kb-builder"].ready and "知识侧" in infos["kb-builder"].reason   # 知识侧不能在项目里对话
    assert not infos["coach"].ready and "预留" in infos["coach"].reason


def test_md_tools_mismatch_is_reported_not_enforced():
    with SessionLocal() as db:
        infos = {i.code: i for i in runtime.list_agents(db)}
    assert infos["solution-designer"].notes == []               # md 与授权表一致
    assert any("RAG 检索" in n for n in infos["consultant-lead"].notes)  # 旧 md 写法，只提示


# ---- 系统提示与项目卡 ----

def _user(roles=("consultant",), projects=None):
    name, *_ = create_user(roles, projects or {"BANK-A": "member", "GRID-B": "member"})
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


def test_system_prompt_has_common_role_and_card():
    user = _user()
    with SessionLocal() as db:
        doc = runtime.ready_doc(db, "solution-designer")
        card = runtime.project_card(db, db.merge(user), "solution-designer", "BANK-A")
    prompt = runtime.system_prompt(doc, card)
    assert prompt.index("公共规范") < prompt.index("# 角色") < prompt.index("# 项目卡（平台生成）")
    assert "服务产品*：SOC 运营体系设计" in prompt and "数据档位*：标准档" in prompt
    assert "name: solution-designer" not in prompt  # frontmatter 不进提示


def test_strict_project_card_has_no_deliverables():
    user = _user()
    with SessionLocal() as db:
        card = runtime.project_card(db, db.merge(user), "consultant-lead", "GRID-B")
    assert "严格档" in card and "交付件进度" not in card and "摘要卡" in card


# ---- 对话接口（模型用 MockTransport） ----

class Model:
    def __init__(self):
        self.requests: list[dict] = []
        self.reply = "建议先做告警降噪。"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4.1-flash"}]})
        body = json.loads(request.content)
        self.requests.append(body)
        lines = [f"data: {json.dumps({'choices': [{'delta': {'content': self.reply}}]}, ensure_ascii=False)}",
                 "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 12}}),
                 "data: [DONE]"]
        return httpx.Response(200, content="\n\n".join(lines).encode(), headers={"content-type": "text/event-stream"})


@pytest.fixture()
def model(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    m = Model()
    gateway.set_transport(httpx.MockTransport(m.handler))
    yield m
    gateway.set_transport(None)


@pytest.fixture()
def client():
    c = logged_in_client(app, create_user(("consultant",), {"BANK-A": "member", "GRID-B": "member"}))
    yield c
    c.__exit__(None, None, None)


def _events(text):
    return [(b.split("\n")[0][7:], json.loads(b.split("\n")[1][6:])) for b in text.strip().split("\n\n")]


def test_conversation_flow(model, client):
    r = client.post("/api/projects/BANK-A/conversations", json={"agent_code": "solution-designer"})
    assert r.status_code == 201
    conv = r.json()
    assert conv["agent_name"] == "方案设计" and len(conv["prompt_version"]) == 12

    r1 = client.post(f"/api/conversations/{conv['id']}/messages", json={"message": "开始做 SOC 设计方案"})
    assert r1.status_code == 200
    events = _events(r1.text)
    assert events[-1][0] == "done" and events[-1][1]["prompt_version"] == conv["prompt_version"]
    sent = model.requests[-1]
    assert sent["model"] == get_settings().llm_model_reasoning           # md 的 model_tier: 推理
    assert "# 项目卡（平台生成）" in sent["messages"][0]["content"]
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]

    client.post(f"/api/conversations/{conv['id']}/messages", json={"message": "人力按不扩编考虑"})
    roles = [m["role"] for m in model.requests[-1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]              # 第二轮带上了历史

    detail = client.get(f"/api/conversations/{conv['id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    assert detail["title"] == "开始做 SOC 设计方案"
    assert all(m["prompt_version"] == conv["prompt_version"] for m in detail["messages"])
    listed = client.get("/api/projects/BANK-A/conversations").json()
    assert listed[0]["id"] == conv["id"] and listed[0]["message_count"] == 4

    with SessionLocal() as db:
        u = db.scalars(select(LlmUsage).order_by(LlmUsage.id.desc())).first()
        a = db.scalars(select(AuditLog).where(AuditLog.action == "llm.chat").order_by(AuditLog.id.desc())).first()
    assert u.agent_code == "solution-designer" and u.completion_tokens == 12
    detail_json = json.loads(a.detail)
    assert detail_json["agent"] == "solution-designer" and detail_json["conversation_id"] == conv["id"]
    assert "不扩编" not in a.detail                                         # 审计不记原文


def test_blocked_message_is_not_stored(model, client):
    conv = client.post("/api/projects/BANK-A/conversations", json={"agent_code": "consultant-lead"}).json()
    r = client.post(f"/api/conversations/{conv['id']}/messages", json={"message": "管理口令：Abc@12345 主机 10.1.1.1"})
    assert r.status_code == 422
    assert client.get(f"/api/conversations/{conv['id']}").json()["messages"] == []
    assert model.requests == []


def test_strict_project_conversation_appends_strict_rules(model, client):
    conv = client.post("/api/projects/GRID-B/conversations", json={"agent_code": "consultant-lead"}).json()
    client.post(f"/api/conversations/{conv['id']}/messages", json={"message": "梳理访谈提纲"})
    system = model.requests[-1]["messages"][0]["content"]
    assert "严格档" in system and "占位符" in system


def test_agents_not_ready_cannot_start_conversation(client):
    assert client.post("/api/projects/BANK-A/conversations", json={"agent_code": "kb-builder"}).status_code == 409
    assert client.post("/api/projects/BANK-A/conversations", json={"agent_code": "coach"}).status_code == 409
    assert client.post("/api/projects/BANK-A/conversations", json={"agent_code": "nope"}).status_code == 409


def test_conversations_are_private_and_follow_membership(model, client):
    conv = client.post("/api/projects/BANK-A/conversations", json={"agent_code": "consultant-lead"}).json()
    other = logged_in_client(app, create_user(("consultant",), {"BANK-A": "member"}))
    try:
        assert other.get(f"/api/conversations/{conv['id']}").status_code == 404        # 同项目的其他成员也看不到
        assert other.post(f"/api/conversations/{conv['id']}/messages",
                          json={"message": "x"}).status_code == 404
        assert other.get("/api/projects/BANK-A/conversations").json() == []
    finally:
        other.__exit__(None, None, None)
    # 发起人被移出项目后也看不到
    me = client.get("/api/auth/me").json()["username"]
    with SessionLocal() as db:
        from app.models import Project, ProjectMember
        uid = db.scalar(select(User.id).where(User.username == me))
        pid = db.scalar(select(Project.id).where(Project.code == "BANK-A"))
        db.delete(db.get(ProjectMember, (pid, uid)))
        db.commit()
    assert client.get(f"/api/conversations/{conv['id']}").status_code == 404


def test_history_budget_and_interrupted_messages(monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_history_chars", 9)   # 最近两条 5 字，再加最早一条 5 字就超了
    msgs = [Message(role="user", content="一二三四五", status="ok"),
            Message(role="assistant", content="不完整", status="interrupted"),
            Message(role="user", content="六七八", status="ok"),
            Message(role="assistant", content="九十", status="ok")]
    h = runtime.history(msgs)
    assert [m["content"] for m in h] == ["六七八", "九十"]   # 超预算的早期消息和中断的回复都不带


def test_conversation_records_prompt_version_at_creation(model, client, agents_copy):
    conv = client.post("/api/projects/BANK-A/conversations", json={"agent_code": "consultant-lead"}).json()
    md = agents_copy / "roles" / "consultant-lead.md"
    md.write_text(md.read_text(encoding="utf-8") + "\n更新。\n", encoding="utf-8")
    client.post(f"/api/conversations/{conv['id']}/messages", json={"message": "继续"})
    with SessionLocal() as db:
        c = db.get(Conversation, conv["id"])
        msg_versions = {m.prompt_version for m in c.messages}
    assert len(msg_versions) == 1 and msg_versions != {c.prompt_version}   # 消息记的是发送时的新版本
