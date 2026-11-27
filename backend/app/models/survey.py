"""四期 E2①：调研助手平台侧（迁移 0056）。规则只在 app/project/survey.py。

survey_questions 题库：全局，不连项目、不含客户信息（E1 题库文件导入，正文由线 2 出）。
survey_packs 调研包：按项目画像选题生成，存题号清单和包哈希，不存回传原文。
survey_answers 回传答案：封闭题程序直转“事实”（fact_status pending 待确认 / auto 自动确认 / confirmed 人工确认 /
superseded 被同题另一条确认取代）；0057 起即答案池：每条记来源渠道 channel、可信度来源 source、抽检状态 spot_check；
survey_selfeval_packs 客户自评包（0058）：从调研包按受访角色派生，记版本、包哈希、截止日、发出时间、提醒阶段和导入的答案编号；
口令和答卷原文不存。
“不清楚”只计数不成事实；开放题只存过了检查的文字（严格档按摘要卡规则检查）。确认只记确认人角色，不记姓名。
"""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    key_control: Mapped[bool] = mapped_column(Boolean, server_default=sql_false())   # 关键控制项总是人工确认（0057）
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
    channel: Mapped[str] = mapped_column(String(16), server_default="onsite")  # 来源渠道（0057）
    source: Mapped[str] = mapped_column(String(16), server_default="oral")     # 可信度来源（0057）
    spot_check: Mapped[str] = mapped_column(String(16), server_default="")     # 自动确认的抽检状态（0057）
    conflict: Mapped[bool] = mapped_column(Boolean, server_default=sql_false())
    confirmed_role: Mapped[str] = mapped_column(String(32), server_default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SelfEvalPack(Base):
    __tablename__ = "survey_selfeval_packs"
    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_survey_selfeval_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    pack_id: Mapped[int] = mapped_column(ForeignKey("survey_packs.id"), index=True)
    code: Mapped[str] = mapped_column(String(16))                 # S-01
    version: Mapped[int] = mapped_column(Integer, server_default="1")   # 同一调研包同一角色第几版
    respondent_role: Mapped[str] = mapped_column(String(32))
    pack_hash: Mapped[str] = mapped_column(String(64))            # 自评包明文（不含哈希）的 SHA-256
    qids: Mapped[list] = mapped_column(JSON, default=list)
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), server_default="issued")   # issued / sent / imported / superseded
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remind_stage: Mapped[int] = mapped_column(Integer, server_default="0")    # 0 / 1 前一天 / 2 当天 / 3 逾期升级
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answer_ids: Mapped[list] = mapped_column(JSON, default=list)              # 本次导入的答案编号（撤销用）
    unclear_qids: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
