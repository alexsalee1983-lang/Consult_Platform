"""四期 F4③：经验萃取触发引擎，先开 3 类信号（总体规划第 113、310 行；三期开发计划第 44 行）。

流程：信号 → 先查后问三道过滤 → 打分 → 每人每周 ≤ WEEKLY_CAP 题 → 超时与跳过收尾。只由事件触发，不按日历出题。

三类信号（全部按“主题”跨项目聚合，不存正文、不存项目代号）：
- knowledge_gap 知识不足卡：runs 发的“知识不足”卡（三期 A12），标题里列着缺的必需槽位，主题 = 槽位键。
- revision_cluster 改稿聚类：F4② 特征表 revision_features 的改稿特征（source=revision），按章节键聚合，
  计数按“删除 + 改写”算（专家改掉 Agent 写的），涉及项目数 ≥ MIN_PROJECTS 才成簇。
- review_waiver 评审驳回：特征表里项目经理驳回评审阻断问题的特征（source=review_waiver），按章节键聚合。
项目范围：开关 settings.f4_revision_mining 开（生产不开）、非严格档、客户同意回流（与 F4② 一致）。

三道过滤（先查后问）：
1. known 知识库已有：主题对应槽位已有已发布的公司做法或黄金样例（与 B17 就绪度同一口径），记 filtered。
2. recent 近期问过：同信号同主题 RECENT_DAYS 天内已有问题（任何状态），不再落新行，只计数。
3. low 低置信：分数 < MIN_SCORE，记 filtered。
打分：score = 0.5 × min(1, 项目数 / 3) + 0.5 × min(1, 计数 / 5)，两位小数；同分按信号优先（知识不足 > 驳回 > 改稿）。

派题：分数从高到低，派给本周（周一起，UTC）已派题数最少、且 < WEEKLY_CAP 的在职知识专家；都满了留 pending 等下周。
已派题 ANSWER_DAYS 天未答 → skipped（timeout）；专家也可主动跳过（expert）。跳过率 = 跳过 /（已答 + 跳过）。
回答：经机密级检测后以 Markdown 投料（知识投料箱，不带来源项目），照常走解析、去标识、起草、
公司做法确认队列（B17）和专家拍板，不绕过任何一道；正文不落本表。审计只记编号、信号、计数、长度，不记正文。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import KNOWLEDGE_EXPERT
from ..kb import intake
from ..models import (Confirmation, ExtractionQuestion, KbItem, KbVersion, Project, RevisionFeature, User,
                      UserRole)
from ..services import guard, jobs
from .revision_mining import enabled

SIGNALS = ("knowledge_gap", "revision_cluster", "review_waiver")
SIGNAL_NAMES = {"knowledge_gap": "知识不足", "revision_cluster": "改稿聚类", "review_waiver": "评审驳回"}
PRIORITY = {"knowledge_gap": 0, "review_waiver": 1, "revision_cluster": 2}
GAP_KIND = "知识不足"
GAP_MARK = "缺少必需知识："
READY_KINDS = ("practice", "golden")
MIN_PROJECTS = 2
MIN_SCORE = 0.4
RECENT_DAYS = 30
WEEKLY_CAP = 3
ANSWER_DAYS = 7
ANSWER_MAX = 8000
JOB_KIND = "f4.extraction_week"


class ExtractionError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


@dataclass
class Signal:
    signal: str
    topic: str
    projects: int
    count: int

    @property
    def score(self) -> float:
        return round(0.5 * min(1.0, self.projects / 3) + 0.5 * min(1.0, self.count / 5), 2)


def _aware(dt: datetime) -> datetime:
    """SQLite 读回的时间不带时区（按 UTC 存），统一补成 UTC 再比较，免得随机器时区变。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _gap_slots(title: str) -> list[str]:
    if GAP_MARK not in title:
        return []
    return [s.strip()[:64] for s in title.split(GAP_MARK, 1)[1].split("、") if s.strip()]


def collect(db: Session) -> list[Signal]:
    """从三类来源聚合信号（只读）。开关关或生产环境返回空。"""
    if not enabled():
        return []
    ids = list(db.scalars(select(Project.id).where(Project.tier != "strict", Project.flowback_allowed.is_(True))))
    if not ids:
        return []
    acc: dict[tuple[str, str], tuple[set[int], int]] = defaultdict(lambda: (set(), 0))

    def add(signal: str, topic: str, pid: int, n: int) -> None:
        ps, c = acc[(signal, topic)]
        ps.add(pid)
        acc[(signal, topic)] = (ps, c + n)

    for pid, title in db.execute(select(Confirmation.project_id, Confirmation.title)
                                 .where(Confirmation.kind == GAP_KIND, Confirmation.project_id.in_(ids))):
        for slot in _gap_slots(title):
            add("knowledge_gap", slot, pid, 1)
    q = (select(RevisionFeature.source, RevisionFeature.section_key, RevisionFeature.project_id,
                func.sum(RevisionFeature.count))
         .where(RevisionFeature.project_id.in_(ids),
                ((RevisionFeature.source == "revision") & RevisionFeature.kind.in_(("delete", "rewrite")))
                | (RevisionFeature.source == "review_waiver"))
         .group_by(RevisionFeature.source, RevisionFeature.section_key, RevisionFeature.project_id))
    for source, key, pid, n in db.execute(q):
        add("revision_cluster" if source == "revision" else "review_waiver", key, pid, int(n or 0))
    out = []
    for (signal, topic), (ps, c) in sorted(acc.items()):
        if signal == "revision_cluster" and len(ps) < MIN_PROJECTS:
            continue
        out.append(Signal(signal, topic, len(ps), c))
    return out


def _known(db: Session) -> set[str]:
    rows = db.scalars(select(KbItem.slot).join(KbVersion, KbVersion.id == KbItem.current_version_id)
                      .where(KbItem.knowledge_kind.in_(READY_KINDS), KbItem.slot != "",
                             KbVersion.status == "published"))
    return set(rows)


def _recent(db: Session, now: datetime) -> set[tuple[str, str]]:
    since = now - timedelta(days=RECENT_DAYS)
    rows = db.execute(select(ExtractionQuestion.signal, ExtractionQuestion.topic, ExtractionQuestion.created_at))
    return {(s, t) for s, t, at in rows if _aware(at) >= since}


def _experts(db: Session) -> list[int]:
    rows = db.scalars(select(UserRole.user_id).join(User, User.id == UserRole.user_id)
                      .where(UserRole.role_code == KNOWLEDGE_EXPERT, User.is_active.is_(True))
                      .order_by(UserRole.user_id))
    return list(dict.fromkeys(rows))


def _week_start(now: datetime) -> datetime:
    d = now.astimezone(timezone.utc).date() - timedelta(days=now.astimezone(timezone.utc).weekday())
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def expire(db: Session, now: datetime) -> int:
    """已派题过期未答 → skipped（timeout）。不提交。"""
    n = 0
    for q in db.scalars(select(ExtractionQuestion).where(ExtractionQuestion.status == "asked")):
        if q.due_at is not None and _aware(q.due_at) <= now:
            q.status, q.skip_reason, q.closed_at = "skipped", "timeout", now
            n += 1
    db.flush()
    return n


def assign(db: Session, now: datetime) -> int:
    """把 pending 题按分数派给本周未满的知识专家（每人每周 ≤ WEEKLY_CAP）。不提交。"""
    experts = _experts(db)
    if not experts:
        return 0
    start = _week_start(now)
    load = dict.fromkeys(experts, 0)
    for uid, at in db.execute(select(ExtractionQuestion.assignee_id, ExtractionQuestion.asked_at)
                              .where(ExtractionQuestion.assignee_id.in_(experts))):
        if at is not None and _aware(at) >= start:
            load[uid] += 1
    pending = sorted(db.scalars(select(ExtractionQuestion).where(ExtractionQuestion.status == "pending")),
                     key=lambda q: (-q.score, PRIORITY.get(q.signal, 9), q.id))
    n = 0
    for q in pending:
        uid = min(experts, key=lambda u: (load[u], u))
        if load[uid] >= WEEKLY_CAP:
            break
        q.status, q.assignee_id, q.asked_at, q.due_at = "asked", uid, now, now + timedelta(days=ANSWER_DAYS)
        load[uid] += 1
        n += 1
    db.flush()
    return n


def run_week(db: Session, now: datetime | None = None, *, actor: str = "platform") -> dict[str, int]:
    """一轮：收尾超时 → 收信号 → 三道过滤 → 落库 → 派题。提交并审计（只记计数）。"""
    now = now or utcnow()
    result = {"expired": expire(db, now), "signals": 0, "known": 0, "recent": 0, "low": 0, "new": 0, "assigned": 0}
    if enabled():
        known, recent = _known(db), _recent(db, now)
        for s in collect(db):
            result["signals"] += 1
            if (s.signal, s.topic) in recent:
                result["recent"] += 1
                continue
            reason = "known" if s.topic in known else ("low" if s.score < MIN_SCORE else "")
            db.add(ExtractionQuestion(signal=s.signal, topic=s.topic, projects=s.projects, count=s.count,
                                      score=s.score, status="filtered" if reason else "pending",
                                      filter_reason=reason, created_at=now))
            result[reason or "new"] += 1
            recent.add((s.signal, s.topic))
        db.flush()
        result["assigned"] = assign(db, now)
    db.commit()
    audit.record(db, actor=actor, action="f4.extraction.run", result="allowed", counts=result)
    return result


def question_text(q: ExtractionQuestion) -> str:
    if q.signal == "knowledge_gap":
        return (f"知识槽位“{q.topic}”在 {q.projects} 个项目里因缺少已发布的公司做法发了知识不足卡，"
                "请写出这一项的公司做法或经验。")
    if q.signal == "review_waiver":
        return (f"章节 {q.topic} 的评审阻断问题在 {q.projects} 个项目里被项目经理驳回 {q.count} 次，"
                "评审规则是否该改？请写出公司口径。")
    return (f"章节 {q.topic} 的 Agent 初稿在 {q.projects} 个项目里被专家删改 {q.count} 处，"
            "请写出这一章的常见坑或正确写法。")


def _view(q: ExtractionQuestion) -> dict:
    return {"id": q.id, "signal": q.signal, "signal_name": SIGNAL_NAMES.get(q.signal, q.signal), "topic": q.topic,
            "projects": q.projects, "count": q.count, "score": q.score, "status": q.status,
            "question": question_text(q), "asked_at": q.asked_at, "due_at": q.due_at}


def mine(db: Session, user: User) -> list[dict]:
    rows = db.scalars(select(ExtractionQuestion).where(ExtractionQuestion.assignee_id == user.id,
                                                       ExtractionQuestion.status == "asked")
                      .order_by(ExtractionQuestion.id))
    return [_view(q) for q in rows]


def _own(db: Session, user: User, qid: int) -> ExtractionQuestion:
    q = db.get(ExtractionQuestion, qid)
    if q is None or q.assignee_id != user.id:
        raise ExtractionError(404, "问题不存在")
    if q.status != "asked":
        raise ExtractionError(409, "这道题已收尾")
    return q


def skip(db: Session, user: User, qid: int) -> dict:
    q = _own(db, user, qid)
    q.status, q.skip_reason, q.closed_at = "skipped", "expert", utcnow()
    db.commit()
    audit.record(db, actor=user.username, action="f4.extraction.skip", result="allowed", question=q.id,
                 signal=q.signal)
    return _view(q)


def answer(db: Session, user: User, qid: int, body: str) -> dict:
    """专家回答：过机密级检测后投料，进 B17 确认队列。命中机密级 422，正文不落本表。"""
    q = _own(db, user, qid)
    body = (body or "").strip()
    if not body:
        raise ExtractionError(422, "回答不能为空")
    if len(body) > ANSWER_MAX:
        raise ExtractionError(422, f"回答不超过 {ANSWER_MAX} 字")
    hit = guard.check(body)
    if hit.blocked:
        audit.record(db, actor=user.username, action="f4.extraction.answer", result="blocked", question=q.id,
                     categories=sorted({f.label for f in hit.findings}), length=len(body))
        raise ExtractionError(422, hit.summary())
    md = f"# 经验萃取回答：{q.topic}\n\n> {question_text(q)}\n\n{body}\n"
    try:
        res = intake.submit(db, user, f"经验萃取-{q.signal}-{q.id}.md", md.encode("utf-8"), "text/markdown")
    except intake.IntakeError as e:
        raise ExtractionError(e.status, str(e)) from e
    db.refresh(q)
    q.status, q.closed_at, q.upload_id = "answered", utcnow(), res.upload.id
    db.commit()
    audit.record(db, actor=user.username, action="f4.extraction.answer", result="allowed", question=q.id,
                 signal=q.signal, upload=res.upload.id, length=len(body))
    return {**_view(q), "upload_id": res.upload.id, "job_id": res.job_id}


def stats(db: Session) -> dict:
    by = dict(db.execute(select(ExtractionQuestion.status, func.count()).group_by(ExtractionQuestion.status)).all())
    counts = {k: int(by.get(k, 0)) for k in ("filtered", "pending", "asked", "answered", "skipped")}
    done = counts["answered"] + counts["skipped"]
    return {**counts, "skip_rate": round(counts["skipped"] / done, 4) if done else 0.0}


@jobs.handler(JOB_KIND)
def _job(payload: dict, ctx: jobs.JobContext | None) -> None:
    from ..core.db import SessionLocal

    with SessionLocal() as db:
        run_week(db)
