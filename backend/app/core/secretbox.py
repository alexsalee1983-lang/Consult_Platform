"""账号凭据的字段级加密（等保自查 ②b）：目前只用于 users.totp_secret（TOTP 共享密钥）。

- 算法 AES-256-GCM，每次加密随机 12 字节 nonce；附加数据固定为字段名，密文挪到别的字段解不开。
- 存储格式：``v1:<密钥编号>:<base64(nonce + 密文 + 认证标签)>``。
- 密钥来自 .env 的 ``TOTP_KEYS``：``编号:base64(32 字节)``，多把用逗号分隔，第一把用于加密，其余只用于解密（轮换期）。
  轮换：新密钥放第一位、旧密钥保留在后面 → ``python -m app.cli totp-rekey`` 全部改用新密钥加密 → 删掉旧密钥。
- 没有前缀的值是加密前的明文：仍可读取，启动时（``encrypt_plaintext_rows``）和 ``totp-rekey`` 会加密。
- 生产环境必须配置 ``TOTP_KEYS``（bootstrap.check_settings）；开发、测试环境不配置时按明文存（只为本地方便）。

密钥、明文和密文都不进日志和审计。
"""
import base64
import binascii
import re
import secrets
from functools import lru_cache

from Crypto.Cipher import AES

from .config import get_settings

PREFIX = "v1"
_AAD = b"users.totp_secret"
_KID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class SecretBoxError(ValueError):
    pass


@lru_cache
def parse_keys(spec: str) -> tuple[tuple[str, bytes], ...]:
    keys: list[tuple[str, bytes]] = []
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        kid, sep, b64 = part.partition(":")
        if not sep or not _KID.match(kid):
            raise SecretBoxError("TOTP_KEYS 格式应为 编号:base64密钥（编号 1～16 位字母、数字、-、_）")
        try:
            key = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise SecretBoxError(f"TOTP_KEYS 中密钥 {kid} 不是有效的 base64") from e
        if len(key) != 32:
            raise SecretBoxError(f"TOTP_KEYS 中密钥 {kid} 应为 32 字节")
        if any(k == kid for k, _ in keys):
            raise SecretBoxError(f"TOTP_KEYS 中密钥编号 {kid} 重复")
        keys.append((kid, key))
    return tuple(keys)


def keys() -> tuple[tuple[str, bytes], ...]:
    return parse_keys(get_settings().totp_keys)


def check_config() -> None:
    """启动时检查 TOTP_KEYS 格式（不打印密钥）。"""
    keys()


def is_encrypted(stored: str) -> bool:
    return stored.startswith(PREFIX + ":")


def current_kid(stored: str) -> str | None:
    return stored.split(":", 2)[1] if is_encrypted(stored) and stored.count(":") >= 2 else None


def encrypt(plain: str) -> str:
    ks = keys()
    if not ks:
        return plain  # 开发、测试环境未配置密钥（生产环境 check_settings 已拦截）
    kid, key = ks[0]
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(_AAD)
    ct, tag = cipher.encrypt_and_digest(plain.encode())
    return f"{PREFIX}:{kid}:{base64.b64encode(nonce + ct + tag).decode()}"


def decrypt(stored: str) -> str:
    if not is_encrypted(stored):
        return stored  # 加密前的明文
    try:
        _, kid, b64 = stored.split(":", 2)
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, binascii.Error) as e:
        raise SecretBoxError("TOTP 密钥密文格式错误") from e
    key = dict(keys()).get(kid)
    if key is None:
        raise SecretBoxError(f"没有编号为 {kid} 的 TOTP_KEYS 密钥，无法解密（轮换时旧密钥要保留到 totp-rekey 完成）")
    if len(raw) < 12 + 16:
        raise SecretBoxError("TOTP 密钥密文长度错误")
    cipher = AES.new(key, AES.MODE_GCM, nonce=raw[:12])
    cipher.update(_AAD)
    try:
        return cipher.decrypt_and_verify(raw[12:-16], raw[-16:]).decode()
    except ValueError as e:
        raise SecretBoxError("TOTP 密钥密文校验失败（密钥不对或数据被改动）") from e


def needs_rekey(stored: str) -> bool:
    """明文，或者不是用当前第一把密钥加密的。"""
    ks = keys()
    if not ks:
        return False
    return current_kid(stored) != ks[0][0]
