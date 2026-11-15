"""依据原文自动核验发布（二期 B10，决策记录 2026-11-15“自动化优先”、二期开发计划第十二节公开知识一级）。

python -m app.cli kb-source-publish <清单.csv> --dir <资料目录> --as <知识专家或知识管理员>

- 从官方原件直接切条（app/kb/sources.py，机械切分、不经模型、不经人手改的中间文件），逐份核验 sources.verify：
  官方来源白名单、原件 sha256、文号或标准号、切分完整、法规条号连续。
- 全部通过的条款由平台自动发布：版本 auto_review=platform，审核记录写“平台核验”（reviewer=platform，
  review_role=platform），不是人；没通过的照常提交审核，走两项人工审核（app/kb/review.py）。
- 每批（一次命令）按 KB_SAMPLE_RATE_SRC（默认 5%，至少 1 条）随机抽检，sample_state=pending，进抽检队列
  （GET /api/kb/samples）；知识专家逐条对照原件判通过或不通过（不能是这批的导入人）。
- 暂停：最近 20 次抽检里不通过的比例超过 KB_SAMPLE_FAIL_MAX（默认 10%），或 KB_SRC_AUTO_PUBLISH=false，
  整批转人工审核；知识专家可以对任何自动发布的版本补抽检，问题率降下来后自动恢复。
- 与现行版本相同的跳过；已有未完成的草稿或待审版本时，内容和原件一致的接着处理，不一致的提示先处理它。
- 审计只记编号、版本、规则、计数和抽检结论，不记正文和意见。
"""
from __future__ import annotations

import math
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..core.db import utcnow
from ..core.permissions import KNOWLEDGE_EXPERT, roles_of
from ..models import KbItem, KbReview, KbVersion, User
from . import importer, parse, sources
from . import review as rv

PLATFORM = "platform"
RECENT_SAMPLES = 20
SAMPLE_DECISIONS = {"pass": "approve", "fail": "reject"}


@dataclass
class DocOutcome:
    file: str
    title: str
    published: int = 0
    manual: int = 0          # 转人工审核
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class PublishReport:
    docs: list[DocOutcome] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)       # 清单里跳过的行
    sampled: list[str] = field(default_factory=list)       # 抽检的 doc_id
    suspended: str = ""                                    # 暂停自动发布的原因

    def to_json(self) -> dict:
        return {"docs": [d.__dict__ for d in self.docs], "skipped": self.skipped, "sampled": self.sampled,
                "suspended": self.suspended}


def sample_stats(db: Session) -> dict:
    """最近 RECENT_SAMPLES 次抽检的结果和是否暂停自动发布。"""
    rows = db.scalars(select(KbVersion.sample_state)
                      .where(KbVersion.auto_review == PLATFORM, KbVersion.sample_state.in_(("passed", "failed")))
                      .order_by(KbVersion.id.desc()).limit(RECENT_SAMPLES)).all()
    failed = sum(1 for s in rows if s == "failed")
    pending = db.scalar(select(KbVersion.id).where(KbVersion.sample_state == "pending").limit(1)) is not None
    limit = get_settings().kb_sample_fail_max
    suspended = bool(rows) and failed / len(rows) > limit
    return {"checked": len(rows), "failed": failed, "fail_max": limit, "suspended": suspended,
            "has_pending": pending, "enabled": get_settings().kb_src_auto_publish}


def _suspended(db: Session) -> str:
    if not get_settings().kb_src_auto_publish:
        return "KB_SRC_AUTO_PUBLISH=false"
    s = sample_stats(db)
    if s["suspended"]:
        return f"最近 {s['checked']} 次抽检不通过 {s['failed']} 次，超过 {s['fail_max']:.0%}"
    return ""


def _target(db: Session, user: User, e) -> tuple[KbVersion | None, str]:
    """导入一条，返回 (要处理的版本, 说明)；版本为 None 时说明是跳过原因或 unchanged。"""
    r = importer.import_entry(db, user, e)
    if r.action in ("created", "revised"):
        return db.get(KbVersion, r.version_id), ""
    if r.action == "unchanged":
        return None, "unchanged"
    if r.action == "skipped":
        item = db.scalar(select(KbItem).where(KbItem.import_key == e.key))
        latest = item.versions[-1] if item is not None and item.versions else None
        if latest is not None and latest.status in ("draft", "in_review") and importer.same(latest, e):
            return latest, ""
    return None, f"{e.key}：{r.message}"


def _auto_publish(db: Session, user: User, v: KbVersion) -> None:
    if v.status == "draft":
        v.status, v.submitted_by, v.submitted_at = "in_review", user.username, utcnow()
    db.add(KbReview(version_id=v.id, reviewer=PLATFORM, review_role=PLATFORM, decision="approve",
                    comment="平台核验：" + "、".join(sources.VERIFY_RULES.values())))
    v.auto_review = PLATFORM
    db.commit()
    rv.publish(db, v)
    audit.record(db, actor=PLATFORM, action="kb.auto_publish", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, operator=user.username, rules=list(sources.VERIFY_RULES))


def run(db: Session, user: User, manifest: Path, src_dir: Path) -> PublishReport:
    rv.require_knowledge(db, user)
    docs, skipped = sources.read_manifest(manifest.read_bytes())
    report = PublishReport(skipped=skipped, suspended=_suspended(db))
    published: list[KbVersion] = []
    for d in docs:
        out = DocOutcome(file=d.file, title=d.title)
        report.docs.append(out)
        f = src_dir / d.file
        if not f.is_file():
            out.message, out.failed = "文件不存在", ["complete"]
            continue
        data = f.read_bytes()
        try:
            entries, rejected, out.message = sources.split_doc(d, data)
        except parse.ParseError as e:
            out.message, out.failed = str(e), ["complete"]
            continue
        out.failed = sources.verify(d, data, entries, rejected)
        auto = not out.failed and not report.suspended
        for e in entries:
            v, why = _target(db, user, e)
            if v is None:
                if why == "unchanged":
                    out.unchanged += 1
                else:
                    out.skipped.append(why)
                continue
            if auto:
                _auto_publish(db, user, v)
                published.append(v)
                out.published += 1
            else:
                if v.status == "draft":
                    rv.submit(db, user, v)
                out.manual += 1
    if published:
        n = max(1, math.ceil(len(published) * get_settings().kb_sample_rate_src))
        pick = secrets.SystemRandom().sample(published, min(n, len(published)))
        for v in pick:
            v.sample_state = "pending"
        db.commit()
        report.sampled = [v.item.doc_id for v in pick]
    audit.record(db, actor=user.username, action="kb.source_publish", result="allowed", docs=len(report.docs),
                 published=len(published), manual=sum(d.manual for d in report.docs), sampled=len(report.sampled),
                 suspended=bool(report.suspended))
    return report


def sample_queue(db: Session) -> list[KbVersion]:
    """待抽检和抽检不通过、还没处理（仍是现行版本）的自动发布版本。"""
    return list(db.scalars(select(KbVersion).join(KbItem, KbItem.current_version_id == KbVersion.id)
                           .where(KbVersion.sample_state.in_(("pending", "failed")), KbItem.retired.is_(False))
                           .order_by(KbVersion.sample_state.desc(), KbVersion.id)))


def sample_check(db: Session, user: User, v: KbVersion, decision: str, comment: str = "") -> KbVersion:
    """抽检：知识专家对照原件判通过（pass）或不通过（fail，写明问题）。自动发布的版本都可以补抽检。"""
    def deny(status: int, reason: str, message: str):
        audit.record(db, actor=user.username, action="kb.sample", result="blocked", doc_id=v.item.doc_id,
                     version=v.version, reason=reason)
        raise rv.ReviewError(status, message)

    if decision not in SAMPLE_DECISIONS:
        raise rv.ReviewError(422, "decision 只能是 pass / fail")
    if KNOWLEDGE_EXPERT not in roles_of(db, user.id):
        deny(403, "role_mismatch", "抽检须由知识专家进行")
    if v.auto_review != PLATFORM or v.status != "published":
        raise rv.ReviewError(409, "只有平台自动发布的现行版本需要抽检")
    if v.sample_state in ("passed", "failed"):
        raise rv.ReviewError(409, "这个版本已经抽检过")
    if user.username in {v.created_by, v.submitted_by}:
        deny(403, "self_review", "不能抽检自己导入的版本")
    if decision == "fail" and len(comment.strip()) < 5:
        raise rv.ReviewError(422, "不通过要写明问题（至少 5 个字）")
    db.add(KbReview(version_id=v.id, reviewer=user.username, review_role="sample",
                    decision=SAMPLE_DECISIONS[decision], comment=comment[:1000]))
    v.sample_state = "passed" if decision == "pass" else "failed"
    db.commit()
    audit.record(db, actor=user.username, action="kb.sample", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, decision=decision)
    return v
