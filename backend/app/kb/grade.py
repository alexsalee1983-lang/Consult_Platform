"""条目分级：候选 → 可用 → 推荐（四期 B43，平台规划刷新设计 6.2，知识中心改版方案 4.1；接闭环第 12 条“价值与淘汰”）。

- 新条目从“候选”起（kb_items.grade，知识区迁移 k0025；迁移时已有现行版本的回填为“可用”）。
- 靠实际使用逐级升（每次扫描最多升一级）：近 WINDOW_DAYS 天引用（章节引用 + 对话参考，feedback.stats 同一口径）
  ≥ USABLE_CITES 且采纳率不低于 USABLE_ADOPTION 升“可用”；≥ RECOMMEND_CITES、采纳率不低于 RECOMMEND_ADOPTION、
  至少 RECOMMEND_USEFUL 个“有用”且没有“不对”升“推荐”。
- 被反复驳回或报“有误”的降一级（负面信号：“过时 / 不对”≥ DEMOTE_FLAGS；引用 ≥ DEMOTE_MIN_CITES 而采纳率低于
  DEMOTE_ADOPTION，即顾问反复删掉；抽检或复查判不通过）；已是“候选”还有负面信号的标待复查（复用 B13，进抽检队列），
  由知识专家复查后修正或下线——直至淘汰，不自动删除。
- 封顶：未经作者以外知识专家认可的 AI 基线（deid_status=virtual）最多“候选”；认可过的 AI 基线最多“可用”
  （风险与优化第 4 条：权重始终低于真实项目的样例）；单人发布待补复审的最多“候选”。
- worker 每天扫描（_daily），应急 python -m app.cli kb-grade-scan [--dry-run]；审计 kb.grade_scan 只记计数和编号。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..models import KbItem, KbVersion, KnowledgeFeedback
from . import feedback, recheck
from . import review as rv

GRADES: dict[str, str] = {"candidate": "候选", "usable": "可用", "recommended": "推荐"}
ORDER = list(GRADES)
WINDOW_DAYS = 180
USABLE_CITES = 3
USABLE_ADOPTION = 0.5
RECOMMEND_CITES = 10
RECOMMEND_ADOPTION = 0.7
RECOMMEND_USEFUL = 2
DEMOTE_FLAGS = 2
DEMOTE_MIN_CITES = 5
DEMOTE_ADOPTION = 0.3


@dataclass
class Signals:
    citations: int = 0
    adoption: float | None = None
    useful: int = 0
    outdated: int = 0
    wrong: int = 0


@dataclass
class GradeReport:
    up: list[str] = field(default_factory=list)
    down: list[str] = field(default_factory=list)
    capped: list[str] = field(default_factory=list)
    recheck: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {k: len(getattr(self, k)) for k in ("up", "down", "capped", "recheck")}


def cap(v: KbVersion) -> str:
    """这一版本最高能到哪一级。"""
    if v.deid_status == "virtual":
        return "usable" if rv.expert_confirmed(v) else "candidate"
    if v.single_review:
        return "candidate"
    return "recommended"


def negative(v: KbVersion, s: Signals) -> bool:
    return (s.outdated + s.wrong >= DEMOTE_FLAGS
            or (s.citations >= DEMOTE_MIN_CITES and s.adoption is not None and s.adoption < DEMOTE_ADOPTION)
            or v.sample_state == "failed" or v.recheck_state == "failed")


def earned(s: Signals) -> str:
    if (s.citations >= RECOMMEND_CITES and (s.adoption is None or s.adoption >= RECOMMEND_ADOPTION)
            and s.useful >= RECOMMEND_USEFUL and s.wrong == 0):
        return "recommended"
    if s.citations >= USABLE_CITES and (s.adoption is None or s.adoption >= USABLE_ADOPTION):
        return "usable"
    return "candidate"


def next_grade(current: str, v: KbVersion, s: Signals) -> tuple[str, bool]:
    """返回 (新等级, 是否该标待复查)。升最多一级，负面信号降一级，最后按封顶压。"""
    i = ORDER.index(current) if current in ORDER else 0
    flag = False
    if negative(v, s):
        if i == 0:
            flag = True
        i = max(0, i - 1)
    elif ORDER.index(earned(s)) > i:
        i += 1
    return ORDER[min(i, ORDER.index(cap(v)))], flag


def _signals(db: Session) -> dict[str, Signals]:
    out: dict[str, Signals] = {}
    for x in feedback.stats(db, days=WINDOW_DAYS)["items"]:
        fb = x["feedback"]
        out[x["doc_id"]] = Signals(x["citations"], x["adoption_rate"], fb.get("useful", 0), fb.get("outdated", 0),
                                   fb.get("wrong", 0))
    return out


def _flags(db: Session) -> dict[str, list[tuple[str, object]]]:
    out: dict[str, list[tuple[str, object]]] = {}
    for d, rating, at in db.execute(select(KnowledgeFeedback.doc_id, KnowledgeFeedback.rating,
                                           KnowledgeFeedback.updated_at)
                                    .where(KnowledgeFeedback.rating.in_(feedback.FLAGS))).tuples():
        out.setdefault(d, []).append((rating, at))
    return out


def _since_cutoff(v: KbVersion, flags: list[tuple[str, object]]) -> tuple[int, int]:
    """“过时 / 不对”只数现行版本发布、上次复查之后的（与 feedback.mark_flagged 同口径），复查过的不重复降级。"""
    times = [t for t in (recheck._aware(v.published_at), recheck._aware(v.recheck_at)) if t is not None]
    cutoff = max(times) if times else None
    keep = [r for r, at in flags if cutoff is None or recheck._aware(at) >= cutoff]
    return keep.count("outdated"), keep.count("wrong")


def scan(db: Session, *, actor: str = "platform", dry_run: bool = False) -> GradeReport:
    rep = GradeReport()
    sig, flags = _signals(db), _flags(db)
    rows = db.execute(select(KbItem, KbVersion).join(KbVersion, KbVersion.id == KbItem.current_version_id)
                      .where(KbItem.retired.is_(False))).tuples().all()
    for item, v in rows:
        s = sig.get(item.doc_id, Signals())
        s.outdated, s.wrong = _since_cutoff(v, flags.get(item.doc_id, []))
        new, flag = next_grade(item.grade, v, s)
        old = item.grade if item.grade in ORDER else "candidate"
        if ORDER.index(new) > ORDER.index(old):
            rep.up.append(item.doc_id)
        elif ORDER.index(new) < ORDER.index(old):
            (rep.down if negative(v, s) else rep.capped).append(item.doc_id)
        if flag and v.recheck_state not in recheck.OPEN_STATES and v.recheck_state != "failed":
            rep.recheck.append(item.doc_id)
            if not dry_run:
                v.recheck_state, v.recheck_at = "pending", utcnow()
                v.recheck_reason = "分级：候选条目仍被反复驳回或报有误，请复查后修正或下线"
        if not dry_run and new != item.grade:
            item.grade = new
    if not dry_run:
        db.commit()
        audit.record(db, actor=actor, action="kb.grade_scan", result="allowed", **rep.counts(),
                     doc_ids=(rep.up + rep.down + rep.capped + rep.recheck)[:50])
    return rep
