import redis
from fastapi import APIRouter
from sqlalchemy import text

from ..core.config import get_settings
from ..core.db import engine
from ..services import storage
from ..services.gateway import availability, uses_external_model

router = APIRouter(tags=["health"])


def _db() -> str:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"down: {type(e).__name__}"


def _redis() -> str:
    try:
        r = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1)
        r.ping()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"down: {type(e).__name__}"


@router.get("/api/health")
def health() -> dict:
    checks = {
        "database": _db(),
        "redis": _redis(),
        "object_storage": storage.health(),   # 两个区的桶都在且开启了服务端加密才是 ok
        "llm": availability(),
    }
    # llm_external：界面据此提示“当前为外部测试模型，勿输入真实客户信息”
    return {"status": "ok" if checks["database"] == "ok" else "degraded", "checks": checks,
            "llm_external": uses_external_model()}
