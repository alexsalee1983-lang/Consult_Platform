"""项目里的 Agent 对话。要求业务角色 + 项目成员；对话只有发起人能看和继续。

POST /api/projects/{code}/conversations          以某个 Agent 新建对话
GET  /api/projects/{code}/conversations          我在这个项目里的对话
GET  /api/conversations/{id}                     对话和消息
POST /api/conversations/{id}/messages            发一条消息，SSE 流式返回（event: delta / done / error）
POST /api/conversations/{id}/handoff             按某条回复里的交接信息转交专业 Agent（二期 C2，app/agents/handoff.py）

被机密级拦截的消息不存，只记审计（不含原文）；通过拦截的提问和 Agent 回复存入项目区。
"""
from collections.abc import Iterator
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..agents import chat_tools, handoff, knowledge, roster, runtime, tools
from ..core import audit
from ..core.auth import current_user
from ..core.config import get_settings
from ..core.db import SessionLocal, get_db, utcnow
from ..core.permissions import membership, project_for_member, require_business
from ..models import Agent, Conversation, Message, Project, User
from ..schemas import ChatIn
from ..services import gateway
from ..services.gateway import BlockedError, Completion, ExternalModelForbidden, GatewayError, ToolUse
from .chat import UNAVAILABLE, _blocked, _external_forbidden, _failed, _sse, _succeeded

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
    references: list[dict] = []     # 本条回复附给 Agent 的知识库条目（doc_id、标题、章节、引用格式）
    handoffs: list[dict] = []       # 本条回复里可转交的目标（agent_code、agent_name），只有有转交授权的 Agent 才有


class ConversationOut(BaseModel):
    id: int
    project_code: str
    agent_code: str
    agent_name: str
    title: str
    prompt_version: str
    updated_at: datetime
    message_count: int = 0
    handoff_from: int | None = None   # 转交来源对话


class HandoffIn(BaseModel):
    message_id: int
    agent_code: str = Field(max_length=32)


class HandoffOut(BaseModel):
    conversation: ConversationOut
    draft: str                        # 交接说明草稿，顾问可修改后发送


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


def _out(db: Session, c: Conversation, project_code: str) -> ConversationOut:
    count = db.scalar(select(func.count(Message.id)).where(Message.conversation_id == c.id))
    return ConversationOut(id=c.id, project_code=project_code, agent_code=c.agent_code,
                           agent_name=db.get(Agent, c.agent_code).name, title=c.title,
                           prompt_version=c.prompt_version, updated_at=c.updated_at, message_count=count,
                           handoff_from=c.handoff_from)


def _mine(db: Session, cid: int, user: User) -> tuple[Conversation, Project]:
    """只有发起人、且仍是项目成员才能访问；否则与不存在一样返回 404。"""
    c = db.get(Conversation, cid)
    if c is None or c.user_id != user.id or membership(db, c.project_id, user.id) is None:
        raise HTTPException(404, NOT_FOUND)
    return c, db.get(Project, c.project_id)


@router.post("/projects/{code}/conversations", response_model=ConversationOut, status_code=201)
def create(code: str, body: ConversationIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for_member(db, code, user)
    agent_code = roster.resolve(db, body.agent_code)      # C6-P：旧代号已停用时新对话记新代号
    try:
        doc = runtime.ready_doc(db, agent_code)
    except runtime.AgentNotReady as e:
        raise HTTPException(409, str(e)) from e
    c = Conversation(project_id=project.id, user_id=user.id, agent_code=agent_code, title="",
                     prompt_version=doc.version)
    db.add(c)
    db.commit()
    audit.record(db, actor=user.username, action="conversation.create", result="allowed", project_code=project.code,
                 agent=agent_code, conversation_id=c.id, prompt_version=doc.version)
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
    can_handoff = HANDOFF_TOOL in tools.granted_tools(db, c.agent_code)
    return ConversationDetail(**base.model_dump(), messages=[
        MessageOut(id=m.id, role=m.role, content=m.content, status=m.status, model=m.model,
                   prompt_version=m.prompt_version, created_at=m.created_at, references=m.kb_refs or [],
                   handoffs=_handoffs(db, c, m) if can_handoff else [])
        for m in c.messages])


HANDOFF_TOOL = "agent.handoff"


def _handoffs(db: Session, c: Conversation, m: Message) -> list[dict]:
    if m.role != "assistant" or m.status != "ok":
        return []
    return [{"agent_code": h.agent_code, "agent_name": h.agent_name} for h in handoff.parse(db, m.content, c.agent_code)]


@router.post("/conversations/{cid}/handoff", response_model=HandoffOut, status_code=201)
def hand_off(cid: int, body: HandoffIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """顾问点“转交”：找到这条回复里给该 Agent 的交接信息，经工具层 agent.handoff 新建对话（授权、成员检查、审计）。"""
    c, project = _mine(db, cid, user)
    m = db.get(Message, body.message_id)
    if m is None or m.conversation_id != c.id or m.role != "assistant" or m.status != "ok":
        raise HTTPException(404, "消息不存在")
    picked = next((h for h in handoff.parse(db, m.content, c.agent_code) if h.agent_code == body.agent_code), None)
    if picked is None:
        raise HTTPException(422, "这条回复里没有转交给该 Agent 的交接信息")
    try:
        r = tools.invoke(db, user, c.agent_code, HANDOFF_TOOL,
                         {"from_conversation": c.id, "target": picked.agent_code, "brief": picked.brief}, project.code)
    except tools.ToolError as e:
        raise HTTPException(e.status, e.detail) from e
    return HandoffOut(conversation=_out(db, db.get(Conversation, r["conversation_id"]), project.code), draft=r["draft"])


@router.post("/conversations/{cid}/messages")
def send(cid: int, body: MessageIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c, project = _mine(db, cid, user)
    try:
        doc = runtime.ready_doc(db, c.agent_code)
        card = runtime.project_card(db, user, c.agent_code, project.code)
    except runtime.AgentNotReady as e:
        raise HTTPException(409, str(e)) from e
    except tools.ToolError as e:
        raise HTTPException(e.status, e.detail) from e

    chat_in = ChatIn(project_code=project.code, message=body.message, level=doc.level)
    username, agent_code, conv_id, version = user.username, c.agent_code, c.id, doc.version
    refs: list[dict] = []
    used: list[str] = []
    offered = chat_tools.available(db, agent_code) if get_settings().llm_tool_calling else []
    if offered:
        # 二期 A7：Agent 自己决定调用哪个工具（function calling），每次调用经工具层（授权、硬规则、审计）
        system = runtime.system_prompt(doc, card, tool_mode=True)
        tool_args = {"tools": chat_tools.schemas(offered),
                     "run_tool": chat_tools.runner(user.id, agent_code, project.code, refs, used, conv_id)}
    else:
        # 提问前先检索知识库，命中的条目附进系统提示（检索经工具层，按授权和可见范围，写审计）
        kb_block, refs = knowledge.retrieve(db, user, agent_code, project, body.message)
        system, tool_args = runtime.system_prompt(doc, card, kb_block), {}
    extra = {"conversation_id": conv_id, "prompt_version": version, "kb_refs": len(refs)}
    gen = gateway.stream(body.message, tier=project.tier, project_code=project.code, level=doc.level,
                         system=system, history=runtime.history(list(c.messages)), **tool_args)
    try:
        first = next(gen)  # 拦截（422）和开始输出前的失败（502）以普通 HTTP 错误返回，被拦截的内容不存
    except ExternalModelForbidden as e:
        _external_forbidden(db, username, project.code, chat_in, e, stream=True, agent=agent_code, extra=extra)
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
                if isinstance(item, ToolUse):
                    tcode = chat_tools.BY_FUNC.get(item.name, "")
                    yield _sse("tool", {"tool": tcode, "name": chat_tools.DISPLAY.get(tcode, item.name)})
                    item = next(gen)
                    continue
                if isinstance(item, Completion):
                    extra["kb_refs"], extra["tool_calls"] = len(refs), item.tool_calls
                    with SessionLocal() as s:
                        reply = Message(conversation_id=conv_id, role="assistant", content=item.text, model=item.model,
                                        prompt_version=version, prompt_tokens=item.prompt_tokens,
                                        completion_tokens=item.completion_tokens, kb_refs=refs)
                        s.add(reply)
                        s.get(Conversation, conv_id).updated_at = utcnow()
                        s.commit()
                        reply_id = reply.id
                        _succeeded(s, username, code, chat_in, item, stream=True, agent=agent_code, extra=extra)
                    yield _sse("done", {"model": item.model, "demo": item.demo, "prompt_version": version,
                                        "prompt_tokens": item.prompt_tokens,
                                        "completion_tokens": item.completion_tokens, "references": refs,
                                        "tool_calls": item.tool_calls, "message_id": reply_id})
                    return
                parts.append(item)
                yield _sse("delta", {"text": item})
                item = next(gen)
        except GatewayError as e:  # 输出途中中断：已输出部分存为不完整回复，下次对话不带上
            with SessionLocal() as s:
                s.add(Message(conversation_id=conv_id, role="assistant", content="".join(parts),
                              status="interrupted", prompt_version=version, kb_refs=refs))
                s.commit()
                _failed(s, username, code, chat_in, e, stream=True, agent=agent_code, extra=extra)
            yield _sse("error", {"detail": "模型输出中断，请重试"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
