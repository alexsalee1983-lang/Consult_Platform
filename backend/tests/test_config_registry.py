"""四期 G3①：统一配置登记（docs/4A架构设计.md 5.6 配置三层）。

锁定层、方法层、运行层汇成一个只读视图，每层和总体各出一个哈希；密钥只显示“已配置/未配置”。"""
import json

from app.core.config import get_settings
from app.main import app
from app.services import config_registry
from .conftest import create_user, logged_in_client


def _layer(reg: dict, key: str) -> dict:
    return next(x for x in reg["layers"] if x["key"] == key)


def test_three_layers_and_hashes():
    reg = config_registry.build()
    assert [x["key"] for x in reg["layers"]] == ["locked", "method", "runtime"]
    assert [x["name"] for x in reg["layers"]] == ["锁定层", "方法层", "运行层"]
    for layer in reg["layers"]:
        assert len(layer["sha"]) == 64 and layer["items"]
        for item in layer["items"]:
            assert {"key", "value", "source"} <= set(item)
    assert len(reg["sha"]) == 64
    assert config_registry.build()["sha"] == reg["sha"]     # 同样配置哈希稳定


def test_secrets_never_shown(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_api_key", "sk-SECRET-G3-0001")
    monkeypatch.setattr(s, "minio_secret_key", "minio-SECRET-G3")
    monkeypatch.setattr(s, "rerank_api_key", "")
    reg = config_registry.build()
    raw = json.dumps(reg, ensure_ascii=False)
    assert "SECRET-G3" not in raw
    items = {i["key"]: i["value"] for i in _layer(reg, "locked")["items"]}
    assert items["llm_api_key"] == "已配置" and items["rerank_api_key"] == "未配置"


def test_runtime_change_only_moves_runtime_hash(monkeypatch):
    before = config_registry.build()
    monkeypatch.setattr(get_settings(), "kb_vector_min_similarity", 0.77)
    after = config_registry.build()
    assert _layer(after, "runtime")["sha"] != _layer(before, "runtime")["sha"]
    assert _layer(after, "locked")["sha"] == _layer(before, "locked")["sha"]
    assert _layer(after, "method")["sha"] == _layer(before, "method")["sha"]
    assert after["sha"] != before["sha"]
    items = {i["key"]: i for i in _layer(after, "runtime")["items"]}
    assert items["kb_vector_min_similarity"]["value"] == 0.77
    assert items["kb_agent_auto_publish"]["confirm"] is True      # 开自动发布要负责人二次确认
    assert items["llm_model_reasoning"]["confirm"] is True        # 切模型要负责人二次确认


def test_method_layer_lists_agents_and_catalog():
    items = {i["key"]: i for i in _layer(config_registry.build(), "method")["items"]}
    assert any(k.startswith("agent:") for k in items)
    assert any(k.startswith("products/") for k in items) and any(k.startswith("modules/") for k in items)
    assert all(len(str(i["value"])) == 12 for k, i in items.items() if k.startswith(("products/", "modules/")))


def test_admin_endpoint():
    admin = logged_in_client(app, create_user(("sys_admin",)))
    try:
        r = admin.get("/api/admin/config-registry")
    finally:
        admin.__exit__(None, None, None)
    assert r.status_code == 200
    assert r.json()["sha"] == config_registry.build()["sha"]
