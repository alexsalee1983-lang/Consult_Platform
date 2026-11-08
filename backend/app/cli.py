"""运维命令行。在后端容器里执行，例如：

    docker compose exec backend python -m app.cli create-user --username zhangsan --display-name 张三
    docker compose exec backend python -m app.cli unlock --username zhangsan
    docker compose exec backend python -m app.cli reset-totp --username zhangsan
    docker compose exec backend python -m app.cli grant-role --username zhangsan --role consultant
    docker compose exec backend python -m app.cli grant-role --username wangwu --role security_auditor
    docker compose exec backend python -m app.cli list-users

安全审计员由单独任命，只能用 grant-role / revoke-role 在服务器上授予或撤销，任何网页接口都做不到。
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
from .core.permissions import EXCLUSIVE_ROLES, ROLE_NAMES, RoleRuleError, roles_of, set_roles
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


def _change_role(args: argparse.Namespace, grant: bool) -> int:
    with SessionLocal() as db:
        user = _get(db, args.username)
        if user is None:
            return 1
        current = roles_of(db, user.id)
        target = current | {args.role} if grant else current - {args.role}
        if target == current:
            print(f"{args.username} 的角色未变化：{_names(current)}")
            return 0
        try:
            set_roles(db, user, target, granted_by="cli")
        except RoleRuleError as e:
            print(e, file=sys.stderr)
            return 1
        if args.role in EXCLUSIVE_ROLES:  # 三权分立角色变化后，旧会话作废，重新登录才生效
            sessions.destroy_all_for_user(user.id)
        audit.record(db, actor="cli", action="user.role_grant" if grant else "user.role_revoke", result="allowed",
                     target=args.username, role=args.role)
        print(f"{args.username} 现在的角色：{_names(target)}")
    return 0


def _names(roles) -> str:
    return "、".join(ROLE_NAMES[r] for r in sorted(roles)) or "（无）"


def grant_role(args: argparse.Namespace) -> int:
    return _change_role(args, True)


def revoke_role(args: argparse.Namespace) -> int:
    return _change_role(args, False)


def list_users(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        for u in db.scalars(select(User).order_by(User.username)):
            state = "启用" if u.is_active else "停用"
            print(f"{u.username:<20} {u.display_name:<12} {state}  {_names(roles_of(db, u.id))}")
    return 0


def verify_audit(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        r = audit.verify_chain(db, args.expect_head)
        audit.record(db, actor="cli", action="audit.verify", result="allowed" if r.ok else "error",
                     checked=r.checked, head_id=r.head_id, error_id=r.error_id)
    print(f"结果：{'完整' if r.ok else '发现问题'}")
    print(f"链上记录：{r.checked} 条；链前旧记录：{r.legacy} 条")
    print(f"链头：id={r.head_id}  hash={r.head_hash}")
    if not r.ok:
        print(f"问题：{r.error}" + (f"（id={r.error_id}）" if r.error_id else ""))
    return 0 if r.ok else 2


def usage_report(args: argparse.Namespace) -> int:
    from .services import usage

    group = tuple(g.strip() for g in args.by.split(",") if g.strip())
    if not group or set(group) - {"username", "project_code", "model"}:
        print("--by 只能是 username、project_code、model 的组合", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        rows = usage.summary(db, args.days, group)
    print(f"最近 {args.days} 天大模型用量（按 {'、'.join(group)}）：")
    for r in rows:
        keys = "  ".join(f"{r[g] or '-':<16}" for g in group)
        print(f"{keys}  调用 {r['calls']:>5}  输入 {r['prompt_tokens']:>9}  输出 {r['completion_tokens']:>9}  "
              f"失败 {r['errors']:>3}  拦截 {r['blocked']:>3}")
    return 0


def jobs_status(args: argparse.Namespace) -> int:
    from .models import Job
    from .services import jobs

    with SessionLocal() as db:
        summary = jobs.summary(db)
        failed = db.scalars(select(Job).where(Job.status == "failed").order_by(Job.id.desc()).limit(10)).all()
    print("后台任务（种类：状态 数量）：")
    for kind, counts in sorted(summary.items()):
        print(f"  {kind}：" + "  ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if not summary:
        print("  （暂无任务）")
    for j in failed:
        print(f"  失败 #{j.id} {j.kind} 尝试 {j.attempts} 次：{j.last_error}")
    return 0


def selfcheck(args: argparse.Namespace) -> int:
    import time

    from .models import Job
    from .services import jobs

    with SessionLocal() as db:
        job_id = jobs.enqueue(db, "system.selfcheck", created_by="cli", max_attempts=1)
    print(f"已排自检任务 #{job_id}，等待 worker 执行（最多 {args.wait} 秒）……")
    deadline = time.time() + args.wait
    while time.time() < deadline:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job.status in ("done", "failed"):
                print(f"任务 #{job_id}：{job.status}" + (f"（{job.last_error}）" if job.last_error else ""))
                return 0 if job.status == "done" else 2
        time.sleep(1)
    print("超时：worker 没有执行，检查 docker compose ps 和 docker compose logs worker", file=sys.stderr)
    return 3


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
    role_help = "，".join(f"{k}={v}" for k, v in ROLE_NAMES.items())
    g = sub.add_parser("grant-role", help="授予系统角色（安全审计员只能用此命令授予）")
    g.add_argument("--username", required=True)
    g.add_argument("--role", required=True, choices=sorted(ROLE_NAMES), help=role_help)
    g.set_defaults(func=grant_role)
    v = sub.add_parser("revoke-role", help="撤销系统角色")
    v.add_argument("--username", required=True)
    v.add_argument("--role", required=True, choices=sorted(ROLE_NAMES), help=role_help)
    v.set_defaults(func=revoke_role)
    sub.add_parser("list-users", help="列出用户和角色").set_defaults(func=list_users)
    a = sub.add_parser("verify-audit", help="校验审计哈希链（发现问题时退出码为 2）")
    a.add_argument("--expect-head", default=None, help="之前记下的链头哈希，校验它仍在链上")
    a.set_defaults(func=verify_audit)
    us = sub.add_parser("usage-report", help="大模型用量汇总")
    us.add_argument("--days", type=int, default=30)
    us.add_argument("--by", default="username,project_code,model",
                    help="汇总维度，逗号分隔：username / project_code / model")
    us.set_defaults(func=usage_report)
    sub.add_parser("jobs", help="后台任务状态").set_defaults(func=jobs_status)
    sc = sub.add_parser("selfcheck", help="排一个 worker 自检任务并等待结果（成功退出码 0）")
    sc.add_argument("--wait", type=int, default=60)
    sc.set_defaults(func=selfcheck)
    args = p.parse_args(argv)
    upgrade_to_head()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
