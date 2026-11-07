import hashlib
import json
from sqlalchemy.orm import Session

from ..models import AuditLog


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def record(db: Session, *, actor: str, action: str, result: str,
           project_code: str = "", **detail) -> None:
    actor = actor[:64]  # 操作人取自服务端会话中的用户名，不再信任客户端传来的值
    db.add(AuditLog(actor=actor, project_code=project_code, action=action, result=result,
                    detail=json.dumps(detail, ensure_ascii=False)))
    db.commit()
