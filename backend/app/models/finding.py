"""发现表（三期 F1，迁移 0039；4A 架构设计 v4.0 §4.3 发现记录 F-xx）。

一条发现 = 一个控制项在本项目的初判、风险和整改建议，是整改跟踪、行业基准和可验证交付件的基础。
字段对齐 OSCAL 评估结果（finding / observation / risk）与 POA&M 条目，只对齐字段，不引入 OSCAL 格式：
- control_code ↔ finding.target（控制项）；evidence_refs ↔ related-observations；basis_* ↔ 依据条款与版本；
- conclusion ↔ target.status（met / partial / unmet / insufficient / na）；current_score / target_score 现状与目标分；
- impact / likelihood / risk ↔ related-risks；priority / owner_dept / period / acceptance ↔ POA&M 整改四要素；
- responsibility ↔ 责任方（customer 客户自担、inherited 继承自云厂商、shared 共担）；
- status：draft 初判 → confirmed 确认 → signed 签署 → remediating 整改中 → verified 已复核。
agent_conclusion 留 Agent 的初判，确认时与顾问的结论组成训练三元组（只在 JUDGMENT_CAPTURE 打开时记，见 app/project/judgment.py）。
规则只在 app/project/findings.py；项目删除时一并删除。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, utcnow


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (UniqueConstraint("project_id", "fid", name="uq_findings_project_fid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    fid: Mapped[str] = mapped_column(String(8))                          # F-01，平台编号
    control_code: Mapped[str] = mapped_column(String(64), default="")
    domain: Mapped[str] = mapped_column(String(40), default="")
    item: Mapped[str] = mapped_column(String(200), default="")           # 要求要点
    current: Mapped[str] = mapped_column(String(500), default="")        # 现状
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)       # 证据编号
    basis_clause: Mapped[str] = mapped_column(String(120), default="")
    basis_version: Mapped[str] = mapped_column(String(32), default="")
    conclusion: Mapped[str] = mapped_column(String(16), default="")
    agent_conclusion: Mapped[str] = mapped_column(String(16), default="")
    current_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    impact: Mapped[str] = mapped_column(String(4), default="")
    likelihood: Mapped[str] = mapped_column(String(4), default="")
    risk: Mapped[str] = mapped_column(String(500), default="")
    priority: Mapped[str] = mapped_column(String(8), default="")
    owner_dept: Mapped[str] = mapped_column(String(64), default="")
    period: Mapped[str] = mapped_column(String(32), default="")
    acceptance: Mapped[str] = mapped_column(String(500), default="")
    recommendation: Mapped[str] = mapped_column(String(500), default="")
    responsibility: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    source: Mapped[str] = mapped_column(String(16), default="agent")     # agent / consultant
    agent_code: Mapped[str] = mapped_column(String(32), default="")
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
