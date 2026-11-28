"""项目定制项（四期 A30）。要求业务角色；只有项目成员能看能操作，非成员和别的项目的定制项一律 404。

GET  /api/projects/{code}/customizations                 定制项清单（含优先级裁决结果）与各交付件“需更新”的章
POST /api/projects/{code}/customizations                 对话入口（source=chat，拆条发确认卡）或编辑器入口（source=editor，一次性）
POST /api/projects/{code}/customizations/{cid}/withdraw  撤回（生效后撤回按上游变更处理）
D1 客户要求栏经启动包表单提交（forms.submit）；确认与项目经理审批经确认卡 /api/confirmations/{id}/resolve。
规则见 app/project/customizations.py。
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..project import customizations as svc
from ..project import deliverables as dsvc

router = APIRouter(prefix="/api/projects", tags=["customizations"], dependencies=[Depends(require_business)])


class CustomizationIn(BaseModel):
    source: Literal["chat", "editor"]
    text: str = Field(min_length=1, max_length=svc.MAX_TEXT * svc.MAX_ITEMS)
    deliverable: str = Field(default="", max_length=8)
    section: int | None = None


def _raise(e: svc.CustomizationError):
    raise HTTPException(e.status, e.detail) from e


@router.get("/{code}/customizations")
def list_customizations(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    items = svc.list_items(db, p)
    lost = svc.resolve_priority(items)
    needs = {d.code: seqs for d in p.deliverables if (seqs := svc.needs_update(db, p, d))}
    return {"items": [svc.out(i, lost) for i in items], "needs_update": needs, "layers": list(svc.LAYERS)}


@router.post("/{code}/customizations", status_code=201)
def create_customization(code: str, body: CustomizationIn, db: Session = Depends(get_db),
                         user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    try:
        if body.source == "chat":
            items = svc.from_chat(db, user, p, body.text)
        else:
            d = dsvc.get_deliverable(db, p, body.deliverable.strip().upper())
            if d is None or body.section is None:
                raise HTTPException(422, "编辑器入口须指明交付件和章")
            items = [svc.from_editor(db, user, p, d, body.section, body.text)]
    except svc.CustomizationError as e:
        _raise(e)
    lost = svc.resolve_priority(svc.list_items(db, p))
    return {"items": [svc.out(i, lost) for i in items]}


@router.post("/{code}/customizations/{cid}/withdraw")
def withdraw_customization(code: str, cid: int, db: Session = Depends(get_db),
                           user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    item = svc.get(db, p, cid)
    if item is None:
        raise HTTPException(404, "定制项不存在")
    try:
        item = svc.withdraw(db, user, p, item)
    except svc.CustomizationError as e:
        _raise(e)
    return svc.out(item, svc.resolve_priority(svc.list_items(db, p)))
