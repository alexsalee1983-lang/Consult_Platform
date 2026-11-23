"""三期 A13：评审问题固定编号、五类阻断、已驳回不重提（迁移 0041，表 review_issues）。

- 封闭五类阻断：评审 Agent 判的阻断只有落在五类里才算阻断（编造或用错依据、资质用语、机密与客户信息、
  对客结论无证据、与已确认事项矛盾），其余自动降为重要并标 downgraded；平台规则判的不动。
  归类按问题的“维度”和“问题”两栏找关键词，宁宽勿严（找到任何一类就保留阻断）。
- 固定编号：每条问题按要素（提出方、维度、位置、问题前 40 字，去空白标点）算指纹，同一交付件内指纹相同或
  评审 Agent 在位置 / 问题栏写了已有编号 Q-001 的，沿用原编号；新问题按交付件顺序编号。
- 状态：上一轮未解决、这一轮没再出现的记 resolved；resolved 的再出现重新打开（同一编号）；
  项目经理驳回过的（waived），评审 Agent 再提时标“已驳回”、不计入统计、门禁不再要求处理。
- 台账只记编号、指纹、类别和状态，不记问题原文；审计只记计数。
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.db import utcnow
from ..models import Deliverable, QualityReview, ReviewIssue

BLOCKING = "阻断"
MAJOR = "重要"
WAIVED_MARK = "已驳回"
# 封闭五类（与 qa-reviewer.md 的阻断定义一致，C9 同日合并）：类别名 → 关键词
CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("依据", ("依据", "条款", "标准号", "文号", "法规")),
    ("资质用语", ("资质", "测评结论", "认证结论", "密评结论")),
    ("机密与客户信息", ("保密", "机密", "客户信息", "客户名称", "个人信息", "口令", "密钥")),
    ("结论无证据", ("证据",)),
    ("与已确认事项矛盾", ("矛盾", "确认卡", "确认单", "已确认")),
)
_CODE = re.compile(r"\bQ-(\d{3,4})\b")
_NOISE = re.compile(r"[\s\W_]+", re.UNICODE)


def category(issue: dict) -> str:
    text = f"{issue.get('dimension', '')} {issue.get('problem', '')}"
    return next((name for name, words in CATEGORIES if any(w in text for w in words)), "")


def apply_closed_categories(issues: list[dict], counts: dict[str, int]) -> int:
    """评审 Agent 判的阻断不在五类内的降为重要；改 issues 和 Agent 统计 counts，返回降级条数。"""
    n = 0
    for i in issues:
        if i.get("by") != "agent" or i.get("severity") != BLOCKING:
            continue
        cat = category(i)
        if cat:
            i["category"] = cat
        else:
            i["severity"], i["downgraded"] = MAJOR, True
            n += 1
    if n:
        counts[BLOCKING] = max(counts.get(BLOCKING, 0) - n, 0)
        counts[MAJOR] = counts.get(MAJOR, 0) + n
    return n


def _norm(s: str) -> str:
    return _NOISE.sub("", s or "")


def fingerprint(issue: dict) -> str:
    key = "|".join((issue.get("by", ""), _norm(issue.get("dimension", "")), _norm(issue.get("location", "")),
                    _norm(issue.get("problem", ""))[:40]))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _cited_code(issue: dict) -> str:
    m = _CODE.search(f"{issue.get('location', '')} {issue.get('problem', '')}")
    return f"Q-{int(m.group(1)):03d}" if m else ""


def rows(db: Session, d: Deliverable) -> list[ReviewIssue]:
    return list(db.scalars(select(ReviewIssue).where(ReviewIssue.deliverable_id == d.id).order_by(ReviewIssue.seq)))


def reconcile(db: Session, d: Deliverable, r: QualityReview, issues: list[dict],
              counts: dict[str, int]) -> dict[str, int]:
    """给本轮问题编号、更新台账状态；已驳回的 Agent 问题标记并从 counts 扣除。返回计数（进审计）。"""
    existing = rows(db, d)
    by_fp = {x.fingerprint: x for x in existing}
    by_code = {x.code: x for x in existing}
    seq = db.scalar(select(func.max(ReviewIssue.seq)).where(ReviewIssue.deliverable_id == d.id)) or 0
    stats = {"new": 0, "reopened": 0, "carried_waived": 0, "resolved": 0}
    seen: set[int] = set()
    now = utcnow()
    for i in issues:
        fp = fingerprint(i)
        row = by_code.get(_cited_code(i)) if i.get("by") == "agent" else None
        row = row or by_fp.get(fp)
        if row is None:
            seq += 1
            row = ReviewIssue(project_id=d.project_id, deliverable_id=d.id, seq=seq, code=f"Q-{seq:03d}",
                              fingerprint=fp, by=i.get("by", ""), category=i.get("category", "")[:16],
                              status="open", first_review_id=r.id, last_review_id=r.id, updated_at=now)
            db.add(row)
            by_fp[fp], by_code[row.code] = row, row
            stats["new"] += 1
        elif row.id not in seen:
            if row.status == "waived" and i.get("by") == "agent":
                i["status"] = WAIVED_MARK
                sev = i.get("severity", "")
                if sev in counts:
                    counts[sev] = max(counts[sev] - 1, 0)
                stats["carried_waived"] += 1
            elif row.status == "resolved":
                row.status, row.resolved_review_id = "open", None
                stats["reopened"] += 1
            row.last_review_id, row.updated_at = r.id, now
        elif row.status == "waived" and i.get("by") == "agent":
            i["status"] = WAIVED_MARK
        i["code"] = row.code
        if row.id is not None:
            seen.add(row.id)
    for row in existing:
        if row.status == "open" and row.id not in seen:
            row.status, row.resolved_review_id, row.updated_at = "resolved", r.id, now
            stats["resolved"] += 1
    db.flush()
    return stats


def mark_waived(db: Session, d: Deliverable, issue: dict, waiver_id: int) -> None:
    code = issue.get("code")
    if not code:
        return
    row = db.scalar(select(ReviewIssue).where(ReviewIssue.deliverable_id == d.id, ReviewIssue.code == code))
    if row is not None:
        row.status, row.waiver_id, row.updated_at = "waived", waiver_id, utcnow()
