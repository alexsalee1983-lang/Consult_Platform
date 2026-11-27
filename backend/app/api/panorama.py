"""项目全景（四期 A25①）：GET /api/projects/{code}/panorama 按产品配置生成的泳道全景，只读。

业务角色可用；非项目成员 404。生成规则见 app/project/panorama.py。
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..products import config
from ..project import panorama

router = APIRouter(prefix="/api", tags=["panorama"], dependencies=[Depends(require_business)])


@router.get("/projects/{code}/panorama")
def get_panorama(code: str, runs: bool = Query(False), db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> dict[str, Any]:
    """runs=1：页面右上角“查看 Agent 运行情况”打开时多出“Agent 运行”一行（四期 A29，成员只读）。"""
    p = project_for_member(db, code, user)
    try:
        return panorama.build(db, p, user, runs=runs)
    except config.CatalogError as e:
        raise HTTPException(409, "项目配置有问题，暂时生成不了全景，请联系管理员") from e
