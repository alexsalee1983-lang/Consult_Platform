import httpx
import redis
from fastapi import APIRouter
from sqlalchemy import text

from ..core.config import get_settings
from ..core.db import engine
from ..services.gateway import availability

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


def _minio() -> str:
    s = get_settings()
    if not s.minio_endpoint:
        return "not enabled (M1)"
    scheme = "https" if s.minio_secure else "http"
    try:
        resp = httpx.get(f"{scheme}://{s.minio_endpoint}/minio/health/live", timeout=2)
        return "ok" if resp.status_code == 200 else f"down: HTTP {resp.status_code}"
    except Exception as e:  # noqa: BLE001
        return f"down: {type(e).__name__}"


@router.get("/api/health")
def health() -> dict:
    checks = {
        "database": _db(),
        "redis": _redis(),
        "object_storage": _minio(),
        "llm": availability(),
    }
    return {"status": "ok" if checks["database"] == "ok" else "degraded", "checks": checks}
