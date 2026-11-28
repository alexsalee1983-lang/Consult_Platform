"""交付件 Word 导出（M3 第 ⑥ 个 PR）。

导出 = 模板 + 各章当前版本正文。规则：

1. 模板从配置位置读取（EXPORT_TEMPLATE，决策记录 2026-11-10）：不配置时用内置默认模板（python-docx 空白文档，按正式公文排版：
   A4、封面单独一页、标题黑体黑色、正文宋体小四 1.5 倍行距，负责人 2026-11-28）；
   配置了但文件不存在、打不开或缺页面设置时拒绝导出（503），不悄悄退回默认模板，避免用错模板交给客户。
   模板里可以写占位符，导出时替换（正文段落、表格、页眉页脚都查）：
   {{项目代号}} {{服务}} {{行业}} {{交付件编号}} {{交付件名称}} {{修订}} {{导出日期}} {{状态}}；
   单独一段写 {{正文}} 的，各章正文插在这一段的位置（这一段删去），没有就接在模板正文末尾。
2. 未签署的交付件每页页眉标“草稿”；页脚写导出人、导出时间和导出编号（审计里能按编号查到这次导出），作为水印留痕。
3. 来源标注（[D-12] [I-02-05] [O-05] [L4-…]）在正文中保留原样，同一章里每个标注第一次出现处加 Word 脚注：
   证据写编号、类别和标题（访谈写受访者角色），知识库条目写编号、层级和名称；核对不上的写“未核对到”。
   [推断] 不加脚注。
4. 表格（内置和配置模板都适用）：表头灰底加粗并跨页重复，行不跨页拆开，列宽按内容分配，五号字。
   正文按常用 Markdown 写法排版：# 标题、- / * 列表、1. 编号列表、> 引用、| 表格 |、**加粗**；其余按普通段落。
5. 严格档项目导出的是占位符版本（M4 ④b，写入时已查占位符）；没有大纲的交付件不能导出（409）。

审计只记项目、交付件编号、修订号、各章版本号、文件大小和摘要哈希、模板类别（内置 / 配置）、是否草稿和导出编号，
不记正文、章节标题和模板路径。

四期 A32①（Word 回传）：文档属性“标识”写 EXPORT_MARK + 导出编号；套模板且 {{正文}} 后还有模板内容时，
在正文末尾放书签 BODY_END_BOOKMARK，回传时据此只读正文部分（规则见 app/project/word_return.py）。
"""
from __future__ import annotations

import hashlib
import io
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from docx import Document
from docx.document import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.opc.constants import CONTENT_TYPE as CT, RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt, RGBColor
from docx.text.paragraph import Paragraph
from lxml import etree
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..core.db import utcnow
from ..core.errors import BAD_CHARS
from ..kb import refs as kb_refs_svc
from ..kb.meta import LAYERS
from ..models import Deliverable, Evidence, KbVersion, Project, SummaryCard, User
from . import deliverables as dsvc

TZ = ZoneInfo("Asia/Shanghai")
MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DRAFT_MARK = "草稿 · 未经签署 · 仅供内部审阅"
BODY_MARK = "{{正文}}"
EXPORT_MARK = "CP-EXPORT-"                 # 文档属性“标识”= EXPORT_MARK + 导出编号（四期 A32① Word 回传凭此匹配）
BODY_END_BOOKMARK = "cp_body_end"
KIND_NAMES = {"document": "客户资料", "interview": "访谈纪要", "observation": "现场观察"}
STATUS_NAMES = {"not_started": "未开始", "drafting": "起草中", "review": "待审核", "signed": "已签署"}

_REF = re.compile(r"\[((?:I|D|O|S)-\d{2,4})(-\d{1,3})?\]|" + kb_refs_svc.MARK_PATTERN)   # 第 3 组是知识编号
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d{1,3}[.、)）]\s*(.*)$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


class ExportError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class ExportResult:
    data: bytes
    filename: str
    export_id: str
    draft: bool


# ---- 模板 ----

def _load_template() -> tuple[DocxDocument, str]:
    path = (get_settings().export_template or "").strip()
    if not path:
        doc = Document()
        _default_fonts(doc)
        sec = doc.sections[0]                                 # A4，上下 2.54、左右 3.17 厘米（Word 默认中文版式）
        sec.page_width, sec.page_height = Cm(21), Cm(29.7)
        sec.top_margin = sec.bottom_margin = Cm(2.54)
        sec.left_margin = sec.right_margin = Cm(3.17)
        return doc, "builtin"
    p = Path(path)
    if not p.is_file():
        raise ExportError(503, "导出模板文件不存在，请系统管理员检查 EXPORT_TEMPLATE 配置")
    try:
        doc = Document(str(p))
    except Exception as e:                                   # 不是有效的 .docx
        raise ExportError(503, "导出模板无法打开（需要 .docx 文件），请系统管理员检查 EXPORT_TEMPLATE 配置") from e
    if not doc.sections:                                     # 缺页面设置（sectPr），表格、页眉页脚都没法写
        raise ExportError(503, "导出模板缺少页面设置（用 Word 另存一次可修复），请系统管理员检查 EXPORT_TEMPLATE 配置")
    return doc, "configured"


# 内置模板按正式公文排版（负责人 11-28）：样式名 → (中文字体, 西文字体, 字号, 加粗, 段前, 段后)
_BUILTIN_STYLES = {
    "Normal": ("宋体", "Times New Roman", 12, False, 0, 6),
    "Title": ("黑体", "Arial", 22, True, 0, 12),
    "Heading 1": ("黑体", "Arial", 16, True, 18, 8),
    "Heading 2": ("黑体", "Arial", 14, True, 12, 6),
    "Heading 3": ("黑体", "Arial", 12, True, 8, 4),
    "List Bullet": ("宋体", "Times New Roman", 12, False, 0, 2),
    "List Number": ("宋体", "Times New Roman", 12, False, 0, 2),
}


def _default_fonts(doc: DocxDocument) -> None:
    """内置模板：正文宋体小四、西文 Times New Roman、1.5 倍行距；标题黑体黑色（不用 Word 默认的蓝色和下划线）。"""
    for name, (cn, en, size, bold, before, after) in _BUILTIN_STYLES.items():
        try:
            style = doc.styles[name]
        except KeyError:
            continue
        style.font.name = en
        style.font.size = Pt(size)
        style.font.bold = bold
        style.font.italic = False
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), cn)
        fmt = style.paragraph_format
        fmt.space_before, fmt.space_after = Pt(before), Pt(after)
        fmt.line_spacing = 1.5 if name == "Normal" else 1.3 if name.startswith("List") else 1.15
        ppr = style.element.get_or_add_pPr()
        for bdr in ppr.findall(qn("w:pBdr")):                  # Title 默认带蓝色下划线
            ppr.remove(bdr)
    doc.styles["Title"].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _cover(doc: DocxDocument, title: str, lines: list[str]) -> None:
    """内置模板封面：标题居中，项目信息逐行居中，之后分页。"""
    for _ in range(6):
        doc.add_paragraph()
    doc.add_paragraph(title, style="Title")
    doc.add_paragraph()
    for i, line in enumerate(lines):
        p = doc.add_paragraph(line)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if i == len(lines) - 1:
            p.add_run().add_break(WD_BREAK.PAGE)


def _replace_in_paragraph(p: Paragraph, values: dict[str, str]) -> None:
    text = p.text
    if "{{" not in text or not any(k in text for k in values):
        return
    for k, v in values.items():
        text = text.replace(k, v)
    runs = p.runs
    if not runs:
        return
    runs[0].text = text                                      # 占位符可能被 Word 拆在几个 run 里：合并到第一个
    for r in runs[1:]:
        r.text = ""


def _paragraphs_everywhere(doc: DocxDocument):
    def walk(container):
        yield from container.paragraphs
        for t in container.tables:
            for row in t.rows:
                for cell in row.cells:
                    yield from walk(cell)
    yield from walk(doc)
    for s in doc.sections:
        for hf in (s.header, s.footer, s.first_page_header, s.first_page_footer, s.even_page_header,
                   s.even_page_footer):
            if not hf.is_linked_to_previous:
                yield from walk(hf)


# ---- 脚注 ----

_FOOTNOTES_XML = (
    f'<w:footnotes {nsdecls("w")}>'
    '<w:footnote w:type="separator" w:id="-1"><w:p><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/>'
    '</w:pPr><w:r><w:separator/></w:r></w:p></w:footnote>'
    '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:pPr><w:spacing w:after="0" w:line="240" '
    'w:lineRule="auto"/></w:pPr><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
    '</w:footnotes>'
)


class _Footnotes:
    """python-docx 不支持脚注：直接维护 word/footnotes.xml。模板里已有脚注部件就沿用（编号接着往下排）。"""

    def __init__(self, doc: DocxDocument):
        self.part: Part | None = None
        for rel in doc.part.rels.values():
            if rel.reltype == RT.FOOTNOTES:
                self.part = rel.target_part
        if self.part is None:
            self.part = Part(PackURI("/word/footnotes.xml"), CT.WML_FOOTNOTES, _FOOTNOTES_XML.encode("utf-8"),
                             doc.part.package)
            doc.part.relate_to(self.part, RT.FOOTNOTES)
        self.root = parse_xml(self.part.blob)
        ids = [int(x) for x in self.root.xpath("./w:footnote/@w:id", namespaces=self.root.nsmap) if x.lstrip("-").isdigit()]
        self.next_id = max([0, *ids]) + 1
        self.count = 0

    def add(self, paragraph: Paragraph, text: str) -> None:
        fid = self.next_id
        self.next_id += 1
        self.count += 1
        fn = parse_xml(
            f'<w:footnote {nsdecls("w")} w:id="{fid}"><w:p><w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
            '<w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> </w:t></w:r></w:p></w:footnote>')
        fn.xpath(".//w:t", namespaces=fn.nsmap)[-1].text = " " + text
        self.root.append(fn)
        run = paragraph.add_run()
        rpr = run._r.get_or_add_rPr()
        va = etree.SubElement(rpr, qn("w:vertAlign"))
        va.set(qn("w:val"), "superscript")
        ref = etree.SubElement(run._r, qn("w:footnoteReference"))
        ref.set(qn("w:id"), str(fid))

    def save(self) -> None:
        self.part._blob = etree.tostring(self.root, xml_declaration=True, encoding="UTF-8", standalone=True)


# ---- 正文排版 ----

class _Writer:
    def __init__(self, doc: DocxDocument, anchor: Paragraph | None, notes: _Footnotes, labels: dict[str, str]):
        self.doc = doc
        self.anchor = anchor                                  # 插在 {{正文}} 段之前；None 则接在末尾
        self.notes = notes
        self.labels = labels                                  # 来源标注 → 脚注文字
        self.noted: set[str] = set()                          # 本章已加过脚注的标注

    def _para(self, style: str | None = None) -> Paragraph:
        p = self.anchor.insert_paragraph_before() if self.anchor is not None else self.doc.add_paragraph()
        if style:
            try:
                p.style = self.doc.styles[style]
            except KeyError:                                  # 公司模板里没有这个样式：用正文样式
                pass
        return p

    def heading(self, text: str, level: int) -> None:
        p = self._para(f"Heading {min(max(level, 1), 9)}")
        if not p.style.name.startswith("Heading"):
            self._inline(p, text, bold=True)
        else:
            self._inline(p, text)

    def _inline(self, p: Paragraph, text: str, bold: bool = False) -> None:
        pos = 0
        for m in _BOLD.finditer(text):
            self._refs(p, text[pos:m.start()], bold)
            self._refs(p, m.group(1), True)
            pos = m.end()
        self._refs(p, text[pos:], bold)

    def _refs(self, p: Paragraph, text: str, bold: bool) -> None:
        pos = 0
        for m in _REF.finditer(text):
            p.add_run(text[pos:m.end()]).bold = bold or None
            ref = m.group(3) or (m.group(1) + (m.group(2) or ""))
            key = m.group(3) or m.group(1)
            if ref not in self.noted:
                self.noted.add(ref)
                self.notes.add(p, f"{ref}：{self.labels.get(key, '未核对到对应的证据或知识库条目')}")
            pos = m.end()
        if pos < len(text):
            p.add_run(text[pos:]).bold = bold or None

    def _table(self, rows: list[str]) -> None:
        cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows if not _TABLE_SEP.match(r.strip())]
        if not cells:
            return
        width = max(len(r) for r in cells)
        t = self.doc.add_table(rows=len(cells), cols=width)
        try:
            t.style = self.doc.styles["Table Grid"]
        except KeyError:
            pass
        for i, r in enumerate(cells):
            for j in range(width):
                cell_p = t.cell(i, j).paragraphs[0]
                cell_p.paragraph_format.space_after = Pt(0)
                cell_p.paragraph_format.line_spacing = 1.15
                self._inline(cell_p, r[j] if j < len(r) else "", bold=(i == 0))
                for run in cell_p.runs:                       # 表格用五号字
                    run.font.size = Pt(10.5)
        sec = self.doc.sections[-1]
        _format_table(t, cells, width, (sec.page_width - sec.left_margin - sec.right_margin) / 360000)
        if self.anchor is not None:                           # add_table 总是加在末尾：挪到锚点前
            self.anchor._p.addprevious(t._tbl)

    def body(self, text: str) -> None:
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            s = line.strip()
            if not s or s.startswith("```") or re.fullmatch(r"[-*_]{3,}", s):
                i += 1
                continue
            if s.startswith("|"):
                block = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    block.append(lines[i])
                    i += 1
                self._table(block)
                continue
            if m := _HEADING.match(s):
                self.heading(m.group(2).strip(), len(m.group(1)) + 1)
            elif m := _BULLET.match(line):
                self._inline(self._para("List Bullet"), m.group(1))
            elif (m := _NUMBERED.match(line)) and not _REF.match(s):
                self._inline(self._para("List Number"), m.group(1))
            elif s.startswith(">"):
                self._inline(self._para("Quote"), s.lstrip("> ").strip())
            else:
                self._inline(self._para(), s)
            i += 1


def _format_table(t, cells: list[list[str]], width: int, total: float) -> None:
    """表头加灰底并跨页重复、行不跨页拆开；列宽按各列最长内容分配（每列至少 12%），总宽为版心宽 total（厘米）。"""
    longest = [max(min(len(r[j]) if j < len(r) else 0, 30) for r in cells) + 2 for j in range(width)]
    share = [max(n / sum(longest), 0.12) for n in longest]
    share = [x / sum(share) for x in share]
    t.autofit = False
    for i, row in enumerate(t.rows):
        trpr = row._tr.get_or_add_trPr()
        etree.SubElement(trpr, qn("w:cantSplit"))
        if i == 0:
            etree.SubElement(trpr, qn("w:tblHeader"))
        for j, cell in enumerate(row.cells):
            cell.width = Cm(total * share[j])
            if i == 0:
                cell._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="D9D9D9"/>'))


# ---- 来源脚注文字 ----

def _source_labels(db: Session, project: Project, d: Deliverable) -> dict[str, str]:
    codes: set[str] = set()
    docs: set[str] = set()
    for s in d.sections:
        v = dsvc.get_version(s)
        if v is None:
            continue
        for e in (v.sources or {}).get("evidence", []):
            codes.add(e["code"])
        for k in (v.sources or {}).get("knowledge", []):
            docs.add(k["ref"])
    labels: dict[str, str] = {}
    if codes:
        for e in db.scalars(select(Evidence).where(Evidence.project_id == project.id, Evidence.code.in_(codes))):
            extra = f"（受访者：{e.interviewee_role}）" if e.interviewee_role else ""
            date = f"，{e.occurred_on.isoformat()}" if e.occurred_on else ""
            labels[e.code] = f"{KIND_NAMES.get(e.kind, '证据')}《{e.title}》{extra}{date}"
        cards = {c for c in codes if c.startswith("S-")}
        if cards:                                     # 严格档摘要卡：撤下的不给说明（脚注写“未核对到”）
            for sc in db.scalars(select(SummaryCard).where(SummaryCard.project_id == project.id,
                                                           SummaryCard.code.in_(cards),
                                                           SummaryCard.withdrawn.is_(False))):
                labels[sc.code] = f"去标识摘要卡《{sc.item}》，客户审阅 {sc.client_reviewed_on.isoformat()}"
    if docs:
        labels.update(kb_labels(db, docs))
    return {k: _xml_safe(v) for k, v in labels.items()}


def kb_labels(db: Session, docs: set[str]) -> dict[str, str]:
    """知识编号 → 脚注文字（现行版本标题）；解析不到、已下线、没有现行版本的不给（脚注写“未核对到”）。
    kb-ref-check 也调用这里取导出标签的哈希（app/kb/refcheck.py）。"""
    found = {ref: r for ref, r in kb_refs_svc.resolve(db, docs).items() if r.usable}
    titles = dict(db.execute(select(KbVersion.id, KbVersion.title).where(
        KbVersion.id.in_({r.current_version_id for r in found.values()}))).all()) if found else {}
    from ..kb import multilang                    # 四期 B28①：译文引用旁注“非官方译本，以原文为准”并给原文条款

    out = {ref: f"知识库 {r.layer} {LAYERS.get(r.layer, '')}《{titles[r.current_version_id]}》"
           for ref, r in found.items() if r.current_version_id in titles}
    for ref, t in multilang.trace(db, set(found)).items():
        if ref in out:
            out[ref] += f"（{multilang.EXPORT_NOTE}；原文 {t.original_doc_id}《{t.original_title}》）"
    return out


def _xml_safe(text: str) -> str:
    """去掉 Word（XML 1.0）写不进的字符。入口已拒收（core/errors.reject_control_chars），这里兜底旧数据。"""
    return BAD_CHARS.sub("", text)


# ---- 页眉页脚 ----

def _mark_pages(doc: DocxDocument, draft: bool, footer_text: str) -> None:
    for i, s in enumerate(doc.sections):
        pairs = [(s.header, s.footer)]
        if s.different_first_page_header_footer:
            pairs.append((s.first_page_header, s.first_page_footer))
        if doc.settings.odd_and_even_pages_header_footer:
            pairs.append((s.even_page_header, s.even_page_footer))
        for header, footer in pairs:
            if i > 0 and header.is_linked_to_previous and footer.is_linked_to_previous:
                continue
            if draft:
                hp = header.add_paragraph()
                r = hp.add_run(DRAFT_MARK)
                r.bold = True
            fp = footer.add_paragraph()
            r = fp.add_run(footer_text)
            r.font.size = Pt(8)


# ---- 导出 ----

def export_deliverable(db: Session, user: User, project: Project, d: Deliverable) -> ExportResult:
    action = "deliverable.export"
    if not d.sections:
        raise ExportError(409, f"{d.code} 还没有大纲，不能导出")
    doc, template_kind = _load_template()

    now = utcnow().astimezone(TZ)
    export_id = secrets.token_hex(4).upper()
    draft = d.status != "signed"
    values = {"{{项目代号}}": project.code, "{{服务}}": project.service, "{{行业}}": project.industry,
              "{{交付件编号}}": d.code, "{{交付件名称}}": d.title, "{{修订}}": f"v{d.revision}",
              "{{导出日期}}": now.strftime("%Y-%m-%d"), "{{状态}}": STATUS_NAMES.get(d.status, d.status)}
    values = {k: _xml_safe(v) if isinstance(v, str) else v for k, v in values.items()}
    anchor = None
    for p in list(_paragraphs_everywhere(doc)):
        if p.text.strip() == BODY_MARK and anchor is None and p._parent is doc._body:
            anchor = p
            continue
        _replace_in_paragraph(p, values)

    notes = _Footnotes(doc)
    labels = _source_labels(db, project, d)
    w = _Writer(doc, anchor, notes, labels)
    if anchor is None and template_kind == "builtin":
        _cover(doc, f"{d.code} {_xml_safe(d.title)}",
               [f"项目：{project.code}", f"服务：{_xml_safe(project.service)}",
                f"修订：v{d.revision}　状态：{STATUS_NAMES.get(d.status, d.status)}", f"导出日期：{now:%Y-%m-%d}"])
    versions = {}
    for s in d.sections:
        w.noted = set()
        w.heading(f"{s.seq}　{_xml_safe(s.title)}", 1)
        v = dsvc.get_version(s)
        versions[str(s.seq)] = v.version if v else 0
        if v is None:
            w._inline(w._para(), "（本章还没有正文）")
            continue
        w.body(_xml_safe(v.body))
    if anchor is not None:                                     # 正文结束书签：回传时不把模板尾部内容算进末章
        for tag in ("w:bookmarkEnd", "w:bookmarkStart"):
            el = etree.Element(qn(tag))
            el.set(qn("w:id"), "9999")
            if tag == "w:bookmarkStart":
                el.set(qn("w:name"), BODY_END_BOOKMARK)
            anchor._p.addnext(el)
        anchor._p.getparent().remove(anchor._p)
    notes.save()
    doc.core_properties.identifier = EXPORT_MARK + export_id

    footer = f"导出：{user.username}　{now:%Y-%m-%d %H:%M}　导出编号 {export_id}　{project.code} {d.code} v{d.revision}"
    _mark_pages(doc, draft, footer)

    buf = io.BytesIO()
    doc.save(buf)
    data = buf.getvalue()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 deliverable=d.code, revision=d.revision, versions=versions, size=len(data),
                 file_sha=hashlib.sha256(data).hexdigest()[:16], template=template_kind, draft=draft,
                 footnotes=notes.count, export_id=export_id)
    db.commit()
    suffix = "-草稿" if draft else ""
    return ExportResult(data=data, filename=f"{project.code}-{d.code}-v{d.revision}{suffix}.docx",
                        export_id=export_id, draft=draft)

