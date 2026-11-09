"""依据查询评测（python -m app.cli kb-eval <题目文件> --as <用户>）。S2 验收：依据查询评测准确率 ≥ 90%。

题目文件（YAML 列表），条目按 key（条目文件里的固定标识）对应：

    - id: B001
      question: 网络运营者对网络日志要留存多久？
      mode: basis                 # basis 依据查询（只查 L4）/ search 知识检索
      expect: [csl-art21-mlps]    # 期望命中的条目 key，命中其一即算对；空列表 = 期望查不到（应提示【依据待核实】）
      product: SOC                # 可选，search 时按服务产品过滤

指标：
- 准确率（验收口径）：期望命中的题，期望条目出现在前 3 条；期望查不到的题，结果为空。
- 前 1 / 3 / 5 命中率、MRR（只算期望命中的题）。
- 相似度分布：期望条目的向量相似度、排第一但不对的条目的相似度、“期望查不到”的题里最高的相似度，
  用来校准 KB_VECTOR_MIN_SIMILARITY / KB_BASIS_MIN_SIMILARITY。
评测按普通检索调用，审计照常记录（不记题目原文）。
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KbItem, User
from . import search

TOP_K = 5
ACCURACY_K = 3


class EvalError(ValueError):
    pass


@dataclass
class QuestionResult:
    id: str
    mode: str
    expect: list[str]
    got: list[str]                  # 返回条目的 key（没有 key 的条目用 doc_id）
    rank: int | None                # 期望条目的名次（1 起）；没命中为 None
    correct: bool
    expected_similarity: float | None = None
    top_wrong_similarity: float | None = None


@dataclass
class EvalReport:
    total: int = 0
    accuracy: float = 0.0
    by_mode: dict = field(default_factory=dict)
    similarity: dict = field(default_factory=dict)
    vector: str = ""
    results: list[QuestionResult] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["results"] = [asdict(r) for r in self.results]
        return d


def load_questions(path: Path) -> list[dict]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError as e:
        raise EvalError(f"题目文件不是合法的 YAML：{str(e)[:100]}") from e
    if not isinstance(data, list):
        raise EvalError("题目文件应是题目列表")
    seen = set()
    for q in data:
        if not isinstance(q, dict) or not q.get("id") or not q.get("question"):
            raise EvalError(f"题目缺少 id 或 question：{q!r:.80}")
        if q["id"] in seen:
            raise EvalError(f"题目 id 重复：{q['id']}")
        seen.add(q["id"])
        if q.get("mode", "basis") not in ("basis", "search"):
            raise EvalError(f"{q['id']}：mode 只能是 basis 或 search")
        if not isinstance(q.get("expect", []), list):
            raise EvalError(f"{q['id']}：expect 应是列表")
    return data


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    return {"n": len(values), "min": round(min(values), 4), "median": round(statistics.median(values), 4),
            "max": round(max(values), 4)}


def run(db: Session, user: User, questions: list[dict]) -> EvalReport:
    keys = dict(db.execute(select(KbItem.doc_id, KbItem.import_key)).all())
    report = EvalReport(total=len(questions))
    for q in questions:
        mode, expect = q.get("mode", "basis"), [str(x) for x in q.get("expect", [])]
        if mode == "basis":
            out = search.basis(db, user, q["question"], limit=TOP_K)
        else:
            out = search.search(db, user, q["question"], product=q.get("product", ""), limit=TOP_K)
        report.vector = out.get("vector", "")
        hits = out["items"]
        got = [keys.get(h["doc_id"]) or h["doc_id"] for h in hits]
        rank = next((i for i, k in enumerate(got, 1) if k in expect), None)
        correct = (rank is not None and rank <= ACCURACY_K) if expect else not hits
        exp_sim = hits[rank - 1]["similarity"] if rank else None
        wrong = [h["similarity"] for h, k in zip(hits, got, strict=True) if k not in expect and h["similarity"] is not None]
        report.results.append(QuestionResult(q["id"], mode, expect, got, rank, correct, exp_sim,
                                             max(wrong) if wrong else None))

    for mode in ("basis", "search"):
        rs = [r for r in report.results if r.mode == mode]
        if not rs:
            continue
        pos = [r for r in rs if r.expect]
        report.by_mode[mode] = {
            "questions": len(rs), "negatives": len(rs) - len(pos),
            "accuracy": round(sum(r.correct for r in rs) / len(rs), 4),
            "hit@1": round(sum(1 for r in pos if r.rank == 1) / len(pos), 4) if pos else None,
            "hit@3": round(sum(1 for r in pos if r.rank and r.rank <= 3) / len(pos), 4) if pos else None,
            "hit@5": round(sum(1 for r in pos if r.rank) / len(pos), 4) if pos else None,
            "mrr": round(sum(1 / r.rank for r in pos if r.rank) / len(pos), 4) if pos else None,
        }
    basis = [r for r in report.results if r.mode == "basis"]
    report.accuracy = round(sum(r.correct for r in basis) / len(basis), 4) if basis else 0.0
    report.similarity = {
        "expected_hits": _stats([r.expected_similarity for r in report.results if r.expected_similarity is not None]),
        "top_wrong": _stats([r.top_wrong_similarity for r in report.results
                             if r.expect and r.top_wrong_similarity is not None]),
        "negatives_max": _stats([r.top_wrong_similarity for r in report.results
                                 if not r.expect and r.top_wrong_similarity is not None]),
    }
    return report


def format_report(r: EvalReport) -> str:
    lines = [f"依据查询准确率（验收口径，≥ 90% 为达标）：{r.accuracy:.1%}" + ("  达标" if r.accuracy >= 0.9 else "  未达标"),
             f"向量检索：{r.vector or '—'}", ""]
    for mode, m in r.by_mode.items():
        name = "依据查询" if mode == "basis" else "知识检索"
        hits = "" if m["hit@1"] is None else \
            f"，前1 {m['hit@1']:.0%} / 前3 {m['hit@3']:.0%} / 前5 {m['hit@5']:.0%}，MRR {m['mrr']:.3f}"
        lines.append(f"{name}：{m['questions']} 题（其中期望查不到 {m['negatives']} 题），准确率 {m['accuracy']:.0%}{hits}")
    lines += ["", "相似度分布（用于校准门槛）："]
    for k, label in (("expected_hits", "期望条目"), ("top_wrong", "不对但排在前面的"), ("negatives_max", "期望查不到的题里最高的")):
        s = r.similarity.get(k)
        lines.append(f"  {label}：" + (f"{s['n']} 个，最低 {s['min']} / 中位 {s['median']} / 最高 {s['max']}" if s else "无数据"))
    wrong = [x for x in r.results if not x.correct]
    if wrong:
        lines += ["", f"答错的题（{len(wrong)}）："]
        lines += [f"  {x.id}（{x.mode}）期望 {x.expect or '查不到'}，实际 {x.got[:3] or '空'}" for x in wrong]
    return "\n".join(lines)
