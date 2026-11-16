"""复审到期由知识审核 Agent 复查（人工环节复查 K7，决策记录 2026-11-15“人工环节复查的落地范围”第 4 条）。

- 范围同第二级（agent_review.eligible）：公开来源、无需去标识的 REF / MAP / MTD 及只含公开资料的 CAS / OBJ 的现行版本，
  复审日期已到（review_date <= 今天）、未下线、不是待补复审（single_review 的照旧要人补复审）、没有抽检不通过。
  依据原文 SRC、项目沉淀、专家经验不在此列，照旧由人复审（review_due 列表）。
- worker 每小时扫描一次（scan_due），每个到期版本排一个后台任务 kb.agent_reconfirm；扫描可重复执行：
  本周期已复查过（复查记录晚于复审日期）的不再排。
- 复查 = 程序规则 + 知识审核 Agent 按同一份七项清单（agent_review.rule_failures / _agent），审的是现行版本，不改正文。
  全过且 Agent 自动发布没有暂停：复审日期从今天起顺延一个周期，复查记录 review_role=reconfirm、decision=approve；
  Agent 审核发布的版本（auto_review=agent）且从未抽检过的，按 KB_SAMPLE_RATE_AGENT 进抽检队列。
  否则 decision=flag，问题清单写在复查记录里，留在复审到期列表，由知识专家看标出的问题后复审确认、改版本或下线。
- 复查记录每个版本只有一条（唯一约束 version_id + reviewer + review_role），每个周期覆盖更新。
- 审计 kb.agent_reconfirm 只记编号、版本、结论、没过的规则、提示词版本、模型和计数，不记正文和意见。
"""
from __future__ import annotations

import secrets
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core import audit
from ..core.db import utcnow
from ..models import KbItem, KbReview, KbVersion
from ..services import jobs
from . import agent_review as ar
from . import autopublish, meta

ROLE = "reconfirm"
KIND = "kb.agent_reconfirm"


def _record(db: Session, v: KbVersion) -> KbReview | None:
    return db.scalar(select(KbReview).where(KbReview.version_id == v.id, KbReview.reviewer == ar.REVIEWER,
                                            KbReview.review_role == ROLE))


def _checked_this_cycle(db: Session, v: KbVersion) -> bool:
    r = _record(db, v)
    return r is not None and v.review_date is not None and r.created_at.date() >= v.review_date


def due(db: Session, today: date | None = None) -> list[KbVersion]:
    today = today or date.today()
    rows = db.scalars(select(KbVersion).join(KbItem, KbItem.current_version_id == KbVersion.id)
                      .where(KbVersion.status == "published", KbItem.retired.is_(False),
                             KbVersion.review_date <= today, KbVersion.single_review.is_(False),
                             KbVersion.sample_state != "failed")
                      .options(selectinload(KbVersion.item), selectinload(KbVersion.reviews))
                      .order_by(KbVersion.review_date, KbVersion.id))
    return [v for v in rows if ar.eligible(v) and not _checked_this_cycle(db, v)]


def scan_due(db: Session) -> list[int]:
    """为到期的版本排复查任务；先写一条 queued 记录占位，重复扫描不会重复排。"""
    made = []
    for v in due(db):
        r = _record(db, v)
        if r is None:
            r = KbReview(version_id=v.id, reviewer=ar.REVIEWER, review_role=ROLE)
            db.add(r)
        r.decision, r.comment, r.findings, r.created_at = "queued", "复审到期，已排知识审核 Agent 复查", {}, utcnow()
        db.commit()
        jobs.enqueue(db, KIND, {"version_id": v.id}, created_by="platform", max_attempts=3)
        made.append(v.id)
    return made


def run(db: Session, v: KbVersion) -> str:
    """复查一个版本，返回 extended（已顺延）/ flagged（转人工）/ skipped（已不在范围）。"""
    item = v.item
    if (v.status != "published" or item.retired or item.current_version_id != v.id or not ar.eligible(v)
            or v.single_review or v.sample_state == "failed" or v.review_date is None or v.review_date > date.today()):
        return "skipped"
    r = _record(db, v)
    if r is not None and r.decision in ("approve", "flag") and _checked_this_cycle(db, v):
        return "skipped"
    failed, cites = ar.rule_failures(db, v)
    status, note, findings, prompt_version, model = ar._agent(db, v, cites)
    findings = {**findings, "rules_failed": failed, "agent": status, "note": note}
    passed = not failed and status == "ok" and ar.agent_passed(findings)
    suspended = autopublish.suspended(db, ar.KIND)
    ok = passed and not suspended
    if passed and suspended:
        findings["note"] = f"Agent 复查通过，但自动发布已暂停（{suspended}），转人工复审"
    comment = ("知识审核 Agent 复查：七项检查通过，复审日期已顺延" if ok else
               "知识审核 Agent 复查：存疑，请知识专家复审"
               + (f"；程序规则未过：{'、'.join(ar.RULES[k] for k in failed)}" if failed else "")
               + (f"；{findings['note']}" if findings["note"] else ""))
    if r is None:
        r = KbReview(version_id=v.id, reviewer=ar.REVIEWER, review_role=ROLE)
        db.add(r)
    r.decision, r.comment, r.findings, r.created_at = ("approve" if ok else "flag"), comment[:1000], findings, utcnow()
    sampled = False
    if ok:
        v.review_date = meta.default_review_date(item.layer, date.today(), item.doc_type)
        if v.auto_review == ar.KIND and not v.sample_state:
            sampled = secrets.SystemRandom().random() < ar.sample_rate(db)
            if sampled:
                v.sample_state = "pending"
    db.commit()
    db.refresh(v)
    audit.record(db, actor=ar.AGENT, action="kb.agent_reconfirm", result="allowed", doc_id=item.doc_id,
                 version=v.version, outcome="extended" if ok else "flagged", rules_failed=failed, agent=ar.AGENT,
                 agent_status=status, prompt_version=prompt_version, model=model,
                 issues=len(findings.get("issues", [])), sampled=sampled, suspended=bool(suspended),
                 review_date=v.review_date.isoformat() if v.review_date else "")
    return "extended" if ok else "flagged"


@jobs.handler(KIND)
def _job(payload: dict, ctx: jobs.JobContext) -> None:
    from ..core.db import SessionLocal

    with SessionLocal() as db:
        v = db.get(KbVersion, int(payload["version_id"]))
        if v is not None:
            run(db, v)

