from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    database_url: str = "postgresql+psycopg://consult:consult@postgres:5432/consult"
    redis_url: str = "redis://redis:6379/0"

    minio_endpoint: str = "minio:9000"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
