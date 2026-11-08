"""演示数据：虚拟客户，不是真实客户信息。"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Confirmation, Deliverable, Project

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
