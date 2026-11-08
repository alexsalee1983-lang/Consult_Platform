"""顾问对话与机密级检查。要求业务角色；对话只能在自己是成员的项目里进行。

每次对话：机密级拦截 → 模型网关 → 写审计（不记原文）→ 写用量（不记内容）。
/api/chat 一次返回；/api/chat/stream 以 Server-Sent Events 逐段返回（event: delta / done / error）。
"""
import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..core import audit
from ..core.auth import current_user
from ..core.db import SessionLocal, get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..schemas import ChatIn, ChatOut, FindingOut, GuardIn, GuardOut
from ..services import gateway, usage
from ..services.gateway import BlockedError, Completion, GatewayError
from ..services.guard import check

router = APIRouter(prefix="/api", tags=["gateway"], dependencies=[Depends(require_business)])

UNAVAILABLE = "模型服务暂时不可用，请稍后再试"


def _guard_out(result) -> GuardOut:
    return GuardOut(blocked=result.blocked, summary=result.summary(),
                    findings=[FindingOut(kind=f.kind, label=f.label, excerpt=f.excerpt) for f in result.findings])


def _meta(body: ChatIn) -> dict:
    return {"length": len(body.message), "sha": audit.digest(body.message), "level": body.level}


def _blocked(db: Session, username: str, project_code: str, body: ChatIn, e: BlockedError, stream: bool):
    audit.record(db, actor=username, action="llm.chat", result="blocked", project_code=project_code,
                 kinds=sorted({f.kind for f in e.result.findings}), stream=stream, **_meta(body))
    usage.record(db, username=username, project_code=project_code, level=body.level,
                 model=gateway.model_for(body.level), status="blocked", stream=stream)
    raise HTTPException(422, detail=_guard_out(e.result).model_dump()) from e


def _failed(db: Session, username: str, project_code: str, body: ChatIn, e: GatewayError, stream: bool) -> None:
    audit.record(db, actor=username, action="llm.chat", result="error", project_code=project_code,
                 error=str(e), attempts=e.attempts, stream=stream, **_meta(body))
    usage.record(db, username=username, project_code=project_code, level=body.level,
                 model=gateway.model_for(body.level), status="error", stream=stream, attempts=e.attempts)


def _succeeded(db: Session, username: str, project_code: str, body: ChatIn, out: Completion, stream: bool) -> None:
    audit.record(db, actor=username, action="llm.chat", result="allowed", project_code=project_code,
                 model=out.model, demo=out.demo, stream=stream, attempts=out.attempts,
                 pseudonymized=out.pseudonymized, **_meta(body))
    usage.record(db, username=username, project_code=project_code, level=body.level, model=out.model,
                 status="ok", stream=stream, completion=out)


@router.post("/gateway/check", response_model=GuardOut)
def gateway_check(body: GuardIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    result = check(body.text)
    audit.record(db, actor=user.username, action="guard.check", result="blocked" if result.blocked else "allowed",
                 length=len(body.text), sha=audit.digest(body.text),
                 kinds=sorted({f.kind for f in result.findings}))
    return _guard_out(result)


@router.post("/chat", response_model=ChatOut)
def chat(body: ChatIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for_member(db, body.project_code, user)
    username, code = user.username, project.code
    try:
        out = gateway.complete(body.message, tier=project.tier, project_code=code, level=body.level)
    except BlockedError as e:
        _blocked(db, username, code, body, e, stream=False)
    except GatewayError as e:
        _failed(db, username, code, body, e, stream=False)
        raise HTTPException(502, UNAVAILABLE) from e
    _succeeded(db, username, code, body, out, stream=False)
    return ChatOut(reply=out.text, model=out.model, demo=out.demo)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat/stream")
def chat_stream(body: ChatIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for_member(db, body.project_code, user)
    username, code = user.username, project.code
    gen = gateway.stream(body.message, tier=project.tier, project_code=code, level=body.level)
    # 先取第一段：机密级拦截（422）和开始输出前的连接失败（502）在这里以普通 HTTP 错误返回
    try:
        first = next(gen)
    except BlockedError as e:
        _blocked(db, username, code, body, e, stream=True)
    except GatewayError as e:
        _failed(db, username, code, body, e, stream=True)
        raise HTTPException(502, UNAVAILABLE) from e

    def events() -> Iterator[str]:
        # 请求级的数据库会话在开始输出前就会关闭，这里单独开一个，只用于最后写审计和用量
        item = first
        try:
            while True:
                if isinstance(item, Completion):
                    with SessionLocal() as s:
                        _succeeded(s, username, code, body, item, stream=True)
                    yield _sse("done", {"model": item.model, "demo": item.demo,
                                        "prompt_tokens": item.prompt_tokens,
                                        "completion_tokens": item.completion_tokens})
                    return
                yield _sse("delta", {"text": item})
                item = next(gen)
        except GatewayError as e:  # 输出途中中断：已输出的内容保留在页面上，提示用户
            with SessionLocal() as s:
                _failed(s, username, code, body, e, stream=True)
            yield _sse("error", {"detail": "模型输出中断，请重试"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
