"""四期 B44：知识专家便利性（平台规划刷新设计第十三节第 1～4 项，负责人 11-27“都可以”；在 B23 专家工作台上扩展）。

目标：专家一到位，用最少时间消化积压（首批 54 份黄金样例）。审核规则全部沿用 review.review / review.publish，
不另立规则；只给知识专家（作者不能认可自己的）。

1. 对照审核 compare：条目里引用的“文件 + 条款号”经 citations.check 找到依据原文，条目所在段落与原文并排，
   按字比对切成 segments（same=False 即出入处，前端高亮）；知识审核 Agent 的意见（看法、把握、问题、拿不准的字段）
   并列。flags 列出要专家看的地方：依据找不到、依据已废止或被替代、与原文出入大、Agent 提的问题、Agent 拿不准的
   字段、Agent 存疑；没有 flags 的条目专家可以只扫一眼。
2. 修改后认可 revise：专家在线改完即认可。改动另建一版（items.create_expert_revision，专家版本），原稿改为已替代、
   留作历史；专家以内容审核通过（findings 记 expert_revision、revised_from、edited、minutes），随即经 review.publish
   发布，认可人即该专家。AI 基线草稿没有作者可接退回，所以必须能改完就认。改后正文过元数据校验和机密级检测；
   专家新写的文字有可识别信息时要先看去标识预览（同 B23②，deid_ack）。
3. 分组、按影响排序、成批认可 groups / batch_approve：本人可认可的待审版本按“产品 × 章节槽位”分组；
   影响 = 认可后知识健康（readiness.matrix 的总体就绪度）提升多少，组内、组间都按它从高到低排，同分按同槽位已发布
   条数从少到多；同组可一次认可（≤ 50 条，逐条走 review.review、各自留审计），有 flags 的跳过、请逐条看。
4. 按专长派卡 set_profile / assign_pending：专家档案写擅长的产品、行业；待审版本按专长派给一位专家（产品对上为 1 分，
   行业也对上为 2 分，取最高分里当前待审最少的），没有匹配的不派、发全体知识专家；档案撤了或不再是知识专家的
   收回重派。派给别人的卡不进本人“我的任务”（只加数据源过滤，收件箱框架不变）。

审计只记编号、版本、条数、耗时与修改幅度，不记正文。迁移 k0023（知识区链）。
"""
from __future__ import annotations

import difflib
import re
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import KNOWLEDGE_EXPERT
from ..models import KbExpertProfile, KbItem, KbPracticeDecision, KbReview, KbReviewAssignment, KbVersion, User
from ..models.rbac import UserRole
from . import citations, meta, readiness
from . import expert_bench as eb
from . import practice_queue as pq
from . import review as rv
from .items import VersionContent, create_expert_revision

LOW_OVERLAP = 0.5           # 条目段落与原文按字重合低于一半，标“与原文出入大”
BATCH_MAX = 50
CONTEXT_MAX = 600
NO_SLOT = "未分章节"


# ---- 1. 对照审核 ----

def _paragraph(body: str, clause: str) -> str:
    """条目里包含该条款号的段落（空行分段）；找不到返回空。"""
    key = re.sub(r"\s+", "", clause)
    for p in re.split(r"\n\s*\n", body):
        if key and key in re.sub(r"\s+", "", p):
            return p.strip()[:CONTEXT_MAX]
    return ""


def _segments(a: str, b: str) -> tuple[list[dict], list[dict], float]:
    """按字比对：返回 (a 的分段, b 的分段, b 被 a 覆盖的比例)。same=False 的段是出入处。"""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    sa: list[dict] = []
    sb: list[dict] = []

    def put(out: list[dict], text: str, same: bool) -> None:
        if not text:
            return
        if out and out[-1]["same"] == same:
            out[-1]["text"] += text
        else:
            out.append({"text": text, "same": same})

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        put(sa, a[i1:i2], tag == "equal")
        put(sb, b[j1:j2], tag == "equal")
    matched = sum(m.size for m in sm.get_matching_blocks())
    return sa, sb, round(matched / len(b), 4) if b else 0.0


def _citation_rows(db: Session, v: KbVersion) -> list[dict]:
    rows = []
    for c in citations.check(db, v.body, v.ref_document, v.ref_code):
        entry = _paragraph(v.body, c.clause)
        row = {"document": c.document, "clause": c.clause, "found": c.found, "src_doc_id": c.src_doc_id,
               "ref_status": c.ref_status, "ref_version": c.ref_version, "catalog": c.catalog,
               "entry": entry, "source": c.excerpt, "entry_segments": [{"text": entry, "same": True}] if entry else [],
               "source_segments": [{"text": c.excerpt, "same": True}] if c.excerpt else [], "overlap": None,
               "flag": ""}
        if not c.found:
            row["flag"] = "依据里找不到这一条，请核对文件名和条款号"
        elif c.ref_status in ("repealed", "superseded"):
            row["flag"] = "所引依据已废止或被替代"
        elif c.excerpt and entry:
            row["entry_segments"], row["source_segments"], row["overlap"] = _segments(entry, c.excerpt)
            if row["overlap"] < LOW_OVERLAP:
                row["flag"] = "与依据原文出入较大，请看标出的字句"
        rows.append(row)
    return rows


def _agent(v: KbVersion) -> dict:
    basis, confidence, issues = pq._agent_opinion(v)
    agent = [r for r in v.reviews if r.review_role == "agent"]
    last = max(agent, key=lambda r: r.id) if agent else None
    unsure: set[str] = set()
    for r in agent:
        f = r.findings if isinstance(r.findings, dict) else {}
        unsure |= {x for x in f.get("uncertain_fields", []) if isinstance(x, str)}
    return {"reviewed": last is not None, "basis": basis, "confidence": confidence, "issues": issues,
            "flagged": bool(last and last.decision == "flag"), "uncertain_fields": sorted(unsure)}


def _flags(cites: list[dict], agent: dict) -> list[dict]:
    flags = [{"where": f"citation:{i}", "reason": c["flag"]} for i, c in enumerate(cites) if c["flag"]]
    flags += [{"where": f"agent:issue:{i}", "reason": f"Agent 提的问题：{x}"} for i, x in enumerate(agent["issues"])]
    labels = dict(eb.FIELDS)
    flags += [{"where": f"field:{n}", "reason": f"Agent 拿不准：{labels.get(n, n)}"} for n in agent["uncertain_fields"]]
    if agent["flagged"] and not agent["issues"]:
        flags.append({"where": "agent:decision", "reason": "Agent 存疑，请通看一遍"})
    return flags


def require_expert(db: Session, user: User) -> None:
    eb.require_expert(db, user)


def compare(db: Session, user: User, version_id: int) -> dict:
    require_expert(db, user)
    v = db.get(KbVersion, version_id)
    if v is None:
        raise rv.ReviewError(404, "没有这个版本")
    cites = _citation_rows(db, v)
    agent = _agent(v)
    flags = _flags(cites, agent)
    return {"version_id": v.id, "doc_id": v.item.doc_id, "version": v.version, "title": v.title, "body": v.body,
            "status": v.status, "citations": cites, "agent": agent, "flags": flags, "flagged": bool(flags)}


# ---- 2. 修改后认可 ----

def _added_text(old: str, new: str) -> str:
    """专家新写的行（只拿这些做去标识预览，原稿本身已过入口闸）。"""
    olds = set(old.splitlines())
    return "\n".join(x for x in new.splitlines() if x.strip() and x not in olds)


def _precheck(db: Session, user: User, v: KbVersion | None) -> KbVersion:
    require_expert(db, user)
    if v is None or v.status != "in_review" or not eb._content_pending(v):
        raise rv.ReviewError(404, "这条不在待认可的卡里")
    if user.username in rv._authors(v):
        raise rv.ReviewError(403, "不能认可自己提交或创建的条目")
    if rv.level3(v) and not any(r.review_role == "agent" for r in v.reviews):
        raise rv.ReviewError(409, "知识审核 Agent 的意见还没出来，请稍后再拍板")
    return v


def revise(db: Session, user: User, version_id: int, body: str, comment: str = "", elapsed_ms: int = 0,
           deid_ack: bool = False) -> dict:
    v = _precheck(db, user, db.get(KbVersion, version_id))
    body = body.replace("\r\n", "\n")
    if not body.strip():
        raise rv.ReviewError(422, "改后的正文不能为空")
    if body.strip() == v.body.strip():
        raise rv.ReviewError(422, "正文没有改动；不用改就直接认可")
    try:
        VersionContent(title=v.title, body=body, source_type=v.source_type, source_detail=v.source_detail,
                       ref_document=v.ref_document, ref_version=v.ref_version,
                       ref_status=v.ref_status).check(v.item.layer)
    except meta.MetaError as e:
        raise rv.ReviewError(422, str(e)) from e
    rv._check_text(v.title, body)                          # 机密级检测，命中 422
    hits = eb._deid_hits([_added_text(v.body, body), comment])
    if hits and not deid_ack:
        desc = "、".join(f"{h['category']} {h['count']} 处" for h in hits)
        raise rv.ReviewError(409, f"你改的文字里可能有能认出客户的信息（{desc}），请先看去标识预览：换成占位符，"
                                  "或确认不是客户信息再交")
    change = round(1 - difflib.SequenceMatcher(None, v.body, body, autojunk=False).ratio(), 4)
    elapsed = max(0, min(int(elapsed_ms or 0), pq.MAX_ELAPSED_MS))
    old_version = v.version
    try:
        nv = create_expert_revision(db, v, body, user.username)
        db.add(KbReview(version_id=nv.id, reviewer=user.username, review_role="content", decision="approve",
                        comment=comment[:1000],
                        findings={"expert_revision": True, "revised_from": old_version, "edited": True,
                                  "change_ratio": change, "minutes": round(elapsed / 60000, 1)}))
        if not nv.item.owner:
            nv.item.owner = user.username                  # 署名：条目的专家负责人
        db.add(KbPracticeDecision(version_id=nv.id, reviewer=user.username, decision="modify", elapsed_ms=elapsed,
                                  change_ratio=change))
        db.flush()
        db.refresh(nv)
        rv.publish(db, nv)
    except (meta.MetaError, rv.ReviewError) as e:
        db.rollback()
        if isinstance(e, meta.MetaError):
            raise rv.ReviewError(422, str(e)) from e
        raise
    audit.record(db, actor=user.username, action="kb.expert_revise", result="allowed", doc_id=nv.item.doc_id,
                 version=nv.version, revised_from=old_version, change_ratio=change, elapsed_ms=elapsed,
                 length=len(body), deid_hits=sum(h["count"] for h in hits), deid_ack=bool(deid_ack))
    return {"version_id": nv.id, "doc_id": nv.item.doc_id, "version": nv.version, "status": nv.status,
            "revised_from": old_version, "approved_by": user.username, "expert_revision": True,
            "change_ratio": change}


# ---- 4. 专家档案与按专长派卡 ----

def experts(db: Session) -> set[str]:
    return set(db.scalars(select(User.username).join(UserRole, UserRole.user_id == User.id)
                          .where(UserRole.role_code == KNOWLEDGE_EXPERT, User.is_active.is_(True))))


def _profile_dict(p: KbExpertProfile | None, username: str) -> dict:
    return {"username": username, "products": list(p.products) if p else [],
            "industries": list(p.industries) if p else []}


def set_profile(db: Session, user: User, products: list[str], industries: list[str]) -> dict:
    require_expert(db, user)
    products = list(dict.fromkeys(str(x).strip() for x in products if str(x).strip()))
    industries = list(dict.fromkeys(str(x).strip() for x in industries if str(x).strip() and x != "通用"))
    bad = [x for x in products if x not in meta.PRODUCTS] + [x for x in industries if x not in meta.INDUSTRIES]
    if bad:
        raise rv.ReviewError(422, f"不认识的产品或行业：{'、'.join(bad)}")
    p = db.scalar(select(KbExpertProfile).where(KbExpertProfile.username == user.username))
    if p is None:
        p = KbExpertProfile(username=user.username)
        db.add(p)
    p.products, p.industries, p.updated_at = products, industries, utcnow()
    db.commit()
    audit.record(db, actor=user.username, action="kb.expert_profile", result="allowed", products=products,
                 industries=industries)
    return _profile_dict(p, user.username)


def _pending(db: Session) -> list[KbVersion]:
    rows = db.scalars(select(KbVersion).where(KbVersion.status == "in_review")
                      .options(selectinload(KbVersion.item), selectinload(KbVersion.reviews)))
    return sorted((v for v in rows if eb._content_pending(v)), key=lambda v: (v.submitted_at or v.created_at, v.id))


def _score(p: KbExpertProfile, item: KbItem) -> int:
    if item.product not in (p.products or []):
        return 0
    inds = {x for x in (item.industries or []) if x != "通用"}
    return 2 if inds & set(p.industries or []) else 1


def assign_pending(db: Session) -> dict[int, str]:
    """给还没派的待审版本按专长派；返回 {版本 id: 专家用户名}（不在里面的发全体）。"""
    pending = _pending(db)
    ids = [v.id for v in pending]
    profs = {p.username: p for p in db.scalars(select(KbExpertProfile))}
    profs = {u: p for u, p in profs.items() if u in experts(db)} if profs else {}
    rows = {a.version_id: a for a in db.scalars(select(KbReviewAssignment).where(
        KbReviewAssignment.version_id.in_(ids)))} if ids else {}
    changed = False
    for vid, a in list(rows.items()):              # 档案撤了、不再是知识专家、专长对不上了：收回重派
        v = next(x for x in pending if x.id == vid)
        p = profs.get(a.assignee)
        if p is None or _score(p, v.item) == 0 or a.assignee in rv._authors(v):
            db.delete(a)
            del rows[vid]
            changed = True
    if changed:
        db.flush()                                 # 先删后加，免得同一版本的新派单撞唯一约束
    load = Counter(a.assignee for a in rows.values())
    for v in pending:
        if v.id in rows or not profs:
            continue
        authors = rv._authors(v)
        cands = [(s, u) for u, p in profs.items() if u not in authors and (s := _score(p, v.item))]
        if not cands:
            continue
        top = max(s for s, _ in cands)
        who = min((u for s, u in cands if s == top), key=lambda u: (load[u], u))
        a = KbReviewAssignment(version_id=v.id, assignee=who, score=top)
        db.add(a)
        rows[v.id] = a
        load[who] += 1
        changed = True
    if changed:
        db.commit()
    return {vid: a.assignee for vid, a in rows.items()}


def assignee_map(db: Session) -> dict[int, str]:
    return assign_pending(db)


def loads(db: Session) -> Counter:
    return Counter(assign_pending(db).values())


def profile_view(db: Session, user: User) -> dict:
    require_expert(db, user)
    amap = assign_pending(db)
    load = Counter(amap.values())
    profs = {p.username: p for p in db.scalars(select(KbExpertProfile))}
    team = [{**_profile_dict(profs.get(u), u), "load": load.get(u, 0)} for u in sorted(experts(db))]
    return {"mine": _profile_dict(profs.get(user.username), user.username), "experts": team,
            "unassigned": len(_pending(db)) - len(amap),
            "options": {"products": meta.PRODUCTS, "industries": [x for x in meta.INDUSTRIES if x != "通用"]}}


# ---- 3. 分组、按影响排序、成批认可 ----

def _gains(db: Session, paths: list[Path] | None) -> tuple[dict[str, float], float | None]:
    """知识 key → 认可后总体就绪度提升（readiness.matrix：总体 = 各章平均，章 = 就绪槽位 ÷ 槽位数）。"""
    m = readiness.matrix(db, paths)
    chapters = [c for es in m["evalsets"] for c in es["chapters"]]
    gains: dict[str, float] = {}
    for ch in chapters:
        for s in ch["slots"]:
            if s["state"] != "ready":
                gains[s["key"]] = gains.get(s["key"], 0.0) + 1 / len(ch["slots"]) / len(chapters)
    return {k: round(g, 4) for k, g in gains.items()}, m["readiness"]


def _mine(db: Session, user: User) -> list[KbVersion]:
    amap = assign_pending(db)
    return [v for v in _pending(db) if user.username not in rv._authors(v)
            and amap.get(v.id, user.username) == user.username]


def groups(db: Session, user: User, paths: list[Path] | None = None) -> dict:
    require_expert(db, user)
    gains, overall = _gains(db, paths)
    ready = pq._slot_ready(db)
    amap = assign_pending(db)
    out: dict[tuple[str, str], dict] = {}
    for v in _mine(db, user):
        item = v.item
        key = (item.product, item.slot or "")
        g = out.setdefault(key, {"product": item.product, "slot": item.slot or "",
                                 "label": f"{meta.PRODUCTS.get(item.product, item.product)} · {item.slot or NO_SLOT}",
                                 "slot_ready": ready.get(item.slot, 0) if item.slot else None, "items": []})
        flags = _flags(_citation_rows(db, v), _agent(v))
        g["items"].append({"version_id": v.id, "doc_id": item.doc_id, "version": v.version, "title": v.title,
                           "kind_plain": meta.KNOWLEDGE_KINDS.get(item.knowledge_kind, "") or "通用",
                           "doc_type": item.doc_type, "gain": gains.get(item.import_key or "", 0.0),
                           "flagged": bool(flags), "flag_count": len(flags), "assigned": v.id in amap})
    result = []
    for g in out.values():
        g["items"].sort(key=lambda x: (-x["gain"], x["flagged"], x["version_id"]))
        g["gain"] = round(sum(x["gain"] for x in g["items"]), 4)
        g["count"] = len(g["items"])
        g["clean"] = sum(not x["flagged"] for x in g["items"])
        result.append(g)
    result.sort(key=lambda g: (-g["gain"], g["slot_ready"] is None, g["slot_ready"] or 0, -g["count"], g["label"]))
    return {"readiness": overall, "groups": result, "batch_max": BATCH_MAX}


def batch_approve(db: Session, user: User, version_ids: list[int], comment: str = "", elapsed_ms: int = 0,
                  paths: list[Path] | None = None) -> dict:
    require_expert(db, user)
    ids = list(dict.fromkeys(int(x) for x in version_ids))
    if not ids or len(ids) > BATCH_MAX:
        raise rv.ReviewError(422, f"一次认可 1～{BATCH_MAX} 条")
    mine = {v.id: v for v in _mine(db, user)}
    missing = [i for i in ids if i not in mine]
    if missing:
        raise rv.ReviewError(404, "有的条目不在你的待认可卡里（已处理、派给了别人或你是作者）")
    keys = {(mine[i].item.product, mine[i].item.slot or "") for i in ids}
    if len(keys) > 1:
        raise rv.ReviewError(422, "成批认可只限同一组（同产品、同章节）")
    per = max(0, min(int(elapsed_ms or 0), pq.MAX_ELAPSED_MS)) // len(ids)
    approved: list[int] = []
    skipped: list[dict] = []
    for i in ids:
        v = mine[i]
        flags = _flags(_citation_rows(db, v), _agent(v))
        if flags:
            skipped.append({"version_id": i, "reason": f"有 {len(flags)} 处存疑，请逐条看"})
            continue
        try:
            v = rv.review(db, user, v, "content", "approve", comment)
        except rv.ReviewError as e:
            db.rollback()
            skipped.append({"version_id": i, "reason": str(e)})
            continue
        db.add(KbPracticeDecision(version_id=v.id, reviewer=user.username, decision="adopt", elapsed_ms=per,
                                  change_ratio=0.0))
        db.commit()
        approved.append(i)
    product, slot = keys.pop()
    audit.record(db, actor=user.username, action="kb.expert_batch_approve", result="allowed", product=product,
                 slot=slot, requested=len(ids), approved=len(approved), skipped=len(skipped),
                 version_ids=approved)
    return {"approved": approved, "skipped": skipped}
