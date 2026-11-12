"""演示数据：虚拟客户，不是真实客户信息。"""
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from .core.db import utcnow
from .models import (Confirmation, Deliverable, DeliverableSection, Project, QualityReview, SectionVersion,
                     SignOff)

DELIVERABLES = ["启动包", "调研包", "事实清单", "现状评估报告", "设计方案", "制度文件包", "实施路线图", "高层汇报"]


def _project(code, service, industry, tier, phase, week, total, score, milestone, statuses):
    p = Project(code=code, service=service, industry=industry, tier=tier, phase=phase, week=week,
                total_weeks=total, posture_score=score, next_milestone=milestone)
    p.deliverables = [Deliverable(code=f"D{i + 1}", title=t, status=statuses[i]) for i, t in enumerate(DELIVERABLES)]
    return p


def seed(db: Session) -> None:
    if db.scalar(select(Project.id).limit(1)) is not None:
        return
    ns = "not_started"
    bank = _project("BANK-A", "SOC 运营体系设计", "城商行", "standard", 3, 7, 12, 68, "11 月 25 日 方案评审会",
                    ["signed", "signed", "signed", "signed", "review", "drafting", ns, ns])
    grid = _project("GRID-B", "网络安全治理规划", "电力", "strict", 1, 3, 16, None, "11 月 20 日 CIO 访谈",
                    ["signed", "drafting", ns, ns, ns, ns, ns, ns])
    mfg = _project("MFG-C", "SOC 运营体系设计", "制造", "standard", 0, 1, 12, 52, "11 月 21 日 签范围确认单",
                   ["drafting", ns, ns, ns, ns, ns, ns, ns])
    bank.confirmations = [
        Confirmation(kind="方案取舍", title="一期人力模型选哪一种？", agent="方案设计 Agent"),
        Confirmation(kind="来源冲突", title="一级事件响应时限不一致", agent="依据与差距分析 Agent"),
        Confirmation(kind="证据不足", title="缺少近 3 个月的值班记录", agent="质量评审 Agent"),
    ]
    grid.confirmations = [Confirmation(kind="范围确认", title="摘要卡模板是否需要增加生产控制区专项", agent="调研访谈 Agent")]
    mfg.confirmations = [Confirmation(kind="范围确认", title="客户希望顺带评估云上日志，是否纳入本期？", agent="项目交付管理 Agent")]
    db.add_all([bank, grid, mfg])
    db.commit()


SAMPLE_SECTIONS = [
    ("项目目标与范围",
     "\n".join(["本项目为 BANK-A 设计 SOC 运营体系，范围包括组织与人员、流程、技术平台与度量四个能力域 [推断]。", "",
                "- 一期聚焦安全监测与事件响应", "- 不含渗透测试等专项评估"])),
    ("工作计划与里程碑",
     "\n".join(["| 阶段 | 周次 | 主要产出 |", "|---|---|---|", "| 启动 | 1 | D1 启动包 |",
                "| 调研 | 2～4 | D2 调研包、D3 事实清单 |", "| 评估 | 5～7 | D4 现状评估报告 |", "",
                "里程碑以双方确认的范围确认单为准 [推断]。"])),
]


def seed_signed_sample(db: Session) -> None:
    """补一份“已签署且有章节”的演示交付件（BANK-A D1），便于测试签署后的导出和只读（#42 测试建议）。
    只在 BANK-A D1 已签署且还没有章节时补，可重复执行。"""
    p = db.scalar(select(Project).where(Project.code == "BANK-A"))
    d = next((x for x in p.deliverables if x.code == "D1"), None) if p else None
    if d is None or d.status != "signed" or d.sections:
        return
    now = utcnow()
    for i, (title, body) in enumerate(SAMPLE_SECTIONS, 1):
        sec = DeliverableSection(seq=i, title=title, current_version=1, created_by="demo", created_at=now, updated_at=now)
        sec.versions.append(SectionVersion(
            version=1, body=body, char_count=len(body), body_sha=hashlib.sha256(body.encode()).hexdigest(),
            sources={"evidence": [], "knowledge": [], "inferred": 1,
                     "counts": {"evidence": 0, "knowledge": 0, "inferred": 1}, "unresolved": []},
            note="演示数据", author="demo"))
        d.sections.append(sec)
    d.revision, d.updated_at, d.updated_by = len(SAMPLE_SECTIONS) + 1, now, "demo"
    db.flush()
    versions = {str(s.seq): 1 for s in d.sections}
    r = QualityReview(project_id=p.id, deliverable_id=d.id, revision=d.revision, versions=versions, status="done",
                      conclusion="pass", minor=1, report="（演示数据）", agent_code="qa-reviewer", created_by="demo",
                      finished_at=now, issues=[{"severity": "建议", "location": "第 2 章 工作计划与里程碑",
                                                "dimension": "表达", "problem": "里程碑缺少具体日期",
                                                "suggestion": "补充日期", "by": "agent"}])
    db.add(r)
    db.flush()
    db.add_all([SignOff(project_id=p.id, deliverable_id=d.id, action=a, revision=d.revision, versions=versions,
                        review_id=r.id, actor=who, comment="（演示数据）")
                for a, who in (("submit", "demo-consultant"), ("sign", "demo-manager"))])
    db.commit()
