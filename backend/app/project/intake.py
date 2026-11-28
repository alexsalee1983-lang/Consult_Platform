"""四期 A32：立项与合同要点卡、授权台账与关口 1 授权条件、先行启动（docs/交付链输入输出方案.md 4.1、4.6、4.7、8.2）。

新建项目三步（网页立项带 intake=true 的项目才走这套规则；命令行与旧项目不受影响）：
1. 上传依据：合同，或先行启动时的中标通知书 / 委托函 / 建议书 / 售前沟通纪要 / 客户确认范围的邮件。
   解析与机密级检测复用 evidence.extract_text（严格档拒收，严格档不上传合同、手填要点卡），正文经 storage.py 暂存，
   后台任务 project.contract_card 经 agents/call.py（→ gateway）请交付项目经理 Agent 抽成要点卡（复用 A28② 的做法），
   结果过机密级检测后写进 contract_cards，随后删掉正文。依据不是合同的，整卡 provisional（先行启动 · 待合同确认），
   每个要点的出处标依据名称。
2. 核对要点卡：项目经理逐项标字段状态（已核对 / 合同未写明 / 待与客户澄清 / 合同外新增）后保存为“已核对”；
   要点出现测评、认证、密评结论用语的不可接受，改为差距分析、符合性初判口径才能保存（资质边界）；其余规则检查见 A40。
   已核对的卡不改，补充协议或合同到达时上传出新版本，旧版保留。
3. 授权与成员：授权台账只记签署状态（版本、日期、记录人），追加式留历史；去标识经验积累同意同步 projects.flowback_allowed。

关口 1 进入条件（gate1_missing）：要点卡已核对；附件三与附件一（严格档附件二）已签；先行启动已经部门负责人确认。
未满足时阶段不自动推进出“启动”。授权不先行（data_missing）：附件一、附件三（严格档附件二、附件三）签署前，客户资料、
纪要、自评答卷、摘要卡一律拒收——这条与合同是否签订无关（数据保护红线），立项依据本身不受此限。

先行启动：立项时勾选，部门负责人（项目负责人角色，且不是本项目的项目经理）一键确认；全程提示“先行启动第 N 天 · 合同未签”，
满 EARLY_START_ESCALATE_DAYS 天未签标 overdue（待我处理升级提醒部门负责人）。合同到达：上传合同抽出正式卡后逐字段与临时卡比对，
一致的自动转为已核对；不一致的标“待与客户澄清”，范围、交付件、验收标准有变化的发确认卡，D1 已写的章标“需更新”。
合同未签不能签署 D8、不能登记验收（结项）。

审计只记项目、版本、字数、正文摘要哈希、字段数与状态计数，不记合同原文和要点内容。
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..agents import call
from ..core import audit
from ..core.config import get_settings
from ..core.db import SessionLocal, utcnow
from ..core.permissions import PROJECT_LEAD, PROJECT_MANAGER, membership, roles_of
from ..models import Confirmation, ContractCard, Project, ProjectAuthorization, ProjectMember, User
from ..services import gateway, guard, jobs, storage
from . import evidence

JOB_KIND = "project.contract_card"
AGENT = "delivery-pm"
KINDS = ("docx", "pdf", "markdown", "text")
KINDS_MSG = " Word（.docx）、PDF（文字版）、Markdown（.md）和纯文本（.txt）"
MAX_TEXT_CHARS = 60_000
MAX_VALUE = 2000
UPSTREAM = "合同要点卡变更"
CONTRACT_AGENT = "交付项目经理"

# 要点（4.1）：键、名称、驱动什么（界面提示用）
FIELDS: list[tuple[str, str, str]] = [
    ("goal", "目标", "D1 范围确认单"),
    ("scope", "范围", "D1、调研范围、全程范围检查"),
    ("exclusions", "不包含项", "全程范围检查"),
    ("deliverables", "交付件清单", "输出包组件、质量门禁"),
    ("acceptance", "验收标准", "验收阶段逐条对照"),
    ("period", "周期", "关口日期、进度预测"),
    ("milestones", "里程碑", "关口日期"),
    ("payments", "付款节点", "进度预测"),
    ("tier", "数据处理档位", "立项设置"),
    ("retention", "保留期", "删除计划"),
    ("experience", "是否参与经验积累", "结项复盘入口"),
    ("requirements", "客户书面要求", "项目定制项"),
    ("confidentiality", "保密条款", "项目定制项"),
    ("roles", "双方角色与对接人", "成员、调研受访角色"),
]
FIELD_KEYS = [k for k, _, _ in FIELDS]
FIELD_NAMES = {k: n for k, n, _ in FIELDS}
SCOPE_KEYS = ("goal", "scope", "exclusions", "deliverables", "acceptance")   # 合同到达时有变化发确认卡
STATES = {"verified": "已核对", "unstated": "合同未写明", "clarify": "待与客户澄清", "extra": "合同外新增"}
BASIS_KINDS = {"contract": "合同", "award": "中标通知书", "letter": "委托函", "proposal": "建议书",
               "presales": "售前沟通纪要", "email": "客户确认范围的邮件", "supplement": "补充协议", "manual": "手填"}
CONTRACT_BASES = ("contract", "supplement")

AUTH_KINDS = {"annex3": "附件三合同条款", "annex1": "附件一（标准档）", "annex2": "附件二（严格档）",
              "deid_consent": "去标识经验积累同意（X.4）", "strict_switch": "中途转严格处理通知",
              "deletion_request": "删除请求"}
AUTH_STATUS = {"signed": "已签", "withdrawn": "已撤回"}

TASK = """

---

## 本次任务：从立项依据抽取合同要点卡

下面给你一份立项依据（合同，或合同签订前的中标通知书、委托函、建议书、售前沟通纪要、客户确认范围的邮件）的正文，
已经过机密级检测。只抽取下列要点，不起草任何交付件，不编造依据里没有的内容；依据没写的要点给空字符串。
只输出一个 JSON 对象，键为要点键，值为字符串（多项时每行一项）：
- 不写口令、密钥、IP、个人联系方式；人员只写岗位或角色；
- 不出具或暗示测评、认证、密评结论，合同里有这类字样时照录原意但改写为“差距分析”“符合性初判”。
"""


class IntakeError(Exception):
    def __init__(self, status: int, detail: object) -> None:
        super().__init__(str(detail))
        self.status, self.detail = status, detail


# ---- 查询 ----

def cards(db: Session, p: Project) -> list[ContractCard]:
    return list(db.scalars(select(ContractCard).where(ContractCard.project_id == p.id)
                           .order_by(ContractCard.version.desc())))


def latest(db: Session, p: Project) -> ContractCard | None:
    return db.scalar(select(ContractCard).where(ContractCard.project_id == p.id)
                     .order_by(ContractCard.version.desc()).limit(1))


def _checked(db: Session, p: Project, provisional: bool | None = None) -> ContractCard | None:
    stmt = select(ContractCard).where(ContractCard.project_id == p.id, ContractCard.status == "checked")
    if provisional is not None:
        stmt = stmt.where(ContractCard.provisional.is_(provisional))
    return db.scalar(stmt.order_by(ContractCard.version.desc()).limit(1))


def contract_signed(db: Session, p: Project) -> bool:
    """正式合同（非临时卡）的要点卡已核对。"""
    return _checked(db, p, provisional=False) is not None


def auth_state(db: Session, p: Project) -> dict[str, ProjectAuthorization]:
    """每类授权的当前记录（最新一行）。"""
    rows = db.scalars(select(ProjectAuthorization).where(ProjectAuthorization.project_id == p.id)
                      .order_by(ProjectAuthorization.id))
    return {r.kind: r for r in rows}


def _signed(state: dict[str, ProjectAuthorization], kind: str) -> bool:
    r = state.get(kind)
    return r is not None and r.status == "signed"


def required_auths(p: Project) -> tuple[str, ...]:
    return ("annex3", "annex2") if p.tier == "strict" else ("annex3", "annex1")


def data_missing(db: Session, p: Project) -> list[str]:
    """导入客户资料前必须已签的授权（未签的名称）；旧流程项目（intake=False）不限。"""
    if not p.intake:
        return []
    state = auth_state(db, p)
    return [AUTH_KINDS[k] for k in required_auths(p) if not _signed(state, k)]


def data_refusal(db: Session, p: Project) -> str:
    """不能导入客户资料时的提示；可以导入返回空串。"""
    missing = data_missing(db, p)
    if not missing:
        return ""
    return f"授权台账里{'、'.join(missing)}还没有登记签署，客户资料暂不能导入（只能做不碰客户数据的准备工作）"


def gate1_missing(db: Session, p: Project) -> list[str]:
    """关口 1 进入条件里还差的事；旧流程项目返回空。"""
    if not p.intake:
        return []
    out: list[str] = []
    if _checked(db, p) is None:
        out.append("合同要点卡未核对")
    out += [f"{n}未签" for n in data_missing(db, p)]
    if p.early_start_on is not None and not p.early_start_approved_by and not contract_signed(db, p):
        out.append("先行启动待部门负责人确认")
    return out


def early_start(db: Session, p: Project, today: date | None = None) -> dict | None:
    if p.early_start_on is None:
        return None
    signed = contract_signed(db, p)
    days = ((today or date.today()) - p.early_start_on).days + 1
    limit = get_settings().early_start_escalate_days
    return {"on": p.early_start_on.isoformat(), "days": days, "approved": bool(p.early_start_approved_by),
            "approved_by": p.early_start_approved_by, "contract_signed": signed,
            "overdue": not signed and days >= limit, "escalate_days": limit}


def contract_block(db: Session, p: Project) -> str:
    """合同未签时不能出 D8 验收、不能结项；没有限制返回空串。"""
    if p.intake and p.early_start_on is not None and not contract_signed(db, p):
        return "先行启动的项目合同未签（正式合同的要点卡未核对），不能签署 D8 验收、不能结项"
    return ""


def card_out(c: ContractCard) -> dict:
    fields = c.fields or {}
    return {"id": c.id, "version": c.version, "provisional": c.provisional, "basis_kind": c.basis_kind,
            "basis_name": BASIS_KINDS.get(c.basis_kind, c.basis_kind), "status": c.status, "error": c.error,
            "created_by": c.created_by, "created_at": c.created_at.isoformat() if c.created_at else None,
            "checked_by": c.checked_by, "checked_at": c.checked_at.isoformat() if c.checked_at else None,
            "fields": {k: {"value": "", "state": "", "source": "", **(fields.get(k) or {})} for k in FIELD_KEYS}}


def auth_out(r: ProjectAuthorization) -> dict:
    return {"id": r.id, "kind": r.kind, "name": AUTH_KINDS.get(r.kind, r.kind), "status": r.status,
            "status_name": AUTH_STATUS.get(r.status, r.status), "version": r.version,
            "signed_on": r.signed_on.isoformat(), "recorded_by": r.recorded_by,
            "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None}


def overview(db: Session, p: Project) -> dict:
    from . import cards as card_svc

    history = list(db.scalars(select(ProjectAuthorization).where(ProjectAuthorization.project_id == p.id)
                              .order_by(ProjectAuthorization.id.desc())))
    state = auth_state(db, p)
    consent = card_svc.active_consent(db, p) if p.tier == "strict" else None
    ledger = [{"kind": k, "name": n, "required": k in required_auths(p),
               "current": auth_out(state[k]) if k in state else None}
              for k, n in AUTH_KINDS.items() if not (k == "annex1" and p.tier == "strict")
              and not (k == "annex2" and p.tier != "strict")]
    all_cards = cards(db, p)
    return {
        "intake": p.intake, "tier": p.tier,
        "fields": [{"key": k, "name": n, "drives": d} for k, n, d in FIELDS],
        "states": STATES, "basis_kinds": BASIS_KINDS,
        "card": card_out(all_cards[0]) if all_cards else None,
        "versions": [{"version": c.version, "provisional": c.provisional, "status": c.status,
                      "basis_name": BASIS_KINDS.get(c.basis_kind, c.basis_kind)} for c in all_cards],
        "ledger": ledger, "history": [auth_out(r) for r in history],
        "strict_consent": ({"doc_ref": consent.doc_ref, "signed_on": consent.signed_on.isoformat()}
                           if consent else None),
        "gate1": {"missing": gate1_missing(db, p)}, "data_refusal": data_refusal(db, p),
        "early_start": early_start(db, p),
    }


# ---- 权限 ----

def _require_manager(db: Session, user: User, p: Project, action: str) -> None:
    m = membership(db, p.id, user.id)
    if m is None or m.project_role != PROJECT_MANAGER:
        audit.record(db, actor=user.username, action=action, result="denied", project_code=p.code,
                     reason="not_manager")
        db.commit()
        raise IntakeError(403, "只有本项目的项目经理能维护合同要点卡和授权台账")


def _lock(db: Session, p: Project) -> None:
    db.refresh(p, with_for_update=True)


def _next_version(db: Session, p: Project) -> int:
    return (db.scalar(select(func.max(ContractCard.version)).where(ContractCard.project_id == p.id)) or 0) + 1


# ---- 第一步：上传依据 ----

def upload_basis(db: Session, user: User, p: Project, basis_kind: str, filename: str, data: bytes) -> ContractCard:
    action = "project.contract_card.upload"
    _require_manager(db, user, p, action)
    if not p.intake:
        raise IntakeError(409, "本项目不是按新立项流程建的，合同要点请在 D1 表单里导入")
    if basis_kind not in BASIS_KINDS or basis_kind == "manual":
        raise IntakeError(422, "依据类型只能是：" + "、".join(v for k, v in BASIS_KINDS.items() if k != "manual"))
    if basis_kind not in CONTRACT_BASES and p.early_start_on is None:
        raise IntakeError(422, "合同未签请在立项时选“合同未签，先行启动”；已签合同请上传合同")
    cur = latest(db, p)
    if cur is not None and cur.status == "extracting":
        raise IntakeError(409, "上一份依据还在抽取，请稍候")
    try:
        text = evidence.extract_text(db, user, p, filename, data, action=action, label="立项依据",
                                     kinds=KINDS, kinds_msg=KINDS_MSG, basis=basis_kind)
    except evidence.EvidenceError as e:
        raise IntakeError(e.status, e.detail) from None
    if not storage.enabled():
        raise IntakeError(503, "对象存储未启用，暂时不能导入依据；可以先手填要点卡")
    _lock(db, p)
    prefix = f"{p.code}/contract_card/{uuid.uuid4().hex}/"
    storage.put(evidence.AREA, prefix + "basis.txt", text.encode("utf-8"), "text/plain; charset=utf-8")
    card = ContractCard(project_id=p.id, version=_next_version(db, p), provisional=basis_kind not in CONTRACT_BASES,
                        basis_kind=basis_kind, status="extracting", fields={}, created_by=user.username)
    db.add(card)
    db.flush()
    job_id = jobs.enqueue(db, JOB_KIND, {"project": p.code, "card": card.id, "user": user.username, "prefix": prefix},
                          created_by=user.username, project_code=p.code, max_attempts=2)
    audit.record(db, actor=user.username, action=action, result="queued", project_code=p.code, version=card.version,
                 basis=basis_kind, chars=len(text), digest=evidence.text_digest(text), job=job_id)
    db.commit()
    return card


def _message(text: str) -> str:
    keys = "\n".join(f"- {k}（{n}）" for k, n, _ in FIELDS)
    return "要点：\n" + keys + "\n\n依据正文：\n" + text[:MAX_TEXT_CHARS]


def normalize(data: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in FIELD_KEYS:
        v = data.get(k)
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, list):
            v = "\n".join(" ".join(str(x).split()) for x in v if x not in (None, ""))
        if v is None:
            continue
        v = str(v).strip()
        if v:
            out[k] = v[:MAX_VALUE]
    return out


def _same(a: str, b: str) -> bool:
    return "".join(a.split()) == "".join(b.split())


def _compare(db: Session, p: Project, card: ContractCard, prev: ContractCard) -> list[str]:
    """合同到达：与先行启动的临时卡逐字段比对。一致的转为已核对；不一致的标待澄清，范围类发确认卡。返回不一致的键。"""
    fields, diff = dict(card.fields), []
    for k in FIELD_KEYS:
        new, old = fields.get(k, {}).get("value", ""), (prev.fields or {}).get(k, {}).get("value", "")
        if _same(new, old):
            fields[k] = {**fields.get(k, {"value": new}), "state": "verified" if new else "unstated",
                         "source": "合同（与先行启动卡一致）" if new else "合同"}
            continue
        diff.append(k)
        fields[k] = {**fields.get(k, {"value": new}), "state": "clarify", "source": "合同（与先行启动卡不一致）"}
    card.fields = fields
    scope = [k for k in diff if k in SCOPE_KEYS]
    for k in scope:
        title = f"合同与先行启动要点卡不一致：{FIELD_NAMES[k]}"
        exists = db.scalar(select(Confirmation.id).where(Confirmation.project_id == p.id, Confirmation.status == "open",
                                                         Confirmation.title == title))
        if exists is None:
            db.add(Confirmation(
                project_id=p.id, kind="范围确认", title=title, agent=f"{CONTRACT_AGENT} Agent", agent_code=AGENT,
                options=[{"key": "A", "text": "按正式合同，已做的准备工作按合同调整"},
                         {"key": "B", "text": "保留先行启动时的约定，走补充协议"}],
                recommendation="A", reason="正式合同与先行启动依据在这一要点上不一致",
                impact="影响 D1 范围确认单及下游交付件", deliverable_code="D1" if p.deliverables else "",
                created_by="platform", subject=f"contract_card:{card.version}:{k}"))
    if diff:
        from . import loops
        loops.on_upstream_change(db, p, UPSTREAM, [("D1", None)])
    if not diff:
        card.status, card.checked_by, card.checked_at = "checked", "platform", utcnow()
    return diff


def run(db: Session, user: User, p: Project, card: ContractCard, prefix: str) -> dict:
    text = storage.get(evidence.AREA, prefix + "basis.txt").decode("utf-8")
    res = call.call(db, AGENT, _message(text), actor=user.username, extra=TASK, project=p, parse=call.json_object)
    values = normalize(res.data or {})            # 演示模式没有结构化输出，留空由项目经理手填
    if values and guard.check("\n".join(values.values())).blocked:
        raise jobs.JobError("抽取结果命中机密级检测，未保存", retry=False)
    source = BASIS_KINDS[card.basis_kind]
    card.fields = {k: {"value": values.get(k, ""), "state": "" if values.get(k) else "unstated",
                       "source": source if values.get(k) else ""} for k in FIELD_KEYS}
    card.status = "draft"
    diff: list[str] = []
    prev = db.scalar(select(ContractCard).where(ContractCard.project_id == p.id, ContractCard.id != card.id,
                                                ContractCard.status == "checked", ContractCard.provisional.is_(True))
                     .order_by(ContractCard.version.desc()).limit(1))
    if not card.provisional and prev is not None and _checked(db, p, provisional=False) is None:
        diff = _compare(db, p, card, prev)
    storage.delete(evidence.AREA, prefix + "basis.txt")          # 依据正文不留存
    return {"fields": len(values), "demo": bool(res.completion.demo), "compared": prev is not None and not card.provisional,
            "diff": len(diff), "auto_checked": card.status == "checked"}


@jobs.handler(JOB_KIND)
def _job(payload: dict, ctx: jobs.JobContext) -> None:
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == payload.get("project", "")))
        card = db.get(ContractCard, int(payload.get("card", 0) or 0))
        user = db.scalar(select(User).where(User.username == payload.get("user", "")))
        if p is None or card is None or card.project_id != p.id:
            raise jobs.JobError("项目或要点卡不存在", retry=False)
        if user is None or not user.is_active or membership(db, p.id, user.id) is None:
            card.status, card.error = "failed", "发起人已不是项目成员"
            db.commit()
            raise jobs.JobError("发起人已不是项目成员", retry=False)
        action = "project.contract_card.extract"
        try:
            out = run(db, user, p, card, str(payload.get("prefix", "")))
        except (gateway.GatewayError, call.OutputInvalid, call.AgentUnavailable, call.QualificationBlocked,
                jobs.JobError, storage.StorageError) as e:
            db.rollback()
            card = db.get(ContractCard, card.id)
            card.status, card.error = "failed", "抽取失败，可重新上传依据或直接手填"
            try:
                storage.delete(evidence.AREA, str(payload.get("prefix", "")) + "basis.txt")
            except storage.StorageError:
                pass
            audit.record(db, actor=user.username, action=action, result="error", project_code=p.code,
                         version=card.version, reason=type(e).__name__)
            db.commit()
            raise jobs.JobError(f"抽取失败（{type(e).__name__}），可重新上传或直接手填", retry=False) from e
        audit.record(db, actor=user.username, action=action, result="allowed", project_code=p.code,
                     version=card.version, **out)
        db.commit()


# ---- 第二步：核对要点卡 ----

def save_card(db: Session, user: User, p: Project, fields: dict, checked: bool) -> ContractCard:
    """保存要点卡。没有卡时新建手填卡（严格档、或没有依据文件）；已核对的卡不改。checked=True 时每个要点都要有状态。"""
    from .quality import qualification_matches

    action = "project.contract_card.save"
    _require_manager(db, user, p, action)
    if not p.intake:
        raise IntakeError(409, "本项目不是按新立项流程建的")
    _lock(db, p)
    card = latest(db, p)
    if card is not None and card.status == "extracting":
        raise IntakeError(409, "依据还在抽取，请稍候")
    if card is not None and card.status == "checked":
        raise IntakeError(409, "已核对的要点卡不改；补充协议或合同到达请上传，出新版本")
    clean: dict[str, dict] = {}
    for k in FIELD_KEYS:
        raw = fields.get(k) or {}
        if not isinstance(raw, dict):
            raise IntakeError(422, f"{FIELD_NAMES[k]}格式不对")
        value = str(raw.get("value", "")).strip()
        state = str(raw.get("state", "")).strip()
        if len(value) > MAX_VALUE:
            raise IntakeError(422, f"{FIELD_NAMES[k]}不超过 {MAX_VALUE} 字")
        if state and state not in STATES:
            raise IntakeError(422, "字段状态只能是：" + "、".join(STATES.values()))
        old = ((card.fields if card else {}) or {}).get(k) or {}
        source = old.get("source", "") if old.get("value", "") == value else ("项目经理填写" if value else "")
        clean[k] = {"value": value, "state": state, "source": source}
    text = "\n".join(v["value"] for v in clean.values() if v["value"])
    if text and qualification_matches(text):
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=p.code,
                     reason="qualification")
        db.commit()
        raise IntakeError(422, "不可接受：要点里出现测评、认证或密评结论用语，请改为“差距分析”“符合性初判”“整改建议”的口径后再保存")
    r = guard.check(text) if text else None
    if r is not None and r.blocked:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=p.code, reason="guard",
                     categories=sorted({f.kind for f in r.findings}))
        db.commit()
        raise IntakeError(422, {"blocked": True, "summary": f"{r.summary()}，要点卡没有保存",
                                "findings": [{"kind": f.kind, "label": f.label} for f in r.findings[:10]]})
    if checked:
        lacking = [FIELD_NAMES[k] for k, v in clean.items() if not v["state"]]
        if lacking:
            raise IntakeError(422, "以下要点还没标状态：" + "、".join(lacking))
        empty = [FIELD_NAMES[k] for k, v in clean.items() if v["state"] == "verified" and not v["value"]]
        if empty:
            raise IntakeError(422, "以下要点标了已核对但没有内容，请填写或改标“合同未写明”：" + "、".join(empty))
    if card is None or card.status == "failed":
        provisional = p.early_start_on is not None and not contract_signed(db, p)
        card = ContractCard(project_id=p.id, version=_next_version(db, p), provisional=provisional,
                            basis_kind="manual", status="draft", fields={}, created_by=user.username)
        db.add(card)
    card.fields = clean
    if checked:
        card.status, card.checked_by, card.checked_at = "checked", user.username, utcnow()
    db.flush()
    counts = {s: sum(1 for v in clean.values() if v["state"] == s) for s in STATES}
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=p.code, version=card.version,
                 checked=checked, provisional=card.provisional, chars=len(text), **counts)
    db.commit()
    db.refresh(card)
    return card


# ---- 第三步：授权台账 ----

def record_auth(db: Session, user: User, p: Project, kind: str, status: str, version: str,
                signed_on: date) -> ProjectAuthorization:
    action = "project.authorization.record"
    _require_manager(db, user, p, action)
    if kind not in AUTH_KINDS:
        raise IntakeError(422, "授权类型只能是：" + "、".join(AUTH_KINDS.values()))
    if (kind == "annex1" and p.tier == "strict") or (kind == "annex2" and p.tier != "strict"):
        raise IntakeError(422, "附件一用于标准档，附件二用于严格档")
    if status not in AUTH_STATUS:
        raise IntakeError(422, "状态只能是已签或已撤回")
    version = " ".join(version.split())[:32]
    if version and guard.check(version).blocked:
        raise IntakeError(422, "版本号命中机密级检测，请只写版本编号")
    if signed_on > date.today():
        raise IntakeError(422, "签署日期不能晚于今天")
    _lock(db, p)
    r = ProjectAuthorization(project_id=p.id, kind=kind, status=status, version=version, signed_on=signed_on,
                             recorded_by=user.username)
    db.add(r)
    if kind == "deid_consent":
        p.flowback_allowed = status == "signed"           # 未同意或已撤回：结项复盘不能提交沉淀候选
    db.flush()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=p.code, kind=kind,
                 status=status, signed_on=signed_on.isoformat())
    db.commit()
    db.refresh(r)
    return r


# ---- 先行启动 ----

def approve_early_start(db: Session, user: User, p: Project) -> Project:
    """部门负责人一键确认先行启动：须持项目负责人角色，且不是本项目的项目经理（不签合同就投入人力，须由人担责）。"""
    action = "project.early_start.approve"
    if PROJECT_LEAD not in roles_of(db, user.id):
        audit.record(db, actor=user.username, action=action, result="denied", project_code=p.code, reason="not_lead")
        db.commit()
        raise IntakeError(403, "先行启动须由部门负责人（项目负责人角色）确认")
    m = membership(db, p.id, user.id)
    if m is not None and m.project_role == PROJECT_MANAGER:
        audit.record(db, actor=user.username, action=action, result="denied", project_code=p.code,
                     reason="self_approve")
        db.commit()
        raise IntakeError(403, "项目经理不能确认自己项目的先行启动，请由部门负责人确认")
    if p.early_start_on is None:
        raise IntakeError(409, "本项目没有申请先行启动")
    if p.early_start_approved_by:
        raise IntakeError(409, "先行启动已确认")
    p.early_start_approved_by, p.early_start_approved_at = user.username, utcnow()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=p.code)
    db.commit()
    return p


def pending_early_starts(db: Session) -> list[dict]:
    """待部门负责人确认或合同超期未签的先行启动项目（只给项目负责人看，只含元数据）。"""
    rows = db.scalars(select(Project).where(Project.early_start_on.is_not(None), Project.deleted_at.is_(None))
                      .order_by(Project.early_start_on))
    out = []
    for p in rows:
        st = early_start(db, p)
        if st is None or (st["approved"] and not st["overdue"]) or st["contract_signed"]:
            continue
        mgr = db.scalar(select(User.username).join(ProjectMember, ProjectMember.user_id == User.id)
                        .where(ProjectMember.project_id == p.id, ProjectMember.project_role == PROJECT_MANAGER)
                        .limit(1))
        out.append({"code": p.code, "service": p.service, "manager": mgr or "", **st})
    return out


def delete_for_project(db: Session, pid: int) -> int:
    """删除项目时清掉要点卡（含合同要点）；授权台账只有签署状态，留作删除证明的依据。"""
    from sqlalchemy import delete

    return db.execute(delete(ContractCard).where(ContractCard.project_id == pid)).rowcount or 0

