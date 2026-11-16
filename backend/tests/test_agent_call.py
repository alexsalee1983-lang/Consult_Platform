"""统一的 Agent 调用层（决策记录 2026-11-16）：md 写了 model_tier 时以 md 为准、没写用调用方默认，
结构化输出格式不对时带纠错说明重试一次、仍不对报 OutputInvalid，用量每次调用记一条，停用的 Agent 不调用。"""
import json

import httpx
import pytest
from sqlalchemy import select

from app.agents import call
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import Agent, LlmUsage
from app.services import gateway


class _Model:
    def __init__(self, replies: list[str]):
        self.replies, self.requests = list(replies), []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(200, json={"model": "mock-model", "choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


@pytest.fixture()
def model(monkeypatch):
    def make(replies: list[str]) -> _Model:
        m = _Model(replies)
        gateway.set_transport(httpx.MockTransport(m.handler))
        return m
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
    yield make
    gateway.set_transport(None)


def _usage(actor: str) -> list[LlmUsage]:
    with SessionLocal() as db:
        return list(db.scalars(select(LlmUsage).where(LlmUsage.username == actor)))


def test_bad_format_is_retried_once_with_correction(model):
    m = model(["不是 JSON", '{"conclusion": "通过"}'])
    with SessionLocal() as db:
        r = call.call(db, "kb-reviewer", "审这一条", actor="call-retry", parse=call.json_object)
        db.commit()
    assert r.data == {"conclusion": "通过"} and r.version and r.completion.model == "mock-model"
    assert len(m.requests) == 2 and call.RETRY_NOTE in m.requests[1]["messages"][-1]["content"]
    assert len(_usage("call-retry")) == 2


def test_bad_format_twice_raises(model):
    model(["不是 JSON"])
    with SessionLocal() as db, pytest.raises(call.OutputInvalid):
        call.call(db, "kb-reviewer", "审这一条", actor="call-bad", parse=call.json_object)


def test_md_model_tier_wins_over_caller_default(model):
    model(["好"])
    with SessionLocal() as db:
        assert call.call(db, "solution-designer", "x", actor="t", level="light").level == "reasoning"  # md 写了推理
        assert call.call(db, "kb-reviewer", "x", actor="t", level="light").level == "light"            # md 没写


def test_disabled_agent_is_not_called(model):
    m = model(["好"])
    with SessionLocal() as db:
        a = db.get(Agent, "kb-reviewer")
        a.enabled = False
        db.flush()
        with pytest.raises(call.AgentUnavailable):
            call.call(db, "kb-reviewer", "x", actor="t")
        db.rollback()
    assert m.requests == []


def test_json_object():
    assert call.json_object('前言 {"a": 1} 结尾') == {"a": 1}
    for bad in ("没有", "[1, 2]", "{坏的}"):
        with pytest.raises(ValueError):
            call.json_object(bad)
