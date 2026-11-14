"""外部模型保护（决策记录 2026-11-08）：严格档拒绝、强制假名化、审计标记、健康检查标记、生产拒绝启动。"""
import json

import httpx
import pytest
from sqlalchemy import select

from app.core.bootstrap import BootstrapError, check_settings
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, LlmUsage
from app.services import gateway

from .conftest import create_user, logged_in_client


class Model:
    def __init__(self):
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4.1-flash"}]})
        body = json.loads(request.content)
        self.requests.append(body)
        if body.get("stream"):
            lines = ['data: {"choices": [{"delta": {"content": "回复"}}]}', "data: [DONE]"]
            return httpx.Response(200, content="\n\n".join(lines).encode(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "回复"}}], "usage": {}})


@pytest.fixture()
def external(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_base_url", "https://api.example-cloud.test")
    monkeypatch.setattr(s, "llm_external", True)
    monkeypatch.setattr(s, "llm_pseudonymize", False)
    m = Model()
    gateway.set_transport(httpx.MockTransport(m.handler))
    yield m
    gateway.set_transport(None)


@pytest.fixture()
def client():
    c = logged_in_client(app, create_user(("consultant",), {"BANK-A": "member", "GRID-B": "member"}))
    yield c
    c.__exit__(None, None, None)


def _last_audit():
    with SessionLocal() as db:
        return db.scalars(select(AuditLog).where(AuditLog.action == "llm.chat").order_by(AuditLog.id.desc())).first()


def test_strict_project_refused_before_any_request(external, client):
    for path in ("/api/chat", "/api/chat/stream"):
        r = client.post(path, json={"project_code": "GRID-B", "message": "梳理访谈提纲"})
        assert r.status_code == 403 and "严格档" in r.json()["detail"]
    conv = client.post("/api/projects/GRID-B/conversations", json={"agent_code": "consultant-lead"}).json()
    r = client.post(f"/api/conversations/{conv['id']}/messages", json={"message": "梳理访谈提纲"})
    assert r.status_code == 403
    assert external.requests == []                                   # 没有任何请求发出
    assert client.get(f"/api/conversations/{conv['id']}").json()["messages"] == []   # 也没有保存
    a = _last_audit()
    assert a.result == "blocked" and json.loads(a.detail)["reason"] == "external_model_strict"
    with SessionLocal() as db:
        assert db.scalars(select(LlmUsage).order_by(LlmUsage.id.desc())).first().status == "blocked"


def test_standard_project_allowed_and_marked_external(external, client):
    r = client.post("/api/chat", json={"project_code": "BANK-A", "message": "一期人力模型怎么选？"})
    assert r.status_code == 200 and len(external.requests) == 1
    assert json.loads(_last_audit().detail)["external"] is True


def test_pseudonymization_forced_for_external(external):
    gateway.complete("某某城商行的值班情况", tier="standard", project_code="BANK-A", terms={"某某城商行": "客户"})
    sent = external.requests[-1]["messages"][-1]["content"]
    assert "某某城商行" not in sent and "【客户1】" in sent


def test_internal_model_not_marked(monkeypatch, client):
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    m = Model()
    gateway.set_transport(httpx.MockTransport(m.handler))
    try:
        r = client.post("/api/chat", json={"project_code": "GRID-B", "message": "梳理访谈提纲"})
        assert r.status_code == 200                                   # 内部模型：严格档照常可用
        assert json.loads(_last_audit().detail)["external"] is False
    finally:
        gateway.set_transport(None)


def test_demo_mode_is_not_external(monkeypatch, client):
    monkeypatch.setattr(get_settings(), "llm_external", True)
    monkeypatch.setattr(get_settings(), "llm_base_url", "")           # 没配地址：演示回复，不出公司
    r = client.post("/api/chat", json={"project_code": "GRID-B", "message": "梳理访谈提纲"})
    assert r.status_code == 200 and r.json()["demo"] is True


def test_health_reports_external(external, client):
    assert client.get("/api/health").json()["llm_external"] is True


def test_prod_refuses_external_model():
    base = {"database_url": "postgresql+psycopg://consult:x@db:5432/consult",
            "kb_database_url": "postgresql+psycopg://consult_kb:x@db:5432/consult_kb",
            "app_env": "prod", "session_backend": "redis", "cookie_secure": True, "test_account_username": "",
            "test_account_password": "", "llm_external": True,
            "minio_endpoint": "minio:9000", "minio_access_key": "k", "minio_secret_key": "x" * 24}
    with pytest.raises(BootstrapError, match="外部模型"):
        check_settings(Settings(_env_file=None, **base))
    check_settings(Settings(_env_file=None, **{**base, "app_env": "test"}))


def test_prod_refuses_external_rerank():
    base = {"database_url": "postgresql+psycopg://consult:x@db:5432/consult",
            "kb_database_url": "postgresql+psycopg://consult_kb:x@db:5432/consult_kb",
            "app_env": "prod", "session_backend": "redis", "cookie_secure": True, "test_account_username": "",
            "test_account_password": "", "rerank_external": True,
            "minio_endpoint": "minio:9000", "minio_access_key": "k", "minio_secret_key": "x" * 24}
    with pytest.raises(BootstrapError, match="外部重排"):
        check_settings(Settings(_env_file=None, **base))
    check_settings(Settings(_env_file=None, **{**base, "app_env": "test"}))


def test_external_rerank_availability_without_model_list(monkeypatch):
    """外部平台（如硅基流动）的模型列表默认可能不列重排模型：外部模式下能连通即算可用。"""
    s = get_settings()
    monkeypatch.setattr(s, "rerank_base_url", "https://rerank.example/v1")
    monkeypatch.setattr(s, "rerank_model", "Qwen/Qwen3-Reranker-8B")
    gateway.set_transport(httpx.MockTransport(lambda req: httpx.Response(200, json={"data": [{"id": "chat-model"}]})))
    try:
        assert gateway.rerank_availability().startswith("down")
        monkeypatch.setattr(s, "rerank_external", True)
        assert gateway.rerank_availability() == "ok"
    finally:
        gateway.set_transport(None)
