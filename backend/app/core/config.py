from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"   # dev 开发 / test 测试机 / prod 生产
    database_url: str = "postgresql+psycopg://consult:consult@postgres:5432/consult"
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

    seed_demo_data: bool = True

    # Agent md 目录（01_公共规范.md、roles/*.md）。留空时依次找 /srv/agents（镜像内）和仓库根目录的 agents/。
    # 测试机可把仓库的 agents/ 只读挂载到容器里，md 改了不用重建镜像，运行时会自动重新加载。
    agents_dir: str = ""
    agent_history_chars: int = 24_000   # 多轮对话时随请求带上的历史消息总字数上限（从最近往前取）

    # ---- 登录与会话 ----
    session_backend: str = "redis"          # redis；memory 只用于单元测试，生产环境拒绝启动
    session_idle_minutes: int = 30          # 无操作超时
    session_absolute_hours: int = 12        # 登录后最长有效期，到期必须重新登录
    cookie_secure: bool = True              # 只经 HTTPS 发送 Cookie；测试机走 HTTP 时在 .env 里关闭
    login_max_failures: int = 5             # 连续失败次数达到后锁定
    login_lock_minutes: int = 15
    password_min_length: int = 12

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

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
