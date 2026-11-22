"""知识条目与版本的基础操作：建条目（分配 doc_id）、建草稿版本、取现行版本。

审批流转（提交、双人审核、发布、复审、下线）在审批 PR 里实现；这里只保证写进库的元数据合法。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.db import utcnow
from ..models import KbItem, KbItemAlias, KbMapping, KbVersion
from . import labels, mapping, meta


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
    control_ids: list[str] = field(default_factory=list)   # 公司控制项编号（4A 埋点，k0008）
    products: list[str] = field(default_factory=list)   # 全部服务产品（B15，主产品 product 在前）；空 = 只有主产品

    def check(self) -> None:
        meta.check_classification(self.layer, self.domain, self.product, self.doc_type)
        if not self.product and self.products:
            self.product = str(self.products[0]).strip()
        self.products = labels.check_products(self.product, self.products)
        self.industries = labels.check_industries(self.industries)
        meta.check_tags(self.stage, self.capabilities, self.industries)
        meta.check_visibility(self.classification, self.visible_roles)
        self.tags = check_search_tags(self.tags)
        self.control_ids = check_control_ids(self.control_ids)


SEARCH_TAGS_MAX, SEARCH_TAG_LEN = 10, 20


def check_search_tags(tags: list[str]) -> list[str]:
    """检索加权标签：去空白、去重，至多 10 个、每个至多 20 字。返回规整后的列表。"""
    out = list(dict.fromkeys(str(t).strip() for t in tags or [] if str(t).strip()))
    if len(out) > SEARCH_TAGS_MAX or any(len(t) > SEARCH_TAG_LEN for t in out):
        raise meta.MetaError(f"检索标签至多 {SEARCH_TAGS_MAX} 个、每个至多 {SEARCH_TAG_LEN} 字")
    return out


CONTROL_IDS_MAX = 20
CONTROL_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")


def check_control_ids(ids: list[str]) -> list[str]:
    """公司控制项编号：去空白、转大写、去重，至多 20 个，字母数字和 . _ -、至多 32 位。返回规整后的列表。"""
    out = list(dict.fromkeys(str(t).strip().upper() for t in ids or [] if str(t).strip()))
    if len(out) > CONTROL_IDS_MAX or not all(CONTROL_ID_RE.match(t) for t in out):
        raise meta.MetaError(f"控制项编号至多 {CONTROL_IDS_MAX} 个，只用字母、数字和 . _ -，每个至多 32 位")
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
    ref_code: str = ""
    ref_replaced_by: str = ""
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
    existing = list(db.scalars(select(KbItem.doc_id).where(KbItem.doc_id.like(prefix + "%"))))
    # 已登记为别名的旧编号也不再占用（B15：别名与现行编号互斥）
    existing += [a for a in db.scalars(select(KbItemAlias.alias).where(KbItemAlias.alias.like(prefix + "%")))
                 if meta.DOC_ID_RE.match(a)]
    seqs = [meta.parse_doc_id(d)[4] for d in existing]
    return meta.format_doc_id(m.layer, m.domain, m.product, m.doc_type, max(seqs, default=0) + 1)


def _new_version(db: Session, item: KbItem, c: VersionContent, created_by: str) -> KbVersion:
    c.check(item.layer)
    rows = mapping.parse_rows(c.body) if item.doc_type == "MAP" else []   # 映射矩阵：正文表格必须能解析（B3）
    n = (db.scalar(select(func.max(KbVersion.version)).where(KbVersion.item_id == item.id)) or 0) + 1
    effective = c.effective_date
    review = c.review_date or (meta.default_review_date(item.layer, effective, item.doc_type) if effective else None)
    v = KbVersion(item_id=item.id, version=n, status="draft", title=c.title.strip(), summary=c.summary,
                  body=c.body, applicability=c.applicability, change_note=c.change_note,
                  source_type=c.source_type, source_detail=c.source_detail, ref_document=c.ref_document,
                  ref_version=c.ref_version, ref_status=c.ref_status, ref_code=c.ref_code,
                  ref_replaced_by=c.ref_replaced_by, effective_date=effective,
                  review_date=review, created_by=created_by)
    db.add(v)
    if rows:
        db.flush()
        db.add_all(KbMapping(version_id=v.id, seq=i, **r.as_dict()) for i, r in enumerate(rows, 1))
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
                      visible_roles=list(m.visible_roles), owner=m.owner, tags=list(m.tags), control_ids=list(m.control_ids),
                      created_by=created_by)
        db.add(item)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            if attempt == retries - 1:
                raise
            continue
        labels.sync(db, item, m.products)
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


def replace_mapping_rows(db: Session, v: KbVersion) -> None:
    """草稿正文改了以后重新解析映射行（只用于草稿；已提交、已发布的版本不再改）。格式不对抛 MetaError。"""
    if v.item.doc_type != "MAP":
        return
    rows = mapping.parse_rows(v.body)
    db.execute(delete(KbMapping).where(KbMapping.version_id == v.id))
    db.add_all(KbMapping(version_id=v.id, seq=i, **r.as_dict()) for i, r in enumerate(rows, 1))


def current_version(db: Session, item: KbItem) -> KbVersion | None:
    return db.get(KbVersion, item.current_version_id) if item.current_version_id else None
