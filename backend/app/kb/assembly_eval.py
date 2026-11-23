"""章节级装配评测（三期 A24）：python -m app.cli kb-assembly-eval <评测集> --as <用户>。

交付链每一步按“固定查询”检索知识再起草；A24 衡量每一章该用的知识有没有被装配进来，
给 A12（按知识清单装配）一个必须超过的基线，并把“章节缺槽”报给缺口池（B25）和缺槽访谈（C13）。

评测集（YAML），每章 3～5 条“应命中知识”，按条目 key（条目文件里的固定标识）对应：

    product: SOC                  # 服务产品代号，按产品配置取交付链和检索默认值
    modules: []                   # 可选：模块代号，合并进配置（kb_tags、basis 跟着变）
    chapters:
      - id: SOC-D5-01
        step: design_doc          # 交付链步骤 key；基线用这一步配置里的固定查询
        title: 组织与人员
        query: SOC 组织 岗位 编制 值班   # 可选：章节查询，chapter 策略用（不写用 title）
        expect: [soc-night-duty, soc-shift-handover, soc-metrics]

指标（每章和总体）：
- 就绪度：应命中知识里库中已发布（有现行版本、未停用）的比例；没就绪的列为“缺槽”。
- 命中率：应命中知识里被装配进来的比例（验收口径，A12 必须高于基线）。
- 就绪命中率：只算已就绪的，衡量检索本身。

策略：
- baseline：照现在交付链的做法，用步骤固定查询检索（知识 5 条 + 依据 3 条，产品、kb_tags、basis 按配置），
  与 agents/knowledge.py 一致；不带项目主输入前 200 字（评测不跑项目），所以是保守基线。
- chapter：同样的检索，但用章节查询；只为验证框架能比较不同策略，A12 的按槽位装配另加策略。
评测按普通检索调用，审计照常记录（不记查询原文）。报告只含章节编号、key 和计数，不含正文。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KbItem, User
from . import search

MIN_EXPECT, MAX_EXPECT = 3, 5
KB_LIMIT, BASIS_LIMIT = 5, 3          # 与 agents/knowledge.py 的 MAX_ITEMS、MAX_BASIS 一致
STRATEGIES = ("baseline", "chapter")


class AssemblyEvalError(ValueError):
    pass


@dataclass
class ChapterResult:
    id: str
    step: str
    title: str
    expect: list[str]
    got: list[str]
    ready: list[str]
    hit: list[str]

    @property
    def missing(self) -> list[str]:
        return [k for k in self.expect if k not in self.ready]


@dataclass
class AssemblyReport:
    product: str
    strategy: str
    chapters: list[ChapterResult] = field(default_factory=list)

    def _ratio(self, num: int, den: int) -> float:
        return round(num / den, 4) if den else 0.0

    @property
    def expected(self) -> int:
        return sum(len(c.expect) for c in self.chapters)

    @property
    def readiness(self) -> float:
        return self._ratio(sum(len(c.ready) for c in self.chapters), self.expected)

    @property
    def hit_rate(self) -> float:
        return self._ratio(sum(len(c.hit) for c in self.chapters), self.expected)

    @property
    def ready_hit_rate(self) -> float:
        return self._ratio(sum(len(c.hit) for c in self.chapters), sum(len(c.ready) for c in self.chapters))

    def to_json(self) -> dict[str, Any]:
        return {"product": self.product, "strategy": self.strategy, "expected": self.expected,
                "readiness": self.readiness, "hit_rate": self.hit_rate, "ready_hit_rate": self.ready_hit_rate,
                "chapters": [{**asdict(c), "missing": c.missing} for c in self.chapters]}


def load(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise AssemblyEvalError(f"读不了评测集：{type(e).__name__}") from e
    if not isinstance(data, dict) or not str(data.get("product") or "").strip():
        raise AssemblyEvalError("评测集要写 product")
    chapters = data.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise AssemblyEvalError("评测集要有 chapters")
    seen: set[str] = set()
    for i, ch in enumerate(chapters, 1):
        if not isinstance(ch, dict):
            raise AssemblyEvalError(f"第 {i} 章格式不对")
        cid = str(ch.get("id") or "").strip()
        if not cid or cid in seen:
            raise AssemblyEvalError(f"第 {i} 章 id 为空或重复")
        seen.add(cid)
        for k in ("step", "title"):
            if not str(ch.get(k) or "").strip():
                raise AssemblyEvalError(f"{cid} 要写 {k}")
        exp = ch.get("expect")
        if (not isinstance(exp, list) or len(set(map(str, exp))) != len(exp)
                or not MIN_EXPECT <= len(exp) <= MAX_EXPECT):
            raise AssemblyEvalError(f"{cid} 的 expect 要有 {MIN_EXPECT}～{MAX_EXPECT} 条不重复的知识 key")
    modules = data.get("modules") or []
    if not isinstance(modules, list):
        raise AssemblyEvalError("modules 要是列表")
    return {"product": str(data["product"]).strip(), "modules": [str(m) for m in modules], "chapters": chapters}


def _snapshot(evalset: dict[str, Any]) -> dict[str, Any]:
    from ..products import config

    try:
        return config.compose_codes(evalset["product"], evalset["modules"])
    except config.CatalogError as e:
        raise AssemblyEvalError(f"产品配置读不出来：{e}") from e


Retriever = Callable[[str], list[str]]


def _retriever(db: Session, user: User, snap: dict[str, Any], keys: dict[str, str | None]) -> Retriever:
    product = str(snap.get("product") or "")
    tags, basis = list(snap.get("kb_tags") or []), list(snap.get("basis") or [])

    def retrieve(query: str) -> list[str]:
        hits = search.search(db, user, query, product=product, limit=KB_LIMIT, boost_tags=tags)["items"]
        hits += search.basis(db, user, query, limit=BASIS_LIMIT, product=product, boost_refs=basis)["items"]
        return list(dict.fromkeys(keys.get(h["doc_id"]) or h["doc_id"] for h in hits))
    return retrieve


def run(db: Session, user: User, evalset: dict[str, Any], strategy: str = "baseline") -> AssemblyReport:
    if strategy not in STRATEGIES:
        raise AssemblyEvalError(f"没有策略 {strategy}（可选 {'、'.join(STRATEGIES)}）")
    snap = _snapshot(evalset)
    queries = {s["key"]: s["query"] for s in snap["chain"]}
    unknown = sorted({str(c["step"]) for c in evalset["chapters"]} - set(queries))
    if unknown:
        raise AssemblyEvalError(f"产品 {evalset['product']} 的交付链里没有步骤 {'、'.join(unknown)}")
    keys = dict(db.execute(select(KbItem.doc_id, KbItem.import_key)).all())
    wanted = {str(k) for c in evalset["chapters"] for k in c["expect"]}
    ready_keys = set(db.scalars(select(KbItem.import_key).where(
        KbItem.import_key.in_(wanted), KbItem.retired.is_(False), KbItem.current_version_id.is_not(None))))
    retrieve = _retriever(db, user, snap, keys)
    report = AssemblyReport(evalset["product"], strategy)
    cache: dict[str, list[str]] = {}
    for ch in evalset["chapters"]:
        step = str(ch["step"])
        query = (queries[step] if strategy == "baseline"
                 else str(ch.get("query") or ch["title"])).strip() or str(ch["title"])
        if query not in cache:                     # 基线同一步的各章查询相同，只检索一次
            cache[query] = retrieve(query)
        got = cache[query]
        expect = [str(k) for k in ch["expect"]]
        report.chapters.append(ChapterResult(str(ch["id"]), step, str(ch["title"]), expect, got,
                                             [k for k in expect if k in ready_keys],
                                             [k for k in expect if k in got]))
    return report


def compare(report: AssemblyReport, baseline: dict[str, Any], min_gain: float = 0.0) -> tuple[bool, str]:
    """和基线报告（--out 写出的 JSON）比总体命中率：高出 min_gain 以上（含）才算通过。"""
    try:
        base = float(baseline["hit_rate"])
    except (KeyError, TypeError, ValueError) as e:
        raise AssemblyEvalError("基线文件里没有 hit_rate") from e
    if baseline.get("product") != report.product:
        raise AssemblyEvalError(f"基线是 {baseline.get('product')} 的，本次是 {report.product}")
    diff = round(report.hit_rate - base, 4)
    ok = diff >= min_gain
    return ok, (f"命中率 {report.hit_rate:.1%}，基线 {base:.1%}，{'+' if diff >= 0 else ''}{diff:.1%}"
                f"（要求 ≥ +{min_gain:.1%}）：{'通过' if ok else '未通过'}")


def format_report(r: AssemblyReport) -> str:
    lines = [f"章节级装配评测（{r.product}，策略 {r.strategy}）：{len(r.chapters)} 章、应命中知识 {r.expected} 条",
             f"- 命中率 {r.hit_rate:.1%}；就绪度 {r.readiness:.1%}；就绪命中率 {r.ready_hit_rate:.1%}", "",
             "| 章节 | 步骤 | 应命中 | 就绪 | 命中 | 缺槽 | 漏装 |", "|---|---|---|---|---|---|---|"]
    for c in r.chapters:
        miss = [k for k in c.ready if k not in c.hit]
        lines.append(f"| {c.id} {c.title} | {c.step} | {len(c.expect)} | {len(c.ready)} | {len(c.hit)} "
                     f"| {'、'.join(c.missing) or '—'} | {'、'.join(miss) or '—'} |")
    return "\n".join(lines)
