"""测试环境隔离：容器或测试机 .env 里的配置不能漏进单元测试（测试机误报：RERANK_BASE_URL、LLM_EXTERNAL、
KB_SINGLE_REVIEWER_PUBLIC 等）。conftest 在设测试配置之前清掉所有对应 Settings 字段的环境变量，并不读 .env 文件。"""
from __future__ import annotations

from app.core.config import Settings, get_settings
from tests.envguard import clean_settings_env


def test_clean_removes_settings_vars_only() -> None:
    env = {"RERANK_BASE_URL": "http://rerank:8000", "LLM_EXTERNAL": "true", "kb_single_reviewer_public": "true",
           "TEST_DATABASE_URL": "x", "PATH": "/bin"}
    removed = clean_settings_env(env)
    assert env == {"TEST_DATABASE_URL": "x", "PATH": "/bin"}
    assert sorted(removed) == ["LLM_EXTERNAL", "RERANK_BASE_URL", "kb_single_reviewer_public"]


def test_settings_do_not_read_env_file() -> None:
    assert Settings.model_config.get("env_file") is None


def test_leaky_settings_at_defaults() -> None:
    s = get_settings()
    assert s.rerank_base_url == "" and s.embed_base_url == "" and s.llm_base_url == ""
    assert s.llm_external is False and s.rerank_external is False
    assert s.kb_single_reviewer_public is False


def test_agents_dir_pinned_to_repo() -> None:
    """测试机容器里有 /srv/agents（部署挂载的 md，可能是别的版本）：测试一律读本仓库 agents/，
    不随部署副本变（测试机误报：test_md_model_tier_wins_over_caller_default）。"""
    from pathlib import Path

    from app.agents import registry
    repo_agents = Path(__file__).resolve().parents[2] / "agents"
    assert registry.agents_dir().resolve() == repo_agents.resolve()
    assert Path(get_settings().agents_dir).resolve() == repo_agents.resolve()
