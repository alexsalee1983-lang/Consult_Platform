from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .db import SessionLocal, init_db
from .routers import chat, health, projects
from .seed import seed


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if get_settings().seed_demo_data:
        with SessionLocal() as db:
            seed(db)
    yield


app = FastAPI(title="安全咨询 Agent 平台", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(projects.router)
app.include_router(chat.router)
