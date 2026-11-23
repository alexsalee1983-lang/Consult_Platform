"""使用反馈标待复查（二期 B13，二期开发计划第十二节“发布后兜底”）。

信号：Agent 写入的章节版本引用了知识条目 X（来源标注 [L…-…]，写入时记在 section_versions.sources），
紧接着的下一个版本是顾问手改（agent_code 为空）且不再引用 X，记为“顾问删掉了一次 X 的引用”。
近 RECHECK_DAYS 天内，X 被删掉的章节数 ≥ MIN_REMOVALS、涉及项目数 ≥ MIN_PROJECTS、占 Agent 引用它的章节数
比例 ≥ MIN_RATIO 时，平台把 X 的现行版本标为待复查（recheck_state=pending），进抽检队列，由一位知识专家按七项清单看
（app/kb/autopublish.sample_check：通过清除标记，不通过留在队列里直到改出新版本或下线）。

- 只数现行版本发布之后、上一次复查处理之后的删除：改出新版本或复查过，旧的删除不再算。
- 已在队列里（pending / failed）的不重复标记。人审、自动发布的版本都可能被标记；与自动发布抽检 sample_state 分开，
  不计入自动发布的抽检不通过率（不影响暂停自动发布）。
- 主库（章节版本、交付件）和知识区库（条目、版本）分开查，在内存里合并，不 JOIN。
- 原因只写计数；审计 kb.recheck_mark 只记条目编号和计数，不记交付件正文、标题和项目代号。
worker 每天扫描一次（app/worker.py），也可 python -m app.cli kb-recheck-scan。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..models import KbItem, KbVersion
from ..models.deliverable import DeliverableSection, SectionVersion
from ..models.project import Deliverable
from . import refs as kb_refs

RECHECK_DAYS = 90
MIN_REMOVALS = 3                 # 至少 3 个章节里被删掉
MIN_PROJECTS = 2                 # 至少涉及 2 个项目（不是一个项目的个别情况）
MIN_RATIO = 0.5                  # 至少占 Agent 引用它的章节的一半
OPEN_STATES = ("pending", "failed")


@dataclass
class Usage:
    cited: set[int] = field(default_factory=set)       # Agent 引用过它的章节
    removed: set[int] = field(default_factory=set)     # 顾问随后删掉引用的章节
    projects: set[int] = field(default_factory=set)    # 删掉引用的章节所属项目


def _aware(t: datetime | None) -> datetime | None:
    return t.replace(tzinfo=UTC) if t is not None and t.tzinfo is None else t


def _raw_refs(sv: SectionVersion) -> set[str]:
    return {k["ref"] for k in (sv.sources or {}).get("knowledge", []) if k.get("found") and k.get("ref")}


def _refs(sv: SectionVersion, canon: dict[str, str]) -> set[str]:
    """本版本引用的条目，换成现行编号（旧编号别名和现行编号算同一条，B15）。"""
    return {canon.get(r, r) for r in _raw_refs(sv)}


def usage(db: Session, since: datetime, cutoffs: dict[str, datetime] | None = None) -> dict[str, Usage]:
    """近期各知识条目在交付件里的使用情况。cutoffs：条目编号 → 只数这个时间之后的版本。"""
    cutoffs = cutoffs or {}
    sections = list(db.execute(
        select(DeliverableSection.id, Deliverable.project_id)
        .join(Deliverable, Deliverable.id == DeliverableSection.deliverable_id)
        .where(DeliverableSection.id.in_(select(SectionVersion.section_id).where(SectionVersion.created_at >= since)
                                         .distinct()))).tuples())
    project_of = dict(sections)
    out: dict[str, Usage] = defaultdict(Usage)
    if not project_of:
        return {}
    versions = list(db.scalars(select(SectionVersion).where(SectionVersion.section_id.in_(list(project_of)))
                               .order_by(SectionVersion.section_id, SectionVersion.version)))
    canon = kb_refs.canonical(db, {r for sv in versions for r in _raw_refs(sv)})
    prev: SectionVersion | None = None
    for sv in versions:
        at = _aware(sv.created_at)
        if sv.agent_code and at >= since:
            for ref in _refs(sv, canon):
                if at >= cutoffs.get(ref, since):
                    out[ref].cited.add(sv.section_id)
        if prev is not None and prev.section_id == sv.section_id and prev.agent_code and not sv.agent_code \
                and at >= since:
            for ref in _refs(prev, canon) - _refs(sv, canon):
                if at >= cutoffs.get(ref, since) and _aware(prev.created_at) >= cutoffs.get(ref, since):
                    out[ref].removed.add(sv.section_id)
                    out[ref].projects.add(project_of[sv.section_id])
        prev = sv
    return dict(out)


def _hit(u: Usage) -> bool:
    return (len(u.removed) >= MIN_REMOVALS and len(u.projects) >= MIN_PROJECTS
            and len(u.removed) >= MIN_RATIO * max(len(u.cited), 1))


def reason(u: Usage) -> str:
    return (f"使用反馈：近 {RECHECK_DAYS} 天 {len(u.projects)} 个项目的 {len(u.removed)} 个章节里，"
            f"顾问删掉了 Agent 引用的这一条（Agent 共在 {len(u.cited)} 个章节引用）")


def scan(db: Session, actor: str = "platform") -> list[str]:
    """扫描并标记待复查，返回新标记的条目编号（可重复执行）。"""
    now = utcnow()
    since = now - timedelta(days=RECHECK_DAYS)
    live = {i.doc_id: v for i, v in db.execute(
        select(KbItem, KbVersion).join(KbVersion, KbVersion.id == KbItem.current_version_id)
        .where(KbItem.retired.is_(False), KbVersion.status == "published")).tuples()}
    if not live:
        return []
    cutoffs = {d: max(t for t in (since, _aware(v.published_at), _aware(v.recheck_at)) if t is not None)
               for d, v in live.items()}
    marked: list[str] = []
    for doc_id, u in sorted(usage(db, since, cutoffs).items()):
        v = live.get(doc_id)
        if v is None or v.recheck_state in OPEN_STATES or not _hit(u):
            continue
        v.recheck_state, v.recheck_reason, v.recheck_at = "pending", reason(u)[:200], now
        marked.append(doc_id)
    if marked:
        db.commit()
        audit.record(db, actor=actor, action="kb.recheck_mark", result="allowed", marked=len(marked),
                     doc_ids=marked[:200], rule="usage-feedback")
    return marked
