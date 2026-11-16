"""知识迁移：把测试环境已发布的公开知识连同审核记录导出，在生产环境校验后导入（二期，决策记录 2026-11-16）。

导出（python -m app.cli kb-export <目录> --as <知识专家或知识管理员>）：
- 只导出未下线、现行版本已发布、来源为公开资料（无需去标识）的条目；抽检或复查不通过的不导出。
- 每条一个条目文件（files.format_entry 格式），另写 manifest.json：每条的文件 sha256、原 doc_id、版本、作者、
  审核记录（审核人、审核项、结论、时间；不带意见原文和 Agent 问题清单）、自动发布方式、抽检状态、生效和复审日期。
- 打印 manifest.json 的 sha256，导入时必须原样提供（经测试机以外的渠道交给生产运维），用来发现清单被改动。
- 依据原文（SRC）排在前面，导入时解读、映射等的引用才能在生产库里核对到。

导入（python -m app.cli kb-import-export <目录> --sha256 <清单哈希> --as <知识专家或知识管理员>）：
- 清单哈希、每个文件的哈希都对得上才导入；任何一个不对整批不导入。
- 按 key 对应生产条目：没有的新建（原 doc_id 未被占用时沿用）；与现行版本相同的跳过；有未完成草稿的跳过。
- 平台在生产上重新自动核验（自动化优先）：机密级检测、资质边界用语、引用核对（依据原文以外的引用类型）、
  元数据（自动发布的依据原文须有文号或标准号，Agent 审核发布的须有摘要和适用条件，解读、映射、执法案例须有依据文件），
  并核对导出的审核记录是完整的（平台核验、Agent 审核或两项人工审核各有一次通过）。
- 全部通过的直接发布：保留原作者和原审核记录（审核人名字照旧），另加一条审核记录 reviewer platform:kb-transfer、
  review_role transfer；抽检待做、补复审、使用反馈待复查的状态照带过去。
- 没通过的提交审核（引用待核拦截时留作草稿），在生产上照常走审核。
审计只记编号、计数和规则，不记正文。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..models import KbItem, KbReview, KbVersion, User
from . import citations, importer
from . import review as rv
from .files import Entry, EntryError, format_entry, parse_entry
from .items import ItemMeta, VersionContent

FORMAT = "kb-export/1"
MANIFEST = "manifest.json"
REVIEWER = "platform:kb-transfer"
ROLE = "transfer"
RULES = {"hash": "文件哈希一致", "reviews": "原审核记录完整", "confidential": "没有机密级内容",
         "boundary": "没有测评 / 认证结论用语", "citations": "引用都已核对到依据原文", "metadata": "元数据齐全"}


class TransferError(ValueError):
    pass


@dataclass
class ExportReport:
    out: Path
    count: int = 0
    manifest_sha256: str = ""


@dataclass
class ImportOutcome:
    file: str
    key: str
    doc_id: str = ""
    action: str = ""             # published / review / draft / unchanged / skipped / error
    failed: list[str] = field(default_factory=list)
    message: str = ""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(d: date | datetime | None) -> str:
    return d.isoformat() if d else ""


def exportable(db: Session) -> list[tuple[KbItem, KbVersion]]:
    rows = db.execute(select(KbItem, KbVersion).join(KbVersion, KbVersion.id == KbItem.current_version_id)
                      .where(KbItem.retired.is_(False), KbVersion.status == "published",
                             KbVersion.source_type == "public", KbVersion.deid_status == "not_needed")).all()
    rows = [(i, v) for i, v in rows if v.sample_state != "failed" and v.recheck_state != "failed"]
    return sorted(rows, key=lambda r: (r[0].doc_type != "SRC", r[0].doc_id))


def _entry(item: KbItem, v: KbVersion) -> Entry:
    m = ItemMeta(layer=item.layer, domain=item.domain, product=item.product, doc_type=item.doc_type,
                 stage=item.stage, capabilities=list(item.capabilities or []), industries=list(item.industries or []),
                 classification=item.classification, visible_roles=list(item.visible_roles or []),
                 owner=item.owner, tags=list(item.tags or []))
    c = VersionContent(title=v.title, body=v.body, source_type=v.source_type, source_detail=v.source_detail,
                       summary=v.summary, applicability=v.applicability, change_note=v.change_note,
                       ref_document=v.ref_document, ref_version=v.ref_version, ref_status=v.ref_status,
                       ref_code=v.ref_code, ref_replaced_by=v.ref_replaced_by, effective_date=v.effective_date)
    return Entry(key=item.import_key or item.doc_id.lower(), meta=m, content=c)


def export(db: Session, user: User, out: Path) -> ExportReport:
    rv.require_knowledge(db, user)
    if out.exists() and any(out.iterdir()):
        raise TransferError(f"目录不是空的：{out}")
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    for n, (item, v) in enumerate(exportable(db), 1):
        e = _entry(item, v)
        name = f"{n:04d}-{e.key}.md"
        data = format_entry(e).encode("utf-8")
        (out / name).write_bytes(data)
        entries.append({
            "file": name, "sha256": sha256(data), "key": e.key, "doc_id": item.doc_id, "version": v.version,
            "created_by": v.created_by, "submitted_by": v.submitted_by, "auto_review": v.auto_review,
            "single_review": v.single_review, "sample_state": v.sample_state, "recheck_state": v.recheck_state,
            "effective_date": _iso(v.effective_date), "review_date": _iso(v.review_date),
            "published_at": _iso(v.published_at),
            "reviews": [{"reviewer": r.reviewer, "review_role": r.review_role, "decision": r.decision,
                         "created_at": _iso(r.created_at)} for r in v.reviews],
        })
    data = json.dumps({"format": FORMAT, "exported_at": _iso(utcnow()), "exported_by": user.username,
                       "count": len(entries), "entries": entries}, ensure_ascii=False, indent=1).encode("utf-8")
    (out / MANIFEST).write_bytes(data)
    r = ExportReport(out=out, count=len(entries), manifest_sha256=sha256(data))
    audit.record(db, actor=user.username, action="kb.export", result="allowed", count=r.count,
                 manifest_sha256=r.manifest_sha256)
    return r


def load(src: Path, expect_sha256: str) -> tuple[dict, dict[str, Entry]]:
    """校验清单和全部文件，返回清单和按文件名的条目；任何一项不对抛 TransferError（整批不导入）。"""
    p = src / MANIFEST
    if not p.is_file():
        raise TransferError(f"缺少 {MANIFEST}")
    raw = p.read_bytes()
    if sha256(raw) != expect_sha256.strip().lower():
        raise TransferError("清单哈希与导出时打印的不一致：清单可能被改动，整批不导入")
    manifest = json.loads(raw)
    if manifest.get("format") != FORMAT:
        raise TransferError(f"清单格式不是 {FORMAT}")
    entries: dict[str, Entry] = {}
    problems = []
    for x in manifest.get("entries", []):
        f = src / x["file"]
        if f.name != x["file"] or not f.is_file():
            problems.append(f"{x['file']}：文件不存在")
            continue
        data = f.read_bytes()
        if sha256(data) != x["sha256"]:
            problems.append(f"{x['file']}：文件哈希不一致")
            continue
        try:
            e = parse_entry(data.decode("utf-8"))
        except EntryError as ex:
            problems.append(f"{x['file']}：{ex}")
            continue
        if e.key != x["key"]:
            problems.append(f"{x['file']}：key 与清单不一致")
            continue
        entries[x["file"]] = e
    if problems:
        raise TransferError("；".join(problems[:5]) + (f" 等 {len(problems)} 处" if len(problems) > 5 else ""))
    return manifest, entries


def reviews_complete(x: dict) -> bool:
    ok = {(r["reviewer"], r["review_role"]) for r in x.get("reviews", []) if r.get("decision") == "approve"}
    if x.get("auto_review") == "platform":
        return ("platform", "platform") in ok
    if x.get("auto_review") == "agent":
        return any(role == "agent" for _, role in ok)
    roles = {role for _, role in ok}
    return {"content", "compliance"} <= roles


def rule_failures(db: Session, v: KbVersion, x: dict) -> list[str]:
    from ..project.quality import qualification_hits
    from ..services import guard

    text = f"{v.title}\n{v.summary}\n{v.applicability}\n{v.body}"
    failed = []
    if not reviews_complete(x):
        failed.append("reviews")
    if guard.check(text).blocked:
        failed.append("confidential")
    if qualification_hits(text):
        failed.append("boundary")
    if v.item.doc_type != "SRC" and citations.pending(citations.for_version(db, v)):
        failed.append("citations")
    meta_ok = True
    if x.get("auto_review") == "platform" and not v.ref_code.strip():
        meta_ok = False
    if x.get("auto_review") == "agent" and (not v.summary.strip() or not v.applicability.strip()):
        meta_ok = False
    if v.item.doc_type in ("REF", "MAP", "ENF") and not v.ref_document.strip():
        meta_ok = False
    if not meta_ok:
        failed.append("metadata")
    return failed


def _publish(db: Session, v: KbVersion, x: dict, manifest_sha: str) -> None:
    v.created_by = x.get("created_by") or v.created_by
    v.status, v.submitted_by, v.submitted_at = "in_review", x.get("submitted_by") or v.created_by, utcnow()
    for r in x.get("reviews", []):
        db.add(KbReview(version_id=v.id, reviewer=r["reviewer"][:64], review_role=r["review_role"][:16],
                        decision=r["decision"][:16], comment="测试环境原审核记录",
                        created_at=datetime.fromisoformat(r["created_at"]) if r.get("created_at") else utcnow()))
    db.add(KbReview(version_id=v.id, reviewer=REVIEWER, review_role=ROLE, decision="approve",
                    comment=f"迁移核验（清单 {manifest_sha[:12]}）：" + "、".join(RULES.values())))
    v.auto_review = x.get("auto_review", "")
    v.single_review = bool(x.get("single_review"))
    v.sample_state = x.get("sample_state", "") if x.get("sample_state") == "pending" else ""
    v.recheck_state = x.get("recheck_state", "") if x.get("recheck_state") == "pending" else ""
    if x.get("effective_date"):
        v.effective_date = date.fromisoformat(x["effective_date"])
    if x.get("review_date"):
        v.review_date = date.fromisoformat(x["review_date"])
    db.commit()
    rv.publish(db, v)


def _one(db: Session, user: User, e: Entry, x: dict, manifest_sha: str) -> ImportOutcome:
    o = ImportOutcome(file=x["file"], key=e.key)
    item = db.scalar(select(KbItem).where(KbItem.import_key == e.key))
    if item is None:
        v = rv.new_item(db, user, e.meta, e.content)
        v.item.import_key = e.key
        want = x.get("doc_id", "")
        if want and want != v.item.doc_id and db.scalar(select(KbItem.id).where(KbItem.doc_id == want)) is None:
            v.item.doc_id = want              # 原 doc_id 未被占用时沿用，交付件和评测里的引用不变
        db.commit()
    else:
        latest = item.versions[-1] if item.versions else None
        if latest is not None and latest.status in ("draft", "in_review"):
            o.doc_id, o.action, o.message = item.doc_id, "skipped", f"已有未完成的 v{latest.version}（{latest.status}）"
            return o
        if latest is not None and latest.status == "published" and importer.same(latest, e):
            o.doc_id, o.action = item.doc_id, "unchanged"
            return o
        v = rv.new_version(db, user, item, e.content)
    o.doc_id = v.item.doc_id
    o.failed = rule_failures(db, v, x)
    if not o.failed:
        _publish(db, v, x, manifest_sha)
        o.action = "published"
    else:
        try:
            rv.submit(db, user, v)
            o.action = "review"
        except rv.ReviewError as ex:
            o.action, o.message = "draft", str(ex)
    audit.record(db, actor=user.username, action="kb.transfer_import", result="allowed", doc_id=o.doc_id,
                 version=v.version, outcome=o.action, failed=o.failed)
    return o


def import_export(db: Session, user: User, src: Path, expect_sha256: str) -> list[ImportOutcome]:
    rv.require_knowledge(db, user)
    try:
        manifest, entries = load(src, expect_sha256)
    except TransferError:
        audit.record(db, actor=user.username, action="kb.transfer_import", result="blocked", reason="verify")
        raise
    out = []
    for x in manifest["entries"]:
        try:
            out.append(_one(db, user, entries[x["file"]], x, expect_sha256.strip().lower()))
        except rv.ReviewError as ex:
            db.rollback()
            out.append(ImportOutcome(file=x["file"], key=x["key"], action="error", message=str(ex)))
    return out
