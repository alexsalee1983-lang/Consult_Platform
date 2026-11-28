"""四期 A32①：Word 回传（平台规划刷新设计第十二节）。

顾问把本平台导出的交付件 Word 改完传回，平台逐章比对，改过的章新建版本，再自动发起质量评审。规则：

1. 只认本平台导出的本交付件：文档属性“标识”（导出时写 export.EXPORT_MARK + 导出编号）或页脚“导出编号 XXXXXXXX”，
   编号要在审计里查得到本项目、本交付件的 Word 导出记录；导出时各章版本号也从这条审计记录取（不信任文件里的内容）。
2. 严格档一律拒收（导出的 Word 带回现场回填真名，不能再传回平台）；已签署 409；文件 ≤10MB，压缩炸弹拒收。
3. 按导出时的章节标题“序号　标题”（Heading 1）切章；{{正文}} 模板的正文结束书签之后的内容不算。
   每章转成块（标题、列表、引用、段落、表格）与导出时版本按同样规则排版出的块比较（只比文字，不比字体）；
   没改的块原样保留原 Markdown（加粗、编号、空行不丢），改过和新增的块按 Word 段落样式转回 Markdown（加粗保留）。
4. 改过的章：导出后平台里这一章已有新版本的报冲突（409，一章都不写）；全文先过机密级检测，命中不保存；
   写入仍走 deliverables.write_section（同样的机密级、来源核对、版本只追加），修改说明记“Word 回传（导出编号）”。
5. 写完自动发起质量评审（trigger word-return）；评审是增量的，只把改过的章交给评审 Agent，平台规则照常全文重算。
   已有进行中的评审等发起不了时不报错，返回原因，顾问稍后手动发起（门禁要求评审针对当前修订，不会漏过）。
6. 修订痕迹 = 返回体里每章与导出时版本的统一 diff（之后在版本对比里也能看到）。文件不保存。
审计 deliverable.word_return 只记导出编号、改动章号、文件大小和摘要哈希，不记正文。
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..models import AuditLog, Deliverable, Project, User
from ..services import guard
from . import deliverables as dsvc
from . import export as export_svc

ACTION = "deliverable.word_return"
MAX_BYTES = 10 * 1024 * 1024
MAX_UNZIPPED = 20 * MAX_BYTES
NOTE = "Word 回传（导出编号 {}）"
EMPTY_MARK = "（本章还没有正文）"

_EXPORT_ID = re.compile(r"^[0-9A-F]{8}$")
_FOOTER_ID = re.compile(r"导出编号\s*([0-9A-F]{8})")
_SECTION_HEAD = re.compile(r"^(\d{1,2})　")
_BOLD_MARK = re.compile(r"\*\*(.+?)\*\*")
_SPACE = re.compile(r"\s+")


class WordReturnError(ValueError):
    def __init__(self, status: int, detail: object):
        super().__init__(str(detail))
        self.status, self.detail = status, detail


@dataclass
class Block:
    kind: str                     # h2…h9 / li / ol / quote / p / table
    text: str                     # 比较用的纯文字（去掉加粗标记、压空白）
    md: list[str]                 # Markdown 行
    start: int = 0                # 原正文里的行号范围（只对导出时版本有意义）
    end: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.text


@dataclass
class Result:
    export_id: str
    changed: list[dict] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)
    review: dict | None = None
    review_note: str = ""


def _norm(text: str) -> str:
    return _SPACE.sub(" ", _BOLD_MARK.sub(r"\1", text)).strip()


# ---- 导出时版本 → 块（与 export._Writer.body 同样的规则） ----

def markdown_blocks(body: str) -> list[Block]:
    lines = body.splitlines()
    out: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()
        if not s or s.startswith("```") or re.fullmatch(r"[-*_]{3,}", s):
            i += 1
            continue
        if s.startswith("|"):
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            rows = [r for r in lines[i:j] if not export_svc._TABLE_SEP.match(r.strip())]
            text = "\n".join("|".join(_norm(c) for c in r.strip().strip("|").split("|")) for r in rows)
            out.append(Block("table", text, lines[i:j], i, j))
            i = j
            continue
        if m := export_svc._HEADING.match(s):
            kind, text = f"h{min(len(m.group(1)) + 1, 9)}", m.group(2).strip()
        elif m := export_svc._BULLET.match(line):
            kind, text = "li", m.group(1)
        elif (m := export_svc._NUMBERED.match(line)) and not export_svc._REF.match(s):
            kind, text = "ol", m.group(1)
        elif s.startswith(">"):
            kind, text = "quote", s.lstrip("> ").strip()
        else:
            kind, text = "p", s
        out.append(Block(kind, _norm(text), [lines[i]], i, i + 1))
        i += 1
    return out


# ---- Word → 块 ----

def _style(p: Paragraph) -> str:
    try:
        return (p.style.name or "") if p.style is not None else ""
    except (KeyError, ValueError):
        return ""


def _inline_md(p: Paragraph) -> str:
    parts: list[str] = []
    for r in p.runs:
        t = r.text
        if not t:
            continue
        parts.append(f"**{t}**" if r.bold and t.strip() else t)
    return re.sub(r"\*\*\*\*", "", "".join(parts)).strip()


def _para_block(p: Paragraph) -> Block | None:
    text = p.text.strip()
    if not text:
        return None
    style = _style(p)
    m = re.match(r"(?:Heading|标题)\s*(\d)", style)
    if m and int(m.group(1)) >= 2:
        n = int(m.group(1))
        return Block(f"h{n}", _norm(text), [f"{'#' * (n - 1)} {text}"])
    md = _inline_md(p) or text
    if style.startswith("List Bullet"):
        return Block("li", _norm(text), [f"- {md}"])
    if style.startswith("List Number"):
        return Block("ol", _norm(text), [f"1. {md}"])
    if style in ("Quote", "Intense Quote", "引用"):
        return Block("quote", _norm(text), [f"> {md}"])
    return Block("p", _norm(text), [md])


def _table_block(t: Table) -> Block | None:
    rows: list[list[str]] = []
    for row in t.rows:
        cells: list[str] = []
        for c in row.cells:
            v = _SPACE.sub(" ", c.text).strip().replace("|", "｜")
            cells.append(v)
        rows.append(cells)
    if not rows or not any(any(r) for r in rows):
        return None
    md = [f"| {' | '.join(rows[0])} |", "|" + "---|" * len(rows[0])] + [f"| {' | '.join(r)} |" for r in rows[1:]]
    return Block("table", "\n".join("|".join(_norm(c) for c in r) for r in rows), md)


def _has_end_bookmark(el) -> bool:
    if el.tag == qn("w:bookmarkStart"):
        return el.get(qn("w:name")) == export_svc.BODY_END_BOOKMARK
    return any(b.get(qn("w:name")) == export_svc.BODY_END_BOOKMARK for b in el.iter(qn("w:bookmarkStart")))


def word_sections(doc, seqs: set[int]) -> dict[int, list[Block]]:
    """按“序号　标题”切章：Heading 1 样式，或模板没有标题样式时整段文字是“序号　标题”。序号须递增。"""
    out: dict[int, list[Block]] = {}
    current: int | None = None
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if _has_end_bookmark(child):
            break
        if tag == "p":
            p = Paragraph(child, doc)
            m = _SECTION_HEAD.match(p.text.strip())
            if m and int(m.group(1)) in seqs and (current is None or int(m.group(1)) > current):
                current = int(m.group(1))
                out[current] = []
                continue
            if current is None:
                continue
            b = _para_block(p)
        elif tag == "tbl" and current is not None:
            b = _table_block(Table(child, doc))
        else:
            continue
        if b is not None:
            out[current].append(b)
    for seq, blocks in out.items():                      # 导出时空章的占位文字不算正文
        if len(blocks) == 1 and blocks[0].kind == "p" and blocks[0].text == EMPTY_MARK:
            out[seq] = []
    return out


# ---- 合并：没改的块原样保留原 Markdown ----

def merge(base_body: str, base: list[Block], new: list[Block]) -> str:
    lines = base_body.splitlines()
    chunks: list[tuple[str, str, list[str]]] = []      # (首块类别, 末块类别, Markdown 行)
    sm = difflib.SequenceMatcher(a=[b.key for b in base], b=[b.key for b in new], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            chunks.append((base[i1].kind, base[i2 - 1].kind, lines[base[i1].start:base[i2 - 1].end]))
        else:
            chunks.extend((b.kind, b.kind, b.md) for b in new[j1:j2])
    out: list[str] = []
    prev = ""
    for first, last, md in chunks:
        if out and not (first == prev and first in ("li", "ol")):   # 同一列表里的相邻项不空行
            out.append("")
        out.extend(md)
        prev = last
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---- 识别导出 ----

def _open(data: bytes):
    if len(data) > MAX_BYTES:
        raise WordReturnError(413, f"文件超过 {MAX_BYTES // 1024 // 1024}MB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "word/document.xml" not in z.namelist():
                raise WordReturnError(422, "只收 Word（.docx）文件，请上传本平台导出、改好后另存的 .docx")
            if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
                raise WordReturnError(422, "文件解压后过大")
        return Document(io.BytesIO(data))
    except WordReturnError:
        raise
    except Exception as e:  # noqa: BLE001
        raise WordReturnError(422, "Word 文件无法打开（可能已损坏或加密），请上传本平台导出的 .docx") from e


def export_ids(doc) -> list[str]:
    ids: list[str] = []
    ident = (doc.core_properties.identifier or "").strip()
    if ident.startswith(export_svc.EXPORT_MARK):
        ids.append(ident[len(export_svc.EXPORT_MARK):])
    for s in doc.sections:
        for footer in (s.footer, s.first_page_footer, s.even_page_footer):
            for p in footer.paragraphs:
                ids += _FOOTER_ID.findall(p.text)
    return [x for x in dict.fromkeys(ids) if _EXPORT_ID.match(x)]


def find_export(db: Session, project: Project, d: Deliverable, ids: list[str]) -> tuple[str, dict] | None:
    """在审计里找本项目、本交付件的 Word 导出记录 → (导出编号, 各章版本号)。"""
    for eid in ids:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "deliverable.export",
                                                 AuditLog.result == "allowed",
                                                 AuditLog.project_code == project.code,
                                                 AuditLog.detail.like(f'%"export_id": "{eid}"%'))).all()
        for a in rows:
            try:
                det = json.loads(a.detail)
            except ValueError:
                continue
            if det.get("export_id") == eid and det.get("deliverable") == d.code and det.get("format", "docx") == "docx":
                return eid, {int(k): int(v) for k, v in (det.get("versions") or {}).items()}
    return None


def _refuse(db: Session, user: User, project: Project, d: Deliverable, reason: str, status: int, detail: object,
            **meta) -> None:
    audit.record(db, actor=user.username, action=ACTION, result="blocked", project_code=project.code,
                 deliverable=d.code, reason=reason, **meta)
    raise WordReturnError(status, detail)


# ---- 回传 ----

def apply(db: Session, user: User, project: Project, d: Deliverable, data: bytes) -> Result:
    file_meta = {"size": len(data), "file_sha": hashlib.sha256(data).hexdigest()[:16]}
    if project.tier == "strict":
        _refuse(db, user, project, d, "strict_tier", 422,
                "严格档项目不收回传的 Word：导出稿带回现场回填后不再进入平台，修改请在平台里按占位符写", **file_meta)
    if d.status == "signed":
        _refuse(db, user, project, d, "signed", 409, f"{d.code} 已签署，不能再修改", **file_meta)
    try:
        doc = _open(data)
    except WordReturnError as e:
        _refuse(db, user, project, d, "bad_file", e.status, e.detail, **file_meta)
    found = find_export(db, project, d, export_ids(doc))
    if found is None:
        _refuse(db, user, project, d, "not_platform_export", 422,
                f"只收本平台导出的 {d.code} Word：文件里找不到本交付件的导出编号（文档属性或页脚），"
                "请先在本页导出 Word 再修改后回传", **file_meta)
    export_id, versions = found
    sections = {s.seq: s for s in d.sections}
    uploaded = word_sections(doc, set(sections))
    res = Result(export_id=export_id)
    plan: list[tuple[int, int, str]] = []                 # (章号, 导出时版本, 新正文)
    conflicts: list[int] = []
    for seq, s in sorted(sections.items()):
        if seq not in uploaded:
            res.missing.append(seq)
            continue
        base_ver = versions.get(seq, 0)
        bv = dsvc.get_version(s, base_ver) if base_ver else None
        base_body = bv.body if bv else ""
        base_blocks = markdown_blocks(base_body)
        new_blocks = uploaded[seq]
        if [b.key for b in base_blocks] == [b.key for b in new_blocks]:
            res.unchanged.append(seq)
            continue
        if s.current_version != base_ver:
            conflicts.append(seq)
            continue
        body = merge(base_body, base_blocks, new_blocks) if base_blocks else \
            "\n\n".join("\n".join(b.md) for b in new_blocks)
        if not body.strip():
            res.unchanged.append(seq)                     # Word 里整章删空：不写空版本，按没改处理
            continue
        plan.append((seq, base_ver, body))
    meta = {"export_id": export_id, **file_meta}
    if conflicts:
        listed = "、".join(f"第 {x} 章" for x in conflicts)
        _refuse(db, user, project, d, "conflict", 409,
                f"{listed}在导出后平台里已有新版本，Word 里也改了，没有写入任何一章；请重新导出最新版本再改", conflicts=conflicts,
                **meta)
    if not plan:
        _refuse(db, user, project, d, "no_change", 409, "与导出时相比没有改动，没有新建版本", **meta)
    for seq, _, body in plan:
        if len(body) > dsvc.MAX_BODY_CHARS:
            _refuse(db, user, project, d, "too_long", 422, f"第 {seq} 章超过 {dsvc.MAX_BODY_CHARS} 字，请拆成多章",
                    **meta)
    result = guard.check("\n".join(body for _, _, body in plan))
    if result.blocked:
        audit.record(db, actor=user.username, action=ACTION, result="blocked", project_code=project.code,
                     deliverable=d.code, reason="guard", categories=sorted({f.kind for f in result.findings}), **meta)
        raise WordReturnError(422, {"blocked": True, "summary": f"{result.summary()}，没有保存",
                                    "findings": [{"kind": f.kind, "label": f.label, "excerpt": f.excerpt}
                                                 for f in result.findings[:10]]})
    note = NOTE.format(export_id)
    for seq, base_ver, body in plan:
        try:
            w = dsvc.write_section(db, user, project, d, seq, body, note=note, base_version=base_ver)
        except dsvc.DeliverableError as e:
            raise WordReturnError(e.status, e.detail) from e
        s = sections[seq]
        res.changed.append({"seq": seq, "title": s.title, "from_version": base_ver, "version": w.version.version,
                            "diff": list(difflib.unified_diff(
                                (dsvc.get_version(s, base_ver).body if base_ver else "").splitlines(),
                                body.splitlines(), f"v{base_ver}", f"v{w.version.version}", lineterm="", n=2))})
    audit.record(db, actor=user.username, action=ACTION, result="allowed", project_code=project.code,
                 deliverable=d.code, changed=[x["seq"] for x in res.changed],
                 versions={str(x["seq"]): x["version"] for x in res.changed}, unchanged=len(res.unchanged),
                 missing=res.missing, **meta)
    from . import quality                              # 避免循环导入：quality 依赖 deliverables

    db.refresh(d)
    try:
        r = quality.request_review(db, user, project, d, trigger="word-return")
        res.review = {"id": r.id, "status": r.status}
    except quality.QualityError as e:
        res.review_note = f"没有自动发起质量评审：{e.detail}；请稍后在本页发起"
    return res
