"""四期 A35：改稿——审阅清单、改动意图识别与扩散、人工定稿保护、不满意刷新（docs/交付链输入输出方案.md 第七节）。

不建表、不加迁移，全部从已有数据推出：
- 人工定稿：某章当前版本是顾问手改（agent_code 为空）的，Agent 刷新时跳过；上游变化只标“需更新”，由人决定。
- 审阅清单（第 1 条）：按风险排序——门禁提醒（需更新、评审阻断）＞ 评审重要问题、来源核对不到、缺正文 ＞
  无来源句子、低置信（模型推断）；第二轮起（已有完成的评审）标出本轮变化的章（当前版本 ≠ 评审时版本）。
- 意图识别（第 3 条）：比较本章最近一次人工修改与上一版——数字、日期、来源标注变了算“事实”，把引用同一证据的
  其他章标“需更新”（loops.on_upstream_change，门禁拦签署，改写后清空）；称呼、视角或详略（长度变化过半）变了算
  “口径”，界面问“全项目都这样改？”，确认后走现有定制项入口（POST /customizations，口径类）；其余为“措辞”，只改这一处。
- 不满意刷新（第 8 条）：先问原因（4 类），前三类转去改事实、定制项、组件清单，只有“写得不好”才排后台任务重新起草；
  重新起草跳过人工定稿章，经报告与汇报 Agent 改写（沿用 revise._rewrite，模型只经 agents/call.py，写入只经
  deliverables.write_section），生成新版本，版本差异与一键回退用已有 diff 接口和本模块 rollback；同一章连续刷新
  REFRESH_MAX 轮仍不满意转人工改写或对话整章，不无限重跑。
审计只记编号、类别、计数，不记正文。
"""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import call, runtime, tools
from ..core import audit
from ..core.db import SessionLocal
from ..core.permissions import membership
from ..models import Deliverable, DeliverableSection, Job, Project, User
from ..services import gateway, jobs
from . import deliverables, loops, quality, review_issues, revise

JOB_KIND = "deliverable.refresh"
AGENT = revise.AGENT
REFRESH_MAX = 2
REFRESH_NOTE = "不满意刷新"
FACT, TONE, WORDING = "事实", "口径", "措辞"
# 原因 → 去向：fact 改事实（证据）、customization 定制项、outputs 组件清单、redraft 重新起草
REASONS: dict[str, tuple[str, str]] = {
    "事实不对": ("fact", "事实不对请直接改正文里的事实或证据，平台会把引用同一事实的其他章标为需更新"),
    "口径不对": ("customization", "口径不对请写成定制项（称呼、视角、详略），确认后全项目按此起草"),
    "结构不对": ("outputs", "结构不对请到输出页调整组件清单或在定制项里增删章节（须项目经理确认）"),
    "写得不好": ("redraft", "重新起草，跳过人工定稿的章节"),
}
REDRAFT_ITEM = "顾问对本章写法不满意：重新组织语言，表达更清楚、更有针对性；事实、数字和来源标注不变"
HANDOFF_HINT = f"同一章已连续刷新 {REFRESH_MAX} 轮，请人工改写或在对话里对整章提要求"
TONE_WORDS = ("贵行", "贵司", "贵公司", "贵单位", "客户", "甲方", "我方", "我司", "我们", "本公司", "你们", "您")
_FACT_TOKEN = re.compile(r"\[[^\]]+\]|\d+(?:\.\d+)?%?|\d{4}[-年/]\d{1,2}")
_EV_CODE = re.compile(r"\[((?:I|D|O|S)-\d+)")
_SENT = re.compile(r"[^。！？!?\n]+[。！？!?]?")
MIN_SENTENCE = 20
RANK = {"门禁提醒": 0, "评审问题": 1, "来源核对不到": 1, "缺正文": 1, "无来源句子": 2, "低置信": 2}
ACTIVE = ("queued", "running")


class ReworkError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def human_final(s: DeliverableSection) -> bool:
    v = deliverables.get_version(s)
    return v is not None and not v.agent_code


def refresh_rounds(s: DeliverableSection) -> int:
    """本章末尾连续几版是“不满意刷新”写的（人工改过即清零）。"""
    n = 0
    for v in sorted(s.versions, key=lambda x: x.version, reverse=True):
        if v.agent_code and v.note.startswith(REFRESH_NOTE):
            n += 1
        else:
            break
    return n


def unsourced_sentences(body: str) -> int:
    n = 0
    for line in body.splitlines():
        t = line.strip()
        if not t or t.startswith(("#", "|", ">")):
            continue
        for m in _SENT.finditer(t.lstrip("-*0123456789. ")):
            sent = m.group(0).strip()
            if len(sent) >= MIN_SENTENCE and "[" not in sent:
                n += 1
    return n


def _jobs(db: Session, project: Project, d: Deliverable) -> list[Job]:
    rows = db.scalars(select(Job).where(Job.kind == JOB_KIND, Job.project_code == project.code)
                      .order_by(Job.id.desc())).all()
    return [j for j in rows if json.loads(j.payload or "{}").get("deliverable") == d.code]


def _item(kind: str, hint: str, seq: int, changed: bool) -> dict:
    return {"seq": seq, "kind": kind, "hint": hint, "rank": RANK[kind], "changed": changed}


def review_list(db: Session, project: Project, d: Deliverable) -> dict:
    last = quality.latest_done_review(db, d)
    reviewed = {str(k): int(v) for k, v in (last.versions or {}).items()} if last else {}
    items: list[dict] = []
    sections = []
    for s in sorted(d.sections, key=lambda x: x.seq):
        v = deliverables.get_version(s)
        changed = bool(last) and reviewed.get(str(s.seq)) != s.current_version
        sections.append({"seq": s.seq, "title": s.title, "version": s.current_version, "human_final": human_final(s),
                         "changed": changed, "stale": s.stale_reason, "refresh_rounds": refresh_rounds(s)})

        def add(kind: str, hint: str, seq: int = s.seq, flag: bool = changed) -> None:
            items.append(_item(kind, hint, seq, flag))

        if v is None:
            add("缺正文", "本章还没有正文")
            continue
        if s.stale_reason:
            add("门禁提醒", f"需更新：{s.stale_reason}（签署前须改写或复核）")
        src = v.sources or {}
        if src.get("unresolved"):
            add("来源核对不到", f"{len(src['unresolved'])} 处来源标注在本项目或知识库里找不到")
        n = unsourced_sentences(v.body)
        if n:
            add("无来源句子", f"{n} 句没有来源标注，可补来源或标 [推断]")
        inferred = int((src.get("counts") or {}).get("inferred", 0))
        if inferred:
            add("低置信", f"{inferred} 处是模型推断，请核对")
    if last is not None:
        titles = {str(s.seq): s.title for s in d.sections}
        for i in last.issues or []:
            if i.get("status") == review_issues.WAIVED_MARK or i.get("severity") not in (review_issues.BLOCKING,
                                                                                        review_issues.MAJOR):
                continue
            seqs = sorted(int(x) for x in review_issues.sections_of(i.get("location", ""), titles)) or [0]
            kind = "门禁提醒" if i.get("severity") == review_issues.BLOCKING else "评审问题"
            hint = f"{i.get('code') or ''} {i.get('severity')}：{i.get('problem', '')}".strip()[:120]
            for seq in seqs:
                items.append(_item(kind, hint, seq, next((x["changed"] for x in sections if x["seq"] == seq), False)))
    items.sort(key=lambda x: (x["rank"], x["seq"]))
    job = next(iter(_jobs(db, project, d)), None)
    return {"round": 2 if last is not None else 1, "items": items, "sections": sections,
            "refresh": {"status": job.status if job else "", "error": job.last_error if job else ""},
            "reasons": [{"reason": k, "action": a, "hint": h} for k, (a, h) in REASONS.items()],
            "refresh_max": REFRESH_MAX}


# ---- 意图识别与扩散 ----

def classify(old: str, new: str) -> dict:
    """返回 {"intent": 事实/口径/措辞, "changes": [(原, 改), ...]（最多 5 处，各 ≤40 字）}。"""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    changes = [(old[i1:i2][:40], new[j1:j2][:40]) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"][:5]
    if Counter(_FACT_TOKEN.findall(old)) != Counter(_FACT_TOKEN.findall(new)):
        intent = FACT
    elif any(Counter(re.findall(w, old)) != Counter(re.findall(w, new)) for w in TONE_WORDS) \
            or (old and abs(len(new) - len(old)) / len(old) > 0.5):
        intent = TONE
    else:
        intent = WORDING
    return {"intent": intent, "changes": changes}


def detect(db: Session, user: User, project: Project, d: Deliverable, seq: int) -> dict:
    """本章最近一次人工修改的意图；事实类自动把引用同一证据的其他章标“需更新”（已提交）。"""
    s = deliverables.require_section(d, seq)
    cur = deliverables.get_version(s)
    prev = deliverables.get_version(s, s.current_version - 1) if s.current_version > 1 else None
    if cur is None or prev is None or cur.agent_code:
        return {"intent": "", "changes": [], "marked": 0, "ask_project_wide": False}
    out = classify(prev.body, cur.body)
    marked = 0
    if out["intent"] == FACT:
        codes = set(_EV_CODE.findall(prev.body)) | set(_EV_CODE.findall(cur.body))
        targets: list[tuple[str | None, int | None]] = []
        for other in project.deliverables:
            for x in other.sections:
                ov = deliverables.get_version(x)
                if (other.id, x.seq) != (d.id, seq) and ov is not None and codes & set(_EV_CODE.findall(ov.body)):
                    targets.append((other.code, x.seq))
        if targets:
            marked = loops.on_upstream_change(db, project, f"事实更正：{d.code} 第 {seq} 章", targets)
    audit.record(db, actor=user.username, action="deliverable.edit_intent", result="allowed",
                 project_code=project.code, deliverable=d.code, section=seq, version=cur.version,
                 intent=out["intent"], marked=marked)
    db.commit()
    return {**out, "marked": marked, "ask_project_wide": out["intent"] == TONE}


# ---- 不满意刷新 ----

def refresh(db: Session, user: User, project: Project, d: Deliverable, reason: str, seq: int | None) -> dict:
    if reason not in REASONS:
        raise ReworkError(422, f"原因只能是：{'、'.join(REASONS)}")
    action, hint = REASONS[reason]
    base = {"reason": reason, "action": action, "hint": hint, "job_id": None, "seqs": [], "skipped": [],
            "handoff": []}
    if action != "redraft":
        audit.record(db, actor=user.username, action="deliverable.refresh", result="allowed",
                     project_code=project.code, deliverable=d.code, reason=action)
        db.commit()
        return base
    if d.status == "signed":
        raise ReworkError(409, f"{d.code} 已签署，只能发起勘误")
    pool = [s for s in d.sections if s.current_version and (seq is None or s.seq == seq)]
    if seq is not None and not pool:
        raise ReworkError(404 if deliverables.get_section(d, seq) is None else 409, f"第 {seq} 章没有正文，不能刷新")
    skipped = [s.seq for s in pool if human_final(s)]
    handoff = [s.seq for s in pool if not human_final(s) and refresh_rounds(s) >= REFRESH_MAX]
    seqs = sorted(s.seq for s in pool if s.seq not in skipped and s.seq not in handoff)
    base.update(skipped=sorted(skipped), handoff=sorted(handoff))
    if not seqs:
        base.update(action="handoff" if handoff else "none",
                    hint=HANDOFF_HINT if handoff else "可刷新的章都是人工定稿，平台不覆盖；请直接改字或对话提要求")
        return base
    if any(j.status in ACTIVE for j in _jobs(db, project, d)):
        raise ReworkError(409, f"{d.code} 已有进行中的刷新，请等它结束")
    try:
        runtime.ready_doc(db, AGENT)
    except runtime.AgentNotReady as e:
        raise ReworkError(503, str(e)) from e
    job_id = jobs.enqueue(db, JOB_KIND, {"project": project.code, "deliverable": d.code, "user": user.username,
                                         "seqs": seqs}, created_by=user.username, project_code=project.code,
                          max_attempts=1)
    audit.record(db, actor=user.username, action="deliverable.refresh", result="allowed", project_code=project.code,
                 deliverable=d.code, reason=action, job_id=job_id, sections=len(seqs), skipped=len(skipped),
                 handoff=len(handoff))
    db.commit()
    base.update(job_id=job_id, seqs=seqs)
    return base


def run_refresh(db: Session, user: User, project: Project, d: Deliverable, seqs: list[int]) -> dict:
    written = conflicts = 0
    for seq in seqs:
        s = deliverables.get_section(d, seq)
        if s is None or human_final(s):            # 排队期间顾问改过：人工定稿，不覆盖
            continue
        base = s.current_version
        body = revise._rewrite(db, user, project, d, seq, [REDRAFT_ITEM])
        db.refresh(d)
        s = deliverables.require_section(d, seq)
        if loops.human_edits_since(s, base):
            loops.conflict_card(db, project, d, s, base, body, user.username, REFRESH_NOTE)
            conflicts += 1
            continue
        try:
            deliverables.write_section(db, user, project, d, seq, body, agent_code=AGENT,
                                       note=f"{REFRESH_NOTE}第 {refresh_rounds(s) + 1} 轮")
            written += 1
        except deliverables.DeliverableError:
            db.rollback()
        db.refresh(d)
    return {"sections": len(seqs), "written": written, "conflicts": conflicts}


def rollback(db: Session, user: User, project: Project, d: Deliverable, seq: int, version: int) -> None:
    """一键回退：把某个旧版本的正文写成新版本（只追加），人选的版本算人工定稿。"""
    s = deliverables.require_section(d, seq)
    v = deliverables.get_version(s, version)
    if v is None or version >= s.current_version:
        raise ReworkError(422, f"第 {seq} 章没有可回退的 v{version}")
    try:
        deliverables.write_section(db, user, project, d, seq, v.body, note=f"回退到 v{version}",
                                   base_version=s.current_version)
    except deliverables.DeliverableError as e:
        raise ReworkError(e.status, e.detail) from e


@jobs.handler(JOB_KIND)
def _job(payload: dict, ctx: jobs.JobContext) -> None:
    with SessionLocal() as db:
        project = db.scalar(select(Project).where(Project.code == payload.get("project", "")))
        d = deliverables.get_deliverable(db, project, payload.get("deliverable", "")) if project else None
        user = db.scalar(select(User).where(User.username == payload.get("user", "")))
        if d is None or user is None or not user.is_active or membership(db, project.id, user.id) is None:
            raise jobs.JobError("发起人已不是项目成员或交付件不存在", retry=False)
        try:
            out = run_refresh(db, user, project, d, [int(x) for x in payload.get("seqs", [])])
        except (revise.RevisionError, runtime.AgentNotReady, tools.ToolError, gateway.GatewayError,
                call.OutputInvalid) as e:
            db.rollback()
            audit.record(db, actor=user.username, action="deliverable.refresh.run", result="error",
                         project_code=project.code, deliverable=d.code, reason=type(e).__name__)
            db.commit()
            raise jobs.JobError(getattr(e, "detail", "") or type(e).__name__, retry=False) from e
        audit.record(db, actor=user.username, action="deliverable.refresh.run", result="allowed",
                     project_code=project.code, deliverable=d.code, **out)
        db.commit()
