"""交付件一键修订与客户意见回路（四期 A26，docs/驾驶舱改版方案.md 第四节回路、第七节、第九节第 1 条）。

一键修订（后台任务 deliverable.revise，顾问点按钮或客户意见处理完自动发起）：
- 修改清单 = 本轮要改的条目，按章归组：
  ① 最近一次评审（针对当前修订）里未驳回的“机械问题”（mechanical：资质用语、表达口径、格式术语、来源标注等，
     不含机密、矛盾、缺正文、只有推断这类须人定夺的）；
  ② 第 1 轮另加：已采纳、还没修订过的客户意见（确认卡 kind=客户意见，选 A）和引用知识条目已更新 / 已下线的章
     （影响面，kb_impact）。
- 每章经报告与汇报 Agent（report-writer，沿用读项目区、知识检索授权，不新增工具授权）改一次，模型只经 agents/call.py；
  写入只经 deliverables.write_section（机密级检测、严格档占位符、来源核对照旧），修改说明记“一键修订第 N 轮”。
- 每轮写完立即复查（quality.execute_review）；最多 REVISE_MAX_ROUNDS 轮（负责人 2026-11-25 定 2 轮），
  仍有机械问题的转人工：状态接口给出剩余条数 handoff，界面提示顾问逐条处理。内容取舍、签署仍由人做。
客户意见回路 v1：顾问录入客户意见（confirmations.create_feedback，每条一张确认卡）→ 逐条采纳 / 不采纳 →
  该交付件的客户意见全部处理完且有采纳的：已签署的先重开（sign_offs 追加 reopen，已签版本的各章版本号留在签署记录里，
  只追加不删除）→ 发起一键修订 → 评审 → 重签。
不建表：已修订过的客户意见按本交付件历次修订任务参数里的卡号判断。审计只记编号、轮次和计数，不记正文和意见原文。
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import call, knowledge, runtime, tools
from ..core import audit
from ..core.config import get_settings
from ..core.db import SessionLocal
from ..core.permissions import membership
from ..models import Confirmation, Deliverable, Job, Project, SignOff, User
from ..services import gateway, injection, jobs
from . import confirmations, deliverables, kb_impact, loops, quality, review_issues

JOB_KIND = "deliverable.revise"
AGENT = "report-writer"
ACTIVE = ("queued", "running")
TRIGGER_FEEDBACK = "client-feedback"
_HUMAN = ("保密", "机密", "客户信息", "矛盾", "没有正文", "已删除", "仅现场", "只有推断", "产品规则", "评审模型")
_MECHANICAL = ("资质", "用语", "表达", "口径", "格式", "术语", "措辞", "错别字", "来源标注", "依据名称", "引用格式")
_FENCE = re.compile(r"^```[a-zA-Z]*\n|\n?```\s*$")
INSTRUCTIONS = (
    "任务：按下面的修改清单修订本章正文。只改清单涉及的地方，其他内容和来源标注（[I-xx] [D-xx] [O-xx] [S-xx] [L…] [推断]）"
    "原样保留；不新增客户真实名称、人名、口令、IP 等机密级内容；不写测评、认证结论，用“差距分析、符合性初判、整改建议”。"
    "只输出修订后的本章正文（Markdown），不要章标题，不要解释。"
)


class RevisionError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def max_rounds() -> int:
    return max(int(get_settings().revise_max_rounds), 0)


def mechanical(issue: dict) -> bool:
    """平台可自动修的机械问题：未驳回、阻断或重要、命中机械类关键词且不涉及须人定夺的类别。"""
    if issue.get("status") == review_issues.WAIVED_MARK or issue.get("severity") not in ("阻断", "重要"):
        return False
    text = f"{issue.get('dimension', '')} {issue.get('problem', '')}"
    return not any(w in text for w in _HUMAN) and any(w in text for w in _MECHANICAL)


def open_mechanical(db: Session, d: Deliverable) -> list[dict]:
    r = quality.latest_done_review(db, d)
    if r is None or r.revision != d.revision:
        return []
    return [i for i in r.issues or [] if mechanical(i)]


def _jobs_of(db: Session, project: Project, d: Deliverable) -> list[tuple[Job, dict]]:
    rows = db.scalars(select(Job).where(Job.kind == JOB_KIND, Job.project_code == project.code)
                      .order_by(Job.id.desc())).all()
    out = []
    for j in rows:
        payload = json.loads(j.payload or "{}")
        if payload.get("deliverable") == d.code:
            out.append((j, payload))
    return out


def pending_feedback(db: Session, project: Project, d: Deliverable) -> list[Confirmation]:
    """已采纳、还没进过修订任务的客户意见。"""
    used = {i for _, p in _jobs_of(db, project, d) for i in p.get("feedback", [])}
    rows = db.scalars(select(Confirmation).where(
        Confirmation.project_id == project.id, Confirmation.kind == confirmations.FEEDBACK_KIND,
        Confirmation.deliverable_code == d.code, Confirmation.status == "resolved",
        Confirmation.choice == confirmations.FEEDBACK_ADOPT).order_by(Confirmation.id)).all()
    return [c for c in rows if c.id not in used]


def state(db: Session, project: Project, d: Deliverable) -> dict:
    last = next(iter(_jobs_of(db, project, d)), None)
    remaining = len(open_mechanical(db, d))
    status = last[0].status if last else ""
    return {"status": status, "trigger": last[1].get("trigger", "") if last else "",
            "created_at": last[0].created_at if last else None, "finished_at": last[0].finished_at if last else None,
            "error": last[0].last_error if last else "", "max_rounds": max_rounds(), "mechanical_open": remaining,
            "pending_feedback": len(pending_feedback(db, project, d)),
            "handoff": status == "done" and remaining > 0}


def reopen(db: Session, user: User, project: Project, d: Deliverable) -> None:
    """客户意见回路：已签署的交付件重开修订。签署记录（含已签各章版本号）只追加不改，已签版本留存只读。"""
    db.refresh(d, with_for_update=True)
    if d.status != "signed":
        return
    db.add(SignOff(project_id=project.id, deliverable_id=d.id, action="reopen", revision=d.revision,
                   versions={str(s.seq): s.current_version for s in d.sections}, actor=user.username,
                   comment="客户意见采纳，生成修订稿"))
    d.status = "drafting"
    audit.record(db, actor=user.username, action="deliverable.reopen", result="allowed", project_code=project.code,
                 deliverable=d.code, revision=d.revision, trigger=TRIGGER_FEEDBACK)
    db.commit()


def start(db: Session, user: User, project: Project, d: Deliverable, trigger: str = "manual") -> int:
    """排一次一键修订。已签署的只有带着待修订的客户意见才重开。返回任务编号。"""
    feedback = [c.id for c in pending_feedback(db, project, d)]
    if d.status == "signed":
        if not feedback:
            raise RevisionError(409, f"{d.code} 已签署；客户有意见请先录入客户意见")
        reopen(db, user, project, d)
    if not d.sections or not any(s.current_version for s in d.sections):
        raise RevisionError(409, f"{d.code} 还没有正文，不能修订")
    if any(j.status in ACTIVE for j, _ in _jobs_of(db, project, d)):
        raise RevisionError(409, f"{d.code} 已有进行中的一键修订，请等它结束")
    try:
        runtime.ready_doc(db, AGENT)
    except runtime.AgentNotReady as e:
        raise RevisionError(503, str(e)) from e
    job_id = jobs.enqueue(db, JOB_KIND, {"project": project.code, "deliverable": d.code, "user": user.username,
                                         "feedback": feedback, "trigger": trigger},
                          created_by=user.username, project_code=project.code, max_attempts=1)
    audit.record(db, actor=user.username, action="deliverable.revise.request", result="allowed",
                 project_code=project.code, deliverable=d.code, job_id=job_id, feedback=len(feedback), trigger=trigger)
    db.commit()
    return job_id


def on_card_resolved(db: Session, user: User, c: Confirmation) -> int | None:
    """客户意见卡处理后：本交付件的客户意见都处理完、且有采纳的，自动发起修订（失败不影响处理结果，可再点一键修订）。"""
    if c.kind != confirmations.FEEDBACK_KIND or not c.deliverable_code:
        return None
    still_open = db.scalar(select(Confirmation.id).where(
        Confirmation.project_id == c.project_id, Confirmation.kind == confirmations.FEEDBACK_KIND,
        Confirmation.deliverable_code == c.deliverable_code, Confirmation.status == "open").limit(1))
    project = db.get(Project, c.project_id)
    d = deliverables.get_deliverable(db, project, c.deliverable_code)
    if still_open is not None or d is None or not pending_feedback(db, project, d):
        return None
    try:
        return start(db, user, project, d, trigger=TRIGGER_FEEDBACK)
    except RevisionError:
        db.rollback()
        return None


# ---- 任务 ----

def _plan(db: Session, d: Deliverable, first: bool, feedback: list[Confirmation]) -> dict[int, list[str]]:
    titles = {str(s.seq): s.title for s in d.sections}
    plan: dict[int, list[str]] = {}

    def add(seqs, text: str) -> None:
        for seq in seqs:
            plan.setdefault(int(seq), []).append(text)

    for i in open_mechanical(db, d):
        add(review_issues.sections_of(i.get("location", ""), titles),
            f"{i.get('code') or ''} {i.get('problem', '')}；建议：{i.get('suggestion', '')}".strip())
    if first:
        for c in feedback:
            add([c.section_seq] if c.section_seq else titles, f"客户意见（已采纳）：{c.reason}")
        for seq, changes in kb_impact.deliverable_updates(db, d).items():
            for ch in changes:
                add([seq], f"引用的知识条目 {ch['doc_id']} "
                           f"{'已下线，换用现行依据或删去' if ch['change'] == kb_impact.RETIRED else '已更新，按现行版本核对表述'}")
    return plan


def _rewrite(db: Session, user: User, project: Project, d: Deliverable, seq: int, items: list[str]) -> str:
    s = deliverables.require_section(d, seq)
    v = deliverables.get_version(s)
    doc = runtime.ready_doc(db, AGENT)
    card = runtime.project_card(db, user, AGENT, project.code)
    kb_block, _ = knowledge.retrieve(db, user, AGENT, project, f"{d.title} {s.title} {' '.join(items)[:200]}")
    todo = "\n".join(f"- {x}" for x in items)
    message = f"{INSTRUCTIONS}\n\n# 修改清单\n{todo}\n\n# 第 {seq} 章 {s.title}（当前 v{s.current_version}）\n\n{v.body if v else ''}"
    res = call.call(db, AGENT, message, actor=user.username, doc=doc, project=project,
                    system=runtime.system_prompt(doc, card, injection.as_data("检索到的知识库内容", kb_block)),
                    level=doc.level)
    if res.completion.demo:
        raise RevisionError(503, "未配置大模型，不能自动修订")
    return _FENCE.sub("", res.completion.text.strip()).strip()


def run(db: Session, user: User, project: Project, d: Deliverable, feedback_ids: list[int], ctx: jobs.JobContext,
        trigger: str = "") -> dict:
    """逐轮修订 + 复查；返回计数（进审计）。"""
    feedback = [c for c in (db.get(Confirmation, i) for i in feedback_ids) if c is not None]
    rounds = written = 0
    for rnd in range(1, max_rounds() + 1):
        plan = _plan(db, d, rnd == 1, feedback)
        if not plan:
            break
        rounds = rnd
        wrote = 0
        for seq, items in sorted(plan.items()):
            if deliverables.get_section(d, seq) is None:
                continue
            base = deliverables.require_section(d, seq).current_version
            body = _rewrite(db, user, project, d, seq, items)
            db.refresh(d)
            s = deliverables.require_section(d, seq)
            if loops.human_edits_since(s, base):          # 四期 A31④：修订期间顾问手改过，不覆盖，发冲突卡
                loops.conflict_card(db, project, d, s, base, body, user.username, "一键修订")
                continue
            try:
                deliverables.write_section(db, user, project, d, seq, body, agent_code=AGENT,
                                           note=f"一键修订第 {rnd} 轮：{len(items)} 条")
                wrote += 1
            except deliverables.DeliverableError:
                db.rollback()                      # 机密级、占位符、内容没变等：这章不写，留给人处理
            db.refresh(d)
        written += wrote
        if not wrote:
            break
        try:
            r = quality.request_review(db, user, project, d, trigger=f"auto:revise-{rnd}")
            quality.execute_review(r.id, ctx)
        except (quality.QualityError, jobs.JobError):
            db.rollback()
            break
        db.expire_all()
    remaining = len(open_mechanical(db, d))
    return {"rounds": rounds, "written": written, "remaining": remaining, "handoff": remaining > 0,
            "feedback": len(feedback), "trigger": trigger}


@jobs.handler(JOB_KIND)
def _job(payload: dict, ctx: jobs.JobContext) -> None:
    with SessionLocal() as db:
        project = db.scalar(select(Project).where(Project.code == payload.get("project", "")))
        d = deliverables.get_deliverable(db, project, payload.get("deliverable", "")) if project else None
        user = db.scalar(select(User).where(User.username == payload.get("user", "")))
        if d is None or user is None or not user.is_active or membership(db, project.id, user.id) is None:
            raise jobs.JobError("发起人已不是项目成员或交付件不存在", retry=False)
        try:
            out = run(db, user, project, d, [int(i) for i in payload.get("feedback", [])], ctx,
                      trigger=payload.get("trigger", ""))
        except (RevisionError, runtime.AgentNotReady, tools.ToolError, gateway.GatewayError, call.OutputInvalid) as e:
            db.rollback()
            audit.record(db, actor=user.username, action="deliverable.revise", result="error",
                         project_code=project.code, deliverable=d.code, reason=type(e).__name__)
            db.commit()
            raise jobs.JobError(getattr(e, "detail", "") or type(e).__name__, retry=False) from e
        audit.record(db, actor=user.username, action="deliverable.revise", result="allowed",
                     project_code=project.code, deliverable=d.code, **out)
        db.commit()
