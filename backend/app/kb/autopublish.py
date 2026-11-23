"""依据原文自动核验发布（二期 B10，决策记录 2026-11-15“自动化优先”、二期开发计划第十二节公开知识一级）。

python -m app.cli kb-source-publish <清单.csv> --dir <资料目录> --as <知识专家或知识管理员>

- 从官方原件直接切条（app/kb/sources.py，机械切分、不经模型、不经人手改的中间文件），逐份核验 sources.verify：
  官方来源白名单、原件 sha256、文号或标准号、切分完整、法规条号连续。
- 全部通过的条款由平台自动发布：版本 auto_review=platform，审核记录写“平台核验”（reviewer=platform，
  review_role=platform），不是人；没通过的照常提交审核，走两项人工审核（app/kb/review.py）。
- 每批（一次命令）按统计抽样定抽样量（三期 B20）：批量 N、可接受缺陷率 KB_SAMPLE_DEFECT_MAX（默认 10%）、
  置信度 KB_SAMPLE_CONFIDENCE（默认 80%），按超几何分布取最小的 n，使缺陷率达到上限时 n 条里一条问题都抽不到的概率
  不超过 1-置信度（零接收抽样）；大批量不随 N 线性涨。KB_SAMPLE_RATE_SRC / _PARTIAL 是按比例的兜底下限（有条款被退回的份
  按 40% 兜底，负责人 2026-11-16），取两者大者。抽中的 sample_state=pending，进抽检队列（GET /api/kb/samples）；
  知识专家逐条对照原件判通过或不通过（不能是这批的导入人）。
- 暂停（停止规则，三期 B20）：最近 20 次抽检攒满 20 个样本后，不通过比例超过 KB_SAMPLE_FAIL_MAX（默认 10%）即暂停；
  不满 20 个时只在“凑满 20 个也必超上限”（不通过数已超过 上限 × 20）时提前暂停，免得一两次抽检就把自动发布停掉。
  KB_SRC_AUTO_PUBLISH=false 也暂停。暂停时整批转人工审核；知识专家可以对任何自动发布的版本补抽检，问题率降下来后自动恢复。
- 清单改了标题：条目编号由标题算出，标题一改就成了新编号；若这份的条款正文与库里另一份公开资料的现行条款逐条相同，
  判为改名，整份跳过并提示（把标题改回原样，或先下线旧条目），不重复入库。
- 与现行版本相同的跳过；已有未完成的草稿或待审版本时，内容和原件一致的接着处理，不一致的提示先处理它。
- 审计只记编号、版本、规则、计数和抽检结论，不记正文和意见。
"""
from __future__ import annotations

import math
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..core.db import utcnow
from ..core.permissions import KNOWLEDGE_EXPERT, roles_of
from ..models import KbItem, KbReview, KbVersion, User
from . import candidate, importer, parse, sources
from . import review as rv

PLATFORM = "platform"
AGENT = "agent"                    # 知识审核 Agent 自动发布（二期 B11，app/kb/agent_review.py）
RECENT_SAMPLES = 20
SAMPLE_DECISIONS = {"pass": "approve", "fail": "reject"}


@dataclass
class DocOutcome:
    file: str
    title: str
    published: int = 0
    manual: int = 0          # 转人工审核
    rejected: int = 0        # 机密级检测退回、不入库的条款数
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


def _enabled(kind: str) -> tuple[bool, str]:
    if kind == AGENT:
        return get_settings().kb_agent_auto_publish, "KB_AGENT_AUTO_PUBLISH"
    return get_settings().kb_src_auto_publish, "KB_SRC_AUTO_PUBLISH"


def sample_size(population: int, defect_max: float, confidence: float) -> int:
    """零接收抽样的最小样本量（超几何分布）：批里缺陷条数达到 ceil(N × defect_max) 时，n 条全无缺陷的概率 ≤ 1 - confidence。"""
    if population <= 0 or confidence <= 0:
        return 0
    defects = max(1, math.ceil(population * defect_max))
    miss, n = 1.0, 0                       # miss = P(n 条里一条缺陷都没有)
    while n < population and miss > 1 - confidence:
        miss *= (population - defects - n) / (population - n)
        n += 1
    return n


def batch_sample_count(population: int, partial: bool) -> int:
    """一批自动发布的版本要抽检几条：统计抽样量与按比例兜底取大者，不超过批量。"""
    s = get_settings()
    rate = s.kb_sample_rate_src_partial if partial else s.kb_sample_rate_src
    n = max(sample_size(population, s.kb_sample_defect_max, s.kb_sample_confidence), math.ceil(population * rate))
    return min(population, n)


def stop_rule(checked: int, failed: int, fail_max: float) -> bool:
    """停止规则：攒满 RECENT_SAMPLES 个样本才按比例判断；不满时只在凑满也必超上限时提前停。"""
    if checked >= RECENT_SAMPLES:
        return failed / checked > fail_max
    return failed > fail_max * RECENT_SAMPLES


def sample_stats(db: Session, kind: str = PLATFORM) -> dict:
    """某类自动发布（platform 平台核验 / agent 知识审核 Agent）最近 RECENT_SAMPLES 次抽检的结果和是否暂停。"""
    rows = db.scalars(select(KbVersion.sample_state)
                      .where(KbVersion.auto_review == kind, KbVersion.sample_state.in_(("passed", "failed")))
                      .order_by(KbVersion.id.desc()).limit(RECENT_SAMPLES)).all()
    failed = sum(1 for s in rows if s == "failed")
    pending = db.scalar(select(KbVersion.id).where(KbVersion.auto_review == kind,
                                                   KbVersion.sample_state == "pending").limit(1)) is not None
    limit = get_settings().kb_sample_fail_max
    over = stop_rule(len(rows), failed, limit)
    return {"checked": len(rows), "failed": failed, "fail_max": limit, "suspended": over,
            "armed": len(rows) >= RECENT_SAMPLES,
            "has_pending": pending, "enabled": _enabled(kind)[0]}


def suspended(db: Session, kind: str = PLATFORM) -> str:
    """暂停自动发布的原因；空串表示没暂停。"""
    on, var = _enabled(kind)
    if not on:
        return f"{var}=false"
    s = sample_stats(db, kind)
    if s["suspended"]:
        return f"最近 {s['checked']} 次抽检不通过 {s['failed']} 次，超过 {s['fail_max']:.0%}"
    from . import compare                   # 每批入库后的对比评测未达标，也暂停（二期 B1 补充）

    return compare.gate_reason(db)


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


def renamed_from(db: Session, d: sources.Doc, entries: list) -> str:
    """清单改了标题时返回库里原来的标题：本份条目编号一个都不在库里，而另一份公开资料的现行条款与本份逐条相同。"""
    if not entries or db.scalar(select(KbItem.id).where(KbItem.import_key.in_([e.key for e in entries])).limit(1)):
        return ""
    bodies = sorted(e.content.body for e in entries)
    probe = max(bodies, key=len)
    current = (select(KbItem.import_key, KbVersion.ref_document, KbVersion.body)
               .join(KbVersion, KbItem.current_version_id == KbVersion.id)
               .where(KbItem.import_key.like("src-%"), KbItem.retired.is_(False)))
    for key, title, _ in db.execute(current.where(KbVersion.body == probe)).all():
        prefix = "-".join(key.split("-")[:2]) + "-"
        rows = db.execute(current.where(KbItem.import_key.like(prefix + "%"))).all()
        if title != d.title and sorted(r.body for r in rows) == bodies:
            return title
    return ""


def _auto_publish(db: Session, user: User, v: KbVersion) -> None:
    if v.status == "draft":
        v.status, v.submitted_by, v.submitted_at = "in_review", user.username, utcnow()
    db.add(KbReview(version_id=v.id, reviewer=PLATFORM, review_role=PLATFORM, decision="approve",
                    comment="平台核验：" + "、".join(sources.VERIFY_RULES.values())))
    v.auto_review = PLATFORM
    db.commit()
    shadow = candidate.enabled()
    if shadow:                       # 候选影子期（三期 B24①）：先当候选，达标后晋级
        candidate.stage(db, v, PLATFORM)
    else:
        rv.publish(db, v)
    audit.record(db, actor=PLATFORM, action="kb.auto_publish", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, operator=user.username, rules=list(sources.VERIFY_RULES), candidate=shadow)


def run(db: Session, user: User, manifest: Path, src_dir: Path) -> PublishReport:
    rv.require_knowledge(db, user)
    docs, skipped = sources.read_manifest(manifest.read_bytes())
    report = PublishReport(skipped=skipped, suspended=suspended(db))
    published: list[KbVersion] = []
    partial: list[KbVersion] = []          # 有条款被退回的那几份里自动发布的，抽检比例更高
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
        out.rejected = len(rejected)
        old = renamed_from(db, d, entries)
        if old:
            out.message = (f"条款正文与库里的“{old}”逐条相同，疑似清单改了标题（条目编号随标题变），整份跳过；"
                           "请把标题改回原样，或先下线旧条目再发布")
            continue
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
                (partial if rejected else published).append(v)
                out.published += 1
            else:
                if v.status == "draft":
                    rv.submit(db, user, v)
                out.manual += 1
    pick: list[KbVersion] = []
    for group, is_partial in ((published, False), (partial, True)):
        n = batch_sample_count(len(group), is_partial)
        if n:
            pick += secrets.SystemRandom().sample(group, n)
    if pick:
        for v in pick:
            v.sample_state = "pending"
        db.commit()
        report.sampled = [v.item.doc_id for v in pick]
    published += partial
    audit.record(db, actor=user.username, action="kb.source_publish", result="allowed", docs=len(report.docs),
                 published=len(published), manual=sum(d.manual for d in report.docs), sampled=len(report.sampled),
                 suspended=bool(report.suspended))
    return report


def sample_queue(db: Session) -> list[KbVersion]:
    """待抽检和抽检不通过、还没处理（仍是现行版本）的自动发布版本，以及使用反馈待复查的现行版本（B13，app/kb/recheck.py）。
    候选影子期（三期 B24①）的候选版本不是现行版本，也在队列里（抽检判不通过即退回人工）。"""
    return list(db.scalars(select(KbVersion).join(KbItem, KbItem.id == KbVersion.item_id)
                           .where(or_(KbItem.current_version_id == KbVersion.id, KbVersion.status == candidate.STATUS),
                                  or_(KbVersion.sample_state.in_(("pending", "failed")),
                                      KbVersion.recheck_state.in_(("pending", "failed"))),
                                  KbItem.retired.is_(False))
                           .order_by(KbVersion.sample_state.desc(), KbVersion.recheck_state.desc(), KbVersion.id)))


def sample_check(db: Session, user: User, v: KbVersion, decision: str, comment: str = "") -> KbVersion:
    """抽检：知识专家对照原件判通过（pass）或不通过（fail，写明问题）。自动发布（平台核验、知识审核 Agent）的版本都可以补抽检。"""
    def deny(status: int, reason: str, message: str):
        audit.record(db, actor=user.username, action="kb.sample", result="blocked", doc_id=v.item.doc_id,
                     version=v.version, reason=reason)
        raise rv.ReviewError(status, message)

    if decision not in SAMPLE_DECISIONS:
        raise rv.ReviewError(422, "decision 只能是 pass / fail")
    if KNOWLEDGE_EXPERT not in roles_of(db, user.id):
        deny(403, "role_mismatch", "抽检须由知识专家进行")
    sample = v.auto_review in (PLATFORM, AGENT) and v.sample_state not in ("passed", "failed")
    recheck = v.recheck_state == "pending"           # 使用反馈待复查（B13）：人审版本也可以，不计入抽检不通过率
    shadow = v.status == candidate.STATUS and not v.item.retired          # 候选（三期 B24①）
    current = v.item.current_version_id == v.id and not v.item.retired and v.status == "published"
    if not (current or shadow) or not (sample or recheck):
        if v.sample_state in ("passed", "failed") and v.recheck_state != "pending":
            raise rv.ReviewError(409, "这个版本已经抽检过")
        raise rv.ReviewError(409, "只有自动发布或待复查的现行版本需要抽检")
    if user.username in {v.created_by, v.submitted_by}:
        deny(403, "self_review", "不能抽检自己导入的版本")
    if decision == "fail" and len(comment.strip()) < 5:
        raise rv.ReviewError(422, "不通过要写明问题（至少 5 个字）")
    state = "passed" if decision == "pass" else "failed"
    db.add(KbReview(version_id=v.id, reviewer=user.username, review_role="sample" if sample else "recheck",
                    decision=SAMPLE_DECISIONS[decision], comment=comment[:1000]))
    if sample:                                       # 一次看完同时了结抽检和待复查
        v.sample_state = state
    if recheck:
        v.recheck_state, v.recheck_at = state, utcnow()
    db.commit()
    audit.record(db, actor=user.username, action="kb.sample", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, decision=decision, sample=sample, recheck=recheck, candidate=shadow)
    if shadow and decision == "fail":                # 候选抽检不通过：不晋级，退回人工审核
        candidate.return_to_review(db, v, user.username, "sample_failed")
    return v
