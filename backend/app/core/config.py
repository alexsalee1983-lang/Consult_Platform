from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"   # dev 开发 / test 测试机 / prod 生产
    database_url: str = "postgresql+psycopg://consult:consult@postgres:5432/consult"
    redis_url: str = "redis://redis:6379/0"

    # 对象存储 M1 启用；留空表示未启用
    minio_endpoint: str = ""
    minio_access_key: str = "consult"
    minio_secret_key: str = "change-me-minio"
    minio_bucket: str = "project-area"
    minio_secure: bool = False

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

    seed_demo_data: bool = True

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
