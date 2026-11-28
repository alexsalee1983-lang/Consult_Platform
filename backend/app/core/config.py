from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"   # dev 开发 / test 测试机 / prod 生产
    database_url: str = "postgresql+psycopg://consult:consult@postgres:5432/consult"
    # 知识区数据库（M5 ③，决策记录 2026-11-09）：单独的库、单独的账号，只放 kb_ 开头的表；
    # 留空表示与 DATABASE_URL 同库（本地开发、单元测试），生产必须分库（启动自检会拒绝）。
    kb_database_url: str = ""
    redis_url: str = "redis://redis:6379/0"

    # 对象存储（S3 接口，一期为自建 MinIO）；留空表示未启用（本地开发、单元测试）。
    # 项目区与知识区分桶（架构设计第三节），桶默认开启服务端加密。
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    # 对象存储根账号名（只用名字核对后端没有用根账号连接，口令不给后端；三期 G1 最小权限）
    minio_root_user: str = ""
    minio_secure: bool = False
    minio_bucket_project: str = "project-area"
    minio_bucket_knowledge: str = "knowledge-area"

    # 公司私有化部署的白名单大模型，OpenAI 兼容接口。留空时平台用演示回复，便于先跑通流程。
    # 一期测试用公司内部离线部署的 DeepSeek V4.1 Flash，三个能力档先都指向它；
    # 模型 ID 以模型平台接口说明为准，拿到后只改 .env。
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model_reasoning: str = "deepseek-v4.1-flash"
    llm_model_standard: str = "deepseek-v4.1-flash"
    llm_model_light: str = "deepseek-v4.1-flash"
    # 模型目录 v1（三期 G2）：留空 = 照旧按上面三个变量单模型调用；填 YAML/JSON 文件路径或一段 JSON 时
    # 按目录选首选和备选模型，评审、知识审核、仲裁强制与生成方不同家族（app/services/model_catalog.py）
    llm_model_catalog: str = ""
    llm_connect_timeout_seconds: float = 5        # 建立连接超时
    llm_timeout_seconds: int = 120                # 等待模型回复（非流式整体 / 流式两段输出之间）的超时
    llm_max_retries: int = 2                      # 连接失败、超时、429、5xx 时重试次数（流式只在开始输出前重试）
    llm_retry_backoff_seconds: float = 1.0        # 指数退避基数：1s、2s、4s……
    llm_max_concurrency: int = 0                  # 四期 A31⑪：同时在途的模型调用上限（0 = 不限），满了按 对话>交付链>知识后台 排队
    llm_max_continuations: int = 3                # 输出被截断（finish_reason=length）时最多续写几次，仍不完整按失败处理
    llm_health_ttl_seconds: int = 30              # 模型可用性检查结果缓存时间
    # 自动假名化：把项目卡中登记的客户名、人名、系统名替换成占位符再发给模型，回复里再换回来。
    # 模型为公司私有化部署，默认关闭；以后接入云端模型时，标准档必须开启（决策记录 2026-11-07）。
    llm_pseudonymize: bool = False
    # 外部模型（公司外部的云端服务，如 DeepSeek 官方 API）。决策记录 2026-11-08：只允许在开发和测试环境、
    # 只处理虚拟客户数据时使用。开启后：严格档项目一律拒绝调用；自动假名化强制开启；审计标记 external；
    # 界面提示“勿输入真实客户信息”；生产环境（APP_ENV=prod）开启则拒绝启动。
    llm_external: bool = False
    # 对话里 Agent 自主调用工具（二期 A7，function calling；负责人 11-16：公司模型支持工具调用）。
    # 关掉时退回提问前检索注入；每次提问最多 llm_tool_max_rounds 轮工具调用，之后要求模型直接答复。
    llm_tool_calling: bool = True
    llm_tool_max_rounds: int = 4
    llm_tool_max_calls: int = 8       # 每次提问最多执行几次工具调用，超过的回错误说明并要求直接答复
    llm_model_tools: str = ""
    # 我的任务（三期 B17②）：分数 = (1 + 影响权重 × 影响) × (1 + 不确定度权重 × 不确定度)；超出每日额度的折叠。
    tasks_impact_weight: float = 1.0
    tasks_uncertainty_weight: float = 2.0
    tasks_daily_quota: int = 20         # 对话带工具时用的模型；空为按 Agent 等级选（工具调用不稳的轻量模型可换掉）

    # 向量服务（OpenAI 兼容 /embeddings），同样只经网关调用。决策记录 2026-11-08：公司模型平台的 embedding
    # 接口就绪前，用 CPU 上的小型中文向量模型（docker compose 的 embed 服务）；就绪后只改这里并重新向量化。
    # 留空表示不启用向量检索，知识检索只用全文检索。维度必须与数据库向量列一致（迁移 0014：512）。
    embed_base_url: str = ""
    embed_api_key: str = ""
    embed_model: str = "BAAI/bge-small-zh-v1.5"
    embed_batch_size: int = 32
    # 向量检索的相似度下限（余弦相似度，0～1）：向量检索总会返回“最近的”切片，哪怕毫不相关，
    # 所以只有向量命中、全文没命中的结果，相似度低于下限就丢掉。依据查询更严，避免把弱相关条款当依据引用。
    # 默认值按 bge-small-zh-v1.5 的经验取，S2 评测（scripts/kb_eval.py）后校准。
    kb_vector_min_similarity: float = 0.5
    # 依据查询门槛：示范评测（加“标题 + 摘要”前缀后）正例最低 0.5019、负例最高 0.4486，取空隙里偏严的 0.48，
    # 两侧各留约 0.02～0.03。0.55 会误杀正例（B005），0.5 离正例只差 0.002。正式评测题扩充后重新校准。
    kb_basis_min_similarity: float = 0.48
    # 公开资料单人发布（决策记录 2026-11-13）：开启后，来源为公开资料、无需去标识的版本可由作者本人完成两项审核
    # （须同时持有知识专家、知识管理员角色），发布后标记“单人发布”，等其他知识专家补复审。项目回流等其他来源不适用。
    kb_single_reviewer_public: bool = False
    # 起草引用校验（二期 B8）：解读、映射、案例、异议应答引用的“文件 + 条款号”在库里找不到依据原文时不能提交审核。
    # 关掉只显示“待核”、不拦提交（例如依据原文还没切条入库的过渡期）。
    kb_citation_gate: bool = True
    # 四期 B28①：知识条目可选的语种与辖区（逗号分隔，app/kb/multilang.py）。辖区默认含负责人 2026-11-16 定的
    # 7 个非洲国家（埃及、科特迪瓦、塞内加尔、南非、安哥拉、埃塞俄比亚、尼日利亚）和港、澳、泰、印尼、越、新、马
    kb_languages: str = "zh,zh-Hant,en,fr,pt,ar,th,id,vi,ms,am"
    kb_jurisdictions: str = "CN,HK,MO,TH,ID,VN,SG,MY,EG,CI,SN,ZA,AO,ET,NG"
    # 三期 F4 最小版：改稿差异转候选（app/project/revision_mining.py）。只在测试机、试用机虚拟数据上开；生产环境一律不生效
    f4_revision_mining: bool = False
    # 依据原文自动核验发布（二期 B10，决策记录 2026-11-15）：kb-source-publish 核验通过的 SRC 由平台自动发布，
    # 每批按比例随机抽检；最近 20 次抽检不通过率超过上限时暂停自动发布、全部转人工审核，直到抽检恢复。
    kb_src_auto_publish: bool = True
    # 三期 B20：每批抽检量按统计抽样（超几何零接收）算，下面两个比例只作兜底下限；停止规则攒满 20 个样本才启用
    kb_sample_defect_max: float = 0.1
    kb_sample_confidence: float = 0.8
    kb_sample_rate_src: float = 0.0
    kb_sample_rate_src_partial: float = 0.4   # 有条款被机密级检测退回的那几份（负责人 2026-11-16）
    kb_sample_fail_max: float = 0.1
    # 四期 E3① 答案池：自动确认的事实按此比例随机进人工抽检队列；自适应调比由 F5（#473）替换 survey.spot_rate()
    survey_spot_rate: float = 0.1
    # 知识审核 Agent（二期 B11，二期开发计划第十二节第二级）：公开资料的解读、映射、方法和只含公开资料的案例、异议应答，
    # 提交后由程序规则 + 知识审核 Agent（经网关）逐项检查，全部通过的自动发布并按比例抽检，存疑的进人工队列。
    # 最近 20 次抽检不通过率超过 KB_SAMPLE_FAIL_MAX 时暂停；超过一半时抽检比例加倍。
    kb_agent_auto_publish: bool = True
    kb_sample_rate_agent: float = 0.2
    # 候选影子期（三期 B24①）：B10、B11 自动发布通过的版本先落 candidate，只供评测和影子检索，交付侧检索仍用旧的已发布版本；
    # 满 KB_CANDIDATE_DAYS 天且抽检没有判不通过的，每日计划任务自动晋级为 published。关掉回到自动发布直接 published。
    # 四期 B43 收录标准与判定（app/kb/intake_criteria.py）：知识审核 Agent 按 6 条收录标准给四种判定之一。
    # 关（默认）：判定只记录，非“收录”的一律不自动发布、转人工；开：评测达标后由负责人打开，“不收 / 合并为印证 / 退回补充”
    # 由平台直接退回，不再等人，Agent 漏答收录标准或类型问题的也不算“收录”。
    kb_intake_verdict_live: bool = False
    # 四期 B43 公开资料合规关自动为主（app/kb/compliance_auto.py，负责人 2026-11-27“1可以”）：程序规则 + 知识审核 Agent
    # 判合规，通过的平台记合规审核通过并按比例抽检；去标识没过、来源不在白名单、疑似投毒、抽检差错超门槛等例外才留给知识管理员。
    kb_compliance_auto: bool = True
    kb_sample_rate_compliance: float = 0.1
    kb_candidate_shadow: bool = True
    kb_candidate_days: int = 7
    # 有无知识库对比评测（二期 B1 补充）：kb-eval --compare 用知识库的准确率达标线；开启门禁时最近一次对比评测
    # 未达标就暂停自动发布（B10、B11 两级），直到再跑一次达标
    kb_eval_min_accuracy: float = 0.9
    kb_eval_gate: bool = True
    # 知识批次页（三期 B18）发起 kb-import、kb-eval 时，资料目录和题目文件只能在这个目录下（后端容器里的只读挂载）
    kb_batch_dir: str = "/srv/knowledge/downloads"
    # 就绪度看板（三期 F2②）读哪些章节级装配评测集（A24 格式，逗号分隔的文件或目录，目录取其中 *.yaml）；
    # 留空用平台回归样例 knowledge/eval/assembly_samples.yaml。真实评测集在私有资料仓，部署机指到 /srv/knowledge/downloads 下
    kb_readiness_evalsets: str = ""
    # 四格关键章节就绪度目标（四期 F2③，按三期实测校准后填）：如 "SOC·银行=0.7,IRE·电力=0.6"；没配的格看板显示“未校准”
    kb_readiness_targets: str = ""
    # 官方来源白名单之外再认哪些网站（逗号分隔的域名，例如 tc260.org.cn）；内置 gov.cn、nist.gov
    kb_source_hosts: str = ""
    # 四期 B41 缺口闭环：一批回传导入后正文率（正文页 ÷ 非栏目页文件）低于此值或入库数为 0 推入库报警卡给知识管理员
    kb_body_rate_min: float = 0.5
    # 来源推荐 Agent 命中率（推荐的网址最终采到 ÷ 已有结果的推荐）低于此值、且样本不少于下一项时自动停用（近 90 天）
    kb_recommend_min_hit: float = 0.3
    kb_recommend_min_samples: int = 10

    # 重排服务（/rerank，交叉编码模型，决策记录 2026-11-11），同样只经网关调用。留空表示不启用：检索只用 RRF 排序，
    # 依据查询退回上面的余弦相似度门槛。docker compose 默认指向 rerank 服务（tools/rerank）。
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3-int8"   # int8 量化版（决策记录 11-11 补充）
    # 外部重排服务（如硅基流动 Qwen3-Reranker，决策记录 2026-11-14）：与 LLM_EXTERNAL 同样只允许开发和测试环境、只处理
    # 虚拟客户数据；查询和知识片段原样发出（片段已去标识，查询先过机密级检测），不做假名化。生产环境开启即拒绝启动。
    rerank_external: bool = False
    # 送去重排的候选片段数（RRF 融合后的前 N 个）和每个片段的字数上限。CPU 上重排耗时大致与“个数 × 字数”成正比。
    kb_rerank_candidates: int = 20   # 决策记录 2026-11-16：评测 85% 未达标，回调到 20（11-13 曾为 10）
    kb_rerank_max_chars: int = 300
    # 依据查询门槛（重排分数，0～1，sigmoid 后）：启用重排时取代 KB_BASIS_MIN_SIMILARITY。初值按开发电脑上的
    # 示范评测：正例最低 0.14（B004），该查不到的题最高 0.001，取 0.05。已定（决策记录 2026-11-20）；生产换向量或重排模型后重新校准。
    kb_basis_min_rerank: float = 0.05
    # 交付流程自动推进（二期 C5，app/project/autoflow.py）：自动发起交付链、自动评审、门禁通过自动提交签署
    auto_flow: bool = True
    auto_run_quiet_hours: float = 2       # 最近一条证据之后这么久没有新证据才自动发起交付链
    # 判断数据埋点（顾问改 Agent 稿的幅度、确认卡选择）：只记计数和类别；等法务意见，默认关（决策记录 2026-11-18）
    judgment_capture: bool = False
    auto_review_idle_hours: float = 24    # 顾问最后一次修改之后这么久没再改才自动评审
    # 门禁通过后自动提交签署的交付件（自主度 L3）；不在其中的为 L2，由顾问看过标出的问题后提交（决策记录 2026-11-18）
    auto_submit_deliverables: str = "D1,D2,D6,D8"
    # 四期 A26（负责人 2026-11-25）：机械问题一键修订最多自动修几轮，超出转人工
    revise_max_rounds: int = 2
    # 写签分离豁免的交付件（对内件，起草人 / 最后修改人也能签）；其余对客户交付件阻断
    sign_separation_exempt: str = "D3"
    # 门禁 0 问题时“核对与签署合并”、项目经理可批量签的交付件
    merged_sign_deliverables: str = "D3"
    # 普通检索的重排分数下限：只挡掉趋近 0 分的无关结果（测试 #34：该查不到的 S011 得分 0.0003～0.0008）。
    # 比依据查询宽得多，宁可多给几条弱相关的；已定（决策记录 2026-11-20）；与 KB_BASIS_MIN_RERANK 一起，生产换向量或重排模型后重新校准。
    kb_search_min_rerank: float = 0.005

    seed_demo_data: bool = True

    # Agent md 目录（01_公共规范.md、roles/*.md）。留空时依次找 /srv/agents（镜像内）和仓库根目录的 agents/。
    # 测试机可把仓库的 agents/ 只读挂载到容器里，md 改了不用重建镜像，运行时会自动重新加载。
    agents_dir: str = ""
    # 服务产品与模块配置的上级目录（下有 products/、modules/，二期 A1）。留空时依次找 /srv（镜像内）和仓库根目录。
    catalog_dir: str = ""
    agent_history_chars: int = 24_000   # 多轮对话时随请求带上的历史消息总字数上限（从最近往前取）

    # 交付件 Word 导出模板（.docx 文件路径，决策记录 2026-11-10）。留空用内置默认模板；部署后管理员把公司模板放到
    # 部署机上（如 /srv/templates/deliverable.docx，只读挂载进容器）并在 .env 里配置，不需要改代码。
    # 配置了但文件不存在时拒绝导出，不退回默认模板。
    export_template: str = ""
    # PPT 导出模板（二期 A9，app/project/export_pptx.py）：同上，不配用内置 16:9 空白模板，配了打不开拒绝导出
    export_template_pptx: str = ""

    # 运行指标端口（core/metrics.py，docs/监控.md）：容器里设 9100，只在 docker 内部网络；0 = 不开
    metrics_port: int = 0
    # ---- 备份（scripts/backup.sh 读同一个 .env 值）----
    # 备份保留天数：超过的备份自动删除；删除记录据此写明“备份中的副本最迟清除日”（完成日 + 天数）。
    backup_keep_days: int = 30

    # ---- 登录与会话 ----
    session_backend: str = "redis"          # redis；memory 只用于单元测试，生产环境拒绝启动
    session_idle_minutes: int = 30          # 无操作超时
    session_absolute_hours: int = 12        # 登录后最长有效期，到期必须重新登录
    cookie_secure: bool = True              # 只经 HTTPS 发送 Cookie；测试机走 HTTP 时在 .env 里关闭
    login_max_failures: int = 5             # 连续失败次数达到后锁定
    login_lock_minutes: int = 15
    password_min_length: int = 12
    password_max_age_days: int = 180        # 口令有效期（等保自查 ②d），到期登录后只能改口令；0 = 不限
    inactive_account_days: int = 90         # 超过这么多天没登录的账号由 worker 每天自动停用（系统管理员只标出），0 关闭

    # 第一个管理员：库里还没有任何用户时，启动时按这里创建一次。也可以用命令行创建：
    #   python -m app.cli create-user --username <名字>
    # 不设默认值；创建后应从 .env 删除口令。
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_totp_secret: str = ""

    # 测试账号：只在 APP_ENV=dev/test 时创建，供冒烟测试和测试助手使用；生产环境配置了就拒绝启动。
    test_account_username: str = ""
    test_account_password: str = ""
    test_account_totp_secret: str = ""

    # TOTP 密钥加密存储（core/secretbox.py）：编号:base64(32 字节)，逗号分隔，第一把加密、其余只解密；生产必须配置
    totp_keys: str = ""

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
