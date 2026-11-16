"""依据变更登记（二期 B5、B5 补充）：python -m app.cli kb-basis-check <清单.csv> --as <知识管理员> [--apply]。

季度官网核对清单（测试助手按官方网站核对，产出 manifest，字段同下载清单，见 app/kb/sources.py 的 COLUMNS；
“文件名”“获取方式”可空）与库内依据元数据（L4 条目现行版本的 ref_document / ref_code / ref_version /
ref_status）比对，逐行归类：

- status   时效变了：清单与条目的文件、版本对得上，现行状态不同（已废止、已被替代、即将实施 → 现行……）。
           --apply 时由平台自动更新：沿用现行版本的正文和来源建新版本，只改现行状态、替代文件（和清单写了的
           施行日期），平台核对后直接发布（审核记录写 platform:basis-check，自动化优先原则，二期开发计划第十二节），
           旧版本保留。发布后 B4 提醒引用它的交付件复核，B9 在评审和签署时拦下已废止、已被替代的引用。
- revised  清单版本与库内版本不同（修订、新版发布）：待重新下载、kb-source-split 切条后 kb-import，不自动改。
- review   只有部分对应：条目同时引用了多份文件（映射、解读），清单说其中一份已废止或已被替代；
           或条目有未完成的草稿。列出编号，由知识专家复核。
- new      库里没有对应条目：新发布的文件，待下载入库。
- unchanged 一致。

只有清单写明的变化才自动改；清单没提到的条目不动（清单可以只核对一部分）。不联网、不调模型。
审计只记清单行数、各类计数和条目编号，不记文件名称。
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import KNOWLEDGE_ADMIN, roles_of
from ..models import KbItem, KbReview, KbVersion, User
from . import meta
from .items import VersionContent, create_draft
from .sources import COLUMNS, STATUS, _parse_date

PLATFORM_REVIEWER = "platform:basis-check"
KIND_STATUS, NEW, REVISED, REVIEW, UNCHANGED = "status", "new", "revised", "review", "unchanged"
KINDS = (KIND_STATUS, REVISED, REVIEW, NEW, UNCHANGED)
KIND_NAMES = {KIND_STATUS: "时效变更", REVISED: "版本不同", REVIEW: "待复核", NEW: "库内没有", UNCHANGED: "一致"}
_MULTI = re.compile(r"[；;、]")


class BasisChangeError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Row:
    line: int
    title: str
    code: str = ""
    version: str = ""
    status: str = "current"
    replaced_by: str = ""
    effective: date | None = None


@dataclass
class Finding:
    line: int
    title: str
    code: str
    kind: str
    status: str                                       # 清单上的现行状态
    doc_ids: list[str] = field(default_factory=list)
    detail: str = ""
    applied: list[str] = field(default_factory=list)  # --apply 后新发布的条目编号

    def to_json(self) -> dict:
        return {**self.__dict__, "kind_name": KIND_NAMES[self.kind],
                "status_name": meta.REF_STATUSES.get(self.status, self.status)}


@dataclass
class Report:
    rows: int = 0
    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    applied: bool = False

    def counts(self) -> dict[str, int]:
        return {k: sum(1 for f in self.findings if f.kind == k) for k in KINDS}

    def to_json(self) -> dict:
        return {"rows": self.rows, "applied": self.applied, "counts": self.counts(),
                "findings": [f.to_json() for f in self.findings], "skipped": self.skipped}


def norm(s: str) -> str:
    """名称、文号、版本规整：全角转半角、去空白和书名号、大写。"""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s《》〈〉<>\"'“”‘’]+", "", s).upper()


def read_rows(data: bytes) -> tuple[list[Row], list[str]]:
    """读核对清单（CSV，UTF-8，表头同下载清单）。返回 (行, 跳过说明)。"""
    rows: list[Row] = []
    skipped: list[str] = []
    for n, raw in enumerate(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))), 2):
        r = {k: next((str(raw[h]).strip() for h in names if raw.get(h) not in (None, "")), "")
             for k, names in COLUMNS.items()}
        if not r["title"] and not r["code"]:
            skipped.append(f"第 {n} 行：缺名称和文号，跳过")
            continue
        status = STATUS.get(r["status"] or "现行")
        if status is None:
            skipped.append(f"第 {n} 行：现行状态“{r['status']}”认不出，跳过")
            continue
        if status == "superseded" and not r["replaced_by"]:
            skipped.append(f"第 {n} 行：已被替代但没写替代文件，跳过")
            continue
        rows.append(Row(line=n, title=r["title"][:200], code=r["code"][:100], version=r["version"][:64],
                        status=status, replaced_by=r["replaced_by"][:200], effective=_parse_date(r["effective"])))
    return rows, skipped


def _basis_versions(db: Session) -> list[tuple[KbItem, KbVersion]]:
    return list(db.execute(
        select(KbItem, KbVersion).join(KbVersion, KbVersion.id == KbItem.current_version_id)
        .where(KbItem.layer == "L4", KbItem.retired.is_(False), KbVersion.status == "published")
        .order_by(KbItem.doc_id)).tuples())


def _exact(row: Row, v: KbVersion) -> bool:
    if row.code and v.ref_code:
        return norm(row.code) == norm(v.ref_code)
    return bool(row.title) and norm(row.title) == norm(v.ref_document)


def _partial(row: Row, v: KbVersion) -> bool:
    """条目引用了多份文件（映射、解读），其中一份是清单这一行。"""
    docs = [norm(p) for p in _MULTI.split(v.ref_document) if p.strip()]
    codes = [norm(p) for p in _MULTI.split(v.ref_code) if p.strip()]
    if len(docs) < 2 and len(codes) < 2:
        return False
    return bool(({norm(row.title), norm(row.code)} - {""}) & set(docs + codes))


def _version_differs(row: Row, v: KbVersion) -> bool:
    return bool(row.version and v.ref_version and v.ref_version != "未注明" and norm(row.version) != norm(v.ref_version))


def compare(db: Session, rows: list[Row]) -> list[Finding]:
    basis = _basis_versions(db)
    open_drafts = set(db.scalars(select(KbVersion.item_id).where(KbVersion.status.in_(("draft", "in_review")))))
    out: list[Finding] = []
    for row in rows:
        exact = [(i, v) for i, v in basis if _exact(row, v)]
        partial = [(i, v) for i, v in basis if (i, v) not in exact and _partial(row, v)]

        def f(kind: str, pairs, detail: str = "") -> Finding:
            return Finding(row.line, row.title, row.code, kind, row.status, [i.doc_id for i, _ in pairs], detail)

        if not exact:
            if partial and row.status in ("repealed", "superseded"):
                out.append(f(REVIEW, partial, "条目同时引用多份文件，其中这一份已" +
                             meta.REF_STATUSES[row.status] + "，请复核"))
            elif partial:
                out.append(f(UNCHANGED, partial))
            else:
                out.append(f(NEW, [], "库内没有对应条目：待下载、切条入库"))
            continue
        differs = [(i, v) for i, v in exact if _version_differs(row, v)]
        if differs:
            libv = "、".join(sorted({v.ref_version for _, v in differs}))
            out.append(f(REVISED, differs, f"库内版本 {libv}，清单版本 {row.version}：待重新下载切条后导入"))
        same = [(i, v) for i, v in exact if (i, v) not in differs]
        changed = [(i, v) for i, v in same if v.ref_status != row.status
                   or (row.status == "superseded" and norm(v.ref_replaced_by) != norm(row.replaced_by))]
        blocked = [(i, v) for i, v in changed if i.id in open_drafts]
        if blocked:
            out.append(f(REVIEW, blocked, "时效有变化，但条目有未完成的草稿或待审版本，请处理后再登记"))
        auto = [(i, v) for i, v in changed if i.id not in open_drafts]
        if auto:
            olds = "、".join(sorted({meta.REF_STATUSES.get(v.ref_status, v.ref_status or "未注明") for _, v in auto}))
            out.append(f(KIND_STATUS, auto, f"{olds} → {meta.REF_STATUSES[row.status]}"))
        if same and not changed:
            out.append(f(UNCHANGED, same))
        if partial and row.status in ("repealed", "superseded"):
            out.append(f(REVIEW, partial, "条目同时引用多份文件，其中这一份已" + meta.REF_STATUSES[row.status] + "，请复核"))
    return out


def _apply_status(db: Session, user: User, row: Row, item: KbItem) -> KbVersion:
    from . import review as rv

    cur = db.get(KbVersion, item.current_version_id)
    note = f"依据变更登记（季度官网核对清单第 {row.line} 行）：现行状态改为{meta.REF_STATUSES[row.status]}"
    c = VersionContent(title=cur.title, body=cur.body, source_type=cur.source_type, source_detail=cur.source_detail,
                       summary=cur.summary, applicability=cur.applicability, change_note=note[:1000],
                       ref_document=cur.ref_document, ref_version=cur.ref_version, ref_status=row.status,
                       ref_code=cur.ref_code, ref_replaced_by=row.replaced_by if row.status == "superseded" else "",
                       effective_date=row.effective or cur.effective_date, review_date=cur.review_date)
    v = create_draft(db, item, c, created_by=user.username)
    v.deid_status, v.deid_report_id, v.upload_id = cur.deid_status, cur.deid_report_id, cur.upload_id
    v.status, v.submitted_by, v.submitted_at = "in_review", user.username, utcnow()
    db.add(KbReview(version_id=v.id, reviewer=PLATFORM_REVIEWER, review_role="platform", decision="approve",
                    comment=note[:1000]))
    db.commit()
    rv.publish(db, v)
    return v


def check(db: Session, user: User, data: bytes, *, apply: bool = False) -> Report:
    """比对核对清单；apply=True 时自动登记时效变更（只有知识管理员能登记）。"""
    if apply and KNOWLEDGE_ADMIN not in roles_of(db, user.id):
        raise BasisChangeError(403, "只有知识管理员能登记依据变更")
    rows, skipped = read_rows(data)
    report = Report(rows=len(rows), skipped=skipped, findings=compare(db, rows), applied=apply)
    if apply:
        by_line = {r.line: r for r in rows}
        done: set[str] = set()                     # 同一条目被清单多行对上时只登记第一行
        for fd in report.findings:
            if fd.kind != KIND_STATUS:
                continue
            for doc_id in fd.doc_ids:
                if doc_id in done:
                    continue
                done.add(doc_id)
                item = db.scalar(select(KbItem).where(KbItem.doc_id == doc_id))
                v = _apply_status(db, user, by_line[fd.line], item)
                fd.applied.append(f"{doc_id} v{v.version}")
    c = report.counts()
    audit.record(db, actor=user.username, action="kb.basis_change", result="allowed", rows=report.rows,
                 applied=apply, counts=c, rule="basis-check",
                 doc_ids=sorted({d for fd in report.findings if fd.kind == KIND_STATUS for d in fd.doc_ids})[:200])
    return report
