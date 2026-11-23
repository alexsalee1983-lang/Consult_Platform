"""知识区数据表（M2）。字段依据 agents/01_公共规范.md 第 7 节，取值和校验见 app/kb/meta.py。

- kb_items：一个知识条目（doc_id 固定不变，B15 起为 KB-000123，旧编号见 kb_item_aliases），放分类元数据；current_version_id 指向现行（已发布）版本。
- kb_versions：条目的每个版本，放标题、正文、来源、去标识、生效与复审日期；按状态流转，历史版本全部保留。
- kb_chunks：已发布版本切分后的片段，供检索。PostgreSQL 上另有生成列 tsv（中文全文索引，迁移 0011 创建），
  向量列在确定向量模型后加入（S2 检索 PR）。
- kb_reviews：双人审核记录（知识专家审内容、知识管理员审合规）。
- kb_uploads：投料上传记录，原件存对象存储知识区；只记机密级检测命中的类别，不记命中内容。
- kb_deid_reports：去标识审查报告，只记替换的类别、占位符和次数，不记被替换的原文。
- kb_mappings：映射矩阵（类型 MAP）版本里解析出的控制映射行（二期 B3，知识区迁移 k0003），供 kb.mapping 查询。

知识区只收去标识后的做法，不收客户数据；项目区内容不会自动进入这里（架构设计第三节）。
"""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import false as sql_false

from ..core.db import KbBase, utcnow


def _default_visible_roles() -> list[str]:
    return ["consultant", "admin"]


class KbItem(KbBase):
    __tablename__ = "kb_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)   # KB-000123（B15 起；旧编号进别名表）
    # 现行编号的序号（三期 B15，知识区迁移 k0012）：doc_id = KB- + 6 位补零的 seq；迁移时按 id 回填
    seq: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    layer: Mapped[str] = mapped_column(String(2), index=True)                  # L1～L5
    domain: Mapped[str] = mapped_column(String(3), index=True)                 # GOV / OPS ……
    product: Mapped[str] = mapped_column(String(8), index=True)                # SOC / GEN
    doc_type: Mapped[str] = mapped_column(String(3), index=True)               # TPL / MTD ……
    stage: Mapped[str] = mapped_column(String(8), index=True)                  # 启动……通用
    capabilities: Mapped[list] = mapped_column(JSON, default=list)             # 管理 / 运营 / 技术支撑
    industries: Mapped[list] = mapped_column(JSON, default=list)               # 通用 / 银行 / 电力……
    # 检索加权标签（二期 B2，知识区迁移 k0002）：项目勾选的模块声明 kb_tags，带同名标签的条目检索时加权
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # 公司控制项编号（4A 埋点，知识区迁移 k0008）：条目级，同 tags 不进版本、不走审核；控制项中枢 v1 之前只记不用
    control_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 知识类型（三期 B35 最小版，知识区迁移 k0017）："" 通用 / practice 公司做法 / golden 黄金样例（app/kb/meta.py）；
    # 后两类必须由作者以外的知识专家人工确认才能发布。slot：C10 章节槽位键，选填
    knowledge_kind: Mapped[str] = mapped_column(String(16), default="", server_default="", index=True)
    slot: Mapped[str] = mapped_column(String(64), default="", server_default="")
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


class KbItemAlias(KbBase):
    """条目别名（三期 B15，知识区迁移 k0010）：旧编号（legacy，换号）、跨环境迁移时对方的编号（transfer）、
    合并前的编号（merge）。别名永不删除，条目下线后照样能解析（结果为已下线）；与 kb_items.doc_id 互斥，
    只经 app/kb/refs.add_alias 写入。"""
    __tablename__ = "kb_item_aliases"

    alias: Mapped[str] = mapped_column(String(40), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("kb_items.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KbItemLabel(KbBase):
    """条目多值标签（三期 B15，知识区迁移 k0011）：kind 为 product / industry / control / capability / domain / tag，
    一个条目可以挂多个产品、多个行业。只经 app/kb/labels.py 写入；读取以本表为准，kb_items 上的旧列只双写，
    给回滚和旧报表用。source：manual（条目文件 / 界面）/ legacy（迁移回填）/ derived（B19 由控制项推出）。"""
    __tablename__ = "kb_item_labels"
    __table_args__ = (Index("ix_kb_item_labels_kind_value", "kind", "value", "item_id"),)

    item_id: Mapped[int] = mapped_column(ForeignKey("kb_items.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    value: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")


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
    ref_code: Mapped[str] = mapped_column(String(100), default="")          # 文号或标准号（二期 B7）
    ref_replaced_by: Mapped[str] = mapped_column(String(200), default="")   # 已被替代时：替代文件名和版本
    ref_replaces: Mapped[str] = mapped_column(String(200), default="")      # 本文件替代的旧文件（名称或文号，三期 B26）
    ref_amends: Mapped[str] = mapped_column(String(200), default="")        # 本文件部分修订的文件（三期 B26）
    deid_status: Mapped[str] = mapped_column(String(16), default="pending")    # pending / passed / not_needed
    deid_report_id: Mapped[int | None] = mapped_column(ForeignKey("kb_deid_reports.id"), nullable=True)
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("kb_uploads.id"), nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    repeal_date: Mapped[date | None] = mapped_column(Date, nullable=True)   # 废止日（三期 B26，到日自动切换）
    review_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    submitted_by: Mapped[str] = mapped_column(String(64), default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 单人发布（公开资料、KB_SINGLE_REVIEWER_PUBLIC 开启时作者本人审核发布），其他知识专家补复审后清除
    single_review: Mapped[bool] = mapped_column(Boolean, server_default=sql_false())
    # 自动发布（二期 B10）：platform = 平台核验发布的依据原文；空 = 人工审核。sample_state：抽检 pending / passed / failed
    auto_review: Mapped[str] = mapped_column(String(16), default="")
    sample_state: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 使用反馈待复查（二期 B13，知识区迁移 k0007）：顾问在交付件里常删掉 Agent 引用的条目时由平台标记，进抽检队列；
    # recheck_state pending / passed / failed，recheck_reason 只写计数（不写交付件内容），recheck_at 为最近一次标记或处理时间
    recheck_state: Mapped[str] = mapped_column(String(16), default="", index=True)
    recheck_reason: Mapped[str] = mapped_column(String(200), default="")
    recheck_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 候选影子期（三期 B24①，知识区迁移 k0015）：自动发布先落 status=candidate，candidate_since 为进入候选的时间；
    # candidate_result 空 = 未出结果，promoted 晋级 / returned 抽检不通过退回人工 / superseded 被更新的版本替代
    candidate_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    candidate_result: Mapped[str] = mapped_column(String(16), default="")
    # 起草模型与家族（三期 G2③，知识区迁移 k0016）：Agent 起草导入时记，专家修订没写模型的沿用上一版；知识审核据此换家族
    model: Mapped[str] = mapped_column(String(100), server_default="")
    model_family: Mapped[str] = mapped_column(String(32), server_default="")

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


class KbMapping(KbBase):
    """映射矩阵条目（doc_type MAP）某个版本里的一行：源标准条款 ↔ 目标标准条款（二期 B3）。

    建版本时由 app/kb/mapping.py 从正文表格解析写入，随版本只追加；查询只取已发布、未下线的现行版本。
    只存条款号、关系和简短说明，不存标准原文。"""
    __tablename__ = "kb_mappings"
    __table_args__ = (UniqueConstraint("version_id", "seq", name="uq_kb_mappings_version_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("kb_versions.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    source_std: Mapped[str] = mapped_column(String(100), index=True)      # ISO/IEC 27001:2022
    source_ref: Mapped[str] = mapped_column(String(40), index=True)       # A.5.15
    target_std: Mapped[str] = mapped_column(String(100), index=True)      # GB/T 22239-2019
    target_ref: Mapped[str] = mapped_column(String(40), index=True)       # 8.1.4.2
    relation: Mapped[str] = mapped_column(String(16))                     # equivalent / partial / related
    note: Mapped[str] = mapped_column(String(500), default="")


class KbReview(KbBase):
    __tablename__ = "kb_reviews"
    __table_args__ = (UniqueConstraint("version_id", "reviewer", "review_role",
                                       name="uq_kb_reviews_version_reviewer_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("kb_versions.id"), index=True)
    reviewer: Mapped[str] = mapped_column(String(64))
    review_role: Mapped[str] = mapped_column(String(16))                      # content / compliance / platform / agent / sample / reconfirm（复审到期 Agent 复查）
    decision: Mapped[str] = mapped_column(String(16))                         # approve / reject；Agent 存疑为 flag
    comment: Mapped[str] = mapped_column(String(1000), default="")
    # 知识审核 Agent（二期 B11，review_role=agent）的逐项检查 checks 和问题清单 issues；人工审核为空
    findings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    version: Mapped[KbVersion] = relationship(back_populates="reviews")


class KbPracticeDecision(KbBase):
    """三期 B17（拆出）：公司做法确认队列每条决定的耗时与修改幅度（迁移 k0018）。不记正文。"""
    __tablename__ = "kb_practice_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("kb_versions.id"), index=True)
    reviewer: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))                       # adopt 采纳 / modify 修改后采纳 / reject 驳回
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)            # 卡片打开到拍板（前端计时，封顶 1 小时）
    change_ratio: Mapped[float] = mapped_column(Float, default=0.0)        # 修改幅度 0～1（1 - 相似度），采纳、驳回为 0
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    # 来源项目代号（知识区迁移 k0009，二期 P16）：项目沉淀投料时由项目成员选填，删除确认页据此预填经验回流投料编号；
    # 主库与知识区分库，不建外键；投料列表、详情不返回
    source_project: Mapped[str] = mapped_column(String(32), default="", index=True)


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


class KbFramework(KbBase):
    """标准 / 框架的一个版本（三期 B19，知识区迁移 k0013）。换版时旧版 status=superseded、superseded_by 指新版。"""
    __tablename__ = "kb_frameworks"
    __table_args__ = (UniqueConstraint("code", "version", name="uq_kb_frameworks_code_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32))                             # ISO27001 / CSF / MLPS
    version: Mapped[str] = mapped_column(String(32))                          # 2022 / 2.0 / 2019
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="current")        # current / superseded
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("kb_frameworks.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KbControl(KbBase):
    """公司控制项（三期 B19）：编号全局唯一、只增不改，按目录版本记一行。

    applicability：适用性规则 {industries, mlps_levels, jurisdictions, deployments}，缺省或空表示不限（参照 800-53B 基线、OSCAL Profile）；
    params：组织定义参数 [{id, label, default}]（如日志保留月数）。差距分析的评估维度，不是测评或认证判定项。"""
    __tablename__ = "kb_controls"
    __table_args__ = (UniqueConstraint("code", "catalog_version", name="uq_kb_controls_code_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), index=True)                 # SOC.ORG.01
    catalog_version: Mapped[str] = mapped_column(String(16))                  # v0
    name: Mapped[str] = mapped_column(String(200))
    product: Mapped[str] = mapped_column(String(16), index=True)              # SOC / IRE
    domain: Mapped[str] = mapped_column(String(60))
    industries: Mapped[list] = mapped_column(JSON, default=list)
    applicability: Mapped[dict] = mapped_column(JSON, default=dict)
    params: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(12), default="draft")          # draft / active / retired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KbControlCheckpoint(KbBase):
    """控制项下的检查点（参照 SP 800-53A 评估目标）：题库、评分锚定、发现初判挂到这里。methods ⊆ {examine, interview, test}。"""
    __tablename__ = "kb_control_checkpoints"
    __table_args__ = (UniqueConstraint("control_id", "code", name="uq_kb_control_checkpoints_control_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    control_id: Mapped[int] = mapped_column(ForeignKey("kb_controls.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(48), index=True)                 # SOC.ORG.01.a
    seq: Mapped[int] = mapped_column(Integer)
    objective: Mapped[str] = mapped_column(String(500))
    methods: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(12), default="draft")


class KbControlMapping(KbBase):
    """控制项 ↔ 框架条款（三期 B19）：relation 等价 / 子集 / 超集 / 交集（NIST IR 8477、SCF STRM，与 C12 同一套取值），
    strength 1～10，rationale 简短理由；只存条款号，不存标准原文。"""
    __tablename__ = "kb_control_mappings"
    __table_args__ = (UniqueConstraint("control_id", "framework_id", "clause", "mapping_version",
                                       name="uq_kb_control_mappings_key"),
                      Index("ix_kb_control_mappings_framework_clause", "framework_id", "clause"))

    id: Mapped[int] = mapped_column(primary_key=True)
    control_id: Mapped[int] = mapped_column(ForeignKey("kb_controls.id", ondelete="CASCADE"), index=True)
    framework_id: Mapped[int] = mapped_column(ForeignKey("kb_frameworks.id"))
    clause: Mapped[str] = mapped_column(String(40))
    relation: Mapped[str] = mapped_column(String(8))
    strength: Mapped[int] = mapped_column(Integer)
    rationale: Mapped[str] = mapped_column(String(300), default="")
    mapping_version: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KbSource(KbBase):
    """来源登记表（三期 B16，知识区迁移 k0014）：采集机只按这张表里试用、正式的来源抓取。

    状态 candidate 候选 → trial 试用 → active 正式 → demoted 降级 → retired 停用；许可和权威类别必填，
    等级由类别推出（B27 最小版三级）。规则只在 app/kb/source_registry.py。
    """

    __tablename__ = "kb_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)        # SR-0001
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500), unique=True)
    host: Mapped[str] = mapped_column(String(253), index=True)
    publisher: Mapped[str] = mapped_column(String(200), default="")
    license: Mapped[str] = mapped_column(String(300))
    authority_kind: Mapped[str] = mapped_column(String(16))                       # 法律……厂商资料
    authority_level: Mapped[int] = mapped_column(Integer, index=True)             # 1 / 2 / 3
    status: Mapped[str] = mapped_column(String(12), default="candidate", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KbSourceEvent(KbBase):
    """来源状态变更记录，只追加。"""

    __tablename__ = "kb_source_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("kb_sources.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str] = mapped_column(String(12), default="")
    to_status: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str] = mapped_column(String(300), default="")
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KbGap(KbBase):
    """知识缺口池（三期 B25②，知识区迁移 k0019）：五类触发源的信号按 gap_key 合并，每周排计划。

    只存编号与计数，不存原文：gap_key 是来源或依据编号、或网址；events 只是事件编号。
    状态 open 未关闭 → closed 已关闭（close_reason：published 已入库 / collected 已采集 / dropped 不做 / duplicate 重复）；
    关闭后又来新信号就重新打开（reopened 加一）。规则只在 app/kb/gap_pool.py。
    """

    __tablename__ = "kb_gaps"

    id: Mapped[int] = mapped_column(primary_key=True)
    gap_key: Mapped[str] = mapped_column(String(500), unique=True)
    kinds: Mapped[dict] = mapped_column(JSON, default=dict)                     # {触发源: 信号次数}
    events: Mapped[list] = mapped_column(JSON, default=list)                    # 事件编号
    impact: Mapped[int] = mapped_column(Integer, default=0)
    due: Mapped[date | None] = mapped_column(Date, nullable=True)
    authority: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)
    plan: Mapped[str] = mapped_column(String(12), default="")                   # direct / confirm / deferred / waiting
    score: Mapped[float] = mapped_column(Float, default=0.0)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str] = mapped_column(String(16), default="")
    reopened: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
