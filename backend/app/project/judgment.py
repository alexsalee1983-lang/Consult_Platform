"""判断数据埋点（4A 架构设计 v4.0 §7.2，决策记录 2026-11-18）。

顾问对 Agent 产出的判断是改进 Agent、沉淀公司经验的原料，这里只记计数和类别：
- 改稿幅度：顾问改了 Agent 写的章节（上一版本由 Agent 写、这一版本由顾问写），幅度 = 1 − 文本相似度，分 light / moderate / heavy；
- 确认卡：卡片类别、选项数、是否选了选项、是否按推荐、是否写了意见。
不记正文、标题、选项文字和意见；JUDGMENT_CAPTURE 默认关（客户项目上的内部信息等法务意见），关闭时什么也不写。
表 judgment_events（迁移 0038），项目删除时一并删除（app/project/deletion.py）。
"""
from __future__ import annotations

import difflib

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import Confirmation, Deliverable, Finding, JudgmentEvent, Project, SectionVersion

EDIT_BUCKETS = {"light": 0.1, "moderate": 0.4, "heavy": 1.01}   # 幅度上限（不含）
MAX_COMPARE = 20000                                           # 比较的字数上限，防止超长正文拖慢写入


def enabled() -> bool:
    return bool(get_settings().judgment_capture)


def magnitude(before: str, after: str) -> float:
    ratio = difflib.SequenceMatcher(None, before[:MAX_COMPARE], after[:MAX_COMPARE], autojunk=False).ratio()
    return round(1 - ratio, 3)


def bucket(m: float) -> str:
    return next(k for k, top in EDIT_BUCKETS.items() if m < top)


def capture_edit(db: Session, project: Project, d: Deliverable, seq: int, prev: SectionVersion | None,
                 new: SectionVersion) -> None:
    """顾问改 Agent 稿时记一次（调用方提交）。"""
    if not enabled() or prev is None or not prev.agent_code or new.agent_code:
        return
    m = magnitude(prev.body, new.body)
    db.add(JudgmentEvent(project_id=project.id, kind="edit", deliverable_code=d.code, section_seq=seq,
                         agent_code=prev.agent_code, category=bucket(m), magnitude=m,
                         attrs={"from_version": prev.version, "to_version": new.version,
                                "chars_before": prev.char_count, "chars_after": new.char_count}))


def capture_card(db: Session, c: Confirmation, commented: bool) -> None:
    """顾问处理确认卡时记一次（调用方提交）。"""
    if not enabled():
        return
    chose = bool(c.choice)
    db.add(JudgmentEvent(project_id=c.project_id, kind="card", deliverable_code=c.deliverable_code or "",
                         section_seq=c.section_seq, agent_code=c.agent_code or "", category=(c.kind or "")[:32],
                         attrs={"options": len(c.options or []), "chose": chose,
                                "followed": chose and bool(c.recommendation) and c.choice == c.recommendation,
                                "commented": commented}))


def capture_finding(db: Session, project: Project, f: Finding) -> None:
    """顾问确认发现时记一条训练三元组（三期 F1）：特征 + Agent 初判 + 顾问结论；不记现状、风险、建议等文字。"""
    if not enabled() or f.source != "agent" or not f.agent_conclusion:
        return
    db.add(JudgmentEvent(project_id=project.id, kind="finding", deliverable_code="", section_seq=None,
                         agent_code=f.agent_code[:32], category=f.conclusion[:32],
                         attrs={"control": f.control_code[:32], "industry": (project.industry or "")[:16],
                                "agent": f.agent_conclusion, "human": f.conclusion,
                                "agreed": f.agent_conclusion == f.conclusion, "evidence": len(f.evidence_refs or []),
                                "responsibility": f.responsibility, "priority": f.priority,
                                "impact": f.impact, "likelihood": f.likelihood}))


TRIPLE_FEATURES = ("control", "industry", "evidence", "responsibility", "priority", "impact", "likelihood")


def triples(db: Session) -> list[dict]:
    """训练三元组导出（四期训练本地抽取模型用）：（特征，Agent 初判，顾问结论）。"""
    out: list[dict] = []
    for e in db.scalars(select(JudgmentEvent).where(JudgmentEvent.kind == "finding").order_by(JudgmentEvent.id)):
        a = e.attrs or {}
        out.append({"features": {**{k: a.get(k) for k in TRIPLE_FEATURES}, "agent_code": e.agent_code},
                    "agent": a.get("agent"), "human": a.get("human")})
    return out


def delete_for_project(db: Session, project_id: int) -> int:
    return db.execute(delete(JudgmentEvent).where(JudgmentEvent.project_id == project_id)).rowcount or 0
