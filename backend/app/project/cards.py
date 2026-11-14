"""严格档客户同意记录与去标识摘要卡（M4 ④）。

依据《客户数据告知模板与合同条款》严格档《数据处理计划》第四节：
- 客户书面同意后才能使用摘要卡。项目经理登记同意记录（文号 + 签署日期，不写客户名称）；同一项目同时只有一条
  有效记录；客户书面撤回后项目经理登记撤回，已上传的摘要卡内容立即清空（编号保留、不复用），之后不能再上传。
- 摘要卡只写控制项层面的事实：评估项、现状、数量级、现场证据编号（原文留在现场）；客户对接人逐份审阅签字后
  才能带离，上传时必须写审阅日期（不早于同意签署日、不晚于今天）。
- 上传（项目成员，整批要么全收要么全拒）先过机密级检测（guard），再过摘要卡规则：不得含机构名称、人员姓名、
  联系方式、IP / 域名等网络标识、系统或产品名称（拉丁字母名称、版本号）、原话（引号里的长句）。
  占位符（【客户】【人员1】【系统A】等）不算违规。命中则整批不保存，返回第几张、哪个字段、哪类问题。
- 同一项目里内容（评估项 + 现状）相同的卡不重复上传。
- 上传者或项目经理可以撤下单张卡（内容清空，编号保留）。
- Agent 经工具 project.evidence 读摘要卡（严格档项目返回摘要卡，标准档返回证据），不新增工具授权。
- 评估项随产品（二期 A8）：项目配置快照里的 card_items（产品 strict.card_items + 模块 adds.card_items）非空时，
  评估项只能取清单里的值（空白折叠后逐字比对），不在清单的记“评估项不在清单”；清单为空不限。模板带下清单，
  现场工具据此给候选值并自检。快照读不出来时拒收上传（503），模板照旧下载、清单为空并注明。

审计只记项目、编号、张数、字数和命中类别，不记卡片内容和文号。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import PROJECT_MANAGER, membership
from ..kb import deid
from ..models import Project, User
from ..models.consent import ProjectConsent, SummaryCard
from ..products import config
from ..services import guard
from . import configs

MAX_CARDS = 200                 # 一次最多上传多少张
ITEM_MAX, FINDING_MAX, MAGNITUDE_MAX, REFS_MAX = 200, 2000, 100, 20
ITEM_NOT_LISTED = "评估项不在清单"
ONSITE_REF_RE = re.compile(r"[A-Z]{1,3}-\d{1,4}(?:-\d{1,3})?")   # D-03、I-02、纪要某条 I-04-07
STANDARD_REFUSAL = "标准档项目不使用摘要卡：资料和纪要直接录入项目区"

# 占位符：【客户】【甲方】【人员1】【系统A】【信息科技部负责人】……
_PLACEHOLDER_RE = re.compile(r"【[一-龥]{1,12}\s?(?:[A-Z]|\d{1,2}|[一二三四五六七八九十])?】")
# 姓氏紧跟职务（“李处长负责”）：去标识规则要求职务后不再接汉字，摘要卡更严，后面接什么都算
_PERSON_TITLE_RE = re.compile(deid._BEFORE + f"[{deid._SURNAMES}](?:" + "|".join(deid._TITLES[:-4]) + ")")
_QUOTE_RE = re.compile(r"[“「『\"]([^”」』\"]{6,})[”」』\"]")
_LATIN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_.+-]*[A-Za-z0-9]|[A-Za-z])(?![A-Za-z0-9])")
_EVIDENCE_CODE_RE = re.compile(r"(?<![A-Za-z0-9])[DIOS]-\d{2,4}(?:-\d{2,3})?(?![\d-])")
_VERSION_RE = re.compile(r"(?<![A-Za-z0-9.])[Vv]?\d+\.\d+\.\d+(?![\d.])|(?<![A-Za-z0-9])[Vv]\d+(?:\.\d+)?(?![\w.])")
# 通用缩写和术语（不是具体系统、产品名），比较时不分大小写
GENERIC_LATIN = set("""
soc siem soar edr xdr ndr mdr nta ueba hids nids ids ips waf dlp iam pam mfa sso ad ldap dns dhcp vpn ssl tls
https http api cmdb itil itsm sla ola kpi kri okr raci pdca iso iec nist csf gb gb/t gbt jr jrt dl dlt yd ydt pci dss
sox gdpr apt ddos ioc ttp ttps cve cvss cwe mitre att ck oa erp crm hr it ot ics scada dcs plc iot ai llm cpu os db
sql web app pc usb wifi lan wan dmz nat acl vlan bgp ntp smtp imap ftp sftp ssh rdp snmp syslog ca pki kms hsm cdn
ha dr rto rpo bcp bia drp sop faq poc uat ci cd devops devsecops sdl sast dast iast rasp sca sbom va pt ti cti ctf
l1 l2 l3 l4 p0 p1 p2 p3 p4 t1 t2 t3 7x24 5x8 7x8 fte q1 q2 q3 q4 pdf word excel ppt email vip id ip mac url saas paas
iaas idc noc ir csirt cert isms ok na tbd
""".split())
CARD_RULES = [
    ("机构名称", "不写客户、供应商等机构名称，用【客户】【供应商A】等占位符"),
    ("人员姓名", "不写人员姓名或“姓 + 职务”称呼，写角色（如【信息科技部负责人】）"),
    ("联系方式", "不写电话、邮箱"),
    ("网络标识", "不写 IP 地址、网段、域名、网址"),
    ("系统名称", "不写系统、产品的具体名称和版本号，用【系统A】等占位符"),
    ("原话", "不写受访者原话（引号里的长句），概括成事实"),
    ("机密级", "口令、密钥、漏洞细节、拓扑与 IP 规划、业务数据和个人信息样本一律不收"),
]
_DEID_KINDS = {"客户名称": "机构名称", "人名": "人员姓名", "联系方式": "联系方式", "网络标识": "网络标识",
               "系统名称": "系统名称"}


class CardError(ValueError):
    def __init__(self, status: int, detail):
        super().__init__(detail if isinstance(detail, str) else "被拒绝")
        self.status = status
        self.detail = detail


@dataclass
class CardIn:
    item: str
    finding: str
    magnitude: str = ""
    onsite_refs: list[str] | None = None
    client_reviewed_on: date | None = None


def card_problems(text: str) -> list[str]:
    """摘要卡规则检查：返回命中的问题类别（按 CARD_RULES 顺序）。只在内存里看原文，不保存。"""
    found: set[str] = set()
    if guard.check(text).blocked:
        found.add("机密级")
    stripped = _EVIDENCE_CODE_RE.sub(" ", _PLACEHOLDER_RE.sub("【】", text))   # 证据编号（现场 O-03）不算系统名
    for rep in deid.deidentify([("", stripped)], "").replacements:
        kind = _DEID_KINDS.get(rep.category)
        if kind:
            found.add(kind)
    if _PERSON_TITLE_RE.search(stripped):
        found.add("人员姓名")
    if _QUOTE_RE.search(stripped):
        found.add("原话")
    if _VERSION_RE.search(stripped) or any(len(m.group(1)) > 1 and m.group(1).lower() not in GENERIC_LATIN
                                           for m in _LATIN_RE.finditer(stripped)):
        found.add("系统名称")
    return [k for k, _ in CARD_RULES if k in found]


def _is_manager(db: Session, user: User, project: Project) -> bool:
    m = membership(db, project.id, user.id)
    return m is not None and m.project_role == PROJECT_MANAGER


def _require_strict(db: Session, user: User, project: Project, action: str) -> None:
    if project.tier != "strict":
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     reason="standard_tier")
        raise CardError(422, STANDARD_REFUSAL)


def _require_manager(db: Session, user: User, project: Project, action: str, what: str) -> None:
    if not _is_manager(db, user, project):
        audit.record(db, actor=user.username, action=action, result="denied", project_code=project.code,
                     reason="not_manager")
        raise CardError(403, f"只有本项目的项目经理能{what}")


def _clean_ref(db: Session, user: User, project: Project, action: str, ref: str, what: str) -> str:
    ref = ref.strip()
    if not ref:
        raise CardError(422, f"请填写{what}的文号或编号（不写客户名称）")
    if set(card_problems(ref)) - {"系统名称"}:      # 文号里常有字母编号，只拦名称和机密内容
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     reason="ref_rejected")
        raise CardError(422, "文号里不要写客户名称、人员姓名或机密内容，只写编号")
    return ref[:64]


def active_consent(db: Session, project: Project) -> ProjectConsent | None:
    return db.scalar(select(ProjectConsent).where(ProjectConsent.project_id == project.id,
                                                  ProjectConsent.revoked.is_(False))
                     .order_by(ProjectConsent.id.desc()))


def list_consents(db: Session, project: Project) -> list[ProjectConsent]:
    return list(db.scalars(select(ProjectConsent).where(ProjectConsent.project_id == project.id)
                           .order_by(ProjectConsent.id)))


def _lock(db: Session, project: Project) -> None:
    db.scalar(select(Project.id).where(Project.id == project.id).with_for_update())


def record_consent(db: Session, user: User, project: Project, doc_ref: str, signed_on: date) -> ProjectConsent:
    action = "project.consent"
    _require_strict(db, user, project, action)
    _require_manager(db, user, project, action, "登记客户同意")
    ref = _clean_ref(db, user, project, action, doc_ref, "客户书面同意")
    if signed_on > date.today():
        raise CardError(422, "签署日期不能晚于今天")
    _lock(db, project)
    if active_consent(db, project) is not None:
        db.rollback()
        raise CardError(409, "本项目已有有效的客户同意记录；客户重新签署的，先登记撤回再登记新的")
    c = ProjectConsent(project_id=project.id, doc_ref=ref, signed_on=signed_on, recorded_by=user.username)
    db.add(c)
    db.commit()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 consent=c.id, signed_on=signed_on.isoformat())
    return c


def _clear(card: SummaryCard, actor: str) -> None:
    card.item, card.finding, card.magnitude, card.onsite_refs, card.char_count = "", "", "", [], 0
    card.withdrawn, card.withdrawn_by, card.withdrawn_at = True, actor, utcnow()


def revoke_consent(db: Session, user: User, project: Project, consent_id: int, revoke_ref: str,
                   received_on: date) -> ProjectConsent:
    """客户书面撤回同意：登记撤回，清空依据这条同意上传的全部摘要卡内容。不可恢复。"""
    action = "project.consent.revoke"
    _require_manager(db, user, project, action, "登记客户撤回同意")
    ref = _clean_ref(db, user, project, action, revoke_ref, "客户书面撤回")
    _lock(db, project)
    c = db.scalar(select(ProjectConsent).where(ProjectConsent.id == consent_id,
                                               ProjectConsent.project_id == project.id))
    if c is None:
        db.rollback()
        raise CardError(404, "同意记录不存在")
    if c.revoked:
        db.rollback()
        raise CardError(409, "这条同意记录已经撤回")
    if received_on > date.today() or received_on < c.signed_on:
        db.rollback()
        raise CardError(422, "收到撤回的日期不能晚于今天，也不能早于同意签署日期")
    cards = list(db.scalars(select(SummaryCard).where(SummaryCard.consent_id == c.id,
                                                      SummaryCard.withdrawn.is_(False))))
    for card in cards:
        _clear(card, user.username)
    c.revoked, c.revoke_ref, c.revoked_on, c.revoked_by, c.revoked_at = True, ref, received_on, user.username, utcnow()
    derived = clear_derived(db, project, user.username)
    db.commit()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 consent=c.id, received_on=received_on.isoformat(), cards_cleared=len(cards), **derived)
    return c


def clear_derived(db: Session, project: Project, username: str) -> dict:
    """客户撤回同意（M4 ④b，负责人 11-13 交开发定）：据摘要卡写成的未签署内容一并清空，不可恢复。
    - 未签署交付件：各章全部版本的正文和来源清空（版本行和编号保留），修订号 +1，待签署的退回起草中；
    - 这些交付件的质量评审意见和问题清单清空；
    - 交付链：未结束的运行取消，各步产出（差距分析）清空。
    已签署的交付件是交给客户的占位符版本，保留，按保留期和删除流程处理。只返回计数。"""
    from ..models import AgentRun, AgentRunStep, Deliverable, DeliverableSection, QualityReview, SectionVersion

    open_ids = list(db.scalars(select(Deliverable.id).where(Deliverable.project_id == project.id,
                                                           Deliverable.status != "signed")))
    versions = list(db.scalars(select(SectionVersion).join(DeliverableSection).where(
        DeliverableSection.deliverable_id.in_(open_ids), SectionVersion.body != ""))) if open_ids else []
    for v in versions:
        v.body, v.sources, v.char_count = "", {}, 0
    touched = {v.section.deliverable_id for v in versions}
    for d in db.scalars(select(Deliverable).where(Deliverable.id.in_(touched))) if touched else []:
        d.revision += 1
        d.updated_at, d.updated_by = utcnow(), username
        if d.status == "review":
            d.status = "drafting"
    reviews = list(db.scalars(select(QualityReview).where(QualityReview.deliverable_id.in_(open_ids)))) \
        if open_ids else []
    for r in reviews:
        r.report, r.issues = "", []
    run_ids = list(db.scalars(select(AgentRun.id).where(AgentRun.project_id == project.id)))
    cancelled = 0
    for run in db.scalars(select(AgentRun).where(AgentRun.project_id == project.id,
                                                 AgentRun.status.in_(("queued", "running", "waiting")))):
        run.status, run.waiting_cards = "cancelled", []
        run.updated_at = run.finished_at = utcnow()
        cancelled += 1
    steps = list(db.scalars(select(AgentRunStep).where(AgentRunStep.run_id.in_(run_ids),
                                                       AgentRunStep.output != ""))) if run_ids else []
    for s in steps:
        s.output = ""
    return {"versions_cleared": len(versions), "reviews_cleared": len(reviews),
            "runs_cancelled": cancelled, "run_outputs_cleared": len(steps)}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _digest(item: str, finding: str) -> str:
    return hashlib.sha256(f"{_norm(item)}\n{_norm(finding)}".encode()).hexdigest()


def card_items(db: Session, project: Project) -> list[str] | None:
    """本项目摘要卡评估项的可选值（项目配置快照）；空列表 = 不限，快照读不出来返回 None。"""
    try:
        return [_norm(x) for x in configs.snapshot(db, project).get("card_items") or [] if _norm(x)]
    except config.CatalogError:
        return None


def _check_batch(cards: list[CardIn], consent: ProjectConsent,
                 items: list[str] | None = None) -> tuple[list[dict], list[CardIn]]:
    problems: list[dict] = []
    allowed = set(items or [])
    clean: list[CardIn] = []
    today = date.today()
    for i, c in enumerate(cards, start=1):
        item, finding, magnitude = _norm(c.item), (c.finding or "").strip(), _norm(c.magnitude)
        refs = list(dict.fromkeys(r.strip().upper() for r in (c.onsite_refs or []) if r and r.strip()))
        if not item:
            problems.append({"card": i, "field": "item", "kinds": ["缺内容"]})
        elif allowed and item not in allowed:
            problems.append({"card": i, "field": "item", "kinds": [ITEM_NOT_LISTED]})
        if not finding:
            problems.append({"card": i, "field": "finding", "kinds": ["缺内容"]})
        for field, text, limit in (("item", item, ITEM_MAX), ("finding", finding, FINDING_MAX),
                                   ("magnitude", magnitude, MAGNITUDE_MAX)):
            if len(text) > limit:
                problems.append({"card": i, "field": field, "kinds": ["超长"]})
            kinds = card_problems(text) if text else []
            if kinds:
                problems.append({"card": i, "field": field, "kinds": kinds})
        if len(refs) > REFS_MAX or any(not ONSITE_REF_RE.fullmatch(r) for r in refs):
            problems.append({"card": i, "field": "onsite_refs", "kinds": ["证据编号格式"]})
        rv = c.client_reviewed_on
        if rv is None or rv > today or rv < consent.signed_on:
            problems.append({"card": i, "field": "client_reviewed_on", "kinds": ["审阅日期"]})
        clean.append(CardIn(item, finding, magnitude, refs, rv))
    return problems, clean


def upload_cards(db: Session, user: User, project: Project, cards: list[CardIn]) -> list[SummaryCard]:
    """整批上传摘要卡：任何一张不过检查，整批不保存。"""
    action = "project.summary_card"
    _require_strict(db, user, project, action)
    if not cards:
        raise CardError(422, "没有摘要卡")
    if len(cards) > MAX_CARDS:
        raise CardError(422, f"一次最多上传 {MAX_CARDS} 张")
    consent = active_consent(db, project)
    if consent is None:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     reason="no_consent")
        raise CardError(422, "本项目没有有效的客户书面同意记录，不能上传摘要卡；请项目经理先登记同意")

    items = card_items(db, project)
    if items is None:
        audit.record(db, actor=user.username, action=action, result="error", project_code=project.code,
                     reason="config")
        raise CardError(503, "本项目的服务产品配置有问题，摘要卡评估项无法核对，请联系系统管理员")
    problems, clean = _check_batch(cards, consent, items)
    if problems:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     reason="card_rules", cards=len(cards),
                     categories=sorted({k for p in problems for k in p["kinds"]}))
        bad = len({p["card"] for p in problems})
        raise CardError(422, {"blocked": True, "summary": f"{bad} 张摘要卡没有通过检查，整批没有保存",
                              "problems": problems})

    _lock(db, project)
    if active_consent(db, project) is None or active_consent(db, project).id != consent.id:
        db.rollback()
        raise CardError(409, "客户同意记录刚刚发生变化，请刷新后重试")
    digests = [_digest(c.item, c.finding) for c in clean]
    existing = dict(db.execute(select(SummaryCard.sha256, SummaryCard.code)
                               .where(SummaryCard.project_id == project.id, SummaryCard.withdrawn.is_(False),
                                      SummaryCard.sha256.in_(digests))).all())
    dup = [{"card": i, "same_as": existing[d]} for i, d in enumerate(digests, start=1) if d in existing]
    dup += [{"card": i, "same_as": f"第 {digests.index(d) + 1} 张"} for i, d in enumerate(digests, start=1)
            if d not in existing and digests.index(d) + 1 != i]
    if dup:
        db.rollback()
        raise CardError(409, {"summary": "有内容相同的摘要卡（已上传过或本批重复），整批没有保存", "duplicates": dup})

    seq = db.scalar(select(func.max(SummaryCard.seq)).where(SummaryCard.project_id == project.id)) or 0
    out: list[SummaryCard] = []
    for c, digest in zip(clean, digests, strict=True):
        seq += 1
        card = SummaryCard(project_id=project.id, consent_id=consent.id, code=f"S-{seq:02d}", seq=seq, item=c.item,
                           finding=c.finding, magnitude=c.magnitude, onsite_refs=c.onsite_refs,
                           client_reviewed_on=c.client_reviewed_on, sha256=digest,
                           char_count=len(c.item) + len(c.finding) + len(c.magnitude), uploaded_by=user.username)
        db.add(card)
        out.append(card)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise CardError(409, "摘要卡编号分配冲突，请重试") from e
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 consent=consent.id, cards=len(out), codes=[c.code for c in out],
                 chars=sum(c.char_count for c in out))
    return out


def list_cards(db: Session, project: Project) -> list[SummaryCard]:
    return list(db.scalars(select(SummaryCard).where(SummaryCard.project_id == project.id).order_by(SummaryCard.seq)))


def get_card(db: Session, project: Project, code: str) -> SummaryCard | None:
    return db.scalar(select(SummaryCard).where(SummaryCard.project_id == project.id,
                                               SummaryCard.code == code.strip().upper()))


def withdraw_card(db: Session, user: User, project: Project, card: SummaryCard) -> SummaryCard:
    """撤下单张卡（上传者或项目经理）：内容清空，编号保留。"""
    action = "project.summary_card.withdraw"
    if card.uploaded_by != user.username and not _is_manager(db, user, project):
        audit.record(db, actor=user.username, action=action, result="denied", project_code=project.code,
                     card=card.code, reason="not_uploader_or_manager")
        raise CardError(403, "只有上传者或项目经理能撤下摘要卡")
    db.refresh(card, with_for_update=True)
    if card.withdrawn:
        db.rollback()
        raise CardError(409, f"{card.code} 已经撤下")
    _clear(card, user.username)
    db.commit()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code, card=card.code)
    return card


def template(db: Session, project: Project) -> dict:
    """空白摘要卡模板（现场工具离场前下载）：字段说明和规则，不含任何客户内容。"""
    c = active_consent(db, project)
    items = card_items(db, project)
    items_note = ("服务产品配置有问题，评估项清单读不出来，平台上传时会拒收" if items is None
                  else "评估项只能从清单里选" if items else "评估项不限")
    return {"project_code": project.code, "consent_active": c is not None, "consent_id": c.id if c else None,
            "fields": [{"key": "item", "name": "评估项", "max": ITEM_MAX, "required": True},
                       {"key": "finding", "name": "现状", "max": FINDING_MAX, "required": True,
                        "hint": "只写控制项层面的事实，如“特权账号近 12 个月未复核”"},
                       {"key": "magnitude", "name": "数量级", "max": MAGNITUDE_MAX, "required": False,
                        "hint": "如“约 30 个”“近 12 个月”"},
                       {"key": "onsite_refs", "name": "现场证据编号", "max": REFS_MAX, "required": False,
                        "hint": "如 D-03、I-02，原文留在现场"},
                       {"key": "client_reviewed_on", "name": "客户对接人审阅签字日期", "required": True}],
            "rules": [{"kind": k, "text": t} for k, t in CARD_RULES],
            "placeholders": ["【客户】", "【甲方】", "【人员1】", "【信息科技部负责人】", "【系统A】", "【供应商A】"],
            "items": items or [], "items_note": items_note, "checks": field_checks()}


def field_checks() -> dict:
    """现场工具（field-tool/）离线自检用的规则：直接导出本模块和去标识规则的正则与词表，两边不会走样。
    现场工具内置一份（DEFAULT_CHECKS，测试核对一致），导入模板时以模板里的为准。机密级检测现场工具只做简化版，
    以平台上传时的检查为准。"""
    return {
        "schema": 1,
        "placeholder": _PLACEHOLDER_RE.pattern, "evidence_code": _EVIDENCE_CODE_RE.pattern,
        "onsite_ref": ONSITE_REF_RE.pattern,
        # 去标识规则按顺序逐条替换（替换后的文字再查机构名、人名），现场工具照同样顺序执行；kind 为空的只替换不报
        "patterns": [[_DEID_KINDS.get(c, ""), p.pattern, "i" if p.flags & re.I else "", f"【{deid.CATEGORIES[c]}】"]
                     for c, p in deid._PATTERNS],
        "org": {"re": deid._ORG_RE.pattern, "strip": deid._ORG_STRIP_RE.pattern,
                "allow": sorted(deid._ORG_ALLOW), "generic_head": list(deid._ORG_GENERIC_HEAD),
                "tail": ["的", "和", "与", "及", "或"]},
        "person": [deid._PERSON_RE.pattern, deid._PERSON_SHORT_RE.pattern], "person_title": _PERSON_TITLE_RE.pattern,
        "quote": _QUOTE_RE.pattern, "latin": _LATIN_RE.pattern, "version_no": _VERSION_RE.pattern,
        "generic_latin": sorted(GENERIC_LATIN),
    }


def summary(card: SummaryCard) -> dict:
    return {"code": card.code, "kind": "summary_card", "item": card.item, "magnitude": card.magnitude,
            "onsite_refs": card.onsite_refs, "chars": card.char_count,
            "status": "withdrawn" if card.withdrawn else "ready"}


_CITATION_RE = re.compile(r"【依据[：:][^】]*】|【依据待核实】|\[(?:S-\d{2,4}|推断|模型推断)\]|"
                          r"(?<![A-Za-z0-9])[DIOS]-\d{2,4}(?:-\d{2,3})?(?![\d-])|"
                          r"[\[｜|]\s*L[1-5](?:-[A-Z0-9]{2,8}){3,4}-\d{3,5}\s*[\]】]")


def placeholder_problems(text: str) -> list[str]:
    """严格档交付件（占位符版本）的检查：去掉来源标注和依据引用后，按摘要卡规则查真名、IP、系统名、原话。
    机密级由调用方另走 guard（返回打码摘录），这里不重复。"""
    return [k for k in card_problems(_CITATION_RE.sub(" ", text)) if k != "机密级"]


def active_card_codes(db: Session, project: Project) -> set[str]:
    return set(db.scalars(select(SummaryCard.code).where(SummaryCard.project_id == project.id,
                                                         SummaryCard.withdrawn.is_(False))))


STRICT_NOTE = ("严格档项目：只有去标识摘要卡（S-xx），没有资料和纪要原文。输出一律用占位符，"
               "不得补充摘要卡以外的客户事实，缺少的写“待现场补充”")


def read_for_agent(db: Session, project: Project, code: str = "") -> dict:
    """严格档的 project.evidence：不带 code 返回摘要卡清单，带 code 返回该卡全文。撤下的卡不返回内容。"""
    if not code:
        cards = [summary(c) for c in list_cards(db, project) if not c.withdrawn]
        if not cards:
            return {"evidences": [], "note": "严格档项目：还没有可用的摘要卡（客户未同意、已撤回或尚未上传）"}
        return {"evidences": cards, "note": STRICT_NOTE}
    card = get_card(db, project, code)
    if card is None:
        return {"error": f"本项目没有摘要卡 {code}（严格档项目只有 S-xx 摘要卡）"}
    if card.withdrawn:
        return {**summary(card), "text": "", "error": f"{card.code} 已撤下，不能使用"}
    return {**summary(card), "text": card.finding, "note": STRICT_NOTE}
