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

from ..models import KbItem, KbVersion, User
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


def _same(v: KbVersion, e: Entry) -> bool:
    c = e.content
    return (v.title, v.body.strip(), v.summary, v.applicability, v.source_detail, v.ref_document, v.ref_version,
            v.ref_status) == (c.title, c.body.strip(), c.summary, c.applicability, c.source_detail, c.ref_document,
                              c.ref_version, c.ref_status)


def import_entry(db: Session, user: User, e: Entry, submit: bool = False) -> ImportResult:
    r = ImportResult(file="", key=e.key)
    if e.content.source_type != "public":
        r.action, r.message = "error", "批量导入只收公开资料；其他来源请走投料箱并通过去标识"
        return r
    item = db.scalar(select(KbItem).where(KbItem.import_key == e.key))
    if item is None:
        v = rv.new_item(db, user, e.meta, e.content)
        v.item.import_key = e.key
        db.commit()
        r.action = "created"
    else:
        r.doc_id = item.doc_id
        tags_changed = list(item.tags or []) != list(e.meta.tags)
        if tags_changed:          # 检索标签是条目级的排序提示，不进版本内容、不走审核，重复导入时直接更新
            item.tags = list(e.meta.tags)
            db.commit()
        latest = item.versions[-1] if item.versions else None
        if latest is not None and latest.status in ("draft", "in_review"):
            r.action, r.message = "skipped", f"已有未完成的 v{latest.version}（{latest.status}），先处理它"
            return r
        if latest is not None and latest.status == "published" and _same(latest, e):
            r.action, r.message = "unchanged", "与现行版本相同" + ("，检索标签已更新" if tags_changed else "")
            return r
        v = rv.new_version(db, user, item, e.content)
        r.action = "revised"
    if submit:
        rv.submit(db, user, v)
    r.doc_id, r.version_id = v.item.doc_id, v.id
    return r


def import_dir(db: Session, user: User, path: Path, submit: bool = False) -> list[ImportResult]:
    # 跳过 README、以 _ 或 . 开头的文件和目录（如 Agent 起草退回的 _rejected/）、未合并的 *.new.md
    files = sorted(p for p in path.rglob("*.md")
                   if p.name != "README.md" and not p.name.endswith(".new.md")
                   and not any(part.startswith(("_", ".")) for part in p.relative_to(path).parts))
    results: list[ImportResult] = []
    seen: dict[str, str] = {}
    for f in files:
        try:
            e = parse_entry(f.read_text(encoding="utf-8"))
            if e.key in seen:
                raise EntryError(f"key 与 {seen[e.key]} 重复")
            seen[e.key] = f.name
            r = import_entry(db, user, e, submit)
        except (EntryError, rv.ReviewError) as ex:
            db.rollback()
            r = ImportResult(file="", action="error", message=str(ex))
        r.file = str(f.relative_to(path))
        results.append(r)
    return results
