"""M5 ②b：TOTP 共享密钥加密存储、明文自动加密、密钥轮换、生产必须配置密钥。"""
import base64
import secrets

import pyotp
import pytest
from sqlalchemy import select, text

from app.cli import main as cli_main
from app.core import secretbox
from app.core.bootstrap import BootstrapError, check_settings, encrypt_plaintext_rows
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.models import AuditLog, User

from .conftest import create_user, login


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _key(kid: str) -> str:
    return f"{kid}:{base64.b64encode(secrets.token_bytes(32)).decode()}"


@pytest.fixture
def keys(monkeypatch):
    """临时换 TOTP_KEYS；结束后恢复。"""
    def use(spec: str) -> None:
        monkeypatch.setenv("TOTP_KEYS", spec)
        get_settings.cache_clear()
    yield use
    monkeypatch.undo()
    get_settings.cache_clear()


def _raw(username: str) -> str:
    with SessionLocal() as db:
        return db.execute(text("SELECT totp_secret FROM users WHERE username = :u"), {"u": username}).scalar_one()


def test_stored_encrypted_and_login_works(client):
    name, password, secret = create_user(roles={"consultant"})
    raw = _raw(name)
    assert raw.startswith("v1:t1:") and secret not in raw
    assert login(client, name, password, secret).status_code == 200


def test_ciphertext_bound_to_key_and_tamper_detected(keys):
    enc = secretbox.encrypt("JBSWY3DPEHPK3PXP")
    assert secretbox.encrypt("JBSWY3DPEHPK3PXP") != enc  # 随机 nonce
    assert secretbox.decrypt(enc) == "JBSWY3DPEHPK3PXP"
    raw = bytearray(base64.b64decode(enc.split(":", 2)[2]))
    raw[-1] ^= 1
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.decrypt("v1:t1:" + base64.b64encode(bytes(raw)).decode())
    keys(_key("other"))
    with pytest.raises(secretbox.SecretBoxError, match="没有编号为 t1"):
        secretbox.decrypt(enc)


def test_plaintext_rows_encrypted_on_startup(client):
    name, password, secret = create_user(roles={"consultant"})
    with SessionLocal() as db:
        db.execute(text("UPDATE users SET totp_secret = :s WHERE username = :u"), {"s": secret, "u": name})
        db.commit()
        assert login(client, name, password, secret).status_code == 200  # 明文仍可读
        assert encrypt_plaintext_rows(db) >= 1
        assert encrypt_plaintext_rows(db) == 0  # 可重复执行
        a = db.scalar(select(AuditLog).where(AuditLog.action == "user.totp_encrypt").order_by(AuditLog.id.desc()))
        assert a is not None and secret not in a.detail
    assert _raw(name).startswith("v1:t1:")


def test_rotation_with_rekey(client, keys):
    name, password, secret = create_user(roles={"consultant"})
    old = get_settings().totp_keys
    keys(_key("t2") + "," + old)
    assert _raw(name).startswith("v1:t1:")
    assert login(client, name, password, secret).status_code == 200  # 旧密钥仍可解密
    assert cli_main(["totp-rekey"]) == 0
    assert _raw(name).startswith("v1:t2:")
    new = get_settings().totp_keys.split(",")[0]
    keys(new)  # 去掉旧密钥
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        assert user.totp_secret == secret
        a = db.scalar(select(AuditLog).where(AuditLog.action == "user.totp_rekey").order_by(AuditLog.id.desc()))
        assert "t2" in a.detail and secret not in a.detail
    assert pyotp.TOTP(secret).now()
    keys(old + "," + new)  # 改回原密钥，免得影响其他测试的账号
    assert cli_main(["totp-rekey"]) == 0


def test_prod_requires_valid_keys():
    base = {"database_url": "postgresql+psycopg://consult:x@db:5432/consult",
            "kb_database_url": "postgresql+psycopg://consult_kb:x@db:5432/consult_kb",
            "app_env": "prod", "session_backend": "redis", "cookie_secure": True,
            "test_account_username": "", "test_account_password": "", "test_account_totp_secret": "",
            "minio_endpoint": "minio:9000", "minio_access_key": "k", "minio_secret_key": "x" * 24}
    with pytest.raises(BootstrapError, match="TOTP_KEYS"):
        check_settings(Settings(_env_file=None, **base, totp_keys=""))
    for bad in ("nokid", "k1:not-base64!", "k1:" + base64.b64encode(b"short").decode(), _key("a") + "," + _key("a")):
        with pytest.raises(BootstrapError):
            check_settings(Settings(_env_file=None, **base, totp_keys=bad))
    check_settings(Settings(_env_file=None, **base, totp_keys=_key("p1")))
