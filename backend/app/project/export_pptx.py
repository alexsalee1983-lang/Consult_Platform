"""交付件 PowerPoint 导出（二期 A9，汇报材料如 D8）。

与 Word 导出（export.py）同一套规则：

1. 模板从配置位置读取（EXPORT_TEMPLATE_PPTX）：不配置时用内置模板（python-pptx 自带的空白演示文稿，改为 16:9）；
   配置了但文件不存在、打不开时拒绝导出（503），不悄悄退回内置模板。模板里已有的幻灯片保留（如公司封面），
   其中的占位符 {{项目代号}} {{服务}} {{行业}} {{交付件编号}} {{交付件名称}} {{修订}} {{导出日期}} {{状态}} 会替换；
   模板没有幻灯片时平台生成封面。正文页用版式“仅标题”（找不到用第 6 个版式，再不行用最后一个）。
2. 未签署的每页右上角标“草稿”；每页页脚写导出人、导出时间和导出编号（审计能按编号查到）。
3. 每章一页起，章内 ## 小标题另起一页，内容超过 MAX_LINES 行自动分页（标题加“（续）”）；表格单独成页。
   “**讲稿**：……”这样的段落放进演讲者备注，不上屏。
4. 来源标注（[I-01] [D-12] [L4-…]）在正文里保留原样；PPT 没有脚注，末页“来源说明”逐个列出（与 Word 脚注同样的文字），
   [推断] 不列。
5. 严格档导出占位符版本（写入时已查占位符）；没有大纲的交付件不能导出（409）。

审计与 Word 导出同一动作 deliverable.export，另记 format=pptx 和页数；不记正文、章节标题和模板路径。
"""
from __future__ import annotations

import hashlib
import io
import re
import secrets
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.presentation import Presentation as Prs
from pptx.slide import Slide
from pptx.util import Emu, Pt
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..core.db import utcnow
from ..models import Deliverable, Project, User
from . import deliverables as dsvc
from .export import (_BOLD, _BULLET, _HEADING, _NUMBERED, _REF, _TABLE_SEP, STATUS_NAMES, TZ, ExportError,
                     ExportResult, _source_labels, _xml_safe)

MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DRAFT_MARK = "草稿 · 未经签署"
FONT = "微软雅黑"
MAX_LINES = 12                                    # 一页最多几行正文（表格按行数算）
_NOTES = re.compile(r"^\*{0,2}讲稿\*{0,2}\s*[:：]\s*(.*)$")


def _load_template() -> tuple[Prs, str]:
    path = (get_settings().export_template_pptx or "").strip()
    if not path:
        prs = Presentation()
        prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)      # 16:9
        return prs, "builtin"
    p = Path(path)
    if not p.is_file():
        raise ExportError(503, "PPT 导出模板文件不存在，请系统管理员检查 EXPORT_TEMPLATE_PPTX 配置")
    try:
        prs = Presentation(str(p))
    except Exception as e:                                   # 不是有效的 .pptx
        raise ExportError(503, "PPT 导出模板无法打开（需要 .pptx 文件），请系统管理员检查 EXPORT_TEMPLATE_PPTX 配置") from e
    return prs, "configured"


def _font(run, size: int | None = None, bold: bool | None = None) -> None:
    f = run.font
    f.name = FONT
    rpr = run._r.get_or_add_rPr()
    ea = rpr.find(qn("a:ea"))
    if ea is None:
        ea = etree.SubElement(rpr, qn("a:ea"))
    ea.set("typeface", FONT)                                 # 中文字体，不落到西文字体
    if size:
        f.size = Pt(size)
    if bold:
        f.bold = True


def _content_layout(prs: Prs):
    for lay in prs.slide_layouts:
        if lay.name in ("Title Only", "仅标题"):
            return lay
    layouts = list(prs.slide_layouts)
    return layouts[5] if len(layouts) > 5 else layouts[-1]


class _Deck:
    def __init__(self, prs: Prs):
        self.prs = prs
        self.layout = _content_layout(prs)
        self.w, self.h = prs.slide_width, prs.slide_height
        self.refs: list[str] = []                             # 出现过的来源标注（去重、按出现顺序）

    # ---- 页面 ----
    def slide(self, title: str) -> Slide:
        s = self.prs.slides.add_slide(self.layout)
        t = s.shapes.title
        if t is None:
            t = s.shapes.add_textbox(Emu(int(self.w * 0.05)), Emu(int(self.h * 0.05)), Emu(int(self.w * 0.8)),
                                     Emu(int(self.h * 0.12)))
        t.text_frame.text = ""
        self._runs(t.text_frame.paragraphs[0], title, 28, bold=True)
        return s

    def _box(self, s: Slide):
        return s.shapes.add_textbox(Emu(int(self.w * 0.06)), Emu(int(self.h * 0.22)), Emu(int(self.w * 0.88)),
                                    Emu(int(self.h * 0.66)))

    def _runs(self, para, text: str, size: int, bold: bool = False) -> None:
        pos = 0
        for m in _BOLD.finditer(text):
            if m.start() > pos:
                _font(para.add_run(), size, bold)
                para.runs[-1].text = text[pos:m.start()]
            r = para.add_run()
            r.text = m.group(1)
            _font(r, size, True)
            pos = m.end()
        if pos < len(text) or not para.runs:
            r = para.add_run()
            r.text = text[pos:]
            _font(r, size, bold)
        for m in _REF.finditer(text):
            ref = m.group(3) or (m.group(1) + (m.group(2) or ""))
            if ref not in self.refs:
                self.refs.append(ref)

    def text_slide(self, title: str, lines: list[tuple[str, int, bool]], notes: list[str]) -> None:
        chunks = [lines[i:i + MAX_LINES] for i in range(0, len(lines), MAX_LINES)] or [[]]
        for n, chunk in enumerate(chunks):
            s = self.slide(title if n == 0 else f"{title}（续）")
            if chunk:
                tf = self._box(s).text_frame
                tf.word_wrap = True
                for i, (text, level, bullet) in enumerate(chunk):
                    para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    para.level = level
                    self._runs(para, ("• " if bullet else "") + text, 18)
            if notes and n == 0:
                s.notes_slide.notes_text_frame.text = "\n".join(notes)

    def table_slide(self, title: str, rows: list[list[str]]) -> None:
        head, body = rows[0], rows[1:]
        chunks = [body[i:i + MAX_LINES - 1] for i in range(0, len(body), MAX_LINES - 1)] or [[]]
        width = max(len(r) for r in rows)
        for n, chunk in enumerate(chunks):
            s = self.slide(title if n == 0 else f"{title}（续）")
            data = [head, *chunk]
            shape = s.shapes.add_table(len(data), width, Emu(int(self.w * 0.06)), Emu(int(self.h * 0.22)),
                                       Emu(int(self.w * 0.88)), Emu(int(self.h * 0.06 * len(data))))
            for i, r in enumerate(data):
                for j in range(width):
                    tf = shape.table.cell(i, j).text_frame
                    tf.text = ""
                    self._runs(tf.paragraphs[0], r[j] if j < len(r) else "", 14, bold=(i == 0))

    # ---- 正文 ----
    def section(self, title: str, body: str) -> None:
        cur_title, lines, notes = title, [], []

        def flush(force: bool = False) -> None:
            nonlocal lines, notes
            if lines or notes or force:
                self.text_slide(cur_title, lines, notes)
            lines, notes = [], []

        started = False
        src = body.splitlines()
        i = 0
        while i < len(src):
            line = src[i].rstrip()
            s = line.strip()
            if not s or s.startswith("```") or re.fullmatch(r"[-*_]{3,}", s):
                i += 1
                continue
            if s.startswith("|"):
                block = []
                while i < len(src) and src[i].strip().startswith("|"):
                    block.append(src[i])
                    i += 1
                rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in block
                        if not _TABLE_SEP.match(r.strip())]
                if rows:
                    flush()
                    started = True
                    self.table_slide(cur_title, rows)
                continue
            if m := _HEADING.match(s):
                if lines or notes or not started:
                    flush(force=not started)
                started = True
                cur_title = f"{title} · {m.group(2).strip()}"
            elif m := _NOTES.match(s):
                notes.append(m.group(1).replace("**", ""))
            elif m := _BULLET.match(line):
                lines.append((m.group(1), 1 if line[:1].isspace() else 0, True))
            elif (m := _NUMBERED.match(line)) and not _REF.match(s):
                lines.append((s, 0, False))
            else:
                lines.append((s.lstrip("> ").strip(), 0, False))
            i += 1
        if lines or notes or not started:
            flush(force=True)

    # ---- 每页标记 ----
    def mark(self, draft: bool, footer: str) -> None:
        for s in self.prs.slides:
            if draft:
                tb = s.shapes.add_textbox(Emu(int(self.w * 0.70)), Emu(int(self.h * 0.01)), Emu(int(self.w * 0.28)),
                                          Emu(int(self.h * 0.05)))
                self._runs(tb.text_frame.paragraphs[0], DRAFT_MARK, 12, bold=True)
            fb = s.shapes.add_textbox(Emu(int(self.w * 0.04)), Emu(int(self.h * 0.93)), Emu(int(self.w * 0.92)),
                                      Emu(int(self.h * 0.05)))
            self._runs(fb.text_frame.paragraphs[0], footer, 9)


def _replace_placeholders(prs: Prs, values: dict[str, str]) -> None:
    for s in prs.slides:
        for sh in s.shapes:
            if not sh.has_text_frame:
                continue
            for p in sh.text_frame.paragraphs:
                text = "".join(r.text for r in p.runs)
                if "{{" not in text or not any(k in text for k in values):
                    continue
                for k, v in values.items():
                    text = text.replace(k, v)
                p.runs[0].text = text                         # 占位符可能被拆在几个 run 里：合并到第一个
                for r in p.runs[1:]:
                    r.text = ""


def export_deliverable(db: Session, user: User, project: Project, d: Deliverable) -> ExportResult:
    if not d.sections:
        raise ExportError(409, f"{d.code} 还没有大纲，不能导出")
    prs, template_kind = _load_template()
    now = utcnow().astimezone(TZ)
    export_id = secrets.token_hex(4).upper()
    draft = d.status != "signed"
    status = STATUS_NAMES.get(d.status, d.status)
    values = {"{{项目代号}}": project.code, "{{服务}}": project.service, "{{行业}}": project.industry,
              "{{交付件编号}}": d.code, "{{交付件名称}}": d.title, "{{修订}}": f"v{d.revision}",
              "{{导出日期}}": now.strftime("%Y-%m-%d"), "{{状态}}": status}
    values = {k: _xml_safe(v) for k, v in values.items()}
    _replace_placeholders(prs, values)

    deck = _Deck(prs)
    if not len(prs.slides):                                   # 模板没有封面：平台生成
        s = deck.slide(f"{d.code} {_xml_safe(d.title)}")
        tf = deck._box(s).text_frame
        deck._runs(tf.paragraphs[0], f"{project.code} · {_xml_safe(project.service)}", 20)
        deck._runs(tf.add_paragraph(), f"修订 v{d.revision} · {status} · {now:%Y-%m-%d}", 16)
    versions = {}
    for sec in d.sections:
        title = f"{sec.seq}　{_xml_safe(sec.title)}"
        v = dsvc.get_version(sec)
        versions[str(sec.seq)] = v.version if v else 0
        deck.section(title, _xml_safe(v.body) if v else "（本章还没有正文）")

    labels = _source_labels(db, project, d)
    refs = [r for r in deck.refs if r != "推断"]
    if refs:
        rows = []
        for ref in refs:
            key = ref if ref.startswith("L") else re.sub(r"^((?:I|D|O|S)-\d{2,4}).*$", r"\1", ref)
            rows.append((f"{ref}：{labels.get(key, '未核对到对应的证据或知识库条目')}", 0, False))
        deck.text_slide("来源说明", rows, [])

    footer = f"导出：{user.username}　{now:%Y-%m-%d %H:%M}　导出编号 {export_id}　{project.code} {d.code} v{d.revision}"
    deck.mark(draft, footer)

    buf = io.BytesIO()
    prs.save(buf)
    data = buf.getvalue()
    audit.record(db, actor=user.username, action="deliverable.export", result="allowed", project_code=project.code,
                 deliverable=d.code, revision=d.revision, versions=versions, size=len(data),
                 file_sha=hashlib.sha256(data).hexdigest()[:16], template=template_kind, draft=draft,
                 format="pptx", slides=len(prs.slides), sources=len(refs), export_id=export_id)
    db.commit()
    suffix = "-草稿" if draft else ""
    return ExportResult(data=data, filename=f"{project.code}-{d.code}-v{d.revision}{suffix}.pptx",
                        export_id=export_id, draft=draft)
