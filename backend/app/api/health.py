import redis
from fastapi import APIRouter
from sqlalchemy import Engine, text

from ..core.config import get_settings
from ..core.db import KB_SPLIT, engine, kb_engine
from ..services import storage
from ..services.gateway import availability, embed_availability, rerank_availability, uses_external_model

router = APIRouter(tags=["health"])


def _db(eng: Engine) -> str:
    try:
        with eng.connect() as conn:
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


def _worker() -> str:
    try:
        from ..worker import online_workers

        n = online_workers()
    except Exception as e:  # noqa: BLE001
        return f"down: {type(e).__name__}"
    return "ok" if n else "down: 没有在线的 worker"


@router.get("/api/health")
def health() -> dict:
    checks = {
        "database": _db(engine),
        "redis": _redis(),
        "object_storage": storage.health(),   # 两个区的桶都在且开启了服务端加密才是 ok
        "worker": _worker(),                    # 后台任务（解析、向量化、删除），看 worker 心跳
        "llm": availability(),
        "embedding": embed_availability(),       # 向量服务（知识检索的向量部分）；未启用时只用全文检索
        "rerank": rerank_availability(),         # 重排服务；未启用或不可用时检索只用 RRF 排序
    }
    if KB_SPLIT:   # 知识区单独的库（M5 ③）
        checks["kb_database"] = _db(kb_engine)
    db_ok = checks["database"] == "ok" and checks.get("kb_database", "ok") == "ok"
    # llm_external：界面据此提示“当前为外部测试模型，勿输入真实客户信息”
    return {"status": "ok" if db_ok else "degraded", "checks": checks,
            "llm_external": uses_external_model()}
