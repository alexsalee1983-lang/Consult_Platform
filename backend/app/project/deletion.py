"""项目资料删除与删除记录（M4 ②）。

依据：docs/客户数据告知模板与合同条款.md 第六节、第 X.3 条（到期经人工确认后删除并形成删除记录；客户可书面要求提前删除），
决策记录 2026-11-13（删除发起与确认、删除前的经验回流检查点）。

规则：
- 到期删除：平台规则自动发起（scan_due，worker 每小时跑一次，也可 `python -m app.cli deletion-scan`），
  不是大模型 Agent；requested_by="platform"，应完成日期 = 保留期到期日。
  项目经理更正保留期后不再到期的，待确认的到期申请自动撤销（retention.accept 调用 cancel_if_not_due）。
- 提前删除：客户书面要求，由本项目的项目经理发起（写文号、收到日期，文号先过机密级检测、不写客户名称），
  应完成日期 = 收到日期起第 EARLY_WORKING_DAYS 个工作日（周一至周五，不扣法定节假日，节前请留余量）。
  项目负责人可以撤销提前删除申请（客户撤回时，写理由）；到期申请不能撤销，只能由项目经理按约定更正保留期。
- 确认：持“项目负责人”角色（project_lead）的人确认，不必是项目成员；不能是发起人。确认时必须选经验回流情况：
  done 已回流（填投料编号，即知识区上传编号）/ none 无可回流内容 / client_refused 客户不同意回流；
  项目标了“客户不同意回流”的只能选 client_refused。回流不阻塞删除。
- 执行：确认后排队后台任务 project.delete（可重复执行）：删对象存储项目区“项目代号/”下的全部文件，
  删证据、资料、交付件（含已签署的）及全部章节版本、质量评审、签署记录、驳回记录、确认卡、交付链运行、对话；
  移除全部项目成员，项目只留代号、档位、保留期和删除时间（代号不再复用）。审计日志、大模型用量（只有计数）保留。
- 同一项目同时只能有一个未结束的申请（pending / confirmed）。
- 删除记录（record）可交给客户：项目代号、档位、删除范围、计数、发起和确认人、日期、回流情况（只记编号）。
- 审计只记编号、日期、计数和回流情况，不记文号以外的任何客户内容（文号也不进审计）。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import PROJECT_LEAD, PROJECT_MANAGER, membership, roles_of
from ..models import (
    AgentRun,
    AgentRunStep,
    Confirmation,
    Conversation,
    Deliverable,
    DeliverableSection,
    Evidence,
    KbUpload,
    Message,
    Project,
    ProjectDeletion,
    ProjectDocument,
    ProjectMember,
    QualityReview,
    ReviewWaiver,
    SectionVersion,
    SignOff,
    User,
)
from ..services import guard, jobs, storage
from . import retention

JOB_KIND = "project.delete"
PLATFORM = "platform"
EARLY_WORKING_DAYS = 10
MAX_FLOWBACK_REFS = 20

EXPIRY, EARLY = "expiry", "early"
PENDING, CONFIRMED, DONE, CANCELLED = "pending", "confirmed", "done", "cancelled"
OPEN = (PENDING, CONFIRMED)
FLOWBACK = {"done": "已回流", "none": "无可回流内容", "client_refused": "客户不同意回流"}
KIND_NAMES = {EXPIRY: "保留期到期", EARLY: "客户书面要求提前删除"}

SCOPE = ("对象存储中的客户资料原件与解析结果", "证据（资料、访谈纪要、现场观察）", "交付件（含已签署的最终版本）及全部章节版本",
         "质量评审、签署与驳回记录", "确认卡", "交付链运行记录", "顾问与 Agent 的对话", "项目成员关系")
KEPT = ("项目代号、档位、验收日和保留期", "本删除记录", "审计日志（只有编号、计数和哈希，不含客户内容）",
        "大模型用量统计（只有次数和字数）")


class DeletionError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def add_working_days(d: date, n: int) -> date:
    """从 d 起数 n 个工作日（周一至周五，不含 d 当天）。"""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def open_request(db: Session, project_id: int) -> ProjectDeletion | None:
    return db.scalar(select(ProjectDeletion).where(ProjectDeletion.project_id == project_id,
                                                   ProjectDeletion.status.in_(OPEN)))


def _lock_project(db: Session, p: Project) -> None:
    db.refresh(p, with_for_update=True)       # PostgreSQL 行锁：同一项目的发起、确认串行，避免出现两个未结束的申请


def _is_lead(db: Session, user: User) -> bool:
    return PROJECT_LEAD in roles_of(db, user.id)


# ---- 发起 ----

def scan_due(db: Session, today: date | None = None) -> list[ProjectDeletion]:
    """平台规则：为已到期、还没删除、没有未结束申请的项目发起到期删除。可重复执行。"""
    today = today or date.today()
    made = []
    due = db.scalars(select(Project).where(Project.deleted_at.is_(None), Project.deletion_due_on.is_not(None),
                                           Project.deletion_due_on <= today).order_by(Project.id)).all()
    for p in due:
        _lock_project(db, p)
        if p.deleted_at is not None or open_request(db, p.id) is not None \
                or retention.status(p, today).state != retention.DUE:
            db.commit()
            continue
        d = ProjectDeletion(project_id=p.id, kind=EXPIRY, status=PENDING, requested_by=PLATFORM,
                            due_by=p.deletion_due_on, flowback_refs=[], counts={})
        db.add(d)
        db.commit()
        audit.record(db, actor=PLATFORM, action="project.deletion.request", result="allowed", project_code=p.code,
                     deletion_id=d.id, kind=EXPIRY, due_by=d.due_by.isoformat())
        made.append(d)
    return made


def request_early(db: Session, user: User, p: Project, request_ref: str, received_on: date,
                  today: date | None = None) -> ProjectDeletion:
    """客户书面要求提前删除：项目经理发起。"""
    action = "project.deletion.request"
    m = membership(db, p.id, user.id)
    if m is None or m.project_role != PROJECT_MANAGER:
        audit.record(db, actor=user.username, action=action, result="denied", project_code=p.code,
                     reason="not_manager")
        raise DeletionError(403, "只有本项目的项目经理能发起提前删除")
    ref = request_ref.strip()
    if not ref:
        raise DeletionError(422, "请填写客户书面要求的文号或编号（不写客户名称）")
    if received_on > (today or date.today()):
        raise DeletionError(422, "收到日期不能晚于今天")
    result = guard.check(ref)
    if result.blocked:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=p.code,
                     reason="guard", categories=sorted({f.kind for f in result.findings}))
        raise DeletionError(422, f"{result.summary()}，没有保存")
    _lock_project(db, p)
    if open_request(db, p.id) is not None:
        db.commit()
        raise DeletionError(409, "这个项目已有未完成的删除申请")
    d = ProjectDeletion(project_id=p.id, kind=EARLY, status=PENDING, requested_by=user.username,
                        request_ref=ref[:64], received_on=received_on,
                        due_by=add_working_days(received_on, EARLY_WORKING_DAYS), flowback_refs=[], counts={})
    db.add(d)
    db.commit()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=p.code,
                 deletion_id=d.id, kind=EARLY, received_on=received_on.isoformat(), due_by=d.due_by.isoformat())
    return d


def cancel_if_not_due(db: Session, actor: str, p: Project) -> None:
    """保留期更正后不再到期：撤销待确认的到期申请（已确认、执行中的不撤销）。"""
    if retention.status(p).state == retention.DUE:
        return
    for d in db.scalars(select(ProjectDeletion).where(ProjectDeletion.project_id == p.id,
                                                      ProjectDeletion.kind == EXPIRY,
                                                      ProjectDeletion.status == PENDING)):
        d.status, d.cancelled_by, d.cancel_reason = CANCELLED, actor, "保留期已更正，未到期"
        db.commit()
        audit.record(db, actor=actor, action="project.deletion.cancel", result="allowed", project_code=p.code,
                     deletion_id=d.id, reason="retention_corrected")


# ---- 项目负责人：确认、撤销 ----

def get_for_lead(db: Session, user: User, deletion_id: int, action: str) -> ProjectDeletion:
    if not _is_lead(db, user):
        audit.record(db, actor=user.username, action=action, result="denied", reason="not_project_lead",
                     deletion_id=deletion_id)
        raise DeletionError(403, "只有项目负责人能确认或撤销删除")
    d = db.get(ProjectDeletion, deletion_id)
    if d is None:
        raise DeletionError(404, "删除申请不存在")
    return d


def confirm(db: Session, user: User, deletion_id: int, flowback: str, flowback_refs: list[int]) -> ProjectDeletion:
    action = "project.deletion.confirm"
    d = get_for_lead(db, user, deletion_id, action)
    p = d.project
    if flowback not in FLOWBACK:
        raise DeletionError(422, "请选择经验回流情况：已回流 / 无可回流内容 / 客户不同意回流")
    refs = sorted({int(x) for x in flowback_refs})
    if flowback == "done":
        if not refs:
            raise DeletionError(422, "选“已回流”时请填投料编号")
        if len(refs) > MAX_FLOWBACK_REFS:
            raise DeletionError(422, f"投料编号最多 {MAX_FLOWBACK_REFS} 个")
        found = set(db.scalars(select(KbUpload.id).where(KbUpload.id.in_(refs))))
        if missing := [x for x in refs if x not in found]:
            raise DeletionError(422, f"投料编号不存在：{', '.join(map(str, missing))}")
    else:
        refs = []
    if not p.flowback_allowed and flowback != "client_refused":
        raise DeletionError(422, "这个项目标记了“客户不同意回流”，回流情况只能选“客户不同意回流”")
    _lock_project(db, p)
    db.refresh(d, with_for_update=True)
    if d.status != PENDING:
        db.commit()
        raise DeletionError(409, "这个申请不在待确认状态")
    if d.requested_by == user.username:
        db.commit()
        audit.record(db, actor=user.username, action=action, result="denied", project_code=p.code,
                     deletion_id=d.id, reason="same_person")
        raise DeletionError(403, "发起人不能确认自己发起的删除，请由另一位项目负责人确认")
    d.status, d.confirmed_by, d.confirmed_at = CONFIRMED, user.username, utcnow()
    d.flowback, d.flowback_refs = flowback, refs
    db.commit()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=p.code, deletion_id=d.id,
                 kind=d.kind, flowback=flowback, flowback_refs=refs)
    d.job_id = jobs.enqueue(db, JOB_KIND, {"deletion_id": d.id}, created_by=user.username, project_code=p.code)
    db.commit()
    return d


def cancel(db: Session, user: User, deletion_id: int, reason: str) -> ProjectDeletion:
    action = "project.deletion.cancel"
    d = get_for_lead(db, user, deletion_id, action)
    if d.kind != EARLY:
        raise DeletionError(409, "到期删除不能撤销；保留期另有约定的，请项目经理更正保留期")
    if len("".join(reason.split())) < 4:
        raise DeletionError(422, "请写明撤销理由（例如客户书面撤回）")
    result = guard.check(reason)
    if result.blocked:
        raise DeletionError(422, f"{result.summary()}，没有保存")
    db.refresh(d, with_for_update=True)
    if d.status != PENDING:
        db.commit()
        raise DeletionError(409, "这个申请不在待确认状态")
    d.status, d.cancelled_by, d.cancel_reason = CANCELLED, user.username, reason.strip()[:200]
    db.commit()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=d.project.code,
                 deletion_id=d.id)
    return d


# ---- 执行 ----

def purge(db: Session, p: Project) -> dict:
    """删除项目区的全部内容，返回各类计数。可重复执行（已删的计 0）。"""
    counts: dict[str, int] = {}
    pid = p.id

    # 对象存储：项目代号/ 下的全部对象，加上资料表里登记的键（防止键不在前缀下）
    keys = set()
    for doc in db.scalars(select(ProjectDocument).where(ProjectDocument.project_id == pid)):
        keys.update(k for k in (doc.object_key, doc.parsed_key) if k)
    if storage.enabled():
        keys.update(storage.list_keys("project", f"{p.code}/"))
        for k in sorted(keys):
            storage.delete("project", k)
    elif keys:
        raise jobs.JobError("对象存储未启用，项目文件删不掉，稍后重试")
    counts["files"] = len(keys)

    def drop(model, *where) -> int:
        return db.execute(delete(model).where(*where)).rowcount or 0

    conv = select(Conversation.id).where(Conversation.project_id == pid)
    counts["messages"] = drop(Message, Message.conversation_id.in_(conv))
    counts["conversations"] = drop(Conversation, Conversation.project_id == pid)
    runs = select(AgentRun.id).where(AgentRun.project_id == pid)
    drop(AgentRunStep, AgentRunStep.run_id.in_(runs))
    counts["runs"] = drop(AgentRun, AgentRun.project_id == pid)
    drop(ReviewWaiver, ReviewWaiver.project_id == pid)
    counts["sign_offs"] = drop(SignOff, SignOff.project_id == pid)
    counts["quality_reviews"] = drop(QualityReview, QualityReview.project_id == pid)
    counts["confirmations"] = drop(Confirmation, Confirmation.project_id == pid)
    dels = select(Deliverable.id).where(Deliverable.project_id == pid)
    secs = select(DeliverableSection.id).where(DeliverableSection.deliverable_id.in_(dels))
    counts["signed_deliverables"] = db.scalar(select(func.count()).select_from(Deliverable)
                                              .where(Deliverable.project_id == pid, Deliverable.status == "signed"))
    counts["section_versions"] = drop(SectionVersion, SectionVersion.section_id.in_(secs))
    drop(DeliverableSection, DeliverableSection.deliverable_id.in_(dels))
    counts["deliverables"] = drop(Deliverable, Deliverable.project_id == pid)
    counts["evidences"] = drop(Evidence, Evidence.project_id == pid)
    counts["documents"] = drop(ProjectDocument, ProjectDocument.project_id == pid)
    counts["members"] = drop(ProjectMember, ProjectMember.project_id == pid)
    return counts


@jobs.handler(JOB_KIND)
def _run(payload: dict, ctx: jobs.JobContext) -> None:
    from ..core.db import SessionLocal

    with SessionLocal() as db:
        d = db.get(ProjectDeletion, int(payload["deletion_id"]))
        if d is None:
            return
        db.refresh(d, with_for_update=True)
        if d.status != CONFIRMED:
            db.commit()
            return                                  # 已完成或已撤销：什么也不做
        p = d.project
        counts = purge(db, p)
        p.deleted_at = utcnow()
        d.status, d.completed_at = DONE, utcnow()
        # 重复执行时第二次计数为 0：保留第一次的结果
        d.counts = {k: max(v, (d.counts or {}).get(k, 0)) for k, v in counts.items()}
        db.commit()
        db.expire_all()
        audit.record(db, actor=PLATFORM, action="project.deletion.done", result="allowed", project_code=p.code,
                     deletion_id=d.id, confirmed_by=d.confirmed_by, flowback=d.flowback, counts=d.counts)


# ---- 回流标记、查询与删除记录 ----

def set_flowback_allowed(db: Session, user: User, p: Project, allowed: bool) -> Project:
    action = "project.flowback"
    m = membership(db, p.id, user.id)
    if m is None or m.project_role != PROJECT_MANAGER:
        audit.record(db, actor=user.username, action=action, result="denied", project_code=p.code,
                     reason="not_manager")
        raise DeletionError(403, "只有本项目的项目经理能修改回流标记")
    p.flowback_allowed = allowed
    db.commit()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=p.code, allowed=allowed)
    return p


def list_for_lead(db: Session, user: User, status: str = "open") -> list[ProjectDeletion]:
    if not _is_lead(db, user):
        raise DeletionError(403, "只有项目负责人能查看删除确认队列")
    stmt = select(ProjectDeletion).order_by(ProjectDeletion.due_by, ProjectDeletion.id)
    if status == "open":
        stmt = stmt.where(ProjectDeletion.status.in_(OPEN))
    elif status != "all":
        stmt = stmt.where(ProjectDeletion.status == status)
    return list(db.scalars(stmt.limit(500)))


def list_for_project(db: Session, p: Project) -> list[ProjectDeletion]:
    return list(db.scalars(select(ProjectDeletion).where(ProjectDeletion.project_id == p.id)
                           .order_by(ProjectDeletion.id.desc())))


def pending_counts(db: Session, p: Project) -> dict:
    """确认前给项目负责人看的计数（不含任何内容）。"""
    pid = p.id
    dels = select(Deliverable.id).where(Deliverable.project_id == pid)

    def n(model, *where) -> int:
        return db.scalar(select(func.count()).select_from(model).where(*where)) or 0

    return {"documents": n(ProjectDocument, ProjectDocument.project_id == pid),
            "evidences": n(Evidence, Evidence.project_id == pid),
            "deliverables_with_content": n(Deliverable, Deliverable.project_id == pid, Deliverable.revision > 0),
            "signed_deliverables": n(Deliverable, Deliverable.project_id == pid, Deliverable.status == "signed"),
            "section_versions": n(SectionVersion, SectionVersion.section_id.in_(
                select(DeliverableSection.id).where(DeliverableSection.deliverable_id.in_(dels)))),
            "conversations": n(Conversation, Conversation.project_id == pid),
            "runs": n(AgentRun, AgentRun.project_id == pid)}


def record(d: ProjectDeletion) -> dict:
    """可交给客户的删除记录。"""
    p = d.project
    return {"deletion_id": d.id, "project_code": p.code, "tier": p.tier, "kind": d.kind, "kind_name": KIND_NAMES[d.kind],
            "accepted_on": p.accepted_on, "retention_months": p.retention_months,
            "deletion_due_on": p.deletion_due_on, "request_ref": d.request_ref, "received_on": d.received_on,
            "requested_by": d.requested_by, "requested_at": d.requested_at, "due_by": d.due_by,
            "confirmed_by": d.confirmed_by, "confirmed_at": d.confirmed_at, "completed_at": d.completed_at,
            "on_time": d.completed_at is not None and d.completed_at.date() <= d.due_by,
            "status": d.status, "flowback": d.flowback, "flowback_name": FLOWBACK.get(d.flowback, ""),
            "flowback_refs": d.flowback_refs, "counts": d.counts, "scope": list(SCOPE), "kept": list(KEPT)}

