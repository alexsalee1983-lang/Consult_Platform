"""三期 B25②：知识缺口池的持久化、每周计划任务、关闭率（表 kb_gaps，知识区迁移 k0019）。

合并、打分、排计划的规则在 app/kb/acquisition.py（B25①，纯逻辑）；本模块只负责落表：
- record：信号按 gap_key 并入未关闭的缺口，同一事件编号只算一次；已关闭的缺口又来新信号就重新打开。
- run_week：每周后台任务（worker 每周排一次 kb.gap_week），白名单取 B16 正式来源的网址前缀，
  结果写回每个缺口的 plan / score / planned_at；审计只记各路数量。
- close / stats：关闭原因固定四类；关闭率与平均关闭时长供 F2 看板。
- confirm_queue / decide_confirm：知识审批队列“来源确认”类。本周落在 confirm 的缺口按网址主机分组（编号类一组）；
  知识管理员批量确认或不做。确认可顺带登记一个候选来源（必须覆盖这批网址），缺口标 confirmed，
  仍要等来源由另一人批准转正式、进白名单，下周任务才把它排进 direct 出下发包；不做即关闭（dropped）。
- dispatch_package / import_return：本周“直接采集”的缺口出下发包（只有网址与规则）；采集机回传批次先 verify_return，
  通过才关缺口（collected）并交 kb-source-publish；审计只记数量和问题类别，不记文件名。
不存原文：gap_key 是编号或网址，events 只是事件编号（acquisition.EVENT_REF 校验）。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import KNOWLEDGE_ADMIN, roles_of
from ..models import KbGap, KbSource, User
from ..services import jobs
from . import acquisition as acq
from . import source_registry as reg

JOB_KIND = "kb.gap_week"
CLOSE_REASONS = {"published": "已入库", "collected": "已采集", "dropped": "不做", "duplicate": "重复"}
DEFAULT_CAP = 20
CONFIRMED = "confirmed"          # 来源确认已通过、等来源转正式
DECISIONS = ("approve", "reject")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def record(db: Session, signals: list[acq.Signal]) -> list[KbGap]:
    merged = acq.merge_signals(signals)          # 校验触发源与事件编号
    rows: list[KbGap] = []
    now = utcnow()
    for g in merged:
        row = db.scalar(select(KbGap).where(KbGap.gap_key == g.key))
        if row is None:
            row = KbGap(gap_key=g.key, kinds={}, events=[], impact=g.impact, due=g.due, authority=g.authority,
                        opened_at=now, updated_at=now)
            db.add(row)
        new_events = [e for e in g.events if e not in (row.events or [])]
        if not new_events:
            rows.append(row)
            continue
        if row.status == "closed":
            row.status, row.closed_at, row.close_reason, row.plan = "open", None, "", ""
            row.reopened = (row.reopened or 0) + 1
            row.opened_at = now
        kinds = dict(row.kinds or {})
        for s in signals:
            if s.key == g.key and s.event_ref in new_events:
                kinds[s.kind] = kinds.get(s.kind, 0) + 1
        row.kinds = kinds
        row.events = [*(row.events or []), *new_events]
        row.impact = max(row.impact or 0, g.impact)
        if g.due and (row.due is None or g.due < row.due):
            row.due = g.due
        row.authority = min(row.authority or 3, g.authority)
        row.updated_at = now
        rows.append(row)
    db.commit()
    return rows


def whitelist(db: Session) -> tuple[str, ...]:
    return tuple(db.scalars(select(KbSource.url).where(KbSource.status == "active").order_by(KbSource.url)))


def run_week(db: Session, today: date, *, thresholds: dict[str, int] | None = None, cap: int = DEFAULT_CAP,
             actor: str = "platform") -> dict[str, int]:
    wl = whitelist(db)
    rows = list(db.scalars(select(KbGap).where(KbGap.status == "open").order_by(KbGap.id)))
    # 来源确认过的缺口不再回队列：来源转正式（进白名单）后直接采集，之前一直等着
    held = [r for r in rows if r.plan == CONFIRMED and not acq.in_whitelist(r.gap_key, wl)]
    promoted = {r.gap_key for r in rows if r.plan == CONFIRMED and acq.in_whitelist(r.gap_key, wl)}
    rows = [r for r in rows if r not in held]
    gaps = {r.gap_key: acq.Gap(key=r.gap_key, kinds=dict(r.kinds or {}), events=list(r.events or []),
                               impact=r.impact, due=r.due, authority=r.authority) for r in rows}
    plan = acq.plan_week([g for k, g in gaps.items() if k not in promoted], today, thresholds=thresholds or {},
                         whitelist=wl, cap=max(cap - len(promoted), 0))
    now = utcnow()
    lanes = {"direct": [gaps[k] for k in sorted(promoted)] + plan.direct, "confirm": plan.confirm,
             "deferred": plan.deferred, "waiting": plan.waiting}
    lane_of = {g.key: lane for lane, gs in lanes.items() for g in gs}
    for r in rows:
        r.plan = lane_of.get(r.gap_key, "")
        r.score = acq.score(gaps[r.gap_key], today)
        r.planned_at = now
        r.updated_at = now
    db.commit()
    out = {lane: len(gs) for lane, gs in lanes.items()}
    if held:
        out[CONFIRMED] = len(held)
    audit.record(db, actor=actor, action="kb.gap.week", result="allowed", today=today.isoformat(), **out)
    return out


def close(db: Session, key: str, reason: str, *, now: datetime | None = None, actor: str = "platform") -> KbGap:
    if reason not in CLOSE_REASONS:
        raise ValueError(f"关闭原因只能是：{'、'.join(CLOSE_REASONS)}")
    row = db.scalar(select(KbGap).where(KbGap.gap_key == key, KbGap.status == "open"))
    if row is None:
        raise LookupError("没有这个未关闭的缺口")
    row.status, row.close_reason, row.plan = "closed", reason, ""
    row.closed_at = now or utcnow()
    row.updated_at = row.closed_at
    db.commit()
    audit.record(db, actor=actor, action="kb.gap.close", result="allowed", reason=reason, gap_id=row.id)
    return row


def stats(db: Session) -> dict:
    """关闭率 = 已关闭 / 全部；平均关闭时长按天，只算已关闭的。"""
    rows = list(db.scalars(select(KbGap)))
    closed = [r for r in rows if r.status == "closed" and r.closed_at is not None]
    days = [(_aware(r.closed_at) - _aware(r.opened_at)).total_seconds() / 86400 for r in closed]  # type: ignore[operator]
    by_reason: dict[str, int] = {}
    for r in closed:
        by_reason[r.close_reason] = by_reason.get(r.close_reason, 0) + 1
    return {"total": len(rows), "open": len(rows) - len(closed), "closed": len(closed),
            "close_rate": round(len(closed) / len(rows), 4) if rows else 0.0,
            "avg_close_days": round(sum(days) / len(days), 2) if days else 0.0, "by_reason": by_reason}


def confirm_queue(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(select(KbGap).where(KbGap.status == "open", KbGap.plan == "confirm")
                      .order_by(KbGap.score.desc(), KbGap.id))
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        u = urlsplit(r.gap_key)
        group = (u.hostname or "").lower() if u.scheme in ("http", "https") and u.hostname else "编号"
        groups.setdefault(group, []).append({
            "id": r.id, "gap_key": r.gap_key, "kinds": dict(r.kinds or {}), "impact": r.impact, "due": r.due,
            "authority": r.authority, "score": r.score, "opened_at": r.opened_at})
    return [{"group": g, "items": items} for g, items in groups.items()]


def decide_confirm(db: Session, user: User, gap_ids: list[int], decision: str,
                   source: dict[str, str] | None = None) -> dict[str, Any]:
    """只限知识管理员。approve：缺口标 confirmed，带 source 则登记候选来源；reject：关闭（dropped）。"""
    if KNOWLEDGE_ADMIN not in roles_of(db, user.id):
        raise reg.SourceError(403, "只有知识管理员能确认来源")
    if decision not in DECISIONS:
        raise reg.SourceError(422, "决定只能是 approve 或 reject")
    ids = sorted(set(gap_ids))
    rows = list(db.scalars(select(KbGap).where(KbGap.id.in_(ids), KbGap.status == "open", KbGap.plan == "confirm")))
    if not ids or len(rows) != len(ids):
        raise reg.SourceError(422, "有缺口不在来源确认队列里")
    code = None
    if decision == "approve" and source:
        prefix = str(source.get("url", "")).strip()
        if not all(acq.in_whitelist(r.gap_key, (prefix,)) for r in rows):
            raise reg.SourceError(422, "登记的来源地址必须覆盖这批缺口的网址")
        code = reg.create(db, user, **source).code
    now = utcnow()
    for r in rows:
        if decision == "approve":
            r.plan, r.updated_at = CONFIRMED, now
        else:
            r.status, r.close_reason, r.plan, r.closed_at, r.updated_at = "closed", "dropped", "", now, now
    db.commit()
    audit.record(db, actor=user.username, action="kb.gap.confirm", result="allowed", decision=decision,
                 count=len(rows), source=code or "")
    return {"decided": len(rows), "source": code}


def dispatch_package(db: Session, rule: dict[str, Any] | None = None, *, actor: str = "cli") -> dict[str, Any]:
    """本周计划为 direct（白名单内外部变化）的未关闭缺口出下发包；白名单外的 build_dispatch 直接拒绝。"""
    wl = whitelist(db)
    urls = [k for k in db.scalars(select(KbGap.gap_key).where(KbGap.status == "open", KbGap.plan == "direct")
                                  .order_by(KbGap.score.desc(), KbGap.id)) if acq.in_whitelist(k, wl)]
    pkg = acq.build_dispatch(urls, wl, rule)
    audit.record(db, actor=actor, action="kb.gap.dispatch", result="allowed", tasks=len(pkg["tasks"]))
    return pkg


def import_return(db: Session, batch: Path, *, actor: str = "cli") -> tuple[acq.ReturnReport, int]:
    """回传批次核对；通过的按 manifest 里的网址关闭对应缺口（collected）。返回（核对结果，关闭数）。"""
    report = acq.verify_return(batch, whitelist(db))
    if not report.ok:
        cats: dict[str, int] = {}
        for p in report.problems:
            c = p.split(":", 1)[0]
            cats[c] = cats.get(c, 0) + 1
        audit.record(db, actor=actor, action="kb.gap.return", result="blocked", problems=cats)
        return report, 0
    rows = json.loads((batch / acq.RETURN_MANIFEST).read_text(encoding="utf-8"))["files"]
    closed = 0
    for url in sorted({str(r["url"]) for r in rows}):
        try:
            close(db, url, "collected", actor=actor)
            closed += 1
        except LookupError:
            pass
    audit.record(db, actor=actor, action="kb.gap.return", result="allowed", files=report.count, closed=closed)
    return report, closed


@jobs.handler(JOB_KIND)
def _job(payload: dict, ctx: jobs.JobContext | None) -> None:
    from ..core.db import SessionLocal

    today = date.fromisoformat(payload["today"]) if payload.get("today") else utcnow().date()
    with SessionLocal() as db:
        run_week(db, today, cap=int(payload.get("cap", DEFAULT_CAP)))
