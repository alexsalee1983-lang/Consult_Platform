"""运维命令行。在后端容器里执行，例如：

    docker compose exec backend python -m app.cli create-user --username zhangsan --display-name 张三
    docker compose exec backend python -m app.cli unlock --username zhangsan
    docker compose exec backend python -m app.cli reset-totp --username zhangsan
    docker compose exec backend python -m app.cli totp-rekey      # 轮换 TOTP_KEYS 后重新加密全部账号的 TOTP 密钥
    docker compose exec backend python -m app.cli grant-role --username zhangsan --role consultant
    docker compose exec backend python -m app.cli grant-role --username wangwu --role security_auditor
    docker compose exec backend python -m app.cli list-users
    docker compose exec backend python -m app.cli add-member --project GRID-B --username zhangsan --role manager
    docker compose exec backend python -m app.cli create-project --code BANK-D --industry 银行 --tier standard --manager zhangsan
    docker compose exec backend python -m app.cli retention-report
    docker compose exec backend python -m app.cli deletion-scan
    docker compose exec backend python -m app.cli deletion-report [--all]

项目成员日常由项目经理在网页上调整；add-member 只用于给还没有项目经理的项目指定第一位经理，或应急。
create-project 立项（一期没有网页立项）：项目代号只用客户代号（不写客户真实名称），立项时必须定档，同时指定首位项目经理；
自动建 D1～D8 交付件（未开始）。
retention-report 列出已验收项目的保留期和到期状态（只有代号和日期，不含客户内容），供负责人跟踪到期删除。
loadtest-accounts 只在开发、测试环境建压测账号（凭据写文件，见 loadtest/README.md）。
deletion-scan 立即为已到期项目发起删除申请（worker 每小时自动做）；deletion-report 列出删除申请和删除记录。
删除本身只能由项目负责人在网页上确认（规则见 app/project/deletion.py），命令行不提供直接删除。
安全审计员由单独任命，只能用 grant-role / revoke-role 在服务器上授予或撤销，任何网页接口都做不到。
口令交互输入，不出现在命令行参数和 shell 历史里。TOTP 密钥只显示一次，请当面交给用户录入认证器。
所有操作写入审计日志（操作人记为 cli）。
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys

from sqlalchemy import select

from .core import audit, secretbox, sessions
from .core.config import get_settings
from .core.db import SessionLocal
from .core.migrate import KbSplitPending, upgrade_to_head
from .core.permissions import (BUSINESS_ROLES, CONSULTANT, EXCLUSIVE_ROLES, PROJECT_ROLES, ROLE_NAMES,
                               RoleRuleError, membership, roles_of, set_roles)
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


def totp_rekey(args: argparse.Namespace) -> int:
    """用 TOTP_KEYS 的第一把密钥重新加密全部账号的 TOTP 密钥（含加密前的明文）。不打印密钥。"""
    if not secretbox.keys():
        print("没有配置 TOTP_KEYS，无法加密", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        users = db.scalars(select(User)).all()
        todo = [u for u in users if secretbox.needs_rekey(u.totp_secret_enc)]
        failed = 0
        for user in todo:
            try:
                user.totp_secret = user.totp_secret
            except secretbox.SecretBoxError as e:
                failed += 1
                print(f"{user.username}：{e}", file=sys.stderr)
        db.commit()
        audit.record(db, actor="cli", action="user.totp_rekey", result="allowed" if not failed else "error",
                     count=len(todo) - failed, failed=failed, key_id=secretbox.keys()[0][0])
    print(f"已用密钥 {secretbox.keys()[0][0]} 重新加密 {len(todo) - failed} 个账号（共 {len(users)} 个）"
          + (f"，{failed} 个失败" if failed else ""))
    return 0 if not failed else 2


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
        if project.deleted_at is not None:
            print(f"项目 {args.project} 的资料已删除，不能再加成员", file=sys.stderr)
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


PROJECT_CODE = re.compile(r"^[A-Z]{2,8}-[A-Z0-9]{1,6}$")   # 同 app/project/configs.py
TIERS = {"standard": "标准档", "strict": "严格档"}


def create_project(args: argparse.Namespace) -> int:
    """立项：选产品和模块、定档，按产品配置建交付件、记下配置快照，指定首位项目经理（规则在 app/project/configs.py）。"""
    from .project import configs

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.manager))
        try:
            p = configs.create(db, actor="cli", code=args.code, tier=args.tier, manager=user, product=args.product,
                               modules=args.module or [], industry=args.industry, weeks=args.weeks, org_type=args.org_type,
                               service=args.service)
        except configs.CreateError as e:
            print(str(e) if user is not None else f"没有用户 {args.manager}", file=sys.stderr)
            return 1
        codes = "、".join(d.code for d in p.deliverables)
        print(f"已立项 {p.code}（{TIERS[p.tier]}，{p.service}），交付件 {codes}，项目经理 {args.manager}；"
              f"其他成员由项目经理在网页上添加")
    return 0


LOADTEST_PREFIX = "lt-"


def loadtest_accounts(args: argparse.Namespace) -> int:
    """压测账号（只在 APP_ENV=dev / test）：建或重置 lt-01… 顾问账号并加入压测项目，凭据写进 --out 文件（权限 600）。

    凭据只在这个文件里，不打印到终端；压测完用 --disable 停用账号、删掉文件。账号和项目都是虚拟的。
    """
    import json
    import os
    import secrets

    if get_settings().app_env not in ("dev", "test"):
        print("压测账号只能在开发和测试环境（APP_ENV=dev / test）创建", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        users = list(db.scalars(select(User).where(User.username.startswith(LOADTEST_PREFIX))))
        if args.disable:
            for u in users:
                u.is_active = False
            db.commit()
            for u in users:
                sessions.destroy_all_for_user(u.id)
            audit.record(db, actor="cli", action="loadtest.accounts", result="allowed", op="disable",
                         count=len(users))
            print(f"已停用 {len(users)} 个压测账号")
            return 0
        if not 1 <= args.count <= 50:
            print("--count 取 1～50", file=sys.stderr)
            return 1
        if not args.out:
            print("要用 --out 指定凭据文件", file=sys.stderr)
            return 1
        code = args.project.strip().upper()
        if not PROJECT_CODE.match(code):
            print("项目代号格式为“字母-字母或数字”，如 LOAD-A", file=sys.stderr)
            return 1
        creds = []
        for i in range(1, args.count + 1):
            name = f"{LOADTEST_PREFIX}{i:02d}"
            password = secrets.token_urlsafe(18) + "aA1!"
            secret = new_totp_secret()
            u = db.scalar(select(User).where(User.username == name))
            if u is None:
                u = User(username=name, display_name=f"压测{i:02d}", password_hash="", totp_secret=secret)
                db.add(u)
            u.set_password_hash(hash_password(password))
            u.totp_secret = secret
            u.totp_last_step = 0
            u.is_active = True
            u.must_change_password = False
            u.failed_logins = 0
            u.locked_until = None
            db.flush()
            set_roles(db, u, {CONSULTANT}, granted_by="cli")
            creds.append({"username": name, "password": password, "totp_secret": secret})
        db.commit()
        manager = db.scalar(select(User).where(User.username == creds[0]["username"]))
        project = db.scalar(select(Project).where(Project.code == code))
        if project is None:
            from .seed import DELIVERABLES
            project = Project(code=code, service="压测", industry="制造", tier="standard", total_weeks=8)
            project.deliverables = [Deliverable(code=f"D{i + 1}", title=t, status="not_started")
                                    for i, t in enumerate(DELIVERABLES)]
            db.add(project)
            db.flush()
            db.add(ProjectMember(project_id=project.id, user_id=manager.id, project_role="manager",
                                 added_by="cli"))
        elif project.deleted_at is not None or project.tier != "standard":
            print(f"项目 {code} 已删除或不是标准档，换一个压测项目代号", file=sys.stderr)
            return 1
        for c in creds[1:]:
            u = db.scalar(select(User).where(User.username == c["username"]))
            if membership(db, project.id, u.id) is None:
                db.add(ProjectMember(project_id=project.id, user_id=u.id, project_role="member", added_by="cli"))
        db.commit()
        # 压测写章节、导出用 D2（交付链不写 D1、D2，互不干扰；二期 A5 起交付链写 D3～D8）：没有大纲时建 n 章的大纲，每个虚拟用户写一章
        from .project import deliverables as dsvc
        d2 = next(d for d in project.deliverables if d.code == "D2")
        if not d2.sections:
            dsvc.set_outline(db, manager, project, d2, [f"压测章节 {i:02d}" for i in range(1, len(creds) + 1)])
        audit.record(db, actor="cli", action="loadtest.accounts", result="allowed", op="create",
                     count=len(creds), project_code=code)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"project": code, "users": creds}, f, ensure_ascii=False)
    print(f"已准备 {len(creds)} 个压测账号（项目 {code}），凭据写入 {args.out}；压测完执行 --disable 并删除该文件")
    return 0


def list_users(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        for u in db.scalars(select(User).order_by(User.username)):
            state = "启用" if u.is_active else "停用"
            print(f"{u.username:<20} {u.display_name:<12} {state}  {_names(roles_of(db, u.id))}")
    return 0


def retention_report(args: argparse.Namespace) -> int:
    from .project import retention
    names = {retention.ACTIVE: "未验收", retention.RETAINED: "保留中", retention.DUE_SOON: "即将到期",
             retention.DUE: "已到期"}
    with SessionLocal() as db:
        rows = [(p, retention.status(p)) for p in db.scalars(select(Project).order_by(Project.deletion_due_on,
                                                                                         Project.code))]
    rows = [(p, st) for p, st in rows if args.all or st.state != retention.ACTIVE]
    if not rows:
        print("没有已验收的项目")
        return 0
    for p, st in rows:
        due = p.deletion_due_on.isoformat() if p.deletion_due_on else "-"
        acc = p.accepted_on.isoformat() if p.accepted_on else "-"
        left = f"{st.days_left} 天" if st.days_left is not None else "-"
        state = "已删除" if p.deleted_at is not None else names[st.state]
        print(f"{p.code:<12} {p.tier:<8} 验收 {acc}  保留 {p.retention_months} 个月  到期 {due}  "
              f"剩余 {left:<8} {state}")
    return 0


def kb_recheck_scan(args: argparse.Namespace) -> int:
    """立即执行一次使用反馈扫描（worker 每天自动执行）：顾问常删掉 Agent 引用的知识条目标为待复查（二期 B13）。"""
    from .kb import recheck
    with SessionLocal() as db:
        made = recheck.scan(db, actor="cli")
    print(f"标记待复查 {len(made)} 条" + ("：" + "、".join(made) if made else ""))
    return 0


def kb_candidate_promote(args: argparse.Namespace) -> int:
    """候选影子期晋级（三期 B24①，worker 每天自动执行）：满 KB_CANDIDATE_DAYS 天且抽检没判不通过的候选转为已发布，
    抽检不通过的退回人工审核。--dry-run 只列出、不改。"""
    from .kb import candidate
    with SessionLocal() as db:
        r = candidate.promote_due(db, actor="cli", dry_run=args.dry_run)
    head = "（只列出，未改动）" if args.dry_run else ""
    for key, label in (("promoted", "晋级"), ("returned", "退回人工审核"), ("superseded", "已被新版本替代"),
                       ("waiting", "继续候选")):
        items = getattr(r, key)
        print(f"{label}{head} {len(items)} 条" + ("：" + "、".join(items[:50]) if items else ""))
    return 0


def kb_agent_review_rerun(args: argparse.Namespace) -> int:
    """积压重跑（四期试用缺陷，负责人 2026-11-25 同意）：待审版本里，规则预判上线前知识审核 Agent 判
    “不重复、不冲突”没过的，各排一次 kb.agent_review（rerun）。--dry-run 只按规则预判类别打印计数，不排队。
    自动发布照常受抽检和 KB_AGENT_AUTO_PUBLISH / 抽检不通过率暂停约束。"""
    from .kb import agent_review, conflict_rules
    from .models import KbVersion
    from .services import jobs as _jobs

    with SessionLocal() as db:
        due = [v for v in db.scalars(select(KbVersion).where(KbVersion.status == "in_review").order_by(KbVersion.id))
               if (agent_review.eligible(v) or agent_review.advisory(v)) and conflict_rules.rerun_due(v)]
        if args.limit:
            due = due[:args.limit]
        if args.dry_run:
            verdicts: dict[str, int] = {}
            kinds: dict[str, int] = {}
            for v in due:
                pre = conflict_rules.prejudge(db, v)
                verdicts[pre.verdict] = verdicts.get(pre.verdict, 0) + 1
                for r in pre.relations:
                    kinds[r.kind] = kinds.get(r.kind, 0) + 1
            print(f"待重跑 {len(due)} 条（只统计，未排队）")
            print("按结论：" + ("、".join(f"{k} {n}" for k, n in sorted(verdicts.items())) or "无"))
            print("按相近条目关系：" + ("、".join(f"{k} {n}" for k, n in sorted(kinds.items())) or "无"))
            return 0
        queued = [_jobs.enqueue(db, "kb.agent_review", {"version_id": v.id, "rerun": True}, created_by="cli",
                                max_attempts=3) for v in due]
    print(f"已排队重跑 {len(queued)} 条")
    return 0


def deletion_scan(args: argparse.Namespace) -> int:
    """立即执行一次到期扫描（worker 每小时自动执行），为已到期的项目发起删除申请。"""
    from .project import deletion
    with SessionLocal() as db:
        made = deletion.scan_due(db)
        print(f"发起到期删除申请 {len(made)} 个" + ("：" + "、".join(d.project.code for d in made) if made else ""))
    return 0


def deletion_report(args: argparse.Namespace) -> int:
    """删除申请与删除记录（只有代号、日期、计数和回流情况）。"""
    from .project import deletion
    names = {"pending": "待确认", "confirmed": "执行中", "done": "已删除", "cancelled": "已撤销"}
    if args.monthly:
        from .services import ops
        with SessionLocal() as db:
            months = ops.monthly_deletions(db)
        if not months:
            print("没有删除申请")
        for m in months:
            print(f"{m['month']}  发起 {m['requested']:>3}  已删除 {m['done']:>3}（按期 {m['on_time']}）  "
                  f"撤销 {m['cancelled']:>3}  未完成 {m['open']:>3}（逾期 {m['overdue']}）")
        return 0
    with SessionLocal() as db:
        rows = db.scalars(select(deletion.ProjectDeletion).order_by(deletion.ProjectDeletion.id)).all()
        if not args.all:
            rows = [d for d in rows if d.status in deletion.OPEN]
        if not rows:
            print("没有删除申请" if args.all else "没有未完成的删除申请（--all 含已完成）")
            return 0
        for d in rows:
            done = d.completed_at.date().isoformat() if d.completed_at else "-"
            print(f"#{d.id:<4} {d.project.code:<12} {deletion.KIND_NAMES[d.kind]:<12} {names[d.status]:<4} "
                  f"应完成 {d.due_by.isoformat()}  确认 {d.confirmed_by or '-'}  完成 {done}  "
                  f"回流 {deletion.FLOWBACK.get(d.flowback, '-')}")
    return 0


def storage_provision(args: argparse.Namespace) -> int:
    """三期 G1 最小权限：运维用对象存储根账号建桶、加密、建应用账号、挂只限两个桶的策略（可重复执行）。
    根账号口令只从本次命令的环境变量 MINIO_ROOT_PASSWORD 读，不写进平台配置；审计不记口令。"""
    from .services import storage

    s = get_settings()
    root_user, root_password = os.environ.get("MINIO_ROOT_USER", ""), os.environ.get("MINIO_ROOT_PASSWORD", "")
    if not s.minio_endpoint or not root_user or not root_password:
        print("要配置 MINIO_ENDPOINT，并在本次命令的环境变量里给出 MINIO_ROOT_USER、MINIO_ROOT_PASSWORD"
              "（docker compose run --rm -e MINIO_ROOT_PASSWORD=... backend python -m app.cli storage-provision）",
              file=sys.stderr)
        return 1
    try:
        r = storage.provision(root_user, s.minio_access_key, s.minio_secret_key, root_password=root_password)
    except storage.StorageError as e:
        print(str(e), file=sys.stderr)
        return 1
    with SessionLocal() as db:
        audit.record(db, actor="cli", action="storage.provision", result="allowed", account=r.account,
                     policy=r.policy, created_buckets=len(r.created_buckets))
    print(f"应用账号 {r.account} 已挂策略 {r.policy}；新建桶 {', '.join(r.created_buckets) or '无'}")
    return 0


def indicators_cmd(args: argparse.Namespace) -> int:
    """指标看板（三期 F2①）；--v4 只出 v4.0 验收口径一页汇总（四期 F2③）。只有数字，不出项目代号和正文。"""
    import json
    from datetime import datetime, timedelta, timezone

    from .services import indicators, indicators_v4

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    with SessionLocal() as db:
        if args.v4:
            print(indicators_v4.render(indicators_v4.summary(db, start, end)))
        else:
            print(json.dumps(indicators.compute(db, start, end), ensure_ascii=False, indent=2))
    return 0


def backup_record(args: argparse.Namespace) -> int:
    """备份、恢复脚本（scripts/backup.sh、restore.sh）完成后记一条审计：只有时间戳、链头编号和字节数。"""
    if not re.fullmatch(r"\d{8}T\d{6}Z", args.stamp):
        print("--stamp 格式为 20270110T020000Z", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        audit.record(db, actor="cli", action=f"backup.{args.op}", result="allowed", stamp=args.stamp,
                     head_id=args.head_id, bytes=args.bytes)
    print(f"已记录 backup.{args.op} {args.stamp}")
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


def quality_fp_stats(args: argparse.Namespace) -> int:  # noqa: ARG001
    """四期 A18②：各类阻断的驳回率（误报率）和是否已自动降为提醒。"""
    from .project import contradictions

    with SessionLocal() as db:
        rows = contradictions.false_positive_stats(db)
    print(f"阻断误报率（样本不少于 {contradictions.CALIBRATE_MIN} 条且驳回率不低于 "
          f"{contradictions.CALIBRATE_RATE:.0%} 的类别自动降为提醒；机密与客户信息、资质用语不降）：")
    for r in rows:
        mark = "已降为提醒" if r["calibrated"] else ""
        print(f"{r['by']:<6} {r['category']:<10} 阻断 {r['total']:>5}  驳回 {r['waived']:>5}  驳回率 {r['rate']:.1%}  {mark}")
    return 0


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


def kb_source_split(args: argparse.Namespace) -> int:
    """依据原文切条（二期 B7）：按下载清单把公开资料机械切成 SRC 条目文件，不入库、不调模型。"""
    from pathlib import Path

    from .kb import batches, sources

    r = sources.split_all(Path(args.manifest), Path(args.dir), Path(args.out))
    total = 0
    for d in r.docs:
        total += d.entries
        extra = f"，退回 {d.rejected} 条（见 _rejected/）" if d.rejected else ""
        print(f"{d.title}（{d.mode}）：{d.entries} 条{extra}" + (f"；{d.message}" if d.message else ""))
        if d.failed:
            print("  核验未通过（kb-source-publish 会转人工审核）：" + "；".join(sources.VERIFY_RULES[k] for k in d.failed))
    for s in r.skipped:
        print(f"跳过：{s}", file=sys.stderr)
    batches.record_cli("split", "cli", {"docs": len(r.docs), "entries": total,
                                       "rejected": sum(d.rejected for d in r.docs)}, args.dir)
    print(f"共 {len(r.docs)} 份、{total} 条，写到 {args.out}；核对后用 kb-import {args.out} --as <知识专家> --submit 导入")
    return 0 if total else 1


def kb_reg_lifecycle(args: argparse.Namespace) -> int:
    """法规生命周期（三期 B26）：按日期切换依据现行状态、处理替代关系，列出受影响条目与交付件计数。"""
    from datetime import date

    from .kb import reg_lifecycle as rl

    try:
        day = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print(f"日期格式应为 YYYY-MM-DD：{args.date}", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        r = rl.run(db, day, apply=not args.dry_run, actor="cli", only=set(args.doc) if args.doc else None)
    print(rl.format_report(r))
    return 0


def kb_basis_check(args: argparse.Namespace) -> int:
    """依据变更登记（二期 B5）：季度官网核对清单与库内依据比对；--apply 自动登记时效变更。"""
    import json
    from pathlib import Path

    from .kb import basis_change as bc
    from .kb import batches

    path = Path(args.manifest)
    if not path.is_file():
        print(f"清单不存在：{path}", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        user = _knowledge_user(db, args.as_user)
        if user is None:
            return 1
        try:
            r = bc.check(db, user, path.read_bytes(), apply=args.apply)
        except bc.BasisChangeError as e:
            print(str(e), file=sys.stderr)
            return 1
    for f in r.findings:
        if f.kind == bc.UNCHANGED:
            continue
        ids = "、".join(f.applied or f.doc_ids)
        print(f"  {bc.KIND_NAMES[f.kind]}  第 {f.line} 行 {f.code or f.title}" + (f"  [{ids}]" if ids else "")
              + (f"  —— {f.detail}" if f.detail else ""))
    for s in r.skipped:
        print(f"跳过：{s}", file=sys.stderr)
    c = r.counts()
    batches.record_cli("basis", args.as_user, {**c, "apply": bool(args.apply)}, args.manifest)
    print("合计：" + "，".join(f"{bc.KIND_NAMES[k]} {n}" for k, n in c.items() if n)
          + ("；时效变更已登记并发布" if args.apply else "；未登记，确认后加 --apply"))
    if args.out:
        Path(args.out).write_text(json.dumps(r.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def kb_ref_check(args: argparse.Namespace) -> int:
    """知识引用校验基线（三期 B15）：快照交付件、导出脚注、对话里的知识引用，--compare 对比上一次快照，退化时退出码 2。"""
    import json
    from pathlib import Path

    from .kb import refcheck

    before = None
    if args.compare:
        path = Path(args.compare)
        if not path.is_file():
            print(f"快照不存在：{path}", file=sys.stderr)
            return 1
        before = json.loads(path.read_text(encoding="utf-8"))
    with SessionLocal() as db:
        snap = refcheck.snapshot(db, signed_only=args.signed_only)
        r = refcheck.compare(before, snap) if before is not None else None
        c = snap["counts"]
        sha = refcheck.snapshot_sha(snap)
        audit.record(db, actor="cli", action="kb.ref_check", result="error" if r and r.regressed else "allowed",
                     refs=c.get("found", 0) + c.get("unresolved", 0), unresolved=c.get("unresolved", 0),
                     signed_unresolved=c.get("signed_unresolved", 0), snapshot_sha=sha,
                     signed_only=args.signed_only, **({"compare": r.to_json()} if r else {}))
    print("快照：" + "，".join(f"{k} {v}" for k, v in sorted(c.items())) + f"；哈希 {sha}")
    if args.snapshot:
        Path(args.snapshot).write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已写入 {args.snapshot}（含项目 id，只留在本机，不提交）")
    if r is None:
        return 0
    print(f"对比：一致 {r.consistent}，前后都解析不到 {r.both_missing}，退化 {r.regressed}，新增解析到 {r.new_found}，"
          f"记录已不在 {r.removed}")
    if r.missing_refs:
        print("前后都解析不到：" + "、".join(r.missing_refs))
    if r.regressed:
        print("退化：" + "、".join(r.regressed_refs), file=sys.stderr)
        return 2
    return 0


def catalog_check(args: argparse.Namespace) -> int:
    """校验 products/、modules/ 下的服务产品与模块配置（二期 A1，docs/接口约定.md）。"""
    from .products import config

    cat = config.load()
    for prod in cat.products.values():
        print(f"产品 {prod.code} {prod.name}（{prod.status}，版本 {prod.version}）：交付件 {len(prod.deliverables)} 个、"
              f"交付链 {len(prod.chain)} 步、门禁 {len(prod.gates)} 条")
    for mod in cat.modules.values():
        print(f"模块 {mod.code} {mod.name}（{mod.status}，版本 {mod.version}）：可挂 {'、'.join(mod.products)}")
    for problem in cat.problems:
        print(f"问题：{problem}", file=sys.stderr)
    print(f"共 {len(cat.products)} 个产品、{len(cat.modules)} 个模块、{len(cat.problems)} 个问题")
    return 1 if cat.problems else 0


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


def kb_split(args: argparse.Namespace) -> int:
    from .core import kb_split as ks
    from .core.db import engine, kb_engine

    try:
        r = ks.split(engine, kb_engine, drop_source=args.drop_source)
    except ks.SplitError as e:
        print(str(e), file=sys.stderr)
        return 1
    with SessionLocal() as db:
        audit.record(db, actor="cli", action="kb.split", result="allowed",
                     rows=r.counts, copied=r.copied, dropped=r.dropped)
    for t, n in r.counts.items():
        print(f"{t}	{n}")
    print(("已复制并核对一致" if r.copied else "两边行数一致") + ("，主库里的知识区表已删除" if r.dropped else
          "；主库里的旧副本保留，确认无误后加 --drop-source 删除"))
    return 0


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

    from .kb import batches, importer

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
    batches.record_cli("import", args.as_user, {**counts, "total": len(results), "submit": bool(args.submit)}, args.dir)
    print("合计：" + "，".join(f"{label[k]} {n}" for k, n in counts.items() if n) +
          ("；已提交审核，仍需双人审核后发布" if args.submit else "；均为草稿，在审批队列提交审核"))
    return 2 if counts["error"] else 0


def kb_export(args: argparse.Namespace) -> int:
    """导出已发布的公开知识（连同审核记录和哈希），交生产环境 kb-import-export 导入（app/kb/transfer.py）。"""
    from pathlib import Path

    from .kb import transfer

    with SessionLocal() as db:
        user = _knowledge_user(db, args.as_user)
        if user is None:
            return 1
        try:
            r = transfer.export(db, user, Path(args.out), include_company=args.include_company)
        except transfer.TransferError as e:
            print(str(e), file=sys.stderr)
            return 1
    print(f"导出 {r.count} 条到 {r.out}")
    print(f"清单哈希（导入时用 --sha256 原样提供，经测试机以外的渠道交给生产运维）：{r.manifest_sha256}")
    return 0


def kb_import_export(args: argparse.Namespace) -> int:
    """生产环境导入 kb-export 的导出包：先校验哈希，再自动核验，通过的保留原审核人发布，其余转审核。"""
    from pathlib import Path

    from .kb import transfer

    with SessionLocal() as db:
        user = _knowledge_user(db, args.as_user)
        if user is None:
            return 1
        try:
            results = transfer.import_export(db, user, Path(args.dir), args.sha256)
        except transfer.TransferError as e:
            print(f"校验未通过，整批不导入：{e}", file=sys.stderr)
            return 2
    label = {"published": "核验发布", "review": "转审核", "draft": "留作草稿", "unchanged": "无变化",
             "skipped": "跳过", "error": "失败"}
    for o in results:
        if o.action != "unchanged":
            why = "、".join(transfer.RULES.get(f, f) for f in o.failed)
            print(f"  {label.get(o.action, o.action)}  {o.file} {o.doc_id}" + (f"  —— 未通过：{why}" if why else "")
                  + (f"  —— {o.message}" if o.message else ""))
    counts = {k: sum(1 for o in results if o.action == k) for k in label}
    print("合计：" + "，".join(f"{label[k]} {n}" for k, n in counts.items() if n))
    return 2 if counts["error"] else 0


def kb_reconfirm_scan(args: argparse.Namespace) -> int:
    """立即扫描复审到期的公开知识条目，排知识审核 Agent 复查（worker 每小时自动执行；复查本身在 worker 里做）。"""
    from .kb import reconfirm_agent
    with SessionLocal() as db:
        made = reconfirm_agent.scan_due(db)
    print(f"排知识审核 Agent 复查 {len(made)} 条（结果见知识审批页“复审到期”）")
    return 0


def kb_source_registry(args: argparse.Namespace) -> int:
    """来源登记表导出（三期 B16）：试用、正式的来源，供采集机按表抓取；候选、降级、停用不下发。"""
    import csv
    import json

    from .kb import source_registry
    with SessionLocal() as db:
        rows = source_registry.export_for_collector(db)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    cols = ["code", "name", "url", "host", "status", "license", "authority_kind", "authority_level"]
    w = csv.DictWriter(sys.stdout, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    return 0


def kb_source_seed(args: argparse.Namespace) -> int:
    """来源批量登记（四期 B25）：按资料仓的已批准来源清单（CSV）登记为候选或试用，规则与审计同页面登记。

    已登记的地址跳过；坏行报行号、不影响其他行。转正式仍须另一位知识管理员在页面批准，本命令不做。
    """
    import csv
    from pathlib import Path

    from .kb import review as rv
    from .kb import source_registry as sr
    from .models import KbSource

    path = Path(args.csv)
    if not path.is_file():
        print("来源清单文件不存在", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.as_user))
        if user is None or not user.is_active:
            print(f"用户 {args.as_user} 不存在或已停用", file=sys.stderr)
            return 1
        if not rv.is_knowledge_user(db, user):
            print("只有知识专家、知识管理员能登记来源", file=sys.stderr)
            return 1
        made = skipped = 0
        bad: list[str] = []
        with path.open(encoding="utf-8-sig", newline="") as f:
            for n, row in enumerate(csv.DictReader(f), start=2):
                url = (row.get("url") or "").strip()
                if url and db.scalar(select(KbSource.id).where(KbSource.url == url)) is not None:
                    skipped += 1
                    continue
                try:
                    src = sr.create(db, user, name=row.get("name") or "", url=url, license=row.get("license") or "",
                                    authority_kind=(row.get("authority_kind") or "").strip(),
                                    publisher=row.get("publisher") or "", note=row.get("note") or "")
                    if args.to == "trial":
                        sr.transition(db, user, src.code, "trial", "批量登记（资料仓已批准来源清单）")
                except sr.SourceError as e:
                    db.rollback()
                    bad.append(f"第 {n} 行：{e.detail}")
                    continue
                made += 1
    print(f"来源批量登记：新登记 {made} 个（{sr.STATUS_LABELS[args.to]}），跳过 {skipped} 个已登记，坏行 {len(bad)} 个")
    for line in bad:
        print(line, file=sys.stderr)
    return 3 if bad else 0


def kb_acquire_sources(args: argparse.Namespace) -> int:
    """知识源清单（三期 B25①）：来源确认队列的缺口经网关交 kb-builder 列清单，程序核对后写成 JSON 供来源确认。"""
    import json
    from pathlib import Path

    from .kb import source_list
    with SessionLocal() as db:
        try:
            rep = source_list.suggest(db, actor=args.as_user or "cli")
        except source_list.SourceListError as e:
            print(str(e), file=sys.stderr)
            return 1
    Path(args.out).write_text(json.dumps(rep.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"知识源清单：{len(rep.rows)} 行，需登记来源 {len(rep.needs_registration)} 行，丢弃 {rep.problems or 0}"
          + ("（演示模式，未接入模型）" if rep.demo else ""))
    return 0


def extraction_week(args: argparse.Namespace) -> int:
    """经验萃取一轮（四期 F4③）：超时收尾 → 3 类信号 → 三道过滤 → 派题（每人每周 ≤ 3 题）；只打印计数。"""
    from .project import extraction_trigger as et
    with SessionLocal() as db:
        r = et.run_week(db, actor="cli")
        st = et.stats(db)
    print("经验萃取：" + "，".join(f"{k}={v}" for k, v in r.items()) + f"；跳过率 {st['skip_rate']:.0%}")
    return 0


def survey_import(args: argparse.Namespace) -> int:
    """调研题库导入（四期 E2①）：整批校验后按题号增改；只打印计数。"""
    from pathlib import Path

    from .project import survey
    try:
        with SessionLocal() as db:
            added, updated = survey.import_bank(db, [q for f in args.files for q in survey.load_bank_file(Path(f))])
    except survey.SurveyError as e:
        print(f"题库未导入：{e.detail}")
        return 1
    print(f"调研题库：新增 {added}，更新 {updated}")
    return 0


def kb_acquire_now(args: argparse.Namespace) -> int:
    """立即全量采集（四期，负责人 11-27）：新正式来源登记首采缺口并放大 cap 排计划；下发包仍由 kb-acquire-dispatch 出。"""
    from datetime import date

    from .kb import gap_pool
    with SessionLocal() as db:
        out = gap_pool.first_collect(db, date.today(), cap=args.cap)
    print("立即全量：" + "，".join(f"{k}={v}" for k, v in out.items()))
    return 0


def kb_acquire_dispatch(args: argparse.Namespace) -> int:
    """采集下发包导出（三期 B25③）：本周“直接采集”的缺口，只含网址与采集规则，交采集机。"""
    import json
    from pathlib import Path

    from .kb import gap_pool
    with SessionLocal() as db:
        try:
            pkg = gap_pool.dispatch_package(db)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
    Path(args.out).write_text(json.dumps(pkg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"下发包已写出：{len(pkg['tasks'])} 个网址")
    return 0


def kb_acquire_import(args: argparse.Namespace) -> int:
    """采集回传批次导入（三期 B25③）：先核对清单、哈希、白名单，通过才关缺口；带 --manifest 时接着走 kb-source-publish。"""
    from pathlib import Path

    from .kb import gap_pool
    batch = Path(args.batch)
    if not batch.is_dir():
        print("回传批次目录不存在", file=sys.stderr)
        return 1
    if (args.manifest or args.auto_manifest) and not args.as_user:
        print("带 --manifest / --auto-manifest 入库时必须指定 --as", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        report, closed = gap_pool.import_return(db, batch)
    if not report.ok:
        print("回传批次核对未通过，整批不入库：" + "；".join(report.problems[:20]), file=sys.stderr)
        return 2
    print(f"回传批次核对通过：{report.count} 个文件，关闭缺口 {closed} 个"
          + (f"，{report.manual} 个取不到的转需人工下载" if report.manual else ""))
    if args.auto_manifest:
        import tempfile

        with SessionLocal() as db:
            text, unmatched = gap_pool.publish_manifest(db, batch)
        if unmatched:
            print(f"{len(unmatched)} 个文件找不到正式来源，未入库（先在来源登记表登记后重导）：" + "、".join(unmatched[:20]))
        if len(text.splitlines()) <= 1:
            return 0
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as fh:
            fh.write(text)
        args.manifest = fh.name
    if not args.manifest:
        return 0
    if not args.as_user:
        print("带 --manifest 入库时必须指定 --as", file=sys.stderr)
        return 1
    return kb_source_publish(argparse.Namespace(manifest=args.manifest, dir=str(batch), as_user=args.as_user,
                                                report=args.report))


def kb_gap_manual(args: argparse.Namespace) -> int:
    """登记“需人工下载”缺口（四期 B25）：资料仓 backlog 里自动取不到的条目，进知识审批页“需人工下载”清单。"""
    from .kb import acquisition, gap_pool

    with SessionLocal() as db:
        try:
            g = gap_pool.mark_manual(db, args.key.strip(), args.reason, create=True, event_ref=args.ref, actor="cli")
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
    print(f"缺口 {g.id} 已转需人工下载（{acquisition.MANUAL_REASONS[args.reason]}）")
    return 0


def kb_source_publish(args: argparse.Namespace) -> int:
    """依据原文自动核验发布（二期 B10）：从原件切条、核验，通过的由平台自动发布并抽检，没通过的转人工审核。"""
    import json
    from pathlib import Path

    from .kb import autopublish, batches, sources

    manifest, src = Path(args.manifest), Path(args.dir)
    if not manifest.is_file() or not src.is_dir():
        print("清单文件或资料目录不存在", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        user = _knowledge_user(db, args.as_user)
        if user is None:
            return 1
        r = autopublish.run(db, user, manifest, src)
    if r.suspended:
        print(f"自动发布已暂停（{r.suspended}），本批全部转人工审核")
    for d in r.docs:
        print(f"{d.title}：自动发布 {d.published}，转人工 {d.manual}，无变化 {d.unchanged}"
              + (f"，退回 {d.rejected}（不入库）" if d.rejected else "") + (f"；{d.message}" if d.message else ""))
        if d.failed:
            print("  核验未通过：" + "；".join(sources.VERIFY_RULES[k] for k in d.failed))
        for s in d.skipped:
            print(f"  跳过：{s}")
    for s in r.skipped:
        print(f"清单跳过：{s}", file=sys.stderr)
    batches.record_cli("source_publish", args.as_user,
                       {"docs": len(r.docs), "published": sum(d.published for d in r.docs),
                        "manual": sum(d.manual for d in r.docs), "rejected": sum(d.rejected for d in r.docs),
                        "sampled": len(r.sampled), "suspended": bool(r.suspended)}, args.dir)
    print(f"合计：自动发布 {sum(d.published for d in r.docs)}，转人工 {sum(d.manual for d in r.docs)}；"
          f"抽检 {len(r.sampled)} 条（知识审批页“抽检”）")
    if args.report:
        Path(args.report).write_text(json.dumps(r.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def kb_draft(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .kb import batches, drafting

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
    batches.record_cli("draft", args.as_user, {"accepted": len(report.accepted), "rejected": len(report.rejected),
                                               "pending": len(report.pending), "calls": report.calls}, args.source)
    print(f"调用模型 {report.calls} 次（{report.model}）。{report.note}")
    print(f"起草 {len(report.accepted)} 条，写到 {args.out}；退回 {len(report.rejected)} 条（见 {args.out}/_rejected/）")
    for p in written:
        todo = report.pending.get(p.name.removesuffix(".new.md").removesuffix(".md"))
        print(f"  {p.name}" + (f"  待核引用 {len(todo)} 处：{'、'.join(todo[:5])}" if todo else ""))
    if report.pending:
        print(f"{len(report.pending)} 条有待核引用（库里没有对应的依据原文）：先 kb-source-split 切条入库原文，或改正引用，否则不能提交审核。")
    print("这些只是草稿文件：专家逐条修改核对后，用 kb-import 导入，再经双人审核发布。")
    return 0


def agent_eval_lint(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .agents import evalgate

    n, issues, extras = evalgate.run(Path(args.path), [Path(d) for d in args.entries or []])
    for i in issues:
        print(f"  {i}")
    print(f"评测门：{n} 个文件，问题 {len(issues)} 处；扩展：{'、'.join(extras) or '无（A24 未接入）'}")
    print("评测门通过" if n and not issues else "评测门未通过")
    return 0 if n and not issues else 1


def agent_eval_majority(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from .agents import evalgate

    ok, lines = evalgate.majority([json.loads(Path(f).read_text(encoding="utf-8")) for f in args.reports])
    for line in lines:
        print(line)
    print(f"{len(args.reports)} 轮多数判定：{'通过' if ok else '未通过'}")
    return 0 if ok else 1


def agent_eval(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from .agents import evalset

    paths = sorted(Path(args.path).glob("*.yaml")) if Path(args.path).is_dir() else [Path(args.path)]
    try:
        cases = evalset.validate([c for p in paths for c in evalset.load(p)])
    except evalset.EvalError as e:
        print(f"评测集有问题：{e}", file=sys.stderr)
        return 1
    if args.product:
        cases = [c for c in cases if c.get("product", "") == args.product]
    if args.agent:
        cases = [c for c in cases if c["agent"] == args.agent]
    if not cases:
        print("没有符合条件的题目", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.as_user))
        if user is None or not user.is_active:
            print(f"用户 {args.as_user} 不存在或已停用", file=sys.stderr)
            return 1
        answers = None
        if args.answers:
            if get_settings().is_prod:
                print("生产环境不写答案原文（--answers 只用于开发、测试环境回放虚拟题目）", file=sys.stderr)
            else:
                answers = Path(args.answers)
                answers.mkdir(parents=True, exist_ok=True)
        total = len(cases)
        done = [0]

        def progress(r: evalset.CaseResult, secs: float, text: str) -> None:
            done[0] += 1
            mark = "通过" if r.passed else {"demo": "跳过", "error": "出错", "blocked": "拦截"}.get(r.status, "未通过")
            why = f"：{'；'.join(r.reasons)}" if r.reasons else ""
            print(f"[{done[0]}/{total}] {r.id} {mark} {secs:.0f} 秒{why}", flush=True)
            if answers is not None and text:
                (answers / f"{r.id}.md").write_text(text, encoding="utf-8")

        rep = evalset.run(db, user, cases, timeout=args.case_timeout, on_result=progress)
    print(evalset.format_report(rep), flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(rep.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写到 {args.out}（只有题号、计数和未通过原因，不含答案原文）")
    if answers is not None:
        print(f"答案原文已逐题写到 {answers}（{len(rep.answers)} 份，虚拟题目回放用，不要提交或外传）")
    return 0 if rep.passed else 3


def kb_assembly_eval(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from .kb import assembly_eval as ae
    from .kb import search

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.as_user))
        if user is None or not user.is_active:
            print(f"用户 {args.as_user} 不存在或已停用", file=sys.stderr)
            return 1
        try:
            if args.gate:
                ok, line, base, report = ae.gate(db, user, ae.load(Path(args.evalset)))
                print(ae.format_report(base))
                print()
                print(ae.format_report(report))
                print(line)
                if args.out:
                    Path(args.out).write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2),
                                              encoding="utf-8")
                return 0 if ok else 3
            report = ae.run(db, user, ae.load(Path(args.evalset)), strategy=args.strategy)
            verdict = None
            if args.baseline:
                base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
                verdict = ae.compare(report, base, min_gain=args.min_gain)
        except (ae.AssemblyEvalError, search.SearchError, OSError, ValueError) as e:
            print(f"装配评测没有完成：{e}", file=sys.stderr)
            return 1
    print(ae.format_report(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写到 {args.out}（只有章节编号、知识 key 和计数，不含正文）")
    if verdict is not None:
        print(verdict[1])
        return 0 if verdict[0] else 3
    return 0


def kb_eval(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from .kb import batches, embed_compare, evaluate
    from .services import gateway

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.as_user))
        if user is None or not user.is_active:
            print(f"用户 {args.as_user} 不存在或已停用", file=sys.stderr)
            return 1
        try:
            questions = evaluate.load_questions(Path(args.questions))
            if args.embed_compare:
                emb = embed_compare.run(db, user, questions, b_url=args.b_url, b_model=args.b_model,
                                        b_dim=args.b_dim, b_key=os.environ.get("EMBED_B_API_KEY", ""))
            elif args.sweep:
                from .kb import sweep

                swp = sweep.run(db, user, questions)
            elif args.compare:
                from .kb import compare

                cmp = compare.run(db, user, questions)
            else:
                report = evaluate.run(db, user, questions, include_candidates=args.with_candidates)
        except (evaluate.EvalError, ValueError) as e:
            print(f"题目文件有问题：{e}", file=sys.stderr)
            return 1
        except (embed_compare.EmbedCompareError, gateway.GatewayError, gateway.BlockedError) as e:
            print(f"向量模型对比没有完成：{e}", file=sys.stderr)
            return 1
    if args.embed_compare:
        print(embed_compare.format_report(emb))
        if args.out:
            Path(args.out).write_text(json.dumps(emb.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"明细已写到 {args.out}（只有题号、名次和指标）")
        return 0
    if args.sweep:
        print(sweep.format_report(swp))
        if args.out:
            Path(args.out).write_text(json.dumps(swp.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"明细已写到 {args.out}（只有门槛、计数和准确率）")
        return 0
    if args.compare:
        batches.record_cli("eval", args.as_user, {"total": cmp.total, "compare": True, "passed": cmp.passed,
                                                  "demo": cmp.demo, "accuracy_with_kb": cmp.with_kb.get("accuracy"),
                                                  "accuracy_no_kb": cmp.no_kb.get("accuracy")}, args.questions)
        print(compare.format_report(cmp))
        if args.out:
            Path(args.out).write_text(json.dumps(cmp.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"明细已写到 {args.out}（含题号和识别出的引用，不含答案原文）")
        return 0 if cmp.passed else 3
    batches.record_cli("eval", args.as_user, {"total": report.total, "compare": False,
                                              "accuracy": round(report.accuracy, 4),
                                              "passed": report.accuracy >= 0.9}, args.questions)
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
    sub.add_parser("totp-rekey", help="用 TOTP_KEYS 第一把密钥重新加密全部账号的 TOTP 密钥（密钥轮换后执行）")         .set_defaults(func=totp_rekey)
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
    cp.add_argument("--product", default="SOC", help="服务产品代号（products/ 下 ready 的产品，默认 SOC）")
    cp.add_argument("--module", action="append", help="勾选的模块代号，可重复；只能选产品允许的 ready 模块")
    cp.add_argument("--service", default="", help="显示名（默认用产品名称）")
    cp.add_argument("--industry", default="", help="行业，如 银行")
    cp.add_argument("--weeks", type=int, default=12, help="计划周数（默认 12）")
    cp.add_argument("--org-type", default="", help="组织类型（可空）：central_soe 央企、local_soe 地方国企、private 民营企业、foreign 外资企业、"
                    "government 政府机关、institution 事业单位、other 其他")
    cp.set_defaults(func=create_project)
    rr = sub.add_parser("retention-report", help="已验收项目的保留期与到期状态（--all 含未验收）")
    rr.add_argument("--all", action="store_true")
    rr.set_defaults(func=retention_report)
    sub.add_parser("kb-recheck-scan", help="立即扫描交付件里的知识引用，标记使用反馈待复查（worker 每天自动执行）")         .set_defaults(func=kb_recheck_scan)
    sub.add_parser("deletion-scan", help="立即扫描到期项目并发起删除申请（worker 每小时自动执行）")         .set_defaults(func=deletion_scan)
    dr = sub.add_parser("deletion-report", help="未完成的删除申请（--all 含已删除、已撤销）")
    dr.add_argument("--all", action="store_true")
    dr.add_argument("--monthly", action="store_true", help="按申请月份汇总（只有计数，二期 C5 O6）")
    dr.set_defaults(func=deletion_report)
    a = sub.add_parser("verify-audit", help="校验审计哈希链（发现问题时退出码为 2）")
    a.add_argument("--expect-head", default=None, help="之前记下的链头哈希，校验它仍在链上")
    a.set_defaults(func=verify_audit)
    sub.add_parser("quality-fp-stats", help="各类阻断的驳回率（误报率）与自动校准状态（四期 A18②）"
                   ).set_defaults(func=quality_fp_stats)
    us = sub.add_parser("usage-report", help="大模型用量汇总")
    us.add_argument("--days", type=int, default=30)
    us.add_argument("--by", default="username,project_code,model",
                    help="汇总维度，逗号分隔：username / project_code / model")
    us.set_defaults(func=usage_report)
    sub.add_parser("jobs", help="后台任务状态").set_defaults(func=jobs_status)
    sub.add_parser("catalog-check", help="校验服务产品、模块与行业包配置（products/、modules/、industries/）").set_defaults(func=catalog_check)
    sc = sub.add_parser("selfcheck", help="排一个 worker 自检任务并等待结果（成功退出码 0）")
    sc.add_argument("--wait", type=int, default=60)
    sc.set_defaults(func=selfcheck)
    sub.add_parser("kb-reembed", help="更换向量模型后，重新向量化全部已发布知识").set_defaults(func=kb_reembed)
    ki = sub.add_parser("kb-import", help="批量导入知识条目文件（只收公开资料，导入为草稿）")
    ki.add_argument("dir", help="条目文件所在目录（*.md，带元数据头）")
    ki.add_argument("--as", dest="as_user", required=True, help="以哪个知识专家或知识管理员的身份导入")
    ki.add_argument("--submit", action="store_true", help="导入后一并提交审核")
    ki.set_defaults(func=kb_import)
    ke = sub.add_parser("kb-export", help="导出已发布的公开知识及审核记录、哈希，交生产环境导入")
    ke.add_argument("out", help="导出目录（必须为空或不存在）")
    ke.add_argument("--as", dest="as_user", required=True, help="以哪个知识专家或知识管理员的身份导出")
    ke.add_argument("--include-company", action="store_true", help="另含已发布的公司做法类条目（只限测试 / 试用环境，生产拒绝）")
    ke.set_defaults(func=kb_export)
    kx = sub.add_parser("kb-import-export", help="导入 kb-export 的导出包：校验哈希、自动核验，通过的保留原审核人发布")
    kx.add_argument("dir", help="导出目录")
    kx.add_argument("--sha256", required=True, help="导出时打印的清单哈希")
    kx.add_argument("--as", dest="as_user", required=True, help="以哪个知识专家或知识管理员的身份导入")
    kx.set_defaults(func=kb_import_export)
    kd = sub.add_parser("kb-draft", help="由知识库建设 Agent 从公开资料原文起草条目文件（不直接入库）")
    kd.add_argument("--source", required=True, help="公开资料文件：.docx / .pdf / .md / .txt")
    kd.add_argument("--source-detail", required=True, help="来源说明，如“中华人民共和国网络安全法（2016）全文”")
    kd.add_argument("--out", required=True, help="起草结果写到哪个目录")
    kd.add_argument("--as", dest="as_user", required=True, help="以哪个知识专家或知识管理员的身份起草")
    kd.add_argument("--product", default="", help="默认服务产品缩写，如 SOC、GEN")
    kd.add_argument("--domain", default="", help="默认一级目录缩写，如 OPS、CMP")
    kd.set_defaults(func=kb_draft)
    kss = sub.add_parser("kb-source-split", help="依据原文切条：按下载清单把法规按条、标准按条款目录切成条目文件（不入库）")
    kss.add_argument("manifest", help="下载清单 CSV（文件名、名称、文号/标准号、现行状态、切分方式……）")
    kss.add_argument("--dir", required=True, help="下载文件所在目录（清单里的文件名相对它）")
    kss.add_argument("--out", required=True, help="条目文件写到哪个目录")
    kss.set_defaults(func=kb_source_split)
    kbc = sub.add_parser("kb-basis-check", help="依据变更登记：季度官网核对清单与库内依据比对，--apply 自动登记时效变更")
    kbc.add_argument("manifest", help="核对清单 CSV（表头同下载清单：名称、文号/标准号、版本、现行状态、替代文件、施行日期）")
    kbc.add_argument("--as", dest="as_user", required=True, help="以哪个知识管理员的身份登记（只比对时知识专家也可）")
    kbc.add_argument("--apply", action="store_true", help="自动登记时效变更：建新版本、平台核对后发布")
    kbc.add_argument("--out", default="", help="把比对结果写成 JSON 文件")
    kbc.set_defaults(func=kb_basis_check)
    krl = sub.add_parser("kb-reg-lifecycle", help="法规生命周期：按施行日、废止日切换依据现行状态，处理替代关系（worker 每日自动执行）")
    krl.add_argument("--date", default="", help="按哪一天判断（YYYY-MM-DD，默认今天）")
    krl.add_argument("--dry-run", action="store_true", help="只列出到期变化和受影响清单，不登记")
    krl.add_argument("--doc", action="append", default=[], help="只处理这个条目编号（可重复，排查用）")
    krl.set_defaults(func=kb_reg_lifecycle)
    krc = sub.add_parser("kb-ref-check", help="知识引用校验基线：快照交付件、导出脚注、对话里的知识引用，--compare 退化时退出码 2")
    krc.add_argument("--snapshot", help="快照写入的文件（含项目 id，只留在本机）")
    krc.add_argument("--compare", help="与之对比的上一次快照文件")
    krc.add_argument("--signed-only", action="store_true", help="只扫已签署交付件")
    krc.set_defaults(func=kb_ref_check)
    ksp = sub.add_parser("kb-source-publish", help="依据原文自动核验发布：从原件切条、核验，通过的平台自动发布并抽检，没通过的转人工")
    ksp.add_argument("manifest", help="下载清单 CSV（要有原网址和原件 sha256 列）")
    ksp.add_argument("--dir", required=True, help="下载文件所在目录")
    ksp.add_argument("--as", dest="as_user", required=True, help="导入人（知识专家或知识管理员）")
    ksp.add_argument("--report", default="", help="核验记录写成 JSON 文件")
    ksp.set_defaults(func=kb_source_publish)
    kas = sub.add_parser("kb-acquire-sources", help="知识源清单：来源确认队列的缺口经网关交 kb-builder 列清单，程序核对不编造网址")
    kas.add_argument("--out", required=True, help="清单 JSON 文件")
    kas.add_argument("--as", dest="as_user", default="", help="发起人（记用量与审计）")
    kas.set_defaults(func=kb_acquire_sources)
    ew = sub.add_parser("extraction-week", help="经验萃取一轮（四期 F4③）：收信号、先查后问过滤、派题、超时收尾")
    ew.set_defaults(func=extraction_week)
    si = sub.add_parser("survey-import", help="导入调研题库文件（四期 E2①，E1 题库格式 YAML，可多个）")
    si.add_argument("files", nargs="+")
    si.set_defaults(func=survey_import)
    kan = sub.add_parser("kb-acquire-now", help="立即全量采集：新正式来源登记首采缺口，按放大的上限排一次计划（已采过的不重开）")
    kan.add_argument("--cap", type=int, default=2000, help="本次计划上限（默认 2000，每周例行为 20）")
    kan.set_defaults(func=kb_acquire_now)
    kad = sub.add_parser("kb-acquire-dispatch", help="导出采集下发包：本周直接采集的缺口网址与规则（白名单外拒绝）")
    kad.add_argument("--out", required=True, help="下发包 JSON 文件")
    kad.set_defaults(func=kb_acquire_dispatch)
    kai = sub.add_parser("kb-acquire-import", help="导入采集回传批次：核对清单、哈希、白名单，通过才关缺口并可接着自动核验发布")
    kai.add_argument("batch", help="回传批次目录（含 manifest.json）")
    kai.add_argument("--manifest", default="", help="下载清单；给了就接着走 kb-source-publish")
    kai.add_argument("--auto-manifest", action="store_true",
                     help="按来源登记表自动生成下载清单（名称、发布机关取正式来源）再走 kb-source-publish；须带 --as")
    kai.add_argument("--as", dest="as_user", default="", help="导入人（知识专家或知识管理员）")
    kai.add_argument("--report", default="", help="核验记录写成 JSON 文件")
    kai.set_defaults(func=kb_acquire_import)
    kgm = sub.add_parser("kb-gap-manual", help="登记需人工下载的缺口（网址或编号），进知识审批页“需人工下载”清单")
    kgm.add_argument("key", help="官方网址，或没有网址时用“国家 法规名称”")
    kgm.add_argument("--reason", required=True, choices=("blocked", "dns", "timeout", "no_file", "not_found", "other"),
                     help="原因类别")
    kgm.add_argument("--ref", default="", help="事件编号（如 BACKLOG-12），只许编号字符")
    kgm.set_defaults(func=kb_gap_manual)
    ksr = sub.add_parser("kb-source-registry", help="导出来源登记表里试用、正式的来源（CSV，供采集机按表抓取）")
    ksr.add_argument("--json", action="store_true", help="输出 JSON")
    ksr.set_defaults(func=kb_source_registry)
    kss = sub.add_parser("kb-source-seed", help="按资料仓已批准来源清单（CSV）批量登记为候选或试用；转正式仍须知识管理员在页面批准")
    kss.add_argument("csv", help="来源清单 CSV（列：name,url,publisher,license,authority_kind,note）")
    kss.add_argument("--as", dest="as_user", required=True, help="登记人（知识专家或知识管理员）")
    kss.add_argument("--to", choices=("candidate", "trial"), default="candidate", help="登记后的状态，默认候选")
    kss.set_defaults(func=kb_source_seed)
    sub.add_parser("kb-reconfirm-scan", help="立即扫描复审到期的公开知识条目，排知识审核 Agent 复查（worker 每小时自动执行）")         .set_defaults(func=kb_reconfirm_scan)
    ke = sub.add_parser("kb-eval", help="依据查询评测：准确率、命中率、相似度分布（准确率 ≥ 90%% 时退出码 0）")
    ke.add_argument("questions", help="评测题目文件（YAML）")
    ke.add_argument("--as", dest="as_user", required=True, help="以哪个用户的身份检索（可见范围按该用户）")
    ke.add_argument("--out", default="", help="把明细写成 JSON 文件")
    ke.add_argument("--compare", action="store_true",
                    help="有无知识库对比评测（二期 B1 补充）：同一批题分别不用、用知识库让模型作答，核对文件名和条款号，达标时退出码 0")
    ke.add_argument("--sweep", action="store_true",
                    help="门槛校准：按题目算依据查询、知识检索各个重排门槛下的准确率，给出推荐值（只出报告，不改配置）")
    ke.add_argument("--embed-compare", action="store_true",
                    help="向量模型对比（只在开发、测试环境）：现用 EMBED_MODEL 与候选模型各自向量化已发布切片，比向量召回命中率和 MRR")
    ke.add_argument("--b-url", default="", help="候选向量服务地址（OpenAI 兼容，如 http://embed-m3:8000/v1）；密钥放环境变量 EMBED_B_API_KEY")
    ke.add_argument("--b-model", default="", help="候选模型名，如 BAAI/bge-m3")
    ke.add_argument("--b-dim", type=int, default=0, help="候选模型向量维度，如 1024")
    ke.add_argument("--with-candidates", action="store_true",
                    help="影子评测（三期 B24①）：检索带上候选影子期的候选版本（同一条目用候选替换旧现行版本）；只对普通评测生效")
    ke.set_defaults(func=kb_eval)
    kae = sub.add_parser("kb-assembly-eval", help="章节级装配评测（三期 A24）：每章应命中知识的就绪度、命中率、缺槽；"
                         "给 --baseline 时命中率不低于基线 + --min-gain 退出码 0，否则 3")
    kae.add_argument("evalset", help="评测集文件（YAML，每章 3～5 条应命中知识）")
    kae.add_argument("--as", dest="as_user", required=True, help="以哪个用户的身份检索（可见范围按该用户）")
    kae.add_argument("--strategy", default="baseline", choices=["baseline", "chapter", "slots"],
                     help="baseline 交付链步骤固定查询（现行做法）；chapter 章节查询；slots A12 按知识槽位装配")
    kae.add_argument("--out", default="", help="把明细写成 JSON 文件（可作下次的 --baseline）")
    kae.add_argument("--baseline", default="", help="基线报告 JSON（--out 写出的）")
    kae.add_argument("--min-gain", type=float, default=0.0, help="要求比基线高出多少（如 0.01 = 1 个百分点）")
    kae.add_argument("--gate", action="store_true",
                     help="A12 程序门：同时跑 baseline 和 slots，按槽位装配命中率高于基线才返回 0（否则 3）")
    kae.set_defaults(func=kb_assembly_eval)
    kar = sub.add_parser("kb-agent-review-rerun",
                         help="积压重跑：规则预判上线前知识审核 Agent 判“不重复、不冲突”没过的待审版本各重跑一次")
    kar.add_argument("--limit", type=int, default=None, help="最多排几条")
    kar.add_argument("--dry-run", action="store_true", help="只按规则预判类别打印计数，不排队")
    kar.set_defaults(func=kb_agent_review_rerun)
    kcp = sub.add_parser("kb-candidate-promote",
                         help="候选影子期晋级：满 KB_CANDIDATE_DAYS 天且抽检没判不通过的候选转为已发布，抽检不通过的退回人工（worker 每天自动执行）")
    kcp.add_argument("--dry-run", action="store_true", help="只列出会晋级、退回的候选，不改动")
    kcp.set_defaults(func=kb_candidate_promote)
    ae = sub.add_parser("agent-eval", help="Agent 评测（二期 C4）：红线用例、黄金样例要点、埋雷，全部通过时退出码 0")
    ae.add_argument("path", help="评测集 YAML 文件或目录（knowledge/eval/agents）")
    ae.add_argument("--as", dest="as_user", required=True, help="以哪个用户的身份调用（记用量）")
    ae.add_argument("--product", default="", help="只跑某个产品的题")
    ae.add_argument("--agent", default="", help="只跑某个 Agent 的题")
    ae.add_argument("--out", default="", help="把明细写成 JSON 文件")
    ae.add_argument("--case-timeout", type=float, default=300, help="单题总耗时上限（秒，默认 300，0 不限）；超时记出错、继续下一题")
    ae.add_argument("--answers", default="", help="把每题答案原文写到目录（<题号>.md，只限开发、测试环境，回放核对用）")
    ae.set_defaults(func=agent_eval)
    al = sub.add_parser("agent-eval-lint", help="评测门（三期 C8，CI 跑，不连模型）：评测集格式、红线、资质用语，全部通过时退出码 0")
    al.add_argument("path", help="评测集 YAML 文件或目录（knowledge/eval/agents）；装配评测集（有 chapters）也可")
    al.add_argument("--entries", action="append", help="条目目录（可多次）：核对装配评测集 expect 的 key 都存在（四期 F6）")
    al.set_defaults(func=agent_eval_lint)
    am3 = sub.add_parser("agent-eval-majority", help="真模型多轮判定（三期 C8，测试机用）：多份 agent-eval --out 结果按每题多数判定，红线题一轮失败即不通过")
    am3.add_argument("reports", nargs="+", help="各轮 agent-eval --out 写出的 JSON")
    am3.set_defaults(func=agent_eval_majority)
    lt = sub.add_parser("loadtest-accounts", help="压测账号（只在开发、测试环境）：建或重置 lt-xx 并加入压测项目；--disable 停用")
    lt.add_argument("--count", type=int, default=20)
    lt.add_argument("--project", default="LOAD-A")
    lt.add_argument("--out", default="", help="凭据文件（JSON，权限 600）；不打印到终端")
    lt.add_argument("--disable", action="store_true", help="停用全部压测账号并注销其会话")
    lt.set_defaults(func=loadtest_accounts)
    ks = sub.add_parser("kb-split", help="分库：把知识区数据从主库搬到知识区数据库并核对行数")
    ks.add_argument("--drop-source", action="store_true", help="核对一致后删掉主库里的知识区表（删数据，先经负责人同意）")
    ks.set_defaults(func=kb_split)
    sub.add_parser("storage-provision", help="对象存储最小权限：用根账号建桶、建应用账号、挂只限两个桶的策略（可重复执行）")         .set_defaults(func=storage_provision)
    ind = sub.add_parser("indicators", help="指标看板（只有数字）；--v4 出 v4.0 验收口径汇总（萃取跳过率、四格分列、四格就绪度）")
    ind.add_argument("--days", type=int, default=30)
    ind.add_argument("--v4", action="store_true")
    ind.set_defaults(func=indicators_cmd)
    br = sub.add_parser("backup-record", help="备份 / 恢复脚本完成后记审计（由 scripts/backup.sh、restore.sh 调用）")
    br.add_argument("--op", choices=["create", "restore"], required=True)
    br.add_argument("--stamp", required=True)
    br.add_argument("--head-id", type=int, default=0)
    br.add_argument("--bytes", type=int, default=0)
    br.set_defaults(func=backup_record)
    args = p.parse_args(argv)
    if args.func in (catalog_check, kb_source_split, agent_eval_lint, agent_eval_majority):      # 只读配置文件，不连数据库（开发线 2 本地校验用）
        return args.func(args)
    try:
        upgrade_to_head()
    except KbSplitPending:
        if args.func is not kb_split:
            raise
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
