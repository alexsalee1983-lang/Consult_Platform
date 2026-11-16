"""运维自动化（二期 C5：O5～O8，决策记录 2026-11-15“人工环节复查的落地范围”）。

- O5 每日自检：worker 每 SELFCHECK_SECONDS 排一个 system.selfcheck 任务（已有排队中的不重复排），结果进指标
  cp_selfcheck_ok / cp_selfcheck_timestamp_seconds，失败或超过 26 小时没完成告警。
- O6 删除逾期：后端指标 cp_deletions_open / cp_deletions_overdue（未完成且过了应完成日期），逾期告警；
  按月汇总用 python -m app.cli deletion-report --monthly（只有计数）。
- O7 长期未登录自动停用：worker 每天把超过 INACTIVE_ACCOUNT_DAYS 天没登录（从没登录过按建号时间算）的账号停用，
  踢掉会话，审计 user.disable（actor=platform，trigger=auto:inactive）。系统管理员不自动停用（免得没人能在网页上恢复），
  照旧在用户页标出；被停用的由系统管理员在用户页“启用”恢复，启用后重新计时（users.enabled_at）。INACTIVE_ACCOUNT_DAYS=0 关闭。
- O8 备份时效：后端指标 cp_backup_last_success_timestamp_seconds 取最近一条 backup.create 审计的时间，超过 26 小时告警。

都只记计数和时间，不记任何业务内容。
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import audit, sessions
from ..core.config import get_settings
from ..core.db import utcnow
from ..core.permissions import SYS_ADMIN, roles_of
from ..models import AuditLog, User

RULE_INACTIVE = "auto:inactive"
PLATFORM = "platform"


def _aware(t: datetime | None) -> datetime | None:
    return t.replace(tzinfo=UTC) if t is not None and t.tzinfo is None else t


def last_seen(u: User) -> datetime | None:
    """最近一次登录（从没登录过按建号时间）和最近一次被启用，取晚的。"""
    times = [t for t in (_aware(u.last_login_at or u.created_at), _aware(u.enabled_at)) if t is not None]
    return max(times) if times else None


def disable_inactive(db: Session, now: datetime | None = None, usernames: list[str] | None = None) -> list[str]:
    """O7：停用长期未登录的账号，返回停用的用户名（可重复执行；usernames 只查这些，测试用）。"""
    days = get_settings().inactive_account_days
    if days <= 0:
        return []
    now = now or utcnow()
    cutoff = now - timedelta(days=days)
    made: list[str] = []
    q = select(User).where(User.is_active.is_(True))
    if usernames is not None:
        q = q.where(User.username.in_(usernames))
    for u in db.scalars(q.order_by(User.id)).all():
        seen = last_seen(u)
        if seen is None or seen >= cutoff or SYS_ADMIN in roles_of(db, u.id):
            continue
        u.is_active = False
        db.commit()
        sessions.destroy_all_for_user(u.id)
        audit.record(db, actor=PLATFORM, action="user.disable", result="allowed", target=u.username,
                     trigger=RULE_INACTIVE, days=days)
        made.append(u.username)
    return made


def deletion_counts(db: Session, today: date | None = None) -> tuple[int, int]:
    """O6：(未完成的删除申请数, 其中已过应完成日期的数)。"""
    from ..project.deletion import OPEN, ProjectDeletion

    today = today or utcnow().date()
    open_n = db.scalar(select(func.count()).select_from(ProjectDeletion).where(ProjectDeletion.status.in_(OPEN))) or 0
    overdue = db.scalar(select(func.count()).select_from(ProjectDeletion)
                        .where(ProjectDeletion.status.in_(OPEN), ProjectDeletion.due_by < today)) or 0
    return int(open_n), int(overdue)


def last_backup(db: Session) -> datetime | None:
    """O8：最近一次备份成功（backup.create 审计）的时间；从没备份过返回 None。"""
    return _aware(db.scalar(select(AuditLog.at).where(AuditLog.action == "backup.create", AuditLog.result == "allowed")
                            .order_by(AuditLog.id.desc()).limit(1)))


def monthly_deletions(db: Session) -> list[dict]:
    """O6 按月汇总：按申请月份统计发起数、完成数、按期完成数、撤销数、未完成数和其中逾期数（只有计数）。"""
    from ..project.deletion import CANCELLED, DONE, OPEN, ProjectDeletion

    today = utcnow().date()
    rows: dict[str, dict] = {}
    for d in db.scalars(select(ProjectDeletion).order_by(ProjectDeletion.id)):
        month = _aware(d.requested_at).strftime("%Y-%m")
        r = rows.setdefault(month, {"month": month, "requested": 0, "done": 0, "on_time": 0, "cancelled": 0,
                                    "open": 0, "overdue": 0})
        r["requested"] += 1
        if d.status == DONE:
            r["done"] += 1
            if d.completed_at is not None and _aware(d.completed_at).date() <= d.due_by:
                r["on_time"] += 1
        elif d.status == CANCELLED:
            r["cancelled"] += 1
        elif d.status in OPEN:
            r["open"] += 1
            if d.due_by < today:
                r["overdue"] += 1
    return [rows[k] for k in sorted(rows)]
