"""四期 A18：严格度二批（决策记录 2026-11-24，#430）。

① 与已确认事项矛盾的程序核对（评审时与平台规则一起跑，判阻断）：
   - 确认卡：已处理、选了选项、关联到本交付件的卡（关联到章的只查那一章，没关联章的查全文各章）。
     某章正文写了未选选项的原文、却没写所选选项的原文 → 阻断。
   - 事实确认单：已确认的封闭题事实（E2①）。某章正文写了该题其他选项的原文、却没写已确认选项的原文 → 阻断。
   比较前去掉空白和标点；选项原文规范化后不足 MIN_CHARS 个字的不查（“有”“无”这类短选项误报太多），
   未选选项是所选选项一部分的不查。问题栏只写卡号 / 题号和选项键，不摘正文和选项原文。
   这类阻断是程序按文字比对判的，可能误报，项目经理可以写理由驳回（与评审 Agent 的阻断一样）。
② 阻断误报率度量与自动校准：按问题台账（review_issues，只有编号、类别和状态），
   每个“提出方 + 五类类别”统计提出过的阻断条数和被项目经理驳回的条数，驳回率即误报率。
   样本不少于 CALIBRATE_MIN 条、驳回率不低于 CALIBRATE_RATE 的类别，之后的评审中这类阻断自动降为“重要”（提醒），
   问题标 calibrated。机密与客户信息、资质用语两类是红线和资质边界，永不降级。
"""
from __future__ import annotations

import re

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..models import Confirmation, Deliverable, Project, ReviewIssue, SurveyAnswer, SurveyQuestion
from . import deliverables

RULE = "contradiction"
DIMENSION = "与已确认事项矛盾"
MIN_CHARS = 6
CALIBRATE_MIN = 20
CALIBRATE_RATE = 0.3
PROTECTED = frozenset({"机密与客户信息", "资质用语"})
_NOISE = re.compile(r"[\s\W_]+", re.UNICODE)


def _norm(s: str) -> str:
    return _NOISE.sub("", s or "")


def _contradicts(body: str, chosen: str, others: list[tuple[str, str]]) -> list[str]:
    """正文（已规范化）写了哪些未选选项（返回选项键）；写了所选选项的不算矛盾。"""
    c = _norm(chosen)
    if not c or c in body:
        return []
    normed = [(k, _norm(t)) for k, t in others]
    return [k for k, t in normed if len(t) >= MIN_CHARS and t not in c and t in body]


def _chapters(d: Deliverable, versions: dict) -> list[tuple[int, str, str]]:
    out = []
    for s in d.sections:
        ver = int(versions.get(str(s.seq), 0) or 0)
        v = deliverables.get_version(s, ver) if ver else None
        if v is not None:
            out.append((s.seq, f"第 {s.seq} 章 {s.title}", _norm(v.body)))
    return out


def _issue(loc: str, problem: str, suggestion: str) -> dict:
    return {"location": loc, "dimension": DIMENSION, "severity": "阻断", "problem": problem,
            "suggestion": suggestion, "by": "rule", "rule": RULE, "category": DIMENSION}


def check(db: Session, project: Project, d: Deliverable, versions: dict) -> list[dict]:
    chapters = _chapters(d, versions)
    if not chapters:
        return []
    issues: list[dict] = []
    cards = db.scalars(select(Confirmation).where(
        Confirmation.project_id == project.id, Confirmation.status == "resolved",
        Confirmation.deliverable_code == d.code, Confirmation.choice != "").order_by(Confirmation.id)).all()
    for c in cards:
        opts = {o.get("key", ""): o.get("text", "") for o in c.options or []}
        if c.choice not in opts:
            continue
        others = [(k, t) for k, t in opts.items() if k != c.choice]
        for seq, loc, body in chapters:
            if c.section_seq is not None and c.section_seq != seq:
                continue
            hit = _contradicts(body, opts[c.choice], others)
            if hit:
                issues.append(_issue(loc, f"与确认卡 #{c.id} 矛盾：已选 {c.choice}，正文写的是未选的 {'、'.join(hit)}",
                                     "按确认卡的结论改写；结论确实要变，重新发确认卡"))
    facts = db.scalars(select(SurveyAnswer).where(
        SurveyAnswer.project_id == project.id, SurveyAnswer.kind == "closed",
        SurveyAnswer.fact_status == "confirmed").order_by(SurveyAnswer.qid)).all()
    if facts:
        qs = {q.qid: q for q in db.scalars(select(SurveyQuestion).where(
            SurveyQuestion.qid.in_({f.qid for f in facts}))).all()}
        for f in facts:
            q = qs.get(f.qid)
            opts = {o.get("key", ""): o.get("label", "") for o in (q.options if q else []) or []}
            if f.value not in opts:
                continue
            others = [(k, t) for k, t in opts.items() if k != f.value]
            for _, loc, body in chapters:
                hit = _contradicts(body, opts[f.value], others)
                if hit:
                    issues.append(_issue(loc, f"与事实确认单 {f.qid} 矛盾：已确认 {f.value}，正文写的是 {'、'.join(hit)}",
                                         "按已确认的事实改写；事实有变，先在事实确认单里重新确认"))
    return issues


# ---- ② 误报率度量与自动校准 ----

def false_positive_stats(db: Session) -> list[dict]:
    """各“提出方 + 类别”的阻断条数、被驳回条数和驳回率（全平台，只读台账，不含原文）。"""
    rows = db.execute(select(ReviewIssue.by, ReviewIssue.category, func.count(),
                             func.sum(case((ReviewIssue.status == "waived", 1), else_=0)))
                      .where(ReviewIssue.category != "").group_by(ReviewIssue.by, ReviewIssue.category)).all()
    out = []
    for by, cat, total, waived in rows:
        waived = int(waived or 0)
        rate = waived / total if total else 0.0
        out.append({"by": by, "category": cat, "total": int(total), "waived": waived, "rate": round(rate, 3),
                    "calibrated": cat not in PROTECTED and total >= CALIBRATE_MIN and rate >= CALIBRATE_RATE})
    return sorted(out, key=lambda x: (x["by"], x["category"]))


def calibrated_keys(db: Session) -> set[tuple[str, str]]:
    return {(x["by"], x["category"]) for x in false_positive_stats(db) if x["calibrated"]}


def apply_calibration(issues: list[dict], counts: dict[str, int], keys: set[tuple[str, str]]) -> int:
    """误报偏高的类别：阻断降为重要并标 calibrated；评审 Agent 的同步改 counts（平台规则的计数按问题清单重算）。"""
    n = 0
    for i in issues:
        cat = i.get("category", "")
        if i.get("severity") != "阻断" or cat in PROTECTED or (i.get("by", ""), cat) not in keys:
            continue
        if i.get("by") == "rule" and i.get("rule") != RULE:
            continue                       # 平台硬规则不驳回、不校准
        i["severity"], i["calibrated"] = "重要", True
        if i.get("by") == "agent":
            counts["阻断"] = max(counts.get("阻断", 0) - 1, 0)
            counts["重要"] = counts.get("重要", 0) + 1
        n += 1
    return n
