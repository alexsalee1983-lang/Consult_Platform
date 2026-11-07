"""模型网关：重试、流式、可用性检查、假名化、用量计量。模型服务用 httpx.MockTransport 模拟（OpenAI 兼容）。"""
import json

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, LlmUsage
from app.services import gateway
from app.services.gateway import BlockedError, Completion, GatewayError
from app.services.pseudonym import Pseudonymizer

from .conftest import create_user, logged_in_client

MODEL = "deepseek-v4.1-flash"
REPLY = "建议采用 5×8 + 远程值守的一期人力模型。"


class FakeModel:
    """可编排的模拟模型服务：fail 列表依次决定每次请求的结果（状态码或异常），用完后正常返回。"""

    def __init__(self):
        self.fail: list = []
        self.calls: list[httpx.Request] = []
        self.models = [MODEL]
        self.reply = REPLY
        self.break_stream = False
        self.headers: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if self.fail:
            f = self.fail.pop(0)
            if isinstance(f, Exception):
                raise f
            return httpx.Response(f, json={"error": "x"}, headers=self.headers)
        body = json.loads(request.content)
        usage = {"prompt_tokens": 120, "completion_tokens": 30}
        if not body.get("stream"):
            return httpx.Response(200, json={"model": body["model"], "usage": usage,
                                             "choices": [{"message": {"content": self._answer(body)}}]})
        text = self._answer(body)
        lines = []
        for i in range(0, len(text), 5):
            lines.append("data: " + json.dumps({"model": body["model"],
                                                "choices": [{"delta": {"content": text[i:i + 5]}}]}, ensure_ascii=False))
        lines.append("data: " + json.dumps({"model": body["model"], "choices": [], "usage": usage}))
        lines.append("data: [DONE]")
        payload = "\n\n".join(lines).encode()
        if self.break_stream:  # 输出两段后断开
            cut = payload.index(b"\n\n", payload.index(b"\n\n") + 2) + 2
            return httpx.Response(200, stream=_BrokenStream(payload[:cut]))
        return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})

    def _answer(self, body) -> str:
        return self.reply

    def last_user_message(self) -> str:
        body = json.loads(self.calls[-1].content)
        return body["messages"][-1]["content"]


class _BrokenStream(httpx.SyncByteStream):
    def __init__(self, head: bytes):
        self.head = head

    def __iter__(self):
        yield self.head
        raise httpx.ReadError("connection reset")


@pytest.fixture()
def fake(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(s, "llm_max_retries", 2)
    monkeypatch.setattr(s, "llm_retry_backoff_seconds", 1.0)
    sleeps: list[float] = []
    monkeypatch.setattr(gateway, "_sleep", sleeps.append)
    m = FakeModel()
    m.sleeps = sleeps
    gateway.set_transport(httpx.MockTransport(m.handler))
    yield m
    gateway.set_transport(None)


def _call(**kw):
    return gateway.complete("一期人力模型怎么选？", tier="standard", project_code="BANK-A", **kw)


# ---- 一次返回 ----

def test_complete_returns_text_and_usage(fake):
    out = _call()
    assert out.text == REPLY and out.model == MODEL and not out.demo
    assert (out.prompt_tokens, out.completion_tokens, out.attempts) == (120, 30, 1)
    assert fake.calls[0].url.path == "/v1/chat/completions"


def test_routes_by_level(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_model_reasoning", "reasoner-x")
    fake.models.append("reasoner-x")
    _call(level="reasoning")
    assert json.loads(fake.calls[0].content)["model"] == "reasoner-x"


def test_retries_transient_errors_with_backoff(fake):
    fake.fail = [503, httpx.ConnectError("refused")]
    out = _call()
    assert out.text == REPLY and out.attempts == 3
    assert fake.sleeps == [1.0, 2.0]


def test_respects_retry_after(fake):
    fake.fail = [429]
    fake.headers = {"retry-after": "7"}
    _call()
    assert fake.sleeps == [7.0]


def test_gives_up_after_max_retries(fake):
    fake.fail = [503, 503, 503]
    with pytest.raises(GatewayError) as e:
        _call()
    assert e.value.attempts == 3 and len(fake.calls) == 3


def test_client_errors_are_not_retried(fake):
    fake.fail = [400]
    with pytest.raises(GatewayError) as e:
        _call()
    assert e.value.attempts == 1 and fake.sleeps == []


def test_blocked_before_any_network_call(fake):
    with pytest.raises(BlockedError):
        gateway.complete("核心交换机 10.1.20.1 密码：Abc@12345", tier="standard", project_code="BANK-A")
    assert fake.calls == []


# ---- 流式 ----

def _stream(**kw):
    return list(gateway.stream("一期人力模型怎么选？", tier="standard", project_code="BANK-A", **kw))


def test_stream_yields_deltas_then_completion(fake):
    items = _stream()
    *deltas, done = items
    assert len(deltas) > 1 and "".join(deltas) == REPLY
    assert isinstance(done, Completion) and done.text == REPLY and done.completion_tokens == 30
    sent = json.loads(fake.calls[0].content)
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}


def test_stream_retries_before_first_byte(fake):
    fake.fail = [502]
    *_, done = _stream()
    assert done.attempts == 2


def test_stream_break_mid_output_raises(fake):
    fake.break_stream = True
    gen = gateway.stream("一期人力模型怎么选？", tier="standard", project_code="BANK-A")
    got = []
    with pytest.raises(GatewayError, match="中断"):
        for item in gen:
            got.append(item)
    assert got  # 中断前已经输出了一部分


def test_demo_mode_streams_without_network(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_base_url", "")
    *deltas, done = _stream()
    assert done.demo and "".join(deltas) == done.text


# ---- 可用性检查 ----

def test_availability_ok_and_cached(fake):
    gateway._health_cache.clear()
    assert gateway.availability() == "ok"
    assert gateway.availability() == "ok"
    assert sum(c.url.path.endswith("/models") for c in fake.calls) == 1


def test_availability_reports_missing_model(fake):
    gateway._health_cache.clear()
    fake.models = ["other-model"]
    assert gateway.availability().startswith("down") and MODEL in gateway.availability()


def test_availability_down(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")

    def boom(request):
        raise httpx.ConnectError("refused")

    gateway.set_transport(httpx.MockTransport(boom))
    try:
        assert gateway.availability() == "down: ConnectError"
    finally:
        gateway.set_transport(None)


# ---- 假名化 ----

TERMS = {"某某城商行": "客户", "张三": "人员", "核心账务系统": "系统"}


def test_pseudonymize_off_by_default(fake):
    gateway.complete("某某城商行的值班情况", tier="standard", project_code="BANK-A", terms=TERMS)
    assert "某某城商行" in fake.last_user_message()


def test_pseudonymize_replaces_and_restores(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_pseudonymize", True)
    p = Pseudonymizer.from_terms(TERMS)
    fake.reply = f"{p.terms['某某城商行']}应由{p.terms['张三']}牵头，先梳理{p.terms['核心账务系统']}的告警。"
    out = gateway.complete("某某城商行的张三问核心账务系统告警怎么梳理", tier="standard", project_code="BANK-A",
                           terms=TERMS)
    sent = fake.last_user_message()
    assert all(t not in sent for t in TERMS) and "【客户1】" in sent
    assert out.text == "某某城商行应由张三牵头，先梳理核心账务系统的告警。" and out.pseudonymized


def test_pseudonymize_restores_placeholders_split_across_stream_chunks(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_pseudonymize", True)
    fake.reply = "请【客户1】的【人员1】确认"   # 每 5 个字一段，占位符会被拆开
    *deltas, done = list(gateway.stream("x", tier="standard", project_code="BANK-A", terms=TERMS))
    assert "".join(deltas) == done.text == "请某某城商行的张三确认"
    assert all("【" not in d for d in deltas)


def test_longer_terms_replaced_first():
    p = Pseudonymizer.from_terms({"某某银行": "客户", "某某银行信息科技部": "部门"})
    assert p.apply("某某银行信息科技部和某某银行") == "【部门1】和【客户1】"


# ---- 接口：审计与用量 ----

@pytest.fixture()
def member():
    c = logged_in_client(app, create_user(("consultant",), {"BANK-A": "member"}))
    yield c
    c.__exit__(None, None, None)


def _latest_usage(username):
    with SessionLocal() as db:
        return db.scalars(select(LlmUsage).where(LlmUsage.username == username).order_by(LlmUsage.id.desc())).first()


def _me(client):
    return client.get("/api/auth/me").json()["username"]


def test_chat_records_usage_and_audit(fake, member):
    r = member.post("/api/chat", json={"project_code": "BANK-A", "message": "一期人力模型怎么选？"})
    assert r.status_code == 200 and r.json()["reply"] == REPLY and r.json()["demo"] is False
    u = _latest_usage(_me(member))
    assert (u.status, u.project_code, u.model, u.prompt_tokens, u.completion_tokens, u.stream) == \
        ("ok", "BANK-A", MODEL, 120, 30, False)
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "llm.chat").order_by(AuditLog.id.desc())).first()
    assert a.result == "allowed" and "一期人力模型" not in a.detail


def test_chat_model_down_returns_502_and_records_error(fake, member):
    fake.fail = [503, 503, 503]
    r = member.post("/api/chat", json={"project_code": "BANK-A", "message": "问题"})
    assert r.status_code == 502
    u = _latest_usage(_me(member))
    assert u.status == "error" and u.attempts == 3


def test_chat_stream_sse(fake, member):
    r = member.post("/api/chat/stream", json={"project_code": "BANK-A", "message": "一期人力模型怎么选？"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [(blk.split("\n")[0][7:], json.loads(blk.split("\n")[1][6:]))
              for blk in r.text.strip().split("\n\n")]
    assert "".join(d["text"] for e, d in events if e == "delta") == REPLY
    assert events[-1][0] == "done" and events[-1][1]["completion_tokens"] == 30
    u = _latest_usage(_me(member))
    assert u.stream and u.status == "ok" and u.completion_tokens == 30


def test_chat_stream_blocked_is_422_not_stream(fake, member):
    r = member.post("/api/chat/stream", json={"project_code": "BANK-A", "message": "密码：Abc@12345 主机 10.1.1.1"})
    assert r.status_code == 422
    assert _latest_usage(_me(member)).status == "blocked"
    assert fake.calls == []


def test_chat_stream_break_reports_error_event(fake, member):
    fake.break_stream = True
    r = member.post("/api/chat/stream", json={"project_code": "BANK-A", "message": "问题"})
    assert r.status_code == 200 and "event: error" in r.text
    assert _latest_usage(_me(member)).status == "error"


def test_admin_usage_report_has_no_project_dimension(fake, member):
    member.post("/api/chat", json={"project_code": "BANK-A", "message": "问题"})
    admin = logged_in_client(app, create_user(("sys_admin",)))
    try:
        rows = admin.get("/api/admin/usage").json()
    finally:
        admin.__exit__(None, None, None)
    assert rows and all("project_code" not in r for r in rows)
    assert any(r["username"] == _me(member) and r["calls"] >= 1 for r in rows)


def test_health_reports_model_availability(fake, member):
    gateway._health_cache.clear()
    assert member.get("/api/health").json()["checks"]["llm"] == "ok"
