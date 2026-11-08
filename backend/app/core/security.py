"""口令与动态码。

- 口令用 Argon2id 哈希（argon2-cffi，OWASP 推荐算法），参数取库的默认值。
- 第二因素用 TOTP（pyotp，RFC 6238，30 秒一步，允许前后各一步的时钟误差）。
  已用过的时间步记在用户表里，同一个动态码不能用两次（防重放）。
"""
from __future__ import annotations

import hmac
import secrets
import time

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()
# 用户不存在时也做一次同样耗时的校验，避免通过响应时间判断用户名是否存在
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")

TOTP_INTERVAL = 30
TOTP_ISSUER = "安全咨询平台"


class PasswordPolicyError(ValueError):
    pass


def check_password_policy(password: str, min_length: int) -> None:
    if len(password) < min_length:
        raise PasswordPolicyError(f"口令至少 {min_length} 位")
    kinds = sum([any(c.islower() for c in password), any(c.isupper() for c in password),
                 any(c.isdigit() for c in password), any(not c.isalnum() for c in password)])
    if kinds < 3:
        raise PasswordPolicyError("口令须包含大写字母、小写字母、数字、符号中的至少三类")


# 生成的初始口令不含易混淆字符（0/O、1/l/I），方便管理员线下转交
_LOWER = "abcdefghijkmnpqrstuvwxyz"
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_DIGITS = "23456789"
_SYMBOLS = "#%+-=@"


def generate_initial_password(length: int = 16) -> str:
    """一次性初始口令：四类字符各至少一个，满足口令策略。用户首次登录后必须修改。"""
    pool = _LOWER + _UPPER + _DIGITS + _SYMBOLS
    chars = [secrets.choice(k) for k in (_LOWER, _UPPER, _DIGITS, _SYMBOLS)]
    chars += [secrets.choice(pool) for _ in range(max(length, 12) - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def is_valid_totp_secret(secret: str) -> bool:
    try:
        pyotp.TOTP(secret).now()
    except Exception:  # noqa: BLE001  Base32 解码失败
        return False
    return len(secret) >= 16


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER)


def match_totp_step(secret: str, code: str, now: float | None = None) -> int | None:
    """动态码正确时返回它对应的时间步，否则返回 None。调用方负责拒绝已用过的时间步。"""
    code = code.strip()
    if not (code.isdigit() and len(code) == 6):
        return None
    now = time.time() if now is None else now
    totp = pyotp.TOTP(secret, interval=TOTP_INTERVAL)
    for offset in (-1, 0, 1):
        t = now + offset * TOTP_INTERVAL
        if hmac.compare_digest(totp.at(t), code):
            return int(t // TOTP_INTERVAL)
    return None
