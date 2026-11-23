"""知识地图按产品（二期 B6，设计稿 06）：覆盖矩阵、缺口 TOP、贡献统计。

- 覆盖矩阵（三期 B15 起按产品标签计数，一个条目计入它的每个产品）：行是服务产品（products/*.yaml 的全部产品，含草稿状态的，按配置读，新产品加了配置就出现）、
  知识元数据里登记了但还没有配置的产品、通用知识 GEN，以及模块（modules/*.yaml，按模块声明的 kb_tags
  统计带这些标签的条目，不分产品）；列是交付阶段。只统计已发布、未下线的现行版本。
- 缺口 TOP：产品（不含 GEN）× 阶段（不含“通用”）里条目不足 8 条的格子，按近 30 天零命中检索次数、条目数排序。
  审计不记检索词原文（数据保护红线第 4 条），所以只能按检索时带的产品、阶段过滤条件归到格子里，看不到具体问题；
  检索没带阶段的零命中只计到产品上（product_zero_hits）。同一问题反复零命中的次数按检索词摘要哈希统计。
- 贡献统计：按现行版本的作者统计已发布条数、近 30 天新发布条数和来源构成；按审核人统计近 30 天的审核次数
  （平台自动审核 platform:*、审核 Agent agent:* 不计）。只有内部账号和计数，不含条目内容。
知识区与主库是两个库：条目、审核在知识区，账号姓名和审计在主库，分开查、在内存里合并，不 JOIN。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, KbItem, KbReview, KbVersion, User
from . import labels, meta

GAP_LIMIT, CONTRIB_LIMIT = 5, 10
OK_MIN = 8                                   # 充足 ≥ 8（设计稿 06）
STAGES = meta.STAGES


def _level(n: int) -> str:
    return "ok" if n >= OK_MIN else "weak" if n >= 3 else "gap"


def _catalog():
    from ..products import config

    try:
        return config.catalog()
    except Exception:                        # noqa: BLE001  配置读不出来时地图仍可用，只按知识元数据列产品
        return None


def coverage_rows(live: list[tuple[KbItem, KbVersion]], products: dict[int, list[str]] | None = None) -> list[dict]:
    """products：条目 → 产品标签（B15，labels.products_of）；一个条目计入它的每个产品。不传时按主产品列。"""
    by_product: dict[str, Counter] = {}
    for item, _ in live:
        for code in (products or {}).get(getattr(item, "id", None), [item.product]):
            by_product.setdefault(code, Counter())[item.stage] += 1
    cat = _catalog()
    rows: list[dict] = []
    seen: set[str] = set()
    products = sorted(cat.products.values(), key=lambda p: (p.status != "ready", p.code)) if cat else []
    for p in products:
        seen.add(p.code)
        rows.append({"kind": "product", "code": p.code, "name": p.name, "status": p.status})
    for code, name in meta.PRODUCTS.items():
        if code not in seen and code != "GEN":
            seen.add(code)
            rows.append({"kind": "product", "code": code, "name": name, "status": "unconfigured"})
    rows.append({"kind": "general", "code": "GEN", "name": meta.PRODUCTS.get("GEN", "通用"), "status": "ready"})
    for r in rows:
        c = by_product.get(r["code"], Counter())
        r["cells"] = {s: c.get(s, 0) for s in STAGES}
        r["total"] = sum(c.values())
    for m in sorted(cat.modules.values(), key=lambda m: m.code) if cat else []:
        tags = set(m.kb_tags)
        c = Counter(item.stage for item, _ in live if tags & set(item.tags or []))
        rows.append({"kind": "module", "code": m.code, "name": m.name, "status": m.status, "tags": sorted(tags),
                     "cells": {s: c.get(s, 0) for s in STAGES}, "total": sum(c.values())})
    return rows


def _zero_hits(db: Session, since: datetime) -> tuple[Counter, Counter, int]:
    """近 30 天零命中检索：按（产品, 阶段）、按产品计数，以及反复零命中（同一摘要哈希 ≥2 次）的问题数。"""
    cells: Counter = Counter()
    products: Counter = Counter()
    shas: Counter = Counter()
    for d in db.scalars(select(AuditLog.detail).where(AuditLog.action.in_(("kb.search", "kb.basis")),
                                                     AuditLog.result == "allowed", AuditLog.at >= since)):
        try:
            x = json.loads(d or "{}")
        except ValueError:
            continue
        if x.get("results") != 0:
            continue
        f = x.get("filters") or {}
        if f.get("product"):
            products[f["product"]] += 1
            if f.get("stage"):
                cells[(f["product"], f["stage"])] += 1
        if x.get("query_sha"):
            shas[x["query_sha"]] += 1
    return cells, products, sum(1 for n in shas.values() if n >= 2)


def gaps(rows: list[dict], zero_cells: Counter) -> list[dict]:
    out = []
    for r in rows:
        if r["kind"] != "product":
            continue
        for s in STAGES:
            if s == "通用":
                continue
            n = r["cells"][s]
            if n >= OK_MIN:
                continue
            out.append({"product": r["code"], "product_name": r["name"], "stage": s, "items": n,
                        "level": _level(n), "zero_hits_30d": zero_cells.get((r["code"], s), 0)})
    out.sort(key=lambda g: (-g["zero_hits_30d"], g["items"], g["product"], STAGES.index(g["stage"])))
    return out[:GAP_LIMIT]


def contributors(db: Session, live: list[tuple[KbItem, KbVersion]], since: datetime) -> list[dict]:
    stats: dict[str, dict] = {}

    def row(name: str) -> dict:
        return stats.setdefault(name, {"username": name, "published": 0, "published_30d": 0, "reviews_30d": 0,
                                       "by_source": {}})

    for _, v in live:
        if not v.created_by or v.created_by.startswith(("platform", "agent:")):
            continue
        r = row(v.created_by)
        r["published"] += 1
        r["by_source"][v.source_type] = r["by_source"].get(v.source_type, 0) + 1
        if v.published_at and _aware(v.published_at) >= since:
            r["published_30d"] += 1
    for name in db.scalars(select(KbReview.reviewer).where(KbReview.created_at >= since)):
        if name and not name.startswith(("platform", "agent:")):
            row(name)["reviews_30d"] += 1
    names: dict[str, str] = {}
    if stats:
        for username, display in db.execute(select(User.username, User.display_name)
                                            .where(User.username.in_(list(stats)))):
            names[username] = display
    out = sorted(stats.values(), key=lambda r: (-r["published_30d"], -r["reviews_30d"], -r["published"],
                                                r["username"]))[:CONTRIB_LIMIT]
    for r in out:
        r["display_name"] = names.get(r["username"]) or r["username"]
    return out


def _aware(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


def overview(db: Session, live: list[tuple[KbItem, KbVersion]], since: datetime) -> dict:
    rows = coverage_rows(live, labels.products_of(db, [item.id for item, _ in live]))
    zero_cells, zero_products, repeated = _zero_hits(db, since)
    return {"matrix": rows, "stages": list(STAGES), "gaps": gaps(rows, zero_cells),
            "product_zero_hits_30d": dict(zero_products), "repeated_zero_hit_questions_30d": repeated,
            "contributors": contributors(db, live, since)}
