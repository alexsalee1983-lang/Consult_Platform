"""候选影子期（三期 B24①）：自动发布的知识先当候选，只用于评测和影子检索，达标后才进交付。

- 进入候选：B10（autopublish）、B11（agent_review）自动发布通过时，KB_CANDIDATE_SHADOW 开启（默认）就调 stage()：
  切好检索切片、排向量化，status=candidate、记 candidate_since；不改条目的 current_version_id，
  交付侧（search 默认、工具、导出）继续用旧的已发布版本（若有）。人工审核发布不经这里，照旧直接发布。
- 晋级：promote_due()（worker 每日排 kb.candidate_promote 任务；应急 python -m app.cli kb-candidate-promote [--dry-run]）
  逐个看候选：抽检判不通过 → 退回人工审核（return_to_review）；抽检还没判 → 等；
  满 KB_CANDIDATE_DAYS 天、自动发布未暂停 → 晋级（review.publish，切换现行版本、旧版本已替代）。
  使用反馈“不对 / 过时”：main 上还没有这类反馈表（B13 只有交付件删引用的待复查），有了以后在 _blocked_by_feedback 里加。
- 审计只记编号、计数和结论，不记正文。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..core.db import utcnow
from ..models import KbChunk, KbVersion
from ..services import jobs
from . import review as rv

STATUS = "candidate"
JOB = "kb.candidate_promote"


def enabled() -> bool:
    return get_settings().kb_candidate_shadow


def stage(db: Session, v: KbVersion, actor: str) -> None:
    """把通过自动审核的版本落为候选（调用前审核记录、auto_review、sample_state 已写好）。"""
    chunks = rv.make_chunks(db, v)
    rv.supersede_candidates(db, v)
    v.status, v.candidate_since, v.candidate_result = STATUS, utcnow(), ""
    v.effective_date = v.effective_date or datetime.now(timezone.utc).date()
    db.commit()
    audit.record(db, actor=actor, action="kb.candidate", result="allowed", doc_id=v.item.doc_id, version=v.version,
                 chunks=chunks, auto_review=v.auto_review)
    from .search import enqueue_embedding          # 候选也要向量化，影子检索和评测才查得到

    enqueue_embedding(db, v.id, created_by=v.submitted_by or actor)


def return_to_review(db: Session, v: KbVersion, actor: str, reason: str) -> None:
    """抽检不通过：退回人工两项审核队列（in_review），删掉候选切片；人工发布时重新切片。"""
    db.execute(delete(KbChunk).where(KbChunk.version_id == v.id))
    v.status, v.candidate_result = "in_review", "returned"
    db.commit()
    audit.record(db, actor=actor, action="kb.candidate_return", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, reason=reason)


def _blocked_by_feedback(db: Session, v: KbVersion) -> bool:  # noqa: ARG001
    """使用反馈“不对 / 过时”：反馈表还没建（另开单），先不拦。"""
    return False


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


@dataclass
class PromoteReport:
    promoted: list[str] = field(default_factory=list)      # doc_id vN
    returned: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)       # 天数未满、抽检未判、自动发布暂停中
    dry_run: bool = False

    def counts(self) -> dict:
        return {k: len(getattr(self, k)) for k in ("promoted", "returned", "superseded", "waiting")}


def candidates(db: Session) -> list[KbVersion]:
    return list(db.scalars(select(KbVersion).where(KbVersion.status == STATUS).order_by(KbVersion.id)))


def promote_due(db: Session, *, actor: str = "platform", dry_run: bool = False,
                now: datetime | None = None) -> PromoteReport:
    from . import autopublish

    now = now or utcnow()
    days = get_settings().kb_candidate_days
    out = PromoteReport(dry_run=dry_run)
    for v in candidates(db):
        label = f"{v.item.doc_id} v{v.version}"
        if v.item.retired:
            continue
        newer = [o for o in v.item.versions if o.version > v.version and o.status in ("published", STATUS)]
        if newer:
            out.superseded.append(label)
            if not dry_run:
                v.status, v.candidate_result = "superseded", "superseded"
                db.commit()
            continue
        if v.sample_state == "failed" or _blocked_by_feedback(db, v):
            out.returned.append(label)
            if not dry_run:
                return_to_review(db, v, actor, "sample_failed")
            continue
        due = v.candidate_since is None or _aware(v.candidate_since) + timedelta(days=days) <= _aware(now)
        if not due or v.sample_state == "pending" or autopublish.suspended(db, v.auto_review or autopublish.PLATFORM):
            out.waiting.append(label)
            continue
        out.promoted.append(label)
        if not dry_run:
            v.candidate_result = "promoted"
            rv.publish(db, v)
            audit.record(db, actor=actor, action="kb.candidate_promote", result="allowed", doc_id=v.item.doc_id,
                         version=v.version, days=days, sample_state=v.sample_state)
    if not dry_run:
        audit.record(db, actor=actor, action="kb.candidate_scan", result="allowed", days=days, **out.counts())
    return out


def enqueue(db: Session) -> int | None:
    """每日计划任务：已有没做完的不重复排。"""
    from ..models import Job

    pending = db.scalar(select(Job.id).where(Job.kind == JOB, Job.status.in_((jobs.QUEUED, jobs.RUNNING))).limit(1))
    if pending is not None:
        return None
    return jobs.enqueue(db, JOB, {}, created_by="platform", max_attempts=1)


@jobs.handler(JOB)
def _job(payload: dict, ctx: jobs.JobContext) -> None:  # noqa: ARG001
    from ..core.db import SessionLocal

    with SessionLocal() as db:
        promote_due(db)
