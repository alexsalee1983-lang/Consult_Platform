from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import chat, health, projects
from .core.config import get_settings
from .core.db import SessionLocal
from .core.migrate import upgrade_to_head
from .seed import seed


@asynccontextmanager
async def lifespan(app: FastAPI):
    upgrade_to_head()
    if get_settings().seed_demo_data:
        with SessionLocal() as db:
            seed(db)
    yield


app = FastAPI(title="安全咨询 Agent 平台", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(projects.router)
app.include_router(chat.router)
