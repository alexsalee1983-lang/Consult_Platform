"""项目里的 Agent 对话。要求业务角色 + 项目成员；对话只有发起人能看和继续。

POST /api/projects/{code}/conversations          以某个 Agent 新建对话
GET  /api/projects/{code}/conversations          我在这个项目里的对话
GET  /api/conversations/{id}                     对话和消息
POST /api/conversations/{id}/messages            发一条消息，SSE 流式返回（event: delta / done / error）

被机密级拦截的消息不存，只记审计（不含原文）；通过拦截的提问和 Agent 回复存入项目区。
"""
from collections.abc import Iterator
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..agents import runtime, tools
from ..core import audit
from ..core.auth import current_user
from ..core.db import SessionLocal, get_db, utcnow
from ..core.permissions import membership, project_for_member, require_business
from ..models import Agent, Conversation, Message, Project, User
from ..schemas import ChatIn
from ..services import gateway
from ..services.gateway import BlockedError, Completion, GatewayError
from .chat import UNAVAILABLE, _blocked, _failed, _sse, _succeeded

router = APIRouter(prefix="/api", tags=["conversations"], dependencies=[Depends(require_business)])

NOT_FOUND = "对话不存在"


class ConversationIn(BaseModel):
    agent_code: str = Field(max_length=32)


class MessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    status: str
    model: str
    prompt_version: str
    created_at: datetime


class ConversationOut(BaseModel):
    id: int
    project_code: str
    agent_code: str
    agent_name: str
    title: str
    prompt_version: str
    updated_at: datetime
    message_count: int = 0


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


def _out(db: Session, c: Conversation, project_code: str) -> ConversationOut:
    count = db.scalar(select(func.count(Message.id)).where(Message.conversation_id == c.id))
    return ConversationOut(id=c.id, project_code=project_code, agent_code=c.agent_code,
                           agent_name=db.get(Agent, c.agent_code).name, title=c.title,
                           prompt_version=c.prompt_version, updated_at=c.updated_at, message_count=count)


def _mine(db: Session, cid: int, user: User) -> tuple[Conversation, Project]:
    """只有发起人、且仍是项目成员才能访问；否则与不存在一样返回 404。"""
    c = db.get(Conversation, cid)
    if c is None or c.user_id != user.id or membership(db, c.project_id, user.id) is None:
        raise HTTPException(404, NOT_FOUND)
    return c, db.get(Project, c.project_id)


@router.post("/projects/{code}/conversations", response_model=ConversationOut, status_code=201)
def create(code: str, body: ConversationIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for_member(db, code, user)
    try:
        doc = runtime.ready_doc(db, body.agent_code)
    except runtime.AgentNotReady as e:
        raise HTTPException(409, str(e)) from e
    c = Conversation(project_id=project.id, user_id=user.id, agent_code=body.agent_code, title="",
                     prompt_version=doc.version)
    db.add(c)
    db.commit()
    audit.record(db, actor=user.username, action="conversation.create", result="allowed", project_code=project.code,
                 agent=body.agent_code, conversation_id=c.id, prompt_version=doc.version)
    return _out(db, c, project.code)


@router.get("/projects/{code}/conversations", response_model=list[ConversationOut])
def list_mine(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for_member(db, code, user)
    rows = db.scalars(select(Conversation).where(Conversation.project_id == project.id,
                                                 Conversation.user_id == user.id)
                      .order_by(Conversation.updated_at.desc()).limit(100))
    return [_out(db, c, project.code) for c in rows]


@router.get("/conversations/{cid}", response_model=ConversationDetail)
def get(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c, project = _mine(db, cid, user)
    base = _out(db, c, project.code)
    return ConversationDetail(**base.model_dump(), messages=[
        MessageOut(id=m.id, role=m.role, content=m.content, status=m.status, model=m.model,
                   prompt_version=m.prompt_version, created_at=m.created_at) for m in c.messages])


@router.post("/conversations/{cid}/messages")
def send(cid: int, body: MessageIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c, project = _mine(db, cid, user)
    try:
        doc = runtime.ready_doc(db, c.agent_code)
        card = runtime.project_card(db, user, c.agent_code, project.code)
    except runtime.AgentNotReady as e:
        raise HTTPException(409, str(e)) from e
    except tools.ToolError as e:
        raise HTTPException(e.status, str(e)) from e

    chat_in = ChatIn(project_code=project.code, message=body.message, level=doc.level)
    username, agent_code, conv_id, version = user.username, c.agent_code, c.id, doc.version
    extra = {"conversation_id": conv_id, "prompt_version": version}
    gen = gateway.stream(body.message, tier=project.tier, project_code=project.code, level=doc.level,
                         system=runtime.system_prompt(doc, card), history=runtime.history(list(c.messages)))
    try:
        first = next(gen)  # 拦截（422）和开始输出前的失败（502）以普通 HTTP 错误返回，被拦截的内容不存
    except BlockedError as e:
        _blocked(db, username, project.code, chat_in, e, stream=True, agent=agent_code, extra=extra)
    except GatewayError as e:
        _failed(db, username, project.code, chat_in, e, stream=True, agent=agent_code, extra=extra)
        raise HTTPException(502, UNAVAILABLE) from e

    db.add(Message(conversation_id=conv_id, role="user", content=body.message, prompt_version=version))
    if not c.title:
        c.title = body.message.strip().splitlines()[0][:40]
    c.updated_at = utcnow()
    db.commit()
    code = project.code

    def events() -> Iterator[str]:
        parts: list[str] = []
        item = first
        try:
            while True:
                if isinstance(item, Completion):
                    with SessionLocal() as s:
                        s.add(Message(conversation_id=conv_id, role="assistant", content=item.text, model=item.model,
                                      prompt_version=version, prompt_tokens=item.prompt_tokens,
                                      completion_tokens=item.completion_tokens))
                        s.get(Conversation, conv_id).updated_at = utcnow()
                        s.commit()
                        _succeeded(s, username, code, chat_in, item, stream=True, agent=agent_code, extra=extra)
                    yield _sse("done", {"model": item.model, "demo": item.demo, "prompt_version": version,
                                        "prompt_tokens": item.prompt_tokens,
                                        "completion_tokens": item.completion_tokens})
                    return
                parts.append(item)
                yield _sse("delta", {"text": item})
                item = next(gen)
        except GatewayError as e:  # 输出途中中断：已输出部分存为不完整回复，下次对话不带上
            with SessionLocal() as s:
                s.add(Message(conversation_id=conv_id, role="assistant", content="".join(parts),
                              status="interrupted", prompt_version=version))
                s.commit()
                _failed(s, username, code, chat_in, e, stream=True, agent=agent_code, extra=extra)
            yield _sse("error", {"detail": "模型输出中断，请重试"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
