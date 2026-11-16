"""二期 B4：发布影响——交付件引用的知识条目更新或下线后，提醒项目顾问复核。

依据：章节版本的 sources.knowledge（写入时核对过的 [L4-…] 等条目标注，见 app/project/deliverables.py）。
不新增表：读取时比较“条目现行版本的发布时间”与“章节当前版本的写入时间”——
- updated：条目在本章当前版本写入之后发布了新版本，建议复核；
- retired：条目已下线。
章节改写（新版本）后，提醒自然消失。

隔离：项目侧接口只给项目成员（非成员 404），列出本项目哪些章节引用了哪些条目；知识侧接口只返回计数
（引用它的项目数、交付件数、章节数），不返回项目代号和交付件内容（知识侧看不到项目区）。
知识区与主库是两个库：分开查询，在内存里比对，不 JOIN。
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Deliverable, DeliverableSection, Project, SectionVersion
from ..models.kb import KbItem, KbVersion

UPDATED, RETIRED = "updated", "retired"


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def cited_doc_ids(v: SectionVersion | None) -> list[str]:
    """本版本里核对上的知识条目编号（去重，保持顺序）。"""
    if v is None:
        return []
    refs = (v.sources or {}).get("knowledge", [])
    return list(dict.fromkeys(x["ref"] for x in refs if isinstance(x, dict) and x.get("found") and x.get("ref")))


def _kb_state(db: Session, doc_ids: set[str]) -> dict[str, tuple[bool, int | None, datetime | None]]:
    """doc_id → (已下线, 现行版本号, 现行版本发布时间)。"""
    if not doc_ids:
        return {}
    rows = db.execute(select(KbItem.doc_id, KbItem.retired, KbVersion.version, KbVersion.published_at)
                      .outerjoin(KbVersion, KbVersion.id == KbItem.current_version_id)
                      .where(KbItem.doc_id.in_(doc_ids))).all()
    return {r[0]: (bool(r[1]), r[2], _utc(r[3])) for r in rows}


def _changes(v: SectionVersion, state: dict) -> list[dict]:
    out = []
    written = _utc(v.created_at)
    for doc_id in cited_doc_ids(v):
        if doc_id not in state:
            continue
        retired, version, published = state[doc_id]
        if retired:
            out.append({"doc_id": doc_id, "change": RETIRED, "current_version": version})
        elif published is not None and written is not None and published > written:
            out.append({"doc_id": doc_id, "change": UPDATED, "current_version": version})
    return out


def _current_versions(d: Deliverable) -> list[tuple[DeliverableSection, SectionVersion]]:
    out = []
    for s in d.sections:
        v = next((x for x in s.versions if x.version == s.current_version), None)
        if v is not None:
            out.append((s, v))
    return out


def deliverable_updates(db: Session, d: Deliverable) -> dict[int, list[dict]]:
    """交付件各章（seq → 变化列表），只含有变化的章。"""
    pairs = _current_versions(d)
    state = _kb_state(db, {x for _, v in pairs for x in cited_doc_ids(v)})
    out = {}
    for s, v in pairs:
        ch = _changes(v, state)
        if ch:
            out[s.seq] = ch
    return out


def project_updates(db: Session, project: Project) -> list[dict]:
    """本项目所有交付件里需要复核的引用（按交付件、章节排序）。"""
    ds = list(db.scalars(select(Deliverable).where(Deliverable.project_id == project.id).order_by(Deliverable.code)))
    pairs = [(d, s, v) for d in ds for s, v in _current_versions(d)]
    state = _kb_state(db, {x for _, _, v in pairs for x in cited_doc_ids(v)})
    rows = []
    for d, s, v in pairs:
        for ch in _changes(v, state):
            rows.append({"deliverable": d.code, "deliverable_title": d.title, "status": d.status,
                         "section": s.seq, "section_title": s.title, "section_version": v.version, **ch})
    return rows


def usage_counts(db: Session, doc_id: str) -> dict:
    """知识侧：引用该条目的章节当前版本计数。不返回项目代号和内容。"""
    rows = db.execute(select(Deliverable.project_id, Deliverable.id, SectionVersion.sources)
                      .join(DeliverableSection, DeliverableSection.deliverable_id == Deliverable.id)
                      .join(SectionVersion, (SectionVersion.section_id == DeliverableSection.id)
                            & (SectionVersion.version == DeliverableSection.current_version))).all()
    hits = [(p, d) for p, d, src in rows
            if any(isinstance(x, dict) and x.get("found") and x.get("ref") == doc_id
                   for x in (src or {}).get("knowledge", []))]
    return {"projects": len({p for p, _ in hits}), "deliverables": len({d for _, d in hits}), "sections": len(hits)}
