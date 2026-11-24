"""四期 G3②：运行层配置编辑（理由、审计只记哈希、回滚到任一版本、敏感项须另一位系统管理员二次确认）。"""
import json

import pytest
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, RuntimeConfigItem, RuntimeConfigVersion
from app.services import config_registry, runtime_config
from .conftest import create_user, logged_in_client

BASE = "/api/admin/config-registry/runtime"


@pytest.fixture(autouse=True)
def _clean():
    def wipe():   # 库是整轮测试共用的：留下的版本会被其他测试里的 sync 用上
        with SessionLocal() as db:
            db.execute(delete(RuntimeConfigItem))
            db.execute(delete(RuntimeConfigVersion))
            db.commit()
        runtime_config.reset_process_state()

    wipe()
    yield
    wipe()


@pytest.fixture
def admins():
    a = logged_in_client(app, create_user(("sys_admin",)))
    b = logged_in_client(app, create_user(("sys_admin",)))
    yield a, b
    a.__exit__(None, None, None)
    b.__exit__(None, None, None)


def _runtime_item(key: str) -> dict:
    reg = config_registry.build()
    layer = next(x for x in reg["layers"] if x["key"] == "runtime")
    return next(i for i in layer["items"] if i["key"] == key)


def test_reason_required_and_only_runtime_keys(admins):
    a, _ = admins
    assert a.post(BASE, json={"reason": "", "values": {"kb_vector_min_similarity": 0.6}}).status_code == 422
    assert a.post(BASE, json={"reason": "调门槛", "values": {"llm_api_key": "x"}}).status_code == 422   # 锁定层不能改
    assert a.post(BASE, json={"reason": "调门槛", "values": {"kb_rerank_candidates": "很多"}}).status_code == 422


def test_plain_change_applies_and_moves_hash(admins):
    a, _ = admins
    before = config_registry.effective_sha()
    default = get_settings().kb_vector_min_similarity
    r = a.post(BASE, json={"reason": "试用期放宽门槛", "values": {"kb_vector_min_similarity": 0.61}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "applied" and body["needs_confirm"] is False
    assert get_settings().kb_vector_min_similarity == 0.61
    assert body["config_registry_sha"] != before
    item = _runtime_item("kb_vector_min_similarity")
    assert item["value"] == 0.61 and item["source"] == "页面"
    hist = a.get(f"{BASE}/versions").json()["versions"]
    assert hist[0]["items"][0]["old_value"] == default and hist[0]["reason"] == "试用期放宽门槛"


def test_audit_records_hashes_not_values(admins):
    a, _ = admins
    r = a.post(BASE, json={"reason": "理由-G3审计", "values": {"llm_model_light": "model-G3-SECRETNAME"}})
    vid = r.json()["id"]
    with SessionLocal() as db:
        rows = list(db.scalars(select(AuditLog).where(AuditLog.action.like("config.runtime.%"))))
    raw = json.dumps([r.detail for r in rows], ensure_ascii=False, default=str)
    assert "SECRETNAME" not in raw and "理由-G3审计" not in raw
    details = [json.loads(x.detail) if isinstance(x.detail, str) else x.detail for x in rows]
    d = [x for x in details if x.get("version") == vid][-1]     # 清表后版本号会复用，取最新一条
    assert d["keys"] == ["llm_model_light"] and len(d["values"][0]["new"]) == 16


def test_sensitive_change_needs_another_admin(admins):
    a, b = admins
    old = get_settings().llm_model_reasoning
    r = a.post(BASE, json={"reason": "切推理模型", "values": {"llm_model_reasoning": "model-x"}})
    assert r.status_code == 201
    vid = r.json()["id"]
    assert r.json()["status"] == "pending" and get_settings().llm_model_reasoning == old   # 未确认不生效
    assert a.post(f"{BASE}/versions/{vid}/confirm").status_code == 403                    # 发起人不能自己确认
    r = b.post(f"{BASE}/versions/{vid}/confirm")
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert get_settings().llm_model_reasoning == "model-x"
    assert b.post(f"{BASE}/versions/{vid}/confirm").status_code == 409


def test_reject_keeps_value(admins):
    a, b = admins
    old = get_settings().auto_flow
    vid = a.post(BASE, json={"reason": "开自动推进", "values": {"auto_flow": not old}}).json()["id"]
    r = b.post(f"{BASE}/versions/{vid}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert get_settings().auto_flow == old


def test_rollback_to_any_version(admins):
    a, _ = admins
    default = get_settings().kb_rerank_candidates
    v1 = a.post(BASE, json={"reason": "一", "values": {"kb_rerank_candidates": 11}}).json()["id"]
    sha1 = config_registry.effective_sha()
    a.post(BASE, json={"reason": "二", "values": {"kb_rerank_candidates": 22, "kb_candidate_days": 9}})
    assert get_settings().kb_rerank_candidates == 22
    r = a.post(f"{BASE}/rollback", json={"reason": "回到一", "to_version": v1})
    assert r.status_code == 201 and r.json()["status"] == "applied"
    assert get_settings().kb_rerank_candidates == 11
    assert config_registry.effective_sha() == sha1                   # 回到版本一的生效配置哈希
    assert a.post(f"{BASE}/rollback", json={"reason": "再回", "to_version": v1}).status_code == 409
    r = a.post(f"{BASE}/rollback", json={"reason": "全部恢复", "to_version": 0})
    assert r.status_code == 201 and get_settings().kb_rerank_candidates == default
    assert _runtime_item("kb_rerank_candidates")["source"] == ".env"
    assert a.post(f"{BASE}/rollback", json={"reason": "不存在", "to_version": 999999}).status_code == 404


def test_rollback_of_sensitive_key_needs_confirm(admins):
    a, b = admins
    old = get_settings().llm_model_standard
    vid = a.post(BASE, json={"reason": "切", "values": {"llm_model_standard": "model-y"}}).json()["id"]
    b.post(f"{BASE}/versions/{vid}/confirm")
    r = a.post(f"{BASE}/rollback", json={"reason": "切回", "to_version": 0})
    assert r.json()["status"] == "pending" and get_settings().llm_model_standard == "model-y"
    b.post(f"{BASE}/versions/{r.json()['id']}/confirm")
    assert get_settings().llm_model_standard == old


def test_sync_picks_up_other_process():
    """另一进程（worker）启动或扫描时 sync 追上库里的版本。"""
    with SessionLocal() as db:
        v = runtime_config.propose(db, "proc-a", "调抽检", {"kb_sample_rate_agent": 0.42})
        assert v.status == "applied"
    runtime_config.reset_process_state()                 # 模拟另一个进程：settings 仍是 .env 值
    assert get_settings().kb_sample_rate_agent != 0.42
    with SessionLocal() as db:
        runtime_config.sync(db)
    assert get_settings().kb_sample_rate_agent == 0.42
