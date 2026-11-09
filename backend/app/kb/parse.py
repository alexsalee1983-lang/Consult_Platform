"""投料解析：把 Word / PDF / Markdown / 文本解析成“章节 + 正文”，再切成检索用的片段。

- 只在本地解析，不联网；不执行文件中的宏、不加载外部引用。
- 文件类型以内容判断（文件头），扩展名只作参考：.docx 必须是 zip、PDF 必须以 %PDF 开头、文本必须能按 UTF-8 解码。
- 扫描件（PDF 每页几乎没有文字）判为不支持：OCR 一期不做（决策记录 2026-11-08），提示提供文字版。
- 解析结果只给调用方，不写日志；出错时只抛 ParseError 的安全说明，不带文件内容。
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import asdict, dataclass, field

MAX_BYTES = 20 * 1024 * 1024          # 单个文件上限（nginx 上限 50MB，这里更严）
MAX_CHARS = 2_000_000                  # 解析后正文上限
MAX_PDF_PAGES = 500
SCANNED_CHARS_PER_PAGE = 30            # 平均每页少于这么多字，视为扫描件
CHUNK_CHARS = 800                      # 检索片段目标长度
CHUNK_OVERLAP = 100

EXTENSIONS = {".docx": "docx", ".pdf": "pdf", ".md": "markdown", ".markdown": "markdown", ".txt": "text"}


class ParseError(ValueError):
    """解析失败的原因（可展示给用户，不含文件内容）。"""


@dataclass
class Section:
    heading: str            # 标题路径，例如“值班管理规程 / 交接班”
    text: str


@dataclass
class ParsedDoc:
    kind: str               # docx / pdf / markdown / text
    sections: list[Section] = field(default_factory=list)
    pages: int = 0

    @property
    def chars(self) -> int:
        return sum(len(s.text) for s in self.sections)

    def full_text(self) -> str:
        return "\n\n".join(f"{s.heading}\n{s.text}" if s.heading else s.text for s in self.sections)

    def to_json(self) -> dict:
        return {"kind": self.kind, "pages": self.pages, "chars": self.chars,
                "sections": [asdict(s) for s in self.sections]}


def detect_kind(filename: str, data: bytes) -> str:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    kind = EXTENSIONS.get(ext)
    if kind is None:
        raise ParseError("只支持 Word（.docx）、PDF、Markdown（.md）和纯文本（.txt）；旧版 .doc 请另存为 .docx")
    if kind == "docx" and not data.startswith(b"PK\x03\x04"):
        raise ParseError("文件内容不是 .docx 格式")
    if kind == "pdf" and not data.lstrip()[:5].startswith(b"%PDF-"):
        raise ParseError("文件内容不是 PDF 格式")
    return kind


def parse(filename: str, data: bytes) -> ParsedDoc:
    if not data:
        raise ParseError("文件是空的")
    if len(data) > MAX_BYTES:
        raise ParseError(f"文件超过 {MAX_BYTES // 1024 // 1024}MB")
    kind = detect_kind(filename, data)
    doc = {"docx": _docx, "pdf": _pdf, "markdown": _markdown, "text": _text}[kind](data)
    doc.sections = [s for s in doc.sections if s.text.strip()]
    if not doc.sections:
        raise ParseError("没有解析出文字内容")
    if doc.chars > MAX_CHARS:
        raise ParseError(f"正文超过 {MAX_CHARS // 10000} 万字，请拆分后上传")
    return doc


# ---- 各格式 ----

def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParseError("文本编码无法识别，请另存为 UTF-8")


def _text(data: bytes) -> ParsedDoc:
    text = _decode_text(data).replace("\r\n", "\n")
    return ParsedDoc("text", [Section("", p.strip()) for p in re.split(r"\n\s*\n", text) if p.strip()])


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _markdown(data: bytes) -> ParsedDoc:
    return ParsedDoc("markdown", _by_headings(
        ((len(m.group(1)), m.group(2)) if (m := _MD_HEADING.match(line)) else (0, line))
        for line in _decode_text(data).replace("\r\n", "\n").split("\n")))


def _by_headings(lines) -> list[Section]:
    """lines：(标题级别, 文字)，级别 0 表示正文。按标题切成章节，标题路径用“ / ”连接。"""
    path: list[tuple[int, str]] = []
    sections: list[Section] = []
    buf: list[str] = []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            sections.append(Section(" / ".join(t for _, t in path), text))
        buf.clear()

    for level, text in lines:
        if level:
            flush()
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, text.strip()[:100]))
        else:
            buf.append(text)
    flush()
    return sections


def _docx(data: bytes) -> ParsedDoc:
    import docx

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "word/document.xml" not in z.namelist():
                raise ParseError("文件内容不是 .docx 格式")
            if sum(i.file_size for i in z.infolist()) > 20 * MAX_BYTES:   # 压缩炸弹
                raise ParseError("文件解压后过大")
        d = docx.Document(io.BytesIO(data))
    except ParseError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ParseError("Word 文件无法打开（可能已损坏或加密）") from e

    def lines():
        for block in _docx_blocks(d):
            if isinstance(block, str):
                yield 0, block
                continue
            style = (block.style.name or "") if block.style is not None else ""
            m = re.match(r"(?:Heading|标题)\s*(\d)", style)
            if m:
                yield int(m.group(1)), block.text
            elif style == "Title":
                yield 1, block.text
            else:
                yield 0, block.text

    return ParsedDoc("docx", _by_headings(lines()))


def _docx_blocks(d):
    """按文档顺序给出段落和表格（表格转成“单元格 | 单元格”的行文字）。"""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, d)
        elif tag == "tbl":
            rows = []
            for row in Table(child, d).rows:
                cells = []
                for c in row.cells:
                    t = c.text.strip().replace("\n", " ")
                    if not cells or cells[-1] != t:     # 合并单元格会重复出现
                        cells.append(t)
                rows.append(" | ".join(cells))
            yield "\n".join(rows)


def _pdf(data: bytes) -> ParsedDoc:
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ParseError("PDF 已加密，请提供未加密的文件")
        n = len(reader.pages)
        if n > MAX_PDF_PAGES:
            raise ParseError(f"PDF 超过 {MAX_PDF_PAGES} 页，请拆分后上传")
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except ParseError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ParseError("PDF 无法打开（可能已损坏）") from e
    total = sum(len(re.sub(r"\s", "", p)) for p in pages)
    if n and total / n < SCANNED_CHARS_PER_PAGE:
        raise ParseError("这是扫描件（几乎没有可提取的文字），一期不做文字识别，请提供文字版 PDF 或 Word")
    return ParsedDoc("pdf", [Section(f"第 {i} 页", p) for i, p in enumerate(pages, 1) if p], pages=n)


# ---- 切分 ----

def chunk(sections: list[Section], size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[Section]:
    """按章节切片：短章节整段一片；长章节按段落累积到 size 左右，片与片之间重叠 overlap 字，保留标题路径。"""
    out: list[Section] = []
    for s in sections:
        text = s.text.strip()
        if len(text) <= size:
            out.append(Section(s.heading, text))
            continue
        paras = [p for p in re.split(r"\n+", text) if p.strip()]
        buf = ""
        for p in paras:
            while len(p) > size:                     # 单段超长：硬切
                head, p = p[:size], p[size - overlap:]
                if buf:
                    out.append(Section(s.heading, buf))
                    buf = ""
                out.append(Section(s.heading, head))
            if buf and len(buf) + len(p) + 1 > size:
                out.append(Section(s.heading, buf))
                buf = buf[-overlap:] + "\n" + p if overlap else p
            else:
                buf = f"{buf}\n{p}" if buf else p
        if buf.strip():
            out.append(Section(s.heading, buf))
    return out
