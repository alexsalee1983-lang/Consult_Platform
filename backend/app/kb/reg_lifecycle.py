"""三期 B26：法规生命周期跟踪——python -m app.cli kb-reg-lifecycle [--date YYYY-MM-DD] [--dry-run]，worker 每日执行。

依据（L4 条目现行版本）记施行日 effective_date、废止日 repeal_date、替代关系：ref_replaces（本文件替代的旧文件）、
ref_replaced_by（被谁替代）、ref_amends（本文件部分修订的文件）。按日期自动切换现行状态：

- 即将实施且施行日已到 → 现行；
- 废止日已到 → 已废止（写了替代文件的为已被替代）；
- 本文件为现行且写了 ref_replaces：被替代的旧文件（名称或文号对上的依据，现行或即将实施）→ 已被替代，替代文件记本文件；
- 写了 ref_amends 的：被部分修订的文件不改状态，只进受影响清单，由知识专家复核相关条款。

切换沿用 B5 的做法（basis_change.publish_status）：沿用现行版本建新版本、平台核对后直接发布、旧版本保留
（自动化优先原则，二期开发计划第十二节）。发布后 B4 照旧提醒引用它的交付件复核，B9 照旧在评审和签署时拦下已废止、
已被替代的引用；已签署的交付件不改动，只在受影响清单里计数提示。条目有未完成的草稿或待审版本时不动，转人工。

受影响清单（每份状态变化或被部分修订的依据一行）：正文或依据字段写了该文件名称或文号的其他知识条目编号；
引用该依据条目的交付件计数（在办 / 已签署 / 章节数，不返回项目代号，知识侧看不到项目区）。
可重复执行：状态已对的不再改。不联网、不调模型。审计只记计数和条目编号，不记文件名称。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..models import KbItem, KbVersion
from . import basis_change as bc
from . import meta

PLATFORM_REVIEWER = "platform:reg-lifecycle"
AMENDED, CHANGED = "amended", "changed"
_MIN_NAME = 4                     # 名称或文号太短时不按正文包含判断，免得误报


@dataclass
class Change:
    doc_id: str
    old: str
    new: str
    reason: str
    applied: bool = False
    version: int | None = None
    skipped: str = ""


@dataclass
class Affected:
    doc_id: str
    kind: str                      # changed 状态变了 / amended 被部分修订
    items: list[str] = field(default_factory=list)
    deliverables: dict = field(default_factory=dict)


@dataclass
class Report:
    day: date
    applied: bool
    changes: list[Change] = field(default_factory=list)
    affected: list[Affected] = field(default_factory=list)

    def counts(self) -> dict:
        return {"changes": len(self.changes), "applied": sum(c.applied for c in self.changes),
                "skipped": sum(bool(c.skipped) for c in self.changes), "affected": len(self.affected),
                "items": sum(len(a.items) for a in self.affected),
                "open_deliverables": sum(a.deliverables.get("open", 0) for a in self.affected)}


def due_status(v: KbVersion, today: date) -> str | None:
    """按日期该有的现行状态；不需要改时返回 None。"""
    if v.ref_status in ("repealed", "superseded"):
        return None
    if v.repeal_date and v.repeal_date <= today:
        return "superseded" if v.ref_replaced_by else "repealed"
    if v.ref_status == "upcoming" and v.effective_date and v.effective_date <= today:
        return "current"
    return None


def _n(s: str) -> str:
    return bc.norm(unicodedata.normalize("NFKC", s or ""))


def _names(v: KbVersion) -> set[str]:
    return {x for x in (_n(v.ref_document), _n(v.ref_code)) if len(x) >= _MIN_NAME}


def _deliverable_refs(db: Session) -> list[tuple[int, str, dict]]:
    """各交付件章节当前版本的来源标注：(交付件 id, 交付件状态, sources)。"""
    from ..models import Deliverable, DeliverableSection, SectionVersion

    return [tuple(r) for r in db.execute(
        select(Deliverable.id, Deliverable.status, SectionVersion.sources)
        .join(DeliverableSection, DeliverableSection.deliverable_id == Deliverable.id)
        .join(SectionVersion, (SectionVersion.section_id == DeliverableSection.id)
              & (SectionVersion.version == DeliverableSection.current_version))).all()]


def deliverable_counts(refs: list[tuple[int, str, dict]], doc_id: str) -> dict:
    """引用 doc_id 的交付件：在办（未签署）、已签署（只提示）和章节数。"""
    hits = [(d, st) for d, st, src in refs
            if any(isinstance(x, dict) and x.get("found") and x.get("ref") == doc_id
                   for x in (src or {}).get("knowledge", []))]
    return {"open": len({d for d, st in hits if st != "signed"}), "signed": len({d for d, st in hits if st == "signed"}),
            "sections": len(hits)}


def _plan(basis: list[tuple[KbItem, KbVersion]], today: date) -> tuple[dict[int, tuple[str, str, str]], set[int]]:
    """{item.id: (新状态, 替代文件, 原因)} 与被部分修订的条目 id。"""
    plan: dict[int, tuple[str, str, str]] = {}
    for i, v in basis:
        ns = due_status(v, today)
        if ns:
            why = "废止日已到" if ns in ("repealed", "superseded") else "施行日已到"
            plan[i.id] = (ns, v.ref_replaced_by, why)
    amended: set[int] = set()
    for i, v in basis:
        status = plan.get(i.id, (v.ref_status,))[0]
        if status != "current":
            continue
        for target, is_replace in ((v.ref_replaces, True), (v.ref_amends, False)):
            t = _n(target)
            if len(t) < _MIN_NAME:
                continue
            by = f"{v.ref_document} {v.ref_version}".strip()
            for j, w in basis:
                if j.id == i.id or t not in {_n(w.ref_document), _n(w.ref_code)}:
                    continue
                if _n(w.ref_document) == _n(v.ref_document) and w.ref_version == v.ref_version:
                    continue                                  # 同一文件同一版本的其他条款
                if not is_replace:
                    amended.add(j.id)
                elif plan.get(j.id, (w.ref_status,))[0] in ("current", "upcoming"):
                    plan[j.id] = ("superseded", by, f"被替代：{j.doc_id} 由 {i.doc_id} 替代")
    return plan, amended


def _citing_items(db: Session, v: KbVersion, self_id: int) -> list[str]:
    names = _names(v)
    if not names:
        return []
    rows = db.execute(select(KbItem.id, KbItem.doc_id, KbVersion.body, KbVersion.ref_document, KbVersion.ref_code)
                      .join(KbVersion, KbVersion.id == KbItem.current_version_id)
                      .where(KbItem.retired.is_(False), KbVersion.status == "published")
                      .order_by(KbItem.doc_id)).all()
    out = []
    for iid, doc_id, body, doc, code in rows:
        if iid == self_id or _names_eq(names, doc, code):
            continue
        text = _n(body) + "|" + _n(doc) + "|" + _n(code)
        if any(n in text for n in names):
            out.append(doc_id)
    return out


def _names_eq(names: set[str], doc: str, code: str) -> bool:
    """同一份文件的其他条款不算“引用它的条目”。"""
    return bool(names & {_n(doc), _n(code)}) and _n(doc) in names


def run(db: Session, today: date, *, apply: bool = True, actor: str = "platform",
        only: set[str] | None = None) -> Report:
    """only：只处理这些条目编号（排查用）；替代关系仍按全库匹配。"""
    basis = bc._basis_versions(db)
    by_id = {i.id: (i, v) for i, v in basis}
    open_drafts = set(db.scalars(select(KbVersion.item_id).where(KbVersion.status.in_(("draft", "in_review")))))
    plan, amended = _plan(basis, today)
    if only is not None:
        plan = {k: x for k, x in plan.items() if by_id[k][0].doc_id in only}
        amended = {k for k in amended if by_id[k][0].doc_id in only}
    report = Report(day=today, applied=apply)
    refs: list[tuple[int, str, dict]] | None = None
    for iid in sorted(plan, key=lambda k: by_id[k][0].doc_id):
        item, v = by_id[iid]
        ns, replaced_by, why = plan[iid]
        c = Change(doc_id=item.doc_id, old=v.ref_status, new=ns, reason=why)
        report.changes.append(c)
        if iid in open_drafts:
            c.skipped = "条目有未完成的草稿或待审版本，请处理后再登记"
        elif apply:
            note = f"法规生命周期（{today.isoformat()}）：{why}，现行状态改为{meta.REF_STATUSES[ns]}"
            eff = v.effective_date or today if ns == "current" else None
            nv = bc.publish_status(db, PLATFORM_REVIEWER, item, status=ns, replaced_by=replaced_by,
                                   effective=eff, note=note, reviewer=PLATFORM_REVIEWER)
            c.applied, c.version = True, nv.version
    for iid in sorted(set(plan) | amended, key=lambda k: by_id[k][0].doc_id):
        item, v = by_id[iid]
        if refs is None:
            refs = _deliverable_refs(db)
        report.affected.append(Affected(doc_id=item.doc_id, kind=CHANGED if iid in plan else AMENDED,
                                        items=_citing_items(db, v, iid),
                                        deliverables=deliverable_counts(refs, item.doc_id)))
    if report.changes or report.affected:
        audit.record(db, actor=actor, action="kb.reg_lifecycle", result="allowed", day=today.isoformat(),
                     applied=apply, counts=report.counts(), rule="reg-lifecycle",
                     doc_ids=[c.doc_id for c in report.changes][:200],
                     amended=[a.doc_id for a in report.affected if a.kind == AMENDED][:200])
    return report


def format_report(r: Report) -> str:
    lines = [f"法规生命周期 {r.day.isoformat()}（{'已登记' if r.applied else '只比对'}）"]
    for c in r.changes:
        st = f"v{c.version}" if c.applied else (c.skipped or "未登记")
        lines.append(f"  {c.doc_id}  {meta.REF_STATUSES.get(c.old, c.old or '未注明')} → "
                     f"{meta.REF_STATUSES[c.new]}  {c.reason}  [{st}]")
    for a in r.affected:
        d = a.deliverables
        lines.append(f"  受影响 {a.doc_id}（{'被部分修订' if a.kind == AMENDED else '状态变化'}）：引用条目 "
                     f"{'、'.join(a.items) or '无'}；交付件在办 {d.get('open', 0)}、已签署 {d.get('signed', 0)}"
                     f"（只提示）、章节 {d.get('sections', 0)}")
    if len(lines) == 1:
        lines.append("  无到期变化")
    return "\n".join(lines)
