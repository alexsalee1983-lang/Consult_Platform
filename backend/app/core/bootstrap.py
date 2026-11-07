"""启动检查与初始账号。

- check_settings：生产环境不允许内存会话、不允许配置测试账号，否则拒绝启动。
- 第一个管理员：库里没有任何用户、且 .env 配置了 BOOTSTRAP_ADMIN_* 时创建一次；
  之后再启动不会重复创建或修改。也可以用命令行：python -m app.cli create-user。
  系统管理员角色在“角色与项目隔离”合入后授予（见 docs/开发计划.md S1）。
- 测试账号：只在 APP_ENV=dev / test 时按 TEST_ACCOUNT_* 创建或同步，供冒烟测试使用。
代码里没有任何默认账号或默认口令。
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import User
from . import audit
from .config import Settings
from .security import check_password_policy, hash_password, is_valid_totp_secret, verify_password

log = logging.getLogger(__name__)


class BootstrapError(RuntimeError):
    pass


def check_settings(s: Settings) -> None:
    if s.app_env not in {"dev", "test", "prod"}:
        raise BootstrapError(f"APP_ENV 只能是 dev / test / prod，当前为 {s.app_env!r}")
    if s.is_prod and s.session_backend != "redis":
        raise BootstrapError("生产环境会话必须存 Redis（SESSION_BACKEND=redis）")
    if s.is_prod and (s.test_account_username or s.test_account_password):
        raise BootstrapError("生产环境不得配置测试账号（TEST_ACCOUNT_*），请从 .env 删除")
    if s.is_prod and not s.cookie_secure:
        raise BootstrapError("生产环境必须开启 COOKIE_SECURE（只经 HTTPS 发送会话 Cookie）")


def _validate(s: Settings, label: str, username: str, password: str, totp_secret: str) -> None:
    if not (username and password and totp_secret):
        raise BootstrapError(f"{label} 的用户名、口令、TOTP 密钥必须同时配置")
    try:
        check_password_policy(password, s.password_min_length)
    except ValueError as e:
        raise BootstrapError(f"{label} 的口令不符合要求：{e}") from e
    if not is_valid_totp_secret(totp_secret):
        raise BootstrapError(f"{label} 的 TOTP 密钥不是有效的 Base32（至少 16 位）")


def ensure_initial_users(db: Session, s: Settings) -> None:
    if (s.bootstrap_admin_username or s.bootstrap_admin_password) and db.scalar(select(func.count(User.id))) == 0:
        _validate(s, "BOOTSTRAP_ADMIN", s.bootstrap_admin_username, s.bootstrap_admin_password,
                  s.bootstrap_admin_totp_secret)
        db.add(User(username=s.bootstrap_admin_username, display_name=s.bootstrap_admin_username,
                    password_hash=hash_password(s.bootstrap_admin_password),
                    totp_secret=s.bootstrap_admin_totp_secret))
        db.commit()
        audit.record(db, actor="system", action="user.bootstrap", result="allowed",
                     username=s.bootstrap_admin_username)
        log.warning("已创建第一个管理员 %s，请从 .env 删除 BOOTSTRAP_ADMIN_PASSWORD", s.bootstrap_admin_username)

    if s.test_account_username or s.test_account_password:
        if s.is_prod:  # check_settings 已拦截，这里再防一次
            raise BootstrapError("生产环境不得配置测试账号")
        _validate(s, "TEST_ACCOUNT", s.test_account_username, s.test_account_password, s.test_account_totp_secret)
        user = db.scalar(select(User).where(User.username == s.test_account_username))
        if user is None:
            db.add(User(username=s.test_account_username, display_name="测试账号",
                        password_hash=hash_password(s.test_account_password),
                        totp_secret=s.test_account_totp_secret))
            db.commit()
            audit.record(db, actor="system", action="user.test_account", result="allowed",
                         username=s.test_account_username)
        elif not verify_password(user.password_hash, s.test_account_password) or \
                user.totp_secret != s.test_account_totp_secret:
            # 测试机改了 .env 里的测试账号口令或密钥时同步过来
            user.password_hash = hash_password(s.test_account_password)
            user.totp_secret = s.test_account_totp_secret
            user.totp_last_step = 0
            db.commit()
