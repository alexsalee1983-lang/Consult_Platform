"""三期 B25②：知识缺口池的持久化、每周计划任务、关闭率（表 kb_gaps，知识区迁移 k0019）。

合并、打分、排计划的规则在 app/kb/acquisition.py（B25①，纯逻辑）；本模块只负责落表：
- record：信号按 gap_key 并入未关闭的缺口，同一事件编号只算一次；已关闭的缺口又来新信号就重新打开。
- first_collect：立即全量（kb-acquire-now），只给从没登记过缺口的正式来源记首采信号，再放大 cap 排一次计划。
- patrol：每周任务先给每个正式来源登记一条定时巡检信号（事件编号按 ISO 周，同周不重复，采集关闭后下周重开）。
- run_week：每周后台任务（worker 每周排一次 kb.gap_week），白名单取 B16 正式来源的网址前缀，
  结果写回每个缺口的 plan / score / planned_at；审计只记各路数量。
- close / stats：关闭原因固定四类；关闭率与平均关闭时长供 F2 看板。
- confirm_queue / decide_confirm：知识审批队列“来源确认”类。本周落在 confirm 的缺口按网址主机分组（编号类一组）；
  知识管理员批量确认或不做。确认可顺带登记一个候选来源（必须覆盖这批网址），缺口标 confirmed，
  仍要等来源由另一人批准转正式、进白名单，下周任务才把它排进 direct 出下发包；不做即关闭（dropped）。
- dispatch_package / import_return：本周“直接采集”的缺口出下发包（只有网址与规则）；采集机回传批次先 verify_return，
  通过才关缺口（collected）并交 kb-source-publish；审计只记数量和问题类别，不记文件名。
- mark_manual / manual_queue（四期 B25）：采集机取不到的网址（回传 manifest 的 failed）转“需人工下载”（plan manual，
  记原因类别）；资料仓 backlog 的老条目用 kb-gap-manual 登记。人工上传原件的处理在 app/kb/gap_manual.py。
- 四期 B41：缺口键是编号时，source_url 记要取的正文网址（来源推荐 Agent 或栏目页解析给出），计划、下发、回传都按
  target()（source_url 优先，否则 gap_key）；获取失败（采集机取不到、人工上传核对不过）计 attempts，满 MAX_ATTEMPTS 次
  标“暂无可用来源”（plan unavailable），每月自动再试（app/kb/gap_loop.py retry_unavailable）。
  回传批次里的栏目页由 gap_loop.expand_listings 解析出白名单内的正文链接，登记为下一轮直接采集的缺口，栏目页本身不入库。
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
MANUAL = "manual"                # 需人工下载：采集机取不到，等人上传原件
UPLOADED = "uploaded"            # 原件已上传、后台核对入库中
UNAVAILABLE = "unavailable"      # 暂无可用来源：获取失败满 MAX_ATTEMPTS 次，每月自动再试（四期 B41）
HELD = (MANUAL, UPLOADED, UNAVAILABLE)   # 每周计划不再重排这几类
MAX_ATTEMPTS = 3
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


def target(row: KbGap) -> str:
    """要去取的网址：来源推荐或栏目页解析给的正文网址优先，否则缺口键本身（四期 B41）。"""
    return row.source_url or row.gap_key


def _open_by_url(db: Session, url: str) -> list[KbGap]:
    return list(db.scalars(select(KbGap).where(KbGap.status == "open",
                                               (KbGap.gap_key == url) | (KbGap.source_url == url))
                           .order_by(KbGap.id)))


def whitelist(db: Session) -> tuple[str, ...]:
    return tuple(db.scalars(select(KbSource.url).where(KbSource.status == "active").order_by(KbSource.url)))


def run_week(db: Session, today: date, *, thresholds: dict[str, int] | None = None, cap: int = DEFAULT_CAP,
             actor: str = "platform") -> dict[str, int]:
    wl = whitelist(db)
    rows = list(db.scalars(select(KbGap).where(KbGap.status == "open").order_by(KbGap.id)))
    # 来源确认过的缺口不再回队列：来源转正式（进白名单）后直接采集，之前一直等着
    held = [r for r in rows if r.plan == CONFIRMED and not acq.in_whitelist(target(r), wl)]
    # 来源转正式后的确认缺口、带白名单内正文网址的缺口（B41 来源推荐 / 栏目页解析）直接采集
    promoted = {r.gap_key for r in rows if r.plan not in HELD and (r.plan == CONFIRMED or r.source_url)
                and acq.in_whitelist(target(r), wl)}
    manual = [r for r in rows if r.plan in HELD]
    rows = [r for r in rows if r not in held and r.plan not in HELD]
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
    if manual:
        out[MANUAL] = len(manual)
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
    urls: list[str] = []
    for r in db.scalars(select(KbGap).where(KbGap.status == "open", KbGap.plan == "direct")
                        .order_by(KbGap.score.desc(), KbGap.id)):
        u = target(r)
        if acq.in_whitelist(u, wl) and u not in urls:
            urls.append(u)
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
    manifest = json.loads((batch / acq.RETURN_MANIFEST).read_text(encoding="utf-8"))
    rows = manifest["files"]
    from . import gap_loop                      # 四期 B41：栏目页两级取正文（避免循环导入）

    listing = gap_loop.listing_files(batch, rows, whitelist(db))
    closed = 0
    for url in sorted({str(r["url"]) for r in rows if str(r.get("file", "")) not in listing}):
        for g in _open_by_url(db, url):
            close(db, g.gap_key, "collected", actor=actor)
            closed += 1
    failed = manifest.get("failed") if isinstance(manifest, dict) else None
    for f in failed if isinstance(failed, list) else []:
        url = str(f.get("url", "")) if isinstance(f, dict) else ""
        for row in _open_by_url(db, url) if url else []:
            if row.plan in HELD:
                continue
            reason = acq.manual_reason(str(f.get("reason", "")))
            if note_failure(db, row, actor=actor) == UNAVAILABLE:
                report.unavailable += 1
            else:
                mark_manual(db, row.gap_key, reason, actor=actor)
                report.manual += 1
    report.listing = sorted(listing)
    report.next_round = gap_loop.expand_listings(db, batch, rows, listing, whitelist(db), actor=actor)
    extra = {k: v for k, v in (("manual", report.manual), ("unavailable", report.unavailable),
                               ("listing", len(report.listing)), ("next_round", report.next_round)) if v}
    audit.record(db, actor=actor, action="kb.gap.return", result="allowed", files=report.count, closed=closed, **extra)
    return report, closed


def note_failure(db: Session, row: KbGap, *, actor: str = "platform") -> str:
    """获取失败一次（四期 B41）：attempts 加一，满 MAX_ATTEMPTS 次标暂无可用来源；返回缺口的新 plan（未满时不改）。
    来源推荐给的网址失败了就清掉，下次重新推荐。"""
    row.attempts = (row.attempts or 0) + 1
    if row.source_via == "agent":
        row.source_url = ""
    row.updated_at = utcnow()
    if row.attempts >= MAX_ATTEMPTS:
        row.plan, row.unavailable_at = UNAVAILABLE, row.updated_at
    db.commit()
    if row.plan == UNAVAILABLE:
        audit.record(db, actor=actor, action="kb.gap.unavailable", result="allowed", gap_id=row.id,
                     attempts=row.attempts)
    return row.plan


def mark_manual(db: Session, key: str, reason: str, *, create: bool = False, event_ref: str = "",
                actor: str = "platform") -> KbGap:
    """缺口转“需人工下载”。create：没有未关闭的缺口就按业务需求信号新建（资料仓 backlog 老条目用）。"""
    if reason not in acq.MANUAL_REASONS:
        raise ValueError(f"原因只能是：{'、'.join(acq.MANUAL_REASONS)}")
    row = db.scalar(select(KbGap).where(KbGap.gap_key == key, KbGap.status == "open"))
    if row is None:
        if not create:
            raise LookupError("没有这个未关闭的缺口")
        record(db, [acq.Signal(key, "business", event_ref or f"manual-{utcnow():%Y%m%d}", impact=3)])
        row = db.scalar(select(KbGap).where(KbGap.gap_key == key, KbGap.status == "open"))
        assert row is not None
    row.plan, row.manual_reason, row.updated_at = MANUAL, reason, utcnow()
    db.commit()
    audit.record(db, actor=actor, action="kb.gap.manual", result="allowed", reason=reason, gap_id=row.id)
    return row


def manual_queue(db: Session) -> list[dict[str, Any]]:
    """需人工下载清单（含已上传处理中的），分数高的在前；in_whitelist：网址在正式来源里，上传后能直接入库。"""
    wl = whitelist(db)
    rows = db.scalars(select(KbGap).where(KbGap.status == "open", KbGap.plan.in_(HELD))
                      .order_by(KbGap.score.desc(), KbGap.id))
    out = []
    for r in rows:
        is_url = urlsplit(r.gap_key).scheme in ("http", "https")
        out.append({"id": r.id, "gap_key": r.gap_key, "plan": r.plan, "reason": r.manual_reason,
                    "reason_label": acq.MANUAL_REASONS.get(r.manual_reason, ""), "is_url": is_url,
                    "need": r.need, "attempts": r.attempts, "unavailable": r.plan == UNAVAILABLE,
                    "in_whitelist": is_url and acq.in_whitelist(r.gap_key, wl), "impact": r.impact,
                    "authority": r.authority, "score": r.score, "opened_at": r.opened_at, "updated_at": r.updated_at})
    return out



PUBLISH_COLUMNS = ["文件名", "名称", "发布机关", "原网址", "现行状态", "获取方式", "sha256"]


def publish_manifest(db: Session, batch: Path, skip: frozenset[str] | set[str] = frozenset()) -> tuple[str, list[str]]:
    """回传批次 → 下载清单 CSV（供 kb-source-publish）：名称、发布机关取来源登记表里网址前缀最长匹配的正式来源。

    找不到登记来源的文件不进清单，返回文件名列表，由人登记来源后再导；现行状态先记“现行”，生命周期由 B26 跟踪。
    """
    import csv
    import io

    rows = json.loads((batch / acq.RETURN_MANIFEST).read_text(encoding="utf-8"))["files"]
    srcs = list(db.scalars(select(KbSource).where(KbSource.status == "active")))  # 与 whitelist() 同口径
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=PUBLISH_COLUMNS, lineterminator=chr(10))
    w.writeheader()
    unmatched: list[str] = []
    for r in rows:
        if str(r.get("file", "")) in skip:             # 栏目页（B41）不入库，只用来出下一轮下发包
            continue
        url = str(r.get("url", ""))
        hits = [s for s in srcs if acq.in_whitelist(url, (s.url,))]
        if not hits:
            unmatched.append(str(r.get("file", "")))
            continue
        s = max(hits, key=lambda x: len(x.url))
        w.writerow({"文件名": r["file"], "名称": s.name, "发布机关": s.publisher, "原网址": url,
                    "现行状态": "现行", "获取方式": "已下载", "sha256": str(r.get("sha256", "")).lower()})
    return out.getvalue(), unmatched

def patrol(db: Session, today: date) -> int:
    """正式来源定时巡检：白名单内的定时巡检缺口由 run_week 排进 direct、出下发包。"""
    y, w, _ = today.isocalendar()
    ref = f"patrol-{y}W{w:02d}"
    srcs = db.scalars(select(KbSource).where(KbSource.status == "active").order_by(KbSource.id)).all()
    if srcs:
        record(db, [acq.Signal(s.url, "schedule", ref, authority=s.authority_level or 3) for s in srcs])
    return len(srcs)


def first_collect(db: Session, today: date, *, cap: int = 2000, actor: str = "cli") -> dict[str, int]:
    """立即全量（负责人 2026-11-27）：从没登记过缺口的正式来源记一条首采信号，再按放大的 cap 排一次计划。
    已采过、已关闭的缺口不重开；之前因每周上限顺延的未关闭缺口一并排进来。"""
    seen = set(db.scalars(select(KbGap.gap_key)))
    srcs = [s for s in db.scalars(select(KbSource).where(KbSource.status == "active").order_by(KbSource.id))
            if s.url not in seen]
    if srcs:
        ref = f"first-{today:%Y%m%d}"
        record(db, [acq.Signal(s.url, "schedule", ref, authority=s.authority_level or 3) for s in srcs])
    return {"new": len(srcs), **run_week(db, today, cap=cap, actor=actor)}


@jobs.handler(JOB_KIND)
def _job(payload: dict, ctx: jobs.JobContext | None) -> None:
    from ..core.db import SessionLocal

    today = date.fromisoformat(payload["today"]) if payload.get("today") else utcnow().date()
    from . import gap_loop

    with SessionLocal() as db:
        gap_loop.feed(db, today)                # 四期 B41：就绪度缺格、引用缺失、法规换版、依据到期 → 缺口；已补齐的关闭
        gap_loop.retry_unavailable(db, today)   # 暂无可用来源满一个月再试
        patrol(db, today)
        run_week(db, today, cap=int(payload.get("cap", DEFAULT_CAP)))
        gap_loop.recommend_week(db)             # 来源推荐 Agent（经网关；没接模型或命中率低于门槛时不调）
