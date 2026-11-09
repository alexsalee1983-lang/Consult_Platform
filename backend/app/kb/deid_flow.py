"""去标识流程：规则替换 → 脱敏审查 Agent 复查 → 人工确认。

1. 解析完成后，后台任务 kb.deid 自动跑第一轮（规则 + Agent 复查），生成去标识报告（待确认）。
2. 复核人（知识管理员）看报告和去标识后的预览；发现漏网的名称时，带补充词重跑（同步执行，补充词不保存、
   不写审计，只记个数）。
3. 复核人确认：
   - 通过：报告 passed；投料改用去标识后的文件名；**原件和未去标识的解析结果从对象存储删除**，只留去标识后正文。
   - 退回：报告 rejected；原件、解析结果、去标识结果全部删除。
   复核人必须是知识管理员，且不能是上传者本人（与知识发布的双人审核一致）。
报告、审计、日志都不记原文和“原文 → 占位符”对照（sanitizer.md“边界与禁止”）。
"""
from __future__ import annotations

import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import SessionLocal, utcnow
from ..core.permissions import KNOWLEDGE_ADMIN, roles_of
from ..models import KbDeidReport, KbUpload, User
from ..services import jobs, storage
from . import deid, deid_agent, parse
from .intake import AREA

PENDING, PASSED, REJECTED, SUPERSEDED = "pending", "passed", "rejected", "superseded"


class DeidFlowError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _sections(up: KbUpload) -> list[tuple[str, str]]:
    if not up.parsed_key or up.raw_deleted_at:
        raise DeidFlowError(409, "解析结果不存在或原件已删除，不能再做去标识")
    data = json.loads(storage.get(AREA, up.parsed_key))
    return [(s["heading"], s["text"]) for s in data["sections"]]


def _scrub(texts: list[str], terms: list[deid.Term]) -> list[str]:
    """把 Agent 写的风险描述也过一遍替换，避免名称进报告。"""
    return [deid.deidentify([("", t)], "x", terms).sections[0][1] for t in texts]


def latest_report(db: Session, upload_id: int) -> KbDeidReport | None:
    return db.scalars(select(KbDeidReport).where(KbDeidReport.upload_id == upload_id)
                      .order_by(KbDeidReport.id.desc()).limit(1)).first()


def run(db: Session, up: KbUpload, actor: str, extra_terms: list[deid.Term] | None = None) -> KbDeidReport:
    if up.status not in ("parsed", "deid_pending"):
        raise DeidFlowError(409, "当前状态不能做去标识")
    sections = _sections(up)
    human = list(extra_terms or [])
    first = deid.deidentify(sections, up.filename, human)
    agent = deid_agent.review(db, "\n\n".join(f"{h}\n{b}" for h, b in first.sections), actor, up.id)
    terms = human + agent.residual_terms
    result = deid.deidentify(sections, up.filename, terms) if agent.residual_terms else first

    chunks = parse.chunk([parse.Section(h, b) for h, b in result.sections])
    body = {"filename": result.filename, "sections": [{"heading": h, "text": b} for h, b in result.sections],
            "chunks": [{"heading": c.heading, "text": c.text} for c in chunks]}
    key = up.object_key.rsplit("/", 1)[0] + f"/deid-{utcnow().strftime('%Y%m%d%H%M%S%f')}.json"
    storage.put(AREA, key, json.dumps(body, ensure_ascii=False).encode(), "application/json")

    db.execute(update(KbDeidReport).where(KbDeidReport.upload_id == up.id, KbDeidReport.status == PENDING)
               .values(status=SUPERSEDED))
    risks = agent.combination_risks
    descs = _scrub([r["description"] for r in risks] + [r["handling"] for r in risks] + agent.manual_items, terms)
    n = len(risks)
    report = KbDeidReport(
        upload_id=up.id, replacements=[r.to_json() for r in result.replacements], residual_findings=[],
        status=PENDING, created_by=actor, total=result.total, agent_status=agent.status, agent_note=agent.note,
        conclusion=agent.conclusion, risk_level=agent.risk_level,
        combination_risks=[{"description": descs[i], "handling": descs[n + i]} for i in range(n)],
        manual_items=descs[2 * n:], deid_key=key, safe_filename=result.filename[:255])
    db.add(report)
    up.status = "deid_pending"
    db.commit()
    audit.record(db, actor=actor, action="kb.deid", result="allowed", upload_id=up.id, report_id=report.id,
                 replacements=result.total, extra_terms=len(human), agent_terms=len(agent.residual_terms),
                 agent_status=agent.status, conclusion=agent.conclusion)
    return report


def preview(report: KbDeidReport, limit: int = 20) -> list[dict]:
    if not report.deid_key or report.status == REJECTED:
        return []
    return json.loads(storage.get(AREA, report.deid_key)).get("chunks", [])[:limit]


def decide(db: Session, up: KbUpload, reviewer: User, decision: str, comment: str) -> KbDeidReport:
    if KNOWLEDGE_ADMIN not in roles_of(db, reviewer.id):
        raise DeidFlowError(403, "只有知识管理员能确认去标识结果")
    if reviewer.username == up.uploaded_by:
        audit.record(db, actor=reviewer.username, action="kb.deid_decision", result="blocked", upload_id=up.id,
                     reason="self_review")
        raise DeidFlowError(403, "不能确认自己上传的资料，请由另一位知识管理员确认")
    report = latest_report(db, up.id)
    if report is None or report.status != PENDING or up.status != "deid_pending":
        raise DeidFlowError(409, "没有待确认的去标识报告")
    if decision not in ("pass", "reject"):
        raise DeidFlowError(422, "decision 只能是 pass 或 reject")
    if decision == "pass" and report.conclusion == "退回":
        raise DeidFlowError(409, "脱敏审查 Agent 结论为退回，不能直接通过；请补充替换词重跑，或退回")

    raw_keys = [up.object_key, up.parsed_key]
    old_deid = db.scalars(select(KbDeidReport.deid_key).where(KbDeidReport.upload_id == up.id,
                                                             KbDeidReport.id != report.id)).all()
    delete_keys = raw_keys + [k for k in old_deid if k] + ([report.deid_key] if decision == "reject" else [])
    for k in delete_keys:
        if k:
            storage.delete(AREA, k)
    report.status = PASSED if decision == "pass" else REJECTED
    report.reviewed_by, report.reviewed_at, report.comment = reviewer.username, utcnow(), comment[:1000]
    up.status = "deid_passed" if decision == "pass" else "deid_rejected"
    up.filename = report.safe_filename or up.filename
    up.raw_deleted_at = utcnow()
    db.commit()
    audit.record(db, actor=reviewer.username, action="kb.deid_decision", result="allowed", upload_id=up.id,
                 report_id=report.id, decision=decision, deleted_objects=len([k for k in delete_keys if k]))
    return report


@jobs.handler("kb.deid")
def _deid_job(payload: dict, ctx: jobs.JobContext) -> None:
    with SessionLocal() as db:
        up = db.get(KbUpload, int(payload["upload_id"]))
        if up is None:
            raise jobs.JobError("投料记录不存在", retry=False)
        if up.status != "parsed":          # 已经做过（任务重复执行）或状态已变化
            return
        try:
            run(db, up, actor=up.uploaded_by)
        except DeidFlowError as e:
            raise jobs.JobError(str(e), retry=False) from e
