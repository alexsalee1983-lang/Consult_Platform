"""三期 B17②“我的任务”：各类待办合一，按影响 × 不确定度排序，每日额度，退回排队由 Agent 重写。

四期试用反馈（负责人 11-25）：原“工作台”并入这里，平台只剩一个待办入口、一套计数口径；/api/workbench 只做兼容。
类别（KINDS，顺序即筛选条顺序）：确认卡、待签署、知识内容审核、知识合规审核、待复审、去标识确认、资料删除确认，
以及抽检、公司做法确认、来源确认、客户自评（四期 E3②：自评包到期前一天、当天提醒发包人，逾期 2 天升级项目经理，
阶段由 worker 的 selfeval.scan_reminders 推进）。
- 只列本人有权处理的（项目类只看本人是成员的项目；待签署只给项目经理；知识类按知识角色；
  去标识给知识管理员；资料删除给项目负责人，不必是成员，只给项目代号）；
  本人按规则不能处理的（作者不能审自己的版本、上传者不能确认去标识、发起人不能确认删除）照样列出、
  写明要谁处理，排在最后，不占额度。
- 知识审核按角色拆成内容审核（知识专家，一键进专家工作台对应卡）和合规审核（知识管理员）；
  一人兼两角色时两项都列。退回任一项即整版退回重写，两项一起隐去。
- 公司做法确认（#283）、来源确认（#297）的接口未合并：先留空位（available=False），不报错。
- 影响 = 引用该对象的在办项目数 + 交付件数（未删除、未验收）；不确定度 = Agent 置信度的补数或“存疑”标记，
  拿不到的按 0。分数 = (1 + 影响权重 × 影响) × (1 + 不确定度权重 × 不确定度)；同分按创建时间从早到晚。
- 每日额度：前 N 项（不含被拦的）当天处理，超出的折叠（folded）。权重和额度在配置里（TASKS_*）。
- 元数据预填：每项带 fields，拿不准的（空着或 Agent 标了存疑）uncertain=True，界面标黄，只做显示。
- 退回：记 task_returns 一条、jobs.enqueue(REWRITE_JOB)；已退回、未重写完的对象不再列出。
  本单只做排队入口，不改 Agent md，处理器只把状态推进到 waiting_agent，等 Agent 接上后重写并置 done。
退回意见先过 guard，只存库；审计只记类别、编号和长度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from ..core.config import get_settings
from ..core.permissions import KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT, PROJECT_LEAD, PROJECT_MANAGER, roles_of
from ..kb import review as rv
from ..models import (Confirmation, Deliverable, DeliverableSection, Project, ProjectDeletion, ProjectMember,
                      SectionVersion, TaskReturn, User)
from ..models.kb import KbItem, KbUpload, KbVersion
from ..models.survey import SelfEvalPack
from ..services import jobs

KINDS: dict[str, str] = {
    "confirmation": "确认卡",
    "deliverable_review": "待签署",
    "kb_content": "知识内容审核",
    "kb_compliance": "知识合规审核",
    "recheck": "待复审",
    "deid": "去标识确认",
    "deletion": "资料删除确认",
    "sample": "抽检",
    "practice": "公司做法确认",
    "source_confirm": "来源确认",
    "selfeval": "客户自评",
    "kb_alarm": "入库报警",                             # 四期 B41⑥：采集回传入库数为 0 或正文率低（app/kb/gap_loop.py）
}
UNAVAILABLE = {"practice", "source_confirm"}           # 接口未合并（#283、#297），先留空位
KB_REVIEW_KINDS = ("kb_content", "kb_compliance")
RETURNABLE = {*KB_REVIEW_KINDS, "kb_review", "sample", "recheck", "confirmation"}  # kb_review = 旧类名，兼容
REVIEW_KIND = {"content": "kb_content", "compliance": "kb_compliance"}
PENDING_RETURN = ("queued", "waiting_agent")
REWRITE_JOB = "tasks.rewrite"
KB_FIELDS = (("ref_document", "依据文件"), ("ref_version", "版本"), ("ref_code", "文号或标准号"),
             ("applicability", "适用条件"), ("review_date", "复审日期"))


class TaskError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class Weights:
    impact: float
    uncertainty: float
    quota: int


@dataclass
class Task:
    kind: str
    ref_id: int
    title: str
    link: str
    project_code: str = ""
    impact: float = 0.0
    uncertainty: float = 0.0
    blocked: str = ""
    created_ts: float = 0.0
    fields: list[dict] = field(default_factory=list)
    score: float = 0.0
    folded: bool = False


def weights() -> Weights:
    s = get_settings()
    return Weights(impact=s.tasks_impact_weight, uncertainty=s.tasks_uncertainty_weight, quota=s.tasks_daily_quota)


def score(t: Task, w: Weights) -> float:
    return (1 + w.impact * max(t.impact, 0.0)) * (1 + w.uncertainty * max(t.uncertainty, 0.0))


def rank(tasks: list[Task], w: Weights) -> list[Task]:
    for t in tasks:
        t.score = round(score(t, w), 4)
    return sorted(tasks, key=lambda t: (bool(t.blocked), -t.score, t.created_ts))


def apply_quota(ranked: list[Task], quota: int) -> int:
    """前 quota 项（不含被拦的）当天处理，其余折叠；返回折叠数。"""
    n = folded = 0
    for t in ranked:
        if t.blocked:
            continue
        n += 1
        t.folded = n > quota
        folded += t.folded
    return folded


def _ts(dt: datetime | None) -> float:
    return dt.timestamp() if dt else 0.0


def _field(name: str, label: str, value: object, uncertain: bool = False) -> dict:
    text = "" if value is None else str(value)
    return {"name": name, "label": label, "value": text, "uncertain": uncertain or not text}


def _live_usage(db: Session) -> dict[str, tuple[set[int], set[int]]]:
    """知识条目 doc_id → (在办项目, 交付件)：章节当前版本引用到该条目，项目未删除、未验收。"""
    rows = db.execute(select(Deliverable.project_id, Deliverable.id, SectionVersion.sources)
                      .join(Project, Project.id == Deliverable.project_id)
                      .join(DeliverableSection, DeliverableSection.deliverable_id == Deliverable.id)
                      .join(SectionVersion, (SectionVersion.section_id == DeliverableSection.id)
                            & (SectionVersion.version == DeliverableSection.current_version))
                      .where(Project.deleted_at.is_(None), Project.accepted_on.is_(None))).all()
    out: dict[str, tuple[set[int], set[int]]] = {}
    for pid, did, src in rows:
        for x in (src or {}).get("knowledge", []):
            if isinstance(x, dict) and x.get("found") and x.get("ref"):
                ps, ds = out.setdefault(str(x["ref"]), (set(), set()))
                ps.add(pid)
                ds.add(did)
    return out


def _kb_uncertainty(v: KbVersion) -> tuple[float, set[str]]:
    """Agent 存疑（decision=flag）= 1；否则取 Agent 给的置信度补数；拿不到为 0。另返回 Agent 标存疑的字段名。"""
    unc, unsure = 0.0, set()
    for r in v.reviews:
        f = r.findings if isinstance(r.findings, dict) else {}
        unsure |= {x for x in f.get("uncertain_fields", []) if isinstance(x, str)}
        if r.decision == "flag":
            unc = 1.0
        conf = f.get("confidence")
        if isinstance(conf, int | float) and 0 <= conf <= 1:
            unc = max(unc, 1 - float(conf))
    return unc, unsure


KB_LINK = {"kb_content": "/kb/bench?v={id}", "kb_compliance": "/kb/review?version={id}",
           "recheck": "/kb/review?tab=recheck", "sample": "/kb/review?tab=sample"}


def _kb_task(kind: str, v: KbVersion, usage: dict, blocked: str, at: datetime | None) -> Task:
    ps, ds = usage.get(v.item.doc_id, (set(), set()))
    unc, unsure = _kb_uncertainty(v)
    fields = [_field(n, label, getattr(v, n), n in unsure) for n, label in KB_FIELDS]
    link = KB_LINK.get(kind, "/kb/review").format(id=v.id)
    return Task(kind=kind, ref_id=v.id, title=f"{v.item.doc_id} v{v.version} {v.title}", link=link,
                impact=len(ps) + len(ds), uncertainty=unc, blocked=blocked, created_ts=_ts(at or v.created_at),
                fields=fields)


def _norm(kind: str) -> str:
    """知识审核的两个角色（及旧类名 kb_review）退回的是同一个版本，归一后比较。"""
    return "kb_review" if kind in KB_REVIEW_KINDS else kind


def _returned(db: Session) -> set[tuple[str, int]]:
    return {(_norm(k), r) for k, r in db.execute(select(TaskReturn.kind, TaskReturn.ref_id)
                                                 .where(TaskReturn.status.in_(PENDING_RETURN))).all()}


def _memberships(db: Session, user: User) -> list:
    return list(db.execute(select(ProjectMember.project_id, ProjectMember.project_role, Project.code)
                           .join(Project, Project.id == ProjectMember.project_id)
                           .where(ProjectMember.user_id == user.id, Project.deleted_at.is_(None))).all())


def member_projects(db: Session, user: User) -> int:
    """本人所在的未删除项目数（为 0 时界面给“你还不是任何项目成员”的引导）。"""
    return len(_memberships(db, user))


def collect(db: Session, user: User) -> list[Task]:
    roles = roles_of(db, user.id)
    me = user.username
    tasks: list[Task] = []
    live = _memberships(db, user)
    codes = {m.project_id: m.code for m in live}
    managed = {m.project_id for m in live if m.project_role == PROJECT_MANAGER}

    if codes:
        for c in db.scalars(select(Confirmation).where(Confirmation.status == "open",
                                                       Confirmation.project_id.in_(codes))):
            code = codes[c.project_id]
            unsure = not c.recommendation                   # Agent 没给推荐 = 存疑
            tasks.append(Task(kind="confirmation", ref_id=c.id, title=c.title, project_code=code,
                              link=f"/project/{code}", impact=1 + bool(c.deliverable_code),
                              uncertainty=1.0 if unsure else 0.0, created_ts=_ts(c.created_at),
                              fields=[_field("recommendation", "推荐选项", c.recommendation),
                                      _field("deliverable_code", "关联交付件", c.deliverable_code)]))
    if codes:
        from .selfeval import OPEN, STAGE_TEXT

        for sp in db.scalars(select(SelfEvalPack).where(SelfEvalPack.project_id.in_(codes),
                                                        SelfEvalPack.status.in_(OPEN), SelfEvalPack.remind_stage > 0)):
            if sp.created_by != me and not (sp.remind_stage == 3 and sp.project_id in managed):
                continue
            code = codes[sp.project_id]
            tasks.append(Task(kind="selfeval", ref_id=sp.id, project_code=code, link=f"/project/{code}",
                              title=f"客户自评包 {sp.code}（{sp.respondent_role}）{STAGE_TEXT[sp.remind_stage]}",
                              impact=2.0 if sp.remind_stage == 3 else 1.0, created_ts=_ts(sp.created_at),
                              fields=[_field("due_date", "截止日", sp.due_date.isoformat())]))
    if managed:
        for d in db.scalars(select(Deliverable).where(Deliverable.status == "review",
                                                      Deliverable.project_id.in_(managed))):
            code = codes[d.project_id]
            tasks.append(Task(kind="deliverable_review", ref_id=d.id, title=f"{d.code} {d.title}", project_code=code,
                              link=f"/project/{code}/deliverables/{d.code}", impact=2.0))

    if roles & rv.KNOWLEDGE_ROLES:
        usage = _live_usage(db)
        opts = (selectinload(KbVersion.item), selectinload(KbVersion.reviews))
        for v in db.scalars(select(KbVersion).where(KbVersion.status == "in_review").options(*opts)):
            done = {r.review_role for r in v.reviews if r.decision == "approve"}
            mine = me in rv._authors(v) and not rv.self_review_allowed(v)
            for role, need in rv.ROLE_OF.items():
                if role in rv.required_roles(v) and need in roles and role not in done:
                    other = "知识专家" if need == KNOWLEDGE_EXPERT else "知识管理员"
                    tasks.append(_kb_task(REVIEW_KIND[role], v, usage,
                                          f"你是作者，需另一位{other}处理" if mine else "", v.submitted_at))
        if KNOWLEDGE_EXPERT in roles:
            current = (select(KbVersion).join(KbItem, KbItem.current_version_id == KbVersion.id)
                       .where(KbItem.retired.is_(False)).options(*opts))
            for v in db.scalars(current.where(KbVersion.sample_state.in_(("pending", "failed")))):
                tasks.append(_kb_task("sample", v, usage,
                                      "你是作者，需另一位知识专家抽检" if me in rv._authors(v) else "", v.published_at))
            for v in db.scalars(current.where(KbVersion.recheck_state.in_(("pending", "failed"))
                                              | KbVersion.single_review.is_(True))):
                tasks.append(_kb_task("recheck", v, usage,
                                      "你是作者，需另一位知识专家复查" if me in rv._authors(v) else "",
                                      v.recheck_at or v.published_at))
    if KNOWLEDGE_ADMIN in roles:
        for u in db.scalars(select(KbUpload).where(KbUpload.status == "deid_pending")):
            tasks.append(Task(kind="deid", ref_id=u.id, title=f"上传 #{u.id}", link="/kb/intake", impact=1.0,
                              created_ts=_ts(u.created_at),
                              blocked="你是上传者，需另一位知识管理员确认" if u.uploaded_by == me else ""))
        from ..models import KbAlarm

        for a in db.scalars(select(KbAlarm).where(KbAlarm.status == "open")):
            tasks.append(Task(kind="kb_alarm", ref_id=a.id, title=f"入库报警 #{a.id}（{a.count} 批）",
                              link="/kb/sources", impact=1.0, created_ts=_ts(a.created_at)))
    if PROJECT_LEAD in roles:
        for d in db.scalars(select(ProjectDeletion).where(ProjectDeletion.status == "pending")
                            .options(selectinload(ProjectDeletion.project))):
            tasks.append(Task(kind="deletion", ref_id=d.id, title=f"删除申请 #{d.id}", project_code=d.project.code,
                              link="/deletions", impact=1.0, created_ts=_ts(d.requested_at),
                              blocked="你是发起人，需另一位项目负责人确认" if d.requested_by == me else ""))
    gone = _returned(db)
    return [t for t in tasks if (_norm(t.kind), t.ref_id) not in gone]


def summary(db: Session, user: User) -> dict:
    w = weights()
    ranked = rank(collect(db, user), w)
    folded = apply_quota(ranked, w.quota)
    counts = {k: sum(t.kind == k for t in ranked) for k in KINDS}
    return {"quota": w.quota, "folded": folded, "weights": {"impact": w.impact, "uncertainty": w.uncertainty},
            "kinds": {k: {"name": n, "available": k not in UNAVAILABLE, "count": counts[k]} for k, n in KINDS.items()},
            "tasks": ranked, "member_projects": member_projects(db, user)}


def return_task(db: Session, user: User, kind: str, ref_id: int, reason: str) -> TaskReturn:
    """退回：对象必须在本人的待办里且本人能处理（否则 404，不暴露是否存在）。"""
    if kind not in RETURNABLE:
        raise TaskError(422, f"“{KINDS.get(kind, kind)}”不能从这里退回")
    same = [*KB_REVIEW_KINDS, "kb_review"] if _norm(kind) == "kb_review" else [kind]
    if db.scalar(select(TaskReturn.id).where(TaskReturn.kind.in_(same), TaskReturn.ref_id == ref_id,
                                             TaskReturn.status.in_(PENDING_RETURN))):
        raise TaskError(409, "已退回，等 Agent 重写")
    task = next((t for t in collect(db, user)
                 if _norm(t.kind) == _norm(kind) and t.ref_id == ref_id and not t.blocked), None)
    if task is None:
        raise TaskError(404, "没有这项待办")
    pid = db.scalar(select(Project.id).where(Project.code == task.project_code)) if task.project_code else None
    ret = TaskReturn(kind=kind, ref_id=ref_id, project_id=pid, user_id=user.id, reason=reason, status="queued")
    db.add(ret)
    db.flush()
    ret.job_id = jobs.enqueue(db, REWRITE_JOB, {"return_id": ret.id}, created_by=user.username,
                              project_code=task.project_code)
    db.commit()
    return ret


@jobs.handler(REWRITE_JOB)
def _rewrite(payload: dict, ctx: jobs.JobContext) -> None:
    """排队入口：Agent 重写接上前只把状态推进到 waiting_agent（不改 Agent md，不调模型）。"""
    from ..core.db import SessionLocal

    with SessionLocal() as db:
        ret = db.get(TaskReturn, int(payload.get("return_id", 0)), with_for_update=True)
        if ret is not None and ret.status == "queued":
            ret.status = "waiting_agent"
        db.commit()


def delete_for_project(db: Session, project_id: int) -> int:
    return db.execute(delete(TaskReturn).where(TaskReturn.project_id == project_id)).rowcount or 0
