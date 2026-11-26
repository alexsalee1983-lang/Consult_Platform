"""四期 A28②：D1 启动包从合同导入（Agent 抽要点、程序成稿）。"""
import pytest
from sqlalchemy import select

from app.agents import call
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Job
from app.project import form_import, forms
from app.services import gateway, jobs, storage

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project

CONTRACT = ("服务合同（虚拟）\n甲方：BANK-A\n乙方：咨询公司\n"
            "服务内容：安全运营现状评估、运营体系设计。不包括等级保护测评。\n服务期限：2027-01-05 至 2027-03-31。\n")
AGENT_OUT = {
    "goal": "帮助 BANK-A 建立安全运营体系",
    "period": "2027-01-05   至\n2027-03-31",
    "in_scope": ["安全运营现状评估", "运营体系设计"],
    "out_of_scope": "等级保护测评",
    "milestones": [["启动", "第 1 周", "启动包"], {"阶段": "调研", "时间": "第 2–4 周", "产出": "调研包"}],
    "roles": "客户方｜信息科技部｜确认范围",
    "background": "不在可导入字段里，应丢弃",
    "unknown": "x",
}


@pytest.fixture()
def fake_store(monkeypatch):
    objs: dict[str, bytes] = {}
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "put", lambda area, key, data, ct="": objs.__setitem__(f"{area}:{key}", data))
    monkeypatch.setattr(storage, "get", lambda area, key: objs[f"{area}:{key}"])
    monkeypatch.setattr(storage, "delete", lambda area, key: objs.pop(f"{area}:{key}", None))
    return objs


@pytest.fixture()
def fake_agent(monkeypatch):
    seen: list[str] = []

    def fake(db, code, message, **kw):
        seen.append(message)
        assert code == "delivery-pm" and kw["project"] is not None
        return call.Result(gateway.Completion(text="{}", model="m", demo=False), AGENT_OUT, "v", "standard")
    monkeypatch.setattr(form_import.call, "call", fake)
    return seen


def _client(tier: str = "standard"):
    code = _new_project(tier)
    return code, logged_in_client(app, create_user(("consultant",), {code: "member"}))


def _run_last_job() -> None:
    with SessionLocal() as db:
        job = db.scalars(select(Job).where(Job.kind == form_import.JOB_KIND).order_by(Job.id.desc())).first()
        jobs.run(db, job)


def test_import_fields_are_required_and_prefill():
    keys = {f.key for f in form_import.import_fields(forms.load_form("D1"))}
    assert {"goal", "period", "in_scope", "out_of_scope", "roles"} <= keys
    assert {"milestones", "deliverables", "frameworks"} <= keys and "background" not in keys


def test_contract_import_fills_form_values(fake_store, fake_agent):
    code, c = _client()
    with c:
        assert c.get(f"{_base(code, 'D1')}/form/import").json()["status"] == ""
        r = c.post(f"{_base(code, 'D1')}/form/import", files={"file": ("合同.txt", CONTRACT.encode(), "text/plain")})
        assert r.status_code == 202, r.text
        assert any(k.endswith("contract.txt") for k in fake_store)
        _run_last_job()
        st = c.get(f"{_base(code, 'D1')}/form/import").json()
    assert st["status"] == "done", st
    v = st["values"]
    assert v["period"] == "2027-01-05 至 2027-03-31"                   # text 合并空白
    assert v["in_scope"] == "安全运营现状评估\n运营体系设计"            # 数组按行
    assert v["milestones"] == "启动｜第 1 周｜启动包\n调研｜第 2–4 周｜调研包"
    assert "background" not in v and "unknown" not in v
    assert not any(k.endswith("contract.txt") for k in fake_store)    # 合同正文不留存
    assert "服务期限" in fake_agent[0] and "out_of_scope" in fake_agent[0]
    with SessionLocal() as db:                                         # 审计不记原文
        logs = db.scalars(select(AuditLog).where(AuditLog.action == form_import.ACTION,
                                                 AuditLog.project_code == code)).all()
        assert {lg.result for lg in logs} >= {"queued", "allowed"}
        assert all("BANK-A" not in (lg.detail or "") and "服务期限" not in (lg.detail or "") for lg in logs)


def test_strict_tier_refused(fake_store, fake_agent):
    code, c = _client("strict")
    with c:
        r = c.post(f"{_base(code, 'D1')}/form/import", files={"file": ("合同.txt", CONTRACT.encode(), "text/plain")})
    assert r.status_code == 422 and "严格档" in r.text
    assert not fake_store and not fake_agent


def test_guard_blocks_contract(fake_store, fake_agent):
    code, c = _client()
    with c:
        r = c.post(f"{_base(code, 'D1')}/form/import",
                   files={"file": ("合同.txt", (CONTRACT + "数据库口令：Passw0rd!2027\n").encode(), "text/plain")})
    assert r.status_code == 422 and r.json()["detail"]["blocked"] is True
    assert not fake_store


def test_bad_kind_and_no_form(fake_store, fake_agent):
    code, c = _client()
    with c:
        r = c.post(f"{_base(code, 'D1')}/form/import", files={"file": ("a.xlsx", b"PK\x03\x04", "application/zip")})
        assert r.status_code == 422
        r = c.post(f"{_base(code, 'D4')}/form/import", files={"file": ("合同.txt", CONTRACT.encode(), "text/plain")})
        assert r.status_code == 404


def test_storage_disabled(monkeypatch, fake_agent):
    monkeypatch.setattr(storage, "enabled", lambda: False)
    code, c = _client()
    with c:
        r = c.post(f"{_base(code, 'D1')}/form/import", files={"file": ("合同.txt", CONTRACT.encode(), "text/plain")})
    assert r.status_code == 503


def test_non_member_404(fake_store):
    code = _new_project()
    other = logged_in_client(app, create_user(("consultant",), {}))
    with other:
        assert other.get(f"{_base(code, 'D1')}/form/import").status_code == 404
        r = other.post(f"{_base(code, 'D1')}/form/import", files={"file": ("合同.txt", CONTRACT.encode(), "text/plain")})
        assert r.status_code == 404


def test_agent_error_marks_failed(fake_store, monkeypatch):
    def boom(*a, **kw):
        raise gateway.GatewayError("x")
    monkeypatch.setattr(form_import.call, "call", boom)
    code, c = _client()
    with c:
        c.post(f"{_base(code, 'D1')}/form/import", files={"file": ("合同.txt", CONTRACT.encode(), "text/plain")})
        _run_last_job()
        st = c.get(f"{_base(code, 'D1')}/form/import").json()
    assert st["status"] == "failed" and "GatewayError" in st["error"] and st["values"] == {}


def test_result_gone_reports_failed(fake_store, fake_agent):
    code, c = _client()
    with c:
        c.post(f"{_base(code, 'D1')}/form/import", files={"file": ("合同.txt", CONTRACT.encode(), "text/plain")})
        _run_last_job()
        fake_store.clear()                       # 项目区被清理（如删除流程）后结果不可用
        st = c.get(f"{_base(code, 'D1')}/form/import").json()
    assert st["status"] == "failed" and "重新导入" in st["error"]


def test_guard_on_agent_output(fake_store, monkeypatch):
    out = {"goal": "数据库口令：Passw0rd!2027"}
    monkeypatch.setattr(form_import.call, "call", lambda *a, **kw: call.Result(
        gateway.Completion(text="{}", model="m", demo=False), out, "v", "standard"))
    code, c = _client()
    with c:
        c.post(f"{_base(code, 'D1')}/form/import", files={"file": ("合同.txt", CONTRACT.encode(), "text/plain")})
        _run_last_job()
        st = c.get(f"{_base(code, 'D1')}/form/import").json()
    assert st["status"] == "failed" and "机密级" in st["error"]
