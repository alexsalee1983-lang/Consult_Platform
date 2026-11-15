"""知识区数据表（M2）。字段依据 agents/01_公共规范.md 第 7 节，取值和校验见 app/kb/meta.py。

- kb_items：一个知识条目（doc_id 固定不变），放分类元数据；current_version_id 指向现行（已发布）版本。
- kb_versions：条目的每个版本，放标题、正文、来源、去标识、生效与复审日期；按状态流转，历史版本全部保留。
- kb_chunks：已发布版本切分后的片段，供检索。PostgreSQL 上另有生成列 tsv（中文全文索引，迁移 0011 创建），
  向量列在确定向量模型后加入（S2 检索 PR）。
- kb_reviews：双人审核记录（知识专家审内容、知识管理员审合规）。
- kb_uploads：投料上传记录，原件存对象存储知识区；只记机密级检测命中的类别，不记命中内容。
- kb_deid_reports：去标识审查报告，只记替换的类别、占位符和次数，不记被替换的原文。

知识区只收去标识后的做法，不收客户数据；项目区内容不会自动进入这里（架构设计第三节）。
"""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import false as sql_false

from ..core.db import KbBase, utcnow


def _default_visible_roles() -> list[str]:
    return ["consultant", "admin"]


class KbItem(KbBase):
    __tablename__ = "kb_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)   # L2-OPS-SOC-TPL-007
    layer: Mapped[str] = mapped_column(String(2), index=True)                  # L1～L5
    domain: Mapped[str] = mapped_column(String(3), index=True)                 # GOV / OPS ……
    product: Mapped[str] = mapped_column(String(8), index=True)                # SOC / GEN
    doc_type: Mapped[str] = mapped_column(String(3), index=True)               # TPL / MTD ……
    stage: Mapped[str] = mapped_column(String(8), index=True)                  # 启动……通用
    capabilities: Mapped[list] = mapped_column(JSON, default=list)             # 管理 / 运营 / 技术支撑
    industries: Mapped[list] = mapped_column(JSON, default=list)               # 通用 / 银行 / 电力……
    # 检索加权标签（二期 B2，知识区迁移 k0002）：项目勾选的模块声明 kb_tags，带同名标签的条目检索时加权
    tags: Mapped[list] = mapped_column(JSON, default=list)
    classification: Mapped[str] = mapped_column(String(16), default="internal")  # internal / restricted
    visible_roles: Mapped[list] = mapped_column(JSON, default=_default_visible_roles)
    owner: Mapped[str] = mapped_column(String(64), default="")                 # 专家负责人（用户名）
    # 批量导入的条目标识（迁移 0016）：条目文件里的 key，重复导入时据此修订而不是重复建条目；评测题也按它对应
    import_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("kb_versions.id", use_alter=True, name="fk_kb_items_current_version"), nullable=True)
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    versions: Mapped[list["KbVersion"]] = relationship(
        back_populates="item", foreign_keys="KbVersion.item_id", order_by="KbVersion.version")


class KbVersion(KbBase):
    __tablename__ = "kb_versions"
    __table_args__ = (UniqueConstraint("item_id", "version", name="uq_kb_versions_item_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("kb_items.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)                              # 1, 2, 3……
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    title: Mapped[str] = mapped_column(String(200))                            # 简明，不含客户信息
    summary: Mapped[str] = mapped_column(String(1000), default="")
    body: Mapped[str] = mapped_column(Text, default="")                        # 去标识后的正文（Markdown）
    applicability: Mapped[str] = mapped_column(String(500), default="")        # 适用条件
    change_note: Mapped[str] = mapped_column(String(1000), default="")         # 与上一版的差异说明
    source_type: Mapped[str] = mapped_column(String(16))                       # expert / project / public / internal
    source_detail: Mapped[str] = mapped_column(String(300), default="")        # 文件名、标准编号或授权编号
    # 依据（L4）专用：文件名、版本号、生效状态，Agent 引用时按【依据：文件名 版本号 条款号｜依据库 ID】输出
    ref_document: Mapped[str] = mapped_column(String(200), default="")
    ref_version: Mapped[str] = mapped_column(String(64), default="")
    ref_status: Mapped[str] = mapped_column(String(16), default="")
    deid_status: Mapped[str] = mapped_column(String(16), default="pending")    # pending / passed / not_needed
    deid_report_id: Mapped[int | None] = mapped_column(ForeignKey("kb_deid_reports.id"), nullable=True)
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("kb_uploads.id"), nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    review_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    submitted_by: Mapped[str] = mapped_column(String(64), default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 单人发布（公开资料、KB_SINGLE_REVIEWER_PUBLIC 开启时作者本人审核发布），其他知识专家补复审后清除
    single_review: Mapped[bool] = mapped_column(Boolean, server_default=sql_false())

    item: Mapped[KbItem] = relationship(back_populates="versions", foreign_keys=[item_id])
    reviews: Mapped[list["KbReview"]] = relationship(back_populates="version", order_by="KbReview.id")


class KbChunk(KbBase):
    __tablename__ = "kb_chunks"
    __table_args__ = (UniqueConstraint("version_id", "seq", name="uq_kb_chunks_version_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("kb_versions.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(String(300), default="")            # 所在章节标题路径
    text: Mapped[str] = mapped_column(Text)
    # 向量（迁移 0014）：PostgreSQL 上另有 embedding vector(512) 列和 HNSW 索引，不映射到 ORM，读写走 SQL
    # （app/kb/search.py）；这里记生成向量所用的模型，换模型后据此重新向量化
    embedding_model: Mapped[str] = mapped_column(String(64), default="")


class KbReview(KbBase):
    __tablename__ = "kb_reviews"
    __table_args__ = (UniqueConstraint("version_id", "reviewer", "review_role",
                                       name="uq_kb_reviews_version_reviewer_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("kb_versions.id"), index=True)
    reviewer: Mapped[str] = mapped_column(String(64))
    review_role: Mapped[str] = mapped_column(String(16))                      # content / compliance
    decision: Mapped[str] = mapped_column(String(16))                         # approve / reject
    comment: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    version: Mapped[KbVersion] = relationship(back_populates="reviews")


class KbUpload(KbBase):
    __tablename__ = "kb_uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    object_key: Mapped[str] = mapped_column(String(512), unique=True)          # 对象存储知识区的键
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    # received 待解析 / parsed 已解析 / deid_pending 去标识待确认 / deid_passed 去标识通过 / deid_rejected 退回 / failed
    status: Mapped[str] = mapped_column(String(16), default="received", index=True)
    guard_categories: Mapped[list] = mapped_column(JSON, default=list)        # 机密级命中类别，不记命中内容
    error: Mapped[str] = mapped_column(String(200), default="")
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # jobs 在主库，分库后不能建外键（迁移 0030）
    uploaded_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 解析结果（迁移 0012）：parsed_key 指向对象存储知识区里的解析结果 JSON（章节 + 片段）
    kind: Mapped[str] = mapped_column(String(16), default="")                 # docx / pdf / markdown / text
    parsed_key: Mapped[str] = mapped_column(String(512), default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    section_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 去标识通过或退回后，原件和未去标识的解析结果从对象存储删除（迁移 0013）
    raw_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KbDeidReport(KbBase):
    __tablename__ = "kb_deid_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("kb_uploads.id"), nullable=True, index=True)
    # [{"category": "客户名", "placeholder": "【客户】", "count": 3}, ……]，不记被替换的原文
    replacements: Mapped[list] = mapped_column(JSON, default=list)
    residual_findings: Mapped[list] = mapped_column(JSON, default=list)       # 替换后仍疑似可识别的类别
    status: Mapped[str] = mapped_column(String(16), default="pending")        # pending / passed / rejected / superseded
    reviewed_by: Mapped[str] = mapped_column(String(64), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 迁移 0013：脱敏审查 Agent 复查结果与人工确认（均不含原文）
    created_by: Mapped[str] = mapped_column(String(64), default="")
    total: Mapped[int] = mapped_column(Integer, default=0)                    # 替换总次数
    agent_status: Mapped[str] = mapped_column(String(16), default="")         # ok / skipped / failed
    agent_note: Mapped[str] = mapped_column(String(200), default="")
    conclusion: Mapped[str] = mapped_column(String(16), default="")           # 通过 / 有条件通过 / 退回（Agent 结论）
    risk_level: Mapped[str] = mapped_column(String(8), default="")            # 低 / 中 / 高
    combination_risks: Mapped[list] = mapped_column(JSON, default=list)       # [{description, handling}]
    manual_items: Mapped[list] = mapped_column(JSON, default=list)            # 需人工确认的项
    deid_key: Mapped[str] = mapped_column(String(512), default="")            # 去标识后正文（知识区）
    safe_filename: Mapped[str] = mapped_column(String(255), default="")       # 去标识后的文件名
    comment: Mapped[str] = mapped_column(String(1000), default="")            # 人工确认意见
