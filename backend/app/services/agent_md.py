"""Agent md 版本库与回滚指针（四期 F3②e；负责人 2026-11-24 同意，决策记录同日）。

- 版本库：平台每次经 effective() 取用 md 时，把（公共规范 + 角色 md）原文按内容哈希存一份，同一哈希只存一次。
  部署机只部署 main（测试机按 PR 分支部署的是测试库，不进生产），所以生产库里只有已合入的版本。
- 回滚指针：有指针时运行时按指针取版本库里的原文组装 md；只能指向版本库里已有的版本。撤销即删指针，恢复仓库当前版本。
- 审计只记 Agent 代号和版本哈希，不记 md 原文。
"""
from __future__ import annotations

import threading

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..agents import registry
from ..core import audit
from ..models import Agent, AgentMdPin, AgentMdVersion

_seen: set[tuple[str, str]] = set()
_lock = threading.Lock()


class MdError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _capture(db: Session, doc: registry.AgentDoc) -> None:
    key = (doc.code, doc.version)
    with _lock:
        if key in _seen:
            return
    exists = db.scalar(select(AgentMdVersion.id).where(AgentMdVersion.agent_code == doc.code,
                                                       AgentMdVersion.version == doc.version))
    if exists is None:
        try:
            with db.begin_nested():
                db.add(AgentMdVersion(agent_code=doc.code, version=doc.version, md_version=doc.md_version[:32],
                                      common_text=doc.common, role_text=doc.role_text))
            db.commit()
        except IntegrityError:          # 并发取用同一版本：另一方已存
            db.rollback()
    with _lock:
        _seen.add(key)


def effective(db: Session, code: str) -> registry.AgentDoc | None:
    """运行时取用的 md：有回滚指针按指针，否则仓库当前版本；顺带把仓库当前版本存进版本库。"""
    doc = registry.load(code)
    if doc is not None:
        _capture(db, doc)
    pin = db.get(AgentMdPin, code)
    if pin is None:
        return doc
    row = _version(db, code, pin.version)
    return registry.parse(code, row.common_text, row.role_text) if row else doc


def _version(db: Session, code: str, version: str) -> AgentMdVersion | None:
    return db.scalar(select(AgentMdVersion).where(AgentMdVersion.agent_code == code,
                                                  AgentMdVersion.version == version))


def history(db: Session, code: str) -> dict:
    if db.get(Agent, code) is None:
        raise MdError(404, "Agent 不存在")
    current = registry.load(code)
    if current is not None:
        _capture(db, current)
    pin = db.get(AgentMdPin, code)
    rows = db.scalars(select(AgentMdVersion).where(AgentMdVersion.agent_code == code)
                      .order_by(AgentMdVersion.captured_at.desc(), AgentMdVersion.id.desc())).all()
    return {"agent": code, "current": current.version if current else "", "pinned": pin.version if pin else "",
            "pinned_incident": pin.incident_id if pin else None,
            "items": [{"version": r.version, "md_version": r.md_version, "captured_at": r.captured_at.isoformat()}
                      for r in rows]}


def content(db: Session, code: str, version: str) -> dict:
    row = _version(db, code, version)
    if row is None:
        raise MdError(404, "版本库里没有该版本")
    return {"agent": code, "version": row.version, "md_version": row.md_version,
            "common_text": row.common_text, "role_text": row.role_text}


def pin(db: Session, actor: str, code: str, version: str, incident_id: int | None) -> None:
    """切到版本库里的已合入版本。不提交，由调用方与处置记录一起提交后再审计。"""
    if not code or db.get(Agent, code) is None:
        raise MdError(422, "要回滚的 Agent 不存在")
    if _version(db, code, version) is None:
        raise MdError(422, "只能回滚到版本库里已合入的版本")
    current = registry.load(code)
    if current is not None:
        _capture(db, current)
    old = db.get(AgentMdPin, code)
    in_use = old.version if old else (current.version if current else "")
    if version == in_use:
        raise MdError(422, "已是当前在用的版本")
    if old is None:
        db.add(AgentMdPin(agent_code=code, version=version, incident_id=incident_id, pinned_by=actor))
    else:
        old.version, old.incident_id, old.pinned_by = version, incident_id, actor
    db.flush()
    audit.record(db, actor=actor, action="agent.md_rollback", result="ok", agent=code, from_version=in_use,
                 to_version=version, incident=incident_id)


def unpin(db: Session, actor: str, code: str, reason: str) -> dict:
    p = db.get(AgentMdPin, code)
    if p is None:
        raise MdError(404, "该 Agent 没有回滚")
    pinned = p.version
    db.delete(p)
    db.commit()
    current = registry.load(code)
    audit.record(db, actor=actor, action="agent.md_unpin", result="ok", agent=code, from_version=pinned,
                 to_version=current.version if current else "", reason_len=len(reason))
    return {"agent": code, "current": current.version if current else ""}
