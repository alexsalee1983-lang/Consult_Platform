"""四期 A27：阶段自动推进、阶段包、本周简报（规则见 app/project/stages.py）。

GET  /api/projects/{code}/stages                 各阶段的交付件与退出条件、当前阶段、按规则应到的阶段
GET  /api/projects/{code}/stages/{phase}/pack    阶段包 zip（汇报 PPT 草稿 + 各交付件 Word + 清单）；只出启动、分析、验收
GET  /api/projects/{code}/weekly-brief           本周简报（自动完成 / 人工处理 / 待办，只有计数和编号）

只给项目成员（非成员 404）。
"""
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..project import stages as svc

router = APIRouter(prefix="/api/projects", tags=["stages"], dependencies=[Depends(require_business)])


@router.get("/{code}/stages")
def stage_overview(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return svc.overview(db, project_for_member(db, code, user))


@router.get("/{code}/stages/{phase}/pack")
def stage_pack(code: str, phase: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    try:
        r = svc.stage_pack(db, user, p, phase)
    except svc.StageError as e:
        raise HTTPException(e.status, e.detail) from e
    return Response(r.data, media_type=svc.ZIP_MIME, headers={
        "Content-Disposition": f"attachment; filename=\"{p.code}-pack.zip\"; filename*=UTF-8''{quote(r.filename)}",
        "Cache-Control": "no-store"})


@router.get("/{code}/weekly-brief")
def weekly_brief(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return svc.weekly_brief(db, project_for_member(db, code, user))
