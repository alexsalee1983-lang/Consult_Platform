"""就绪度看板（三期 F2②）：章节 × 槽位就绪度，按条目类型的专家分钟 / 条、采纳率、修改率。

只读、不调模型、不写审计、不建表（主链 0046 空号）；只返回章节编号与标题、知识 key、条目类型和计数，不含正文。

章节 × 槽位：章节和每章应命中知识取 A24 的章节级装配评测集（`app/kb/assembly_eval.py` 格式，
配置项 KB_READINESS_EVALSETS），现阶段一条应命中知识算一个槽位；C10 槽位规格（类型、所需条数、控制项）出来后
按规格换口径，看板结构不变。槽位状态：
- ready 就绪：已发布（有现行版本）、未停用、现行版本不在待复查（B13 使用反馈标记；B29 的“不对 / 过时”接入后同样不算）；
- candidate 候选：库里有草稿或待审版本，还没发布（就绪度口径不含候选）；
- recheck 待复查（reason=feedback：顾问标“过时 / 不对”引起，四期 A17）；retired 已下线；missing 缺槽：库里没有这条。
章节就绪度 = 就绪槽位 ÷ 槽位数；评测集就绪度 = 各章平均，另列最低一章；依据类（L4）槽位单列（v3.0 第 9 条要求 ≥ 95%）。

专家指标（按条目类型 doc_type，统计窗内的人工内容审核 review_role=content）：
- 采纳率 = 通过 ÷ 审核次数；
- 专家分钟 / 条、修改率：取审核记录 findings 里的 minutes（本条耗时，分钟）和 edited（采纳前是否改过），
  由 B17 公司做法确认队列写入；之前没有记录的返回 None（界面显示“暂无”）。
- 修改率长期接近 0 提醒（防“顺手点采纳”，三期计划第三节红线 ②）：窗内有修改记录的采纳 ≥ EDIT_MIN_REVIEWS 条
  且修改率 ≤ EDIT_LOW_RATE 时提醒抽查已采纳条目。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import KbItem, KbReview, KbVersion
from . import assembly_eval, feedback

EDIT_MIN_REVIEWS = 20       # 有修改记录的采纳至少这么多条才判断，免得样本太少误报
EDIT_LOW_RATE = 0.05        # 修改率不高于 5% 算“接近 0”
DEFAULT_EVALSET = Path("knowledge") / "eval" / "assembly_samples.yaml"
STATES = ("ready", "candidate", "recheck", "retired", "missing")
RECHECK = ("pending", "failed")


def _ratio(num: int | float, den: int) -> float | None:
    return round(num / den, 4) if den else None


def evalset_paths() -> list[Path]:
    raw = get_settings().kb_readiness_evalsets.strip()
    if not raw:
        from ..products.config import catalog_dir

        return [catalog_dir() / DEFAULT_EVALSET]
    out: list[Path] = []
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        path = Path(part)
        out.extend(sorted(path.glob("*.yaml")) if path.is_dir() else [path])
    return out


def _slot_states(db: Session, keys: set[str]) -> dict[str, dict[str, str]]:
    if not keys:
        return {}
    items = {i.import_key: i for i in db.scalars(select(KbItem).where(KbItem.import_key.in_(keys)))}
    pending = set(db.scalars(select(KbVersion.item_id).where(
        KbVersion.item_id.in_([i.id for i in items.values()]), KbVersion.status.in_(("draft", "in_review")))))
    current = {v.id: v for v in db.scalars(select(KbVersion).where(
        KbVersion.id.in_([i.current_version_id for i in items.values() if i.current_version_id])))}
    out: dict[str, dict[str, str]] = {}
    for key in keys:
        item = items.get(key)
        if item is None:
            out[key] = {"state": "missing", "doc_type": "", "layer": "", "reason": ""}
            continue
        v = current.get(item.current_version_id or 0)
        if item.retired:
            state = "retired"
        elif v is None:
            state = "candidate" if item.id in pending else "missing"
        elif v.recheck_state in RECHECK:
            state = "recheck"
        else:
            state = "ready"
        reason = "feedback" if state == "recheck" and feedback.is_feedback_recheck(v) else ""
        out[key] = {"state": state, "doc_type": item.doc_type, "layer": item.layer, "reason": reason}
    return out


def _evalset_matrix(db: Session, data: dict[str, Any], name: str) -> dict[str, Any]:
    keys = {str(k) for ch in data["chapters"] for k in ch["expect"]}
    states = _slot_states(db, keys)
    chapters = []
    basis_total = basis_ready = 0
    for ch in data["chapters"]:
        slots = [{"key": str(k), **states[str(k)]} for k in ch["expect"]]
        ready = sum(s["state"] == "ready" for s in slots)
        for s in slots:
            if s["layer"] == "L4":
                basis_total += 1
                basis_ready += s["state"] == "ready"
        chapters.append({"id": str(ch["id"]), "title": str(ch["title"]), "step": str(ch["step"]),
                         "slots": slots, "ready": ready, "readiness": _ratio(ready, len(slots)) or 0.0,
                         "missing": [s["key"] for s in slots if s["state"] != "ready"]})
    low = min(chapters, key=lambda c: c["readiness"])
    return {"file": name, "product": data["product"], "chapters": chapters,
            "readiness": round(sum(c["readiness"] for c in chapters) / len(chapters), 4),
            "lowest": {"id": low["id"], "title": low["title"], "readiness": low["readiness"]},
            "basis_readiness": _ratio(basis_ready, basis_total)}


def matrix(db: Session, paths: list[Path] | None = None) -> dict[str, Any]:
    """章节 × 槽位。评测集读不了或格式不对的列进 errors（只给文件名），不影响其他评测集。"""
    evalsets: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in paths if paths is not None else evalset_paths():
        try:
            data = assembly_eval.load(path)
        except assembly_eval.AssemblyEvalError as e:
            errors.append({"file": path.name, "error": str(e)})
            continue
        evalsets.append(_evalset_matrix(db, data, path.name))
    chapters = [c for es in evalsets for c in es["chapters"]]
    return {"evalsets": evalsets, "errors": errors,
            "readiness": round(sum(c["readiness"] for c in chapters) / len(chapters), 4) if chapters else None}


def expert_metrics(db: Session, since: datetime, until: datetime) -> dict[str, Any]:
    rows = db.execute(
        select(KbItem.doc_type, KbReview.decision, KbReview.findings)
        .join(KbVersion, KbReview.version_id == KbVersion.id).join(KbItem, KbVersion.item_id == KbItem.id)
        .where(KbReview.review_role == "content", KbReview.created_at >= since, KbReview.created_at < until)).all()
    acc: dict[str, dict[str, Any]] = {}
    for doc_type, decision, findings in rows:
        a = acc.setdefault(doc_type, {"reviews": 0, "approved": 0, "minutes": [], "edited": []})
        a["reviews"] += 1
        f = findings if isinstance(findings, dict) else {}
        if isinstance(f.get("minutes"), int | float) and not isinstance(f.get("minutes"), bool):
            a["minutes"].append(float(f["minutes"]))
        if decision == "approve":
            a["approved"] += 1
            if isinstance(f.get("edited"), bool):
                a["edited"].append(f["edited"])
    by_type, alerts = [], []
    for doc_type in sorted(acc):
        a = acc[doc_type]
        edit_rate = _ratio(sum(a["edited"]), len(a["edited"]))
        low = len(a["edited"]) >= EDIT_MIN_REVIEWS and edit_rate is not None and edit_rate <= EDIT_LOW_RATE
        by_type.append({"doc_type": doc_type, "reviews": a["reviews"], "approved": a["approved"],
                        "adopt_rate": _ratio(a["approved"], a["reviews"]),
                        "minutes_per_item": round(sum(a["minutes"]) / len(a["minutes"]), 1) if a["minutes"] else None,
                        "edit_rate": edit_rate, "edit_samples": len(a["edited"]), "low_edit": low})
        if low:
            alerts.append(f"{doc_type}：近期 {len(a['edited'])} 条采纳的修改率 {edit_rate:.0%}，接近 0，"
                          "可能在顺手点采纳，建议抽查已采纳条目")
    return {"by_type": by_type, "alerts": alerts,
            "edit_min_reviews": EDIT_MIN_REVIEWS, "edit_low_rate": EDIT_LOW_RATE}
