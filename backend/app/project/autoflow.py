"""交付流程自动推进（二期 C5：P6、P8、P10，决策记录 2026-11-15“人工环节复查的落地范围”）。

三条平台规则（AUTO_FLOW=false 时全部关闭，顾问照旧手动操作；手动发起、取消、评审、提交都不受影响）：

- P6 自动发起交付链（scan_runs，worker 每小时）：项目未删除、未验收、从来没有运行过交付链，证据已齐且最近
  AUTO_RUN_QUIET_HOURS 小时内没有新证据（等顾问一批传完）时，自动发起一次。证据齐：标准档至少一份未删除、
  不是仅现场使用的证据，上传的文件都已解析完（还在解析的先不发起）；严格档有有效的客户书面同意和至少一张
  未撤下的摘要卡。发起人记为最近一条证据的录入人（运行要按成员身份读证据）；不再是成员的，改由项目经理之一。
  发起前先按 runs.preflight 检查（已签署、Agent 未就绪、配置有问题的不发起，也不写拒绝审计）。
  顾问取消过的运行也算运行过，不会再自动发起；任何交付件已经有正文（顾问在手写）的也不自动发起。
- P8 自动发起质量评审（scan_reviews，worker 每小时；交付链结束时也立即查一次本项目）：交付件在起草中、
  每章都有正文、项目没有未结束的运行、没有进行中的评审、当前修订还没评审过（同一修订只排一次，失败的也不重排），
  并且最后一次写入是 Agent 写的（交付链写完就评），或者顾问最后一次修改已经过了 AUTO_REVIEW_IDLE_HOURS 小时。
  发起人记为最后写入的人（交付链写入时是运行发起人）；不再是成员的不发起。
- P10 门禁通过自动提交签署（after_review，评审完成时）：交付件在起草中、评审针对当前修订、门禁通过时，
  平台转为“待签署”，提交记录的提交人写 platform。签署、退回仍由项目经理做。

审计照旧由 runs.start / quality.request_review / quality.submit 写，另记 trigger（哪条规则判的），不记原文。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.db import utcnow
from ..core.permissions import PROJECT_MANAGER, membership
from ..models import (AgentRun, Deliverable, DeliverableSection, Evidence, Project, ProjectMember, QualityReview,
                      SectionVersion, SummaryCard, User)

RULE_RUN = "auto:evidence-ready"
RULE_REVIEW_CHAIN = "auto:chain-done"
RULE_REVIEW_IDLE = "auto:idle"
RULE_SUBMIT = "auto:gate-passed"
PLATFORM = "platform"


def enabled() -> bool:
    return get_settings().auto_flow


def _aware(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


def _member(db: Session, project: Project, username: str) -> User | None:
    u = db.scalar(select(User).where(User.username == username)) if username else None
    if u is None or not u.is_active or membership(db, project.id, u.id) is None:
        return None
    return u


def _manager(db: Session, project: Project) -> User | None:
    for u in db.scalars(select(User).join(ProjectMember, ProjectMember.user_id == User.id)
                        .where(ProjectMember.project_id == project.id, ProjectMember.project_role == PROJECT_MANAGER)
                        .order_by(ProjectMember.added_at, User.id)):
        if u.is_active:
            return u
    return None


def _evidence_ready(db: Session, project: Project) -> tuple[str, datetime] | None:
    """证据齐了返回 (最近一条的录入人, 时间)；没齐或文件还在解析返回 None。"""
    from . import cards

    rows: list[tuple[str, datetime]] = []
    if project.tier == "strict":
        if cards.active_consent(db, project) is None:
            return None
        rows = [(c.uploaded_by, c.created_at) for c in db.scalars(
            select(SummaryCard).where(SummaryCard.project_id == project.id, SummaryCard.withdrawn.is_(False)))]
    else:
        for e in db.scalars(select(Evidence).where(Evidence.project_id == project.id, Evidence.deleted_at.is_(None))):
            doc = e.document
            if doc is not None:
                if doc.local_only:
                    continue
                if doc.status == "received":
                    return None                    # 还有文件在解析：等解析完再发起
                if doc.status != "parsed":
                    continue
            rows.append((e.created_by, e.created_at))
    if not rows:
        return None
    by, at = max(rows, key=lambda r: _aware(r[1]))
    return by, _aware(at)


def scan_runs(db: Session, project: Project | None = None, now: datetime | None = None) -> list[str]:
    """P6：返回本次自动发起了交付链的项目代号（可重复执行；project 只查这一个项目）。"""
    from . import runs

    if not enabled():
        return []
    now = now or utcnow()
    quiet = timedelta(hours=get_settings().auto_run_quiet_hours)
    started: list[str] = []
    ran = select(AgentRun.project_id).distinct()
    q = select(Project).where(Project.deleted_at.is_(None), Project.accepted_on.is_(None), Project.id.not_in(ran))
    if project is not None:
        q = q.where(Project.id == project.id)
    for p in db.scalars(q.order_by(Project.id)).all():
        if db.scalar(select(DeliverableSection.id).join(Deliverable, Deliverable.id == DeliverableSection.deliverable_id)
                     .where(Deliverable.project_id == p.id, DeliverableSection.current_version > 0).limit(1)) is not None:
            continue                               # 顾问已经在手写交付件：不自动发起，免得覆盖
        ready = _evidence_ready(db, p)
        if ready is None or now - ready[1] < quiet:
            continue
        user = _member(db, p, ready[0]) or _manager(db, p)
        if user is None or runs.preflight(db, p) is not None:
            continue
        try:
            runs.start(db, user, p, trigger=RULE_RUN)
        except runs.RunError:
            db.rollback()
            continue
        started.append(p.code)
    return started


def _last_version(db: Session, d: Deliverable) -> SectionVersion | None:
    return db.scalar(select(SectionVersion).join(DeliverableSection, DeliverableSection.id == SectionVersion.section_id)
                     .where(DeliverableSection.deliverable_id == d.id)
                     .order_by(SectionVersion.created_at.desc(), SectionVersion.id.desc()).limit(1))


def _review_rule(db: Session, d: Deliverable, now: datetime) -> str:
    """该自动评审时返回规则名，否则空串。"""
    from . import quality

    if d.status != "drafting" or not d.sections or not all(s.current_version for s in d.sections):
        return ""
    if db.scalar(select(QualityReview.id).where(QualityReview.deliverable_id == d.id,
                                                (QualityReview.status.in_(quality.ACTIVE))
                                                | (QualityReview.revision == d.revision)).limit(1)) is not None:
        return ""
    last = _last_version(db, d)
    if last is None:
        return ""
    if last.agent_code:
        return RULE_REVIEW_CHAIN
    idle = timedelta(hours=get_settings().auto_review_idle_hours)
    return RULE_REVIEW_IDLE if now - _aware(last.created_at) >= idle else ""


def scan_reviews(db: Session, project: Project | None = None, now: datetime | None = None) -> list[str]:
    """P8：返回自动发起了评审的交付件（项目代号/交付件编号，可重复执行）。"""
    from . import quality, runs

    if not enabled():
        return []
    now = now or utcnow()
    q = (select(Deliverable).join(Project, Project.id == Deliverable.project_id)
         .where(Project.deleted_at.is_(None), Deliverable.status == "drafting").order_by(Deliverable.id))
    if project is not None:
        q = q.where(Deliverable.project_id == project.id)
    made: list[str] = []
    for d in db.scalars(q).all():
        p = d.project
        if db.scalar(select(AgentRun.id).where(AgentRun.project_id == p.id, AgentRun.status.in_(runs.ACTIVE))
                     .limit(1)) is not None:
            continue
        rule = _review_rule(db, d, now)
        if not rule:
            continue
        user = _member(db, p, d.updated_by)
        if user is None:
            continue
        try:
            quality.request_review(db, user, p, d, trigger=rule)
        except quality.QualityError:
            db.rollback()
            continue
        made.append(f"{p.code}/{d.code}")
    return made


def after_run(db: Session, run: AgentRun) -> None:
    """交付链结束（done）：立即给本项目写好的交付件排评审，不等每小时的扫描。"""
    if run.status == "done":
        project = db.get(Project, run.project_id)
        if project is not None:
            scan_reviews(db, project)


def after_review(db: Session, review: QualityReview) -> bool:
    """P10：评审完成且门禁通过时自动提交签署。返回是否提交了。"""
    from . import quality

    if not enabled() or review.status != "done":
        return False
    d = db.get(Deliverable, review.deliverable_id)
    if d is None or d.status != "drafting" or review.revision != d.revision or not quality.gate(db, d).ok:
        return False
    project = db.get(Project, review.project_id)
    user = _member(db, project, review.created_by) or _manager(db, project)
    if user is None:
        return False
    try:
        quality.submit(db, user, project, d, "平台自动提交：质量门禁已通过", actor=PLATFORM, trigger=RULE_SUBMIT)
    except quality.QualityError:
        db.rollback()
        return False
    return True
