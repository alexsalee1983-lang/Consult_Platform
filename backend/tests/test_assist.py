"""写稿助手 v1（三期 A15②）：交付件编辑页侧栏问 / 改。

- 问（ask）与改（edit）都带自动上下文：交付件、章节标题、编辑中的正文（未保存的也算）、选中的文字；
- 快慢分档：fast → light 档模型，deep → reasoning 档模型（顾问显式选择，优先于 md 的 model_tier）；
- 经 report-writer Agent（agents/call.py 的 stream，经网关），SSE 返回 delta / done / error；停止 = 前端断开连接；
- done 带回复里来源标注的核对结果（插入时原样保留，前端据此显示标签）；
- 机密级拦截 422、非成员 404、审计不记原文；
- 插入内容照样过签署门禁：助手给出的资质用语、核对不上的来源，插入保存后评审阻断、提交签署 409（三期计划第八节第 8 条）。
"""
import json
import secrets

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Deliverable, LlmUsage, Project
from app.project import quality
from app.seed import DELIVERABLES
from app.services import gateway, jobs

from .conftest import create_user, logged_in_client, sse

REVIEW_OK = ("# 评审意见\n- 交付物：D4 ｜ 评审结论：通过\n- 统计：阻断 0 项，重要 0 项，建议 0 项\n\n"
             "| # | 位置 | 维度 | 严重度 | 问题 | 修改建议 |\n|---|---|---|---|---|---|\n")
BODY = "SOC 现有 6 人，全部白班，交接记录为纸质 [O-01]。"
BAD_REPLY = "经评估，本系统已通过等保三级测评 [D-99]。"


class _Model:
    """流式请求返回 self.reply 分两段；非流式（质量评审）返回 REVIEW_OK。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.reply = "建议补充夜间值守安排 [O-01]，参照公司做法 [推断]。"

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        if not payload.get("stream"):
            return httpx.Response(200, json={"model": "mock-model", "choices": [{"message": {"content": REVIEW_OK}}],
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        half = len(self.reply) // 2
        lines = [f"data: {json.dumps({'choices': [{'delta': {'content': part}}]}, ensure_ascii=False)}"
                 for part in (self.reply[:half], self.reply[half:])] + ["data: [DONE]"]
        return httpx.Response(200, content="\n\n".join(lines).encode(), headers={"content-type": "text/event-stream"})

    @property
    def last(self) -> dict:
        return [r for r in self.requests if r.get("stream")][-1]


@pytest.fixture()
def model(monkeypatch):
    m = _Model()
    s = get_settings()
    monkeypatch.setattr(s, "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(s, "llm_max_retries", 0)
    monkeypatch.setattr(s, "llm_tool_calling", False)
    monkeypatch.setattr(s, "llm_model_light", "mock-light")
    monkeypatch.setattr(s, "llm_model_reasoning", "mock-reasoning")
    gateway.set_transport(httpx.MockTransport(m.handler))
    yield m
    gateway.set_transport(None)


@pytest.fixture()
def team():
    code = f"AS-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    member = logged_in_client(app, create_user(("consultant",), {code: "manager"}))
    outsider = logged_in_client(app, create_user(("consultant",), {}))
    r = member.post(f"/api/projects/{code}/notes",
                    json={"kind": "observation", "title": "值班室巡查", "content": "值班台交接记录为纸质，夜间无人值守。"})
    assert r.status_code == 201
    assert member.put(f"/api/projects/{code}/deliverables/D4/outline",
                      json={"titles": ["现状概述", "主要差距"]}).status_code == 200
    for i in (1, 2):
        assert member.put(f"/api/projects/{code}/deliverables/D4/sections/{i}", json={"body": BODY}).status_code == 201
    yield code, member, outsider
    for c in (member, outsider):
        c.__exit__(None, None, None)


def _url(code: str, tail: str) -> str:
    return f"/api/projects/{code}/deliverables/D4/{tail}"


def _assist(c, code: str, seq: int = 1, **body):
    return c.post(_url(code, f"sections/{seq}/assist"), json={"mode": "ask", "prompt": "夜间值守怎么写？", **body})


def test_ask_streams_with_auto_context_and_checked_sources(team, model):
    code, member, _ = team
    r = _assist(member, code, context="编辑中未保存的正文：夜班由外包值守 [O-01]。", selection="夜班由外包值守")
    assert r.status_code == 200, r.text
    events = sse(r)
    assert [e for e, _ in events if e == "delta"] and events[-1][0] == "done"
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert text == model.reply
    done = events[-1][1]
    assert done["mode"] == "ask" and done["speed"] == "fast" and done["model"]
    # 回复里的来源标注核对：O-01 是项目证据，推断计数，没有核对不上的
    assert done["sources"]["counts"] == {"evidence": 1, "knowledge": 0, "inferred": 1}
    assert done["sources"]["unresolved"] == []
    # 自动上下文：交付件、章节标题、编辑中的正文、选中文字和问题都在提示里；系统提示是报告 Agent
    sent = model.last["messages"]
    user_msg = sent[-1]["content"]
    for part in ("D4", "现状概述", "编辑中未保存的正文", "夜班由外包值守", "夜间值守怎么写？"):
        assert part in user_msg
    assert "报告" in sent[0]["content"] and code in sent[0]["content"]


def test_context_defaults_to_saved_body(team, model):
    code, member, _ = team
    assert _assist(member, code, seq=2).status_code == 200
    assert BODY in model.last["messages"][-1]["content"]


def test_speed_tiers_pick_model_and_edit_mode_prompt(team, model):
    code, member, _ = team
    assert _assist(member, code, speed="fast").status_code == 200
    assert model.last["model"] == "mock-light"
    r = _assist(member, code, mode="edit", speed="deep", prompt="改得更简洁")
    assert r.status_code == 200
    assert model.last["model"] == "mock-reasoning"
    assert sse(r)[-1][1]["mode"] == "edit" and sse(r)[-1][1]["speed"] == "deep"
    assert "只输出改写后的文本" in model.last["messages"][-1]["content"]
    with SessionLocal() as db:
        rows = db.scalars(select(LlmUsage).where(LlmUsage.project_code == code)
                          .order_by(LlmUsage.id.desc())).all()[:2]
        assert {(u.level, u.agent_code, u.stream, u.status) for u in rows} == {
            ("light", "report-writer", True, "ok"), ("reasoning", "report-writer", True, "ok")}


def test_bad_input_and_isolation(team, model):
    code, member, outsider = team
    assert _assist(member, code, mode="rewrite").status_code == 422
    assert _assist(member, code, prompt="").status_code == 422
    assert _assist(member, code, speed="turbo").status_code == 422
    assert _assist(member, code, seq=9).status_code == 404
    assert _assist(outsider, code).status_code == 404
    assert model.requests == []


def test_guard_blocks_and_audit_has_no_original_text(team, model):
    code, member, _ = team
    r = _assist(member, code, context="核心交换机管理口令 Abc@12345，独特正文甲乙丙")
    assert r.status_code == 422 and r.json()["detail"]["blocked"]
    assert model.requests == []
    assert _assist(member, code, prompt="独特问题丁戊己怎么写").status_code == 200
    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action == "deliverable.assist")).all()
        assert [x.result for x in logs] == ["blocked", "allowed"]
        d = json.loads(logs[-1].detail)
        assert d["deliverable"] == "D4" and d["section"] == 1 and d["mode"] == "ask" and d["reply_chars"] > 0
        dump = json.dumps([x.detail for x in logs], ensure_ascii=False)
        for secret in ("Abc@12345", "独特正文甲乙丙", "独特问题丁戊己", "夜间值守安排"):
            assert secret not in dump


def test_inserted_reply_still_blocked_at_signoff(team, model):
    """助手给出资质用语和核对不上的来源，插入保存后照样过质量门禁：评审阻断、提交签署 409。"""
    code, member, _ = team
    model.reply = BAD_REPLY
    done = sse(_assist(member, code, mode="edit", prompt="写一句结论"))[-1][1]
    assert done["sources"]["unresolved"] == ["D-99"]          # 侧栏提示核对不上，但不拦插入
    # 前端 insertWithSources 把回复原样插到光标处（来源标注保留），再走原保存接口
    body = BODY + "\n\n" + done["text"]
    assert done["text"] == BAD_REPLY
    r = member.put(_url(code, "sections/1"), json={"body": body, "note": "含写稿助手插入内容"})
    assert r.status_code == 201
    assert r.json()["sources"]["unresolved"] == ["D-99"]
    r = member.post(_url(code, "reviews"))
    assert r.status_code == 202
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=[quality.JOB_KIND]):
            pass
    rv = member.get(_url(code, f"reviews/{r.json()['id']}")).json()
    assert rv["conclusion"] == "fail"
    rule = {i["dimension"] for i in rv["issues"] if i["by"] == "rule"}
    assert rule >= {"范围与资质边界", "证据链 / 依据"}
    r = member.post(_url(code, "signoff/submit"), json={})
    assert r.status_code == 409 and any("阻断" in x for x in r.json()["detail"]["reasons"])
