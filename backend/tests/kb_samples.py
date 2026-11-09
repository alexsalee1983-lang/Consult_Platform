"""测试用的投料样本（全部在内存里生成，只含虚拟内容）。"""
import io


def docx_bytes(paragraphs: list[tuple[str, str]], table: list[list[str]] | None = None) -> bytes:
    """paragraphs：[(样式, 文字)]，样式如 "Heading 1"、"Heading 2"、"Normal"。"""
    import docx

    d = docx.Document()
    for style, text in paragraphs:
        d.add_paragraph(text, style=style)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, val in enumerate(row):
                t.cell(r, c).text = val
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def text_pdf_bytes(pages: list[str]) -> bytes:
    """手工拼一个最简单的文字版 PDF（标准 Helvetica 字体，只放 ASCII 文字）。"""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        content_id = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def blank_pdf_bytes(pages: int = 3) -> bytes:
    """没有文字的 PDF，相当于扫描件。"""
    import pypdf

    w = pypdf.PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


SOC_MD = """# 值班管理规程

适用于有 7×24 值守的 SOC。

## 交接班

交接班时必须核对未闭环的一级事件和升级记录。

## 夜间

无夜班客户由托管方初筛，一级事件电话升级轮值负责人，15 分钟内响应。
"""
