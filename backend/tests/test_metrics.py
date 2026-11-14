"""运行指标（M5 ⑤）：按路由模板计数，不含业务内容；业务端口没有 /metrics。"""
import re
from pathlib import Path

import httpx
from prometheus_client import generate_latest

from app.core import metrics
from app.core.db import SessionLocal
from app.main import app
from app.services import gateway, jobs

from .conftest import create_user, logged_in_client, login


def _text() -> str:
    return generate_latest(metrics.REGISTRY).decode()


def _value(name: str, **labels) -> float:
    v = metrics.REGISTRY.get_sample_value(name, labels)
    return v or 0.0


def test_route_template_not_raw_path():
    c = logged_in_client(app, create_user(("consultant",), projects={"BANK-A": "member"}))
    try:
        before = _value("cp_http_requests_total", method="GET", route="/api/projects/{code}", status="200")
        assert c.get("/api/projects/BANK-A").status_code == 200
        assert c.get("/api/projects/NO-SUCH-X9").status_code == 404
        after = _value("cp_http_requests_total", method="GET", route="/api/projects/{code}", status="200")
        assert after == before + 1
        assert _value("cp_http_requests_total", method="GET", route="/api/projects/{code}", status="404") >= 1
        c.get("/no/such/path/secret-looking-xyz")
        text = _text()
        assert "BANK-A" not in text and "NO-SUCH-X9" not in text and "secret-looking-xyz" not in text
        assert 'route="unmatched"' in text
    finally:
        c.__exit__(None, None, None)


def test_no_metrics_on_business_port():
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        assert c.get("/metrics").status_code in (401, 404)


def test_login_results_counted():
    from fastapi.testclient import TestClient
    name, pw, secret = create_user(("consultant",))
    ok0, bad0 = _value("cp_auth_logins_total", result="allowed"), _value("cp_auth_logins_total", result="blocked")
    with TestClient(app) as c:
        assert login(c, name, "Wrong-pass-1!", secret).status_code == 401
        assert login(c, name, pw, secret).status_code == 200
    assert _value("cp_auth_logins_total", result="allowed") == ok0 + 1
    assert _value("cp_auth_logins_total", result="blocked") == bad0 + 1
    assert name not in _text()


def test_upstream_calls_counted():
    gateway.set_transport(httpx.MockTransport(lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": "好"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})))
    try:
        before = _value("cp_upstream_requests_total", service="llm", outcome="ok")
        from app.core.config import get_settings
        s = get_settings()
        old = s.llm_base_url
        s.llm_base_url = "http://model.invalid/v1"
        try:
            gateway.complete("指标检查", tier="standard", project_code="CI")
        finally:
            s.llm_base_url = old
        assert _value("cp_upstream_requests_total", service="llm", outcome="ok") == before + 1
        assert "指标检查" not in _text()
    finally:
        gateway.set_transport(None)


def test_upstream_service_names():
    assert metrics.upstream_service("http://x/v1/chat/completions") == "llm"
    assert metrics.upstream_service("http://embed:8000/v1/embeddings") == "embed"
    assert metrics.upstream_service("http://rerank:8000/v1/rerank") == "rerank"
    assert metrics.upstream_service("http://x/other") == "other"


def test_jobs_counted_and_queue_state():
    metrics.register_state_collector()
    with SessionLocal() as db:
        jobs.enqueue(db, "system.selfcheck", {}, created_by="tester")
        before = _value("cp_jobs_total", kind="system.selfcheck", status="done")
        while jobs.run_once(db, "w-test", kinds=["system.selfcheck"]):
            pass
    assert _value("cp_jobs_total", kind="system.selfcheck", status="done") >= before + 1
    text = _text()
    assert "cp_jobs_by_status" in text and 'source="database"' in text


def test_guard_blocks_counted_by_kind_only():
    from app.services import guard
    before = _value("cp_guard_blocks_total", kind="private_key")
    pem = "-----BEGIN " + "PRIVATE KEY-----\nMIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEA\n-----END " + "PRIVATE KEY-----"
    assert guard.check(pem).blocked
    assert _value("cp_guard_blocks_total", kind="private_key") == before + 1
    assert "MIIBVQ" not in _text()


def test_audit_write_failure_counted(monkeypatch):
    import pytest

    from app.core import audit
    before = _value("cp_audit_write_failures_total")
    with SessionLocal() as db:
        monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(RuntimeError):
            audit.record(db, actor="t", action="metrics.test", result="allowed")
    assert _value("cp_audit_write_failures_total") == before + 1


def test_worker_verifies_audit_chain():
    from app import worker
    worker._verify_audit()
    assert _value("cp_audit_chain_ok") == 1
    assert _value("cp_audit_chain_verified_timestamp_seconds") > 0


def test_single_process_metrics_filtered_by_job():
    """backend、worker 都导出全部指标，只在一个进程里有意义的量查询时必须带 job（测试 #81：审计链告警恒误报）。"""
    root = Path(__file__).resolve().parents[2] / "deploy" / "monitoring"
    text_all = (root / "prometheus" / "alerts.yml").read_text(encoding="utf-8") + \
        (root / "grafana" / "dashboards" / "platform.json").read_text(encoding="utf-8").replace('\\"', '"')
    owner = {"cp_audit_chain_ok": "worker", "cp_audit_chain_verified_timestamp_seconds": "worker",
             "cp_workers_online": "backend", "cp_state_up": "backend"}
    for name, job in owner.items():
        uses = [m.group(0) for m in re.finditer(rf"{name}(\{{[^}}]*\}})?", text_all)]
        assert uses, name
        for sel in uses:
            assert f'job="{job}"' in sel, (name, sel)
