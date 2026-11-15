"""依据原文切条（二期 B7，二期开发计划第十一节）：python -m app.cli kb-source-split <清单.csv> --dir <资料目录> --out <目录>。

公开资料下载后，条款号和原文只取官方原件、机械切分，不经模型改写；模型起草的解读、映射只能引用切出来的条款。

- 输入是下载清单（CSV，UTF-8，表头中英文都认，见 COLUMNS）和资料目录。获取方式不是“已下载”的行跳过。
- 切分方式（清单“切分方式”列，不填按文号判断）：
  - law 按条：法律法规按“第 X 条”切，条号必须从第一条起逐条递增（防止正文里换行开头的“第 X 条”被误切），
    章、节名写进摘要；
  - item 按项：附件、指引（如事件分级指南、合规审计指引）按“一、二、三”切，项号必须从一起逐项递增，
    “（一）”“1.”这类下一级编号留在本项正文里；
  - section 按章节：NIST 等允许收原文的资料，按“AC-2 ……”这类控制号或“3.1 ……”这类编号标题切；
  - catalog 条款目录：国家标准、行业标准（GB / GB/T / GB/Z / JR/T / GM/T / DL/T / YD/T 开头的不填也按目录）
    只收条款号和条款标题，**不收正文**（二期开发计划第六节：标准原文版权），原件只留在下载目录供审改对照。
- 每条一个条目文件（app/kb/files.py 格式）：L4、doc_type SRC（依据原文）、产品 GEN、阶段 通用、来源 public，
  带文件名、版本、现行状态、文号或标准号、施行日期、替代文件。key = src-<文件名哈希>-<条款号>，
  同一文件重新下载新版后再切，key 不变，kb-import 建新版本、历史版本保留。
- 每条先过机密级检测，命中的写进 _rejected/ 并注明类别（不写原文）。
- 只生成文件，不入库、不调模型：核对后用 kb-import --submit 导入，审核时抽检即可（机械切分）。
- 核验（二期 B10，verify）：原网址在官方网站白名单里、清单写了原件 sha256 且一致、写了文号或标准号、切出条款且没有
  退回、法规条号连续没有漏切。结果写进 _report.json 的 failed；kb-source-publish 据此决定自动发布还是转人工
  （app/kb/autopublish.py）。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..services import guard
from . import parse
from .files import Entry, EntryError, format_entry, parse_entry
from .items import ItemMeta, VersionContent

# 清单列：内部名 → 认得的表头
COLUMNS = {
    "file": ("文件名", "file"),
    "title": ("名称", "文件名称", "标准名称", "title"),
    "code": ("文号/标准号", "文号", "标准号", "文号或标准号", "code"),
    "publisher": ("发布机关", "publisher"),
    "published": ("发布日期", "published"),
    "effective": ("施行日期", "实施日期", "effective_date"),
    "status": ("现行状态", "状态", "status"),
    "version": ("版本", "version"),
    "url": ("原网址", "url"),
    "mode": ("切分方式", "mode"),
    "domain": ("一级目录", "domain"),
    "industries": ("适用行业", "industries"),
    "replaced_by": ("替代文件", "替代关系", "replaced_by"),
    "obtained": ("获取方式", "obtained"),
    "sha256": ("sha256", "SHA256", "原件sha256", "原件 sha256", "原件哈希"),
}
STATUS = {"现行": "current", "现行有效": "current", "有效": "current", "即将实施": "upcoming", "尚未施行": "upcoming",
          "已废止": "repealed", "废止": "repealed", "失效": "repealed", "已被替代": "superseded", "被替代": "superseded",
          "已修订": "superseded", "current": "current", "upcoming": "upcoming", "repealed": "repealed",
          "superseded": "superseded"}
MODES = {"law": "law", "按条": "law", "条": "law", "item": "item", "按项": "item", "项": "item",
         "section": "section", "按章节": "section", "章节": "section",
         "catalog": "catalog", "条款目录": "catalog", "目录": "catalog"}
STANDARD_CODE = re.compile(r"^\s*(GB/T|GB/Z|GB|JR/T|GM/T|DL/T|YD/T|GA/T|GA)\s*\d", re.I)
CATALOG_NOTE = "（条款目录：标准原文受版权保护，知识库不收全文，请查阅原件。）"
MAX_CLAUSE_CHARS = 6000

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}
_NUM = "零〇一二三四五六七八九十百千两"
_ARTICLE = re.compile(rf"^\s*第([{_NUM}\d]+)条(?:之([{_NUM}\d]+))?[\s　]*(.*)$")
_CHAPTER = re.compile(rf"^\s*第([{_NUM}\d]+)(章|节)[\s　]*(.*)$")
_APPENDIX = re.compile(r"^\s*附\s*件[一二三四五六七八九十\d]*[:：]?\s*[^。；，]{0,40}$")
_PAGE_NO = re.compile(r"\s*(?:[—－]\s*\d{1,3}\s*[—－]|-\s\d{1,3}\s-)\s*")
_ITEM = re.compile(rf"^\s*([{_NUM}]+)、[\s　]*(.*)$")
_CONTROL = re.compile(r"^\s*([A-Z]{2,3}-\d{1,2}(?:\(\d{1,2}\))?)\s+(\S.{0,80})$")
_NUMBERED = re.compile(r"^\s*((?:[A-Z]\.)?\d{1,2}(?:\.\d{1,2}){0,3})[\s　]+([^\s　].{0,60})$")


def cn_int(s: str) -> int:
    """中文或阿拉伯数字的条号 → 整数：二十一 → 21，一百零三 → 103。认不出返回 0。"""
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in _CN:
            cur = _CN[ch]
        elif ch in _CN_UNIT:
            total += (cur or 1) * _CN_UNIT[ch]
            cur = 0
        else:
            return 0
    return total + cur


@dataclass
class Doc:
    file: str
    title: str
    code: str = ""
    publisher: str = ""
    published: str = ""
    effective: date | None = None
    status: str = "current"
    version: str = ""
    url: str = ""
    mode: str = "law"
    domain: str = "CMP"
    industries: list[str] = field(default_factory=lambda: ["通用"])
    replaced_by: str = ""
    sha256: str = ""


@dataclass
class Piece:
    clause: str          # 条款号：第二十一条 / AC-2 / 8.1.5
    key_part: str        # 用在 key 里：21 / ac-2 / 8-1-5
    heading: str         # 条款标题（按章节、目录时）
    text: str            # 原文（目录方式为空）
    context: str = ""    # 所在章、节


@dataclass
class DocResult:
    file: str
    title: str
    mode: str
    entries: int = 0
    rejected: int = 0
    message: str = ""
    failed: list[str] = field(default_factory=list)     # 核验没通过的规则（VERIFY_RULES 的键）


@dataclass
class SplitReport:
    docs: list[DocResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"docs": [d.__dict__ for d in self.docs], "skipped": self.skipped}


# ---- 清单 ----

def _parse_date(s: str) -> date | None:
    s = s.strip().replace("/", "-").replace(".", "-")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s) or re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$", s)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def read_manifest(data: bytes) -> tuple[list[Doc], list[str]]:
    """返回 (要切的文件, 跳过说明)。"""
    text = data.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    docs: list[Doc] = []
    skipped: list[str] = []
    for n, raw in enumerate(rows, 2):
        row = {k: next((str(raw[h]).strip() for h in names if raw.get(h) not in (None, "")), "")
               for k, names in COLUMNS.items()}
        where = f"第 {n} 行 {row['file'] or row['title']}"
        if row["obtained"] and row["obtained"] not in ("已下载", "downloaded"):
            skipped.append(f"{where}：获取方式为“{row['obtained']}”，跳过")
            continue
        if not row["file"] or not row["title"]:
            skipped.append(f"{where}：缺文件名或名称，跳过")
            continue
        status = STATUS.get(row["status"] or "现行")
        if status is None:
            skipped.append(f"{where}：现行状态“{row['status']}”认不出，跳过")
            continue
        mode = MODES.get(row["mode"]) if row["mode"] else ("catalog" if STANDARD_CODE.match(row["code"]) else
                                                          "section" if re.match(r"^\s*NIST|^\s*SP\s*800", row["code"] or row["title"], re.I)
                                                          else "law")
        if mode is None:
            skipped.append(f"{where}：切分方式“{row['mode']}”认不出，跳过")
            continue
        version = row["version"] or (re.search(r"(\d{4})\s*$", row["code"]) or re.search(r"(\d{4})", row["published"])
                                     or re.search(r"(\d{4})", row["effective"]) or [None, "未注明"])[1]
        docs.append(Doc(file=row["file"], title=row["title"], code=row["code"], publisher=row["publisher"],
                        published=row["published"], effective=_parse_date(row["effective"]), status=status,
                        version=version, url=row["url"], mode=mode, domain=row["domain"] or "CMP",
                        industries=[x.strip() for x in re.split(r"[,，、;；]", row["industries"]) if x.strip()]
                        or ["通用"], replaced_by=row["replaced_by"], sha256=row["sha256"].lower()))
    return docs, skipped


# ---- 切分 ----

def _lines(doc: parse.ParsedDoc) -> list[str]:
    """正文行。PDF 去掉“— 5 —”“- 31 -”这类页码（公文版式页码常夹在行中间）。"""
    out: list[str] = []
    for s in doc.sections:
        if doc.kind in ("docx", "markdown") and s.heading:
            out.append(s.heading.split(" / ")[-1])
        out += [ln.strip() for ln in s.text.splitlines()]
    if doc.kind == "pdf":
        out = [_PAGE_NO.sub("", ln) for ln in out]
    return [ln.lstrip("#").strip() for ln in out if ln.strip()]


def _body(lines: list[str]) -> list[str]:
    """法规正文到“附件：××表”这类附件行为止：附件（审核表、指南）不并进最后一条，另按 item 切或不收。"""
    seen = False
    for i, ln in enumerate(lines):
        seen = seen or bool(_ARTICLE.match(ln))
        if seen and _APPENDIX.match(ln):
            return lines[:i]
    return lines


def _join(lines: list[str]) -> str:
    """PDF 按行断开的中文正文拼回去：中文之间不加空格，西文之间加空格。"""
    text = ""
    for ln in lines:
        if text and re.search(r"[A-Za-z0-9,.;:)]$", text) and re.match(r"^[A-Za-z0-9(]", ln):
            text += " "
        text += ln
    return text.strip()


def split_law(lines: list[str]) -> list[Piece]:
    pieces: list[Piece] = []
    context: dict[str, str] = {}
    buf: list[str] = []
    cur: tuple[int, int, str] | None = None     # (条号, 之几, 条款号原文)

    def flush() -> None:
        if cur is not None:
            n, sub, label = cur
            pieces.append(Piece(clause=label, key_part=f"{n}" + (f"-{sub}" if sub else ""), heading="",
                                text=_join(buf), context=" ".join(v for v in context.values() if v)))

    for ln in lines:
        m = _ARTICLE.match(ln)
        if m:
            n, sub = cn_int(m.group(1)), cn_int(m.group(2)) if m.group(2) else 0
            last, last_sub = (cur[0], cur[1]) if cur else (0, 0)
            if n and ((n == last + 1 and not sub) or (n == last and sub == last_sub + 1)):
                flush()
                cur = (n, sub, f"第{m.group(1)}条" + (f"之{m.group(2)}" if m.group(2) else ""))
                buf = [m.group(3)] if m.group(3) else []
                continue
        c = _CHAPTER.match(ln)
        if c and len(ln) <= 40 and not ln.endswith(("。", "；")):
            if c.group(2) == "章":
                context = {"章": f"第{c.group(1)}章 {c.group(3)}".strip(), "节": ""}
            else:
                context["节"] = f"第{c.group(1)}节 {c.group(3)}".strip()
            continue
        if cur is not None:
            buf.append(ln)
    flush()
    return pieces


def split_items(lines: list[str]) -> list[Piece]:
    """附件、指引按“一、二、三”切（item）：项号从一起逐项递增，正文里的“（一）”“1.”不切。
    项的第一行是短标题（不以标点结尾，“一、特别重大事件”或“一、”下一行）的，作为本项标题。"""
    pieces: list[Piece] = []
    buf: list[str] = []
    cur = 0

    def flush() -> None:
        if not cur:
            return
        body, heading = list(buf), ""
        if len(body) > 1 and len(body[0]) <= 30 and not body[0].endswith(_END):
            heading = body.pop(0)                   # “一、特别重大事件”或“一、”下一行的短标题
        pieces.append(Piece(clause=_cn_label(cur), key_part=str(cur), heading=heading, text=_join(body)))

    for ln in lines:
        m = _ITEM.match(ln)
        if m and cn_int(m.group(1)) == cur + 1:
            flush()
            cur = cn_int(m.group(1))
            buf = [m.group(2)] if m.group(2) else []
            continue
        if cur:
            buf.append(ln)
    flush()
    return pieces


_END = ("。", "；", "，", "：", ":", ";", ",", ".")


def _cn_label(n: int) -> str:
    """1 → 一、，13 → 十三、（条目里显示原文项号）。"""
    digits = "零一二三四五六七八九"
    if n < 10:
        s = digits[n]
    elif n < 20:
        s = "十" + (digits[n % 10] if n % 10 else "")
    else:
        s = digits[n // 10] + "十" + (digits[n % 10] if n % 10 else "")
    return s + "、"


def _heading(ln: str) -> tuple[str, str] | None:
    m = _CONTROL.match(ln)
    if m:
        return m.group(1), m.group(2).strip()
    m = _NUMBERED.match(ln)
    if m and not ln.endswith(("。", "；", "，", ".", ";", ",")) and not re.search(r"\.{4,}|…{2,}|\s\d+$", ln):
        return m.group(1), m.group(2).strip()
    return None


def split_sections(lines: list[str], with_text: bool) -> list[Piece]:
    """按控制号或编号标题切。同一编号只取第一次出现（目录页、页眉重复的跳过）。"""
    pieces: list[Piece] = []
    seen: set[str] = set()
    buf: list[str] = []
    cur: Piece | None = None
    for ln in lines:
        h = _heading(ln)
        if h and h[0] not in seen:
            if cur is not None:
                cur.text = _join(buf) if with_text else ""
                pieces.append(cur)
            seen.add(h[0])
            cur = Piece(clause=h[0], key_part=re.sub(r"[^a-z0-9]+", "-", h[0].lower()).strip("-"), heading=h[1],
                        text="")
            buf = []
        elif cur is not None and with_text:
            buf.append(ln)
    if cur is not None:
        cur.text = _join(buf) if with_text else ""
        pieces.append(cur)
    return pieces


def src_key(ref_document: str, part: str) -> str:
    """依据原文条目的 key：src-<文件名哈希>-<条号>。B8 引用校验按同一规则由文件名和条号找条款。"""
    h = hashlib.sha256(ref_document[:200].encode("utf-8")).hexdigest()[:8]
    return f"src-{h}-{part}"[:64].rstrip("-")


def _key(d: Doc, part: str) -> str:
    return src_key(d.title, part)


def to_entry(d: Doc, p: Piece) -> Entry:
    head = f"{p.clause} {p.heading}".strip()
    # 标准条款号前带标准号：条款号 8.1.4.1 形似 IP，机密级检测据“GB/T ××× 8.1.4.1”认作条款号
    shown = f"{d.code} {head}" if d.mode not in ("law", "item") and d.code else head
    body = f"## {shown}\n\n{CATALOG_NOTE if d.mode == 'catalog' else p.text[:MAX_CLAUSE_CHARS]}"
    detail = " ".join(x for x in ("公开资料：", d.publisher, d.code, d.url) if x).replace("： ", "：")
    summary = " ".join(x for x in (d.title, d.code, p.context, head) if x)
    e = Entry(key=_key(d, p.key_part),
              meta=ItemMeta(layer="L4", domain=d.domain, product="GEN", doc_type="SRC", stage="通用",
                            capabilities=["管理"], industries=d.industries),
              content=VersionContent(title=f"{d.title} {shown}"[:200], body=body, source_type="public",
                                     source_detail=detail[:300], summary=summary[:1000], ref_document=d.title[:200],
                                     ref_version=(d.version or "未注明")[:64], ref_status=d.status, ref_code=d.code[:100],
                                     ref_replaced_by=d.replaced_by[:200], effective_date=d.effective))
    return parse_entry(format_entry(e))        # 走一遍条目文件格式，保证 kb-import 能原样读回


def split_doc(d: Doc, data: bytes) -> tuple[list[Entry], list[tuple[str, str]], str]:
    """返回 (条目, [(key, 退回原因)], 说明)。"""
    lines = _lines(parse.parse(d.file, data))
    if d.mode == "law":
        pieces = split_law(_body(lines))
    elif d.mode == "item":
        pieces = split_items(lines)
    else:
        pieces = split_sections(lines, with_text=d.mode == "section")
    entries: list[Entry] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for p in pieces:
        if d.mode != "catalog" and not p.text:
            continue
        try:
            e = to_entry(d, p)
        except EntryError as ex:
            rejected.append((_key(d, p.key_part), str(ex)))
            continue
        if e.key in seen:
            continue
        seen.add(e.key)
        g = guard.check("\n".join((e.content.title, e.content.summary, e.content.body)))
        if g.blocked:
            rejected.append((e.key, f"命中机密级检测（{'、'.join(sorted({f.label for f in g.findings}))}）"))
            continue
        entries.append(e)
    note = "" if pieces else "没有切出条款：可能是扫描件，或切分方式不对（法规用 law，标准用 catalog，NIST 用 section）"
    return entries, rejected, note


# ---- 核验（二期 B10） ----

VERIFY_RULES = {
    "official_source": "原网址在官方网站白名单里",
    "sha256": "清单写了原件 sha256，与文件一致",
    "code": "清单写了文号或标准号",
    "complete": "切出了条款，没有退回的条款",
    "continuous": "法规条号从第一条起连续、附件项号从一起连续，没有漏切",
}
OFFICIAL_HOSTS = ("gov.cn", "nist.gov")


def official_hosts() -> tuple[str, ...]:
    from ..core.config import get_settings

    extra = [h.strip().lower().lstrip(".") for h in get_settings().kb_source_hosts.split(",") if h.strip()]
    return OFFICIAL_HOSTS + tuple(extra)


def is_official(url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        u = urlsplit(url.strip())
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == h or host.endswith("." + h) for h in official_hosts())


def verify(d: Doc, data: bytes, entries: list[Entry], rejected: list[tuple[str, str]]) -> list[str]:
    """返回核验没通过的规则。全部通过（空列表）才能由平台自动发布。"""
    failed: list[str] = []
    if not is_official(d.url):
        failed.append("official_source")
    if not d.sha256 or hashlib.sha256(data).hexdigest() != d.sha256:
        failed.append("sha256")
    if not d.code.strip():
        failed.append("code")
    if not entries or rejected:
        failed.append("complete")
    if d.mode == "law":
        got = {e.key for e in entries}
        for ln in _body(_lines(parse.parse(d.file, data))):
            m = _ARTICLE.match(ln)
            if m and cn_int(m.group(1)):
                part = f"{cn_int(m.group(1))}" + (f"-{cn_int(m.group(2))}" if m.group(2) else "")
                if _key(d, part) not in got:        # 行首像条号却没切出来：前面有漏切、条号跳号
                    failed.append("continuous")
                    break
    elif d.mode == "item":
        got = {e.key for e in entries}
        for ln in _lines(parse.parse(d.file, data)):
            m = _ITEM.match(ln)
            if m and cn_int(m.group(1)) and _key(d, str(cn_int(m.group(1)))) not in got:
                failed.append("continuous")
                break
    return failed


def split_all(manifest: Path, src_dir: Path, out_dir: Path) -> SplitReport:
    docs, skipped = read_manifest(manifest.read_bytes())
    report = SplitReport(skipped=skipped)
    out_dir.mkdir(parents=True, exist_ok=True)
    rej_dir = out_dir / "_rejected"
    for d in docs:
        r = DocResult(file=d.file, title=d.title, mode=d.mode)
        report.docs.append(r)
        f = src_dir / d.file
        if not f.is_file():
            r.message = "文件不存在"
            continue
        data = f.read_bytes()
        try:
            entries, rejected, r.message = split_doc(d, data)
        except parse.ParseError as e:
            r.message = str(e)
            r.failed = ["complete"]
            continue
        r.failed = verify(d, data, entries, rejected)
        for e in entries:
            (out_dir / f"{e.key}.md").write_text(format_entry(e), encoding="utf-8")
        if rejected:
            rej_dir.mkdir(exist_ok=True)
            for key, why in rejected:
                (rej_dir / f"{key}.txt").write_text(f"{d.title}\n退回原因：{why}\n", encoding="utf-8")
        r.entries, r.rejected = len(entries), len(rejected)
    (out_dir / "_report.json").write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    return report
