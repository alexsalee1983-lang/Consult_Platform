"""质量门禁与签署（M3 ⑤）。

质量评审（agents/roles/qa-reviewer.md；《Agent组开发项目方案》“每步之后”）：
- 项目成员发起，后台任务 quality.review 执行（可重复执行：开头看状态，不是 queued 就不做）。
  评审的是发起时交付件的修订（revision）和各章版本号；评审期间有人改了正文也按发起时的版本评，
  评完之后修订号对不上就算“过期”，门禁要求重新评审。
- 平台硬规则先查（结果并入问题清单，by = rule）：
  章节没有正文 → 阻断；正文命中机密级检测 → 阻断（此时不发往模型，只出硬规则结果）；
  出现测评 / 认证结论用语（资质边界）→ 阻断；来源标注核对不上（证据编号不存在、知识库条目不存在或已下线）→ 阻断；
  整章只有 [推断]、没有证据和知识库来源 → 重要。
- 再由质量评审与异议应答 Agent 经网关评审（平台按发起人的成员身份把正文交给它，它没有 doc.generate 授权）。
  解析输出的问题表（严重度 阻断 / 重要 / 建议）和“统计：阻断 X 项，重要 X 项，建议 X 项”；
  两者不一致取较大值。评审结论由平台按规则重算，不采信模型自报：
  有阻断 → 不通过（fail）；没有阻断但重要 ≥ 3 → 修改后通过（revise）；其他 → 通过（pass）。
- 输出格式不对、模型暂不可用按任务重试（最多 MAX_ATTEMPTS 次）；机密级拦截、未配置模型直接失败。

签署流程（设计稿 04：Agent 起草 → 质量评审 → 顾问审核 → 项目负责人签署）：
- 提交签署（submit）：项目成员；门禁 = 每章都有正文 + 最近一次评审已完成、针对当前修订、阻断为 0。状态 → review。
- 签署（sign）：项目经理；交付件在 review 且修订号与提交时一致，门禁再查一次；项目经理可以签自己提交的（负责人 2026-11-12 定，不设四眼限制）。
  状态 → signed，之后交付件只读（写入 409、交付链不跑），导出不再带草稿页眉。
- 退回（return）：项目经理，必须写理由；状态 → drafting。
- review 状态下任何章节或大纲写入，都把状态改回 drafting（提交作废，要重新评审、重新提交）。
- 签署后不提供撤销（一期）；需要改动时由负责人另行决定。
- 严格档一律 422（M4 接入）。

驳回阻断（负责人 2026-11-12 定，迁移 0022）：AI 给意见、人来定夺。
- 项目经理可以逐条驳回质量评审 Agent（by = agent）提出的阻断问题，必须写理由；只追加，不能撤销（要恢复就重新评审）。
- 平台硬规则（by = rule）判出的阻断不能驳回：缺正文、机密级、测评 / 认证结论用语、来源核对不上。
- 只能驳回最近一次已完成、且针对当前修订的评审；交付件一改，新评审的阻断要重新处理。
- Agent 统计的阻断数比问题清单里列出的多（没法逐条驳回）时，多出的部分照样算阻断，要重新评审。
- 门禁用“未驳回的阻断”判断；驳回记录在签署流程里显示，提交、签署的审计记下驳回条数。

审计只记交付件编号、修订号、各章版本号、评审编号、问题计数、结论和字数，不记正文、问题描述、审核意见和驳回理由。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import knowledge, runtime, tools
from ..core import audit
from ..core.db import SessionLocal, utcnow
from ..core.permissions import PROJECT_MANAGER, membership
from ..models import Deliverable, Project, QualityReview, ReviewWaiver, SignOff, User
from ..services import gateway, guard, jobs, usage
from . import deliverables

AGENT = "qa-reviewer"
JOB_KIND = "quality.review"
MAX_ATTEMPTS = 3
ACTIVE = ("queued", "running")
INPUT_CHARS = 40000
MAX_ISSUES = 100
MAX_COMMENT = 2000
SEVERITIES = ("阻断", "重要", "建议")
CONCLUSIONS = {"pass": "通过", "revise": "修改后通过", "fail": "不通过"}
STRICT_REFUSAL = deliverables.STRICT_REFUSAL

# 资质边界：测评 / 认证结论用语。前面紧跟否定说明（“不构成测评结论”）的不算。
# 级别可在前可在后：“通过等保三级测评”“通过三级等保测评”“等保三级达标”“三级等保合格”（测试 #44）。
_LEVEL = r"(?:第?[一二三四1-4]\s*级)?"
_QUALIF = re.compile(r"(?:测评|认证|密评)结论"
                     rf"|通过了?{_LEVEL}(?:等保|等级保护|密评|商用密码应用安全性评估)"
                     r"|(?:测评|认证|密评)(?:合格|通过)"
                     rf"|{_LEVEL}(?:等保|等级保护){_LEVEL}(?:测评)?(?:达标|合格)")
_NEGATION = re.compile(r"(?:不构成|不出具|不作为|并非|不代表|不是|不得|不涉及|不含|不提供)[^。；\n]{0,6}$")
_STATS = re.compile(r"阻断\s*(\d+)\s*项.{0,10}?重要\s*(\d+)\s*项.{0,10}?建议\s*(\d+)\s*项")

INSTRUCTIONS = (
    "任务：对下面这份交付件做质量评审（按你的 9 个评审维度和严重度定义）。交付件正文属于本项目，"
    "来源标注 [I-xx] [D-xx] [O-xx] 是客户证据编号，[L…] 是知识库条目，[推断] 是模型推断。\n"
    "输出格式（平台据此解析，必须遵守）：\n"
    "1. 第一部分“# 评审意见”，写“评审结论：通过 / 修改后通过 / 不通过”和“统计：阻断 X 项，重要 X 项，建议 X 项”。\n"
    "2. 问题清单用 Markdown 表格，列依次为：# | 位置 | 维度 | 严重度 | 问题 | 修改建议；严重度只能写 阻断、重要、建议。\n"
    "3. 然后是挑战预判表（挑战者角色 | 可能的问题 | 当前稿件能否应对 | 建议补充），列出最可能的 5 个。\n"
    "4. 问题描述只写位置和问题，不要大段照抄正文；只写客户代号，不写客户真实名称、人名、口令、IP 等机密级内容。"
)


class QualityError(ValueError):
    def __init__(self, status: int, detail):
        super().__init__(detail if isinstance(detail, str) else "被拒绝")
        self.status = status
        self.detail = detail


class ReviewRetry(Exception):
    """暂时失败（模型不可用、输出格式不对），按任务重试。"""


class ReviewFailed(Exception):
    """失败、不再重试。message 只写类别，不含客户原文。"""


@dataclass
class Gate:
    ok: bool
    reasons: list[str]
    review: QualityReview | None
    waived: int = 0


@dataclass
class Blocking:
    rule: int           # 平台硬规则判出的阻断（不能驳回）
    agent_open: int     # 评审 Agent 提出、还没驳回的阻断
    unlisted: int       # Agent 统计了但问题清单没列出的阻断（不能逐条驳回）
    waived: int         # 已驳回的阻断

    @property
    def remaining(self) -> int:
        return self.rule + self.agent_open + self.unlisted


# ---- 公共 ----

def _snapshot(d: Deliverable) -> dict:
    return {str(s.seq): s.current_version for s in d.sections}


def _is_manager(db: Session, project: Project, user: User) -> bool:
    m = membership(db, project.id, user.id)
    return m is not None and m.project_role == PROJECT_MANAGER


def _refuse(db: Session, user: User, project: Project, d: Deliverable, action: str, reason: str,
            status: int, detail) -> None:
    audit.record(db, actor=user.username, action=action, result="blocked" if status != 403 else "denied",
                 project_code=project.code, deliverable=d.code, reason=reason)
    db.commit()
    raise QualityError(status, detail)


def latest_review(db: Session, d: Deliverable) -> QualityReview | None:
    return db.scalar(select(QualityReview).where(QualityReview.deliverable_id == d.id)
                     .order_by(QualityReview.id.desc()).limit(1))


def latest_done_review(db: Session, d: Deliverable) -> QualityReview | None:
    return db.scalar(select(QualityReview).where(QualityReview.deliverable_id == d.id, QualityReview.status == "done")
                     .order_by(QualityReview.id.desc()).limit(1))


def list_reviews(db: Session, d: Deliverable) -> list[QualityReview]:
    return list(db.scalars(select(QualityReview).where(QualityReview.deliverable_id == d.id)
                           .order_by(QualityReview.id.desc())))


def list_signoffs(db: Session, d: Deliverable) -> list[SignOff]:
    return list(db.scalars(select(SignOff).where(SignOff.deliverable_id == d.id).order_by(SignOff.id)))


def list_waivers(db: Session, r: QualityReview) -> list[ReviewWaiver]:
    return list(db.scalars(select(ReviewWaiver).where(ReviewWaiver.review_id == r.id).order_by(ReviewWaiver.id)))


def waivable(issue: dict) -> bool:
    return issue.get("by") == "agent" and issue.get("severity") == "阻断"


def blocking_of(db: Session, r: QualityReview) -> Blocking:
    issues = r.issues or []
    rule = sum(1 for i in issues if i.get("by") == "rule" and i.get("severity") == "阻断")
    listed = {n for n, i in enumerate(issues) if waivable(i)}
    agent = max(r.blocking - rule, 0)
    waived = sum(1 for w in list_waivers(db, r) if w.issue_index in listed)
    return Blocking(rule=rule, agent_open=len(listed) - waived, unlisted=max(agent - len(listed), 0), waived=waived)


def gate(db: Session, d: Deliverable) -> Gate:
    """提交签署 / 签署的门禁。reasons 为空才放行。"""
    reasons: list[str] = []
    waived = 0
    if not d.sections:
        reasons.append("还没有大纲和正文")
    empty = [str(s.seq) for s in d.sections if not s.current_version]
    if empty:
        reasons.append(f"第 {'、'.join(empty)} 章还没有正文")
    r = latest_done_review(db, d)
    if r is None:
        reasons.append("还没有完成的质量评审")
    else:
        if r.revision != d.revision:
            reasons.append(f"质量评审针对 v{r.revision}，交付件已改到 v{d.revision}，请重新评审")
        b = blocking_of(db, r)
        if b.rule:
            reasons.append(f"平台规则判出 {b.rule} 项阻断问题，修改后重新评审")
        if b.agent_open:
            reasons.append(f"评审 Agent 提出 {b.agent_open} 项阻断问题未处理：修改后重新评审，或由项目经理写理由驳回")
        if b.unlisted:
            reasons.append(f"评审 Agent 统计的阻断比问题清单多 {b.unlisted} 项，无法逐条驳回，请重新评审")
        waived = b.waived
    return Gate(ok=not reasons, reasons=reasons, review=r, waived=waived)


# ---- 质量评审：发起 ----

def request_review(db: Session, user: User, project: Project, d: Deliverable) -> QualityReview:
    action = "quality.request"
    if project.tier == "strict":
        _refuse(db, user, project, d, action, "strict_tier", 422, STRICT_REFUSAL)
    if d.status == "signed":
        _refuse(db, user, project, d, action, "signed", 409, f"{d.code} 已签署，不再评审")
    if not any(s.current_version for s in d.sections):
        _refuse(db, user, project, d, action, "empty", 409, f"{d.code} 还没有正文，不能评审")
    active = db.scalar(select(QualityReview.id).where(QualityReview.deliverable_id == d.id,
                                                      QualityReview.status.in_(ACTIVE)))
    if active is not None:
        raise QualityError(409, f"{d.code} 已有进行中的质量评审（#{active}），请等它结束")
    try:
        runtime.ready_doc(db, AGENT)
    except runtime.AgentNotReady as e:
        raise QualityError(503, str(e)) from e
    r = QualityReview(project_id=project.id, deliverable_id=d.id, revision=d.revision, versions=_snapshot(d),
                      status="queued", agent_code=AGENT, created_by=user.username)
    db.add(r)
    db.flush()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 deliverable=d.code, review_id=r.id, revision=d.revision, versions=r.versions)
    db.commit()
    jobs.enqueue(db, JOB_KIND, {"review_id": r.id}, created_by=user.username, project_code=project.code,
                 max_attempts=MAX_ATTEMPTS)
    db.refresh(r)
    return r


# ---- 质量评审：硬规则 ----

def _issue(severity: str, location: str, dimension: str, problem: str, suggestion: str, by: str) -> dict:
    return {"severity": severity, "location": location[:100], "dimension": dimension[:40],
            "problem": problem[:500], "suggestion": suggestion[:500], "by": by}


def qualification_hits(text: str) -> int:
    n = 0
    for m in _QUALIF.finditer(text):
        if not _NEGATION.search(text[max(0, m.start() - 16):m.start()]):
            n += 1
    return n


def rule_issues(d: Deliverable, versions: dict) -> tuple[list[dict], bool]:
    """返回 (问题清单, 是否命中机密级)。只用标注和类别描述，不摘正文。"""
    issues: list[dict] = []
    confidential = False
    for s in d.sections:
        loc = f"第 {s.seq} 章 {s.title}"
        ver = int(versions.get(str(s.seq), 0) or 0)
        v = deliverables.get_version(s, ver) if ver else None
        if v is None:
            issues.append(_issue("阻断", loc, "完整性", "本章还没有正文", "补写本章或从大纲中去掉", "rule"))
            continue
        g = guard.check(v.body)
        if g.blocked:
            confidential = True
            kinds = "、".join(sorted({f.label for f in g.findings}))
            issues.append(_issue("阻断", loc, "保密", f"命中机密级检测（{kinds}）", "删除机密级内容后重新评审", "rule"))
        q = qualification_hits(v.body)
        if q:
            issues.append(_issue("阻断", loc, "范围与资质边界", f"出现测评 / 认证结论用语 {q} 处",
                                 "改用“差距分析、符合性初判、整改建议”的说法", "rule"))
        src = v.sources or {}
        unresolved = src.get("unresolved", [])
        if unresolved:
            issues.append(_issue("阻断", loc, "证据链 / 依据", f"来源标注核对不上：{'、'.join(unresolved[:10])}",
                                 "核对证据编号和知识库条目编号，改正或删除", "rule"))
        counts = src.get("counts", {})
        if counts.get("inferred", 0) and not counts.get("evidence", 0) and not counts.get("knowledge", 0):
            issues.append(_issue("重要", loc, "证据链", "本章结论只有推断，没有客户证据或知识库来源",
                                 "补充证据编号或依据，或明确标注为假设", "rule"))
    return issues, confidential


# ---- 质量评审：解析 Agent 输出 ----

def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_review(text: str) -> tuple[list[dict], dict[str, int]] | None:
    """解析问题表和统计行。格式完全对不上（没有表格行、没有统计、没有评审结论）返回 None。"""
    issues: list[dict] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _cells(line)
        if len(cells) >= 6 and cells[3] in SEVERITIES:
            issues.append(_issue(cells[3], cells[1], cells[2], cells[4], cells[5], "agent"))
        elif len(cells) >= 4:
            sev = next((c for c in cells if c in SEVERITIES), "")
            if sev:
                rest = [c for c in cells if c != sev]
                issues.append(_issue(sev, rest[1] if len(rest) > 1 else "", "", " ".join(rest[2:]), "", "agent"))
    stated = {}
    m = _STATS.search(text)
    if m:
        stated = dict(zip(SEVERITIES, (int(x) for x in m.groups()), strict=True))
    if not issues and not stated and "评审结论" not in text:
        return None
    counts = {s: max(sum(1 for i in issues if i["severity"] == s), stated.get(s, 0)) for s in SEVERITIES}
    return issues[:MAX_ISSUES], counts


def conclude(blocking: int, major: int) -> str:
    if blocking:
        return "fail"
    return "revise" if major >= 3 else "pass"


def _text_for_review(d: Deliverable, versions: dict) -> str:
    parts = []
    for s in d.sections:
        ver = int(versions.get(str(s.seq), 0) or 0)
        v = deliverables.get_version(s, ver) if ver else None
        parts.append(f"## 第 {s.seq} 章 {s.title}（v{ver}）\n{v.body if v else '（本章还没有正文）'}")
    return "\n\n".join(parts)[:INPUT_CHARS]


# ---- 质量评审：后台任务 ----

def _lock(db: Session, r: QualityReview) -> str:
    db.refresh(r, with_for_update=True)
    return r.status


@jobs.handler(JOB_KIND)
def _review_job(payload: dict, ctx: jobs.JobContext) -> None:
    with SessionLocal() as db:
        r = db.get(QualityReview, int(payload["review_id"]))
        if r is None or _lock(db, r) not in ACTIVE:
            db.commit()
            return
        project = db.get(Project, r.project_id)
        d = db.get(Deliverable, r.deliverable_id)
        user = db.scalar(select(User).where(User.username == r.created_by))
        if user is None or not user.is_active or membership(db, project.id, user.id) is None:
            _finish_failed(db, r, project, d, "发起人已不是项目成员或账号已停用")
            return
        r.status = "running"
        db.commit()
        try:
            _execute(db, user, project, d, r)
        except ReviewRetry as e:
            db.rollback()
            if ctx.attempt >= MAX_ATTEMPTS:
                _finish_failed(db, r, project, d, str(e))
                raise jobs.JobError(str(e), retry=False) from e
            _lock(db, r)
            r.status = "queued"
            db.commit()
            raise jobs.JobError(str(e)) from e
        except ReviewFailed as e:
            db.rollback()
            _finish_failed(db, r, project, d, str(e))
            raise jobs.JobError(str(e), retry=False) from e
        except Exception as e:  # noqa: BLE001  意外错误：按任务重试，到上限时标为失败，不让它停在 running
            db.rollback()
            if ctx.attempt >= MAX_ATTEMPTS:
                _finish_failed(db, r, project, d, f"内部错误（{type(e).__name__}）")
                raise jobs.JobError(type(e).__name__, retry=False) from e
            _lock(db, r)
            r.status = "queued"
            db.commit()
            raise


def _finish_failed(db: Session, r: QualityReview, project: Project, d: Deliverable, reason: str) -> None:
    _lock(db, r)
    r.status, r.error, r.finished_at = "failed", reason[:200], utcnow()
    audit.record(db, actor="worker", action="quality.review", result="error", project_code=project.code,
                 deliverable=d.code, review_id=r.id, reason=reason[:200])
    db.commit()


def _execute(db: Session, user: User, project: Project, d: Deliverable, r: QualityReview) -> None:
    issues, confidential = rule_issues(d, r.versions)
    report, counts = "", {s: 0 for s in SEVERITIES}
    if confidential:
        report = "（交付件正文命中机密级检测，未发往模型评审。请先处理阻断项，再重新评审。）"
    else:
        try:
            doc = runtime.ready_doc(db, AGENT)
            card = runtime.project_card(db, user, AGENT, project.code)
        except runtime.AgentNotReady as e:
            raise ReviewFailed("质量评审 Agent 暂不可用") from e
        except tools.ToolError as e:
            raise ReviewFailed(f"读项目卡被拒绝（{e.status}）") from e
        text = _text_for_review(d, r.versions)
        kb_block, _ = knowledge.retrieve(db, user, AGENT, project, f"{d.title} 质量评审 依据 {text[:200]}")
        message = f"{INSTRUCTIONS}\n\n# 交付件 {d.code} {d.title}（修订 v{r.revision}）\n\n{text}"
        try:
            c = gateway.complete(message, tier=project.tier, project_code=project.code, level=doc.level,
                                 system=runtime.system_prompt(doc, card, kb_block))
        except gateway.BlockedError as e:
            raise ReviewFailed("输入命中机密级内容，未发往模型") from e
        except gateway.ExternalModelForbidden as e:
            raise ReviewFailed("当前配置不允许把本项目发往外部模型") from e
        except gateway.GatewayError as e:
            usage.record(db, username=user.username, project_code=project.code, level=doc.level, model="",
                         status="error", stream=False, agent_code=AGENT)
            db.commit()
            raise ReviewRetry("模型暂不可用") from e
        if c.demo:
            raise ReviewFailed("未配置大模型，不能做质量评审")
        usage.record(db, username=user.username, project_code=project.code, level=doc.level, model=c.model,
                     status="ok", stream=False, completion=c, agent_code=AGENT)
        db.commit()
        if guard.check(c.text).blocked:
            raise ReviewFailed("模型输出命中机密级内容，未保存")
        parsed = parse_review(c.text)
        if parsed is None:
            raise ReviewRetry("评审输出格式不对")
        agent_issues, counts = parsed
        issues += agent_issues
        report = c.text[:INPUT_CHARS]
        r.prompt_version, r.model = doc.version, c.model[:64]
        r.prompt_tokens, r.completion_tokens = c.prompt_tokens, c.completion_tokens
    rule_counts = {s: sum(1 for i in issues if i["by"] == "rule" and i["severity"] == s) for s in SEVERITIES}
    blocking, major, minor = (rule_counts[s] + counts[s] for s in SEVERITIES)
    if _lock(db, r) != "running":
        db.commit()
        return
    r.issues, r.report = issues[:MAX_ISSUES], report
    r.blocking, r.major, r.minor = blocking, major, minor
    r.conclusion, r.status, r.finished_at = conclude(blocking, major), "done", utcnow()
    audit.record(db, actor="worker", action="quality.review", result="allowed", project_code=project.code,
                 deliverable=d.code, review_id=r.id, revision=r.revision, conclusion=r.conclusion,
                 blocking=blocking, major=major, minor=minor, rule_issues=sum(rule_counts.values()),
                 length=len(report), prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens)
    db.commit()


# ---- 签署流程 ----

def _comment(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_COMMENT:
        raise QualityError(422, f"意见不超过 {MAX_COMMENT} 字")
    if text and guard.check(text).blocked:
        raise QualityError(422, "意见命中机密级检测，没有保存")
    return text


def _lock_deliverable(db: Session, d: Deliverable) -> None:
    db.refresh(d, with_for_update=True)


def _last_submit(db: Session, d: Deliverable) -> SignOff | None:
    return db.scalar(select(SignOff).where(SignOff.deliverable_id == d.id, SignOff.action == "submit")
                     .order_by(SignOff.id.desc()).limit(1))


def submit(db: Session, user: User, project: Project, d: Deliverable, comment: str = "") -> SignOff:
    """顾问审核完成，提交签署。"""
    action = "deliverable.submit"
    comment = _comment(comment)
    _lock_deliverable(db, d)
    if project.tier == "strict":
        _refuse(db, user, project, d, action, "strict_tier", 422, STRICT_REFUSAL)
    if d.status == "signed":
        _refuse(db, user, project, d, action, "signed", 409, f"{d.code} 已签署")
    if d.status == "review":
        _refuse(db, user, project, d, action, "already_submitted", 409, f"{d.code} 已提交签署，等待项目经理签署或退回")
    g = gate(db, d)
    if not g.ok:
        _refuse(db, user, project, d, action, "gate", 409, {"gate": False, "reasons": g.reasons})
    s = SignOff(project_id=project.id, deliverable_id=d.id, action="submit", revision=d.revision,
                versions=_snapshot(d), review_id=g.review.id, comment=comment, actor=user.username)
    db.add(s)
    d.status = "review"
    db.flush()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 deliverable=d.code, revision=d.revision, versions=s.versions, review_id=g.review.id,
                 major=g.review.major, waived=g.waived, comment_length=len(comment))
    db.commit()
    db.refresh(s)
    return s


def sign(db: Session, user: User, project: Project, d: Deliverable, comment: str = "") -> SignOff:
    """项目经理签署。"""
    action = "deliverable.sign"
    comment = _comment(comment)
    if not _is_manager(db, project, user):
        _refuse(db, user, project, d, action, "not_manager", 403, "只有项目经理能签署")
    _lock_deliverable(db, d)
    if project.tier == "strict":
        _refuse(db, user, project, d, action, "strict_tier", 422, STRICT_REFUSAL)
    if d.status != "review":
        _refuse(db, user, project, d, action, "not_submitted", 409,
                f"{d.code} 已签署" if d.status == "signed" else f"{d.code} 还没有提交签署")
    sub = _last_submit(db, d)
    if sub is None or sub.revision != d.revision:
        _refuse(db, user, project, d, action, "stale", 409, f"{d.code} 提交后有修改，请重新评审并提交")
    g = gate(db, d)
    if not g.ok:
        _refuse(db, user, project, d, action, "gate", 409, {"gate": False, "reasons": g.reasons})
    s = SignOff(project_id=project.id, deliverable_id=d.id, action="sign", revision=d.revision,
                versions=_snapshot(d), review_id=g.review.id, comment=comment, actor=user.username)
    db.add(s)
    d.status, d.updated_at, d.updated_by = "signed", utcnow(), user.username
    db.flush()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 deliverable=d.code, revision=d.revision, versions=s.versions, review_id=g.review.id,
                 submit_id=sub.id, waived=g.waived, comment_length=len(comment))
    db.commit()
    db.refresh(s)
    return s


def send_back(db: Session, user: User, project: Project, d: Deliverable, comment: str) -> SignOff:
    """项目经理退回修改（必须写理由）。"""
    action = "deliverable.return"
    comment = _comment(comment)
    if not comment:
        raise QualityError(422, "退回要写理由")
    if not _is_manager(db, project, user):
        _refuse(db, user, project, d, action, "not_manager", 403, "只有项目经理能退回")
    _lock_deliverable(db, d)
    if d.status != "review":
        _refuse(db, user, project, d, action, "not_submitted", 409, f"{d.code} 不在待签署状态")
    s = SignOff(project_id=project.id, deliverable_id=d.id, action="return", revision=d.revision,
                versions=_snapshot(d), comment=comment, actor=user.username)
    db.add(s)
    d.status = "drafting"
    db.flush()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 deliverable=d.code, revision=d.revision, comment_length=len(comment))
    db.commit()
    db.refresh(s)
    return s


def waive(db: Session, user: User, project: Project, d: Deliverable, r: QualityReview, index: int,
          reason: str) -> ReviewWaiver:
    """项目经理驳回评审 Agent 提出的一条阻断问题（必须写理由）。"""
    action = "quality.waive"
    reason = _comment(reason)
    if not reason:
        raise QualityError(422, "驳回要写理由")
    if not _is_manager(db, project, user):
        _refuse(db, user, project, d, action, "not_manager", 403, "只有项目经理能驳回阻断问题")
    _lock_deliverable(db, d)
    if project.tier == "strict":
        _refuse(db, user, project, d, action, "strict_tier", 422, STRICT_REFUSAL)
    if d.status == "signed":
        _refuse(db, user, project, d, action, "signed", 409, f"{d.code} 已签署")
    latest = latest_done_review(db, d)
    if latest is None or latest.id != r.id or r.revision != d.revision:
        _refuse(db, user, project, d, action, "stale", 409, "只能驳回最近一次、针对当前修订的质量评审中的问题")
    issues = r.issues or []
    if not 0 <= index < len(issues):
        raise QualityError(404, "问题不存在")
    if not waivable(issues[index]):
        reason_code = "rule_issue" if issues[index].get("by") == "rule" else "not_blocking"
        _refuse(db, user, project, d, action, reason_code, 422,
                "平台规则判出的阻断不能驳回，请修改正文后重新评审" if reason_code == "rule_issue"
                else "只有阻断问题需要驳回")
    if db.scalar(select(ReviewWaiver.id).where(ReviewWaiver.review_id == r.id, ReviewWaiver.issue_index == index)):
        raise QualityError(409, "这条问题已经驳回过")
    w = ReviewWaiver(project_id=project.id, review_id=r.id, issue_index=index, reason=reason, actor=user.username)
    db.add(w)
    db.flush()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 deliverable=d.code, review_id=r.id, revision=r.revision, issue=index,
                 reason_length=len(reason))
    db.commit()
    db.refresh(w)
    return w
