"""AI 质量事件台账规则（四期 F3；4A 架构设计 6.3）。

- 登记：任何业务角色（程序也可调 register()）；挂项目时必须是项目成员。只记描述的长度和摘要哈希。
- 分级 P1 / P2 / P3 与遏制时限（从登记起算）：P1 4 小时；P2 1 个工作日（跳过周六日）；P3 常规修复不设时限。
- 八步处置只追加：分级可重做（以最后一次为准），复盘即关闭，关闭后不再追加（409）。
- 处置人：项目负责人，或事件所属项目的项目经理；查看：再加登记人；其他人 404。
- 遏制联动：冻结导出见 export_frozen()；下线条目（只用于挂知识条目的事件）当场把条目下线（同知识库“下线条目”，
  审计 kb.retire），处置人须同时是知识管理员（不扩大下线权限），条目已下线时只记步骤；
  回滚 Agent 版本（四期 F3②e）当场把该 Agent 切到 md 版本库里的已合入版本（app/services/agent_md.py），
  处置人须是项目负责人（系统管理员与其他角色互斥，不能兼任处置人），撤销见 /api/agent-md/{代号}/unpin。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.permissions import KNOWLEDGE_ADMIN, PROJECT_LEAD, PROJECT_MANAGER, roles_of
from ..models import Project, ProjectMember, QualityIncident, QualityIncidentEvent, User

CATEGORIES = {
    "fabricated_citation": "编造或错引条款",
    "repealed_basis": "引用已废止依据",
    "fact_mismatch": "事实与证据不符",
    "qualification_wording": "资质边界用语",
    "cross_project_leak": "客户信息跨项目泄露",
    "improper_advice": "明显不当建议",
}
REF_KINDS = {"deliverable": "交付件", "kb_item": "知识条目", "run": "运行记录"}
LEVELS = ("P1", "P2", "P3")
STEPS = ("register", "grade", "contain", "assess", "correct", "root_cause", "prevent", "review")
ACTIONS = {"freeze_export": "冻结导出", "offline_item": "下线条目", "rollback_agent": "回滚 Agent 版本",
           "lower_autonomy": "降自主度"}
ROOT_CAUSES = {"knowledge": "知识", "retrieval": "检索", "md": "md", "model": "模型", "gate": "门禁",
               "human_signoff": "人工签署"}
NOTE_MAX = 2000


class IncidentError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def digest(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16] if s else ""


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def contain_due(level: str, registered: datetime) -> datetime | None:
    """遏制时限：P1 4 小时；P2 1 个工作日（落在周六日顺延到周一）；P3 和未分级不设。"""
    registered = _aware(registered)
    if level == "P1":
        return registered + timedelta(hours=4)
    if level == "P2":
        t = registered + timedelta(days=1)
        while t.weekday() >= 5:
            t += timedelta(days=1)
        return t
    return None


@dataclass
class View:
    incident: QualityIncident
    events: list[QualityIncidentEvent]

    @property
    def level(self) -> str | None:
        return next((e.level for e in reversed(self.events) if e.step == "grade"), None)

    @property
    def status(self) -> str:
        last = self.events[-1].step if self.events else "register"
        return {"register": "registered", "review": "closed"}.get(last, last)

    @property
    def contained_at(self) -> datetime | None:
        return next((_aware(e.created_at) for e in self.events if e.step == "contain"), None)

    @property
    def contain_due(self) -> datetime | None:
        return contain_due(self.level or "", self.incident.created_at)

    def overdue(self, now: datetime) -> bool:
        due = self.contain_due
        return due is not None and (self.contained_at or now) > due

    def out(self, now: datetime, with_events: bool = False) -> dict:
        i = self.incident
        d = {"id": i.id, "project": i.project_code, "category": i.category,
             "category_label": CATEGORIES.get(i.category, ""), "ref_kind": i.ref_kind, "ref_id": i.ref_id,
             "description_len": i.description_len, "description_sha": i.description_sha,
             "reported_by": i.reported_by, "created_at": _aware(i.created_at), "level": self.level,
             "status": self.status, "contain_due": self.contain_due, "contained_at": self.contained_at,
             "overdue": self.overdue(now),
             "root_cause": next((e.root_cause for e in reversed(self.events) if e.step == "root_cause"), None)}
        if with_events:
            d["events"] = [{"step": e.step, "level": e.level or None, "action": e.action or None,
                            "root_cause": e.root_cause or None, "note_len": e.note_len, "note_sha": e.note_sha,
                            "actor": e.actor, "created_at": _aware(e.created_at)} for e in self.events]
        return d


def _view(db: Session, inc: QualityIncident) -> View:
    ev = db.scalars(select(QualityIncidentEvent).where(QualityIncidentEvent.incident_id == inc.id)
                    .order_by(QualityIncidentEvent.id)).all()
    return View(inc, list(ev))


def _is_member(db: Session, code: str, user: User, manager_only: bool = False) -> bool:
    if not code:
        return False
    stmt = select(ProjectMember.project_id).join(Project, Project.id == ProjectMember.project_id) \
        .where(Project.code == code, ProjectMember.user_id == user.id)
    if manager_only:
        stmt = stmt.where(ProjectMember.project_role == PROJECT_MANAGER)
    return db.scalar(stmt) is not None


def _validate(category: str, ref_kind: str) -> None:
    if category not in CATEGORIES:
        raise IncidentError(422, "事件类别不在清单内")
    if ref_kind not in REF_KINDS:
        raise IncidentError(422, "关联对象只能是交付件、知识条目或运行记录")


def register(db: Session, user: User, *, category: str, ref_kind: str, ref_id: str, project: str = "",
             description: str = "") -> View:
    _validate(category, ref_kind)
    if project and not _is_member(db, project, user):
        raise IncidentError(404, "项目不存在")
    return _register(db, user.username, category=category, ref_kind=ref_kind, ref_id=ref_id, project=project,
                     description=description)


def register_system(db: Session, source: str, *, category: str, ref_kind: str, ref_id: str, project: str = "",
                    description: str = "") -> View:
    """程序自动登记（门禁、抽检命中时）：登记人记 system:<来源>，不做成员检查；说明同样只记长度和哈希。"""
    return _register(db, f"system:{source}", category=category, ref_kind=ref_kind, ref_id=ref_id, project=project,
                     description=description)


def _register(db: Session, actor: str, *, category: str, ref_kind: str, ref_id: str, project: str,
              description: str) -> View:
    _validate(category, ref_kind)
    inc = QualityIncident(project_code=project, category=category, ref_kind=ref_kind, ref_id=ref_id,
                          description_len=len(description), description_sha=digest(description),
                          reported_by=actor)
    db.add(inc)
    db.flush()
    db.add(QualityIncidentEvent(incident_id=inc.id, step="register", actor=actor))
    db.commit()
    audit.record(db, actor=actor, action="quality_incident.register", result="ok", project_code=project,
                 incident=inc.id, category=category, ref_kind=ref_kind, description_len=len(description),
                 description_sha=inc.description_sha)
    return _view(db, inc)


def _can_handle(db: Session, inc: QualityIncident, user: User) -> bool:
    return PROJECT_LEAD in roles_of(db, user.id) or _is_member(db, inc.project_code, user, manager_only=True)


def get(db: Session, user: User, incident_id: int, handle: bool = False) -> View:
    inc = db.get(QualityIncident, incident_id)
    if inc is None or not (_can_handle(db, inc, user) or (not handle and inc.reported_by == user.username)):
        raise IncidentError(404, "事件不存在")
    return _view(db, inc)


def add_event(db: Session, user: User, incident_id: int, *, step: str, level: str = "", action: str = "",
              root_cause: str = "", note: str = "", agent: str = "", version: str = "") -> View:
    v = get(db, user, incident_id, handle=True)
    if step not in STEPS[1:]:
        raise IncidentError(422, "处置步骤不在八步内")
    if step == "grade" and level not in LEVELS:
        raise IncidentError(422, "分级只能是 P1、P2、P3")
    if step == "contain" and action not in ACTIONS:
        raise IncidentError(422, "遏制措施不在清单内")
    if step == "root_cause" and root_cause not in ROOT_CAUSES:
        raise IncidentError(422, "根因类别不在清单内")
    if v.status == "closed":
        raise IncidentError(409, "事件已复盘关闭")
    if step == "contain" and action == "offline_item":
        _offline_item(db, user, v.incident)
    if step == "contain" and action == "rollback_agent":
        _rollback_agent(db, user, v.incident, agent, version)
    e = QualityIncidentEvent(incident_id=v.incident.id, step=step, level=level if step == "grade" else "",
                             action=action if step == "contain" else "",
                             root_cause=root_cause if step == "root_cause" else "",
                             note_len=len(note), note_sha=digest(note), actor=user.username)
    db.add(e)
    db.commit()
    audit.record(db, actor=user.username, action="quality_incident.event", result="ok",
                 project_code=v.incident.project_code, incident=v.incident.id, step=step, level=e.level,
                 containment=e.action, root_cause=e.root_cause, note_len=e.note_len, note_sha=e.note_sha)
    return _view(db, v.incident)


def _offline_item(db: Session, user: User, inc: QualityIncident) -> None:
    from ..kb import refs as kb_refs
    from ..kb import review
    from ..models import KbItem

    if inc.ref_kind != "kb_item":
        raise IncidentError(422, "下线条目只用于挂知识条目的事件")
    if KNOWLEDGE_ADMIN not in roles_of(db, user.id):
        raise IncidentError(403, "下线条目须由知识管理员处置")
    r = kb_refs.resolve(db, [inc.ref_id]).get(inc.ref_id)
    item = db.get(KbItem, r.item_id) if r else None
    if item is None:
        raise IncidentError(422, "关联的知识条目不存在")
    if not item.retired:
        review.retire(db, user, item, f"质量事件 #{inc.id} 遏制")


def _rollback_agent(db: Session, user: User, inc: QualityIncident, agent: str, version: str) -> None:
    from . import agent_md

    if PROJECT_LEAD not in roles_of(db, user.id):
        raise IncidentError(403, "回滚 Agent 版本须由项目负责人处置")
    try:
        agent_md.pin(db, user.username, agent, version, inc.id)
    except agent_md.MdError as e:
        raise IncidentError(e.status, str(e)) from None


def all_views(db: Session) -> list[View]:
    incs = db.scalars(select(QualityIncident).order_by(QualityIncident.id.desc())).all()
    evs: dict[int, list[QualityIncidentEvent]] = {}
    for e in db.scalars(select(QualityIncidentEvent).order_by(QualityIncidentEvent.id)):
        evs.setdefault(e.incident_id, []).append(e)
    return [View(i, evs.get(i.id, [])) for i in incs]


def summary(db: Session, now: datetime | None = None) -> dict:
    """运营看板（F2）用：只有计数。"""
    now = now or datetime.now(timezone.utc)
    open_ = [v for v in all_views(db) if v.status != "closed"]
    by_level = {lv: 0 for lv in (*LEVELS, "ungraded")}
    for v in open_:
        by_level[v.level or "ungraded"] += 1
    return {"open": len(open_), "by_level": by_level, "overdue": sum(v.overdue(now) for v in open_)}


def export_frozen(db: Session, project_code: str, deliverable_code: str) -> int | None:
    """遏制联动：挂这个交付件、未关闭、已执行“冻结导出”的事件编号（取最早一条）；没有返回 None。"""
    incs = db.scalars(select(QualityIncident).where(QualityIncident.project_code == project_code,
                                                    QualityIncident.ref_kind == "deliverable",
                                                    QualityIncident.ref_id == deliverable_code)
                      .order_by(QualityIncident.id)).all()
    for inc in incs:
        v = _view(db, inc)
        if v.status != "closed" and any(e.step == "contain" and e.action == "freeze_export" for e in v.events):
            return inc.id
    return None


def register_signed_basis(db: Session, doc_id: str, source: str = "kb.basis",
                          category: str = "repealed_basis") -> list[int]:
    """程序自动登记（F3②b）：条目被标为已废止 / 已被替代时，当前正文引用它的**已签署**交付件各登记一条事件
    （挂交付件，登记人 system:<来源>）。未签署的只走 B4 复核提醒。同一交付件已有未关闭的同类事件就不重复登记。
    说明只写条目编号（不含正文），且只记长度和哈希。"""
    from ..models import Deliverable
    from ..project import kb_impact

    ids = {d for _, d in kb_impact.citing_sections(db, doc_id)}
    if not ids:
        return []
    rows = db.execute(select(Project.code, Deliverable.code).join(Deliverable, Deliverable.project_id == Project.id)
                      .where(Deliverable.id.in_(ids), Deliverable.status == "signed")).tuples()
    out: list[int] = []
    for project, code in sorted(set(rows)):
        open_same = [i for i in db.scalars(select(QualityIncident).where(
            QualityIncident.project_code == project, QualityIncident.ref_kind == "deliverable",
            QualityIncident.ref_id == code, QualityIncident.category == category)).all()
            if _view(db, i).status != "closed"]
        if open_same:
            continue
        out.append(register_system(db, source, category=category, ref_kind="deliverable", ref_id=code,
                                   project=project, description=f"引用条目 {doc_id}").incident.id)
    return out


def impact(db: Session, user: User, v: View) -> list[dict]:
    """影响面反查：挂交付件时就是它自己；挂知识条目时列出当前正文引用了该条目的交付件（含别名，见 kb_impact）。
    项目负责人看全部；其他人只看自己是成员的项目（非成员项目不出现，免得泄露项目代号）。"""
    from ..models import Deliverable
    from ..project import kb_impact

    inc = v.incident
    if inc.ref_kind == "deliverable":
        return [{"project": inc.project_code, "deliverable": inc.ref_id}] if inc.project_code else []
    if inc.ref_kind != "kb_item":
        return []
    ids = {d for _, d in kb_impact.citing_sections(db, inc.ref_id)}
    if not ids:
        return []
    stmt = select(Project.code, Deliverable.code).join(Deliverable, Deliverable.project_id == Project.id)         .where(Deliverable.id.in_(ids))
    if PROJECT_LEAD not in roles_of(db, user.id):
        stmt = stmt.join(ProjectMember, ProjectMember.project_id == Project.id).where(ProjectMember.user_id == user.id)
    return [{"project": p, "deliverable": d} for p, d in sorted(set(db.execute(stmt).tuples()))]
