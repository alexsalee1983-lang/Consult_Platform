"""知识批次接口（三期 B18①，app/kb/batches.py）。只限知识管理员，其他人 403。

GET  /api/kb/batches           最近的批次：页面发起的（jobs 表的状态 + 审计里的计数）和命令行跑的（审计 kb.batch）
GET  /api/kb/batches/sources   资料目录（KB_BATCH_DIR）下可选的导入目录和评测题目文件
POST /api/kb/batches           发起一个批次：{kind: import | eval, path: 相对路径, submit, compare}，只排后台任务，返回 202
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business
from ..kb import batches
from ..models import User

router = APIRouter(prefix="/api/kb", tags=["kb"], dependencies=[Depends(require_business)])


class BatchOut(BaseModel):
    kind: str
    name: str
    status: str
    source: str
    created_by: str
    created_at: datetime
    finished_at: datetime | None
    job_id: int | None
    path: str
    counts: dict
    error: str


class SourcesOut(BaseModel):
    dirs: list[str]
    questions: list[str]


class StartIn(BaseModel):
    kind: str = Field(max_length=16)
    path: str = Field(max_length=200)
    submit: bool = False
    compare: bool = False


class StartOut(BaseModel):
    job_id: int


def _require_admin(db: Session, user: User) -> None:
    if not batches.is_admin(db, user):
        raise HTTPException(403, "只有知识管理员能使用知识批次")


@router.get("/batches", response_model=list[BatchOut])
def list_batches(db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_admin(db, user)
    return [BatchOut.model_validate(b, from_attributes=True) for b in batches.list_batches(db)]


@router.get("/batches/sources", response_model=SourcesOut)
def batch_sources(db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_admin(db, user)
    s = batches.sources()
    return SourcesOut(dirs=s["dirs"], questions=s["questions"])


@router.post("/batches", response_model=StartOut, status_code=202)
def start_batch(body: StartIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_admin(db, user)
    try:
        job_id = batches.start(db, user, body.kind, body.path, submit=body.submit, compare=body.compare)
    except batches.BatchError as e:
        raise HTTPException(e.status, e.detail) from e
    return StartOut(job_id=job_id)
