"""对象存储：分区、对象键校验、启动检查；接真实存储（CI 的平台 MinIO 镜像）时测读写、加密、健康检查。"""
import secrets

import pytest

from app.core.bootstrap import BootstrapError, check_settings
from app.core.config import Settings
from app.services import storage

from .conftest import on_s3


def test_areas_map_to_separate_buckets():
    assert storage.bucket("project") != storage.bucket("knowledge")
    with pytest.raises(ValueError):
        storage.bucket("staging")


@pytest.mark.parametrize("key", ["BANK-A/minutes/0001.docx", "KB/SOC/值班制度-v2.md", "a"])
def test_valid_keys(key):
    assert storage.check_key(key) == key


@pytest.mark.parametrize("key", ["", "/abs/path", "BANK-A/../GRID-B/x", "a//b", "a\nb", "a b", "..", "a/./../b"])
def test_unsafe_keys_rejected(key):
    with pytest.raises(ValueError):
        storage.check_key(key)


def test_prod_requires_object_storage_and_strong_secret():
    base = {"app_env": "prod", "session_backend": "redis", "cookie_secure": True, "test_account_username": "",
            "test_account_password": ""}
    with pytest.raises(BootstrapError, match="对象存储"):
        check_settings(Settings(_env_file=None, **base))
    with pytest.raises(BootstrapError, match="过短"):
        check_settings(Settings(_env_file=None, **base, minio_endpoint="minio:9000", minio_access_key="k",
                                minio_secret_key="short"))
    check_settings(Settings(_env_file=None, **base, minio_endpoint="minio:9000", minio_access_key="k",
                            minio_secret_key="x" * 24))


def test_disabled_storage_reports_not_enabled(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "minio_endpoint", "")
    storage.reset()
    try:
        assert storage.health() == "not enabled"
        with pytest.raises(storage.StorageDisabled):
            storage.put("project", "x", b"1")
    finally:
        storage.reset()


# ---- 接真实对象存储 ----

@on_s3
def test_put_get_stat_delete_roundtrip_and_encryption():
    key = f"CI/{secrets.token_hex(4)}/纪要.txt"
    body = "虚拟客户 BANK-A 访谈纪要".encode()
    o = storage.put("project", key, body, "text/plain; charset=utf-8")
    assert o.size == len(body)
    assert storage.get("project", key) == body
    st = storage.stat("project", key)
    assert st is not None and st.size == len(body)
    assert storage.is_encrypted("project", key)                 # 服务端加密
    assert storage.stat("knowledge", key) is None               # 两区互不可见
    storage.delete("project", key)
    assert storage.stat("project", key) is None
    storage.delete("project", key)                              # 重复删除不报错


@on_s3
def test_health_ok_with_encrypted_buckets():
    storage.ensure_buckets()
    assert storage.health() == "ok"


@on_s3
def test_health_endpoint_reports_object_storage():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/health").json()["checks"]["object_storage"] == "ok"
