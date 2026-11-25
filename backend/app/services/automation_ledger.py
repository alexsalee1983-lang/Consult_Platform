"""四期 F5：自动化账本与自适应抽检。全部从审计记录算，不建表、不调模型、不写审计（驾驶舱改版方案第七节）。

口径：
- 自动：动作名最后一段按下划线拆开含 auto（survey.auto_confirm、kb.auto_publish、project.phase_auto……），
  一条记录带 answer_ids 时按个数计，否则计 1。账本条目给出谁（actor）、什么规则（action 与中文名）、依据来源（审计明细，
  审计本来就不记客户原文，这里再去掉 ip）。
- 人工：MANUAL 里列的“人动手”动作，结果为 ok / allowed；命令行和后台账号（SYSTEM_ACTORS）不算。
- 抽检差错：survey.spot_check 结果为 failed；抽检数为全部 survey.spot_check。
- 自适应抽检（adaptive_spot_rate）：按该类最近的抽检结论调比例——最近一次差错之后连续通过不到 RECOVER 条，比例 1.0
  （每条自动确认都进抽检，即退回人工）；窗口内无差错且连续通过不少于 RELAX 条，放开到 max(MIN_RATE, base/2)；其余用 base。
- 三条验收（第七节）：① SOC 项目人工动手 ≤15 次（取窗内有活动的 SOC 项目里最多的一个，项目整个生命周期累计）；
  ② 事实自动确认率 ≥80% 且抽检差错率 ≤2%；③ 每个项目每天人工处理 ≤5 件（取窗内项目·日的最大值）。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Project

MANUAL = frozenset({
    "confirmation.resolve", "deliverable.sign", "deliverable.return", "quality.waive", "tasks.return",
    "survey.pack", "survey.results", "survey.confirm", "survey.spot_check", "project.note",
})
SYSTEM_ACTORS = frozenset({"cli", "system", "worker", "scheduler"})
OK_RESULTS = ("ok", "allowed")
SPOT_ACTION = "survey.spot_check"
FACT_AUTO, FACT_MANUAL = "survey.auto_confirm", "survey.confirm"
RULES = {"survey.auto_confirm": "事实自动确认", "kb.auto_publish": "知识自动发布", "project.phase_auto": "阶段自动推进"}

WINDOW, RECOVER, RELAX, MIN_RATE = 50, 20, 100, 0.02
SOC_MANUAL_MAX, FACT_AUTO_MIN, SPOT_ERROR_MAX, DAILY_MANUAL_MAX = 15, 0.8, 0.02, 5


def is_auto(action: str) -> bool:
    return "auto" in action.rsplit(".", 1)[-1].split("_")


def is_manual(row: AuditLog) -> bool:
    return row.action in MANUAL and row.result in (*OK_RESULTS, "failed") and row.actor not in SYSTEM_ACTORS


def _detail(row: AuditLog) -> dict[str, Any]:
    try:
        d = json.loads(row.detail or "{}")
    except ValueError:
        return {}
    if not isinstance(d, dict):
        return {}
    d.pop("ip", None)
    return d


def _weight(row: AuditLog) -> int:
    ids = _detail(row).get("answer_ids")
    return len(ids) if isinstance(ids, list) else 1


def _rows(db: Session, start: datetime | None, end: datetime | None, codes: list[str] | None) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.id)
    if start is not None:
        stmt = stmt.where(AuditLog.at >= start)
    if end is not None:
        stmt = stmt.where(AuditLog.at < end)
    if codes is not None:
        stmt = stmt.where(AuditLog.project_code.in_(codes))
    return list(db.scalars(stmt))


def ledger(db: Session, start: datetime, end: datetime, codes: list[str] | None = None, limit: int = 50) -> dict[str, Any]:
    """驾驶舱页头“本周自动 N / 人工 M / 抽检差错 K”与自动条目（新的在前）。codes 为空列表时什么都不算。"""
    auto = manual = spot_checks = spot_errors = 0
    entries: list[dict[str, Any]] = []
    for r in _rows(db, start, end, codes):
        if is_auto(r.action) and r.result in OK_RESULTS:
            n = _weight(r)
            auto += n
            entries.append({"at": r.at.isoformat(), "actor": r.actor, "action": r.action,
                            "rule": RULES.get(r.action, r.action), "count": n, "project_code": r.project_code,
                            "basis": _detail(r)})
        elif is_manual(r):
            manual += 1
        if r.action == SPOT_ACTION:
            spot_checks += 1
            spot_errors += r.result == "failed"
    return {"auto": auto, "manual": manual, "spot_checks": spot_checks, "spot_errors": spot_errors,
            "entries": entries[::-1][:limit]}


def spot_state(verdicts: list[bool], base: float) -> dict[str, Any]:
    """verdicts 按时间先后，True = 抽检通过。返回比例与状态（tight 收紧退回人工 / relaxed 放开 / normal）。"""
    streak = 0
    for ok in reversed(verdicts):
        if not ok:
            break
        streak += 1
    failed = streak < len(verdicts)
    if failed and streak < RECOVER:
        return {"rate": 1.0, "state": "tight", "streak": streak}
    if not any(not ok for ok in verdicts[-WINDOW:]) and streak >= RELAX:
        return {"rate": round(max(MIN_RATE, base / 2), 4), "state": "relaxed", "streak": streak}
    return {"rate": base, "state": "normal", "streak": streak}


def adaptive_spot_rate(db: Session, base: float, action: str = SPOT_ACTION) -> dict[str, Any]:
    """该类（默认事实抽检）最近 RELAX 条抽检结论决定比例；E3 的 survey.spot_rate() 可直接返回这里的 rate。"""
    rows = db.scalars(select(AuditLog.result).where(AuditLog.action == action)
                      .order_by(AuditLog.id.desc()).limit(RELAX)).all()
    return spot_state([r != "failed" for r in reversed(rows)], base)


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _status(value: float | int | None, ok: bool) -> str:
    return "na" if value is None else ("ok" if ok else "breach")


def acceptance(db: Session, start: datetime, end: datetime, codes: list[str] | None = None) -> dict[str, Any]:
    """F2 看板的三条验收指标（只给数字，不给项目代号）。"""
    from .indicators_v4 import _product_of

    window = _rows(db, start, end, codes)
    active = {r.project_code for r in window if r.project_code}
    soc = [p.code for p in db.scalars(select(Project).where(Project.code.in_(active))) if _product_of(db, p) == "SOC"]
    life = Counter(r.project_code for r in _rows(db, None, end, soc) if is_manual(r)) if soc else Counter()
    soc_max = max((life[c] for c in soc), default=None)

    fact_auto = sum(_weight(r) for r in window if r.action == FACT_AUTO and r.result in OK_RESULTS)
    fact_manual = sum(1 for r in window if r.action == FACT_MANUAL and is_manual(r))
    spots = [r for r in window if r.action == SPOT_ACTION]
    auto_rate = _ratio(fact_auto, fact_auto + fact_manual)
    err_rate = _ratio(sum(r.result == "failed" for r in spots), len(spots))

    daily = Counter((r.project_code, r.at.date()) for r in window if r.project_code and is_manual(r))
    day_max = max(daily.values(), default=None)
    return {
        "soc_manual": {"label": "SOC 项目人工动手", "value": soc_max, "threshold": SOC_MANUAL_MAX, "projects": len(soc),
                       "status": _status(soc_max, soc_max is not None and soc_max <= SOC_MANUAL_MAX)},
        "fact_auto": {"label": "事实自动确认率", "value": auto_rate, "threshold": FACT_AUTO_MIN,
                      "auto": fact_auto, "manual": fact_manual,
                      "status": _status(auto_rate, auto_rate is not None and auto_rate >= FACT_AUTO_MIN)},
        "spot_error": {"label": "抽检差错率", "value": err_rate, "threshold": SPOT_ERROR_MAX, "checks": len(spots),
                       "status": _status(err_rate, err_rate is not None and err_rate <= SPOT_ERROR_MAX)},
        "daily_manual": {"label": "每天人工处理（项目·日最多）", "value": day_max, "threshold": DAILY_MANUAL_MAX,
                         "status": _status(day_max, day_max is not None and day_max <= DAILY_MANUAL_MAX)},
    }
