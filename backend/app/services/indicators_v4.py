"""v4.0 验收口径（四期 F2③，总体规划第七节验收第 2、3、7 条）：一处核对，复用已有计算，不建表、不调模型、不写审计。

- 经验萃取跳过率：口径同 app/project/extraction_trigger.stats（跳过 /（已答 + 跳过），累计），高于 30% 标红。
- 四格（SOC、IRE × 银行、电力）分列：
  - 编造条款号：窗内签署交付件按项目归格（产品取立项配置，没有配置按服务名开头；行业取项目行业），
    对比评测的编造条号不分格，只在总数里（indicators.compute 的 fabricated_clauses）。
  - 自动发布率：窗内发布的知识版本，条目产品是该格产品或 GEN、行业含该格行业或“通用”（或未填）就算进该格；
    通用条目会同时算进多格，所以四格的发布数相加可以大于总数。
- 四格关键章节就绪度：就绪度看板的装配评测集按 product（和选填的 industry，不填则该产品两格都算）归格，取章节就绪度平均；
  目标读 KB_READINESS_TARGETS（如 "SOC·银行=0.7,IRE·电力=0.6"，格名也可写 SOC-银行），没配的格显示“未校准”。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import KbItem, KbVersion, Project, ProjectConfig
from . import indicators

PRODUCTS = ("SOC", "IRE")
INDUSTRIES = ("银行", "电力")
GRIDS = tuple(f"{p}·{i}" for p in PRODUCTS for i in INDUSTRIES)
OTHER = "其他"
GENERAL_PRODUCT, GENERAL_INDUSTRY = "GEN", "通用"
SKIP_THRESHOLD = 0.3


def grid_rows() -> list[dict[str, str]]:
    return [{"key": g, "product": g.split("·")[0], "industry": g.split("·")[1]} for g in GRIDS]


def _product_of(db: Session, project: Project) -> str:
    cfg = db.scalar(select(ProjectConfig.product).where(ProjectConfig.project_id == project.id))
    if cfg:
        return str(cfg).upper()
    head = (project.service or "").strip().split(" ")[0].upper()
    return head if head in PRODUCTS else ""


def grid_of(db: Session, project: Project) -> str:
    key = f"{_product_of(db, project)}·{(project.industry or '').strip()}"
    return key if key in GRIDS else OTHER


def item_applies(product: str, industries: list[str] | None, grid: str) -> bool:
    gp, gi = grid.split("·")
    inds = list(industries or [])
    return product in (gp, GENERAL_PRODUCT) and (not inds or gi in inds or GENERAL_INDUSTRY in inds)


def by_grid(db: Session, start: datetime, end: datetime, project_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """四格各一行：签署数、编造条款号（交付件）、发布数、自动发布数、自动发布率。"""
    rows = {g["key"]: {**g, "signed": 0, "fabricated_clauses": 0, "published": 0, "auto": 0} for g in grid_rows()}
    projects: dict[int, str] = {}
    for s in indicators.signed(db, start, end, project_ids):
        if s.project_id not in projects:
            p = db.get(Project, s.project_id)
            projects[s.project_id] = grid_of(db, p) if p else OTHER
        g = projects[s.project_id]
        if g in rows:
            rows[g]["signed"] += 1
            rows[g]["fabricated_clauses"] += s.fabricated
    for item_product, inds, auto_review in db.execute(
            select(KbItem.product, KbItem.industries, KbVersion.auto_review).join(KbItem, KbItem.id == KbVersion.item_id)
            .where(KbVersion.published_at >= start, KbVersion.published_at < end)).all():
        for g, r in rows.items():
            if item_applies(item_product, inds, g):
                r["published"] += 1
                r["auto"] += auto_review in indicators.AUTO_KINDS
    for r in rows.values():
        r["auto_publish_rate"] = round(r["auto"] / r["published"], 4) if r["published"] else None
    return list(rows.values())


def skip_status(counts: dict) -> dict[str, Any]:
    done = int(counts.get("answered", 0)) + int(counts.get("skipped", 0))
    value = round(int(counts.get("skipped", 0)) / done, 4) if done else None
    return {"label": "经验萃取跳过率", "value": value, "threshold": SKIP_THRESHOLD,
            "answered": int(counts.get("answered", 0)), "skipped": int(counts.get("skipped", 0)),
            "status": "na" if value is None else ("breach" if value > SKIP_THRESHOLD else "ok"),
            "note": "" if done else "还没有已答或跳过的萃取题"}


def targets() -> dict[str, float]:
    out: dict[str, float] = {}
    for part in (get_settings().kb_readiness_targets or "").split(","):
        key, _, val = part.strip().partition("=")
        prod, _, ind = key.strip().replace("-", "·").partition("·")
        key = f"{prod.strip().upper()}·{ind.strip()}"
        try:
            num = float(val)
        except ValueError:
            continue
        if key in GRIDS and 0 <= num <= 1:
            out[key] = num
    return out


def readiness_grids(evalsets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """evalsets 取 readiness.matrix()["evalsets"]（每份带 product、industry、chapters[].readiness）。"""
    goal = targets()
    out = []
    for g in grid_rows():
        chs = [c["readiness"] for es in evalsets
               if str(es.get("product", "")).upper() == g["product"] and es.get("industry", "") in ("", g["industry"])
               for c in es["chapters"]]
        value = round(sum(chs) / len(chs), 4) if chs else None
        target = goal.get(g["key"])
        if value is None:
            status, label = "na", "没有评测集"
        elif target is None:
            status, label = "uncalibrated", "未校准"
        else:
            status, label = ("ok", "达标") if value >= target else ("below", "未达标")
        out.append({**g, "chapters": len(chs), "readiness": value, "target": target,
                    "gap": round(value - target, 4) if value is not None and target is not None else None,
                    "status": status, "label": label})
    return out


def summary(db: Session, start: datetime, end: datetime, project_ids: list[int] | None = None) -> dict[str, Any]:
    from ..kb import readiness
    from ..project import extraction_trigger

    return {"extraction_skip": skip_status(extraction_trigger.stats(db)),
            "grids": by_grid(db, start, end, project_ids),
            "readiness_grids": readiness_grids(readiness.matrix(db)["evalsets"])}


def render(rep: dict[str, Any]) -> str:
    """CLI indicators --v4 的一页汇总（只有数字）。"""
    def pct(v: float | None) -> str:
        return "—" if v is None else f"{v * 100:.1f}%"
    s = rep["extraction_skip"]
    lines = [f"经验萃取跳过率：{pct(s['value'])}（已答 {s['answered']}、跳过 {s['skipped']}，阈值 30%）"
             + ("  ← 超阈值" if s["status"] == "breach" else ""),
             "", "四格            签署  编造条款号  发布  自动  自动发布率"]
    for r in rep["grids"]:
        lines.append(f"{r['key']:<10}  {r['signed']:>5}  {r['fabricated_clauses']:>9}  {r['published']:>4}  "
                     f"{r['auto']:>4}  {pct(r['auto_publish_rate']):>8}")
    lines += ["", "四格            章节  就绪度   目标     差距     状态"]
    for r in rep["readiness_grids"]:
        gap = "—" if r["gap"] is None else f"{r['gap'] * 100:+.1f}%"
        lines.append(f"{r['key']:<10}  {r['chapters']:>4}  {pct(r['readiness']):>7}  {pct(r['target']):>7}  {gap:>7}  "
                     f"{r['label']}")
    return "\n".join(lines)
