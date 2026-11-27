"""发现表规则（三期 F1，迁移 0039；字段说明见 app/models/finding.py）。

- 依据与差距分析 Agent（compliance-basis）每跑完一次，把差距分析表同步成发现（sync_from_gap，交付链调用、调用方提交）：
  按控制项编号（没有就按要求要点）对上已有发现；只覆盖仍是 draft 且来自 Agent 的发现，顾问确认过的不动；
  这次输出里没有了的 Agent 初判删掉；新发现接着已有最大 F 编号往下编，F 编号一经分配不变。
- 状态只能按 draft → confirmed → signed → remediating → verified 往前走一步（advance）。
  交付链差距分析后平台发“发现表确认”卡，选 A 时草稿发现一并确认（confirm_drafts，四期 D4）。
  draft → confirmed 时，若 JUDGMENT_CAPTURE 打开，记一条训练三元组（特征、Agent 初判、顾问结论），不记文字。
- 只有 draft、confirmed 可改（update）；取值固定的字段校验取值，文字字段先过机密级检测。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Finding, Project
from ..services import guard
from . import assessment, judgment

STATUSES = ("draft", "confirmed", "signed", "remediating", "verified")
CONCLUSIONS = ("met", "partial", "unmet", "insufficient", "na")
RESPONSIBILITIES = ("", "customer", "inherited", "shared")
PRIORITIES = ("", "高", "中", "低")
EDITABLE = ("draft", "confirmed")
TEXT_FIELDS = {"item": 200, "current": 500, "basis_clause": 120, "basis_version": 32, "risk": 500, "owner_dept": 64,
               "period": 32, "acceptance": 500, "recommendation": 500, "control_code": 64}
CHOICE_FIELDS: dict[str, tuple[str, ...]] = {
    "conclusion": CONCLUSIONS, "responsibility": RESPONSIBILITIES, "priority": PRIORITIES,
    "impact": ("", *assessment.LEVELS), "likelihood": ("", *assessment.LEVELS)}
SCORE_FIELDS = ("current_score", "target_score")
EMPTY = ("—", "-", "无", "")


class FindingError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _key(control_code: str, item: str) -> str:
    return f"c:{control_code}" if control_code else f"i:{item}"


def _text(v: str, n: int) -> str:
    return "" if v in EMPTY else v[:n]


def parse_gap(text: str) -> list[dict[str, Any]]:
    """差距分析表 → 发现字段（表头见 agents/roles/compliance-basis.md 输出规范）。"""
    scores = {s["domain"]: s for s in assessment.parse(text)["scores"]}
    out: list[dict[str, Any]] = []
    for header, rows in assessment._tables(text):
        def col(test: Callable[[str], bool], h: list[str] = header) -> int | None:
            return assessment._col(h, test)

        concl = col(lambda h: "符合性初判" in h)
        if concl is None or col(lambda h: "能力域" in h) is None:
            continue
        cols = {"control_code": col(lambda h: h.endswith("编号") and "证据" not in h),
                "domain": col(lambda h: "能力域" in h), "item": col(lambda h: "要求" in h),
                "current": col(lambda h: h == "现状"), "evidence": col(lambda h: h.startswith("证据")),
                "risk": col(lambda h: h == "业务影响"), "impact": col(lambda h: h == "影响"),
                "likelihood": col(lambda h: h == "可能性"), "basis": col(lambda h: h.startswith("依据")),
                "recommendation": col(lambda h: "整改" in h), "priority": col(lambda h: "优先级" in h)}
        for r in rows:
            c = assessment._conclusion(assessment._cell(r, concl))
            if not c:
                continue
            cell = {k: assessment._cell(r, i, 500).strip() for k, i in cols.items()}
            domain = cell["domain"][:40] or "未分域"
            ev = cell["evidence"].replace("，", ",").replace("、", ",").split(",")
            sc = scores.get(domain, {})
            out.append({"control_code": _text(cell["control_code"], 64), "domain": domain,
                        "item": _text(cell["item"], 200), "current": _text(cell["current"], 500),
                        "evidence_refs": [x.strip()[:32] for x in ev if x.strip() not in EMPTY],
                        "basis_clause": _text(cell["basis"], 120), "conclusion": c, "agent_conclusion": c,
                        "risk": _text(cell["risk"], 500),
                        "impact": cell["impact"] if cell["impact"] in assessment.LEVELS else "",
                        "likelihood": cell["likelihood"] if cell["likelihood"] in assessment.LEVELS else "",
                        "priority": cell["priority"] if cell["priority"] in PRIORITIES else "",
                        "recommendation": _text(cell["recommendation"], 500),
                        "current_score": sc.get("current"), "target_score": sc.get("target")})
    return out


def _next_fid(existing: list[Finding]) -> int:
    nums = [int(f.fid[2:]) for f in existing if f.fid[2:].isdigit()]
    return max(nums, default=0) + 1


def sync_from_gap(db: Session, project: Project, text: str, agent_code: str = assessment.GAP_AGENT,
                  run_id: int | None = None) -> dict[str, int]:
    rows = parse_gap(text)
    existing = list(db.scalars(select(Finding).where(Finding.project_id == project.id).order_by(Finding.id)))
    by_key = {_key(f.control_code, f.item): f for f in existing}
    seen: set[str] = set()
    counts = {"added": 0, "updated": 0, "kept": 0, "removed": 0}
    nxt = _next_fid(existing)
    for row in rows:
        k = _key(row["control_code"], row["item"])
        if k in seen:
            continue
        seen.add(k)
        f = by_key.get(k)
        if f is None:
            db.add(Finding(project_id=project.id, fid=f"F-{nxt:02d}", source="agent", agent_code=agent_code[:32],
                           run_id=run_id, status="draft", **row))
            nxt += 1
            counts["added"] += 1
        elif f.status == "draft" and f.source == "agent":
            for name, value in row.items():
                setattr(f, name, value)
            f.agent_code, f.run_id = agent_code[:32], run_id
            counts["updated"] += 1
        else:
            counts["kept"] += 1
    for k, f in by_key.items():
        if k not in seen and f.status == "draft" and f.source == "agent":
            db.delete(f)
            counts["removed"] += 1
    db.flush()
    return counts


def list_for_project(db: Session, project: Project) -> list[Finding]:
    return list(db.scalars(select(Finding).where(Finding.project_id == project.id).order_by(Finding.fid)))


def update(db: Session, f: Finding, fields: dict[str, Any]) -> Finding:
    if f.status not in EDITABLE:
        raise FindingError(409, "发现已签署，不能再改；整改进展用状态推进")
    for name, value in fields.items():
        if name in TEXT_FIELDS:
            text = str(value or "").strip()
            if len(text) > TEXT_FIELDS[name]:
                raise FindingError(422, f"{name} 超过 {TEXT_FIELDS[name]} 字")
            if text and guard.check(text).blocked:
                raise FindingError(422, "内容命中机密级检测，没有保存")
            setattr(f, name, text)
        elif name in CHOICE_FIELDS:
            if value not in CHOICE_FIELDS[name]:
                raise FindingError(422, f"{name} 取值不对")
            setattr(f, name, value)
        elif name in SCORE_FIELDS:
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not 1 <= value <= 5):
                raise FindingError(422, f"{name} 应为 1～5")
            setattr(f, name, None if value is None else float(value))
        elif name == "evidence_refs":
            if not isinstance(value, list) or not all(isinstance(x, str) and len(x) <= 32 for x in value):
                raise FindingError(422, "evidence_refs 应为证据编号列表")
            f.evidence_refs = list(value)
        else:
            raise FindingError(422, f"{name} 不能修改")
    db.flush()
    return f


def advance(db: Session, project: Project, f: Finding, to: str) -> Finding:
    if to not in STATUSES or f.status not in STATUSES:
        raise FindingError(422, "状态取值不对")
    if STATUSES.index(to) != STATUSES.index(f.status) + 1:
        raise FindingError(409, f"不能从 {f.status} 直接到 {to}")
    if to == "confirmed":
        judgment.capture_finding(db, project, f)
    f.status = to
    db.flush()
    return f


def confirm_drafts(db: Session, project: Project) -> int:
    """发现表确认卡选 A（四期 D4，runs.on_card_resolved 调用、调用方提交）：本项目草稿发现逐条转为已确认，照常记三元组。"""
    drafts = [f for f in list_for_project(db, project) if f.status == "draft"]
    for f in drafts:
        advance(db, project, f, "confirmed")
    return len(drafts)


def delete_for_project(db: Session, project_id: int) -> int:
    return db.execute(delete(Finding).where(Finding.project_id == project_id)).rowcount or 0
