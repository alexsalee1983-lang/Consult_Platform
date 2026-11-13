"""运维命令行。在后端容器里执行，例如：

    docker compose exec backend python -m app.cli create-user --username zhangsan --display-name 张三
    docker compose exec backend python -m app.cli unlock --username zhangsan
    docker compose exec backend python -m app.cli reset-totp --username zhangsan
    docker compose exec backend python -m app.cli grant-role --username zhangsan --role consultant
    docker compose exec backend python -m app.cli grant-role --username wangwu --role security_auditor
    docker compose exec backend python -m app.cli list-users
    docker compose exec backend python -m app.cli add-member --project GRID-B --username zhangsan --role manager
    docker compose exec backend python -m app.cli create-project --code BANK-D --industry 银行 --tier standard --manager zhangsan

项目成员日常由项目经理在网页上调整；add-member 只用于给还没有项目经理的项目指定第一位经理，或应急。
create-project 立项（一期没有网页立项）：项目代号只用客户代号（不写客户真实名称），立项时必须定档，同时指定首位项目经理；
自动建 D1～D8 交付件（未开始）。
安全审计员由单独任命，只能用 grant-role / revoke-role 在服务器上授予或撤销，任何网页接口都做不到。
口令交互输入，不出现在命令行参数和 shell 历史里。TOTP 密钥只显示一次，请当面交给用户录入认证器。
所有操作写入审计日志（操作人记为 cli）。
"""
from __future__ import annotations

import argparse
import getpass
import re
import sys

from sqlalchemy import select

from .core import audit, sessions
from .core.config import get_settings
from .core.db import SessionLocal
from .core.migrate import upgrade_to_head
from .core.permissions import (BUSINESS_ROLES, EXCLUSIVE_ROLES, PROJECT_ROLES, ROLE_NAMES, RoleRuleError,
                               membership, roles_of, set_roles)
from .core.security import PasswordPolicyError, check_password_policy, hash_password, new_totp_secret, totp_uri
from .models import Deliverable, Project, ProjectMember, User


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


def add_member(args: argparse.Namespace) -> int:
    """把用户加入项目或调整其项目内身份（不能降级唯一的项目经理）。规则与网页成员管理一致。"""
    with SessionLocal() as db:
        project = db.scalar(select(Project).where(Project.code == args.project))
        if project is None:
            print(f"项目 {args.project} 不存在", file=sys.stderr)
            return 1
        user = _get(db, args.username)
        if user is None:
            return 1
        if not user.is_active:
            print(f"用户 {args.username} 已停用", file=sys.stderr)
            return 1
        if not roles_of(db, user.id) & BUSINESS_ROLES:
            print("只有持业务角色（顾问、项目负责人、知识专家、知识管理员）的用户能加入项目", file=sys.stderr)
            return 1
        m = membership(db, project.id, user.id)
        if m is not None and m.project_role == args.role:
            print(f"{args.username} 已是 {args.project} 的{PROJECT_ROLES[args.role]}，未变化")
            return 0
        if m is not None and m.project_role == "manager" and len([
                x for x in db.scalars(select(ProjectMember).where(ProjectMember.project_id == project.id))
                if x.project_role == "manager"]) == 1:
            print("项目至少要保留一名项目经理", file=sys.stderr)
            return 1
        if m is None:
            db.add(ProjectMember(project_id=project.id, user_id=user.id, project_role=args.role, added_by="cli"))
        else:
            m.project_role = args.role
        db.commit()
        audit.record(db, actor="cli", action="project.member", result="allowed", project_code=project.code,
                     target=args.username, project_role=args.role)
    print(f"{args.username} 现在是 {args.project} 的{PROJECT_ROLES[args.role]}")
    return 0


PROJECT_CODE = re.compile(r"^[A-Z]{2,8}-[A-Z0-9]{1,6}$")
TIERS = {"standard": "标准档", "strict": "严格档"}


def create_project(args: argparse.Namespace) -> int:
    """立项：建项目和 D1～D8 交付件，指定首位项目经理。"""
    from .seed import DELIVERABLES

    code = args.code.strip().upper()
    if not PROJECT_CODE.match(code):
        print("项目代号格式为“字母-字母或数字”，如 BANK-D、GRID-B2；只用客户代号，不写客户真实名称", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        if db.scalar(select(Project.id).where(Project.code == code)) is not None:
            print(f"项目 {code} 已存在", file=sys.stderr)
            return 1
        user = _get(db, args.manager)
        if user is None:
            return 1
        if not user.is_active or not roles_of(db, user.id) & BUSINESS_ROLES:
            print("项目经理必须是启用状态、持业务角色（顾问、项目负责人、知识专家、知识管理员）的用户", file=sys.stderr)
            return 1
        p = Project(code=code, service=args.service.strip(), industry=args.industry.strip(), tier=args.tier,
                    total_weeks=args.weeks)
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t, status="not_started")
                          for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.flush()
        db.add(ProjectMember(project_id=p.id, user_id=user.id, project_role="manager", added_by="cli"))
        db.commit()
        audit.record(db, actor="cli", action="project.create", result="allowed", project_code=code,
                     tier=args.tier, manager=args.manager)
    print(f"已立项 {code}（{TIERS[args.tier]}，{args.service}），项目经理 {args.manager}；"
          f"其他成员由项目经理在网页上添加")
    return 0


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


def kb_reembed(args: argparse.Namespace) -> int:
    from .kb import search

    with SessionLocal() as db:
        n = search.reembed_all(db, created_by="cli")
    if not n:
        print("没有排任务：向量服务未配置（EMBED_BASE_URL），或数据库不是 PostgreSQL，或没有已发布条目")
        return 1
    print(f"已为 {n} 个已发布版本排向量化任务，由 worker 执行；进度用 python -m app.cli jobs 查看")
    return 0


def _knowledge_user(db, username: str):
    from .kb import review

    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active:
        print(f"用户 {username} 不存在或已停用", file=sys.stderr)
        return None
    if not review.is_knowledge_user(db, user):
        print(f"用户 {username} 不是知识专家或知识管理员", file=sys.stderr)
        return None
    return user


def kb_import(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .kb import importer

    path = Path(args.dir)
    if not path.is_dir():
        print(f"目录不存在：{path}", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        user = _knowledge_user(db, args.as_user)
        if user is None:
            return 1
        results = importer.import_dir(db, user, path, submit=args.submit)
    label = {"created": "新建", "revised": "修订", "unchanged": "无变化", "skipped": "跳过", "error": "失败"}
    for r in results:
        where = f" {r.doc_id}" if r.doc_id else ""
        print(f"  {label.get(r.action, r.action)}  {r.file}{where}" + (f"  —— {r.message}" if r.message else ""))
    counts = {k: sum(1 for r in results if r.action == k) for k in label}
    print("合计：" + "，".join(f"{label[k]} {n}" for k, n in counts.items() if n) +
          ("；已提交审核，仍需双人审核后发布" if args.submit else "；均为草稿，在审批队列提交审核"))
    return 2 if counts["error"] else 0


def kb_draft(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .kb import drafting

    src = Path(args.source)
    if not src.is_file():
        print(f"资料文件不存在：{src}", file=sys.stderr)
        return 1
    defaults = {k: v for k, v in (("product", args.product), ("domain", args.domain)) if v}
    with SessionLocal() as db:
        user = _knowledge_user(db, args.as_user)
        if user is None:
            return 1
        try:
            report = drafting.draft(db, user, src.name, src.read_bytes(), source_detail=args.source_detail,
                                    defaults=defaults)
        except drafting.DraftError as e:
            print(f"起草失败：{e}", file=sys.stderr)
            return 2
    written = drafting.write_report(report, Path(args.out))
    print(f"调用模型 {report.calls} 次（{report.model}）。{report.note}")
    print(f"起草 {len(report.accepted)} 条，写到 {args.out}；退回 {len(report.rejected)} 条（见 {args.out}/_rejected/）")
    for p in written:
        print(f"  {p.name}")
    print("这些只是草稿文件：专家逐条修改核对后，用 kb-import 导入，再经双人审核发布。")
    return 0


def kb_eval(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from .kb import evaluate

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.as_user))
        if user is None or not user.is_active:
            print(f"用户 {args.as_user} 不存在或已停用", file=sys.stderr)
            return 1
        try:
            report = evaluate.run(db, user, evaluate.load_questions(Path(args.questions)))
        except evaluate.EvalError as e:
            print(f"题目文件有问题：{e}", file=sys.stderr)
            return 1
    print(evaluate.format_report(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写到 {args.out}")
    return 0 if report.accuracy >= 0.9 else 3


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
    am = sub.add_parser("add-member", help="把用户加入项目或调整项目内身份（给没有项目经理的项目指定首位经理、应急）")
    am.add_argument("--project", required=True, help="项目代号，如 GRID-B")
    am.add_argument("--username", required=True)
    am.add_argument("--role", required=True, choices=sorted(PROJECT_ROLES), help="manager=项目经理，member=项目成员")
    am.set_defaults(func=add_member)
    cp = sub.add_parser("create-project", help="立项：建项目和 D1～D8 交付件，指定首位项目经理（一期没有网页立项）")
    cp.add_argument("--code", required=True, help="客户代号，如 BANK-D（不写客户真实名称）")
    cp.add_argument("--tier", required=True, choices=sorted(TIERS), help="standard=标准档，strict=严格档（立项时必须定档）")
    cp.add_argument("--manager", required=True, help="首位项目经理的用户名")
    cp.add_argument("--service", default="SOC 运营体系设计", help="服务产品（默认 SOC 运营体系设计）")
    cp.add_argument("--industry", default="", help="行业，如 银行")
    cp.add_argument("--weeks", type=int, default=12, help="计划周数（默认 12）")
    cp.set_defaults(func=create_project)
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
    sub.add_parser("kb-reembed", help="更换向量模型后，重新向量化全部已发布知识").set_defaults(func=kb_reembed)
    ki = sub.add_parser("kb-import", help="批量导入知识条目文件（只收公开资料，导入为草稿）")
    ki.add_argument("dir", help="条目文件所在目录（*.md，带元数据头）")
    ki.add_argument("--as", dest="as_user", required=True, help="以哪个知识专家或知识管理员的身份导入")
    ki.add_argument("--submit", action="store_true", help="导入后一并提交审核")
    ki.set_defaults(func=kb_import)
    kd = sub.add_parser("kb-draft", help="由知识库建设 Agent 从公开资料原文起草条目文件（不直接入库）")
    kd.add_argument("--source", required=True, help="公开资料文件：.docx / .pdf / .md / .txt")
    kd.add_argument("--source-detail", required=True, help="来源说明，如“中华人民共和国网络安全法（2016）全文”")
    kd.add_argument("--out", required=True, help="起草结果写到哪个目录")
    kd.add_argument("--as", dest="as_user", required=True, help="以哪个知识专家或知识管理员的身份起草")
    kd.add_argument("--product", default="", help="默认服务产品缩写，如 SOC、GEN")
    kd.add_argument("--domain", default="", help="默认一级目录缩写，如 OPS、CMP")
    kd.set_defaults(func=kb_draft)
    ke = sub.add_parser("kb-eval", help="依据查询评测：准确率、命中率、相似度分布（准确率 ≥ 90%% 时退出码 0）")
    ke.add_argument("questions", help="评测题目文件（YAML）")
    ke.add_argument("--as", dest="as_user", required=True, help="以哪个用户的身份检索（可见范围按该用户）")
    ke.add_argument("--out", default="", help="把明细写成 JSON 文件")
    ke.set_defaults(func=kb_eval)
    args = p.parse_args(argv)
    upgrade_to_head()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
