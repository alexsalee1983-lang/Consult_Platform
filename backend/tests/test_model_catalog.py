"""模型目录 v1 与异构评审（三期 G2，4A 架构 5.3）：家族校验、备选切换、同家族降级、未配置目录时与原来一致。
模型服务用 httpx.MockTransport 模拟，不同模型 ID 代表不同家族，不连真实地址。"""
import json

import httpx
import pytest

from app.core.bootstrap import BootstrapError, check_settings
from app.core.config import get_settings
from app.services import gateway, model_catalog
from app.services.gateway import GatewayError

FULL = {
    "models": [
        {"id": "ds-r", "family": "deepseek", "tier": "reasoning"},
        {"id": "ds-s", "family": "deepseek", "tier": ["standard", "light"]},
        {"id": "glm-s", "family": "glm", "tier": "standard"},
        {"id": "kimi-s", "family": "kimi", "tier": "standard"},
        {"id": "mm-s", "family": "minimax", "tier": "standard"},
    ],
}


class Multi:
    """按请求里的模型 ID 回复；down 里的模型返回 503。"""

    def __init__(self):
        self.down: set[str] = set()
        self.sent: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m["id"]} for m in FULL["models"]]})
        body = json.loads(request.content)
        self.sent.append(body["model"])
        if body["model"] in self.down:
            return httpx.Response(503, json={"error": "x"})
        if body.get("stream"):
            lines = ["data: " + json.dumps({"model": body["model"], "choices": [{"delta": {"content": "好"}}]}),
                     "data: [DONE]"]
            return httpx.Response(200, content="\n\n".join(lines).encode(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"model": body["model"], "usage": {},
                                         "choices": [{"message": {"content": "评审意见"}}]})


@pytest.fixture()
def multi(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(s, "llm_max_retries", 0)
    monkeypatch.setattr(gateway, "_sleep", lambda _x: None)
    m = Multi()
    gateway.set_transport(httpx.MockTransport(m.handler))
    yield m
    gateway.set_transport(None)


def _catalog(monkeypatch, data: dict) -> None:
    monkeypatch.setattr(get_settings(), "llm_model_catalog", json.dumps(data))


def _call(**kw):
    return gateway.complete("请评审这份交付件", tier="standard", project_code="BANK-A", **kw)


# ---- 未配置目录：与原来完全一致 ----

def test_no_catalog_keeps_single_model_behaviour(multi):
    assert model_catalog.load() is None
    out = _call(role="review", exclude_families={"deepseek"})
    assert multi.sent == [get_settings().llm_model_standard]
    assert out.family == "" and not out.degraded and out.fallbacks == 0


def test_no_catalog_error_is_not_retried_on_other_models(multi):
    multi.down.add(get_settings().llm_model_standard)
    with pytest.raises(GatewayError):
        _call()
    assert len(multi.sent) == 1


# ---- 首选与家族校验 ----

def test_generate_prefers_deepseek_by_tier(multi, monkeypatch):
    _catalog(monkeypatch, FULL)
    assert _call(level="reasoning").model == "ds-r"
    out = _call(level="standard")
    assert (out.model, out.family, out.degraded) == ("ds-s", "deepseek", False)


def test_review_must_differ_from_generator_family(multi, monkeypatch):
    _catalog(monkeypatch, FULL)
    assert _call(role="review", exclude_families={"deepseek"}).family == "glm"
    out = _call(role="review", exclude_families={"glm"})
    assert (out.model, out.family) == ("kimi-s", "kimi")


def test_review_without_generator_info_excludes_default_generator(multi, monkeypatch):
    # 评审首选显式配成 DeepSeek，生成方家族不明时按生成首选（DeepSeek）排除，平台强制换家族
    _catalog(monkeypatch, {**FULL, "roles": {"review": ["ds-s", "glm-s"]}})
    assert _call(role="review").family == "glm"


def test_arbitrate_differs_from_both_sides(multi, monkeypatch):
    _catalog(monkeypatch, FULL)
    out = _call(role="arbitrate", exclude_families={"deepseek", "kimi"})
    assert out.family == "minimax" and not out.degraded


# ---- 备选切换 ----

def test_falls_back_when_preferred_model_errors(multi, monkeypatch):
    _catalog(monkeypatch, FULL)
    multi.down.add("glm-s")
    out = _call(role="review", exclude_families={"deepseek"})
    assert (out.model, out.fallbacks) == ("kimi-s", 1)
    assert multi.sent == ["glm-s", "kimi-s"]


def test_unevaluated_model_is_skipped(multi, monkeypatch):
    data = json.loads(json.dumps(FULL))
    data["models"][2]["evaluated"] = False                # glm 未通过评测
    _catalog(monkeypatch, data)
    assert _call(role="review", exclude_families={"deepseek"}).model == "kimi-s"
    assert "glm-s" not in multi.sent


def test_all_candidates_down_raises_gateway_error(multi, monkeypatch):
    _catalog(monkeypatch, {"models": [{"id": "glm-s", "family": "glm", "tier": "standard"},
                                      {"id": "kimi-s", "family": "kimi", "tier": "standard"}]})
    multi.down |= {"glm-s", "kimi-s"}
    with pytest.raises(GatewayError) as e:
        _call(role="review", exclude_families={"deepseek"})
    assert e.value.attempts == 2


def test_stream_falls_back_before_output(multi, monkeypatch):
    _catalog(monkeypatch, FULL)
    multi.down.add("ds-s")
    parts = list(gateway.stream("你好", tier="standard", project_code="BANK-A"))
    done = parts[-1]
    assert done.model == "glm-s" and done.fallbacks == 1 and done.text == "好"


# ---- 同家族降级 ----

def test_single_family_degrades_without_blocking(multi, monkeypatch):
    _catalog(monkeypatch, {"models": [{"id": "ds-s", "family": "deepseek", "tier": "standard"}]})
    out = _call(role="review", exclude_families={"deepseek"})
    assert (out.model, out.family, out.degraded) == ("ds-s", "deepseek", True)


def test_generation_is_never_marked_degraded(multi, monkeypatch):
    _catalog(monkeypatch, {"models": [{"id": "ds-s", "family": "deepseek", "tier": "standard"}]})
    assert not _call(exclude_families={"deepseek"}).degraded


# ---- 目录加载与家族识别 ----

def test_family_inferred_from_model_id_for_old_records(monkeypatch):
    assert model_catalog.family_of("deepseek-v4.1-flash") == "deepseek"
    assert model_catalog.family_of("GLM-5-Air") == "glm"
    assert model_catalog.family_of("moonshot-v1") == "kimi"
    assert model_catalog.family_of("mock-model") == ""
    _catalog(monkeypatch, FULL)
    assert model_catalog.family_of("mm-s") == "minimax"


def test_catalog_from_yaml_file(tmp_path, monkeypatch):
    f = tmp_path / "models.yaml"
    f.write_text("models:\n  - id: glm-s\n    family: glm\n    tier: standard\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "llm_model_catalog", str(f))
    cat = model_catalog.load()
    assert cat is not None and [m.id for m in cat.models] == ["glm-s"]


@pytest.mark.parametrize("bad", [
    {"models": []},
    {"models": [{"id": "x", "family": "glm", "tier": "huge"}]},
    {"models": [{"id": "x", "family": "glm", "tier": "standard"}], "roles": {"review": ["nope"]}},
    {"models": [{"id": "x", "family": "glm", "tier": "standard"}], "roles": {"judge": ["x"]}},
])
def test_invalid_catalog_refused_at_startup(monkeypatch, bad):
    _catalog(monkeypatch, bad)
    with pytest.raises(model_catalog.CatalogError):
        model_catalog.load()
    with pytest.raises(BootstrapError, match="模型目录"):
        check_settings(get_settings())


def test_availability_with_catalog(multi, monkeypatch):
    _catalog(monkeypatch, FULL)
    assert gateway.availability() == "ok"


def test_example_catalog_is_valid(monkeypatch):
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / "deploy" / "model_catalog.example.yaml"
    cat = model_catalog.load(str(example))
    assert cat is not None
    plan = model_catalog.plan(cat, "review", "standard", {"deepseek"})
    assert plan.models[0].family == "glm" and "minimax-standard-model" not in [m.id for m in plan.models]
