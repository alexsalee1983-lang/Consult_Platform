from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import audit
from ..db import get_db
from ..models import Confirmation, Project
from ..schemas import PHASES, ConfirmationOut, ProjectDetail, ProjectOut, ResolveIn

router = APIRouter(prefix="/api", tags=["projects"])


def _out(p: Project, cls=ProjectOut):
    data = cls.model_validate(p, from_attributes=True)
    data.phase_name = PHASES[p.phase] if 0 <= p.phase < len(PHASES) else ""
    data.open_confirmations = sum(1 for c in p.confirmations if c.status == "open")
    return data


def _conf_out(c: Confirmation) -> ConfirmationOut:
    o = ConfirmationOut.model_validate(c, from_attributes=True)
    o.project_code = c.project.code
    return o


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    rows = db.scalars(select(Project).options(selectinload(Project.confirmations)).order_by(Project.code)).all()
    return [_out(p) for p in rows]


@router.get("/projects/{code}", response_model=ProjectDetail)
def get_project(code: str, db: Session = Depends(get_db)):
    p = db.scalar(select(Project).where(Project.code == code)
                  .options(selectinload(Project.deliverables), selectinload(Project.confirmations)))
    if p is None:
        raise HTTPException(404, "项目不存在")
    detail = ProjectDetail.model_validate(p, from_attributes=True)
    detail.phase_name = PHASES[p.phase] if 0 <= p.phase < len(PHASES) else ""
    detail.open_confirmations = sum(1 for c in p.confirmations if c.status == "open")
    detail.confirmations = [_conf_out(c) for c in p.confirmations if c.status == "open"]
    return detail


@router.get("/confirmations", response_model=list[ConfirmationOut])
def list_confirmations(db: Session = Depends(get_db)):
    rows = db.scalars(select(Confirmation).where(Confirmation.status == "open")
                      .options(selectinload(Confirmation.project)).order_by(Confirmation.id)).all()
    return [_conf_out(c) for c in rows]


@router.post("/confirmations/{cid}/resolve", response_model=ConfirmationOut)
def resolve(cid: int, body: ResolveIn, db: Session = Depends(get_db),
            x_user: str = Header(default="dev", alias="X-User")):
    c = db.get(Confirmation, cid)
    if c is None:
        raise HTTPException(404, "待确认事项不存在")
    c.status = "resolved"
    c.resolution = body.resolution
    db.commit()
    audit.record(db, actor=x_user, action="confirmation.resolve", result="allowed",
                 project_code=c.project.code, confirmation_id=cid)
    return _conf_out(c)
