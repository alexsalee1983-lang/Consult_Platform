"""四期 A32②：确认卡证据并排与同源合卡（平台规划刷新设计第十二节）。只读、只管展示，不改卡的状态机。

1. 证据：卡片标题、理由、影响、选项文字里的证据标注（[I-01-02] [D-12] [O-05]，严格档的 [S-xx] 不取原文）按出现顺序
   取前 MAX_REFS 处，各给一段原文片段并排显示：标注带段落号（如 -02）的取该段，没带的取与卡片文字最相近的一段。
   段落号 = 证据正文按行（非空）从 1 起连续编号；客户资料按解析结果的章节顺序编号，按页解析的 PDF（章节名“第 N 页”）
   同时给出页码。已删除、仅现场使用、还没解析完的只给编号和说明，不给原文。
2. 合卡：同一份资料（卡片里引用的第一处证据）引出的多张卡合成一组，界面显示为一张；处理仍逐张进行。
不建表、不写审计（只读接口，与成熟度等读取接口一致）。
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..models import Confirmation, Project
from . import evidence as evidence_svc

MAX_REFS = 2
EXCERPT_CHARS = 300
_REF = re.compile(r"\[((?:I|D|O)-\d{2,4})(?:-(\d{1,3}))?\]")
_PAGE = re.compile(r"^第\s*(\d+)\s*页$")


def card_text(c: Confirmation) -> str:
    opts = " ".join(str(o.get("text", "")) for o in (c.options or []) if isinstance(o, dict))
    return " ".join(x for x in (c.title, c.reason, c.impact, opts) if x)


def refs_of(c: Confirmation) -> list[tuple[str, int | None]]:
    """卡片里的证据标注 → [(证据编号, 段落号或 None)]，同一证据只取第一次。"""
    seen: dict[str, int | None] = {}
    for m in _REF.finditer(card_text(c)):
        seen.setdefault(m.group(1), int(m.group(2)) if m.group(2) else None)
    return list(seen.items())


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def locate(sections: list[dict], paragraph: int | None, hint: str) -> dict | None:
    """sections：[{heading, text}] → 定位一段：{page, paragraph, heading, excerpt}；没有正文返回 None。"""
    paras: list[tuple[int | None, str, str]] = []          # (页码, 章节名, 段落文字)
    for s in sections:
        heading = s.get("heading") or ""
        m = _PAGE.match(heading.strip())
        page = int(m.group(1)) if m else None
        for line in (s.get("text") or "").splitlines():
            if line.strip():
                paras.append((page, heading, line.strip()))
    if not paras:
        return None
    if paragraph is not None and 1 <= paragraph <= len(paras):
        idx = paragraph - 1
    else:
        want = _bigrams(hint)
        scores = [len(want & _bigrams(t)) for _, _, t in paras]
        idx = max(range(len(paras)), key=lambda i: (scores[i], -i))
    page, heading, text = paras[idx]
    return {"page": page, "paragraph": idx + 1, "heading": heading, "excerpt": text[:EXCERPT_CHARS]}


def _sections_of(ev) -> tuple[list[dict] | None, str]:
    if ev.deleted_at is not None:
        return None, evidence_svc.DELETED_NOTE
    if ev.kind != "document":
        return [{"heading": "", "text": ev.content or ""}], ""
    d = ev.document
    if d is None:
        return None, "资料文件不存在"
    if d.local_only:
        return None, evidence_svc.LOCAL_ONLY_NOTE
    sections = evidence_svc.document_sections(d)
    return (sections, "") if sections is not None else (None, "资料还在解析，稍后再看")


def evidence_for(db: Session, project: Project, cards: list[Confirmation]) -> dict[int, list[dict]]:
    """每张卡 → 前 MAX_REFS 处证据的原文片段。同一份证据在多张卡间只读一次。"""
    cache: dict[str, tuple[object, list[dict] | None, str]] = {}
    out: dict[int, list[dict]] = {}
    for c in cards:
        items: list[dict] = []
        if project.tier != "strict":
            for code, para in refs_of(c)[:MAX_REFS]:
                if code not in cache:
                    ev = evidence_svc.get_evidence(db, project, code)
                    cache[code] = (ev, *(_sections_of(ev) if ev is not None else (None, "本项目没有这条证据")))
                ev, sections, note = cache[code]
                item = {"code": code, "ref": f"{code}-{para:02d}" if para else code,
                        "kind": getattr(ev, "kind", ""), "title": getattr(ev, "title", ""),
                        "interviewee_role": getattr(ev, "interviewee_role", "") or ""}
                hit = locate(sections, para, card_text(c)) if sections is not None else None
                if hit is None:
                    item.update(found=False, note=note or "证据没有正文", page=None, paragraph=None, heading="",
                                excerpt="")
                else:
                    item.update(found=True, note="", **hit)
                items.append(item)
        out[c.id] = items
    return out


def groups(cards: list[Confirmation], evidence: dict[int, list[dict]]) -> list[dict]:
    """同一份资料引出的卡合成一组（按卡里第一处证据），组按最早一张卡排序；没有证据的卡单独成组。"""
    by: dict[str, dict] = {}
    out: list[dict] = []
    for c in sorted(cards, key=lambda x: x.id):
        first = (evidence.get(c.id) or [None])[0]
        if first is None:
            out.append({"source": "", "title": "", "card_ids": [c.id]})
            continue
        g = by.get(first["code"])
        if g is None:
            g = by[first["code"]] = {"source": first["code"], "title": first.get("title", ""), "card_ids": []}
            out.append(g)
        g["card_ids"].append(c.id)
    return out
