"""起草引用校验（二期 B8，二期开发计划第十一节）：解读、映射、案例、异议应答里引用的“文件 + 条款号”必须在库里
找得到依据原文（SRC，B7 切条入库的法规条文或标准条款目录），找不到的列为“待核”，不能提交审核。

- 认哪些引用（find）：
  - 《文件名》第 X 条 / 第 X 条之一（文件名后可带“（2016）”这类年份括注）；
  - 标准号 + 条款号：GB/T 22239-2019 8.1.4.1、JR/T 0071 第 6.2 条（标准号可不带年份）；
  - 条目自己写了 ref_document（L4 解读、映射）时，没写文件名的“第 X 条”、标题里没写标准号的条款号，算引用它自己的依据。
- 怎么核（check）：在已发布、未下线的 SRC 条目里按文件名（去书名号、空格和“中华人民共和国”前缀）或标准号（不带年份时
  优先取现行的那一版）找到文件，再按 B7 的 key 规则（src-<文件名哈希>-<条号>）找条款。找到的带回原文摘录和现行状态，
  供审核页并排显示；条款目录只有条款标题，摘录为空、注明“请查阅原件”。
- 只读知识区，不调模型，不写审计（提交被拦时由 review.submit 照常处理）。
- KB_CITATION_GATE=false 可关掉提交拦截（只显示、不拦），默认开启。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.kb import KbItem, KbVersion
from . import sources

CITING_TYPES = ("REF", "MAP", "CAS", "OBJ", "ENF")
_NUM = "零〇一二三四五六七八九十百千两\\d"
_ART = rf"第\s*([{_NUM}]+)\s*条(?:之([{_NUM}]+))?"
_STD_CLAUSE = r"(\d{1,2}(?:\.\d{1,2}){1,4}|[A-Z]\.\d{1,2}(?:\.\d{1,2}){0,3})"
_BOOK = re.compile(rf"《([^《》\n]{{2,80}})》\s*(?:[（(]\s*\d{{4}}[^）)\n]{{0,12}}[）)])?\s*(?:的\s*)?{_ART}")
_STD = re.compile(rf"(?<![A-Za-z/])((?:GB/T|GB/Z|GB|JR/T|GM/T|DL/T|YD/T|GA/T|GA)\s*\d+(?:\.\d+)?(?:\s*[-—]\s*\d{{4}})?)"
                  rf"\s*(?:第\s*)?{_STD_CLAUSE}(?!\.?\d)")
_BARE_ART = re.compile(_ART)
_HEAD = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_HEAD_STD = re.compile(rf"(?<![\d./-]){_STD_CLAUSE}(?![\d.])")
EXCERPT = 300


@dataclass
class Citation:
    document: str           # 引用写的文件名或标准号
    clause: str             # 第二十一条 / 8.1.4.1
    found: bool = False
    src_doc_id: str = ""    # 找到的依据原文条目
    ref_status: str = ""
    ref_version: str = ""
    catalog: bool = False   # 条款目录（标准，无正文）
    excerpt: str = ""       # 原文摘录（前 300 字）
    doc_known: bool = False  # 文件在库里有依据原文（找不到条款时即条款号不存在，对比评测记“编造条号”）

    def label(self) -> str:
        return f"{self.document} {self.clause}"


def _norm_doc(s: str) -> str:
    s = re.sub(r"[\s《》“”\"]", "", s)
    return s.removeprefix("中华人民共和国")


def _norm_code(s: str) -> str:
    return re.sub(r"\s+", "", s).upper().replace("—", "-")


def _is_code(s: str) -> bool:
    return bool(sources.STANDARD_CODE.match(s))


def _law_part(n: str, sub: str | None) -> str:
    return f"{sources.cn_int(n)}" + (f"-{sources.cn_int(sub)}" if sub else "")


def find(body: str, own_document: str = "", own_code: str = "") -> list[tuple[str, str, str]]:
    """返回 [(文件名或标准号, 条款号原文, key 用的条号)]，去重、保持出现顺序。"""
    found: list[tuple[int, str, str, str]] = []
    spans: list[tuple[int, int]] = []
    for m in _BOOK.finditer(body):
        spans.append(m.span())
        found.append((m.start(), m.group(1).strip(), re.sub(r"\s+", "", m.group(0)[m.group(0).rfind("第"):]),
                      _law_part(m.group(2), m.group(3))))
    for m in _STD.finditer(body):
        spans.append(m.span())
        found.append((m.start(), re.sub(r"\s+", " ", m.group(1)).strip(), m.group(2),
                      re.sub(r"[^a-z0-9]+", "-", m.group(2).lower())))
    own = own_code if own_code and _is_code(own_code) else own_document
    if own:
        for m in _BARE_ART.finditer(body):
            if not any(a <= m.start() < b for a, b in spans):
                found.append((m.start(), own, re.sub(r"\s+", "", m.group(0)), _law_part(m.group(1), m.group(2))))
        if _is_code(own):
            for h in _HEAD.finditer(body):
                for m in _HEAD_STD.finditer(h.group(1)):
                    pos = h.start(1) + m.start()
                    if not any(a <= pos < b for a, b in spans):
                        found.append((pos, own, m.group(1), re.sub(r"[^a-z0-9]+", "-", m.group(1).lower())))
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _, doc, clause, part in sorted(found):
        if (doc, part) not in seen:
            seen.add((doc, part))
            out.append((doc, clause, part))
    return out


def _sources(db: Session) -> list[tuple[str, str, str, str]]:
    """库里已发布、未下线的依据原文所属文件：[(ref_document, ref_code, ref_status, ref_version)]。"""
    rows = db.execute(
        select(KbVersion.ref_document, KbVersion.ref_code, KbVersion.ref_status, KbVersion.ref_version)
        .join(KbItem, KbItem.current_version_id == KbVersion.id)
        .where(KbItem.doc_type == "SRC", KbItem.retired.is_(False))
        .distinct())
    return [tuple(r) for r in rows]


def _pick(cands: list[tuple[str, str, str, str]]) -> tuple[str, str, str, str] | None:
    if not cands:
        return None
    rank = {"current": 0, "upcoming": 1, "superseded": 2, "repealed": 3}
    return sorted(cands, key=lambda r: (rank.get(r[2], 9), r[3]), reverse=False)[0]


def _match(docs: list[tuple[str, str, str, str]], name: str) -> tuple[str, str, str, str] | None:
    if _is_code(name):
        code = _norm_code(name)
        exact = [r for r in docs if r[1] and _norm_code(r[1]) == code]
        if exact:
            return _pick(exact)
        return _pick([r for r in docs if r[1] and re.sub(r"-\d{4}$", "", _norm_code(r[1])) == code])
    n = _norm_doc(name)
    return _pick([r for r in docs if _norm_doc(r[0]) == n])


def check(db: Session, body: str, own_document: str = "", own_code: str = "") -> list[Citation]:
    refs = find(body, own_document, own_code)
    if not refs:
        return []
    docs = _sources(db)
    out: list[Citation] = []
    for name, clause, part in refs:
        c = Citation(document=name, clause=clause)
        out.append(c)
        doc = _match(docs, name)
        if doc is None:
            continue
        c.doc_known = True
        key = sources.src_key(doc[0], part)
        row = db.execute(
            select(KbItem.doc_id, KbVersion.body, KbVersion.ref_status, KbVersion.ref_version)
            .join(KbVersion, KbItem.current_version_id == KbVersion.id)
            .where(KbItem.import_key == key, KbItem.retired.is_(False))).first()
        if row is None:
            continue
        text = re.sub(r"^##[^\n]*\n+", "", row[1]).strip()
        c.found, c.src_doc_id, c.ref_status, c.ref_version = True, row[0], row[2], row[3]
        c.catalog = text == sources.CATALOG_NOTE
        c.excerpt = "" if c.catalog else text[:EXCERPT]
    return out


def for_version(db: Session, v: KbVersion) -> list[Citation]:
    """条目版本的引用核对结果；不是解读、映射、案例、异议应答的返回空。"""
    if v.item.doc_type not in CITING_TYPES:
        return []
    return check(db, v.body, v.ref_document, v.ref_code)


def pending(cites: list[Citation]) -> list[str]:
    return [c.label() for c in cites if not c.found]


def as_dicts(cites: list[Citation]) -> list[dict]:
    return [asdict(c) for c in cites]
