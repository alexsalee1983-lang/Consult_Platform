"""四期 A31 ⑩：判断数据埋点（JUDGMENT_CAPTURE）关闭时的无正文漂移指标。

不新建表：从确认卡、质量评审、章节版本的已有字段按周聚合，只用计数、字符数和选项键，不读正文。
改稿幅度 = 相邻两版字符数之差 / 较大者（粗略幅度，只统计顾问手改，即 agent_code 为空的版本）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Confirmation, QualityReview, SectionVersion
from ..project.judgment import EDIT_BUCKETS


def _aware(t: datetime) -> datetime:
    from datetime import timezone
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def weekly(db: Session, start: datetime, end: datetime, project_ids: list[int] | None = None) -> dict[str, Any]:
    """[start, end) 按 7 天分桶，最后一桶含 end 前的数据；返回 {weeks: [...]}。"""
    n = max(1, round((end - start).total_seconds() / (7 * 86400)))
    weeks = [{"start": (start + timedelta(days=7 * i)).date().isoformat(),
              "cards": {"resolved": 0, "chose": 0, "followed": 0, "recommend_rate": None},
              "review_blocking": 0, "edits": {k: 0 for k in EDIT_BUCKETS}} for i in range(n)]

    def slot(t: datetime | None) -> dict | None:
        if t is None:
            return None
        t = _aware(t)
        s, e = _aware(start), _aware(end)
        return weeks[min(n - 1, int((t - s).total_seconds() // (7 * 86400)))] if s <= t <= e else None

    q = select(Confirmation).where(Confirmation.status == "resolved", Confirmation.resolved_at.is_not(None))
    if project_ids is not None:
        q = q.where(Confirmation.project_id.in_(project_ids))
    for c in db.scalars(q):
        w = slot(c.resolved_at)
        if w is None:
            continue
        w["cards"]["resolved"] += 1
        if c.choice:
            w["cards"]["chose"] += 1
            if c.recommendation and c.choice == c.recommendation:
                w["cards"]["followed"] += 1

    rq = select(QualityReview).where(QualityReview.status == "done")
    if project_ids is not None:
        rq = rq.where(QualityReview.project_id.in_(project_ids))
    for r in db.scalars(rq):
        w = slot(r.created_at)
        if w is not None:
            w["review_blocking"] += r.blocking or 0

    prev: dict[int, int] = {}
    vq = select(SectionVersion.section_id, SectionVersion.char_count, SectionVersion.agent_code,
                SectionVersion.created_at, SectionVersion.version).order_by(SectionVersion.section_id,
                                                                             SectionVersion.version)
    if project_ids is not None:
        from ..models import Deliverable, DeliverableSection
        vq = (vq.join(DeliverableSection, DeliverableSection.id == SectionVersion.section_id)
              .join(Deliverable, Deliverable.id == DeliverableSection.deliverable_id)
              .where(Deliverable.project_id.in_(project_ids)))
    for sid, cc, agent, at, _v in db.execute(vq):
        before = prev.get(sid)
        prev[sid] = cc
        w = slot(at)
        if before is None or agent or w is None:
            continue
        m = abs(cc - before) / max(cc, before, 1)
        w["edits"][next(k for k, top in EDIT_BUCKETS.items() if m < top)] += 1

    for w in weeks:
        c = w["cards"]
        c["recommend_rate"] = round(c["followed"] / c["chose"], 4) if c["chose"] else None
    return {"weeks": weeks}
