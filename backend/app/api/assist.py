"""写稿助手 v1（三期 A15）。要求业务角色 + 项目成员（非成员 404）；规则见 app/project/assist.py。

POST /api/projects/{code}/deliverables/{dcode}/sections/{seq}/assist    侧栏问 / 改，SSE 流式返回（event: delta / done / error）

停止：前端断开连接即可，服务端随之关闭模型流，不写回任何内容。
被机密级拦截 422（不调模型）；严格档用外部模型 403；开始输出前模型不可用 502。助手只给文字，不写交付件。
"""
from collections.abc import Iterator
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..agents import call, runtime, tools
from ..core import audit
from ..core.auth import current_user
from ..core.db import SessionLocal, get_db
from ..core.permissions import project_for_member, require_business
from ..models import Project, User
from ..project import assist
from ..project import deliverables as svc
from ..services.gateway import BlockedError, Completion, ExternalModelForbidden, GatewayError
from .chat import UNAVAILABLE, _guard_out, _sse

router = APIRouter(prefix="/api/projects", tags=["assist"], dependencies=[Depends(require_business)])


class AssistIn(BaseModel):
    mode: Literal["ask", "edit"] = "ask"
    prompt: str = Field(min_length=1, max_length=2_000)
    speed: Literal["fast", "deep"] = "fast"
    context: str | None = Field(default=None, max_length=200_000)   # 编辑中的正文；不传取当前版本
    selection: str = Field(default="", max_length=20_000)


@router.post("/{code}/deliverables/{dcode}/sections/{seq}/assist")
def assist_section(code: str, dcode: str, seq: int, body: AssistIn, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    project = project_for_member(db, code, user)
    try:
        d = svc.require_deliverable(db, project, dcode)
        s = svc.require_section(d, seq)
    except svc.DeliverableError as e:
        raise HTTPException(e.status, e.detail) from e
    try:
        p = assist.prepare(db, user, project, d, s, mode=body.mode, prompt=body.prompt, context=body.context,
                           selection=body.selection, speed=body.speed)
    except runtime.AgentNotReady as e:
        raise HTTPException(409, str(e)) from e
    except tools.ToolError as e:
        raise HTTPException(e.status, e.detail) from e

    username, pcode = user.username, project.code
    meta = {"deliverable": d.code, "section": s.seq, "mode": body.mode, "speed": body.speed, "level": p.level,
            "agent": assist.AGENT, "prompt_version": p.version, "prompt_chars": len(body.prompt),
            "prompt_sha": audit.digest(body.prompt), "context_chars": len(body.context or ""),
            "selection_chars": len(body.selection), "kb_refs": len(p.refs)}
    gen = call.stream(db, assist.AGENT, p.message, actor=username, system=p.system, project=project,
                      level=p.level, level_override=True)
    try:
        first = next(gen)   # 拦截和开始输出前的失败以普通 HTTP 错误返回
    except ExternalModelForbidden as e:
        audit.record(db, actor=username, action="deliverable.assist", result="blocked", project_code=pcode,
                     reason="external_model_strict", **meta)
        raise HTTPException(403, str(e)) from e
    except BlockedError as e:
        audit.record(db, actor=username, action="deliverable.assist", result="blocked", project_code=pcode,
                     kinds=sorted({f.kind for f in e.result.findings}), **meta)
        raise HTTPException(422, detail=_guard_out(e.result).model_dump()) from e
    except GatewayError as e:
        audit.record(db, actor=username, action="deliverable.assist", result="error", project_code=pcode,
                     error=str(e), **meta)
        raise HTTPException(502, UNAVAILABLE) from e
    project_id = project.id

    def events() -> Iterator[str]:
        item = first
        try:
            while True:
                if isinstance(item, Completion):
                    with SessionLocal() as db2:
                        sources = svc.parse_sources(db2, db2.get(Project, project_id), item.text)
                        audit.record(db2, actor=username, action="deliverable.assist", result="allowed",
                                     project_code=pcode, model=item.model, demo=item.demo,
                                     reply_chars=len(item.text), reply_sha=audit.digest(item.text),
                                     sources=sources["counts"], unresolved=len(sources["unresolved"]), **meta)
                    yield _sse("done", {"mode": body.mode, "speed": body.speed, "level": p.level, "model": item.model,
                                        "demo": item.demo, "prompt_version": p.version, "text": item.text,
                                        "sources": sources, "references": p.refs})
                    return
                yield _sse("delta", {"text": item})
                item = next(gen)
        except GatewayError as e:
            with SessionLocal() as db2:
                audit.record(db2, actor=username, action="deliverable.assist", result="error", project_code=pcode,
                             error=str(e), **meta)
            yield _sse("error", {"detail": "模型输出中断，请重试"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
