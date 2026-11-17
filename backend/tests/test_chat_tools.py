"""Agent 自主调用工具（二期 A7）：对话改用 function calling，取代“提问前检索注入”。

- 只给 Agent 已授权的读取类工具（写入类 doc.generate、confirm.create、agent.handoff 不进对话）；
- 每次调用经 tools.invoke（授权、硬规则、项目成员检查、审计）；工具出错把错误说明交回模型，不中断对话；
- 工具结果过机密级检测，命中的不发给模型；
- 最多 LLM_TOOL_MAX_ROUNDS 轮、LLM_TOOL_MAX_CALLS 次，之后最后一轮不带工具定义、把工具结果整理成文字要求直接答复；
- 检索类工具的命中记为回复的引用（只有 doc_id、标题、章节、层级、引用格式）；
- LLM_TOOL_CALLING=false 时退回提问前检索注入（test_chat_kb.py）。
"""
import json

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Message
from app.services import gateway

from .conftest import create_user, logged_in_client
from .test_chat_kb import _ask, _done, _publish


def _sse(*chunks: dict) -> httpx.Response:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks] + ["data: [DONE]"]
    return httpx.Response(200, content="\n\n".join(lines).encode(), headers={"content-type": "text/event-stream"})


def _text(s: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": s}}]}


def _call(name: str, args: dict, idx: int = 0, cid: str = "call_1") -> list[dict]:
    """一次工具调用，参数拆成两段流式下发（同 OpenAI 格式）。"""
    raw = json.dumps(args, ensure_ascii=False)
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": idx, "id": cid, "type": "function", "function": {"name": name, "arguments": raw[:5]}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": idx, "function": {"arguments": raw[5:]}}]}}]}]


def _finish(reason: str) -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class _Model:
    """按顺序回放预设的每一轮回复；记下每次请求体。"""

    def __init__(self, rounds: list[list[dict]]):
        self.rounds, self.requests = rounds, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4.1-flash"}]})
        self.requests.append(json.loads(request.content))
        n = min(len(self.requests), len(self.rounds)) - 1
        return _sse(*self.rounds[n])


@pytest.fixture()
def tool_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_tool_calling", True)

    def use(rounds):
        m = _Model(rounds)
        gateway.set_transport(httpx.MockTransport(m.handler))
        return m
    yield use
    gateway.set_transport(None)


@pytest.fixture()
def client():
    c = logged_in_client(app, create_user(("consultant",), {"BANK-A": "member"}))
    yield c
    c.__exit__(None, None, None)


@pytest.fixture(scope="module")
def night():
    return _publish("工具调用测试-夜间值守做法", "## 夜间值守\n\n无夜班客户由托管方初筛，一级事件电话升级轮值负责人。")


def _events(r) -> list[tuple[str, dict]]:
    out = []
    for b in r.text.split("\n\n"):
        if b.startswith("event: "):
            head, data = b.split("\n", 1)
            out.append((head[7:], json.loads(data.split("data: ", 1)[1])))
    return out


def _tool_audits(after: int) -> list[dict]:
    with SessionLocal() as db:
        return [json.loads(a.detail) | {"result": a.result} for a in db.scalars(
            select(AuditLog).where(AuditLog.action == "tool.call", AuditLog.id > after).order_by(AuditLog.id))]


def _last_audit() -> int:
    with SessionLocal() as db:
        return db.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1)) or 0


def test_model_calls_kb_search_then_answers(tool_mode, client, night):
    m = tool_mode([[_text("我先查一下。"), *_call("kb_search", {"query": "夜间值守 托管方"}), _finish("tool_calls")],
                   [_text("按 ["), _text(f"{night}] 的做法安排托管方初筛。"), _finish("stop")]])
    before = _last_audit()
    conv, r = _ask(client, "consultant-lead", "客户没有夜班，夜间值守怎么安排？")
    assert r.status_code == 200
    first = m.requests[0]
    names = [t["function"]["name"] for t in first["tools"]]
    assert "kb_search" in names and "project_read" in names
    assert not {"doc_generate", "confirm_create", "agent_handoff"} & set(names)       # 写入类不进对话
    assert "托管方初筛" not in first["messages"][0]["content"]                         # 不再提问前注入
    second = m.requests[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["function"]["name"] == "kb_search"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_1"
    assert "托管方初筛" in second[-1]["content"]                                       # 工具结果交回模型
    ev = _events(r)
    assert ("tool", {"tool": "kb.search", "name": "知识检索"}) in ev
    done = _done(r)
    assert night in [x["doc_id"] for x in done["references"]] and done["tool_calls"] == 1
    assert all(set(x) == {"doc_id", "title", "heading", "layer", "citation"} for x in done["references"])
    calls = _tool_audits(before)
    assert [c["tool"] for c in calls] == ["project.read", "kb.search"] and calls[1]["result"] == "allowed"
    with SessionLocal() as db:
        msg = db.scalars(select(Message).where(Message.conversation_id == conv["id"], Message.role == "assistant")).one()
    assert msg.content == f"我先查一下。按 [{night}] 的做法安排托管方初筛。"
    assert night in [x["doc_id"] for x in msg.kb_refs]


def test_ungranted_and_write_tools_refused(tool_mode, client):
    m = tool_mode([[*_call("basis_lookup", {"query": "集中管控"}), *_call("doc_generate", {"code": "D4"}, 1, "c2"),
                    _finish("tool_calls")],
                   [_text("好的。"), _finish("stop")]])
    before = _last_audit()
    _, r = _ask(client, "consultant-lead", "夜间值守依据？")                  # 咨询师 Agent 没有依据查询授权
    assert r.status_code == 200
    results = [x for x in m.requests[1]["messages"] if x["role"] == "tool"]
    assert len(results) == 2 and all("error" in json.loads(x["content"]) for x in results)
    calls = _tool_audits(before)
    assert ("basis.lookup", "blocked") in [(c["tool"], c["result"]) for c in calls]
    assert "doc.generate" not in [c["tool"] for c in calls]                     # 对话里根本不调写入类


def test_bad_arguments_and_unknown_function(tool_mode, client):
    m = tool_mode([[{"choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": 0, "id": "x1", "type": "function",
                         "function": {"name": "kb_search", "arguments": "{not json"}}]}}]},
                    *_call("rm_rf", {}, 1, "x2"), _finish("tool_calls")],
                   [_text("答复。"), _finish("stop")]])
    _, r = _ask(client, "consultant-lead", "夜间值守？")
    assert r.status_code == 200 and _done(r)["tool_calls"] == 2
    results = [json.loads(x["content"]) for x in m.requests[1]["messages"] if x["role"] == "tool"]
    assert all("error" in x for x in results)


def test_rounds_capped_then_tool_choice_none(tool_mode, client, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_tool_max_rounds", 2)
    m = tool_mode([[*_call("kb_search", {"query": "夜间值守"}), _finish("tool_calls")],
                   [*_call("kb_search", {"query": "夜间值守 再查"}), _finish("tool_calls")],
                   [_text("最终答复。"), _finish("stop")]])
    _, r = _ask(client, "consultant-lead", "夜间值守？")
    assert r.status_code == 200 and len(m.requests) == 3
    assert m.requests[0]["tool_choice"] == "auto" and "tools" not in m.requests[2] and "tool_choice" not in m.requests[2]
    final = m.requests[2]["messages"]
    assert not [x for x in final if x["role"] == "tool" or x.get("tool_calls")]   # 不带工具消息，免得模型接着调
    assert final[-1]["role"] == "user" and "知识检索" in final[-1]["content"]
    assert _done(r)["prompt_tokens"] == 30                                      # 三轮用量相加


def test_blocked_tool_result_not_sent(tool_mode, client, monkeypatch):
    from app.agents import tools

    real = tools.IMPLEMENTATIONS["kb.search"]
    monkeypatch.setitem(tools.IMPLEMENTATIONS, "kb.search",
                        lambda ctx, args: {**real(ctx, args), "leak": "管理口令 Abc@12345 核心交换机 10.1.20.1"})
    m = tool_mode([[*_call("kb_search", {"query": "夜间值守"}), _finish("tool_calls")],
                   [_text("答复。"), _finish("stop")]])
    _, r = _ask(client, "consultant-lead", "夜间值守？")
    assert r.status_code == 200
    content = next(x["content"] for x in m.requests[1]["messages"] if x["role"] == "tool")
    assert "Abc@12345" not in content and "机密级" in json.loads(content)["error"]


def test_switch_off_uses_injection(tool_mode, client, monkeypatch, night):
    monkeypatch.setattr(get_settings(), "llm_tool_calling", False)
    m = tool_mode([[_text("答复。"), _finish("stop")]])
    _ask(client, "consultant-lead", "客户没有夜班，夜间值守怎么安排？")
    assert "tools" not in m.requests[0] and "托管方初筛" in m.requests[0]["messages"][0]["content"]


def test_pseudonym_in_tool_args_and_results(tool_mode, monkeypatch):
    """假名化开启时：交回模型的工具结果换成占位符，模型给的参数里的占位符换回真名再执行。"""
    monkeypatch.setattr(get_settings(), "llm_pseudonymize", True)
    m = tool_mode([[*_call("kb_search", {"query": "【客户1】 夜间值守"}), _finish("tool_calls")],
                   [_text("答复。"), _finish("stop")]])
    seen = []

    def run(name, raw):
        seen.append(raw)
        return {"items": [{"title": "示范银行股份 的值守做法"}]}
    out = list(gateway.stream("示范银行股份 夜间值守？", tier="standard", project_code="BANK-A",
                              terms={"示范银行股份": "客户"}, tools=[{"type": "function"}], run_tool=run))
    assert "示范银行股份" in seen[0]
    tool_msg = next(x for x in m.requests[1]["messages"] if x["role"] == "tool")
    assert "示范银行股份" not in tool_msg["content"] and "【客户1】" in tool_msg["content"]
    assert isinstance(out[0], gateway.ToolUse) and out[-1].tool_calls == 1


DSML = ('<｜DSML｜function_calls>\n<｜DSML｜invoke name="kb_search">\n'
        '<｜DSML｜parameter name="query" string="true">夜间值守</｜DSML｜parameter>\n'
        '<｜DSML｜parameter name="limit" string="false">3</｜DSML｜parameter>\n'
        '</｜DSML｜invoke>\n</｜DSML｜function_calls>')


def _deltas(r) -> str:
    return "".join(d["text"] for e, d in _events(r) if e == "delta")


def test_dsml_markup_in_content_becomes_tool_call(tool_mode, client):
    """外部 DeepSeek 多轮后把工具调用写成 DSML 标记放进正文：不外发，还原成工具调用照常执行（#179 测试）。"""
    pieces = [DSML[i:i + 7] for i in range(0, len(DSML), 7)]
    m = tool_mode([[_text("我查一下。"), *[_text(p) for p in pieces], _finish("stop")],
                   [_text("答复。"), _finish("stop")]])
    conv, r = _ask(client, "consultant-lead", "夜间值守？")
    assert "DSML" not in r.text and _deltas(r) == "我查一下。答复。"
    assert ("tool", {"tool": "kb.search", "name": "知识检索"}) in _events(r) and _done(r)["tool_calls"] == 1
    call = m.requests[1]["messages"][-2]["tool_calls"][0]["function"]
    assert call["name"] == "kb_search" and json.loads(call["arguments"]) == {"query": "夜间值守", "limit": 3}
    assert "DSML" not in m.requests[1]["messages"][-2]["content"]
    with SessionLocal() as db:
        msg = db.scalars(select(Message).where(Message.conversation_id == conv["id"], Message.role == "assistant")).one()
    assert msg.content == "我查一下。答复。"


def test_broken_dsml_dropped_and_model_asked_again(tool_mode, client):
    """残缺的标记解析不出调用：丢掉，提醒模型直接答复（不再给工具）。"""
    m = tool_mode([[_text('<｜｜DSML｜｜ query"夜间"｜｜DSML｜｜>'), _finish("stop")],
                   [_text("正常答复。"), _finish("stop")]])
    _, r = _ask(client, "consultant-lead", "夜间值守？")
    assert "DSML" not in r.text and _deltas(r) == "正常答复。"
    assert len(m.requests) == 2 and "tools" not in m.requests[1]
    assert "DSML" not in json.dumps(m.requests[1]["messages"], ensure_ascii=False)


def test_less_than_sign_in_text_kept(tool_mode, client):
    tool_mode([[_text("响应时间 <"), _text("15 分钟，a<b。"), _finish("stop")]])
    _, r = _ask(client, "consultant-lead", "夜间值守？")
    assert _deltas(r) == "响应时间 <15 分钟，a<b。"


def test_calls_capped_per_question(tool_mode, client, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_tool_max_calls", 2)
    m = tool_mode([[*_call("kb_search", {"query": "a"}), *_call("kb_search", {"query": "b"}, 1, "c2"),
                    *_call("kb_search", {"query": "c"}, 2, "c3"), _finish("tool_calls")],
                   [_text("答复。"), _finish("stop")]])
    before = _last_audit()
    _, r = _ask(client, "consultant-lead", "夜间值守？")
    final = m.requests[1]["messages"][-1]["content"]
    assert final.count("【知识检索】") == 3 and "上限" in final           # 第三次没执行，只回上限说明
    assert [c["tool"] for c in _tool_audits(before)].count("kb.search") == 2
    assert "tools" not in m.requests[1]


def test_tool_model_override(tool_mode, client, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_model_tools", "deepseek-v4-pro")
    m = tool_mode([[_text("答复。"), _finish("stop")]])
    _ask(client, "consultant-lead", "夜间值守？")
    assert m.requests[0]["model"] == "deepseek-v4-pro"


def test_final_round_only_markup_gives_notice(tool_mode, client, monkeypatch):
    """用满次数后最后一轮仍只输出调用标记（外部 deepseek-flash，#179 复测 5 次）：不留空回复，给提示。"""
    monkeypatch.setattr(get_settings(), "llm_tool_max_calls", 1)
    m = tool_mode([[*_call("kb_search", {"query": "a"}), _finish("tool_calls")],
                   [_text(DSML), _finish("stop")]])
    _, r = _ask(client, "consultant-lead", "夜间值守？")
    assert len(m.requests) == 2 and "tools" not in m.requests[1]
    text = _deltas(r)
    assert "DSML" not in text and "没有给出答复" in text
