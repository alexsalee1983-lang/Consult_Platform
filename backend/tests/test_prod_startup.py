"""生产启动自检（三期 G1②）：APP_ENV=prod 时任一不安全配置都拒绝启动，一张表列全，缺一项就补一项。"""
import pytest

from app.core.bootstrap import BootstrapError, check_settings
from app.core.config import Settings

PROD_OK = {
    "app_env": "prod",
    "database_url": "postgresql+psycopg://consult_app:x@db:5432/consult",
    "kb_database_url": "postgresql+psycopg://consult_kb:x@db:5432/consult_kb",
    "session_backend": "redis", "cookie_secure": True,
    "test_account_username": "", "test_account_password": "",
    "minio_endpoint": "minio:9000", "minio_access_key": "consult-app", "minio_secret_key": "x" * 24,
    "minio_root_user": "root",
    "totp_keys": "p1:" + "A" * 43 + "=",
    "llm_base_url": "https://llm.intra.example/v1",
    "llm_external": False, "rerank_external": False, "seed_demo_data": False,
}


def test_prod_baseline_starts() -> None:
    check_settings(Settings(_env_file=None, **PROD_OK))


@pytest.mark.parametrize(("override", "message"), [
    ({"session_backend": "memory"}, "Redis"),
    ({"test_account_username": "tester"}, "测试账号"),
    ({"cookie_secure": False}, "COOKIE_SECURE"),
    ({"minio_endpoint": ""}, "对象存储"),
    ({"totp_keys": ""}, "TOTP_KEYS"),
    ({"kb_database_url": ""}, "知识区"),
    ({"llm_external": True}, "外部模型"),
    ({"rerank_external": True}, "外部重排"),
    ({"seed_demo_data": True}, "演示数据"),
    ({"llm_base_url": "http://mock-llm:8000/v1"}, "模拟模型"),
    ({"embed_base_url": "http://mock-llm:8000/v1"}, "模拟模型"),
    ({"rerank_base_url": "http://mock-llm:8000/v1"}, "模拟模型"),
])
def test_prod_refuses_unsafe_setting(override: dict[str, object], message: str) -> None:
    with pytest.raises(BootstrapError, match=message):
        check_settings(Settings(_env_file=None, **{**PROD_OK, **override}))


@pytest.mark.parametrize("override", [
    {"seed_demo_data": True}, {"llm_base_url": "http://mock-llm:8000/v1"}, {"llm_external": True},
])
def test_test_env_allows_dev_settings(override: dict[str, object]) -> None:
    check_settings(Settings(_env_file=None, **{**PROD_OK, "app_env": "test", **override}))
