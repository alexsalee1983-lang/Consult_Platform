"""知识审批流程：草稿 → 提交待审 → 双人审核 → 发布 → 复审 / 修订 / 下线（架构设计第五节“知识”生命周期）。

规则（决策记录 2026-11-08）：
- 双人审核：知识专家审内容（content），知识管理员审合规（compliance）；两个角色各要一人通过，且是两个不同的人。
- 提交人、草稿创建人都不能审核自己的版本；同一人对同一版本只能审一次。
- 任一审核人退回，版本即“已退回”（终态）；要修改就基于它建新版本再提交，审核记录始终对应被审的那一版。
- 来源：投料来的草稿必须是去标识已通过的投料；不经投料直接写的草稿只允许“公开资料”（deid_status=not_needed），
  正文同样先过机密级检测。
- 发布：正文按章节切片写入 kb_chunks（PostgreSQL 自动建中文全文索引）；条目的现行版本切到新版本，旧版本“已替代”。
- 下线：条目标记下线，现行版本“已下线”，检索不再返回；历史版本全部保留。
所有操作写审计（只记编号、状态、角色，不记正文）。
"""
from __future__ import annotations

import difflib
import json
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT, roles_of
from ..models import KbChunk, KbDeidReport, KbItem, KbReview, KbUpload, KbVersion, User
from ..services import guard, storage
from . import meta, parse
from .items import ItemMeta, VersionContent, create_draft, create_item

ROLE_OF = {"content": KNOWLEDGE_EXPERT, "compliance": KNOWLEDGE_ADMIN}
KNOWLEDGE_ROLES = {KNOWLEDGE_EXPERT, KNOWLEDGE_ADMIN}


class ReviewError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def is_knowledge_user(db: Session, user: User) -> bool:
    return bool(roles_of(db, user.id) & KNOWLEDGE_ROLES)


def _require_knowledge(db: Session, user: User) -> None:
    if not is_knowledge_user(db, user):
        raise ReviewError(403, "只有知识专家、知识管理员能编辑和审核知识条目")


def _check_text(title: str, body: str) -> None:
    result = guard.check(title + "\n" + body)
    if result.blocked:
        raise ReviewError(422, result.summary() + "，请修改后再保存")


# ---- 正文来源 ----

def body_from_upload(db: Session, upload_id: int) -> tuple[str, KbDeidReport]:
    """取去标识已通过的投料，拼成 Markdown 正文（章节标题用 ## ）。"""
    up = db.get(KbUpload, upload_id)
    if up is None:
        raise ReviewError(404, "投料记录不存在")
    report = db.scalars(select(KbDeidReport).where(KbDeidReport.upload_id == up.id, KbDeidReport.status == "passed")
                        .order_by(KbDeidReport.id.desc()).limit(1)).first()
    if up.status != "deid_passed" or report is None or not report.deid_key:
        raise ReviewError(409, "这份投料还没有通过去标识，不能建条目")
    data = json.loads(storage.get("knowledge", report.deid_key))
    parts = [f"## {s['heading']}\n\n{s['text']}" if s["heading"] else s["text"] for s in data["sections"]]
    return "\n\n".join(parts), report


def _source_fields(db: Session, c: VersionContent, upload_id: int | None) -> dict:
    if upload_id is not None:
        body, report = body_from_upload(db, upload_id)
        if not c.body.strip():
            c.body = body
        return {"upload_id": upload_id, "deid_status": "passed", "deid_report_id": report.id}
    if c.source_type != "public":
        raise ReviewError(422, "不经投料直接填写正文只适用于“公开资料”；其他来源请先投料并通过去标识")
    return {"deid_status": "not_needed"}


def _apply(v: KbVersion, fields: dict) -> None:
    for k, val in fields.items():
        setattr(v, k, val)


# ---- 草稿 ----

def new_item(db: Session, user: User, m: ItemMeta, c: VersionContent, upload_id: int | None = None) -> KbVersion:
    _require_knowledge(db, user)
    fields = _source_fields(db, c, upload_id)
    _check_text(c.title, c.body)
    try:
        v = create_item(db, m, c, created_by=user.username)
    except meta.MetaError as e:
        raise ReviewError(422, str(e)) from e
    _apply(v, fields)
    db.commit()
    audit.record(db, actor=user.username, action="kb.draft", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, upload_id=upload_id)
    return v


def new_version(db: Session, user: User, item: KbItem, c: VersionContent, upload_id: int | None = None) -> KbVersion:
    _require_knowledge(db, user)
    open_draft = db.scalar(select(KbVersion.id).where(KbVersion.item_id == item.id,
                                                      KbVersion.status.in_(("draft", "in_review"))))
    if open_draft:
        raise ReviewError(409, "这个条目已有未完成的草稿或待审版本，请先处理它")
    if upload_id is None and not c.body.strip():          # 修订默认沿用现行版本正文和来源
        cur = db.get(KbVersion, item.current_version_id) if item.current_version_id else None
        if cur is None:
            raise ReviewError(422, "正文不能为空")
        c.body = cur.body
        fields = {"deid_status": cur.deid_status, "deid_report_id": cur.deid_report_id, "upload_id": cur.upload_id}
        if cur.deid_status == "not_needed" and c.source_type != "public":
            raise ReviewError(422, "现行版本来自公开资料，修订时来源仍须为公开资料")
    else:
        fields = _source_fields(db, c, upload_id)
    _check_text(c.title, c.body)
    try:
        v = create_draft(db, item, c, created_by=user.username)
    except meta.MetaError as e:
        raise ReviewError(422, str(e)) from e
    _apply(v, fields)
    db.commit()
    audit.record(db, actor=user.username, action="kb.draft", result="allowed", doc_id=item.doc_id, version=v.version)
    return v


EDITABLE = ("title", "summary", "body", "applicability", "change_note", "source_detail", "ref_document",
            "ref_version", "ref_status", "effective_date", "review_date")


def edit_draft(db: Session, user: User, v: KbVersion, changes: dict) -> KbVersion:
    _require_knowledge(db, user)
    if v.status != "draft":
        raise ReviewError(409, "只有草稿能修改；已提交的版本请等审核结果，退回的版本请建新版本")
    for k, val in changes.items():
        if k in EDITABLE and val is not None:
            setattr(v, k, val)
    try:
        VersionContent(title=v.title, body=v.body, source_type=v.source_type, source_detail=v.source_detail,
                       ref_document=v.ref_document, ref_version=v.ref_version,
                       ref_status=v.ref_status).check(v.item.layer)
    except meta.MetaError as e:
        db.rollback()
        raise ReviewError(422, str(e)) from e
    _check_text(v.title, v.body)
    db.commit()
    audit.record(db, actor=user.username, action="kb.edit", result="allowed", doc_id=v.item.doc_id, version=v.version,
                 fields=sorted(k for k in changes if k in EDITABLE))
    return v


# ---- 提交与审核 ----

def submit(db: Session, user: User, v: KbVersion) -> KbVersion:
    _require_knowledge(db, user)
    if v.status != "draft":
        raise ReviewError(409, "只有草稿能提交")
    if v.deid_status not in ("passed", "not_needed"):
        raise ReviewError(409, "去标识还没有通过，不能提交")
    if not v.body.strip():
        raise ReviewError(422, "正文不能为空")
    v.status, v.submitted_by, v.submitted_at = "in_review", user.username, utcnow()
    db.commit()
    audit.record(db, actor=user.username, action="kb.submit", result="allowed", doc_id=v.item.doc_id,
                 version=v.version)
    return v


def review(db: Session, user: User, v: KbVersion, role: str, decision: str, comment: str = "") -> KbVersion:
    def deny(status: int, reason: str, message: str):
        audit.record(db, actor=user.username, action="kb.review", result="blocked", doc_id=v.item.doc_id,
                     version=v.version, reason=reason)
        raise ReviewError(status, message)

    if role not in ROLE_OF or decision not in meta.REVIEW_DECISIONS:
        raise ReviewError(422, "role 只能是 content / compliance，decision 只能是 approve / reject")
    if ROLE_OF[role] not in roles_of(db, user.id):
        deny(403, "role_mismatch", "内容审核须由知识专家、合规审核须由知识管理员进行")
    if v.status != "in_review":
        raise ReviewError(409, "这个版本不在待审状态")
    if user.username in (v.submitted_by, v.created_by):
        deny(403, "self_review", "不能审核自己提交或创建的版本")
    done = {r.reviewer: r for r in v.reviews}
    if user.username in done:
        deny(409, "duplicate", "你已经审核过这个版本")
    if any(r.review_role == role and r.decision == "approve" for r in v.reviews):
        deny(409, "role_done", "这个审核角色已有人通过，请由另一角色审核")

    db.add(KbReview(version_id=v.id, reviewer=user.username, review_role=role, decision=decision,
                    comment=comment[:1000]))
    if decision == "reject":
        v.status = "rejected"
    db.commit()
    db.refresh(v)
    audit.record(db, actor=user.username, action="kb.review", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, role=role, decision=decision)
    approved = {r.review_role for r in v.reviews if r.decision == "approve"}
    reviewers = {r.reviewer for r in v.reviews if r.decision == "approve"}
    if v.status == "in_review" and approved == set(ROLE_OF) and len(reviewers) == 2:
        publish(db, v)
    return v


# ---- 发布、下线、复审 ----

def publish(db: Session, v: KbVersion) -> None:
    item = v.item
    doc = parse.parse("body.md", v.body.encode())
    chunks = parse.chunk(doc.sections)
    for i, c in enumerate(chunks, 1):
        db.add(KbChunk(version_id=v.id, seq=i, heading=c.heading[:300], text=c.text))
    if item.current_version_id and item.current_version_id != v.id:
        old = db.get(KbVersion, item.current_version_id)
        if old is not None and old.status == "published":
            old.status = "superseded"
    v.effective_date = v.effective_date or date.today()
    v.review_date = v.review_date or meta.default_review_date(item.layer, v.effective_date)
    v.status, v.published_at = "published", utcnow()
    item.current_version_id, item.updated_at = v.id, utcnow()
    db.commit()
    audit.record(db, actor="kb-review", action="kb.publish", result="allowed", doc_id=item.doc_id, version=v.version,
                 chunks=len(chunks))


def retire(db: Session, user: User, item: KbItem, reason: str) -> KbItem:
    if KNOWLEDGE_ADMIN not in roles_of(db, user.id):
        raise ReviewError(403, "只有知识管理员能下线条目")
    if item.retired:
        raise ReviewError(409, "条目已下线")
    item.retired, item.updated_at = True, utcnow()
    for v in item.versions:
        if v.status in ("published", "draft", "in_review"):
            v.status = "retired"
    db.commit()
    audit.record(db, actor=user.username, action="kb.retire", result="allowed", doc_id=item.doc_id,
                 reason=reason[:200])
    return item


def reconfirm(db: Session, user: User, item: KbItem) -> KbVersion:
    """复审确认内容仍然有效：不改正文，复审日期从今天起顺延一个周期。内容要改就建新版本。"""
    if KNOWLEDGE_EXPERT not in roles_of(db, user.id):
        raise ReviewError(403, "复审确认须由知识专家进行")
    v = db.get(KbVersion, item.current_version_id) if item.current_version_id else None
    if v is None or v.status != "published" or item.retired:
        raise ReviewError(409, "条目没有现行版本")
    v.review_date = meta.default_review_date(item.layer, date.today())
    db.commit()
    audit.record(db, actor=user.username, action="kb.reconfirm", result="allowed", doc_id=item.doc_id,
                 version=v.version, review_date=v.review_date.isoformat())
    return v


def review_due(db: Session, within_days: int = 30) -> list[KbVersion]:
    from datetime import timedelta

    limit = date.today() + timedelta(days=within_days)
    return list(db.scalars(select(KbVersion).join(KbItem, KbItem.current_version_id == KbVersion.id)
                           .where(KbVersion.status == "published", KbItem.retired.is_(False),
                                  KbVersion.review_date <= limit).order_by(KbVersion.review_date)))


# ---- 审核辅助：版本差异、相似条目 ----

def diff_with_current(db: Session, v: KbVersion, max_lines: int = 200) -> list[str]:
    cur = db.get(KbVersion, v.item.current_version_id) if v.item.current_version_id else None
    if cur is None or cur.id == v.id:
        return []
    lines = difflib.unified_diff(cur.body.splitlines(), v.body.splitlines(), f"v{cur.version}", f"v{v.version}",
                                 lineterm="", n=1)
    return list(lines)[:max_lines]


def similar_items(db: Session, v: KbVersion, limit: int = 5) -> list[dict]:
    """同服务产品下、正文与标题相近的已发布条目（可能重复或冲突），供审核人对照。只在 PostgreSQL 上可用。"""
    if db.get_bind().dialect.name != "postgresql":
        return []
    rows = db.execute(text("""
        SELECT i.doc_id, cv.title, max(ts_rank(c.tsv, q)) AS score
        FROM kb_chunks c
        JOIN kb_versions cv ON cv.id = c.version_id AND cv.status = 'published'
        JOIN kb_items i ON i.current_version_id = cv.id AND NOT i.retired
        , replace(plainto_tsquery('zh', :q)::text, '&', '|')::tsquery q   -- 标题里任一词命中即可，按相关度排序
        WHERE c.tsv @@ q AND i.id <> :item AND i.product = :product
        GROUP BY i.doc_id, cv.title ORDER BY score DESC LIMIT :limit
    """), {"q": v.title, "item": v.item_id, "product": v.item.product, "limit": limit}).all()
    return [{"doc_id": r.doc_id, "title": r.title, "score": round(float(r.score), 4)} for r in rows]


# ---- 可见范围 ----

def can_view(db: Session, user: User, item: KbItem, v: KbVersion | None = None) -> bool:
    """知识专家、知识管理员看全部；其他业务角色只看已发布、未下线的现行版本，受限条目按可见角色。"""
    if is_knowledge_user(db, user):
        return True
    if item.retired or (v is not None and v.id != item.current_version_id) or not item.current_version_id:
        return False
    if item.classification == "restricted":
        return "consultant" in (item.visible_roles or [])
    return True
