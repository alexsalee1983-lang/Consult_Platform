"""向量模型对比评测（python -m app.cli kb-eval <题目文件> --as <用户> --embed-compare --b-url … --b-model … --b-dim …）。

决策记录 2026-11-18：在测试机上比较候选向量模型（如 BAAI/bge-m3）和现用模型（EMBED_MODEL），由负责人定生产用哪个。
向量列维度固定在迁移里（gateway.EMBED_DIM），候选模型维度不同时没法直接入库比，所以这里只做离线的向量召回对比：

- 语料：调用人看得到的已发布、未下线现行版本的全部切片，向量化文本同 search.embed_version（标题 + 摘要 + 章节标题 + 正文）；
  依据查询（mode: basis）只在 L4 里找，同 search.basis。机密级检测命中的切片两边都不用。
- 题目：评测题里带 expect、且期望条目在语料里的；负例和库里没有的计入 skipped。
- 指标：按余弦相似度排序（同一条目只算排得最靠前的切片），命中@1/3/5/10、MRR，以及向量化耗时。

不走全文、重排和产品加权，只看向量这一路的区分度；全链路效果换模型、kb-reembed 后用 kb-eval 再跑。
都经网关 gateway.embed（每条过机密级检测），不写库、不改配置；生产环境拒绝执行。
审计 kb.eval_embed 只记题数、切片数、模型名和指标，不记题目、片段和服务地址。
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..models import KbChunk, KbItem, KbVersion, User
from ..services import gateway
from ..services.guard import check
from . import review

KS = (1, 3, 5, 10)


class EmbedCompareError(Exception):
    pass


@dataclass
class EmbedCompareReport:
    questions: int = 0
    skipped: int = 0
    chunks: int = 0
    blocked_chunks: int = 0
    models: list[dict] = field(default_factory=list)          # A（现用）、B（候选）各一行
    per_question: list[dict] = field(default_factory=list)    # id、期望条目在两边的名次

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class _Chunk:
    key: str
    layer: str
    text: str


def _corpus(db: Session, user: User) -> list[_Chunk]:
    rows = db.execute(select(KbChunk.heading, KbChunk.text, KbVersion, KbItem)
                      .join(KbVersion, KbVersion.id == KbChunk.version_id)
                      .join(KbItem, KbItem.current_version_id == KbVersion.id)
                      .where(KbVersion.status == "published", KbItem.retired.is_(False))
                      .order_by(KbChunk.id)).all()
    seen: dict[int, bool] = {}
    out = []
    for heading, body, v, item in rows:
        if v.id not in seen:
            seen[v.id] = review.can_view(db, user, item, v)
        if not seen[v.id]:
            continue
        prefix = "\n".join(x for x in (v.title, v.summary) if x)
        out.append(_Chunk(item.import_key or item.doc_id, item.layer,
                          "\n".join(x for x in (prefix, heading, body) if x)))
    return out


def _embed_all(texts: list[str], **kw) -> list[list[float]]:
    size = max(1, get_settings().embed_batch_size)
    vecs: list[list[float]] = []
    for i in range(0, len(texts), size):
        vecs += gateway.embed(texts[i:i + size], **kw).vectors
    return vecs


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _rank(qv: list[float], chunks: list[_Chunk], cvs: list[list[float]], expect: set[str], basis: bool) -> int | None:
    scored = sorted(((sum(a * b for a, b in zip(qv, cv, strict=True)), i) for i, cv in enumerate(cvs)
                     if not basis or chunks[i].layer == "L4"), key=lambda x: (-x[0], x[1]))
    seen: set[str] = set()
    for _, i in scored:
        k = chunks[i].key
        if k in seen:
            continue
        seen.add(k)
        if k in expect:
            return len(seen)
    return None


def _metrics(ranks: list[int | None]) -> dict:
    n = len(ranks) or 1
    out = {f"hit@{k}": round(sum(1 for r in ranks if r is not None and r <= k) / n, 4) for k in KS}
    out["mrr"] = round(sum(1 / r for r in ranks if r) / n, 4)
    return out


def run(db: Session, user: User, questions: list[dict], *, b_url: str, b_model: str, b_dim: int,
        b_key: str = "") -> EmbedCompareReport:
    s = get_settings()
    if s.is_prod:
        raise EmbedCompareError("向量模型对比评测只在开发、测试环境运行")
    if not s.embed_base_url:
        raise EmbedCompareError("现用向量服务未配置（EMBED_BASE_URL），没有可对比的基线")
    if not b_url or not b_model or b_dim <= 0:
        raise EmbedCompareError("要写候选模型的地址、模型名和维度（--b-url、--b-model、--b-dim）")
    rep = EmbedCompareReport()
    all_chunks = _corpus(db, user)
    chunks = [c for c in all_chunks if not check(c.text).blocked]
    rep.chunks, rep.blocked_chunks = len(chunks), len(all_chunks) - len(chunks)
    keys = {c.key for c in chunks}
    cases = []
    for q in questions:
        expect = {str(x) for x in q.get("expect", [])} & keys
        if not expect or check(q["question"]).blocked:
            rep.skipped += 1
            continue
        cases.append((str(q["id"]), q["question"], expect, q.get("mode", "basis") == "basis"))
    rep.questions = len(cases)
    if not cases:
        raise EmbedCompareError("没有可评测的题：题目要带 expect，且期望条目已发布、调用人看得到")
    sides = (("A", {}, s.embed_model, gateway.EMBED_DIM),
             ("B", {"base_url": b_url, "model": b_model, "dim": b_dim, "api_key": b_key}, b_model, b_dim))
    ranks: dict[str, list[int | None]] = {}
    for side, kw, model, dim in sides:
        t0 = time.monotonic()
        cvs = [_norm(v) for v in _embed_all([c.text for c in chunks], **kw)]
        qvs = [_norm(v) for v in _embed_all([c[1] for c in cases], **kw)]
        secs = round(time.monotonic() - t0, 1)
        ranks[side] = [_rank(qv, chunks, cvs, expect, basis) for qv, (_, _, expect, basis) in zip(qvs, cases, strict=True)]
        rep.models.append({"side": side, "model": model, "dim": dim, **_metrics(ranks[side]), "seconds": secs})
    rep.per_question = [{"id": c[0], "rank_a": a, "rank_b": b} for c, a, b in zip(cases, ranks["A"], ranks["B"], strict=True)]
    audit.record(db, actor=user.username, action="kb.eval_embed", result="ok", questions=rep.questions,
                 skipped=rep.skipped, chunks=rep.chunks,
                 models=[{k: m[k] for k in ("model", "dim", "hit@1", "hit@5", "mrr")} for m in rep.models])
    return rep


def format_report(rep: EmbedCompareReport) -> str:
    lines = [f"向量模型对比（只比向量召回）：{rep.questions} 题，切片 {rep.chunks} 个"
             + (f"，{rep.skipped} 题不计入（负例或期望条目不在库里）" if rep.skipped else "")
             + (f"，{rep.blocked_chunks} 个切片命中机密级检测未用" if rep.blocked_chunks else ""),
             "", "  模型                              维度   命中@1  命中@3  命中@5  命中@10   MRR    耗时(秒)"]
    for m in rep.models:
        lines.append(f"  {m['side']} {m['model']:<32} {m['dim']:>5}  {m['hit@1']:>6.1%}  {m['hit@3']:>6.1%}  "
                     f"{m['hit@5']:>6.1%}  {m['hit@10']:>7.1%}  {m['mrr']:.3f}  {m['seconds']:>7}")
    a, b = rep.models
    diff = [q["id"] for q in rep.per_question if q["rank_a"] != q["rank_b"]]
    lines += ["", f"名次不同的题 {len(diff)} 道" + (f"：{'、'.join(diff[:20])}" if diff else ""),
              f"MRR 差（B − A）：{b['mrr'] - a['mrr']:+.3f}",
              "只出报告、不改配置；换生产向量模型由负责人定，换维度要新迁移并 kb-reembed（见 CLAUDE.md 检索约定）。"]
    return "\n".join(lines)
