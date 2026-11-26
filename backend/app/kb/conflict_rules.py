"""知识审核“不重复、不冲突”的规则预判（四期试用缺陷，负责人 2026-11-25 同意，决策记录同日）。

试用机上大量公开资料解读卡在人工队列，理由多为“与其他法律条文重复或冲突”：Agent 只拿到相近条目的编号和标题，
拿不准就判不过。现在先由程序（不调模型）逐条判定待审版本与相近条目（review.similar_items）的关系：

- duplicate 真重复：正文规范化（全半角、空白、标点、Markdown 符号）后哈希相同。不发布：第二级直接退回
  （同人工驳回，findings 写明重复的编号）；第三级只出意见，由专家拍板。
- source_text 依据原文：相近条目是同一依据文件的原文（SRC），是依据不是冲突。
- supersedes 以本条为准：同一依据文件，本条依据的版本较新（ref_version / 文号年份 / 施行日期）。不冲突；
  本条发布时把旧条目的现行版本标待复查（复用 B13 与专家工作台冲突卡“以这条为准”的做法）。
- outdated 已有较新版本：同一依据文件，已有条目依据的版本较新。规则判“不冲突”不过、理由写明对方编号，
  转人工由冲突处理卡拍板（版本先后有可能是有意保留的历史版本）。
- different_clause 不同条款：同一依据文件、同一版本、两边引到的条款号都能识别且不相交。不冲突。
- jurisdiction 管辖不同：两边都是写了依据文件的 REF，管辖地不同，或行业都不含“通用”且不相交。不冲突。
- agent 其余交 Agent：附编号、标题、依据文件、版本、文号、行业和摘要（总长有上限）。

没有交 Agent 的相近条目时，“不重复、不冲突”一项由规则给出（rule=true），Agent 对这一项的判断和问题降为参考。
审计只记编号和类别，不记正文。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..core.db import utcnow
from ..models import KbItem, KbVersion
from . import citations
from . import review as rv
from .recheck import OPEN_STATES

KINDS = {"duplicate": "正文相同（真重复）", "source_text": "同一依据文件的原文", "supersedes": "同一依据文件的较新版本，以本条为准",
         "outdated": "同一依据文件已有较新版本", "different_clause": "同一依据文件的不同条款",
         "jurisdiction": "管辖地或行业不同", "agent": "交 Agent 判断"}
NOT_CONFLICT = ("source_text", "supersedes", "different_clause", "jurisdiction")
GENERAL = "通用"
SIMILAR_LIMIT = 5
MAX_SIMILAR_PIECE = 300
MAX_SIMILAR_TOTAL = 3_000

_YEAR = re.compile(r"(19|20)\d{2}")
_CODE_YEAR = re.compile(r"\s*[-—]\s*(?:19|20)\d{2}\s*$")
_PAREN = re.compile(r"[（(][^）)]*[）)]")


@dataclass
class Relation:
    doc_id: str
    kind: str
    title: str = ""
    other: KbVersion | None = None

    def label(self) -> str:
        return f"{self.doc_id}（{KINDS[self.kind]}）"


@dataclass
class Prejudge:
    relations: list[Relation] = field(default_factory=list)

    def of(self, *kinds: str) -> list[Relation]:
        return [r for r in self.relations if r.kind in kinds]

    @property
    def verdict(self) -> str:
        """duplicate / outdated（规则判不过）、agent（有交 Agent 的）、pass（规则全判不冲突，含没有相近条目）。"""
        for k in ("duplicate", "outdated", "agent"):
            if self.of(k):
                return k
        return "pass"

    def check(self) -> dict | None:
        """规则给出的“不重复、不冲突”一项；要交 Agent 判断时返回 None。"""
        v = self.verdict
        if v == "agent":
            return None
        if v == "duplicate":
            ids = "、".join(r.doc_id for r in self.of("duplicate"))
            return {"pass": False, "rule": True, "reason": f"规则判定：与 {ids} 正文相同，真重复，本版本不发布"}
        if v == "outdated":
            ids = "、".join(r.doc_id for r in self.of("outdated"))
            return {"pass": False, "rule": True,
                    "reason": f"规则判定：与 {ids} 依据同一文件，{ids} 依据的版本较新，以新为准；请在冲突处理卡上拍板"}
        if not self.relations:
            return {"pass": True, "rule": True, "reason": "规则判定：库里没有同产品的相近条目"}
        return {"pass": True, "rule": True, "reason": "规则判定：" + "；".join(r.label() for r in self.relations)}

    def summary(self) -> dict:
        """写进 findings 和审计：只有编号和类别。"""
        return {"verdict": self.verdict, "relations": [{"doc_id": r.doc_id, "kind": r.kind} for r in self.relations],
                "supersedes": [r.doc_id for r in self.of("supersedes")]}


# ---- 规范化 ----

def body_hash(text: str) -> str:
    s = str(text or "").translate({**{0xFF10 + i: 0x30 + i for i in range(10)}, 0x3000: 0x20})
    s = re.sub(r"[\s#*>`_\-|]+", "", s)
    s = re.sub(r"[，。；：、！？“”‘’（）《》【】,.;:!?\"'()\[\]<>]", "", s).lower()
    return hashlib.sha256(s.encode()).hexdigest()


def doc_keys(v: KbVersion) -> set[str]:
    """依据文件的标识：标准号去掉年份、文件名去掉“中华人民共和国”和括号里的修正说明。"""
    keys = set()
    code = (v.ref_code or "").strip()
    if code:
        keys.add("code:" + _CODE_YEAR.sub("", citations._norm_code(code)))
    name = (v.ref_document or "").strip()
    if name:
        if citations._is_code(name):
            keys.add("code:" + _CODE_YEAR.sub("", citations._norm_code(name)))
        else:
            keys.add("doc:" + citations._norm_doc(_PAREN.sub("", name)))
    return keys


def version_year(v: KbVersion) -> int | None:
    for s in (v.ref_version, v.ref_code, v.ref_document):
        m = _YEAR.search(s or "")
        if m:
            return int(m.group(0))
    return v.effective_date.year if v.effective_date else None


def clauses(v: KbVersion) -> set[str]:
    """本条引到的、属于自己依据文件的条款号（B7 key 用的条号）；标题里有就只看标题。"""
    own = doc_keys(v)
    out = set()
    for text in (v.title, v.body):
        for doc, _, part in citations.find(text or "", v.ref_document, v.ref_code):
            key = ("code:" + _CODE_YEAR.sub("", citations._norm_code(doc)) if citations._is_code(doc)
                   else "doc:" + citations._norm_doc(_PAREN.sub("", doc)))
            if key in own:
                out.add(part)
        if out:
            break
    return out


# ---- 判定 ----

def relate(v: KbVersion, other: KbVersion, doc_id: str) -> Relation:
    rel = Relation(doc_id, "agent", other.title, other)
    if body_hash(v.body) == body_hash(other.body):
        rel.kind = "duplicate"
        return rel
    if doc_keys(v) & doc_keys(other):
        if other.item.doc_type == "SRC":
            rel.kind = "source_text"
            return rel
        mine, theirs = version_year(v), version_year(other)
        if mine and theirs and mine != theirs:
            rel.kind = "supersedes" if mine > theirs else "outdated"
            return rel
        a, b = clauses(v), clauses(other)
        if a and b and not a & b:
            rel.kind = "different_clause"
        return rel
    if (v.item.doc_type == other.item.doc_type == "REF" and v.ref_document.strip() and other.ref_document.strip()):
        ja, jb = (v.item.jurisdiction or "CN"), (other.item.jurisdiction or "CN")
        ia, ib = set(v.item.industries or []), set(other.item.industries or [])
        if ja != jb or (ia and ib and GENERAL not in ia | ib and not ia & ib):
            rel.kind = "jurisdiction"
    return rel


def _live(db: Session, doc_id: str) -> KbVersion | None:
    item = db.query(KbItem).filter(KbItem.doc_id == doc_id).one_or_none()
    cur = db.get(KbVersion, item.current_version_id) if item and item.current_version_id else None
    if item is None or item.retired or cur is None or cur.status != "published":
        return None
    return cur


def prejudge(db: Session, v: KbVersion, similar: list[dict] | None = None) -> Prejudge:
    if similar is None:
        similar = rv.similar_items(db, v, limit=SIMILAR_LIMIT)
    out = Prejudge()
    for s in similar:
        other = _live(db, s["doc_id"])
        if other is None or other.item_id == v.item_id:
            continue
        out.relations.append(relate(v, other, s["doc_id"]))
    return out


def agent_block(pre: Prejudge) -> str:
    """交 Agent 的“相近条目”一节：规则没判掉的附摘要（总长有上限），判掉的只列编号和类别。"""
    lines, total = [], 0
    todo = pre.of("agent")
    for i, r in enumerate(todo):
        o = r.other
        brief = (o.summary or "").strip() or (o.body or "")[:MAX_SIMILAR_PIECE]
        piece = (f"## {r.doc_id} {o.title}\n依据文件：{o.ref_document} {o.ref_version} {o.ref_code}　"
                 f"行业：{'、'.join(o.item.industries or []) or '未标'}\n摘要：{brief[:MAX_SIMILAR_PIECE]}")
        if total + len(piece) > MAX_SIMILAR_TOTAL:
            lines.append(f"（其余 {len(todo) - i} 条相近条目超出长度，只列编号：{'、'.join(x.doc_id for x in todo[i:])}）")
            break
        lines.append(piece)
        total += len(piece)
    done = [r for r in pre.relations if r.kind != "agent"]
    if done:
        lines.append("已由规则判定、无需再判：" + "；".join(r.label() for r in done))
    return "\n\n".join(lines) or "（没有找到相近条目）"


def apply(findings: dict, pre: Prejudge) -> dict:
    """规则给出“不重复、不冲突”时覆盖 Agent 的这一项，Agent 就这一项提的阻断 / 重要问题降为建议（记原级别）。"""
    findings = {**findings, "prejudge": pre.summary()}
    rule = pre.check()
    if rule is None or "checks" not in findings:
        return findings
    agent_said = findings["checks"].get("no_conflict", {})
    findings["checks"] = {**findings["checks"],
                          "no_conflict": {**rule, "agent_reason": str(agent_said.get("reason", ""))[:300]}}
    if rule["pass"]:
        findings["issues"] = [({**i, "severity": "建议", "original_severity": i["severity"]}
                               if i.get("check") == "no_conflict" and i["severity"] in ("阻断", "重要") else i)
                              for i in findings.get("issues", [])]
        clean = (all(c.get("pass") for c in findings["checks"].values())
                 and not any(i["severity"] in ("阻断", "重要") for i in findings["issues"]))
        if clean and findings.get("conclusion") != "通过":   # Agent 只因这一项存疑：规则判掉后结论改为通过，记原结论
            findings["agent_conclusion"], findings["conclusion"] = findings.get("conclusion", ""), "通过"
    return findings


def latest_agent_findings(v: KbVersion) -> dict:
    agent = [r for r in v.reviews if r.review_role == "agent"]
    if not agent:
        return {}
    f = max(agent, key=lambda x: x.id).findings
    return f if isinstance(f, dict) else {}


def mark_superseded(db: Session, v: KbVersion) -> list[str]:
    """本条发布后：规则判定“以本条为准”的旧条目现行版本标待复查（复用 B13，进抽检队列），已在队列里的不重复标。"""
    ids = (latest_agent_findings(v).get("prejudge") or {}).get("supersedes") or []
    marked = []
    for doc_id in ids:
        cur = _live(db, doc_id)
        if cur is None or cur.recheck_state in OPEN_STATES:
            continue
        cur.recheck_state, cur.recheck_at = "pending", utcnow()
        cur.recheck_reason = f"规则预判：{v.item.doc_id} 依据同一文件的较新版本，以新为准"[:200]
        marked.append(doc_id)
    if marked:
        db.commit()
    return marked


def rerun_due(v: KbVersion) -> bool:
    """积压重跑：最近一次 Agent 审核“不重复、不冲突”没过，且是规则预判上线之前审的（没有 prejudge），只重跑一次。"""
    f = latest_agent_findings(v)
    c = (f.get("checks") or {}).get("no_conflict")
    return bool(f) and "prejudge" not in f and isinstance(c, dict) and not c.get("pass", True)
