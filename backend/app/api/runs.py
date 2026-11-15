"""交付链运行（M3 ④ Agent 编排）。要求业务角色；只有项目成员能看能发起，非成员一律 404。

POST /api/projects/{code}/runs                 发起一次标准档交付链运行（3→4→7），202，后台执行
GET  /api/projects/{code}/runs                 本项目的运行列表（新的在前）
GET  /api/projects/{code}/runs/{rid}           某次运行：状态、每步记录、发过的确认卡、差距分析产出
POST /api/projects/{code}/runs/{rid}/cancel    取消未结束的运行

严格档 422；已有未结束的运行 409；Agent 未就绪 409。规则见 app/project/runs.py。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import AgentRun, User
from ..project import runs as svc

router = APIRouter(prefix="/api/projects", tags=["runs"], dependencies=[Depends(require_business)])


class StepOut(BaseModel):
    step: str
    name: str
    agent_code: str
    status: str
    output: str
    refs: dict
    prompt_version: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    started_at: datetime
    finished_at: datetime | None


class RunOut(BaseModel):
    id: int
    chain: str
    status: str
    next_step: str               # 下一步（或正在跑的一步）；结束后为 end
    waiting_cards: list[int]
    card_ids: list[int]
    error: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class RunDetail(RunOut):
    steps: list[StepOut]


def _out(run: AgentRun) -> dict:
    return {"id": run.id, "chain": run.chain, "status": run.status, "next_step": svc._step_key(run),
            "waiting_cards": run.waiting_cards or [], "card_ids": run.card_ids or [], "error": run.error,
            "created_by": run.created_by, "created_at": run.created_at, "updated_at": run.updated_at,
            "finished_at": run.finished_at}


def _detail(run: AgentRun) -> RunDetail:
    names = svc.step_names()
    steps = [StepOut(step=s.step, name=names.get(s.step, s.step), agent_code=s.agent_code, status=s.status,
                     output=s.output, refs=s.refs or {}, prompt_version=s.prompt_version, model=s.model,
                     prompt_tokens=s.prompt_tokens, completion_tokens=s.completion_tokens, started_at=s.started_at,
                     finished_at=s.finished_at) for s in run.steps]
    return RunDetail(**_out(run), steps=steps)


def _run(db: Session, code: str, rid: int, user: User) -> tuple:
    project = project_for_member(db, code, user)
    run = svc.get_run(db, project, rid)
    if run is None:
        raise HTTPException(404, "运行记录不存在")
    return project, run


@router.post("/{code}/runs", response_model=RunOut, status_code=202)
def start(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for_member(db, code, user)
    try:
        run = svc.start(db, user, project)
    except svc.RunError as e:
        raise HTTPException(e.status, str(e)) from e
    return _out(run)


@router.get("/{code}/runs", response_model=list[RunOut])
def list_runs(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for_member(db, code, user)
    return [_out(r) for r in svc.list_runs(db, project)]


@router.get("/{code}/runs/{rid}", response_model=RunDetail)
def get_run(code: str, rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, run = _run(db, code, rid, user)
    return _detail(run)


@router.post("/{code}/runs/{rid}/cancel", response_model=RunOut)
def cancel(code: str, rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project, run = _run(db, code, rid, user)
    try:
        run = svc.cancel(db, user, project, run)
    except svc.RunError as e:
        raise HTTPException(e.status, str(e)) from e
    return _out(run)
