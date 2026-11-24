"""四期 F4②：改稿与驳回特征（迁移 0054），给 F4③ 触发引擎提供信号；本模块只写特征、汇总，不触发任何动作。

三路信号：
- revision 改稿：改稿差异挖掘（revision_mining.mine）跑完时，每章每类差异（delete / rewrite / add）一行；
  同一交付件重挖按 deliverable_id + source 先删后写，幂等。
- review_waiver 评审驳回：项目经理驳回一条评审阻断问题（quality.waive）时写一行（kind=waiver），
  章节键取问题“位置”指向的章节（指不出章节记交付件代号）。
- practice_reject 做法确认驳回：知识区 kb_practice_decisions 的 reject，只读汇总（分库，不跨库写、不 JOIN），
  按投料来源项目（kb_uploads.source_project）归到项目，在汇总里与主库特征合并。

边界：
- 不存正文和标题：只存章节键（交付件代号.章节序号）、计数、改动幅度均值和摘录哈希（audit.digest，16 位）。
- 开关沿用 settings.f4_revision_mining（生产不开）；严格档、客户不同意回流的项目不写。
- 审计只记计数（mine 的审计里记 features 行数）。
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..core.audit import digest
from ..models import Deliverable, KbPracticeDecision, KbUpload, KbVersion, Project, RevisionFeature
from . import review_issues
from .revision_mining import Change, enabled

SOURCES = ("revision", "review_waiver", "practice_reject")


def _eligible(project: Project) -> bool:
    return enabled() and project.tier != "strict" and bool(project.flowback_allowed)


def _hash(parts: Iterable[str], code: str) -> str:
    text = "\n".join(parts)
    return digest(text.replace(code, "【客户】") if code else text)


def record_revision(db: Session, project: Project, d: Deliverable,
                    sections: list[tuple[str, list[Change]]]) -> int:
    """写改稿特征（先删本交付件旧的改稿特征再写）。sections：[(章节键, 差异列表)]。返回写入行数。不提交。"""
    db.execute(delete(RevisionFeature).where(RevisionFeature.deliverable_id == d.id,
                                             RevisionFeature.source == "revision"))
    n = 0
    for key, changes in sections:
        by_kind: dict[str, list[Change]] = defaultdict(list)
        for c in changes:
            by_kind[c.kind].append(c)
        for kind, group in sorted(by_kind.items()):
            ratios = [round(1 - c.ratio, 4) if kind == "rewrite" else 1.0 for c in group]
            db.add(RevisionFeature(project_id=project.id, deliverable_id=d.id, section_key=key, source="revision",
                                   kind=kind, count=len(group), change_ratio=round(sum(ratios) / len(ratios), 4),
                                   text_hash=_hash((c.before + "\n" + c.after for c in group), project.code)))
            n += 1
    db.flush()
    return n


def record_waiver(db: Session, project: Project, d: Deliverable, issue: dict) -> bool:
    """项目经理驳回一条评审阻断问题时写一行评审驳回特征。开关关、严格档、不同意回流时不写。不提交。"""
    if not _eligible(project):
        return False
    titles = {str(s.seq): s.title for s in d.sections}
    secs = sorted(review_issues.sections_of(issue.get("location", ""), titles), key=int)
    key = f"{d.code}.{secs[0]}" if secs else d.code
    db.add(RevisionFeature(project_id=project.id, deliverable_id=d.id, section_key=key, source="review_waiver",
                           kind="waiver", count=1, change_ratio=0.0,
                           text_hash=_hash((issue.get("dimension", ""), issue.get("problem", "")), project.code)))
    db.flush()
    return True


def _practice_rejects(db: Session, codes: list[str]) -> int:
    """知识区只读：来源项目在 codes 里的公司做法确认驳回次数（知识区表之间 JOIN，同库）。"""
    if not codes:
        return 0
    return int(db.scalar(select(func.count()).select_from(KbPracticeDecision)
                         .join(KbVersion, KbVersion.id == KbPracticeDecision.version_id)
                         .join(KbUpload, KbUpload.id == KbVersion.upload_id)
                         .where(KbPracticeDecision.decision == "reject", KbUpload.source_project.in_(codes))) or 0)


def summary(db: Session, projects: list[Project]) -> dict:
    """按章节键和来源聚合计数（主库特征 + 知识区驳回在这里合并）。不返回项目代号以外的标识。"""
    by_source = dict.fromkeys(SOURCES, 0)
    rows: list[dict] = []
    ids = [p.id for p in projects]
    if ids:
        q = (select(RevisionFeature.section_key, RevisionFeature.source, func.sum(RevisionFeature.count),
                    func.avg(RevisionFeature.change_ratio))
             .where(RevisionFeature.project_id.in_(ids))
             .group_by(RevisionFeature.section_key, RevisionFeature.source)
             .order_by(RevisionFeature.section_key, RevisionFeature.source))
        for key, source, total, ratio in db.execute(q):
            rows.append({"section_key": key, "source": source, "count": int(total or 0),
                         "change_ratio": round(float(ratio or 0), 4)})
            by_source[source] = by_source.get(source, 0) + int(total or 0)
    rejects = _practice_rejects(db, [p.code for p in projects])
    if rejects:
        rows.append({"section_key": "", "source": "practice_reject", "count": rejects, "change_ratio": 0.0})
        by_source["practice_reject"] = rejects
    return {"by_source": by_source, "by_section": rows, "total": sum(by_source.values())}
