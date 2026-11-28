"""知识审批流程：草稿 → 提交待审 → 双人审核 → 发布 → 复审 / 修订 / 下线（架构设计第五节“知识”生命周期）。

规则（决策记录 2026-11-08，2026-11-13 调整为两人原则）：
- 两项审核：知识专家审内容（content），知识管理员审合规（compliance）；两项各要一次通过。
- 两人原则：提交人、草稿创建人都不能审核自己的版本；同一人同时持有两个角色时，可以一人完成两项审核。
  人多了只要把角色分给不同的人，自然回到两人分审，不需要开关。
- 公开资料单人发布：KB_SINGLE_REVIEWER_PUBLIC=true 时，来源为公开资料、无需去标识的版本，作者本人也可审核；
  这样发布的版本标记 single_review，由作者以外的知识专家复审确认（补复审）后清除。项目回流等其他来源不适用。
- 第三级（二期 B12，决策记录 2026-11-15）：来源为项目沉淀、专家萃取、内部制度改造且去标识已通过的版本，
  合规审核并入去标识确认（知识管理员，不能是上传者），发布只要一位知识专家（不能是作者）内容审核拍板；
  提交后知识审核 Agent 先审、把问题列在审核页，只出意见、不自动发布（app/kb/agent_review.py）；Agent 意见出来之前
  不能通过（可以驳回）。Agent 调用失败、未登记或没接模型时也记一条说明，之后即可拍板，不会卡住。
- 任一审核人退回，版本即“已退回”（终态）；要修改就基于它建新版本再提交，审核记录始终对应被审的那一版。
- 来源：投料来的草稿必须是去标识已通过的投料；不经投料直接写的草稿只允许“公开资料”（deid_status=not_needed），
  正文同样先过机密级检测。
- AI 基线草稿（负责人 2026-11-27）：虚拟客户的黄金样例、公司做法基线稿，非生产环境经 kb-import --ai-baseline 导入，
  来源只能是专家萃取，deid_status=virtual（虚拟数据免去标识），按第三级由一位非作者知识专家拍板；生产环境导入、提交都拒绝。
- 发布：正文按章节切片写入 kb_chunks（PostgreSQL 自动建中文全文索引）；条目的现行版本切到新版本，旧版本“已替代”。
- 公开资料合规关自动为主（四期 B43，app/kb/compliance_auto.py）：程序规则 + 知识审核 Agent 判合规，通过的由平台记一条
  合规审核通过（reviewer=platform:compliance），例外（去标识没过、来源不在白名单、疑似投毒、抽检差错超门槛等）才留给知识管理员。
- 下线：条目标记下线，现行版本“已下线”，检索不再返回；历史版本全部保留。
所有操作写审计（只记编号、状态、角色，不记正文）。
"""
from __future__ import annotations

import difflib
import json
from datetime import date

from sqlalchemy import bindparam, func, or_, select, text
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..core.db import KB_BIND, utcnow
from ..core.permissions import KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT, roles_of
from ..models import KbChunk, KbDeidReport, KbItem, KbReview, KbUpload, KbVersion, User
from ..services import guard, storage
from . import labels, meta, multilang, parse, poison, refs
from .items import ItemMeta, VersionContent, create_draft, create_item, replace_mapping_rows

ROLE_OF = {"content": KNOWLEDGE_EXPERT, "compliance": KNOWLEDGE_ADMIN}
KNOWLEDGE_ROLES = {KNOWLEDGE_EXPERT, KNOWLEDGE_ADMIN}
LEVEL3_SOURCES = ("project", "expert", "internal")


def level3(v: KbVersion) -> bool:
    """第三级：非公开来源、去标识已由知识管理员确认（合规审核并入去标识确认）。"""
    return v.source_type in LEVEL3_SOURCES and (v.deid_status == "passed" or virtual_ok(v))


def virtual_ok(v: KbVersion) -> bool:
    """AI 基线草稿：虚拟数据免去标识，只在非生产环境认。"""
    return v.deid_status == "virtual" and not get_settings().is_prod


def check_ai_baseline(c: VersionContent) -> None:
    if get_settings().is_prod:
        raise ReviewError(403, "生产环境不收 AI 基线草稿")
    if c.source_type != "expert":
        raise ReviewError(422, "AI 基线草稿只收专家萃取来源（source_type: expert）")


def required_roles(v: KbVersion) -> tuple[str, ...]:
    return ("content",) if level3(v) else tuple(ROLE_OF)


class ReviewError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def is_knowledge_user(db: Session, user: User) -> bool:
    return bool(roles_of(db, user.id) & KNOWLEDGE_ROLES)


def require_knowledge(db: Session, user: User) -> None:
    if not is_knowledge_user(db, user):
        raise ReviewError(403, "只有知识专家、知识管理员能编辑和审核知识条目")


def _check_text(title: str, body: str) -> None:
    result = guard.check(title + "\n" + body)
    if result.blocked:
        raise ReviewError(422, result.summary() + "，请修改后再保存")


# ---- 正文来源 ----

def body_from_upload(db: Session, upload_id: int) -> tuple[str, KbDeidReport]:
    """取去标识已通过的投料，拼成 Markdown 正文（章节标题用 ## ）。"""
    up = db.get(KbUpload, upload_id)
    if up is None:
        raise ReviewError(404, "投料记录不存在")
    report = db.scalars(select(KbDeidReport).where(KbDeidReport.upload_id == up.id, KbDeidReport.status == "passed")
                        .order_by(KbDeidReport.id.desc()).limit(1)).first()
    if up.status != "deid_passed" or report is None or not report.deid_key:
        raise ReviewError(409, "这份投料还没有通过去标识，不能建条目")
    data = json.loads(storage.get("knowledge", report.deid_key))
    parts = [f"## {s['heading']}\n\n{s['text']}" if s["heading"] else s["text"] for s in data["sections"]]
    return "\n\n".join(parts), report


def _source_fields(db: Session, c: VersionContent, upload_id: int | None, ai_baseline: bool = False) -> dict:
    if ai_baseline:
        check_ai_baseline(c)
        return {"deid_status": "virtual"}
    if upload_id is not None:
        body, report = body_from_upload(db, upload_id)
        if not c.body.strip():
            c.body = body
        return {"upload_id": upload_id, "deid_status": "passed", "deid_report_id": report.id}
    if c.source_type != "public":
        raise ReviewError(422, "不经投料直接填写正文只适用于“公开资料”；其他来源请先投料并通过去标识")
    return {"deid_status": "not_needed"}


def _check_derived(db: Session, doc_id: str) -> None:
    """派生条目（解读、做法）挂的依据条目必须存在（四期 B42 第⑥层：清除依据时一起清）。"""
    if doc_id and doc_id not in refs.resolve(db, [doc_id]):
        raise ReviewError(422, f"派生自的条目 {doc_id} 不存在")


def _apply(v: KbVersion, fields: dict) -> None:
    for k, val in fields.items():
        setattr(v, k, val)


# ---- 草稿 ----

def new_item(db: Session, user: User, m: ItemMeta, c: VersionContent, upload_id: int | None = None,
             ai_baseline: bool = False) -> KbVersion:
    require_knowledge(db, user)
    fields = _source_fields(db, c, upload_id, ai_baseline)
    _check_text(c.title, c.body)
    try:
        m.check()
        multilang.check_link(db, m.translation_of, m.language)     # 四期 B28①：译文必须挂原文
        _check_derived(db, m.derived_from)
        v = create_item(db, m, c, created_by=user.username)
    except meta.MetaError as e:
        raise ReviewError(422, str(e)) from e
    _apply(v, fields)
    v.batch_key = poison.batch_key(user.username, upload_id)
    db.commit()
    audit.record(db, actor=user.username, action="kb.draft", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, upload_id=upload_id, batch=v.batch_key)
    return v


def new_version(db: Session, user: User, item: KbItem, c: VersionContent, upload_id: int | None = None,
                ai_baseline: bool = False) -> KbVersion:
    require_knowledge(db, user)
    open_draft = db.scalar(select(KbVersion.id).where(KbVersion.item_id == item.id,
                                                      KbVersion.status.in_(("draft", "in_review"))))
    if open_draft:
        raise ReviewError(409, "这个条目已有未完成的草稿或待审版本，请先处理它")
    if upload_id is None and not c.body.strip():          # 修订默认沿用现行版本正文和来源
        cur = db.get(KbVersion, item.current_version_id) if item.current_version_id else None
        if cur is None:
            raise ReviewError(422, "正文不能为空")
        c.body = cur.body
        fields = {"deid_status": cur.deid_status, "deid_report_id": cur.deid_report_id, "upload_id": cur.upload_id}
        if cur.deid_status == "not_needed" and c.source_type != "public":
            raise ReviewError(422, "现行版本来自公开资料，修订时来源仍须为公开资料")
    else:
        fields = _source_fields(db, c, upload_id, ai_baseline)
    _check_text(c.title, c.body)
    try:
        v = create_draft(db, item, c, created_by=user.username)
    except meta.MetaError as e:
        raise ReviewError(422, str(e)) from e
    _apply(v, fields)
    v.batch_key = poison.batch_key(user.username, upload_id)
    db.commit()
    audit.record(db, actor=user.username, action="kb.draft", result="allowed", doc_id=item.doc_id, version=v.version,
                 batch=v.batch_key)
    return v


EDITABLE = ("title", "summary", "body", "applicability", "change_note", "source_detail", "ref_document",
            "ref_version", "ref_status", "ref_code", "ref_replaced_by", "effective_date", "review_date")


def edit_draft(db: Session, user: User, v: KbVersion, changes: dict) -> KbVersion:
    require_knowledge(db, user)
    if v.status != "draft":
        raise ReviewError(409, "只有草稿能修改；已提交的版本请等审核结果，退回的版本请建新版本")
    for k, val in changes.items():
        if k in EDITABLE and val is not None:
            setattr(v, k, val)
    try:
        VersionContent(title=v.title, body=v.body, source_type=v.source_type, source_detail=v.source_detail,
                       ref_document=v.ref_document, ref_version=v.ref_version,
                       ref_status=v.ref_status).check(v.item.layer)
        if "body" in changes:
            replace_mapping_rows(db, v)          # 映射矩阵草稿：正文表格重新解析（B3）
    except meta.MetaError as e:
        db.rollback()
        raise ReviewError(422, str(e)) from e
    _check_text(v.title, v.body)
    db.commit()
    audit.record(db, actor=user.username, action="kb.edit", result="allowed", doc_id=v.item.doc_id, version=v.version,
                 fields=sorted(k for k in changes if k in EDITABLE))
    return v


# ---- 提交与审核 ----

def submit(db: Session, user: User, v: KbVersion) -> KbVersion:
    require_knowledge(db, user)
    if v.status != "draft":
        raise ReviewError(409, "只有草稿能提交")
    if v.deid_status not in ("passed", "not_needed") and not virtual_ok(v):
        raise ReviewError(409, "去标识还没有通过，不能提交" if v.deid_status != "virtual" else "生产环境不收 AI 基线草稿")
    if not v.body.strip():
        raise ReviewError(422, "正文不能为空")
    _check_multilang(db, v, user.username, "kb.submit")
    if v.item.knowledge_kind in meta.EXPERT_ONLY_KINDS and v.source_type == "public":
        raise ReviewError(422, f"{meta.KNOWLEDGE_KINDS[v.item.knowledge_kind]}不收公开资料：请经投料箱以专家萃取、项目沉淀"
                               "或内部制度改造来源提交")
    if get_settings().kb_citation_gate:
        from . import citations

        todo = citations.pending(citations.for_version(db, v))
        if todo:
            audit.record(db, actor=user.username, action="kb.submit", result="blocked", doc_id=v.item.doc_id,
                         version=v.version, reason="citation_pending", pending=len(todo))
            raise ReviewError(422, f"引用待核 {len(todo)} 处：{'、'.join(todo[:5])}{' 等' if len(todo) > 5 else ''}。"
                                   "库里没有对应的依据原文或条款目录：先把原文切条入库（kb-source-split），或改正引用")
    try:                                    # 四期 B42：冻结批次拒收、指令退回并冻结、事实与提交人记录打标
        poison.screen(db, v, user.username)
    except poison.PoisonError as e:
        audit.record(db, actor=user.username, action="kb.submit", result="blocked", doc_id=v.item.doc_id,
                     version=v.version, reason="poison")
        raise ReviewError(e.status, str(e)) from e
    v.status, v.submitted_by, v.submitted_at = "in_review", user.username, utcnow()
    db.commit()
    audit.record(db, actor=user.username, action="kb.submit", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, flags=v.poison_flags or [], single_source=v.single_source)
    from . import agent_review              # 公开资料的解读、映射等交知识审核 Agent（二期 B11），后台执行；
    job = None
    if get_settings().kb_agent_auto_publish or level3(v):   # 第三级只出意见（B12），不受自动发布开关影响
        job = agent_review.enqueue(db, v, created_by=user.username)
    if job is None:                         # 四期 B43：不交 Agent 的公开资料，合规关按程序规则自动判，例外留给知识管理员
        from . import compliance_auto

        if compliance_auto.run(db, v) is not None:
            db.commit()
    return v


def self_review_allowed(v: KbVersion) -> bool:
    """公开资料单人发布：开关打开、来源为公开资料且无需去标识时，作者本人可以审核。"""
    return (get_settings().kb_single_reviewer_public and v.item.knowledge_kind not in meta.EXPERT_ONLY_KINDS and v.source_type == "public"
            and v.deid_status == "not_needed" and not poison.requires_expert(v))


def _authors(v: KbVersion) -> set[str]:
    return {u for u in (v.submitted_by, v.created_by) if u}


def review(db: Session, user: User, v: KbVersion, role: str, decision: str, comment: str = "") -> KbVersion:
    def deny(status: int, reason: str, message: str):
        audit.record(db, actor=user.username, action="kb.review", result="blocked", doc_id=v.item.doc_id,
                     version=v.version, reason=reason)
        raise ReviewError(status, message)

    if v.item.frozen_by:
        deny(409, "frozen", f"条目已被投毒事件 #{v.item.frozen_by} 冻结，处置完成前不能审核")

    if role not in ROLE_OF or decision not in meta.REVIEW_DECISIONS:
        raise ReviewError(422, "role 只能是 content / compliance，decision 只能是 approve / reject")
    if ROLE_OF[role] not in roles_of(db, user.id):
        deny(403, "role_mismatch", "内容审核须由知识专家、合规审核须由知识管理员进行")
    if v.status != "in_review":
        raise ReviewError(409, "这个版本不在待审状态")
    if user.username in _authors(v) and not self_review_allowed(v):
        deny(403, "self_review", "不能审核自己提交或创建的版本")
    if any(r.review_role == role and r.decision == "approve" for r in v.reviews):
        deny(409, "role_done", "这个审核角色已有人通过，请由另一角色审核")
    if role not in required_roles(v):
        deny(409, "level3_no_compliance", "项目沉淀、专家经验、内部制度改造的合规审核已在去标识确认时完成，"
                                         "只需一位知识专家内容审核拍板")
    if level3(v) and decision == "approve" and not any(r.review_role == "agent" for r in v.reviews):
        deny(409, "level3_wait_agent", "知识审核 Agent 的意见还没出来，请稍后再拍板（Agent 调用失败或没接模型时"
                                       "也会记一条说明，之后即可拍板）")

    db.add(KbReview(version_id=v.id, reviewer=user.username, review_role=role, decision=decision,
                    comment=comment[:1000]))
    if decision == "reject":
        v.status = "rejected"
    db.commit()
    db.refresh(v)
    audit.record(db, actor=user.username, action="kb.review", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, role=role, decision=decision)
    approved = {r.review_role for r in v.reviews if r.decision == "approve" and r.review_role in ROLE_OF}
    if v.status == "in_review" and approved >= set(required_roles(v)):
        reviewers = {r.reviewer for r in v.reviews if r.decision == "approve" and not r.reviewer.startswith("platform")}
        v.single_review = not (reviewers - _authors(v))     # 没有作者以外的人通过（只在开关允许时可能）
        publish(db, v)
    return v


# ---- 发布、下线、复审 ----

def expert_confirmed(v: KbVersion) -> bool:
    """有作者以外的人以知识专家身份人工内容审核通过（Agent、平台记录的 review_role 不是 content，不算）。"""
    return any(r.review_role == "content" and r.decision == "approve" and r.reviewer not in _authors(v)
               for r in v.reviews)


def make_chunks(db: Session, v: KbVersion) -> int:
    """把版本正文切成检索切片；已经切过（候选影子期先切好）的不重复切。返回切片数。"""
    existing = db.scalar(select(func.count()).select_from(KbChunk).where(KbChunk.version_id == v.id)) or 0
    if existing:
        return existing
    chunks = parse.chunk(parse.parse("body.md", v.body.encode()).sections)
    for i, c in enumerate(chunks, 1):
        db.add(KbChunk(version_id=v.id, seq=i, heading=c.heading[:300], text=c.text))
    return len(chunks)


def supersede_candidates(db: Session, v: KbVersion) -> None:
    """同一条目里除 v 以外还在候选的版本（三期 B24①）改为已替代：v 发布或成为新候选后它们不再晋级。"""
    for other in v.item.versions:
        if other.id != v.id and other.status == "candidate":
            other.status, other.candidate_result = "superseded", "superseded"


def _check_multilang(db: Session, v: KbVersion, actor: str, action: str) -> None:
    """四期 B28①：译文缺“非官方译本”标记或原文不对，提交、发布都拦。"""
    try:
        multilang.check_item(db, v.item)
    except meta.MetaError as e:
        audit.record(db, actor=actor, action=action, result="blocked", doc_id=v.item.doc_id, version=v.version,
                     reason="translation_invalid")
        raise ReviewError(422, str(e)) from e


def publish(db: Session, v: KbVersion) -> None:
    item = v.item
    if item.frozen_by:                              # 四期 B42：冻结中的条目守住所有发布入口
        audit.record(db, actor="platform", action="kb.publish", result="blocked", doc_id=item.doc_id,
                     version=v.version, reason="frozen")
        raise ReviewError(409, f"条目已被投毒事件 #{item.frozen_by} 冻结，处置完成前不能发布")
    _check_multilang(db, v, "platform", "kb.publish")
    if item.knowledge_kind in meta.EXPERT_ONLY_KINDS and not expert_confirmed(v):   # 三期 B35：守住所有发布入口
        audit.record(db, actor="platform", action="kb.publish", result="blocked", doc_id=item.doc_id,
                     version=v.version, reason="expert_required")
        raise ReviewError(409, f"{meta.KNOWLEDGE_KINDS[item.knowledge_kind]}必须由作者以外的知识专家确认后才能发布")
    chunks = make_chunks(db, v)
    if item.current_version_id and item.current_version_id != v.id:
        old = db.get(KbVersion, item.current_version_id)
        if old is not None and old.status == "published":
            old.status = "superseded"
    supersede_candidates(db, v)
    v.effective_date = v.effective_date or date.today()
    v.review_date = v.review_date or meta.default_review_date(item.layer, v.effective_date, item.doc_type)
    v.status, v.published_at = "published", utcnow()
    item.current_version_id, item.updated_at = v.id, utcnow()
    db.commit()
    audit.record(db, actor="kb-review", action="kb.publish", result="allowed", doc_id=item.doc_id, version=v.version,
                 chunks=chunks, single_review=v.single_review,
                 single_reviewer_public=get_settings().kb_single_reviewer_public)
    from .search import enqueue_embedding          # 发布后后台向量化（未启用向量服务时跳过）

    enqueue_embedding(db, v.id, created_by=v.submitted_by or "kb-review")
    from .conflict_rules import mark_superseded    # 规则预判“以本条为准”的旧条目标待复查（2026-11-25）

    mark_superseded(db, v)
    if v.ref_status in ("repealed", "superseded"):   # 四期 F3②b：已签署交付件引用了已废止依据，程序自动登记质量事件
        from ..services import quality_incidents

        quality_incidents.register_signed_basis(db, item.doc_id)


def retire(db: Session, user: User, item: KbItem, reason: str) -> KbItem:
    if KNOWLEDGE_ADMIN not in roles_of(db, user.id):
        raise ReviewError(403, "只有知识管理员能下线条目")
    if item.retired:
        raise ReviewError(409, "条目已下线")
    item.retired, item.updated_at = True, utcnow()
    for v in item.versions:
        if v.status in ("published", "draft", "in_review", "candidate"):
            v.status = "retired"
    db.commit()
    audit.record(db, actor=user.username, action="kb.retire", result="allowed", doc_id=item.doc_id,
                 reason=reason[:200])
    return item


def reconfirm(db: Session, user: User, item: KbItem) -> KbVersion:
    """复审确认内容仍然有效：不改正文，复审日期从今天起顺延一个周期。内容要改就建新版本。

    单人发布的版本须由作者以外的知识专家复审确认（补复审），确认后清除单人发布标记。"""
    if KNOWLEDGE_EXPERT not in roles_of(db, user.id):
        raise ReviewError(403, "复审确认须由知识专家进行")
    v = db.get(KbVersion, item.current_version_id) if item.current_version_id else None
    if v is None or v.status != "published" or item.retired:
        raise ReviewError(409, "条目没有现行版本")
    if v.single_review and user.username in _authors(v):
        raise ReviewError(403, "单人发布的条目须由作者以外的知识专家补复审")
    backfilled = v.single_review
    v.review_date = meta.default_review_date(item.layer, date.today(), item.doc_type)
    v.single_review = False
    db.commit()
    audit.record(db, actor=user.username, action="kb.reconfirm", result="allowed", doc_id=item.doc_id,
                 version=v.version, review_date=v.review_date.isoformat(), backfilled=backfilled)
    return v


def review_due(db: Session, within_days: int = 30) -> list[KbVersion]:
    """到期待复审的现行版本，加上单人发布、待补复审的版本。"""
    from datetime import timedelta

    limit = date.today() + timedelta(days=within_days)
    return list(db.scalars(select(KbVersion).join(KbItem, KbItem.current_version_id == KbVersion.id)
                           .where(KbVersion.status == "published", KbItem.retired.is_(False),
                                  or_(KbVersion.review_date <= limit, KbVersion.single_review.is_(True)))
                           .order_by(KbVersion.review_date)))


# ---- 审核辅助：版本差异、相似条目 ----

def diff_with_current(db: Session, v: KbVersion, max_lines: int = 200) -> list[str]:
    cur = db.get(KbVersion, v.item.current_version_id) if v.item.current_version_id else None
    if cur is None or cur.id == v.id:
        return []
    lines = difflib.unified_diff(cur.body.splitlines(), v.body.splitlines(), f"v{cur.version}", f"v{v.version}",
                                 lineterm="", n=1)
    return list(lines)[:max_lines]


def similar_items(db: Session, v: KbVersion, limit: int = 5) -> list[dict]:
    """同服务产品下（按产品标签，B15：和本条目任一产品相同即算）、正文与标题相近的已发布条目（可能重复或冲突），供审核人对照。只在 PostgreSQL 上可用。
    标题分词后任一词命中即可（把 & 换成 |），按相关度排序。"""
    if db.get_bind(KbChunk).dialect.name != "postgresql":
        return []
    rows = db.execute(text("""
        SELECT i.doc_id, cv.title, max(ts_rank(c.tsv, q)) AS score
        FROM kb_chunks c
        JOIN kb_versions cv ON cv.id = c.version_id AND cv.status = 'published'
        JOIN kb_items i ON i.current_version_id = cv.id AND NOT i.retired
        , CAST(replace(CAST(plainto_tsquery('zh', :q) AS text), '&', '|') AS tsquery) q
        WHERE c.tsv @@ q AND i.id <> :item
          AND EXISTS (SELECT 1 FROM kb_item_labels l WHERE l.item_id = i.id AND l.kind = 'product'
                      AND l.value IN :products)
        GROUP BY i.doc_id, cv.title ORDER BY score DESC LIMIT :limit
    """).bindparams(bindparam("products", expanding=True)),
        {"q": v.title, "item": v.item_id, "limit": limit,
         "products": labels.products_of(db, [v.item_id]).get(v.item_id, [v.item.product])},
        bind_arguments=KB_BIND).all()
    return [{"doc_id": r.doc_id, "title": r.title, "score": round(float(r.score), 4)} for r in rows]


# ---- 可见范围 ----

def can_view(db: Session, user: User, item: KbItem, v: KbVersion | None = None) -> bool:
    """知识专家、知识管理员看全部；其他业务角色只看已发布、未下线的现行版本，受限条目按可见角色。"""
    if is_knowledge_user(db, user):
        return True
    if item.retired or (v is not None and v.id != item.current_version_id) or not item.current_version_id:
        return False
    if item.classification == "restricted":
        return "consultant" in (item.visible_roles or [])
    return True
