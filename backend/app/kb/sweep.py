"""门槛校准（python -m app.cli kb-eval <题目文件> --as <用户> --sweep，二期 v0.2 关口“门槛校准”）。

用评测题（格式同 evaluate.py）算重排门槛取各候选值时的准确率：依据查询对应 KB_BASIS_MIN_RERANK，
知识检索对应 KB_SEARCH_MIN_RERANK。每题只检索一次（门槛 0，经网关重排），再按各门槛在本地筛选：
点到条款的（精确匹配）、没进重排候选的、依据查询点名了文件没点到条款的不受门槛限制，同 search.py。
近似：同一条目只按排得最靠前的切片计（门槛挡掉它时不再看同条目的次优切片）。

推荐值：准确率最高的那组门槛里，现值在内就保持现值，否则取中间的一个（两头都留余地）。
只出报告，不改配置，由负责人定。报告和审计只记题数、计数、准确率和门槛，不记检索词。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..models import KbItem, User
from . import search
from .evaluate import ACCURACY_K, TOP_K

BASIS_GRID = (0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.1, 0.15, 0.2, 0.3, 0.5)
SEARCH_GRID = (0.0, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05)
SWEEP_LIMIT = 20          # 门槛 0 时多取一些，门槛提高、前面的被挡掉后后面的能补上来


@dataclass
class SweepReport:
    questions: int = 0
    skipped: int = 0                                       # 重排不可用、无法校准的题
    basis: list[dict] = field(default_factory=list)        # 每个门槛一行：threshold、questions、correct、accuracy、false_hits、missed
    search: list[dict] = field(default_factory=list)
    current: dict = field(default_factory=dict)
    recommend: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class _Case:
    expect: list[str]
    hits: list[tuple[str, float | None, bool]]             # (key, 重排分数, 不受门槛限制)


def _correct(c: _Case, t: float) -> tuple[bool, bool, bool]:
    """返回 (是否答对, 负例是否误命中, 正例是否漏掉)。"""
    kept = [k for k, score, exempt in c.hits if exempt or score is None or score >= t][:TOP_K]
    if not c.expect:
        return not kept, bool(kept), False
    rank = next((i for i, k in enumerate(kept, 1) if k in c.expect), None)
    ok = rank is not None and rank <= ACCURACY_K
    return ok, False, not ok


def _rows(cases: list[_Case], grid: tuple[float, ...]) -> list[dict]:
    rows = []
    for t in grid:
        res = [_correct(c, t) for c in cases]
        n = sum(r[0] for r in res)
        rows.append({"threshold": t, "questions": len(cases), "correct": n,
                     "accuracy": round(n / len(cases), 4), "false_hits": sum(r[1] for r in res),
                     "missed": sum(r[2] for r in res)})
    return rows


def _recommend(rows: list[dict], current: float) -> float:
    best = max(r["accuracy"] for r in rows)
    top = [r["threshold"] for r in rows if r["accuracy"] == best]
    return current if current in top else top[(len(top) - 1) // 2]


def run(db: Session, user: User, questions: list[dict], *, basis: tuple[float, ...] | list[float] = BASIS_GRID,
        search_grid: tuple[float, ...] | list[float] = SEARCH_GRID) -> SweepReport:
    s = get_settings()
    keys = dict(db.execute(select(KbItem.doc_id, KbItem.import_key)).all())
    rep = SweepReport(questions=len(questions),
                      current={"basis": s.kb_basis_min_rerank, "search": s.kb_search_min_rerank})
    cases: dict[str, list[_Case]] = {"basis": [], "search": []}
    for q in questions:
        mode, product = q.get("mode", "basis"), str(q.get("product", "")).strip()
        trace: dict = {}
        if mode == "basis":
            out = search.basis(db, user, q["question"], limit=SWEEP_LIMIT, min_rerank=0.0, product=product, trace=trace)
        else:
            out = search.search(db, user, q["question"], product=product, limit=SWEEP_LIMIT, min_rerank=0.0,
                                trace=trace)
        if out.get("rerank") != "ok":
            # 没有候选的题（多为负例）任何门槛下都对，照常计入；重排不可用的无法校准
            if out.get("rerank") == "no candidates" or not out["items"]:
                cases[mode].append(_Case([str(x) for x in q.get("expect", [])], []))
            else:
                rep.skipped += 1
            continue
        exempt_all = bool(trace.get("floor_exempt_all"))
        hits = [(keys.get(h["doc_id"]) or h["doc_id"], h.get("rerank_score"), exempt_all or "精确" in h["matched"])
                for h in out["items"]]
        cases[mode].append(_Case([str(x) for x in q.get("expect", [])], hits))
    for mode, grid in (("basis", basis), ("search", search_grid)):
        has_scores = any(c.hits for c in cases[mode])
        if cases[mode] and has_scores:
            rows = _rows(cases[mode], tuple(grid))
            setattr(rep, mode, rows)
            rep.recommend[mode] = _recommend(rows, rep.current[mode])
    audit.record(db, actor=user.username, action="kb.eval_sweep", result="ok", questions=rep.questions,
                 skipped=rep.skipped, recommend=rep.recommend,
                 accuracy={m: max(r["accuracy"] for r in getattr(rep, m)) for m in rep.recommend})
    return rep


def format_report(rep: SweepReport) -> str:
    lines = [f"门槛校准：{rep.questions} 题" + (f"，其中 {rep.skipped} 题重排不可用、未计入" if rep.skipped else "")]
    if not rep.recommend:
        lines.append("重排不可用或没有可校准的题：门槛校准要在重排服务正常时运行（.env 的 RERANK_BASE_URL）")
        return "\n".join(lines)
    for mode, name, env in (("basis", "依据查询", "KB_BASIS_MIN_RERANK"), ("search", "知识检索", "KB_SEARCH_MIN_RERANK")):
        rows = getattr(rep, mode)
        if not rows:
            continue
        lines += ["", f"{name}（{env}，现值 {rep.current[mode]}，{rows[0]['questions']} 题）：",
                  "  门槛      准确率   答对  负例误命中  正例漏掉"]
        lines += [f"  {r['threshold']:<8} {r['accuracy']:>6.1%}  {r['correct']:>4}  {r['false_hits']:>8}  {r['missed']:>7}"
                  + ("  ← 现值" if r["threshold"] == rep.current[mode] else "") for r in rows]
        rec = rep.recommend[mode]
        lines.append(f"  推荐 {env}={rec}" + ("（保持现值）" if rec == rep.current[mode] else "（与现值不同，由负责人定）"))
    lines += ["", "只出报告、不改配置；改门槛在 .env 里设，重启后端生效。"]
    return "\n".join(lines)
