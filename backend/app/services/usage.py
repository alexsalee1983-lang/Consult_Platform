"""大模型用量计量：每次经网关的调用写一条 llm_usage（不记内容），并提供按人、项目、模型的汇总。"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..core.db import utcnow
from ..models import LlmUsage
from .gateway import Completion


def record(db: Session, *, username: str, project_code: str, level: str, model: str, status: str,
           stream: bool, completion: Completion | None = None, attempts: int = 1, agent_code: str = "") -> None:
    c = completion
    db.add(LlmUsage(username=username[:64], project_code=project_code, level=level, agent_code=agent_code,
                    model=(c.model if c else model)[:64],
                    status=status, demo=bool(c and c.demo), stream=stream, pseudonymized=bool(c and c.pseudonymized),
                    prompt_tokens=c.prompt_tokens if c else 0, completion_tokens=c.completion_tokens if c else 0,
                    latency_ms=c.latency_ms if c else 0, attempts=c.attempts if c else attempts))
    db.commit()


def summary(db: Session, days: int, group_by: tuple[str, ...]) -> list[dict]:
    """按 group_by（username / project_code / model 的组合）汇总最近 days 天的用量。"""
    cols = [getattr(LlmUsage, g) for g in group_by]
    stmt = (select(*cols, func.count(LlmUsage.id), func.sum(LlmUsage.prompt_tokens),
                   func.sum(LlmUsage.completion_tokens),
                   func.sum(case((LlmUsage.status == "error", 1), else_=0)),
                   func.sum(case((LlmUsage.status == "blocked", 1), else_=0)))
            .where(LlmUsage.at >= utcnow() - timedelta(days=days))
            .group_by(*cols).order_by(*cols))
    out = []
    for row in db.execute(stmt):
        keys = dict(zip(group_by, row[:len(group_by)]))
        calls, prompt, completion, errors, blocked = row[len(group_by):]
        out.append({**keys, "calls": calls, "prompt_tokens": prompt or 0, "completion_tokens": completion or 0,
                    "errors": errors or 0, "blocked": blocked or 0})
    return out
