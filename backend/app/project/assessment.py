"""成熟度与符合性视图（二期 A10）：驾驶舱按能力域给出成熟度初判、符合性条和风险矩阵。

不建表，读取时现算（同 B4 kb_impact），删除项目、撤回同意清掉运行产出和交付件后自然消失：
- 差距分析表、成熟度初判表取最近一次交付链里依据与差距分析 Agent 的产出（运行记录 output）；
  表格格式见 agents/roles/compliance-basis.md 输出规范（差距分析表带能力域、影响、可能性三列）。
- D4 已签署时，分数以 D4 里的“成熟度初判”表为准：顾问在 D4 里直接改分数，不另发确认卡（负责人 2026-11-16）。
- 产品配置 baseline.maturity_scores: false（数据安全）只给符合性条和风险矩阵，不给分数。
- 风险矩阵只放不符合、部分符合、证据不足且写了影响和可能性的差距。

分数只是“成熟度初判”，界面和报告不写成测评、认证或成熟度等级结论（资质边界）。只读，不写审计。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentRun, AgentRunStep, Project
from ..products import config
from . import configs, deliverables

GAP_AGENT = "compliance-basis"
SCORE_DELIVERABLE = "D4"
SCORE_LABELS = {"ISO": "符合性初判"}          # 分数视图的叫法，其余产品称“成熟度初判”
LEVELS = ("高", "中", "低")
RISKY = ("unmet", "partial", "insufficient")
# 先匹配长的：“部分符合”“不符合”里都含“符合”
CONCLUSIONS = (("部分符合", "partial"), ("不符合", "unmet"), ("不适用", "na"), ("证据不足", "insufficient"),
               ("符合", "met"))
_SEP = re.compile(r"^:?-{2,}:?$")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Markdown 表格：（表头，数据行）。"""
    out: list[tuple[list[str], list[list[str]]]] = []
    block: list[list[str]] = []
    for line in [*text.splitlines(), ""]:
        s = line.strip()
        if s.startswith("|"):
            block.append([c.strip().replace("**", "") for c in s.strip("|").split("|")])
            continue
        if len(block) >= 2 and all(_SEP.match(c) for c in block[1] if c):
            out.append((block[0], block[2:]))
        block = []
    return out


def _col(header: list[str], test: Callable[[str], bool]) -> int | None:
    return next((i for i, h in enumerate(header) if test(h)), None)


def _cell(row: list[str], i: int | None, limit: int = 120) -> str:
    return row[i][:limit] if i is not None and i < len(row) else ""


def _conclusion(v: str) -> str:
    return next((k for word, k in CONCLUSIONS if word in v), "")


def _score(v: str) -> float | None:
    m = _NUM.search(v)
    x = float(m.group()) if m else None
    return x if x is not None and 1 <= x <= 5 else None


def parse(text: str) -> dict[str, list[dict[str, Any]]]:
    """从正文里取差距分析表（表头有 能力域 和 符合性初判）和成熟度初判表（能力域、现状…、目标…）。"""
    findings: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    for header, rows in _tables(text):
        domain = _col(header, lambda h: "能力域" in h)
        if domain is None:
            continue
        concl = _col(header, lambda h: "符合性初判" in h)
        if concl is not None:
            code = _col(header, lambda h: h.endswith("编号") and "证据" not in h)
            item = _col(header, lambda h: "要求" in h)
            impact = _col(header, lambda h: h == "影响")
            likely = _col(header, lambda h: h == "可能性")
            for r in rows:
                c = _conclusion(_cell(r, concl))
                if not c:
                    continue
                findings.append({"code": _cell(r, code, 16), "domain": _cell(r, domain, 40) or "未分域",
                                 "item": _cell(r, item), "conclusion": c,
                                 "impact": next((x for x in LEVELS if x == _cell(r, impact)), ""),
                                 "likelihood": next((x for x in LEVELS if x == _cell(r, likely)), "")})
            continue
        cur = _col(header, lambda h: h.startswith("现状"))
        tgt = _col(header, lambda h: h.startswith("目标"))
        if cur is None or tgt is None:
            continue
        basis = _col(header, lambda h: "依据" in h)
        for r in rows:
            a, b = _score(_cell(r, cur)), _score(_cell(r, tgt))
            if a is not None and b is not None and _cell(r, domain):
                scores.append({"domain": _cell(r, domain, 40), "current": a, "target": b,
                               "basis": _cell(r, basis, 80)})
    return {"findings": findings, "scores": scores}


def _gap_output(db: Session, project: Project) -> str:
    return db.scalar(select(AgentRunStep.output).join(AgentRun, AgentRun.id == AgentRunStep.run_id)
                     .where(AgentRun.project_id == project.id, AgentRunStep.agent_code == GAP_AGENT,
                            AgentRunStep.status == "done", AgentRunStep.output != "")
                     .order_by(AgentRunStep.id.desc()).limit(1)) or ""


def _signed_scores(db: Session, project: Project) -> list[dict[str, Any]] | None:
    d = deliverables.get_deliverable(db, project, SCORE_DELIVERABLE)
    if d is None or d.status != "signed":
        return None
    body = "\n\n".join(v.body for s in d.sections if (v := deliverables.get_version(s)) is not None)
    return parse(body)["scores"] or None


def _snap(db: Session, project: Project) -> dict[str, Any]:
    try:
        return configs.snapshot(db, project)
    except config.CatalogError:
        return {}


def view(db: Session, project: Project) -> dict[str, Any]:
    gap = parse(_gap_output(db, project))
    snap = _snap(db, project)
    enabled = bool(snap.get("maturity_scores", True))
    scores: list[dict[str, Any]] = []
    scores_source = ""
    if enabled:
        signed = _signed_scores(db, project)
        if signed is not None:
            scores, scores_source = signed, SCORE_DELIVERABLE
        elif gap["scores"]:
            scores, scores_source = gap["scores"], "gap"
    domains: dict[str, dict[str, int]] = {}
    for f in gap["findings"]:
        counts = domains.setdefault(f["domain"], {"met": 0, "partial": 0, "unmet": 0, "insufficient": 0, "na": 0})
        counts[f["conclusion"]] += 1
    return {
        "scores_enabled": enabled,
        "scores_label": SCORE_LABELS.get(snap.get("product", ""), "成熟度初判"),
        "source": "gap" if gap["findings"] else "",
        "scores_source": scores_source,
        "scores": scores,
        "domains": [{"domain": k, "counts": v} for k, v in domains.items()],
        "findings": [f for f in gap["findings"] if f["conclusion"] in RISKY and f["impact"] and f["likelihood"]],
    }
