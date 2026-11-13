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

from sqlalchemy import func, make_url, select
from sqlalchemy.orm import Session

from ..models import Project, ProjectMember, User
from . import audit, secretbox
from .auth import password_expired
from .db import utcnow
from .config import Settings
from .permissions import CONSULTANT, PROJECT_MANAGER, PROJECT_MEMBER, SYS_ADMIN, roles_of, set_roles
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
    if s.is_prod and not s.minio_endpoint:
        raise BootstrapError("生产环境必须配置对象存储（MINIO_ENDPOINT）")
    if s.minio_endpoint and (not s.minio_access_key or len(s.minio_secret_key) < 20):
        raise BootstrapError("对象存储的访问密钥未配置或过短（MINIO_ACCESS_KEY、MINIO_SECRET_KEY 至少 20 位）")
    try:
        if not secretbox.parse_keys(s.totp_keys) and s.is_prod:
            raise BootstrapError("生产环境必须配置 TOTP_KEYS（账号 TOTP 密钥加密存储，生成方法见 .env.example）")
    except secretbox.SecretBoxError as e:
        raise BootstrapError(str(e)) from e
    if s.is_prod and (not s.kb_database_url or s.kb_database_url == s.database_url):
        raise BootstrapError("生产环境知识区必须单独建库（KB_DATABASE_URL，账号与主库不同，见 deploy/postgres/README.md）")
    if s.kb_database_url and not s.kb_database_url.startswith("sqlite"):
        main, kb = make_url(s.database_url), make_url(s.kb_database_url)
        if kb.username == main.username or (kb.host, kb.port, kb.database) == (main.host, main.port, main.database):
            raise BootstrapError("知识区要用单独的数据库和单独的账号（KB_DATABASE_URL 的库名、用户名都要与主库不同）")
    if s.is_prod and s.llm_external:
        raise BootstrapError("生产环境不得使用外部模型（LLM_EXTERNAL=true 只允许开发和测试环境，决策记录 2026-11-08）")


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
        admin = User(username=s.bootstrap_admin_username, display_name=s.bootstrap_admin_username,
                     password_hash=hash_password(s.bootstrap_admin_password),
                     totp_secret=s.bootstrap_admin_totp_secret)
        db.add(admin)
        db.commit()
        set_roles(db, admin, {SYS_ADMIN}, granted_by="system")
        audit.record(db, actor="system", action="user.bootstrap", result="allowed",
                     username=s.bootstrap_admin_username, role=SYS_ADMIN)
        log.warning("已创建第一个管理员 %s，请从 .env 删除 BOOTSTRAP_ADMIN_PASSWORD", s.bootstrap_admin_username)

    if s.test_account_username or s.test_account_password:
        if s.is_prod:  # check_settings 已拦截，这里再防一次
            raise BootstrapError("生产环境不得配置测试账号")
        _validate(s, "TEST_ACCOUNT", s.test_account_username, s.test_account_password, s.test_account_totp_secret)
        user = db.scalar(select(User).where(User.username == s.test_account_username))
        if user is None:
            user = User(username=s.test_account_username, display_name="测试账号",
                        password_hash=hash_password(s.test_account_password),
                        totp_secret=s.test_account_totp_secret)
            db.add(user)
            db.commit()
            set_roles(db, user, {CONSULTANT}, granted_by="system")
            audit.record(db, actor="system", action="user.test_account", result="allowed",
                         username=s.test_account_username, role=CONSULTANT)
        elif not verify_password(user.password_hash, s.test_account_password) or \
                user.totp_secret != s.test_account_totp_secret:
            # 测试机改了 .env 里的测试账号口令或密钥时同步过来
            user.set_password_hash(hash_password(s.test_account_password))
            user.totp_secret = s.test_account_totp_secret
            user.totp_last_step = 0
            db.commit()
        if password_expired(user):  # 测试账号口令由 .env 管，不让有效期卡住冒烟测试
            user.password_changed_at = utcnow()
            db.commit()
        if not roles_of(db, user.id):  # 登录 PR 时期建的测试账号还没有角色
            set_roles(db, user, {CONSULTANT}, granted_by="system")


def encrypt_plaintext_rows(db: Session) -> int:
    """把加密前存的明文 TOTP 密钥加密（启动时执行，可重复执行）；返回处理的账号数，不记密钥。"""
    if not secretbox.keys():
        return 0
    n = 0
    for user in db.scalars(select(User).where(~User.totp_secret_enc.startswith(secretbox.PREFIX + ":"))).all():
        user.totp_secret = user.totp_secret_enc
        n += 1
    if n:
        db.commit()
        audit.record(db, actor="system", action="user.totp_encrypt", result="allowed", count=n)
        log.warning("已加密 %d 个账号的 TOTP 密钥（原为明文）", n)
    return n


# 测试账号在演示项目中的身份：BANK-A 项目经理、MFG-C 项目成员，故意不加入 GRID-B，
# 冒烟测试用它验证“非项目成员访问返回 404”。
TEST_MEMBERSHIPS = {"BANK-A": PROJECT_MANAGER, "MFG-C": PROJECT_MEMBER}


def ensure_test_memberships(db: Session, s: Settings) -> None:
    if s.is_prod or not s.test_account_username:
        return
    user = db.scalar(select(User).where(User.username == s.test_account_username))
    if user is None:
        return
    for code, role in TEST_MEMBERSHIPS.items():
        project = db.scalar(select(Project).where(Project.code == code))
        if project is not None and db.get(ProjectMember, (project.id, user.id)) is None:
            db.add(ProjectMember(project_id=project.id, user_id=user.id, project_role=role, added_by="system"))
    db.commit()
