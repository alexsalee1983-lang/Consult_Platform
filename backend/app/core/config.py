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
    llm_connect_timeout_seconds: float = 5        # 建立连接超时
    llm_timeout_seconds: int = 120                # 等待模型回复（非流式整体 / 流式两段输出之间）的超时
    llm_max_retries: int = 2                      # 连接失败、超时、429、5xx 时重试次数（流式只在开始输出前重试）
    llm_retry_backoff_seconds: float = 1.0        # 指数退避基数：1s、2s、4s……
    llm_health_ttl_seconds: int = 30              # 模型可用性检查结果缓存时间
    # 自动假名化：把项目卡中登记的客户名、人名、系统名替换成占位符再发给模型，回复里再换回来。
    # 模型为公司私有化部署，默认关闭；以后接入云端模型时，标准档必须开启（决策记录 2026-11-07）。
    llm_pseudonymize: bool = False
    # 外部模型（公司外部的云端服务，如 DeepSeek 官方 API）。决策记录 2026-11-08：只允许在开发和测试环境、
    # 只处理虚拟客户数据时使用。开启后：严格档项目一律拒绝调用；自动假名化强制开启；审计标记 external；
    # 界面提示“勿输入真实客户信息”；生产环境（APP_ENV=prod）开启则拒绝启动。
    llm_external: bool = False

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

    # 重排服务（/rerank，交叉编码模型，决策记录 2026-11-11），同样只经网关调用。留空表示不启用：检索只用 RRF 排序，
    # 依据查询退回上面的余弦相似度门槛。docker compose 默认指向 rerank 服务（tools/rerank）。
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3-int8"   # int8 量化版（决策记录 11-11 补充）
    # 外部重排服务（如硅基流动 Qwen3-Reranker，决策记录 2026-11-14）：与 LLM_EXTERNAL 同样只允许开发和测试环境、只处理
    # 虚拟客户数据；查询和知识片段原样发出（片段已去标识，查询先过机密级检测），不做假名化。生产环境开启即拒绝启动。
    rerank_external: bool = False
    # 送去重排的候选片段数（RRF 融合后的前 N 个）和每个片段的字数上限。CPU 上重排耗时大致与“个数 × 字数”成正比。
    kb_rerank_candidates: int = 20
    kb_rerank_max_chars: int = 400
    # 依据查询门槛（重排分数，0～1，sigmoid 后）：启用重排时取代 KB_BASIS_MIN_SIMILARITY。初值按开发电脑上的
    # 示范评测：正例最低 0.14（B004），该查不到的题最高 0.001，取 0.05。正式评测题编好后重新校准（交接文档 S2 第 5 步）。
    kb_basis_min_rerank: float = 0.05
    # 普通检索的重排分数下限：只挡掉趋近 0 分的无关结果（测试 #34：该查不到的 S011 得分 0.0003～0.0008）。
    # 比依据查询宽得多，宁可多给几条弱相关的；正式评测后与 KB_BASIS_MIN_RERANK 一起校准。
    kb_search_min_rerank: float = 0.005

    seed_demo_data: bool = True

    # Agent md 目录（01_公共规范.md、roles/*.md）。留空时依次找 /srv/agents（镜像内）和仓库根目录的 agents/。
    # 测试机可把仓库的 agents/ 只读挂载到容器里，md 改了不用重建镜像，运行时会自动重新加载。
    agents_dir: str = ""
    agent_history_chars: int = 24_000   # 多轮对话时随请求带上的历史消息总字数上限（从最近往前取）

    # 交付件 Word 导出模板（.docx 文件路径，决策记录 2026-11-10）。留空用内置默认模板；部署后管理员把公司模板放到
    # 部署机上（如 /srv/templates/deliverable.docx，只读挂载进容器）并在 .env 里配置，不需要改代码。
    # 配置了但文件不存在时拒绝导出，不退回默认模板。
    export_template: str = ""

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
    password_max_age_days: int = 90         # 口令有效期（等保自查 ②d），到期登录后只能改口令；0 = 不限
    inactive_account_days: int = 90         # 超过这么多天没登录的账号在用户页标出，供管理员停用（不自动停用）

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
