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
MINOR = "建议"
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


# 按交付件的评审档案（A13，产品配置 deliverables[].review_profile）：名称 → (显示名, 发给评审 Agent 的说明)
PROFILES: dict[str, tuple[str, str]] = {
    "full": ("全面评审", "按全部评审维度评审。"),
    "light": ("只查红线和专项", "本交付件是过程材料，只查封闭五类阻断（编造或用错依据、资质用语、机密与客户信息、"
              "对客结论无证据、与已确认事项矛盾）和产品门禁专项；其他维度的意见最多标“建议”。"),
}


def profile_of(snap: dict, deliverable_code: str) -> str:
    """项目配置快照里某交付件的评审档案；旧快照没有这个字段按 full。"""
    for d in snap.get("deliverables", ()):
        if d.get("code") == deliverable_code:
            return d.get("review_profile") if d.get("review_profile") in PROFILES else "full"
    return "full"


def profile_block(name: str) -> str:
    label, text = PROFILES.get(name, PROFILES["full"])
    return f"\n\n# 评审档案：{label}\n{text}"


def apply_profile(issues: list[dict], counts: dict[str, int], name: str) -> int:
    """light 档：评审 Agent 判的五类以外的阻断、重要一律降为建议（在 apply_closed_categories 之后调用）；返回降级条数。"""
    if name != "light":
        return 0
    n = 0
    for i in issues:
        if i.get("by") != "agent" or i.get("severity") not in (BLOCKING, MAJOR) or category(i):
            continue
        counts[i["severity"]] = max(counts.get(i["severity"], 0) - 1, 0)
        counts[MINOR] = counts.get(MINOR, 0) + 1
        i["severity"], i["profile_capped"] = MINOR, True
        n += 1
    return n


def soft(issue: dict) -> bool:
    """可驳回、驳回后不重提的问题：评审 Agent 提的，或 A18 矛盾核对（程序比对，可能误报）判的。"""
    return issue.get("by") == "agent" or issue.get("rule") == "contradiction"


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
            if row.status == "waived" and soft(i):
                i["status"] = WAIVED_MARK
                sev = i.get("severity", "")
                if sev in counts and i.get("by") == "agent":
                    counts[sev] = max(counts[sev] - 1, 0)
                stats["carried_waived"] += 1
            elif row.status == "resolved":
                row.status, row.resolved_review_id = "open", None
                stats["reopened"] += 1
            row.last_review_id, row.updated_at = r.id, now
        elif row.status == "waived" and soft(i):
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


# ---- A13②：增量评审 ----

_CHAPTER = re.compile(r"第\s*(\d+)\s*章")


def sections_of(location: str, titles: dict[str, str]) -> set[str]:
    """问题“位置”栏指向的章节序号：写了“第 N 章”或含章节标题的算；指不出章节（全文、跨章）返回空集。"""
    found = set(_CHAPTER.findall(location or ""))
    found |= {seq for seq, t in titles.items() if t and t in (location or "")}
    return {s for s in found if s in titles}


def plan_incremental(prev: QualityReview | None, r: QualityReview, prompt_version: str,
                     titles: dict[str, str]) -> tuple[set[str], list[dict], list[dict]] | None:
    """增量评审计划：返回（改过的章节序号、要 Agent 复核的上轮问题、原样沿用的上轮问题）；该全文评审时返回 None。

    全文评审：没有上一轮完成的模型评审、评审提示词换了版本、章节结构变了、没改或全部章节都改了。
    上轮问题只取评审 Agent 提的、未标已驳回的；平台规则问题每轮按全文重算，不沿用。
    """
    if prev is None or not prev.prompt_version or prev.prompt_version != prompt_version:
        return None
    old, new = prev.versions or {}, r.versions or {}
    if set(old) != set(new) or set(new) != set(titles):
        return None
    changed = {k for k in new if new[k] != old[k]}
    if not changed or changed == set(new):
        return None
    recheck, carried = [], []
    for i in prev.issues or []:
        if i.get("by") != "agent" or i.get("status") == WAIVED_MARK:
            continue
        secs = sections_of(i.get("location", ""), titles)
        if secs and not secs & changed:
            carried.append({**i, "carried": True})
        else:
            recheck.append(i)
    return changed, recheck, carried


def recheck_block(recheck: list[dict]) -> str:
    if not recheck:
        return ""
    rows = "\n".join(f"| {i.get('code', '')} | {i.get('location', '')} | {i.get('dimension', '')} | "
                     f"{i.get('severity', '')} | {i.get('problem', '')} |" for i in recheck)
    return ("\n\n# 上轮未关闭的问题（请复核：已改正的不要再列；仍存在的照列，并在“问题”栏写原编号）\n"
            "| 编号 | 位置 | 维度 | 严重度 | 问题 |\n|---|---|---|---|---|\n" + rows)
