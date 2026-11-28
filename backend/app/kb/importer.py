"""批量导入知识条目文件（python -m app.cli kb-import <目录> --as <知识专家或知识管理员>）。

- 只收公开资料（source_type: public）：其他来源必须走投料箱 + 去标识（app/kb/review.py 的规则）。
- 按 key 判断：新 key 建条目和第 1 版草稿；已有 key 且内容有变化就建修订版草稿；没变化跳过；
  已有未完成的草稿或待审版本时跳过并提示。
- 导入的都是草稿；--submit 时一并提交审核（仍要双人审核才发布）。
- 正文经机密级检测（与页面上建条目一样），每条导入都写审计。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..models import KbItem, KbVersion, User
from . import labels, meta, multilang, poison
from . import review as rv
from .files import Entry, EntryError, parse_entry


@dataclass
class ImportResult:
    file: str
    key: str = ""
    action: str = ""          # created / revised / unchanged / skipped / error
    doc_id: str = ""
    version_id: int | None = None
    message: str = ""


def same(v: KbVersion, e: Entry) -> bool:
    c = e.content
    # 文件没写施行日期时不比（发布时平台按发布日补填）
    return (v.title, v.body.strip(), v.summary, v.applicability, v.source_detail, v.ref_document, v.ref_version,
            v.ref_status, v.ref_code, v.ref_replaced_by, v.ref_replaces, v.ref_amends, v.repeal_date) == (
        c.title, c.body.strip(), c.summary, c.applicability, c.source_detail, c.ref_document, c.ref_version,
        c.ref_status, c.ref_code, c.ref_replaced_by, c.ref_replaces, c.ref_amends, c.repeal_date)         and (c.effective_date is None or c.effective_date == v.effective_date)


def import_entry(db: Session, user: User, e: Entry, submit: bool = False, ai_baseline: bool = False) -> ImportResult:
    r = ImportResult(file="", key=e.key)
    if ai_baseline:
        rv.check_ai_baseline(e.content)
    elif e.content.source_type != "public":
        r.action, r.message = "error", "批量导入只收公开资料；其他来源请走投料箱并通过去标识"
        return r
    item = db.scalar(select(KbItem).where(KbItem.import_key == e.key))
    if item is None:
        v = rv.new_item(db, user, e.meta, e.content, ai_baseline=ai_baseline)
        v.item.import_key = e.key
        db.commit()
        r.action = "created"
    else:
        r.doc_id = item.doc_id
        tags_changed = list(item.tags or []) != list(e.meta.tags)
        controls_changed = list(item.control_ids or []) != list(e.meta.control_ids)
        industries_changed = list(item.industries or []) != list(e.meta.industries)
        # 检索标签、控制项、产品、行业是条目级标签，不进版本内容、不走审核，重复导入时直接更新（产品、行业记审计）
        item.tags, item.control_ids = list(e.meta.tags), list(e.meta.control_ids)
        item.industries = list(e.meta.industries)
        if (item.language, item.jurisdiction, item.translation_of, item.translation_notice) != (
                e.meta.language, e.meta.jurisdiction, e.meta.translation_of, e.meta.translation_notice):
            try:                                    # 四期 B28①：语种、辖区、译本字段是条目级，重复导入时复核后更新
                multilang.check_link(db, e.meta.translation_of, e.meta.language, item.doc_id)
            except meta.MetaError as err:
                db.rollback()
                raise rv.ReviewError(422, str(err)) from err
            item.language, item.jurisdiction = e.meta.language, e.meta.jurisdiction
            item.translation_of, item.translation_notice = e.meta.translation_of, e.meta.translation_notice
        changed = labels.sync(db, item, e.meta.products or [e.meta.product], actor=user.username)
        db.commit()
        products_changed = "product" in changed
        latest = item.versions[-1] if item.versions else None
        if latest is not None and latest.status in ("draft", "in_review"):
            r.action, r.message = "skipped", f"已有未完成的 v{latest.version}（{latest.status}），先处理它"
            return r
        if latest is not None and latest.status in ("published", "candidate") and same(latest, e):
            notes = (("，检索标签已更新", tags_changed), ("，控制项已更新", controls_changed),
                     ("，产品已更新", products_changed), ("，行业已更新", industries_changed))
            r.action, r.message = "unchanged", "与现行版本相同" + "".join(n for n, hit in notes if hit)
            return r
        v = rv.new_version(db, user, item, e.content, ai_baseline=ai_baseline)
        r.action = "revised"
    if ai_baseline:
        audit.record(db, actor=user.username, action="kb.ai_baseline_import", result="allowed", doc_id=v.item.doc_id,
                     version=v.version)
    if submit:
        rv.submit(db, user, v)
    r.doc_id, r.version_id = v.item.doc_id, v.id
    return r


def import_dir(db: Session, user: User, path: Path, submit: bool = False, ai_baseline: bool = False) -> list[ImportResult]:
    if ai_baseline and get_settings().is_prod:             # 生产整批拒收，一条也不建
        raise rv.ReviewError(403, "生产环境不收 AI 基线草稿")
    # 跳过 README、以 _ 或 . 开头的文件和目录（如 Agent 起草退回的 _rejected/）、未合并的 *.new.md
    files = sorted(p for p in path.rglob("*.md")
                   if p.name != "README.md" and not p.name.endswith(".new.md")
                   and not any(part.startswith(("_", ".")) for part in p.relative_to(path).parts))
    results: list[ImportResult] = []
    seen: dict[str, str] = {}
    key = poison.import_batch_key(str(path.resolve()))      # 四期 B42 第①层：同一次导入记同一批次，可一键清除
    for f in files:
        try:
            e = parse_entry(f.read_text(encoding="utf-8"))
            if e.key in seen:
                raise EntryError(f"key 与 {seen[e.key]} 重复")
            seen[e.key] = f.name
            with poison.batch(key):
                r = import_entry(db, user, e, submit, ai_baseline)
        except (EntryError, rv.ReviewError) as ex:
            db.rollback()
            r = ImportResult(file="", action="error", message=str(ex))
        r.file = str(f.relative_to(path))
        results.append(r)
    return results
