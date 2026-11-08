import hashlib
import json
from urllib.parse import unquote

from sqlalchemy.orm import Session

from .models import AuditLog


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def record(db: Session, *, actor: str, action: str, result: str,
           project_code: str = "", **detail) -> None:
    actor = unquote(actor)[:64]  # 前端把中文用户名做了 URL 编码
    db.add(AuditLog(actor=actor, project_code=project_code, action=action, result=result,
                    detail=json.dumps(detail, ensure_ascii=False)))
    db.commit()
