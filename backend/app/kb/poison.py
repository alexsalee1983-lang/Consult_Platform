"""投毒防护六层与处置流程（四期 B42；规划刷新设计 2.3、6.4，知识中心改版方案第七节第 13 个闭环）。

六层：
① 实名可追责：没有匿名入口（建版本必须有创建人，items._new_version 守住），每个版本记 batch_key 批次：
   U<投料编号>（人投料）/ I-<日期>-<目录哈希>（批量导入，importer 用 batch() 包起来）/ M-<日期>-<用户名>（手工起草）。
② 入口清洗：parse 只取可见正文（隐藏文字、批注、属性、不可见字符、HTML 注释不取）；投料和提交时用
   guard.instructions() 识别写给 AI 的指令，投料命中整份退回并记安全事件（本表 intake_instruction 一条），
   提交命中则退回、并自动冻结同批（trigger=instruction）。
③ 人投的料不自动发布：人投料本来就按第三级由非作者知识专家拍板（review.level3）；poison_flags 有值的版本
   不得单人发布、不交知识审核 Agent 自动发布（review.self_review_allowed、agent_review.eligible）。
④ 事实核对与单一来源：同领域、同类型已发布条目里同单位数值的中位数差 10 倍以上标 outlier；非公开来源、
   只有一个人提供的版本标 single_source，检索时乘 SINGLE_SOURCE_WEIGHT 降权，非作者知识专家确认有第二来源后清除。
⑤ 提交人记录与限流：被清除过（处置执行为清除 / 部分清除）或 90 天内投料被退回 ≥2 次的人，提交的版本标
   poor_record（须专家审）；1 小时内投料超过 KB_POISON_UPLOADS_PER_HOUR 份的拒收（429），推卡给咨询业务负责人。
⑥ 一键清除：按批次、提交人或条目，派生条目（derived_from、translation_of 指向它的，逐级）一起清除；
   条目原有的非本批版本还在的，退回到最近的非本批版本，否则整条下线。

处置流程（负责人 11-27 最终版：“投毒要首先发给知识管理员，然后升级到负责人审批。”）：
1. 发现即冻结（status=frozen）：受影响条目 retired 置真、frozen_by 记事件号，检索、引用当场停用；
2. 知识管理员核实影响范围（条目、引用计数），提处置意见 clear / partial / restore（propose）；
   知识管理员缺位：冻结超过 ESCALATE_DAYS 天还没人提意见，咨询业务负责人也可以提；
3. 咨询业务负责人（project_lead）批准后执行（decide）；提意见的和批准的必须是两个不同的人，
   被追查的提交人不能提意见、也不能批准；驳回意见回到第 2 步，冻结保持；
4. 执行后：受影响交付件因条目下线由 kb_impact 自动标“需更新”；记入 F3 质量事件台账（knowledge_poisoning）。
全程审计（kb.poison_*），说明只记长度和摘要哈希，不记原文。
"""
from __future__ import annotations

import re
import statistics
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..core.db import utcnow
from ..core.permissions import KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT, PROJECT_LEAD, roles_of
from ..models import KbChunk, KbItem, KbUpload, KbVersion, User
from ..models.kb import KbPoisonIncident
from ..services import guard
from ..services.quality_incidents import digest

ESCALATE_DAYS = 2
RECORD_DAYS = 90
INTAKE_REJECTS_POOR = 2
SINGLE_SOURCE_WEIGHT = 0.7
OUTLIER_FACTOR = 10.0
OUTLIER_MIN_SAMPLES = 5
NOTE_MAX = 2000

TRIGGERS = {"instruction": "写给 AI 的指令", "report": "人工报告", "rate_limit": "提交过快",
            "intake_instruction": "投料含指令被退回"}
SCOPES = {"batch": "批次", "submitter": "提交人", "item": "条目"}
STATUSES = {"notice": "待负责人查看", "frozen": "已冻结，待知识管理员核实", "proposed": "待咨询业务负责人批准",
            "closed": "已关闭"}
PROPOSALS = {"clear": "清除", "partial": "部分清除", "restore": "恢复"}
FLAGS = {"instruction": "含写给 AI 的指令", "outlier": "数值与同类差距过大", "poor_record": "提交人记录差"}
OPEN = ("frozen", "proposed")
VIEW_ROLES = {KNOWLEDGE_ADMIN, PROJECT_LEAD}
REPORT_ROLES = {KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT, PROJECT_LEAD}

_BATCH: ContextVar[str] = ContextVar("kb_batch", default="")
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*(%|％|个工作日|工作日|小时|分钟|个月|天|年)")


class PoisonError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _aware(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


# ---- ① 批次 ----

@contextmanager
def batch(key: str) -> Iterator[None]:
    """批量导入时包起来：期间建的版本都记这个批次。"""
    token = _BATCH.set(key[:64])
    try:
        yield
    finally:
        _BATCH.reset(token)


def batch_key(username: str, upload_id: int | None) -> str:
    if upload_id is not None:
        return f"U{upload_id}"
    return _BATCH.get() or f"M-{utcnow():%Y%m%d}-{username}"[:64]


def import_batch_key(path: str) -> str:
    return f"I-{utcnow():%Y%m%d%H%M%S}-{digest(path)[:8]}"


def provider(db: Session, v: KbVersion) -> str:
    """这个版本内容的提供人：投料来的是上传者，否则是创建人。"""
    if v.upload_id is not None:
        up = db.get(KbUpload, v.upload_id)
        if up is not None:
            return up.uploaded_by
    return v.created_by


# ---- ② 入口：投料 ----

def check_intake(db: Session, user: User, filename: str, text: str) -> None:
    """投料入口（intake.submit 在机密级检测之后调用）：限流、识别写给 AI 的指令。"""
    s = get_settings()
    since = utcnow() - timedelta(hours=1)
    n = db.scalar(select(func.count()).select_from(KbUpload)
                  .where(KbUpload.uploaded_by == user.username, KbUpload.created_at >= since)) or 0
    if n >= s.kb_poison_uploads_per_hour:
        _rate_notice(db, user.username, n)
        audit.record(db, actor=user.username, action="kb.intake", result="blocked", reason="rate_limit", count=n)
        raise PoisonError(429, f"1 小时内已投料 {n} 份，超过上限 {s.kb_poison_uploads_per_hour} 份，请稍后再投；"
                               "已通知咨询业务负责人")
    hits = guard.instructions(filename + "\n" + text)
    if hits:
        kinds = sorted({h.kind for h in hits})
        inc = KbPoisonIncident(trigger="intake_instruction", scope_kind="submitter", scope_value=user.username,
                               submitter=user.username, categories=kinds, status="closed",
                               reported_by="system:kb.intake", result={"rejected_at_intake": True})
        db.add(inc)
        db.commit()
        audit.record(db, actor=user.username, action="kb.intake", result="blocked", reason="instruction",
                     categories=kinds, poison_incident=inc.id)
        raise PoisonError(422, "资料里有写给 AI 的指令样式内容，整份退回，文件没有保存（已记安全事件）。"
                               "知识资料只当参考，请删掉这类文字后再投")


def _rate_notice(db: Session, username: str, n: int) -> None:
    recent = db.scalar(select(KbPoisonIncident.id).where(
        KbPoisonIncident.trigger == "rate_limit", KbPoisonIncident.submitter == username,
        KbPoisonIncident.status == "notice"))
    if recent:
        return
    inc = KbPoisonIncident(trigger="rate_limit", scope_kind="submitter", scope_value=username, submitter=username,
                           categories=[], status="notice", reported_by="system:kb.intake")
    db.add(inc)
    db.commit()
    audit.record(db, actor="system:kb.intake", action="kb.poison_open", result="allowed", poison_incident=inc.id,
                 trigger="rate_limit", count=n)


# ---- ②③④⑤ 提交时筛查 ----

def screen(db: Session, v: KbVersion, actor: str) -> None:
    """review.submit 在改状态之前调用：冻结中的批次直接拒收；命中指令则退回并冻结同批；其余写标记。"""
    item = v.item
    if item.frozen_by:
        raise PoisonError(409, f"条目已被投毒事件 #{item.frozen_by} 冻结，处置完成前不能提交")
    if v.batch_key:
        open_inc = db.scalar(select(KbPoisonIncident).where(
            KbPoisonIncident.scope_kind == "batch", KbPoisonIncident.scope_value == v.batch_key,
            KbPoisonIncident.status.in_(OPEN)))
        if open_inc is not None:
            _freeze(db, open_inc, [item])
            db.commit()
            raise PoisonError(409, f"所在批次已被投毒事件 #{open_inc.id} 冻结，处置完成前不能提交")
    hits = guard.instructions("\n".join((v.title, v.summary, v.applicability, v.body)))
    if hits:
        kinds = sorted({h.kind for h in hits})
        scope = ("batch", v.batch_key) if v.batch_key else ("item", item.doc_id)
        inc = open_incident(db, f"system:{actor}"[:64], "instruction", *scope, submitter=provider(db, v),
                            categories=kinds)
        raise PoisonError(422, f"正文里有写给 AI 的指令样式内容，整份退回；同批资料已冻结（投毒事件 #{inc.id}），"
                               "等知识管理员核实")
    flags = []
    if outliers(db, v):
        flags.append("outlier")
    if poor_record(db, provider(db, v)):
        flags.append("poor_record")
    v.poison_flags = flags or None
    v.single_source = is_single_source(db, v)


def requires_expert(v: KbVersion) -> bool:
    """第③层：有防护标记的版本不得单人发布、不交 Agent 自动发布。"""
    return bool(v.poison_flags)


def outliers(db: Session, v: KbVersion) -> list[str]:
    """第④层：正文里的数值和同领域、同类型已发布条目同单位数值的中位数相差 OUTLIER_FACTOR 倍以上，返回单位列表。"""
    mine: dict[str, list[float]] = {}
    for num, unit in _NUMBER.findall(v.body):
        mine.setdefault(unit.replace("％", "%"), []).append(float(num))
    if not mine:
        return []
    item = v.item
    bodies = db.scalars(select(KbVersion.body).join(KbItem, KbItem.current_version_id == KbVersion.id)
                        .where(KbItem.domain == item.domain, KbItem.doc_type == item.doc_type,
                               KbItem.id != item.id, KbItem.retired.is_(False), KbVersion.status == "published")
                        .limit(300))
    pool: dict[str, list[float]] = {}
    for body in bodies:
        for num, unit in _NUMBER.findall(body):
            pool.setdefault(unit.replace("％", "%"), []).append(float(num))
    out = []
    for unit, vals in mine.items():
        ref = pool.get(unit, [])
        if len(ref) < OUTLIER_MIN_SAMPLES:
            continue
        med = statistics.median(ref)
        if med > 0 and any(x > med * OUTLIER_FACTOR or x < med / OUTLIER_FACTOR for x in vals):
            out.append(unit)
    return sorted(out)


def is_single_source(db: Session, v: KbVersion) -> bool:
    if v.source_type == "public":
        return False
    people = {provider(db, x) for x in v.item.versions if x.status in ("published", "superseded") or x.id == v.id}
    return len(people - {""}) <= 1


def poor_record(db: Session, username: str) -> bool:
    if not username:
        return False
    closed = db.scalars(select(KbPoisonIncident).where(KbPoisonIncident.submitter == username,
                                                       KbPoisonIncident.status == "closed",
                                                       KbPoisonIncident.proposal.in_(("clear", "partial"))))
    if any((i.result or {}).get("cleared") for i in closed):
        return True
    since = utcnow() - timedelta(days=RECORD_DAYS)
    rejects = db.scalar(select(func.count()).select_from(KbPoisonIncident).where(
        KbPoisonIncident.submitter == username, KbPoisonIncident.trigger == "intake_instruction",
        KbPoisonIncident.created_at >= since)) or 0
    return rejects >= INTAKE_REJECTS_POOR


def record_of(db: Session, username: str) -> dict:
    rows = db.scalars(select(KbPoisonIncident).where(KbPoisonIncident.submitter == username)).all()
    return {"incidents": len(rows),
            "cleared": sum(1 for i in rows if i.status == "closed" and (i.result or {}).get("cleared")),
            "intake_rejected": sum(1 for i in rows if i.trigger == "intake_instruction"),
            "poor": poor_record(db, username)}


def single_source_chunks(db: Session, chunk_ids: set[int]) -> set[int]:
    if not chunk_ids:
        return set()
    return set(db.scalars(select(KbChunk.id).join(KbVersion, KbVersion.id == KbChunk.version_id)
                          .where(KbChunk.id.in_(chunk_ids), KbVersion.single_source.is_(True))))


def corroborate(db: Session, user: User, version_id: int) -> KbVersion:
    """非作者知识专家确认已有第二来源：清除单一来源标记。"""
    if KNOWLEDGE_EXPERT not in roles_of(db, user.id):
        raise PoisonError(403, "只有知识专家能确认第二来源")
    v = db.get(KbVersion, version_id)
    if v is None:
        raise PoisonError(404, "版本不存在")
    if user.username in {v.created_by, v.submitted_by, provider(db, v)}:
        raise PoisonError(403, "不能为自己提供的内容确认第二来源")
    v.single_source = False
    db.commit()
    audit.record(db, actor=user.username, action="kb.poison_corroborate", result="allowed", doc_id=v.item.doc_id,
                 version=v.version)
    return v


# ---- 冻结与影响范围 ----

def _scope_versions(db: Session, scope_kind: str, scope_value: str) -> list[KbVersion]:
    if scope_kind == "batch":
        return list(db.scalars(select(KbVersion).where(KbVersion.batch_key == scope_value)))
    if scope_kind == "submitter":
        ups = select(KbUpload.id).where(KbUpload.uploaded_by == scope_value)
        return list(db.scalars(select(KbVersion).where(or_(KbVersion.created_by == scope_value,
                                                           KbVersion.upload_id.in_(ups)))))
    item = db.scalar(select(KbItem).where(KbItem.doc_id == scope_value))
    return list(item.versions) if item is not None else []


def affected(db: Session, scope_kind: str, scope_value: str) -> tuple[list[KbItem], set[int], set[int]]:
    """受影响条目（直接 + 派生，逐级）、直接命中的版本 id、派生条目 id。"""
    vers = _scope_versions(db, scope_kind, scope_value)
    direct = {v.item_id for v in vers}
    items = {i.id: i for i in db.scalars(select(KbItem).where(KbItem.id.in_(direct)))} if direct else {}
    derived: set[int] = set()
    frontier = {i.doc_id for i in items.values()}
    while frontier:
        kids = [i for i in db.scalars(select(KbItem).where(or_(KbItem.derived_from.in_(frontier),
                                                                KbItem.translation_of.in_(frontier))))
                if i.id not in items]
        for k in kids:
            items[k.id] = k
            derived.add(k.id)
        frontier = {k.doc_id for k in kids}
    return sorted(items.values(), key=lambda i: i.id), {v.id for v in vers}, derived


def _freeze(db: Session, inc: KbPoisonIncident, items: list[KbItem]) -> int:
    frozen = list(inc.frozen_items or [])
    n = 0
    for item in items:
        if item.retired or item.frozen_by:
            continue
        item.retired, item.frozen_by, item.updated_at = True, inc.id, utcnow()
        frozen.append(item.id)
        n += 1
    inc.frozen_items = frozen
    known = {a["doc_id"] for a in inc.affected or []}
    inc.affected = [*(inc.affected or []), *({"doc_id": i.doc_id, "derived": False} for i in items
                                             if i.doc_id not in known)]
    return n


def open_incident(db: Session, actor: str, trigger: str, scope_kind: str, scope_value: str, *, submitter: str = "",
                  categories: list[str] | None = None, note: str = "") -> KbPoisonIncident:
    """发现疑似投毒：建事件并当场冻结受影响条目（第 1 步即生效）。"""
    if scope_kind not in SCOPES or not scope_value:
        raise PoisonError(422, "范围只能是批次、提交人或条目")
    items, _, derived = affected(db, scope_kind, scope_value)
    if not items:
        raise PoisonError(404, "范围内没有知识条目")
    if not submitter and scope_kind == "submitter":
        submitter = scope_value
    inc = KbPoisonIncident(trigger=trigger, scope_kind=scope_kind, scope_value=scope_value[:64],
                           submitter=submitter[:64], categories=categories or [], status="frozen",
                           reported_by=actor[:64], note_len=len(note), note_sha=digest(note) if note else "")
    db.add(inc)
    db.flush()
    n = _freeze(db, inc, items)
    inc.affected = [{"doc_id": i.doc_id, "derived": i.id in derived} for i in items]
    db.commit()
    audit.record(db, actor=actor, action="kb.poison_open", result="allowed", poison_incident=inc.id, trigger=trigger,
                 scope=scope_kind, items=len(items), derived=len(derived), frozen=n, categories=categories or [],
                 note_len=len(note), note_sha=inc.note_sha)
    return inc


def report(db: Session, user: User, *, doc_id: str = "", batch_key: str = "", submitter: str = "",
           note: str = "") -> KbPoisonIncident:
    """人工报告疑似投毒：给条目编号时冻结它现行（或最新）版本所在批次；知识管理员、负责人也可直接按批次、提交人。"""
    roles = roles_of(db, user.id)
    if not roles & REPORT_ROLES:
        raise PoisonError(403, "只有知识专家、知识管理员、咨询业务负责人能报告疑似投毒；顾问请用条目“有误”反馈")
    if len(note) > NOTE_MAX:
        raise PoisonError(422, f"说明不超过 {NOTE_MAX} 字")
    if not note.strip():
        raise PoisonError(422, "请写明发现的疑点")
    if guard.check(note).blocked:
        raise PoisonError(422, "说明里有机密级内容，请改写")
    if (batch_key or submitter) and not roles & VIEW_ROLES:
        raise PoisonError(403, "按批次或提交人冻结只限知识管理员、咨询业务负责人")
    if doc_id:
        from . import refs

        r = refs.resolve(db, [doc_id]).get(doc_id)
        item = db.get(KbItem, r.item_id) if r else None
        if item is None:
            raise PoisonError(404, "条目不存在")
        v = (db.get(KbVersion, item.current_version_id) if item.current_version_id else None) or item.versions[-1]
        scope = ("batch", v.batch_key) if v.batch_key else ("item", item.doc_id)
        who = provider(db, v)
    elif batch_key:
        scope, who = ("batch", batch_key.strip()), ""
        first = db.scalar(select(KbVersion).where(KbVersion.batch_key == scope[1]).limit(1))
        who = provider(db, first) if first is not None else ""
    elif submitter:
        scope, who = ("submitter", submitter.strip()), submitter.strip()
    else:
        raise PoisonError(422, "请给出条目编号、批次或提交人")
    if who == user.username:
        raise PoisonError(403, "不能报告自己提供的内容，请联系知识管理员")
    return open_incident(db, user.username, "report", *scope, submitter=who, note=note)


# ---- 处置 ----

def _get(db: Session, inc_id: int) -> KbPoisonIncident:
    inc = db.get(KbPoisonIncident, inc_id)
    if inc is None:
        raise PoisonError(404, "投毒事件不存在")
    return inc


def escalated(inc: KbPoisonIncident, now: datetime | None = None) -> bool:
    """知识管理员缺位：冻结超过 ESCALATE_DAYS 天还没有处置意见，升级到咨询业务负责人。"""
    now = now or utcnow()
    return inc.status == "frozen" and now - _aware(inc.created_at) > timedelta(days=ESCALATE_DAYS)


def propose(db: Session, user: User, inc_id: int, decision: str, doc_ids: list[str], note: str) -> KbPoisonIncident:
    roles = roles_of(db, user.id)
    if not roles & VIEW_ROLES:
        raise PoisonError(403, "只有知识管理员、咨询业务负责人能处理投毒事件")
    inc = _get(db, inc_id)
    if inc.status != "frozen":
        raise PoisonError(409, "只有已冻结、待核实的事件能提处置意见")
    if KNOWLEDGE_ADMIN not in roles and not escalated(inc):
        raise PoisonError(403, f"先由知识管理员核实并提处置意见；冻结超过 {ESCALATE_DAYS} 天没人处理时，"
                               "咨询业务负责人才能直接提")
    if user.username == inc.submitter:
        raise PoisonError(403, "被追查的提交人不能处理这个事件")
    if decision not in PROPOSALS:
        raise PoisonError(422, "处置意见只能是清除、部分清除或恢复")
    if len(note) > NOTE_MAX or guard.check(note).blocked:
        raise PoisonError(422, f"说明不超过 {NOTE_MAX} 字，且不能有机密级内容")
    known = {a["doc_id"] for a in inc.affected or []}
    picked = list(dict.fromkeys(d.strip() for d in doc_ids if d.strip()))
    if decision == "partial":
        if not picked or set(picked) - known:
            raise PoisonError(422, "部分清除要列出要清除的条目，且只能是受影响条目")
    else:
        picked = []
    inc.status, inc.proposal, inc.proposal_items = "proposed", decision, picked
    inc.proposed_by, inc.proposed_at, inc.updated_at = user.username, utcnow(), utcnow()
    db.commit()
    audit.record(db, actor=user.username, action="kb.poison_propose", result="allowed", poison_incident=inc.id,
                 decision=decision, items=len(picked), escalated=KNOWLEDGE_ADMIN not in roles,
                 note_len=len(note), note_sha=digest(note) if note else "")
    return inc


def decide(db: Session, user: User, inc_id: int, approve: bool, note: str = "") -> KbPoisonIncident:
    def deny(status: int, reason: str, msg: str):
        audit.record(db, actor=user.username, action="kb.poison_decide", result="blocked", poison_incident=inc_id,
                     reason=reason)
        raise PoisonError(status, msg)

    if PROJECT_LEAD not in roles_of(db, user.id):
        deny(403, "role", "处置意见须由咨询业务负责人批准")
    inc = _get(db, inc_id)
    if inc.status != "proposed":
        raise PoisonError(409, "这个事件没有待批准的处置意见")
    if user.username == inc.proposed_by:
        deny(403, "same_person", "提出处置意见的人不能自己批准，需另一位咨询业务负责人")
    if user.username == inc.submitter:
        deny(403, "submitter", "被追查的提交人不能批准")
    if len(note) > NOTE_MAX or guard.check(note).blocked:
        raise PoisonError(422, f"说明不超过 {NOTE_MAX} 字，且不能有机密级内容")
    inc.decided_by, inc.decided_at, inc.updated_at = user.username, utcnow(), utcnow()
    if not approve:
        inc.status = "frozen"
        db.commit()
        audit.record(db, actor=user.username, action="kb.poison_decide", result="allowed", poison_incident=inc.id,
                     approve=False, proposal=inc.proposal, note_len=len(note))
        return inc
    result = _execute(db, inc, user.username)
    inc.status, inc.result = "closed", result
    db.commit()
    audit.record(db, actor=user.username, action="kb.poison_decide", result="allowed", poison_incident=inc.id,
                 approve=True, proposal=inc.proposal, cleared=len(result["cleared"]),
                 reverted=len(result["reverted"]), restored=len(result["restored"]), note_len=len(note))
    _ledger(db, inc, result)
    return inc


def _execute(db: Session, inc: KbPoisonIncident, actor: str) -> dict:
    items, scope_vids, derived = affected(db, inc.scope_kind, inc.scope_value)
    known = {a["doc_id"] for a in inc.affected or []}
    items = [i for i in items if i.doc_id in known or i.id in set(inc.frozen_items or [])]
    if inc.proposal == "clear":
        purge = {i.doc_id for i in items}
    elif inc.proposal == "partial":
        purge = set(inc.proposal_items or [])
    else:
        purge = set()
    out: dict[str, list[str]] = {"cleared": [], "reverted": [], "restored": []}
    for item in items:
        if item.doc_id in purge:
            whole = item.id in derived or inc.scope_kind == "item"
            out[_purge(db, item, None if whole else scope_vids, actor)].append(item.doc_id)
        elif item.frozen_by == inc.id:
            item.retired, item.frozen_by, item.updated_at = False, None, utcnow()
            out["restored"].append(item.doc_id)
    return out


def _purge(db: Session, item: KbItem, scope_vids: set[int] | None, actor: str) -> str:
    """清除：条目还有本批之外的已发布或已替代版本，退回到最近的那一版；否则整条下线（历史版本保留）。"""
    from .review import make_chunks

    keep = [] if scope_vids is None else [v for v in item.versions
                                          if v.id not in scope_vids and v.status in ("published", "superseded")]
    if not keep:
        for v in item.versions:
            if v.status in ("published", "draft", "in_review", "candidate"):
                v.status = "retired"
        item.retired, item.frozen_by, item.updated_at = True, None, utcnow()
        db.flush()
        audit.record(db, actor=actor, action="kb.retire", result="allowed", doc_id=item.doc_id, reason="投毒清除")
        return "cleared"
    target = max(keep, key=lambda v: v.version)
    for v in item.versions:
        if v.id in scope_vids and v.status in ("published", "draft", "in_review", "candidate", "superseded"):
            v.status = "retired"
    target.status = "published"
    make_chunks(db, target)
    item.current_version_id = target.id
    item.retired, item.frozen_by, item.updated_at = False, None, utcnow()
    db.flush()
    from .search import enqueue_embedding

    enqueue_embedding(db, target.id, created_by=actor)
    return "reverted"


def _ledger(db: Session, inc: KbPoisonIncident, result: dict) -> None:
    """执行后记入 F3 质量事件台账；说明只是计数。"""
    from ..services import quality_incidents

    desc = (f"投毒事件 #{inc.id}：{PROPOSALS[inc.proposal]}；清除 {len(result['cleared'])}、退回 "
            f"{len(result['reverted'])}、恢复 {len(result['restored'])}")
    view = quality_incidents.register_system(db, "kb.poison", category="knowledge_poisoning", ref_kind="kb_batch",
                                             ref_id=f"P{inc.id}", description=desc)
    inc.quality_incident_id = view.incident.id
    db.commit()


def notice_action(db: Session, user: User, inc_id: int, action: str) -> KbPoisonIncident:
    """提交过快的提醒：负责人看过后关闭，或转为投毒事件（冻结该提交人的全部条目）。"""
    if PROJECT_LEAD not in roles_of(db, user.id):
        raise PoisonError(403, "提交过快的提醒由咨询业务负责人处理")
    inc = _get(db, inc_id)
    if inc.status != "notice":
        raise PoisonError(409, "这条不是待处理的提醒")
    if action == "dismiss":
        inc.status, inc.result, inc.decided_by, inc.decided_at = "closed", {"dismissed": True}, user.username, utcnow()
        db.commit()
        audit.record(db, actor=user.username, action="kb.poison_notice", result="allowed", poison_incident=inc.id,
                     notice_action="dismiss")
        return inc
    if action != "freeze":
        raise PoisonError(422, "只能关闭提醒或转为投毒事件冻结")
    items, _, derived = affected(db, inc.scope_kind, inc.scope_value)
    inc.status, inc.reported_by, inc.updated_at = "frozen", user.username, utcnow()
    inc.created_at = utcnow()      # 升级时限从冻结起算
    n = _freeze(db, inc, items)
    inc.affected = [{"doc_id": i.doc_id, "derived": i.id in derived} for i in items]
    db.commit()
    audit.record(db, actor=user.username, action="kb.poison_notice", result="allowed", poison_incident=inc.id,
                 notice_action="freeze", items=len(items), frozen=n)
    return inc


# ---- 查看 ----

def can_view(db: Session, user: User) -> bool:
    return bool(roles_of(db, user.id) & VIEW_ROLES)


def out(db: Session, inc: KbPoisonIncident, detail: bool = False) -> dict:
    d = {"id": inc.id, "trigger": inc.trigger, "trigger_label": TRIGGERS.get(inc.trigger, inc.trigger),
         "scope_kind": inc.scope_kind, "scope_label": SCOPES.get(inc.scope_kind, ""), "scope_value": inc.scope_value,
         "submitter": inc.submitter, "categories": inc.categories or [], "status": inc.status,
         "status_label": STATUSES.get(inc.status, inc.status), "escalated": escalated(inc),
         "items": len(inc.affected or []), "frozen": len(inc.frozen_items or []),
         "reported_by": inc.reported_by, "note_len": inc.note_len, "note_sha": inc.note_sha,
         "proposal": inc.proposal, "proposal_label": PROPOSALS.get(inc.proposal, ""),
         "proposal_items": inc.proposal_items or [], "proposed_by": inc.proposed_by,
         "proposed_at": _aware(inc.proposed_at) if inc.proposed_at else None, "decided_by": inc.decided_by,
         "decided_at": _aware(inc.decided_at) if inc.decided_at else None, "result": inc.result,
         "quality_incident_id": inc.quality_incident_id, "created_at": _aware(inc.created_at)}
    if detail:
        from ..project import kb_impact

        rows = []
        titles = {i.doc_id: i for i in db.scalars(select(KbItem).where(
            KbItem.doc_id.in_([a["doc_id"] for a in inc.affected or []])))}
        for a in inc.affected or []:
            item = titles.get(a["doc_id"])
            v = item.versions[-1] if item is not None and item.versions else None
            rows.append({"doc_id": a["doc_id"], "derived": a["derived"], "title": v.title if v else "",
                         "frozen": bool(item and item.frozen_by == inc.id),
                         "usage": kb_impact.usage_counts(db, a["doc_id"])})
        d["affected"] = rows
        d["submitter_record"] = record_of(db, inc.submitter) if inc.submitter else None
    return d


def listing(db: Session, user: User, status: str = "") -> list[dict]:
    if not can_view(db, user):
        raise PoisonError(403, "只有知识管理员、咨询业务负责人能查看投毒事件")
    q = select(KbPoisonIncident).order_by(KbPoisonIncident.id.desc()).limit(200)
    if status:
        q = q.where(KbPoisonIncident.status == status)
    return [out(db, i) for i in db.scalars(q)]


def detail(db: Session, user: User, inc_id: int) -> dict:
    if not can_view(db, user):
        raise PoisonError(403, "只有知识管理员、咨询业务负责人能查看投毒事件")
    return out(db, _get(db, inc_id), detail=True)


def tasks(db: Session, user: User) -> list[dict]:
    """我的任务里的投毒卡（置顶）：知识管理员看待核实的；负责人看待批准、提交过快提醒和超时升级的。"""
    roles = roles_of(db, user.id)
    if not roles & VIEW_ROLES:
        return []
    rows = []
    for inc in db.scalars(select(KbPoisonIncident).where(KbPoisonIncident.status.in_(("notice", *OPEN)))
                          .order_by(KbPoisonIncident.id)):
        esc = escalated(inc)
        if inc.status == "frozen" and (KNOWLEDGE_ADMIN in roles or (PROJECT_LEAD in roles and esc)):
            title = f"投毒事件 #{inc.id} 待核实：{len(inc.affected or [])} 条已冻结" + ("（已超时升级）" if esc else "")
            blocked = "你是被追查的提交人，需其他人处理" if inc.submitter == user.username else ""
        elif inc.status == "proposed" and PROJECT_LEAD in roles:
            title = f"投毒事件 #{inc.id} 待批准：{PROPOSALS.get(inc.proposal, '')}"
            blocked = ("你提出了处置意见，需另一位咨询业务负责人批准" if inc.proposed_by == user.username
                       else "你是被追查的提交人，需其他人处理" if inc.submitter == user.username else "")
        elif inc.status == "notice" and PROJECT_LEAD in roles:
            title = f"提交过快已限流：{inc.submitter}"
            blocked = ""
        else:
            continue
        rows.append({"ref_id": inc.id, "title": title, "blocked": blocked, "created_at": inc.created_at})
    return rows
