"""三期 B17（拆出）：公司做法确认队列（三期开发计划第三节，总体规划 v6.0 专家交互）。

- 队列：待审（in_review）的公司做法候选（knowledge_kind=practice），按章节就绪度从低到高排：
  同槽位已发布的公司做法、黄金样例条数越少越靠前；没填槽位的排最后；同档按提交时间。
- 卡片：出处（来源说明）、依据（知识审核 Agent 意见）、把握程度（Agent findings.confidence）、还缺什么（findings.issues）。
- 决定：采纳 adopt = 内容审核通过；修改 modify = 专家改正文后通过（改后正文同样过元数据校验和机密级检测）；
  驳回 reject = 内容审核退回。审核规则全部沿用 review.review（只有知识专家、作者不能确认自己的、等 Agent 意见）。
- 每条决定记耗时与修改幅度（kb_practice_decisions，迁移 k0018），审计只记编号、决定、耗时、幅度，不记正文。
"""
from __future__ import annotations

import difflib
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.permissions import roles_of
from ..models import KbItem, KbPracticeDecision, KbVersion, User
from . import meta
from . import review as rv
from .items import VersionContent, replace_mapping_rows

DECISIONS = ("adopt", "modify", "reject")
MAX_ELAPSED_MS = 3_600_000
READY_KINDS = ("practice", "golden")


def _slot_ready(db: Session) -> Counter:
    rows = db.execute(select(KbItem.slot, func.count()).join(KbVersion, KbVersion.id == KbItem.current_version_id)
                      .where(KbItem.knowledge_kind.in_(READY_KINDS), KbItem.slot != "", KbVersion.status == "published")
                      .group_by(KbItem.slot))
    return Counter({slot: n for slot, n in rows})


def _agent_opinion(v: KbVersion) -> tuple[str, str, list]:
    agent = [r for r in v.reviews if r.review_role == "agent"]
    if not agent:
        return "", "", []
    r = max(agent, key=lambda x: x.id)
    f = r.findings if isinstance(r.findings, dict) else {}
    issues = f.get("issues") or []
    return r.comment, str(f.get("confidence") or ""), [str(x) for x in issues][:10]


def queue(db: Session) -> list[dict]:
    ready = _slot_ready(db)
    rows = db.scalars(select(KbVersion).join(KbItem, KbItem.id == KbVersion.item_id)
                      .where(KbVersion.status == "in_review", KbItem.knowledge_kind == "practice"))
    cards = []
    for v in rows:
        basis, confidence, missing = _agent_opinion(v)
        slot = v.item.slot
        cards.append({"version_id": v.id, "doc_id": v.item.doc_id, "version": v.version, "title": v.title,
                      "body": v.body, "doc_type": v.item.doc_type, "slot": slot,
                      "slot_ready": ready.get(slot, 0) if slot else None,
                      "source_type": v.source_type, "source_detail": v.source_detail,
                      "submitted_by": v.submitted_by, "submitted_at": v.submitted_at,
                      "agent_reviewed": any(r.review_role == "agent" for r in v.reviews),
                      "basis": basis, "confidence": confidence, "missing": missing})
    cards.sort(key=lambda c: (c["slot_ready"] is None, c["slot_ready"] or 0, c["submitted_at"] or 0, c["version_id"]))
    return cards


def _modify(db: Session, v: KbVersion, body: str) -> float:
    if not body.strip():
        raise rv.ReviewError(422, "修改后采纳要给出改后的正文")
    old = v.body
    try:
        VersionContent(title=v.title, body=body, source_type=v.source_type, source_detail=v.source_detail,
                       ref_document=v.ref_document, ref_version=v.ref_version,
                       ref_status=v.ref_status).check(v.item.layer)
    except meta.MetaError as e:
        raise rv.ReviewError(422, str(e)) from e
    rv._check_text(v.title, body)                   # 机密级检测，命中 422
    v.body = body
    replace_mapping_rows(db, v)
    return round(1 - difflib.SequenceMatcher(None, old, body, autojunk=False).ratio(), 4)


def decide(db: Session, user: User, version_id: int, decision: str, body: str = "", comment: str = "",
           elapsed_ms: int = 0) -> KbVersion:
    if decision not in DECISIONS:
        raise rv.ReviewError(422, "decision 只能是 adopt / modify / reject")
    v = db.get(KbVersion, version_id)
    if v is None:
        raise rv.ReviewError(404, "没有这个版本")
    if v.item.knowledge_kind != "practice" or v.status != "in_review":
        raise rv.ReviewError(409, "这个版本不是待确认的公司做法候选")
    change = 0.0
    if decision == "modify":
        # 改正文前先按 review.review 的规则预检，免得改了正文却拍不了板（deny 会写审计并提交会话）
        if rv.ROLE_OF["content"] not in roles_of(db, user.id):
            raise rv.ReviewError(403, "内容审核须由知识专家进行")
        if user.username in rv._authors(v):
            raise rv.ReviewError(403, "不能确认自己提交或创建的条目")
        if rv.level3(v) and not any(r.review_role == "agent" for r in v.reviews):
            raise rv.ReviewError(409, "知识审核 Agent 的意见还没出来，请稍后再拍板")
        try:
            change = _modify(db, v, body)
        except rv.ReviewError:
            db.rollback()
            raise
    v = rv.review(db, user, v, "content", "reject" if decision == "reject" else "approve", comment)
    elapsed = max(0, min(int(elapsed_ms or 0), MAX_ELAPSED_MS))
    db.add(KbPracticeDecision(version_id=v.id, reviewer=user.username, decision=decision, elapsed_ms=elapsed,
                              change_ratio=change))
    db.commit()
    audit.record(db, actor=user.username, action="kb.practice_decide", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, decision=decision, elapsed_ms=elapsed, change_ratio=change)
    return v


def stats(db: Session) -> dict:
    rows = db.scalars(select(KbPracticeDecision)).all()
    times = sorted(r.elapsed_ms for r in rows if r.elapsed_ms)
    mods = [r.change_ratio for r in rows if r.decision == "modify"]
    return {"decided": len(rows), "by_decision": dict(Counter(r.decision for r in rows)),
            "median_elapsed_ms": times[len(times) // 2] if times else None,
            "mean_change_ratio": round(sum(mods) / len(mods), 4) if mods else None,
            "pending": len(queue(db))}
