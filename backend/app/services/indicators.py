"""指标精简版（三期 F2①，4A 架构设计 v4.0 §6.2；总体规划 §6.8）。

10 项指标 + 自动发布率、人工分钟 / 百条，国标内容引用单列（三期开发计划第八节第 6 条：条款号核验只能确认条款存在，
不能确认引用内容对不对，涉及国标内容的引用归抽检高风险层）。

- 按时间窗 [start, end) 现算，不建表（迁移 0046 空号）；只读，不调模型，不写审计。
- 输出只有数字和口径说明，不出项目代号、交付件标题、正文、知识条目名（数据保护红线 4）。
- 平台还没采集的指标（客户评审轮次、事实确认单、专家派单）返回 status=na 并写明何时有数，不编数字。
- 交付件类指标以窗内签署（sign_offs.action=sign）为准，正文取签署时快照的章节版本；已删除的项目不计。
"""
from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (AuditLog, Deliverable, DeliverableSection, Evidence, JudgmentEvent, KbItem, KbReview, KbVersion,
                      Project, SectionVersion, SignOff)

EVAL_ACTION = "kb.eval_compare"
EVAL_FLOOR = 0.9
AUTO_KINDS = ("platform", "agent")                 # B10 平台核验发布、B11 知识审核 Agent 发布
HUMAN_ROLES = ("content", "compliance", "sample")  # 人工审核与抽检（agent / platform / reconfirm 是程序或 Agent）
ADVICE = re.compile(r"建议|整改|规划|路线|措施")     # 建议类章节（按章节标题判断）
GB = re.compile(r"^GB(?:/T|/Z)?\s*\d")


@dataclass
class Indicator:
    key: str
    category: str
    name: str
    basis: str                        # 口径
    target: str                       # 红线或“趋势”
    value: float | int | None = None
    unit: str = ""
    redline: bool = False
    status: str = "trend"             # ok / breach（红线）/ trend（只看趋势）/ na（暂无数据）
    note: str = ""
    detail: dict[str, float | int] = field(default_factory=dict)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


@dataclass
class _Signed:
    deliverable_id: int
    first_pass: bool
    project_id: int = 0
    unresolved: int = 0
    fabricated: int = 0
    gb: int = 0
    kb_refs: set[str] = field(default_factory=set)


def _signed(db: Session, start: datetime, end: datetime, project_ids: list[int] | None) -> list[_Signed]:
    from ..kb import citations
    from ..project.deliverables import parse_sources

    q = (select(SignOff, Project).join(Project, Project.id == SignOff.project_id)
         .where(SignOff.action == "sign", SignOff.created_at >= start, SignOff.created_at < end,
                Project.deleted_at.is_(None)))
    if project_ids is not None:
        q = q.where(SignOff.project_id.in_(project_ids))
    out = []
    for sign, project in db.execute(q.order_by(SignOff.id)).all():
        history = db.scalars(select(SignOff.action).where(SignOff.deliverable_id == sign.deliverable_id,
                                                          SignOff.id < sign.id).order_by(SignOff.id)).all()
        if "sign" in history:                       # 只看这次签署之前、上一次签署之后
            history = history[len(history) - history[::-1].index("sign"):]
        r = _Signed(sign.deliverable_id, first_pass=history.count("submit") == 1 and "return" not in history,
                    project_id=project.id)
        for seq, ver in (sign.versions or {}).items():
            body = db.scalar(select(SectionVersion.body).join(DeliverableSection).where(
                DeliverableSection.deliverable_id == sign.deliverable_id, DeliverableSection.seq == int(seq),
                SectionVersion.version == int(ver)))
            if not body:
                continue
            src = parse_sources(db, project, body)
            r.unresolved += len(src["unresolved"])
            r.kb_refs |= {k["ref"] for k in src["knowledge"] if k["found"]}
            for c in citations.check(db, body):
                if c.doc_known and not c.found:
                    r.fabricated += 1
                elif c.found and GB.match(c.document):
                    r.gb += 1
        out.append(r)
    return out


signed = _signed   # 四期 F2③ 四格分列复用（indicators_v4）


def _latest_eval(db: Session) -> dict:
    d = db.scalar(select(AuditLog.detail).where(AuditLog.action == EVAL_ACTION, AuditLog.result == "allowed")
                  .order_by(AuditLog.id.desc()).limit(1))
    try:
        return json.loads(d) if d else {}
    except ValueError:
        return {}


def _effective(db: Session, refs: set[str]) -> int:
    """被引用且未被推翻：知识条目仍在用（未下线），现行版本没有复查不通过。"""
    if not refs:
        return 0
    rows = db.execute(select(KbItem.doc_id, KbVersion.recheck_state)
                      .join(KbVersion, KbVersion.id == KbItem.current_version_id)
                      .where(KbItem.doc_id.in_(refs), KbItem.retired.is_(False))).all()
    return sum(1 for _, st in rows if st != "failed")


def _edits(db: Session, start: datetime, end: datetime, project_ids: list[int] | None) -> tuple[list[float], list[float]]:
    q = select(JudgmentEvent).where(JudgmentEvent.kind == "edit", JudgmentEvent.magnitude.is_not(None),
                                    JudgmentEvent.created_at >= start, JudgmentEvent.created_at < end)
    if project_ids is not None:
        q = q.where(JudgmentEvent.project_id.in_(project_ids))
    events = db.scalars(q).all()
    titles: dict[tuple[int, str, int], str] = {}
    pids = {e.project_id for e in events}
    if pids:
        for pid, code, seq, title in db.execute(
                select(Deliverable.project_id, Deliverable.code, DeliverableSection.seq, DeliverableSection.title)
                .join(DeliverableSection, DeliverableSection.deliverable_id == Deliverable.id)
                .where(Deliverable.project_id.in_(pids))):
            titles[(pid, code, seq)] = title
    every = [float(e.magnitude) for e in events]
    advice = [float(e.magnitude) for e in events
              if ADVICE.search(titles.get((e.project_id, e.deliverable_code, e.section_seq or 0), ""))]
    return every, advice


def _draft_hours(db: Session, start: datetime, end: datetime, project_ids: list[int] | None) -> list[float]:
    """证据齐到初稿齐：每份交付件所有章节都有了第 1 版的时刻，减去此前最后一份证据的录入时刻（日历小时）。"""
    q = (select(Deliverable.id, Deliverable.project_id, DeliverableSection.id, SectionVersion.created_at)
         .join(DeliverableSection, DeliverableSection.deliverable_id == Deliverable.id)
         .outerjoin(SectionVersion, (SectionVersion.section_id == DeliverableSection.id) & (SectionVersion.version == 1))
         .join(Project, Project.id == Deliverable.project_id).where(Project.deleted_at.is_(None)))
    if project_ids is not None:
        q = q.where(Deliverable.project_id.in_(project_ids))
    firsts: dict[tuple[int, int], list[datetime | None]] = defaultdict(list)
    for did, pid, _sid, at in db.execute(q).all():
        firsts[(did, pid)].append(_aware(at) if at is not None else None)
    out = []
    for (_did, pid), ats in firsts.items():
        if not ats or any(a is None for a in ats):
            continue
        done = max(a for a in ats if a is not None)
        if not (_aware(start) <= done < _aware(end)):
            continue
        last = db.scalar(select(Evidence.created_at).where(Evidence.project_id == pid, Evidence.created_at <= done)
                         .order_by(Evidence.created_at.desc()).limit(1))
        if last is not None:
            out.append(round((done - _aware(last)).total_seconds() / 3600, 1))
    return out


def _kb(db: Session, start: datetime, end: datetime) -> tuple[int, int, int]:
    rows = db.execute(select(KbVersion.id, KbVersion.auto_review).where(
        KbVersion.published_at >= start, KbVersion.published_at < end)).all()
    ids = [i for i, _ in rows]
    auto = sum(1 for _, a in rows if a in AUTO_KINDS)
    human = len(db.scalars(select(KbReview.id).where(KbReview.version_id.in_(ids), KbReview.review_role.in_(HUMAN_ROLES),
                                                     KbReview.reviewer != "platform")).all()) if ids else 0
    return len(rows), auto, human


def compute(db: Session, start: datetime, end: datetime, project_ids: list[int] | None = None) -> dict:
    """project_ids 只给测试用（限定项目）；知识区指标不按项目分。"""
    signed = _signed(db, start, end, project_ids)
    n = len(signed)
    ev = _latest_eval(db)
    fab_deliv = sum(s.fabricated for s in signed)
    fab_eval = int(ev.get("with_kb_fabricated", 0) or 0)
    qualified = sum(1 for s in signed if s.first_pass and not s.unresolved and not s.fabricated)
    every, advice = _edits(db, start, end, project_ids)
    hours = _draft_hours(db, start, end, project_ids)
    published, auto, human = _kb(db, start, end)
    acc = ev.get("with_kb_accuracy")
    out = [
        Indicator("qualified_deliverables", "北极星", "合格交付件数",
                  "窗内签署，且门禁一次通过、来源标注全部核对得上、没有编造条款号", "趋势", qualified, "份",
                  note="“客户评审 ≤ 2 轮”这一条件待客户评审轮次采集后加上", detail={"signed": n}),
        Indicator("fabricated_clauses", "正确", "编造条款号次数",
                  "签署交付件里引用的文件在依据库有原文、条款号却没有的次数，加最近一次对比评测的编造条号次数",
                  "红线 0", fab_deliv + fab_eval, "次", redline=True,
                  status="breach" if fab_deliv + fab_eval else "ok",
                  detail={"deliverables": fab_deliv, "eval": fab_eval}),
        Indicator("gb_citations", "正确", "国标内容引用数（单列）",
                  "签署交付件里核对得上的国标条款引用次数；程序只能确认条款存在、不能确认内容，归抽检高风险层",
                  "趋势", sum(s.gb for s in signed), "次"),
        Indicator("eval_accuracy", "正确", "依据查询评测准确率", "最近一次有无知识库对比评测（kb-eval --compare）用知识库的准确率",
                  "红线 ≥ 90%", acc, "比例", redline=True,
                  status="na" if acc is None else ("ok" if acc >= EVAL_FLOOR else "breach"),
                  note="" if acc is not None else "还没有跑过对比评测",
                  detail={"questions": int(ev.get("questions", 0) or 0)} if ev else {}),
        Indicator("gate_first_pass", "完整", "门禁一次通过率", "窗内签署的交付件中，只提交一次、没被退回就签署的占比",
                  "趋势", _ratio(sum(1 for s in signed if s.first_pass), n), "比例",
                  status="trend" if n else "na", note="" if n else "窗内没有签署", detail={"signed": n}),
        Indicator("advice_edit_magnitude", "适用", "建议类章节改稿幅度",
                  "顾问改 Agent 稿的平均幅度（1 − 相似度），只算标题含建议、整改、规划、路线、措施的章节",
                  "趋势", round(statistics.fmean(advice), 4) if advice else None, "0～1",
                  status="trend" if advice else "na",
                  note="" if advice else "窗内没有改稿记录（判断数据埋点 JUDGMENT_CAPTURE 默认关）",
                  detail={"edits": len(advice), "all_sections": round(statistics.fmean(every), 4) if every else 0}),
        Indicator("client_review_rounds", "客户", "客户评审轮次", "每份对客交付件的客户评审轮次", "趋势",
                  status="na", note="平台还没有登记客户评审，四期随客户评审登记补上"),
        Indicator("draft_hours", "效率", "初稿时长（中位数）",
                  "证据齐（最后一份证据录入）到初稿齐（每章都有第 1 版）的日历小时，含 Agent 自动起草", "趋势",
                  statistics.median(hours) if hours else None, "小时", status="trend" if hours else "na",
                  note="" if hours else "窗内没有初稿齐的交付件", detail={"deliverables": len(hours)}),
        Indicator("fact_sheet_edit_rate", "调研", "事实确认单修改率", "事实确认单修改加待补占比", "趋势",
                  status="na", note="事实确认单随调研助手（E2）四期上线"),
        Indicator("effective_citations", "知识", "有效引用数",
                  "签署交付件引用、仍在用且现行版本没有复查不通过的知识条目数（去重）", "趋势",
                  _effective(db, set().union(*(s.kb_refs for s in signed)) if signed else set()), "条"),
        Indicator("expert_on_time", "专家", "派单按时完成率", "专家派单按时完成占比", "趋势",
                  status="na", note="专家派单功能上线后出数"),
        Indicator("auto_publish_rate", "知识", "自动发布率", "窗内发布的知识版本中，平台核验或知识审核 Agent 自动发布的占比",
                  "B22 实测后校准", _ratio(auto, published), "比例", status="trend" if published else "na",
                  note="" if published else "窗内没有发布", detail={"published": published, "auto": auto}),
        Indicator("human_minutes_per_100", "知识", "人工分钟 / 百条", "每发布 100 条知识的人工审核分钟数",
                  "B22 实测后校准", status="na",
                  note="审核不计时长，B22 实测出每审分钟数后折算；先给人工审核次数 / 百条",
                  detail={"human_reviews_per_100": round(human * 100 / published, 1) if published else 0,
                          "human_reviews": human}),
    ]
    return {"start": _aware(start).isoformat(), "end": _aware(end).isoformat(), "metrics": [asdict(m) for m in out]}
