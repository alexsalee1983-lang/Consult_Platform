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
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model_reasoning: str = "deepseek-r1"
    llm_model_standard: str = "qwen2.5-72b-instruct"
    llm_model_light: str = "qwen2.5-14b-instruct"
    llm_timeout_seconds: int = 120

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
