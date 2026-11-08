"""运维命令行。在后端容器里执行，例如：

    docker compose exec backend python -m app.cli create-user --username zhangsan --display-name 张三
    docker compose exec backend python -m app.cli unlock --username zhangsan
    docker compose exec backend python -m app.cli reset-totp --username zhangsan

口令交互输入，不出现在命令行参数和 shell 历史里。TOTP 密钥只显示一次，请当面交给用户录入认证器。
所有操作写入审计日志（操作人记为 cli）。
"""
from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from .core import audit, sessions
from .core.config import get_settings
from .core.db import SessionLocal
from .core.migrate import upgrade_to_head
from .core.security import PasswordPolicyError, check_password_policy, hash_password, new_totp_secret, totp_uri
from .models import User


def _ask_password() -> str:
    min_len = get_settings().password_min_length
    while True:
        pw = getpass.getpass(f"口令（至少 {min_len} 位，含大写、小写、数字、符号中的三类）：")
        try:
            check_password_policy(pw, min_len)
        except PasswordPolicyError as e:
            print(e)
            continue
        if getpass.getpass("再输入一次：") != pw:
            print("两次输入不一致")
            continue
        return pw


def _show_totp(secret: str, username: str) -> None:
    print("\n请在认证器（如 Microsoft Authenticator、Google Authenticator）中添加以下密钥，只显示这一次：")
    print(f"  密钥：{secret}")
    print(f"  或导入：{totp_uri(secret, username)}\n")


def create_user(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == args.username)):
            print(f"用户 {args.username} 已存在", file=sys.stderr)
            return 1
        secret = new_totp_secret()
        db.add(User(username=args.username, display_name=args.display_name or args.username,
                    password_hash=hash_password(_ask_password()), totp_secret=secret))
        db.commit()
        audit.record(db, actor="cli", action="user.create", result="allowed", username=args.username)
    _show_totp(secret, args.username)
    print(f"已创建用户 {args.username}")
    return 0


def _get(db, username: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        print(f"用户 {username} 不存在", file=sys.stderr)
    return user


def unlock(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        user = _get(db, args.username)
        if user is None:
            return 1
        user.failed_logins = 0
        user.locked_until = None
        db.commit()
        audit.record(db, actor="cli", action="user.unlock", result="allowed", username=args.username)
    print(f"已解锁 {args.username}")
    return 0


def reset_totp(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        user = _get(db, args.username)
        if user is None:
            return 1
        secret = new_totp_secret()
        user.totp_secret = secret
        user.totp_last_step = 0
        db.commit()
        sessions.destroy_all_for_user(user.id)
        audit.record(db, actor="cli", action="user.reset_totp", result="allowed", username=args.username)
    _show_totp(secret, args.username)
    print(f"已重置 {args.username} 的动态码，该用户的现有会话已全部失效")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="安全咨询平台运维命令")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user", help="创建本地账号（交互输入口令，生成 TOTP 密钥）")
    c.add_argument("--username", required=True)
    c.add_argument("--display-name", default="")
    c.set_defaults(func=create_user)
    u = sub.add_parser("unlock", help="解除登录失败锁定")
    u.add_argument("--username", required=True)
    u.set_defaults(func=unlock)
    r = sub.add_parser("reset-totp", help="重置动态码密钥（用户换手机时）")
    r.add_argument("--username", required=True)
    r.set_defaults(func=reset_totp)
    args = p.parse_args(argv)
    upgrade_to_head()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
