"""四期 A31 ⑦项目终止、⑧结项清单（docs/人工职责清单.md“结项清单与项目终止”）。

结项清单：列出未签交付件、未关的卡、待复查（被标“需更新”）的章，逐项关闭后 ready=True。
项目终止：只有项目经理能做；停止自动推进（autoflow 跳过 terminated_on 非空的项目）、关闭全部待办卡，
保留期从终止日起算（到期删除仍走 deletion 的到期流程）。审计只记代号与原因长度，不记原因原文。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.permissions import PROJECT_MANAGER, membership
from ..models import Confirmation, Deliverable, DeliverableSection, Project, User
from . import retention

TERMINATED_NOTE = "项目终止，自动关闭"


class CloseoutError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def checklist(db: Session, p: Project) -> dict:
    dels = list(db.scalars(select(Deliverable).where(Deliverable.project_id == p.id).order_by(Deliverable.id)))
    unsigned = [{"code": d.code, "title": d.title, "status": d.status} for d in dels if d.status != "signed"]
    cards = [{"id": c.id, "kind": c.kind, "title": c.title} for c in db.scalars(
        select(Confirmation).where(Confirmation.project_id == p.id, Confirmation.status == "open")
        .order_by(Confirmation.id))]
    code_of = {d.id: d.code for d in dels}
    stale = [{"deliverable_code": code_of[s.deliverable_id], "seq": s.seq, "title": s.title} for s in db.scalars(
        select(DeliverableSection).where(DeliverableSection.deliverable_id.in_(code_of),
                                         DeliverableSection.stale_reason != "")
        .order_by(DeliverableSection.deliverable_id, DeliverableSection.seq))]
    from . import intake
    contract = intake.contract_block(db, p)          # 四期 A32：先行启动合同未签不能结项
    return {"unsigned": unsigned, "open_cards": cards, "stale_sections": stale, "contract": contract,
            "ready": not (unsigned or cards or stale or contract),
            "terminated_on": p.terminated_on.isoformat() if p.terminated_on else None}


def terminate(db: Session, user: User, p: Project, reason: str, today: date | None = None) -> Project:
    action = "project.terminate"
    m = membership(db, p.id, user.id)
    if m is None or m.project_role != PROJECT_MANAGER:
        audit.record(db, actor=user.username, action=action, result="denied", project_code=p.code,
                     reason="not_manager")
        db.commit()
        raise CloseoutError(403, "只有本项目的项目经理能终止项目")
    if p.deleted_at is not None or p.terminated_on is not None or p.accepted_on is not None:
        raise CloseoutError(409, "项目已验收、已终止或资料已删除")
    if not reason.strip():
        raise CloseoutError(422, "请填写终止原因")
    today = today or date.today()
    p.terminated_on, p.terminate_reason = today, reason
    p.deletion_due_on = retention.add_months(today, p.retention_months)
    closed = 0
    for c in db.scalars(select(Confirmation).where(Confirmation.project_id == p.id, Confirmation.status == "open")):
        c.status, c.resolution = "resolved", TERMINATED_NOTE
        closed += 1
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=p.code,
                 reason_len=len(reason), closed_cards=closed, deletion_due_on=p.deletion_due_on.isoformat())
    db.commit()
    return p
