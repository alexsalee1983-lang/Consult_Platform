from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from .api import admin, agents, audit, auth, chat, conversations, health, projects
from .core.auth import authenticate
from .core.bootstrap import check_settings, ensure_initial_users, ensure_test_memberships
from .core.config import get_settings
from .core.db import SessionLocal
from .core.migrate import upgrade_to_head
from .seed import seed

_settings = get_settings()
check_settings(_settings)  # 配置不安全时直接拒绝启动


@asynccontextmanager
async def lifespan(app: FastAPI):
    upgrade_to_head()
    with SessionLocal() as db:
        ensure_initial_users(db, _settings)
        if _settings.seed_demo_data:
            seed(db)
        ensure_test_memberships(db, _settings)
    yield


# 默认所有接口都要登录（authenticate 内有公开接口白名单），各路由再按角色和项目成员检查；
# 生产环境不开放接口文档
app = FastAPI(
    title="安全咨询 Agent 平台", version="0.1.0", lifespan=lifespan,
    dependencies=[Depends(authenticate)],
    docs_url=None if _settings.is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if _settings.is_prod else "/openapi.json",
)
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(chat.router)
app.include_router(audit.router)
app.include_router(admin.router)
app.include_router(agents.router)
app.include_router(conversations.router)
