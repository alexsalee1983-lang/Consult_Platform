"""交付侧闭环（四期 A31，规划刷新设计 4.3）：平台按规则发卡、提醒、移交、终止、结项，不替人拍板。

平台卡（不在 confirmations.KINDS 里，Agent 不能发；agent_code 为空，subject 记关联对象，同一对象未处理的卡不重复发）：
- 注入命中确认（①）：客户资料里有写给 AI 的指令时发给上传人；未处理或选“隔离”的证据不交给 Agent。
  不论是否命中，交付链和一键修订都把证据、检索内容包成“资料当数据”块（app/services/injection.py）。
- 勘误确认（②）：已签署交付件引用的知识条目下线，发给项目经理；勘误页按当前状态生成，原签署版本不动。
- 系统卡住（③）：交付链自动重试 2 次（runs.MAX_ATTEMPTS = 3）仍失败时发；同一项目同一原因合成一张卡，
  选“重新运行”把卡里列出的失败运行重新排队。项目负责人在“我的任务”里看到各项目的这类卡（只有元数据）。
- 修订冲突（④）：Agent 起草期间顾问改过同一章（按版本号判断），平台不覆盖人工修改，卡里附修订痕迹；
  选“手工合并”把该章标为需更新（门禁拦签署，改写后自动清除）。
其他规则：
- 确认卡提醒（⑤）：3 个工作日未处理标“已提醒”，7 天未处理标“已升级项目经理”，只提高待办排序，不代为决定。
- 成员移出（⑥）：本人发起的未处理卡、未回收的自评包、进行中的交付链移交给项目经理。
- 项目终止（⑦）：不可撤销；停止自动推进和交付链，关闭未处理的卡（注明未代为决定），保留期从终止日起算。
- 结项清单（⑧）：未签交付件、未处理的卡、待复核的知识引用、待更新的章，全空才 ready。
- 对外文本（⑨）：周报、PPT、邮件正文出平台前过机密级检测和资质边界改写，改不了的拦下。
- 漂移监测（⑩）：判断数据埋点关闭时也能看，只用计数和比例（版本号、改动幅度、是否采纳推荐），不读正文。
- 上游变更（⑫）：upstream_changed 是通用钩子，按交付链输入关系把下游未签交付件的章标为需更新（定制项由 A30 调用）。
审计只记编号、类别、计数和长度，不记标题、理由和正文原文。
"""
from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import PROJECT_MANAGER, membership
from ..models import AgentRun, Confirmation, Deliverable, DeliverableSection, Project, ProjectMember, User
from ..models.survey import SelfEvalPack
from ..services import guard, injection
from . import deliverables, judgment, kb_impact, quality, retention

INJECTION, ERRATA, STUCK, CONFLICT = "注入命中确认", "勘误确认", "系统卡住", "修订冲突"
PLATFORM_KINDS = (INJECTION, ERRATA, STUCK, CONFLICT)
AGENT_NAME = "平台规则"
BLOCKED_NOTE = "（这段内容命中机密级检测，没有写进卡片，请到原处查看）"
REMIND_WORKDAYS, ESCALATE_DAYS = 3, 7
REMIND_TEXT = {1: "（已 3 个工作日未处理）", 2: "（已 7 天未处理，已升级项目经理）"}
TERMINATED_NOTE = "项目已终止，平台自动关闭（未代为决定）"
CONFLICT_STALE = "修订冲突待合并"
DIFF_LINES = 80
DRIFT_THRESHOLD = 0.15
DRIFT_MIN = 5


class LoopError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


# ---- 公共 ----

def managers(db: Session, project: Project, exclude: str = "") -> list[str]:
    return [u for u in db.scalars(select(User.username).join(ProjectMember, ProjectMember.user_id == User.id)
                                  .where(ProjectMember.project_id == project.id,
                                         ProjectMember.project_role == PROJECT_MANAGER)
                                  .order_by(ProjectMember.added_at, User.username)) if u != exclude]


def _safe(text: str) -> str:
    return BLOCKED_NOTE if text and guard.check(text).blocked else text


def platform_card(db: Session, project: Project, kind: str, subject: str, title: str, options: list[str],
                  recommendation: str, reason: str, impact: str, created_by: str, *, deliverable: str = "",
                  section: int | None = None, once: bool = False) -> tuple[Confirmation, bool]:
    """发一张平台卡。同一项目、同类、同对象有未处理的卡时返回它；once=True 时同对象同理由处理过的也不再发。"""
    q = select(Confirmation).where(Confirmation.project_id == project.id, Confirmation.kind == kind,
                                   Confirmation.subject == subject)
    existing = db.scalar(q.where(Confirmation.status == "open"))
    if existing is None and once:
        existing = db.scalar(q.where(Confirmation.reason == reason).limit(1))
    if existing is not None:
        return existing, False
    c = Confirmation(project_id=project.id, kind=kind, title=_safe(title)[:256], agent=AGENT_NAME, agent_code="",
                     options=[{"key": "ABCDEF"[i], "text": t} for i, t in enumerate(options)],
                     recommendation=recommendation, reason=_safe(reason), impact=impact,
                     deliverable_code=deliverable, section_seq=section, created_by=created_by, subject=subject[:64])
    db.add(c)
    db.flush()
    audit.record(db, actor="platform", action="confirmation.create", result="allowed", project_code=project.code,
                 confirmation_id=c.id, kind=kind, subject=subject[:64], options=len(options),
                 deliverable=deliverable or None)
    db.refresh(c)
    return c, True


# ---- ① 提示注入 ----

def screen_evidence(db: Session, user: User, project: Project, code: str, text: str) -> Confirmation | None:
    cats = injection.hits(text)
    if not cats:
        return None
    audit.record(db, actor=user.username, action="evidence.injection", result="flagged", project_code=project.code,
                 evidence=code, categories=cats, length=len(text))
    c, _ = platform_card(
        db, project, INJECTION, f"evidence:{code}", f"资料 {code} 疑似夹带写给 AI 的指令",
        ["按普通资料使用（其中的指令一律不执行）", "隔离，不交给 Agent"], "B",
        f"平台在 {code} 里识别到：{'、'.join(cats)}。在你确认前，这份资料不交给交付链和对话 Agent。",
        "选 A：照常作为证据使用，交给模型时仍标为数据；选 B：一直隔离，交付件不能据此起草。", user.username)
    return c


def quarantined_codes(db: Session, project: Project) -> set[str]:
    """隔离中的证据编号：注入命中卡未处理，或处理时没选 A（按普通资料使用）。"""
    rows = db.execute(select(Confirmation.subject, Confirmation.status, Confirmation.choice).where(
        Confirmation.project_id == project.id, Confirmation.kind == INJECTION)).all()
    out: set[str] = set()
    for subject, status, choice in rows:
        code = subject.removeprefix("evidence:")
        if status == "open" or choice != "A":
            out.add(code)
        else:
            out.discard(code)
    return out


# ---- ② 勘误 ----

def _retired(db: Session, d: Deliverable) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for seq, rows in kb_impact.deliverable_updates(db, d).items():
        refs = [r["ref"] for r in rows if r.get("change") == kb_impact.RETIRED]
        if refs:
            out[seq] = refs
    return out


def scan_errata(db: Session) -> int:
    """已签署交付件引用的条目下线：给项目经理发勘误卡（同一组下线条目只发一次）。"""
    made = 0
    for d in db.scalars(select(Deliverable).join(Project, Project.id == Deliverable.project_id)
                        .where(Deliverable.status == "signed", Project.deleted_at.is_(None))):
        retired = _retired(db, d)
        if not retired:
            continue
        project = d.project
        listed = "；".join(f"第 {seq} 章：{'、'.join(refs)}" for seq, refs in sorted(retired.items()))
        pm = next(iter(managers(db, project)), d.updated_by)
        _, created = platform_card(
            db, project, ERRATA, f"deliverable:{d.code}", f"{d.code} 引用的知识条目已下线，需判断是否勘误",
            ["发起勘误：生成勘误页，原签署版本保留", "无需勘误（下线不影响结论）"], "A",
            f"{d.code} v{d.revision} 已签署，引用的条目已下线：{listed}。",
            "选 A：在交付件页面下载勘误页随原件发客户；原签署版本不改。选 B：记为已判断，不再提醒这一组条目。",
            pm, deliverable=d.code, once=True)
        made += created
    db.commit()
    return made


def errata_page(db: Session, project: Project, d: Deliverable) -> dict:
    """勘误页（按当前知识状态计算，不另存文件；原签署版本不动）。"""
    retired = _retired(db, d)
    titles = {s.seq: s.title for s in d.sections}
    items = [{"section": seq, "title": titles.get(seq, ""), "ref": ref, "change": "条目已下线"}
             for seq, refs in sorted(retired.items()) for ref in refs]
    lines = [f"# {project.code} {d.code} {d.title} 勘误页", "",
             f"原签署版本：v{d.revision}（保留不改）；生成日期：{date.today().isoformat()}", ""]
    lines += [f"- 第 {i['section']} 章 {i['title']}：引用的 {i['ref']} 已下线，该处依据以更新后的公司知识库为准，"
              "请顾问复核后向客户说明" for i in items] or ["- 目前没有需要勘误的引用"]
    return {"deliverable": d.code, "signed": d.status == "signed", "revision": d.revision, "items": items,
            "text": "\n".join(lines)}


# ---- ③ 交付链失败 ----

def failure_cause(reason: str) -> str:
    """“第 3 步 起草：模型暂不可用（超时）”→“模型暂不可用”：去掉步骤前缀和括号里的细节，同因合卡。"""
    cause = reason.split("：", 1)[-1]
    return re.split(r"[（(]", cause, maxsplit=1)[0].strip() or "未知原因"


def on_run_failed(db: Session, run: AgentRun, project: Project, reason: str) -> Confirmation:
    cause = failure_cause(reason)
    subject = f"cause:{hashlib.sha256(cause.encode('utf-8')).hexdigest()[:12]}"
    tag = f"#{run.id}"
    c, created = platform_card(
        db, project, STUCK, subject, f"交付链自动重试后仍失败：{cause[:60]}",
        ["重新运行列出的失败运行", "先不处理（关闭本卡）"], "A",
        f"已自动重试 2 次仍失败。失败的运行：{tag}", "选 A：平台把这些运行从失败的那一步重新排队；"
        "选 B：关闭本卡，需要时在项目驾驶舱重新发起。", next(iter(managers(db, project)), run.created_by))
    if not created and tag not in re.findall(r"#\d+", c.reason):
        c.reason = f"{c.reason}、{tag}"
        db.commit()
    return c


def _requeue_failed(db: Session, project: Project, c: Confirmation) -> int:
    from . import runs

    n = 0
    for rid in (int(x) for x in re.findall(r"#(\d+)", c.reason)):
        run = db.get(AgentRun, rid)
        if run is None or run.project_id != project.id or run.status != "failed":
            continue
        run.status, run.error, run.finished_at, run.updated_at = "queued", "", None, utcnow()
        db.commit()
        runs._enqueue(db, run, project)
        n += 1
    return n


# ---- ④ 修订冲突 ----

def human_edits_since(s: DeliverableSection, base: int) -> bool:
    """base 版本之后有没有顾问手改（agent_code 为空）的新版本。"""
    return any(v.version > base and not v.agent_code for v in s.versions)


def conflict_card(db: Session, project: Project, d: Deliverable, s: DeliverableSection, base: int,
                  proposed: str, created_by: str, source: str) -> Confirmation:
    current = deliverables.get_version(s)
    lines = list(difflib.unified_diff((current.body if current else "").splitlines(), proposed.splitlines(),
                                      f"v{s.current_version}（人工修改）", f"{source}稿", lineterm="", n=1))
    more = f"\n……（共 {len(lines)} 行，只列前 {DIFF_LINES} 行）" if len(lines) > DIFF_LINES else ""
    trace = _safe("\n".join(lines[:DIFF_LINES]) + more)
    c, _ = platform_card(
        db, project, CONFLICT, f"section:{d.code}:{s.seq}", f"{d.code} 第 {s.seq} 章：{source}与人工修改冲突",
        ["保留人工修改（不采用这次的 AI 稿）", "按修订痕迹手工合并（本章标为需更新）"], "A",
        f"{source}基于 v{base} 起草，期间顾问已改到 v{s.current_version}；平台没有覆盖人工修改。"
        f"修订痕迹（- 为现稿，+ 为 AI 稿）：\n{trace}",
        "选 A：保持现稿；选 B：本章标为需更新，签署前须按修订痕迹改写。", created_by,
        deliverable=d.code, section=s.seq)
    db.commit()
    return c


# ---- 卡片处理后的平台动作 ----

def on_card_resolved(db: Session, user: User, c: Confirmation) -> None:
    if c.agent_code or c.kind not in PLATFORM_KINDS:
        return
    project = c.project
    if c.kind == STUCK and c.choice == "A":
        n = _requeue_failed(db, project, c)
        audit.record(db, actor=user.username, action="run.retry", result="allowed", project_code=project.code,
                     confirmation_id=c.id, runs=n)
    elif c.kind == CONFLICT and c.choice == "B" and c.section_seq is not None:
        d = deliverables.get_deliverable(db, project, c.deliverable_code)
        s = deliverables.get_section(d, c.section_seq) if d else None
        if s is not None:
            s.stale_reason, s.stale_at = CONFLICT_STALE, utcnow()
            db.commit()


# ---- ⑤ 提醒与升级 ----

def workdays_between(start: date, end: date) -> int:
    """start 之后到 end（含）之间的周一至周五天数。"""
    n, day = 0, start
    while day < end:
        day += timedelta(days=1)
        n += day.weekday() < 5
    return n


def scan_reminders(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    moved = 0
    for c in db.scalars(select(Confirmation).where(Confirmation.status == "open", Confirmation.remind_stage < 2)):
        created = c.created_at if c.created_at.tzinfo else c.created_at.replace(tzinfo=now.tzinfo)
        stage = 2 if now - created >= timedelta(days=ESCALATE_DAYS) else \
            1 if workdays_between(created.date(), now.date()) >= REMIND_WORKDAYS else 0
        if stage > c.remind_stage:
            c.remind_stage = stage
            db.commit()
            audit.record(db, actor="platform", action="confirmation.remind", result="allowed",
                         project_code=c.project.code, confirmation_id=c.id, stage=stage)
            moved += 1
    return moved


# ---- ⑥ 成员移出 ----

def transfer_member_tasks(db: Session, actor: User, project: Project, removed: str) -> dict:
    to = actor.username if actor.username != removed else next(iter(managers(db, project, exclude=removed)), "")
    counts = {"cards": 0, "selfeval": 0, "runs": 0}
    if not to:
        return counts
    from .runs import ACTIVE
    from .selfeval import OPEN

    for model, key, cond in ((Confirmation, "cards", Confirmation.status == "open"),
                             (SelfEvalPack, "selfeval", SelfEvalPack.status.in_(OPEN)),
                             (AgentRun, "runs", AgentRun.status.in_(ACTIVE))):
        for row in db.scalars(select(model).where(model.project_id == project.id, model.created_by == removed, cond)):
            row.created_by = to
            counts[key] += 1
    db.commit()
    if any(counts.values()):
        audit.record(db, actor=actor.username, action="project.member_handover", result="allowed",
                     project_code=project.code, target=removed, to=to, **counts)
    return counts


# ---- ⑦ 项目终止 ----

def terminate(db: Session, user: User, project: Project, confirm: str, today: date | None = None) -> dict:
    action = "project.terminate"
    m = membership(db, project.id, user.id)
    if m is None or m.project_role != PROJECT_MANAGER:
        audit.record(db, actor=user.username, action=action, result="denied", project_code=project.code,
                     reason="not_manager")
        raise LoopError(403, "只有本项目的项目经理能终止项目")
    if (confirm or "").strip() != project.code:
        raise LoopError(422, "请输入项目代号确认终止（终止后不能恢复）")
    if project.terminated_at is not None:
        raise LoopError(409, "项目已经终止")
    if project.deleted_at is not None:
        raise LoopError(409, "项目资料已删除")
    now, today = utcnow(), today or date.today()
    project.terminated_at, project.terminated_by = now, user.username
    cards = 0
    for c in db.scalars(select(Confirmation).where(Confirmation.project_id == project.id,
                                                   Confirmation.status == "open")):
        c.status, c.resolution, c.resolved_by, c.resolved_at = "resolved", TERMINATED_NOTE, "platform", now
        cards += 1
    from .runs import ACTIVE

    stopped = 0
    for r in db.scalars(select(AgentRun).where(AgentRun.project_id == project.id, AgentRun.status.in_(ACTIVE))):
        r.status, r.waiting_cards, r.updated_at, r.finished_at = "cancelled", [], now, now
        stopped += 1
    if project.accepted_on is None:          # 保留期从终止日起算（已验收的按验收日，不推迟删除）
        project.accepted_on = today
        project.deletion_due_on = retention.add_months(today, project.retention_months)
    db.commit()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 cards_closed=cards, runs_cancelled=stopped,
                 deletion_due_on=project.deletion_due_on.isoformat() if project.deletion_due_on else None)
    return {"terminated_at": now, "cards_closed": cards, "runs_cancelled": stopped,
            "deletion_due_on": project.deletion_due_on}


# ---- ⑧ 结项清单 ----

def closeout(db: Session, project: Project) -> dict:
    ordered = sorted(project.deliverables, key=lambda x: (len(x.code), x.code))
    unsigned = [{"code": d.code, "title": d.title, "status": d.status} for d in ordered
                if d.status != "signed" and any(s.current_version for s in d.sections)]
    cards = [{"id": c.id, "kind": c.kind, "title": c.title} for c in db.scalars(
        select(Confirmation).where(Confirmation.project_id == project.id, Confirmation.status == "open")
        .order_by(Confirmation.id))]
    rechecks = kb_impact.project_updates(db, project)
    stale = [{"deliverable": d.code, "section": s.seq, "reason": s.stale_reason}
             for d in ordered for s in d.sections if s.stale_reason]
    return {"unsigned": unsigned, "open_cards": cards, "rechecks": rechecks, "stale": stale,
            "ready": not (unsigned or cards or rechecks or stale)}


# ---- ⑨ 对外文本 ----

@dataclass
class Outbound:
    text: str
    rewritten: int
    blocked: bool
    categories: list[str]


def outbound_check(text: str) -> Outbound:
    """出平台的文字：命中机密级的拦下；“××结论”改写成“符合性初判”，改不了的（通过测评、等保达标等）拦下。"""
    g = guard.check(text or "")
    if g.blocked:
        return Outbound(text, 0, True, sorted({f.kind for f in g.findings}))
    out, n, left = quality.rewrite_qualification(text or "")
    return Outbound(out, n, bool(left), sorted(set(left)))


def outbound_text(text: str) -> str:
    """平台自己拼的对外文本（周报、邮件正文）：改写资质用语；仍有问题的整段换成提示，不外发原文。"""
    r = outbound_check(text)
    return "（这段文字未通过对外文本检查，请在平台内查看）" if r.blocked else r.text


# ---- ⑩ 漂移监测 ----

def _week(t: datetime, now: datetime) -> int:
    t = t if t.tzinfo else t.replace(tzinfo=now.tzinfo)
    return (now - t).days // 7


def drift_metrics(db: Session, weeks: int = 8, now: datetime | None = None) -> dict:
    """无内容指标：顾问对 Agent 稿的大改率、确认卡采纳推荐率、写意见率；最近 2 周对比此前基线。"""
    from ..models import SectionVersion

    now = now or utcnow()
    since = now - timedelta(weeks=weeks)
    edits: list[tuple[int, bool]] = []
    prev: dict[int, SectionVersion] = {}
    for v in db.scalars(select(SectionVersion).where(SectionVersion.created_at >= since - timedelta(weeks=4))
                        .order_by(SectionVersion.section_id, SectionVersion.version)):
        p = prev.get(v.section_id)
        prev[v.section_id] = v
        if p is not None and p.agent_code and not v.agent_code and v.created_at >= since.replace(
                tzinfo=v.created_at.tzinfo):
            heavy = judgment.bucket(judgment.magnitude(p.body, v.body)) == "heavy"
            edits.append((_week(v.created_at, now), heavy))
    cards: list[tuple[int, bool, bool]] = []
    for c in db.scalars(select(Confirmation).where(Confirmation.status == "resolved",
                                                   Confirmation.resolved_at.is_not(None),
                                                   Confirmation.recommendation != "",
                                                   Confirmation.resolved_by != "platform")):
        if c.resolved_at.replace(tzinfo=c.resolved_at.tzinfo or now.tzinfo) < since:
            continue
        cards.append((_week(c.resolved_at, now), c.choice == c.recommendation, not c.choice))

    def rate(rows: list, idx: int) -> dict:
        recent = [r[idx] for r in rows if r[0] < 2]
        base = [r[idx] for r in rows if r[0] >= 2]
        out = {"recent": round(sum(recent) / len(recent), 3) if recent else None, "recent_n": len(recent),
               "baseline": round(sum(base) / len(base), 3) if base else None, "baseline_n": len(base)}
        out["drift"] = bool(len(recent) >= DRIFT_MIN and len(base) >= DRIFT_MIN
                            and abs(out["recent"] - out["baseline"]) > DRIFT_THRESHOLD)
        return out

    metrics = {"heavy_edit_rate": rate(edits, 1), "follow_recommendation_rate": rate(cards, 1),
               "comment_rate": rate(cards, 2)}
    return {"weeks": weeks, "judgment_capture": judgment.enabled(), "metrics": metrics,
            "drift": [k for k, v in metrics.items() if v["drift"]]}


def record_drift(db: Session, now: datetime | None = None) -> dict:
    r = drift_metrics(db, now=now)
    audit.record(db, actor="platform", action="quality.drift", result="flagged" if r["drift"] else "allowed",
                 drift=r["drift"], **{k: {"recent": v["recent"], "baseline": v["baseline"], "n": v["recent_n"]}
                                      for k, v in r["metrics"].items()})
    return r


# ---- ⑫ 上游变更 ----

def downstream(db: Session, project: Project, source: str) -> list[str]:
    """交付链里直接或间接以 source（交付件代号或 evidence）为输入的交付件代号。"""
    from .runs import steps

    chain = steps(db, project)
    keys = {source}
    out: list[str] = []
    changed = True
    while changed:
        changed = False
        for st in chain:
            ins = set(st.inputs)
            if ins & (keys | {f"run:{k}" for k in keys}):
                for k in (st.deliverable, st.key):
                    if k and k not in keys:
                        keys.add(k)
                        changed = True
                if st.deliverable and st.deliverable not in out and st.deliverable != source:
                    out.append(st.deliverable)
    return out


def upstream_changed(db: Session, project: Project, source: str, reason: str,
                     targets: list[str] | None = None, actor: str = "platform") -> list[tuple[str, int]]:
    """上游变更钩子：把下游未签交付件里已有正文的章标为需更新（签署门禁据此拦下，改写后清除）。"""
    reason = (reason or "上游变更").strip()[:64]
    now = utcnow()
    marked: list[tuple[str, int]] = []
    for code in targets if targets is not None else downstream(db, project, source):
        d = deliverables.get_deliverable(db, project, code)
        if d is None or d.status == "signed":
            continue
        for s in d.sections:
            if s.current_version:
                s.stale_reason, s.stale_at = reason, now
                marked.append((d.code, s.seq))
    db.commit()
    audit.record(db, actor=actor, action="project.upstream_change", result="allowed", project_code=project.code,
                 source=source[:32], sections=len(marked), deliverables=sorted({c for c, _ in marked}))
    return marked
