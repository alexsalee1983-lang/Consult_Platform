"""项目验收与保留期（M4 ①）。

依据：docs/客户数据告知模板与合同条款.md 第六节和第 X.3 条——验收后项目资料保留 6 个月（或双方另行约定的期限，
可按客户制度缩短），到期经人工确认后删除并形成删除记录；客户可以要求提前删除。删除见 app/project/deletion.py（M4 ②）。

规则：
- 项目经理登记验收日（不能晚于今天）和保留月数（0～24，默认 6）；平台算出到期日 = 验收日 + 保留月数
  （月末顺延到当月最后一天，例如 8-31 + 6 个月 = 2-28 / 2-29）。可以改（更正日期、按约定调整期限），每次都进审计；
  更正后不再到期的，待确认的到期删除申请自动撤销（M4 ②）。
- 状态：active 未验收 / retained 保留中 / due_soon 到期前 REMIND_DAYS 天内（提醒项目经理）/ due 已到期（待人工确认删除）。
- 两档都适用：严格档平台上只有摘要卡和占位符草稿，同样按保留期删除（附件二第 139 行）。
- 审计只记日期、月数和到期日，不记客户内容。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from ..core import audit
from ..core.permissions import PROJECT_MANAGER, membership
from ..models import Project, User
from ..schemas import PHASES

DEFAULT_MONTHS = 6
MAX_MONTHS = 24
REMIND_DAYS = 30
ACCEPTED_PHASE = PHASES.index("验收")

ACTIVE, RETAINED, DUE_SOON, DUE = "active", "retained", "due_soon", "due"


class RetentionError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def add_months(d: date, months: int) -> date:
    """日期加整月；目标月没有这一天时取当月最后一天。"""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


@dataclass
class Status:
    state: str
    days_left: int | None      # 距到期日的天数；已过期为负数；未验收为 None


def status(p: Project, today: date | None = None) -> Status:
    if p.accepted_on is None or p.deletion_due_on is None:
        return Status(ACTIVE, None)
    left = (p.deletion_due_on - (today or date.today())).days
    if left <= 0:
        return Status(DUE, left)
    return Status(DUE_SOON if left <= REMIND_DAYS else RETAINED, left)


def accept(db: Session, user: User, p: Project, accepted_on: date, months: int,
           today: date | None = None) -> Project:
    """项目经理登记（或更正）验收日和保留期。"""
    action = "project.acceptance"
    m = membership(db, p.id, user.id)
    if m is None or m.project_role != PROJECT_MANAGER:
        audit.record(db, actor=user.username, action=action, result="denied", project_code=p.code,
                     reason="not_manager")
        db.commit()
        raise RetentionError(403, "只有本项目的项目经理能登记验收")
    if p.deleted_at is not None:
        raise RetentionError(409, "项目资料已删除")
    if accepted_on > (today or date.today()):
        raise RetentionError(422, "验收日不能晚于今天")
    if not 0 <= months <= MAX_MONTHS:
        raise RetentionError(422, f"保留期取 0～{MAX_MONTHS} 个月")
    before = (p.accepted_on, p.retention_months)
    p.accepted_on = accepted_on
    p.retention_months = months
    p.deletion_due_on = add_months(accepted_on, months)
    p.phase = ACCEPTED_PHASE
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=p.code,
                 accepted_on=accepted_on.isoformat(), retention_months=months,
                 deletion_due_on=p.deletion_due_on.isoformat(), corrected=before[0] is not None)
    db.commit()
    from .deletion import cancel_if_not_due      # 更正后不再到期：撤销待确认的到期删除申请

    cancel_if_not_due(db, user.username, p)
    return p
