"""确认卡（M3 第 ③ 个 PR）。

Agent 遇到需要顾问判断的事（方案取舍、来源冲突、证据不足、范围确认、入库确认）不自己拍板，经工具 confirm.create
发一张确认卡：标题、选项、推荐和理由、影响，可关联到某份交付件的某一章。顾问在“待我确认”里选一个选项或写自己的意见；
处理后 Agent 再继续（暂停和继续由第 ④ 个 PR 的编排负责）。格式与 agents/roles/solution-designer.md “确认卡”一节一致。

规则：
1. 卡片所有文字过机密级检测，命中不保存。两档项目都可以发卡（严格档的卡同样只能用占位符，由 Agent 遵守公共规范）。
2. 同一个 Agent 在同一项目里重复发同一标题、仍未处理的卡，不新建，返回已有的卡（Agent 重试或重复输出时不刷屏）。
3. 处理：选项卡可以只选选项（choice），也可以选了再补充意见；没有选项或不选时必须写意见。已处理的卡不能再处理。
4. 审计只记项目、卡片编号、类型、Agent、处理方式，不记标题、选项和意见原文。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..models import Agent, Confirmation, Project, User
from ..services import guard
from . import deliverables

KINDS = ("方案取舍", "来源冲突", "证据不足", "范围确认", "入库确认")
MAX_OPTIONS = 6
MAX_OPTION_CHARS = 500
MAX_TEXT = 4000
MAX_TITLE = 256
MAX_RESOLUTION = 2000
_KEY_PREFIX = re.compile(r"^\s*([A-Fa-f])(?:\s*[.．、:：)）]\s*|\s+)")   # “A ”“B．”“C）”这类选项字母前缀


class ConfirmationError(ValueError):
    def __init__(self, status: int, detail):
        super().__init__(detail if isinstance(detail, str) else "被拒绝")
        self.status = status
        self.detail = detail


def _guard(db: Session, user: User, project: Project, action: str, text: str, **meta) -> None:
    result = guard.check(text)
    if result.blocked:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     reason="guard", categories=sorted({f.kind for f in result.findings}), **meta)
        raise ConfirmationError(422, {"blocked": True, "summary": f"{result.summary()}，没有保存",
                                      "findings": [{"kind": f.kind, "label": f.label, "excerpt": f.excerpt}
                                                   for f in result.findings[:10]]})


def normalize_options(raw) -> list[dict]:
    """选项可以写成 ["A 6 人白班", "B 扩编"]、["6 人白班", "扩编"] 或 [{"key": "A", "text": "…"}]，统一成 A、B、C…。"""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ConfirmationError(422, "options 应是选项列表")
    if len(raw) > MAX_OPTIONS:
        raise ConfirmationError(422, f"选项最多 {MAX_OPTIONS} 个")
    out = []
    for i, o in enumerate(raw):
        text = str(o.get("text", "")) if isinstance(o, dict) else str(o)
        m = _KEY_PREFIX.match(text)
        if m and not isinstance(o, dict):
            text = text[m.end():]
        text = text.strip()
        if not text or len(text) > MAX_OPTION_CHARS:
            raise ConfirmationError(422, f"选项不能为空，且不超过 {MAX_OPTION_CHARS} 字")
        out.append({"key": "ABCDEF"[i], "text": text})
    return out


def create(db: Session, user: User, project: Project, agent: Agent, args: dict) -> tuple[Confirmation, bool]:
    """发卡。返回 (卡片, 是否新建)；重复的未处理卡返回已有的卡。"""
    action = "confirmation.create"
    kind = str(args.get("kind", "")).strip()
    title = str(args.get("title", "")).strip()
    if kind not in KINDS:
        raise ConfirmationError(422, f"kind 只能是：{'、'.join(KINDS)}")
    if not title or len(title) > MAX_TITLE:
        raise ConfirmationError(422, f"标题不能为空，且不超过 {MAX_TITLE} 字")
    options = normalize_options(args.get("options"))
    rec = str(args.get("recommendation", "")).strip().upper()[:1]
    if rec and rec not in {o["key"] for o in options}:
        raise ConfirmationError(422, "recommendation 应是某个选项的字母（A、B……）")
    reason, impact = str(args.get("reason", "")).strip(), str(args.get("impact", "")).strip()
    if len(reason) > MAX_TEXT or len(impact) > MAX_TEXT:
        raise ConfirmationError(422, f"理由和影响各不超过 {MAX_TEXT} 字")
    dcode, seq = str(args.get("deliverable", "")).strip().upper(), args.get("section")
    if dcode:
        d = deliverables.get_deliverable(db, project, dcode)
        if d is None:
            raise ConfirmationError(422, "关联的交付件不存在（D1～D8）")
        if seq not in (None, ""):
            try:
                seq = int(seq)
            except (TypeError, ValueError) as e:
                raise ConfirmationError(422, "section 必须是整数") from e
            if deliverables.get_section(d, seq) is None:
                raise ConfirmationError(422, f"{dcode} 没有第 {seq} 章")
        else:
            seq = None
    else:
        seq = None
    _guard(db, user, project, action, "\n".join([title, reason, impact, *(o["text"] for o in options)]),
           agent=agent.code, kind=kind)

    existing = db.scalar(select(Confirmation).where(
        Confirmation.project_id == project.id, Confirmation.status == "open", Confirmation.title == title,
        Confirmation.agent_code == agent.code))
    if existing is not None:
        return existing, False
    c = Confirmation(project_id=project.id, kind=kind, title=title, agent=f"{agent.name} Agent", agent_code=agent.code,
                     options=options, recommendation=rec, reason=reason, impact=impact, deliverable_code=dcode,
                     section_seq=seq, created_by=user.username)
    db.add(c)
    db.flush()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 confirmation_id=c.id, kind=kind, agent=agent.code, options=len(options),
                 deliverable=dcode or None)
    db.commit()
    db.refresh(c)
    return c, True


def resolve(db: Session, user: User, c: Confirmation, choice: str = "", resolution: str = "") -> Confirmation:
    action = "confirmation.resolve"
    choice, resolution = (choice or "").strip().upper(), (resolution or "").strip()
    if c.status != "open":
        raise ConfirmationError(409, "这张确认卡已经处理过了")
    keys = {o.get("key") for o in (c.options or [])}
    if choice and choice not in keys:
        raise ConfirmationError(422, "所选选项不在这张卡的选项里")
    if not choice and not resolution:
        raise ConfirmationError(422, "请选一个选项，或写下你的意见")
    if len(resolution) > MAX_RESOLUTION:
        raise ConfirmationError(422, f"意见不超过 {MAX_RESOLUTION} 字")
    commented = bool(resolution)
    if commented:
        _guard(db, user, c.project, action, resolution, confirmation_id=c.id)
    else:
        resolution = next(f"选 {o['key']}：{o['text']}" for o in c.options if o.get("key") == choice)
    c.status, c.choice, c.resolution = "resolved", choice, resolution
    c.resolved_by, c.resolved_at = user.username, utcnow()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=c.project.code,
                 confirmation_id=c.id, choice=choice or None, with_comment=commented)
    db.commit()
    db.refresh(c)
    return c


def card_for_agent(c: Confirmation, created: bool | None = None) -> dict:
    out = {"id": c.id, "kind": c.kind, "title": c.title, "status": c.status, "options": c.options,
           "recommendation": c.recommendation, "deliverable": c.deliverable_code, "section": c.section_seq}
    if created is not None:
        out["created"] = created
        if not created:
            out["note"] = "已有一张相同标题、尚未处理的卡，没有重复发"
    if c.status == "resolved":
        out.update(choice=c.choice, resolution=c.resolution)
    return out
