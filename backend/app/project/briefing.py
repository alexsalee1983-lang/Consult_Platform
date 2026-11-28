"""D8 高层汇报从真源取要点（四期 A39，《交付链输入输出方案》第六节：程序从 D4 / D5 / D7 真源取要点，模型写结论与预判问答）。

- D4 真源是发现表（app/project/findings.py）：只取顾问确认过的发现（confirmed 及以后），草稿不算；
  先给按结论的计数，再按优先级、影响取不符合 / 部分符合 / 证据不足的前 MAX_FINDINGS 条，每条带 F 编号。
- D5 / D7：A34 / A36 的结构化输出合并前，读交付件当前版本，每章取正文首句，标 [Dx §章节]。
- 程序只摘不改写；模型据此写结论，引用编号让顾问能追溯。只读平台已有内容，不调模型、不记审计（调用方的运行步骤已审计）。
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..models import Finding, Project
from . import deliverables, findings

DELIVERABLE = "D8"
MARK = "# D8 汇报要点（平台从发现表与 D5 / D7 当前版本摘取，结论以此为准）"
MAX_FINDINGS = 8
SENTENCE_CHARS = 120
GAP_CONCLUSIONS = ("unmet", "partial", "insufficient")
LABELS = {"unmet": "不符合", "partial": "部分符合", "insufficient": "证据不足", "met": "符合", "na": "不适用"}
DOCS = ("D5", "D7")
_RANK = {"高": 0, "中": 1, "低": 2}


def _confirmed(db: Session, project: Project) -> list[Finding]:
    return [f for f in findings.list_for_project(db, project) if f.status != "draft"]


def _finding_lines(rows: list[Finding]) -> list[str]:
    if not rows:
        return []
    counts = "、".join(f"{label} {n}" for c, label in LABELS.items()
                       if (n := sum(f.conclusion == c for f in rows)))
    gaps = sorted((f for f in rows if f.conclusion in GAP_CONCLUSIONS),
                  key=lambda f: (_RANK.get(f.priority, 3), _RANK.get(f.impact, 3), f.fid))
    out = ["## 发现表（D4 真源）", f"已确认发现 {len(rows)} 条：{counts}。"]
    for f in gaps[:MAX_FINDINGS]:
        parts = [f.domain, f.item, LABELS.get(f.conclusion, f.conclusion)]
        if f.priority:
            parts.append(f"优先级{f.priority}")
        if f.risk:
            parts.append(f"风险：{f.risk}")
        if f.recommendation:
            parts.append(f"建议：{f.recommendation}")
        out.append(f"- [{f.fid}] " + "｜".join(x for x in parts if x)[:300])
    if len(gaps) > MAX_FINDINGS:
        out.append(f"（另有 {len(gaps) - MAX_FINDINGS} 条差距见 D4 发现表）")
    return out


def _first_sentence(body: str) -> str:
    for line in body.splitlines():
        line = re.sub(r"^\s*(?:[-*+>]|\d+[.、)])\s*", "", line).strip()
        if not line or line.startswith(("#", "|")):
            continue
        m = re.match(r"(.+?[。！？；])", line)
        return (m.group(1) if m else line)[:SENTENCE_CHARS]
    return ""


def _doc_lines(db: Session, project: Project, code: str) -> list[str]:
    d = deliverables.get_deliverable(db, project, code)
    if d is None:
        return []
    out = []
    for s in d.sections:
        v = deliverables.get_version(s)
        if v is not None and (first := _first_sentence(v.body)):
            out.append(f"- [{code} §{s.title}] {first}")
    return [f"## {code} {d.title}", *out] if out else []


def points(db: Session, project: Project) -> str:
    """D8 起草步骤的要点块；真源都还没有内容时返回空串（不加块，步骤照常按 inputs 读全文）。"""
    blocks = [_finding_lines(_confirmed(db, project)), *(_doc_lines(db, project, c) for c in DOCS)]
    blocks = [b for b in blocks if b]
    return "\n\n".join([MARK, *("\n".join(b) for b in blocks)]) if blocks else ""
