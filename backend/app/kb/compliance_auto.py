"""公开资料的合规关：自动为主（四期 B43，平台规划刷新设计 6.3，知识中心改版方案 4.2；负责人 2026-11-27“1可以”）。

- 适用：来源为公开资料、无需去标识的版本（公司做法、黄金样例不收公开资料，不经这里）；KB_COMPLIANCE_AUTO 关掉即不适用。
- 程序规则（RULES）：机密级检测（guard）、去标识无需或已通过、来源网址都在白名单（官方域名 sources.official_hosts，
  或来源登记表里的正式来源）、无资质结论用语、国标依据原文只收条款目录（版权）、没有写给 AI 的指令（疑似投毒）；
  走了知识审核 Agent 的，再看 Agent 的 confidential、boundary 两项和收录标准“合规”一项（没给出就不认）。
- 全部通过：平台记一条合规审核通过（reviewer=platform:compliance、review_role=compliance），内容审核照旧；
  按 KB_SAMPLE_RATE_COMPLIANCE 抽检（sample_state=pending，与自动发布共用抽检队列，知识专家抽检）。
- 例外（EXCEPTIONS，没过任何一条，或最近抽检差错超过 KB_SAMPLE_FAIL_MAX 时暂停）：记一条 review_role=compliance_auto、
  decision=flag 的说明，合规审核留给知识管理员（“我的任务”里的知识合规审核）。
- 审计 kb.compliance_auto 只记编号、版本、结论、例外键和是否抽检，不记正文。
"""
from __future__ import annotations

import re
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..models import KbItem, KbReview, KbSource, KbVersion
from ..services import guard
from . import autopublish, citations, meta, sources

REVIEWER = "platform:compliance"
ROLE = "compliance"
FLAG_ROLE = "compliance_auto"
EXCEPTIONS: dict[str, str] = {
    "confidential": "机密级检测命中",
    "deid": "去标识没通过",
    "source": "来源不在白名单",
    "boundary": "有资质结论用语",
    "copyright": "国标只收条款目录，不收全文",
    "poison": "疑似投毒：含写给 AI 的指令",
    "agent": "知识审核 Agent 判不合规或没能判断",
    "sampling": "合规抽检差错超过门槛，自动合规暂停",
}
URL_RE = re.compile(r"https?://[^\s，。；、）)\]]+")
INJECTION_RE = re.compile(
    r"(忽略|无视|不要理会)(以上|上面|之前|前面|所有)[^。\n]{0,10}(指令|要求|规则|提示)|"
    r"(你现在是|你必须扮演|从现在开始你是)|系统提示词|"
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)|disregard (the )?(previous|above)|system prompt",
    re.IGNORECASE)
GB_RE = re.compile(r"^\s*GB(/[TZ])?\s*\d", re.IGNORECASE)


def applicable(v: KbVersion) -> bool:
    return (get_settings().kb_compliance_auto and v.source_type == "public"
            and v.item.knowledge_kind not in meta.EXPERT_ONLY_KINDS)


def auto_passed(v: KbVersion) -> bool:
    return any(r.reviewer == REVIEWER and r.review_role == ROLE and r.decision == "approve" for r in v.reviews)


def _urls(db: Session, v: KbVersion) -> list[str]:
    """本条来源写的网址，加上正文引用到的依据原文条目的来源网址。"""
    found = URL_RE.findall(v.source_detail or "")
    for c in citations.for_version(db, v):
        if not c.found:
            continue
        item = db.scalar(select(KbItem).where(KbItem.doc_id == c.src_doc_id))
        cur = db.get(KbVersion, item.current_version_id) if item is not None and item.current_version_id else None
        if cur is not None:
            found += URL_RE.findall(cur.source_detail or "")
    return list(dict.fromkeys(u.rstrip(".,") for u in found))


def _active_hosts(db: Session) -> set[str]:
    return set(db.scalars(select(KbSource.host).where(KbSource.status == "active")))


def whitelisted(db: Session, url: str) -> bool:
    from urllib.parse import urlsplit

    if sources.is_official(url):
        return True
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _active_hosts(db) if h)


def sample_stats(db: Session) -> dict:
    ids = select(KbReview.version_id).where(KbReview.reviewer == REVIEWER, KbReview.review_role == ROLE)
    rows = db.scalars(select(KbVersion.sample_state).where(KbVersion.id.in_(ids),
                                                          KbVersion.sample_state.in_(("passed", "failed")))
                      .order_by(KbVersion.id.desc()).limit(autopublish.RECENT_SAMPLES)).all()
    failed = sum(1 for s in rows if s == "failed")
    limit = get_settings().kb_sample_fail_max
    return {"checked": len(rows), "failed": failed, "fail_max": limit,
            "suspended": autopublish.stop_rule(len(rows), failed, limit)}


def rule_exceptions(db: Session, v: KbVersion, agent_findings: dict | None = None) -> list[str]:
    from ..project.quality import qualification_hits

    text = f"{v.title}\n{v.summary}\n{v.applicability}\n{v.body}"
    out = []
    if guard.check(text).blocked:
        out.append("confidential")
    if v.deid_status != "not_needed":
        out.append("deid")
    urls = _urls(db, v)
    if not urls or not all(whitelisted(db, u) for u in urls):
        out.append("source")
    if qualification_hits(text):
        out.append("boundary")
    if v.item.doc_type == "SRC" and GB_RE.match(v.ref_code or "") and sources.CATALOG_NOTE not in v.body:
        out.append("copyright")
    if INJECTION_RE.search(text):
        out.append("poison")
    if agent_findings is not None:
        checks = agent_findings.get("checks", {})
        compliant = agent_findings.get("intake", {}).get("criteria", {}).get("compliant")
        if (agent_findings.get("agent") != "ok" or not checks.get("confidential", {}).get("pass")
                or not checks.get("boundary", {}).get("pass") or not (compliant and compliant.get("pass"))):
            out.append("agent")
    if sample_stats(db)["suspended"]:
        out.append("sampling")
    return out


def run(db: Session, v: KbVersion, agent_findings: dict | None = None) -> list[str] | None:
    """判合规：返回例外键列表（空表示自动通过）；不适用或已有合规通过记录时返回 None。不提交事务，调用方提交。"""
    if not applicable(v) or any(r.review_role == ROLE and r.decision == "approve" for r in v.reviews):
        return None
    exc = rule_exceptions(db, v, agent_findings)
    sampled = False
    old = next((r for r in v.reviews if r.reviewer == REVIEWER and r.review_role == FLAG_ROLE), None)
    if not exc:
        sampled = secrets.SystemRandom().random() < get_settings().kb_sample_rate_compliance
        if sampled and v.sample_state not in ("passed", "failed"):
            v.sample_state = "pending"
        v.reviews.append(KbReview(reviewer=REVIEWER, review_role=ROLE, decision="approve",
                                  comment="合规关自动通过：机密级、去标识、来源白名单、资质用语、版权、指令检测均通过"
                                          + ("；已抽中，待知识专家抽检" if sampled else ""),
                                  findings={"auto": True, "sampled": sampled}))
    else:
        comment = "合规关转知识管理员：" + "；".join(EXCEPTIONS[k] for k in exc)
        if old is None:
            v.reviews.append(KbReview(reviewer=REVIEWER, review_role=FLAG_ROLE, decision="flag", comment=comment,
                                      findings={"exceptions": exc}))
        else:
            old.comment, old.findings = comment, {"exceptions": exc}
    audit.record(db, actor=REVIEWER, action="kb.compliance_auto", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, outcome="exception" if exc else "passed", exceptions=exc, sampled=sampled)
    return exc
