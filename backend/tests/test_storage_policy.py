"""G1①：对象存储最小权限。后端用应用账号（只能读写两个区的桶），运维用根账号一次性建桶、建账号、挂策略；
生产环境用根账号连对象存储拒绝启动。"""
from __future__ import annotations

import pytest

from app.core.bootstrap import BootstrapError, check_settings
from app.core.config import Settings
from app.services import storage


def _prod(**kw) -> Settings:
    base = {"app_env": "prod", "session_backend": "redis", "cookie_secure": True,
            "test_account_username": "", "test_account_password": "", "test_account_totp_secret": "",
            "bootstrap_admin_username": "", "bootstrap_admin_password": "", "bootstrap_admin_totp_secret": "",
            "minio_endpoint": "minio:9000", "minio_access_key": "consult-app", "minio_secret_key": "x" * 24,
            "minio_root_user": "minio-root", "totp_keys": "p1:" + "A" * 43 + "=",
            "database_url": "postgresql+psycopg://consult:p@postgres:5432/consult",
            "kb_database_url": "postgresql+psycopg://consult_kb:q@postgres:5432/consult_kb"}
    return Settings(_env_file=None, **{**base, **kw})


def test_app_policy_only_two_buckets_no_admin():
    pol = storage.app_policy(["project-area", "knowledge-area"])
    resources = {r for st in pol["Statement"] for r in st["Resource"]}
    assert resources == {"arn:aws:s3:::project-area", "arn:aws:s3:::project-area/*",
                         "arn:aws:s3:::knowledge-area", "arn:aws:s3:::knowledge-area/*"}
    actions = {a for st in pol["Statement"] for a in st["Action"]}
    assert {"s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"} <= actions
    assert all(st["Effect"] == "Allow" for st in pol["Statement"])
    # 不能建删桶、改桶策略和加密配置，不能有通配和管理权限
    for bad in ("s3:*", "admin:*", "s3:CreateBucket", "s3:DeleteBucket", "s3:PutBucketPolicy",
                "s3:PutEncryptionConfiguration"):
        assert bad not in actions
    assert not any(a.startswith("admin:") or "*" in a for a in actions)


class _FakeClient:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.buckets = set(existing or ())
        self.encrypted: set[str] = set()

    def bucket_exists(self, name: str) -> bool:
        return name in self.buckets

    def make_bucket(self, name: str) -> None:
        self.buckets.add(name)

    def get_bucket_encryption(self, name: str):
        return object() if name in self.encrypted else None

    def set_bucket_encryption(self, name: str, cfg) -> None:
        self.encrypted.add(name)


class _FakeAdmin:
    def __init__(self) -> None:
        self.users: dict[str, str] = {}
        self.policies: dict[str, dict] = {}
        self.attached: dict[str, list[str]] = {}

    def user_add(self, access_key: str, secret_key: str) -> str:
        self.users[access_key] = secret_key
        return ""

    def policy_add(self, policy_name: str, policy_file=None, policy: dict | None = None) -> str:
        self.policies[policy_name] = policy or {}
        return ""

    def attach_policy(self, policies: list[str], user: str | None = None, group: str | None = None) -> str:
        if user in self.attached and set(policies) <= set(self.attached[user]):
            raise RuntimeError("XMinioAdminPolicyChangeAlreadyApplied")
        self.attached.setdefault(user or "", []).extend(policies)
        return ""


def test_provision_creates_buckets_account_and_policy_idempotent():
    c, a = _FakeClient({"project-area"}), _FakeAdmin()
    r = storage.provision("minio-root", "consult-app", "y" * 24, client=c, admin=a)
    assert c.buckets == {"project-area", "knowledge-area"} and c.encrypted == c.buckets
    assert a.users == {"consult-app": "y" * 24}
    assert a.attached == {"consult-app": [storage.APP_POLICY_NAME]}
    assert a.policies[storage.APP_POLICY_NAME] == storage.app_policy(["project-area", "knowledge-area"])
    assert r.created_buckets == ["knowledge-area"]
    # 再跑一次：不重复建桶，已挂的策略不报错（口令照新值更新）
    r2 = storage.provision("minio-root", "consult-app", "z" * 24, client=c, admin=a)
    assert r2.created_buckets == [] and a.users["consult-app"] == "z" * 24


def test_provision_refuses_root_as_app_account_and_short_secret():
    with pytest.raises(storage.StorageError, match="根账号"):
        storage.provision("minio-root", "minio-root", "y" * 24, client=_FakeClient(), admin=_FakeAdmin())
    with pytest.raises(storage.StorageError, match="20"):
        storage.provision("minio-root", "consult-app", "short", client=_FakeClient(), admin=_FakeAdmin())


def test_ensure_buckets_does_not_reset_existing_encryption(monkeypatch):
    """应用账号没有改加密配置的权限：桶已加密时 ensure_buckets 不再调 set_bucket_encryption。"""
    c = _FakeClient({"project-area", "knowledge-area"})
    c.encrypted = set(c.buckets)
    calls: list[str] = []
    monkeypatch.setattr(c, "set_bucket_encryption", lambda name, cfg: calls.append(name))
    monkeypatch.setattr(storage, "_get_client", lambda: c)
    storage.reset()
    storage.ensure_buckets()
    assert calls == []
    storage.reset()


def test_prod_refuses_object_storage_root_account():
    with pytest.raises(BootstrapError, match="根账号"):
        check_settings(_prod(minio_access_key="minio-root"))
    with pytest.raises(BootstrapError, match="MINIO_ROOT_USER"):
        check_settings(_prod(minio_root_user=""))
    check_settings(_prod())
    # 开发、测试环境沿用根账号不拦
    check_settings(_prod(app_env="test", minio_access_key="minio-root"))


def test_cli_storage_provision_needs_root_password(monkeypatch, capsys):
    from app import cli

    monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
    monkeypatch.setenv("MINIO_ROOT_USER", "minio-root")
    assert cli.main(["storage-provision"]) == 1
    assert "MINIO_ROOT_PASSWORD" in capsys.readouterr().err
