"""确认卡（M3）：Agent 经 confirm.create 发卡（选项、推荐、理由、影响、关联交付件），重复卡不刷屏，机密级拦截，
知识侧 Agent 不能发卡，顾问处理（选选项 / 写意见），已处理不能再处理，成员隔离，审计不记原文。"""
import secrets

import pytest
from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Confirmation, Deliverable, Project, User
from app.project import confirmations
from app.seed import DELIVERABLES

from .conftest import create_user, logged_in_client

CARD = {"kind": "方案取舍", "title": "一期人力模型选哪一种？",
        "options": ["A 6 人白班 + 托管夜间初筛", "B．扩编到 10 人，自建 7×24"],
        "recommendation": "a", "reason": "前提 1 本期不扩编；托管初筛可覆盖夜间一级事件",
        "impact": "选 A，R-3.2 生效；选 B，需重做人力测算", "deliverable": "d5"}


@pytest.fixture()
def proj():
    code = f"CNF-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行")
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    name = create_user(("consultant",), {code: "member"})
    yield code, name


def _send(name: str, code: str, args: dict, agent: str = "solution-designer") -> dict:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name[0]))
        return tools.invoke(db, user, agent, "confirm.create", args, code)


def test_agent_sends_card_and_duplicates_are_folded(proj):
    code, name = proj
    r = _send(name, code, CARD)
    assert r["created"] and r["status"] == "open" and r["recommendation"] == "A" and r["deliverable"] == "D5"
    assert r["options"] == [{"key": "A", "text": "6 人白班 + 托管夜间初筛"}, {"key": "B", "text": "扩编到 10 人，自建 7×24"}]
    again = _send(name, code, CARD)                                         # 同一 Agent 重发同一标题：不新建
    assert again["id"] == r["id"] and not again["created"] and "没有重复发" in again["note"]
    other = _send(name, code, CARD, agent="report-writer")                   # 别的 Agent 发同一标题：另起一张
    assert other["id"] != r["id"]
    with SessionLocal() as db:
        c = db.get(Confirmation, r["id"])
        assert c.agent == "方案设计 Agent" and c.agent_code == "solution-designer" and c.created_by == name[0]
        a = db.scalars(select(AuditLog).where(AuditLog.action == "confirmation.create")
                       .order_by(AuditLog.id.desc())).first()
    assert "人力模型" not in a.detail and "扩编" not in a.detail and '"kind": "方案取舍"' in a.detail


@pytest.mark.parametrize(("args", "msg"), [
    ({**CARD, "kind": "随便"}, "kind"),
    ({**CARD, "title": ""}, "标题"),
    ({**CARD, "recommendation": "C"}, "recommendation"),
    ({**CARD, "options": "A 或 B"}, "options"),
    ({**CARD, "options": [f"选项{i}" for i in range(7)]}, "最多"),
    ({**CARD, "deliverable": "D9"}, "交付件"),
    ({**CARD, "section": 3}, "第 3 章"),
    ({**CARD, "reason": "管理口令 Abc@12345"}, "没有保存"),
])
def test_bad_cards_rejected(proj, args, msg):
    code, name = proj
    with pytest.raises(tools.ToolError, match=msg):
        _send(name, code, args)


def test_tool_channel_keeps_status_and_detail(proj):
    """经接口调用工具时，业务错误的状态码和机密级拦截详情原样返回（测试 #35/#36：曾统一成 400、只剩一句话）。"""
    code, name = proj
    url = "/api/agents/solution-designer/tools/confirm.create"
    with logged_in_client(app, name) as c:
        bad = c.post(url, json={"project_code": code, "args": {**CARD, "recommendation": "C"}})
        assert bad.status_code == 422 and isinstance(bad.json()["detail"], str)
        blocked = c.post(url, json={"project_code": code, "args": {**CARD, "reason": "管理口令 Abc@12345"}})
        assert blocked.status_code == 422
        detail = blocked.json()["detail"]
        assert detail["blocked"] and detail["findings"] and "Abc@12345" not in blocked.text
        assert c.post(url, json={"project_code": code, "args": CARD}).status_code == 200
        assert c.post("/api/agents/kb-builder/tools/confirm.create",
                      json={"project_code": code, "args": CARD}).status_code == 403


def test_knowledge_agents_cannot_send_cards(proj):
    code, name = proj
    with pytest.raises(tools.ToolDenied):
        _send(name, code, CARD, agent="kb-builder")


def test_consultant_resolves_by_choice_or_comment(proj):
    code, name = proj
    card = _send(name, code, CARD)
    plain = _send(name, code, {"kind": "证据不足", "title": "缺少近 3 个月的告警统计"})
    with logged_in_client(app, name) as c:
        listed = c.get(f"/api/projects/{code}/confirmations", params={"status": "open"}).json()
        first = next(x for x in listed if x["id"] == card["id"])
        assert first["reason"].startswith("前提 1") and first["impact"] and first["deliverable_code"] == "D5"
        assert c.post(f"/api/confirmations/{card['id']}/resolve", json={"choice": "C"}).status_code == 422
        assert c.post(f"/api/confirmations/{plain['id']}/resolve", json={"choice": "A"}).status_code == 422
        assert c.post(f"/api/confirmations/{plain['id']}/resolve", json={}).status_code == 422

        r = c.post(f"/api/confirmations/{card['id']}/resolve", json={"choice": "a"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "resolved" and body["choice"] == "A" and body["resolution"].startswith("选 A：")
        assert body["resolved_by"] == name[0] and body["resolved_at"]
        assert c.post(f"/api/confirmations/{card['id']}/resolve", json={"choice": "B"}).status_code == 409

        blocked = c.post(f"/api/confirmations/{plain['id']}/resolve", json={"resolution": "口令 Abc@12345"})
        assert blocked.status_code == 422
        r = c.post(f"/api/confirmations/{plain['id']}/resolve", json={"resolution": "已请客户补近 3 个月统计，先按假设继续"})
        assert r.status_code == 200 and r.json()["choice"] == ""
        done = c.get(f"/api/projects/{code}/confirmations", params={"status": "resolved"}).json()
        assert {x["id"] for x in done} >= {card["id"], plain["id"]}
        assert c.get(f"/api/projects/{code}/confirmations", params={"status": "x"}).status_code == 422
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "confirmation.resolve")
                       .order_by(AuditLog.id.desc())).first()
    assert "先按假设继续" not in a.detail and '"with_comment": true' in a.detail
    # Agent 读项目卡时看到的待确认事项带编号，处理过的不再出现
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name[0]))
        open_cards = tools.invoke(db, user, "consultant-lead", "project.read", {}, code)["open_confirmations"]
    assert card["id"] not in [x["id"] for x in open_cards]


def test_non_member_cannot_see_or_resolve(proj):
    code, name = proj
    card = _send(name, code, CARD)
    other = create_user(("consultant",), {"MFG-C": "member"})
    with logged_in_client(app, other) as c:
        assert c.get(f"/api/projects/{code}/confirmations").status_code == 404
        assert c.post(f"/api/confirmations/{card['id']}/resolve", json={"choice": "A"}).status_code == 404


def test_normalize_options_keeps_dicts_and_strips_prefixes():
    assert confirmations.normalize_options([{"key": "Z", "text": "保持原样"}, "B） 第二个", "第三个"]) == [
        {"key": "A", "text": "保持原样"}, {"key": "B", "text": "第二个"}, {"key": "C", "text": "第三个"}]
    assert confirmations.normalize_options(None) == []
