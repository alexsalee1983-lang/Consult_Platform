"""知识使用反馈（三期 B29 最小版，三期开发计划第九节）。

- 三键：顾问在交付件审阅（章节版本里核对得上的 [L…] 引用）和顾问对话（Agent 回复附的参考知识）里标
  “有用 / 过时 / 不对”；一处引用一人一条，再点覆盖。附言可选、≤200 字、先过机密级检测，严格档项目不收附言。
- 进缺口池：标“过时 / 不对”的，复用二期 B13 待复查（app/kb/recheck.py）——把条目现行版本标 recheck_state=pending，
  进抽检队列；只数现行版本发布、上次复查处理之后的反馈；已在队列里的不重复标。原因只写计数。
  就绪度（F2，app/kb/readiness.py）不把待复查的条目算作就绪，所以被标“过时 / 不对”的条目在复查通过前不计入就绪度。
- 统计：每条知识的引用次数（Agent 写入章节里核对得上的引用 + 对话回复附的参考知识）、采纳率（Agent 引用它的章节里，
  顾问随后没删掉的比例，与 B13 同一口径）、三键计数。只出编号、标题和计数，不出项目、正文和附言。
- 审计 kb.feedback 只记条目编号、选项、入口、附言长度和摘要哈希，不记附言原文和项目内容。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..models import Conversation, KnowledgeFeedback, KbItem, KbVersion, Message, Project, SectionVersion, User
from ..services import guard
from . import recheck

RATINGS = {"useful": "有用", "outdated": "过时", "wrong": "不对"}
FLAGS = ("outdated", "wrong")
MIN_FLAGS = 1                  # 现行版本发布（或上次复查处理）后被标“过时 / 不对”达到几次就进待复查（可调）
NOTE_MAX = 200
STATS_DAYS = 90


class FeedbackError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


@dataclass
class Target:
    """一处引用：surface + ref_id（章节版本 id / 消息 id）和这处引用里能标的知识编号。"""
    surface: str
    ref_id: int
    doc_ids: set[str]


def section_target(sv: SectionVersion) -> Target:
    return Target("review", sv.id, recheck._refs(sv))


def message_target(db: Session, project: Project, user: User, message_id: int) -> Target:
    """对话只有发起人能看：不是本人在本项目对话里的 Agent 回复一律 404。"""
    m = db.get(Message, message_id)
    conv = db.get(Conversation, m.conversation_id) if m is not None else None
    if m is None or m.role != "assistant" or conv is None or conv.project_id != project.id or conv.user_id != user.id:
        raise FeedbackError(404, "回复不存在")
    return Target("chat", m.id, {r.get("doc_id", "") for r in (m.kb_refs or []) if r.get("doc_id")})


def mine(db: Session, user: User, target: Target) -> dict[str, str]:
    """当前用户在这处引用上的选择：知识编号 → rating。"""
    return {d: r for d, r in db.execute(select(KnowledgeFeedback.doc_id, KnowledgeFeedback.rating).where(
        KnowledgeFeedback.user_id == user.id, KnowledgeFeedback.surface == target.surface,
        KnowledgeFeedback.ref_id == target.ref_id))}


def submit(db: Session, project: Project, user: User, target: Target, doc_id: str, rating: str,
           note: str = "") -> tuple[KnowledgeFeedback, bool]:
    """记一条反馈，返回 (反馈, 是否新标了待复查)。"""
    note = note.strip()
    if rating not in RATINGS:
        raise FeedbackError(422, "选项只能是有用、过时、不对")
    if doc_id not in target.doc_ids:
        raise FeedbackError(404, "这里没有引用这条知识")
    if note and project.tier == "strict":
        raise FeedbackError(422, "严格档项目不收附言，只能点选")
    if len(note) > NOTE_MAX:
        raise FeedbackError(422, f"附言最多 {NOTE_MAX} 字")
    if note:
        g = guard.check(note)
        if g.blocked:
            audit.record(db, actor=user.username, action="kb.feedback", result="blocked", project_code=project.code,
                         doc_id=doc_id, surface=target.surface, rating=rating, note_length=len(note),
                         note_sha=audit.digest(note), kinds=sorted({f.kind for f in g.findings}))
            raise FeedbackError(422, "附言命中机密级检测，未保存；请只写一句不含客户信息的话")
    fb = db.scalar(select(KnowledgeFeedback).where(KnowledgeFeedback.user_id == user.id, KnowledgeFeedback.surface == target.surface,
                                            KnowledgeFeedback.ref_id == target.ref_id, KnowledgeFeedback.doc_id == doc_id))
    now = utcnow()
    if fb is None:
        fb = KnowledgeFeedback(project_id=project.id, user_id=user.id, doc_id=doc_id, surface=target.surface,
                        ref_id=target.ref_id, created_at=now)
        db.add(fb)
    fb.rating, fb.note, fb.updated_at = rating, note, now
    db.commit()
    marked = mark_flagged(db, doc_id, actor=user.username) if rating in FLAGS else False
    audit.record(db, actor=user.username, action="kb.feedback", result="allowed", project_code=project.code,
                 doc_id=doc_id, surface=target.surface, rating=rating, note_length=len(note),
                 note_sha=audit.digest(note) if note else "", recheck_marked=marked)
    return fb, marked


def _live(db: Session, doc_id: str) -> KbVersion | None:
    return db.scalar(select(KbVersion).join(KbItem, KbItem.current_version_id == KbVersion.id)
                     .where(KbItem.doc_id == doc_id, KbItem.retired.is_(False), KbVersion.status == "published"))


def mark_flagged(db: Session, doc_id: str, actor: str = "platform") -> bool:
    """现行版本发布（或上次复查处理）之后被标“过时 / 不对”≥ MIN_FLAGS 次，标待复查进抽检队列（复用 B13）。"""
    v = _live(db, doc_id)
    if v is None or v.recheck_state in recheck.OPEN_STATES:
        return False
    times = [t for t in (recheck._aware(v.published_at), recheck._aware(v.recheck_at)) if t is not None]
    cutoff = max(times) if times else None
    counts = dict.fromkeys(FLAGS, 0)
    for rating, at in db.execute(select(KnowledgeFeedback.rating, KnowledgeFeedback.updated_at)
                                 .where(KnowledgeFeedback.doc_id == doc_id, KnowledgeFeedback.rating.in_(FLAGS))).tuples():
        if cutoff is None or recheck._aware(at) >= cutoff:
            counts[rating] += 1
    if sum(counts.values()) < MIN_FLAGS:
        return False
    v.recheck_state, v.recheck_at = "pending", utcnow()
    v.recheck_reason = f"使用反馈：顾问标过时 {counts['outdated']} 次、不对 {counts['wrong']} 次"
    db.commit()
    audit.record(db, actor=actor, action="kb.recheck_mark", result="allowed", marked=1, doc_ids=[doc_id],
                 rule="usage-flag")
    return True


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def stats(db: Session, days: int = STATS_DAYS, now: datetime | None = None) -> dict[str, Any]:
    """近 days 天每条知识的引用次数、采纳率和三键计数（只出编号、标题和计数）。"""
    since = (now or utcnow()) - timedelta(days=days)
    usage = recheck.usage(db, since)
    chat: dict[str, int] = defaultdict(int)
    for refs in db.scalars(select(Message.kb_refs).where(Message.role == "assistant", Message.created_at >= since)):
        for d in {r.get("doc_id", "") for r in (refs or []) if r.get("doc_id")}:
            chat[d] += 1
    votes: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(RATINGS, 0))
    for d, rating, n in db.execute(select(KnowledgeFeedback.doc_id, KnowledgeFeedback.rating, func.count())
                                   .where(KnowledgeFeedback.updated_at >= since)
                                   .group_by(KnowledgeFeedback.doc_id, KnowledgeFeedback.rating)).tuples():
        if rating in RATINGS:
            votes[d][rating] = n
    doc_ids = set(usage) | set(chat) | set(votes)
    meta = {i.doc_id: (i, v) for i, v in db.execute(
        select(KbItem, KbVersion).outerjoin(KbVersion, KbVersion.id == KbItem.current_version_id)
        .where(KbItem.doc_id.in_(doc_ids))).tuples()} if doc_ids else {}
    items = []
    tot_cited = tot_kept = 0
    for d in doc_ids:
        u = usage.get(d)
        cited, removed = (len(u.cited), len(u.removed & u.cited)) if u else (0, 0)
        tot_cited, tot_kept = tot_cited + cited, tot_kept + cited - removed
        i, v = meta.get(d, (None, None))
        items.append({"doc_id": d, "title": v.title if v is not None else "", "retired": bool(i and i.retired),
                      "recheck_state": v.recheck_state if v is not None else "",
                      "citations": cited + chat.get(d, 0), "section_citations": cited,
                      "chat_citations": chat.get(d, 0), "adoption_rate": _ratio(cited - removed, cited),
                      "feedback": votes.get(d, dict.fromkeys(RATINGS, 0))})
    items.sort(key=lambda x: (-x["citations"], x["doc_id"]))
    return {"days": days, "items": items,
            "totals": {"citations": sum(x["citations"] for x in items), "adoption_rate": _ratio(tot_kept, tot_cited),
                       "feedback": {r: sum(x["feedback"][r] for x in items) for r in RATINGS}}}


def delete_for_project(db: Session, project_id: int) -> int:
    return db.execute(delete(KnowledgeFeedback).where(KnowledgeFeedback.project_id == project_id)).rowcount or 0
