"""知识条目与版本的基础操作：建条目（分配 doc_id）、建草稿版本、取现行版本。

审批流转（提交、双人审核、发布、复审、下线）在审批 PR 里实现；这里只保证写进库的元数据合法。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.db import utcnow
from ..models import KbItem, KbVersion
from . import meta


@dataclass
class ItemMeta:
    layer: str
    domain: str
    product: str
    doc_type: str
    stage: str
    capabilities: list[str]
    industries: list[str] = field(default_factory=lambda: ["通用"])
    classification: str = "internal"
    visible_roles: list[str] = field(default_factory=lambda: ["consultant", "admin"])
    owner: str = ""
    tags: list[str] = field(default_factory=list)   # 检索加权标签（二期 B2），对应模块配置的 kb_tags

    def check(self) -> None:
        meta.check_classification(self.layer, self.domain, self.product, self.doc_type)
        meta.check_tags(self.stage, self.capabilities, self.industries)
        meta.check_visibility(self.classification, self.visible_roles)
        self.tags = check_search_tags(self.tags)


SEARCH_TAGS_MAX, SEARCH_TAG_LEN = 10, 20


def check_search_tags(tags: list[str]) -> list[str]:
    """检索加权标签：去空白、去重，至多 10 个、每个至多 20 字。返回规整后的列表。"""
    out = list(dict.fromkeys(str(t).strip() for t in tags or [] if str(t).strip()))
    if len(out) > SEARCH_TAGS_MAX or any(len(t) > SEARCH_TAG_LEN for t in out):
        raise meta.MetaError(f"检索标签至多 {SEARCH_TAGS_MAX} 个、每个至多 {SEARCH_TAG_LEN} 字")
    return out


@dataclass
class VersionContent:
    title: str
    body: str
    source_type: str
    source_detail: str = ""
    summary: str = ""
    applicability: str = ""
    change_note: str = ""
    ref_document: str = ""
    ref_version: str = ""
    ref_status: str = ""
    effective_date: date | None = None
    review_date: date | None = None

    def check(self, layer: str) -> None:
        if not self.title.strip():
            raise meta.MetaError("标题不能为空")
        if len(self.title) > 200:
            raise meta.MetaError("标题不能超过 200 字")
        meta.check_source(self.source_type, self.source_detail)
        meta.check_reference(layer, self.ref_document, self.ref_version, self.ref_status)


def next_doc_id(db: Session, m: ItemMeta) -> str:
    prefix = meta.doc_id_prefix(m.layer, m.domain, m.product, m.doc_type)
    existing = db.scalars(select(KbItem.doc_id).where(KbItem.doc_id.like(prefix + "%"))).all()
    seqs = [meta.parse_doc_id(d)[4] for d in existing]
    return meta.format_doc_id(m.layer, m.domain, m.product, m.doc_type, max(seqs, default=0) + 1)


def _new_version(db: Session, item: KbItem, c: VersionContent, created_by: str) -> KbVersion:
    c.check(item.layer)
    n = (db.scalar(select(func.max(KbVersion.version)).where(KbVersion.item_id == item.id)) or 0) + 1
    effective = c.effective_date
    review = c.review_date or (meta.default_review_date(item.layer, effective) if effective else None)
    v = KbVersion(item_id=item.id, version=n, status="draft", title=c.title.strip(), summary=c.summary,
                  body=c.body, applicability=c.applicability, change_note=c.change_note,
                  source_type=c.source_type, source_detail=c.source_detail, ref_document=c.ref_document,
                  ref_version=c.ref_version, ref_status=c.ref_status, effective_date=effective,
                  review_date=review, created_by=created_by)
    db.add(v)
    item.updated_at = utcnow()
    return v


def create_item(db: Session, m: ItemMeta, c: VersionContent, created_by: str, retries: int = 3) -> KbVersion:
    """新建条目并建第 1 版草稿，返回该版本（已提交）。doc_id 序号并发冲突时重试。"""
    m.check()
    c.check(m.layer)
    for attempt in range(retries):
        item = KbItem(doc_id=next_doc_id(db, m), layer=m.layer, domain=m.domain, product=m.product,
                      doc_type=m.doc_type, stage=m.stage, capabilities=list(m.capabilities),
                      industries=list(m.industries), classification=m.classification,
                      visible_roles=list(m.visible_roles), owner=m.owner, tags=list(m.tags),
                      created_by=created_by)
        db.add(item)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            if attempt == retries - 1:
                raise
            continue
        v = _new_version(db, item, c, created_by)
        db.commit()
        return v
    raise RuntimeError("unreachable")


def create_draft(db: Session, item: KbItem, c: VersionContent, created_by: str) -> KbVersion:
    """给已有条目建新的草稿版本（修订）。条目已下线时不允许。"""
    if item.retired:
        raise meta.MetaError("条目已下线，不能再修订")
    v = _new_version(db, item, c, created_by)
    db.commit()
    return v


def current_version(db: Session, item: KbItem) -> KbVersion | None:
    return db.get(KbVersion, item.current_version_id) if item.current_version_id else None
