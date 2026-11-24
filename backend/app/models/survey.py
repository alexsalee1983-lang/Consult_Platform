"""四期 E2①：调研助手平台侧（迁移 0056）。规则只在 app/project/survey.py。

survey_questions 题库：全局，不连项目、不含客户信息（E1 题库文件导入，正文由线 2 出）。
survey_packs 调研包：按项目画像选题生成，存题号清单和包哈希，不存回传原文。
survey_answers 回传答案：封闭题程序直转“事实”（fact_status pending 待确认 / confirmed 已确认 / superseded 被同题另一条确认取代）；
“不清楚”只计数不成事实；开放题只存过了检查的文字（严格档按摘要卡规则检查）。确认只记确认人角色，不记姓名。
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import false as sql_false
from sqlalchemy.sql import true as sql_true

from ..core.db import Base, utcnow


class SurveyQuestion(Base):
    __tablename__ = "survey_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    qid: Mapped[str] = mapped_column(String(32), unique=True, index=True)   # SOC-ORG-001
    control: Mapped[str] = mapped_column(String(64), index=True)            # 所属控制项
    kind: Mapped[str] = mapped_column(String(8))                            # closed / open
    text: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)               # [{"key": "A", "label": 可观察状态}]，不含 X
    products: Mapped[list] = mapped_column(JSON, default=list)              # 空 = 全部产品
    industries: Mapped[list] = mapped_column(JSON, default=list)            # 空 = 全部行业
    variants: Mapped[dict] = mapped_column(JSON, default=dict)              # {行业: 行业题面}（E1 industry_variants）
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sql_true())
    unclear_count: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SurveyPack(Base):
    __tablename__ = "survey_packs"
    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_survey_packs_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    code: Mapped[str] = mapped_column(String(16))                 # P-01
    pack_hash: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(16))
    qids: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    returned: Mapped[int] = mapped_column(Integer, server_default="0")   # 回传次数


class SurveyAnswer(Base):
    __tablename__ = "survey_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    pack_id: Mapped[int] = mapped_column(ForeignKey("survey_packs.id"), index=True)
    qid: Mapped[str] = mapped_column(String(32), index=True)
    control: Mapped[str] = mapped_column(String(64))
    respondent_role: Mapped[str] = mapped_column(String(32))     # 受访角色，如“安全运营负责人”，不写姓名
    kind: Mapped[str] = mapped_column(String(8))                  # closed / open
    value: Mapped[str] = mapped_column(String(16), server_default="")   # 封闭题选项键
    text: Mapped[str] = mapped_column(Text, server_default="")          # 开放题文字（已过检查）
    fact_status: Mapped[str] = mapped_column(String(16), server_default="")   # 见模块说明；开放题为空
    conflict: Mapped[bool] = mapped_column(Boolean, server_default=sql_false())
    confirmed_role: Mapped[str] = mapped_column(String(32), server_default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
