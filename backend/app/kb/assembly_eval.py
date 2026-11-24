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
- chapter：同样的检索，但用章节查询；只为验证框架能比较不同策略。
- slots：A12 按知识槽位装配，与交付链（project/runs.py + agents/slots.py）一致——保留步骤固定查询的检索作底，
  再按这一步的每个槽位用自己的检索词、按条目类型取 need 条。槽位取产品配置里这一步的 slots；
  评测集可在顶层写 `slots: {步骤 key: [槽位, ...]}` 覆盖（C10 槽位规格写进产品配置前试草稿用，格式同产品配置）。

A12 程序门（`gate`）：同一评测集分别跑 baseline 和 slots，slots 的总体命中率严格高于 baseline 才通过；
评测集涉及的步骤一个槽位都没有时直接判不通过（没有可比的装配）。CI 用 knowledge/eval/assembly_samples.yaml
和样例条目跑（tests/test_kb_assembly_eval.py，评测门 C8 的一部分，不连模型）；测试机用
`kb-assembly-eval <评测集> --as <用户> --gate` 对真实评测集跑。
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
STRATEGIES = ("baseline", "chapter", "slots")


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
    return {"product": str(data["product"]).strip(), "modules": [str(m) for m in modules], "chapters": chapters,
            "slots": _slots_override(data.get("slots"))}


def _slots_override(raw: Any) -> dict[str, tuple[dict, ...]]:
    """评测集顶层的 slots 覆盖：步骤 key → 槽位（按产品配置的 parse_slots 校验，不合格整份拒收）。"""
    from ..products import config

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AssemblyEvalError("slots 要写成 {步骤 key: [槽位, ...]}")
    out: dict[str, tuple[dict, ...]] = {}
    for step, specs in raw.items():
        errors: list[str] = []
        parsed = config.parse_slots(config._P(f"slots.{step}", errors), specs)
        if errors or not parsed:
            raise AssemblyEvalError(errors[0] if errors else f"slots.{step} 不能为空")
        out[str(step)] = parsed
    return out


def _snapshot(evalset: dict[str, Any]) -> dict[str, Any]:
    from ..products import config

    try:
        return config.compose_codes(evalset["product"], evalset["modules"])
    except config.CatalogError as e:
        raise AssemblyEvalError(f"产品配置读不出来：{e}") from e


Retriever = Callable[[str], list[str]]
SlotRetriever = Callable[[dict], list[str]]


def _retriever(db: Session, user: User, snap: dict[str, Any], keys: dict[str, str | None]) -> Retriever:
    product = str(snap.get("product") or "")
    tags, basis = list(snap.get("kb_tags") or []), list(snap.get("basis") or [])

    def retrieve(query: str) -> list[str]:
        hits = search.search(db, user, query, product=product, limit=KB_LIMIT, boost_tags=tags)["items"]
        hits += search.basis(db, user, query, limit=BASIS_LIMIT, product=product, boost_refs=basis)["items"]
        return list(dict.fromkeys(keys.get(h["doc_id"]) or h["doc_id"] for h in hits))
    return retrieve


def _slot_retriever(db: Session, user: User, snap: dict[str, Any], keys: dict[str, str | None]) -> SlotRetriever:
    """与 kb.search 工具在项目里的调用一致（产品、kb_tags 按配置），每个槽位按类型取 need 条。"""
    product, tags = str(snap.get("product") or ""), list(snap.get("kb_tags") or [])

    def retrieve(slot: dict) -> list[str]:
        hits = search.search(db, user, slot["query"], product=product, doc_type=slot["type"], limit=slot["need"],
                             boost_tags=tags)["items"][: slot["need"]]
        return [keys.get(h["doc_id"]) or h["doc_id"] for h in hits]
    return retrieve


def step_slots(evalset: dict[str, Any], snap: dict[str, Any]) -> dict[str, tuple[dict, ...]]:
    """每一步实际用的槽位：评测集覆盖优先，否则取产品配置。"""
    configured = {s["key"]: tuple(s.get("slots") or ()) for s in snap["chain"]}
    return {**configured, **(evalset.get("slots") or {})}


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
    slot_retrieve, specs = _slot_retriever(db, user, snap, keys), step_slots(evalset, snap)
    report = AssemblyReport(evalset["product"], strategy)
    cache: dict[str, list[str]] = {}
    slot_cache: dict[str, list[str]] = {}
    for ch in evalset["chapters"]:
        step = str(ch["step"])
        query = (str(ch.get("query") or ch["title"]) if strategy == "chapter"
                 else queries[step]).strip() or str(ch["title"])
        if query not in cache:                     # 基线同一步的各章查询相同，只检索一次
            cache[query] = retrieve(query)
        got = cache[query]
        if strategy == "slots":                    # 步骤检索作底 + 本步各槽位
            if step not in slot_cache:
                slot_cache[step] = [k for s in specs.get(step, ()) for k in slot_retrieve(s)]
            got = list(dict.fromkeys(got + slot_cache[step]))
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


def gate(db: Session, user: User, evalset: dict[str, Any]) -> tuple[bool, str, AssemblyReport, AssemblyReport]:
    """A12 程序门：按槽位装配（slots）的总体命中率严格高于固定查询基线（baseline）才通过。
    返回 (是否通过, 一行结论, 基线报告, 槽位报告)。"""
    base = run(db, user, evalset, "baseline")
    steps = {str(c["step"]) for c in evalset["chapters"]}
    if not any(step_slots(evalset, _snapshot(evalset)).get(s) for s in steps):
        return False, "A12 门：评测集涉及的步骤都没有配置知识槽位，没有可比的装配：未通过", base, base
    slot = run(db, user, evalset, "slots")
    diff = round(slot.hit_rate - base.hit_rate, 4)
    ok = diff > 0
    return ok, (f"A12 门：按槽位装配命中率 {slot.hit_rate:.1%}，固定查询基线 {base.hit_rate:.1%}，"
                f"{'+' if diff >= 0 else ''}{diff:.1%}（要求高于基线）：{'通过' if ok else '未通过'}"), base, slot


def format_report(r: AssemblyReport) -> str:
    lines = [f"章节级装配评测（{r.product}，策略 {r.strategy}）：{len(r.chapters)} 章、应命中知识 {r.expected} 条",
             f"- 命中率 {r.hit_rate:.1%}；就绪度 {r.readiness:.1%}；就绪命中率 {r.ready_hit_rate:.1%}", "",
             "| 章节 | 步骤 | 应命中 | 就绪 | 命中 | 缺槽 | 漏装 |", "|---|---|---|---|---|---|---|"]
    for c in r.chapters:
        miss = [k for k in c.ready if k not in c.hit]
        lines.append(f"| {c.id} {c.title} | {c.step} | {len(c.expect)} | {len(c.ready)} | {len(c.hit)} "
                     f"| {'、'.join(c.missing) or '—'} | {'、'.join(miss) or '—'} |")
    return "\n".join(lines)
