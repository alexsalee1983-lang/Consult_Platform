"""知识检索：全文 + 向量混合检索（RRF 融合），只查已发布、未下线的现行版本。

- 全文：PostgreSQL 中文全文索引（zh），查询分词后任一词命中即可，按 ts_rank_cd 排序。
- 向量：查询经网关 embed 转成向量，按余弦距离在 HNSW 索引上找最近的切片；向量服务没启用或不可用时只用全文。
- 融合：两路各取前 40 个切片，按 RRF（1 / (60 + 名次)）相加，同一条目只保留得分最高的切片。
- 相似度下限：只有向量命中、全文没命中的切片，余弦相似度低于下限就丢掉（向量检索总会返回“最近的”结果）；
  依据查询用更高的下限（KB_BASIS_MIN_SIMILARITY），查不到就提示【依据待核实】。
- 过滤：层级、服务产品（默认同时包含 GEN 通用知识）、阶段、类型；受限条目按可见角色（与 app/kb/review.py 一致）。
- 查询先过机密级检测；审计只记查询长度和摘要哈希，不记原文。
- 依据查询（basis）：只查 L4 依据库，按公共规范第 3 节给出引用格式，复审日期已过的提示“请确认是否有更新”。
SQLite（本地快速测试）没有全文和向量索引，用二字词匹配的简化实现，只为让接口和规则可测。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..models import KbChunk, KbItem, KbVersion, User
from ..services import gateway, guard, jobs, usage
from . import review

RRF_K = 60
POOL = 40
MAX_LIMIT = 20


class SearchError(ValueError):
    def __init__(self, status: int, detail):
        super().__init__(detail if isinstance(detail, str) else "检索被拒绝")
        self.status = status
        self.detail = detail


@dataclass
class Hit:
    doc_id: str
    version: int
    title: str
    layer: str
    domain: str
    product: str
    stage: str
    doc_type: str
    heading: str
    snippet: str
    score: float
    matched: str                 # 全文 / 向量 / 全文+向量
    review_date: str | None
    ref_document: str = ""
    ref_version: str = ""
    ref_status: str = ""
    needs_update_check: bool = False
    citation: str = ""
    similarity: float | None = None   # 向量余弦相似度（没走向量检索时为空），评测校准门槛用

    def to_json(self) -> dict:
        return asdict(self)


def _filters(layer: str, product: str, stage: str, doc_type: str, include_general: bool) -> tuple[str, dict]:
    sql, params = [], {}
    if layer:
        sql.append("i.layer = :layer")
        params["layer"] = layer
    if product:
        sql.append("i.product IN (:product, 'GEN')" if include_general and product != "GEN" else "i.product = :product")
        params["product"] = product
    if stage:
        sql.append("i.stage IN (:stage, '通用')")
        params["stage"] = stage
    if doc_type:
        sql.append("i.doc_type = :doc_type")
        params["doc_type"] = doc_type
    return "".join(f" AND {s}" for s in sql), params


_BASE = """FROM kb_chunks c
JOIN kb_versions v ON v.id = c.version_id AND v.status = 'published'
JOIN kb_items i ON i.current_version_id = v.id AND NOT i.retired"""


def _fulltext_pg(db: Session, q: str, where: str, params: dict) -> list[int]:
    rows = db.execute(text(f"""SELECT c.id {_BASE},
        CAST(replace(CAST(plainto_tsquery('zh', :q) AS text), '&', '|') AS tsquery) tq
        WHERE c.tsv @@ tq {where} ORDER BY ts_rank_cd(c.tsv, tq) DESC, c.id LIMIT {POOL}"""), {**params, "q": q})
    return [r[0] for r in rows]


def _vector_pg(db: Session, vec: list[float], model: str, where: str, params: dict) -> list[tuple[int, float]]:
    """返回 [(切片编号, 余弦相似度)]，按相似度从高到低。"""
    literal = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
    rows = db.execute(text(f"""SELECT c.id, 1 - (c.embedding <=> CAST(:vec AS vector)) AS sim {_BASE}
        WHERE c.embedding IS NOT NULL AND c.embedding_model = :model {where}
        ORDER BY c.embedding <=> CAST(:vec AS vector) LIMIT {POOL}"""), {**params, "vec": literal, "model": model})
    return [(r[0], float(r[1])) for r in rows]


def _fulltext_simple(db: Session, q: str, where: str, params: dict) -> list[int]:
    """SQLite 简化实现：查询切成二字词，按命中数排序。"""
    words = {m for w in re.findall(r"[一-龥]+|[A-Za-z0-9]{2,}", q)
             for m in ([w[i:i + 2] for i in range(len(w) - 1)] if re.match(r"[一-龥]", w) else [w.lower()])}
    rows = db.execute(text(f"SELECT c.id, c.heading, c.text {_BASE} WHERE 1=1 {where}"), params).all()
    scored = [(sum(1 for w in words if w in (h + t).lower()), cid) for cid, h, t in rows]
    return [cid for s, cid in sorted(scored, key=lambda x: (-x[0], x[1])) if s > 0][:POOL]


def _rrf(*rankings: list[int]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking, 1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    return scores


def search(db: Session, user: User, query: str, *, layer: str = "", product: str = "", stage: str = "",
           doc_type: str = "", limit: int = 8, include_general: bool = True, purpose: str = "kb.search",
           min_similarity: float | None = None) -> dict:
    query = query.strip()
    if not query:
        raise SearchError(422, "检索词不能为空")
    result = guard.check(query)
    if result.blocked:
        audit.record(db, actor=user.username, action=purpose, result="blocked", length=len(query),
                     categories=sorted({f.kind for f in result.findings}))
        raise SearchError(422, {"blocked": True, "summary": result.summary() + "，没有检索",
                                "findings": [{"kind": f.kind, "label": f.label, "excerpt": f.excerpt}
                                             for f in result.findings[:10]]})
    limit = max(1, min(limit, MAX_LIMIT))
    where, params = _filters(layer, product, stage, doc_type, include_general)
    postgres = db.get_bind().dialect.name == "postgresql"
    full = (_fulltext_pg if postgres else _fulltext_simple)(db, query, where, params)

    vec_ids: list[int] = []
    sims: dict[int, float] = {}
    vector_status = "not enabled"
    if postgres and gateway.embedding_enabled():
        try:
            emb = gateway.embed([query])
            usage.record(db, username=user.username, project_code="", level="embed", model=emb.model, status="ok",
                         stream=False, attempts=emb.attempts)
            floor = get_settings().kb_vector_min_similarity if min_similarity is None else min_similarity
            full_set_pre = set(full)
            # 全文也命中的切片照常参与融合；只有向量命中的，相似度够高才要
            nearest = _vector_pg(db, emb.vectors[0], emb.model, where, params)
            sims = dict(nearest)
            vec_ids = [cid for cid, sim in nearest if cid in full_set_pre or sim >= floor]
            vector_status = "ok"
        except gateway.GatewayError as e:         # 向量服务不可用：退回只用全文
            vector_status = f"unavailable: {str(e)[:60]}"

    scores = _rrf(full, vec_ids)
    full_set, vec_set = set(full), set(vec_ids)
    chunks = {c.id: c for c in db.scalars(select(KbChunk).where(KbChunk.id.in_(list(scores))))} if scores else {}
    best: dict[int, tuple[float, KbChunk]] = {}
    for cid, s in scores.items():
        c = chunks.get(cid)
        if c is not None and (c.version_id not in best or s > best[c.version_id][0]):
            best[c.version_id] = (s, c)

    hits: list[Hit] = []
    today = date.today()
    for version_id, (s, c) in sorted(best.items(), key=lambda kv: -kv[1][0]):
        v = db.get(KbVersion, version_id)
        item: KbItem = v.item
        if not review.can_view(db, user, item, v):
            continue
        h = Hit(doc_id=item.doc_id, version=v.version, title=v.title, layer=item.layer, domain=item.domain,
                product=item.product, stage=item.stage, doc_type=item.doc_type, heading=c.heading,
                snippet=c.text[:400], score=round(s, 5),
                matched="+".join(x for x, ok in (("全文", c.id in full_set), ("向量", c.id in vec_set)) if ok),
                review_date=v.review_date.isoformat() if v.review_date else None,
                similarity=round(sims[c.id], 4) if c.id in sims else None)
        if item.layer == "L4":
            h.ref_document, h.ref_version, h.ref_status = v.ref_document, v.ref_version, v.ref_status
            h.needs_update_check = bool(v.review_date and v.review_date < today)
            h.citation = f"【依据：{v.ref_document} {v.ref_version} {c.heading}｜{item.doc_id}】".replace("  ", " ")
        hits.append(h)
        if len(hits) >= limit:
            break

    audit.record(db, actor=user.username, action=purpose, result="allowed", length=len(query),
                 query_sha=audit.digest(query), results=len(hits), vector=vector_status.split(":")[0],
                 filters={k: val for k, val in (("layer", layer), ("product", product), ("stage", stage),
                                                ("doc_type", doc_type)) if val})
    return {"query_length": len(query), "vector": vector_status, "items": [h.to_json() for h in hits]}


def basis(db: Session, user: User, query: str, *, ref_document: str = "", limit: int = 5) -> dict:
    """依据查询：只查 L4 依据库。查不到时提示按公共规范第 3 节标注【依据待核实】。"""
    q = f"{ref_document} {query}".strip() if ref_document else query
    out = search(db, user, q, layer="L4", limit=limit, include_general=True, purpose="kb.basis",
                 min_similarity=get_settings().kb_basis_min_similarity)
    if ref_document:
        out["items"] = [h for h in out["items"] if ref_document in h["ref_document"]] or out["items"]
    if not out["items"]:
        out["note"] = "依据库中没有找到相关条目：请按公共规范第 3 节标注【依据待核实】，不得凭记忆补写条款号或原文"
    return out


# ---- 向量化（后台任务）----

def embed_version(db: Session, version_id: int) -> int:
    """给一个已发布版本中尚未向量化（或模型已更换）的切片生成向量。返回处理的切片数。"""
    s = get_settings()
    rows = db.execute(select(KbChunk.id, KbChunk.heading, KbChunk.text)
                      .where(KbChunk.version_id == version_id, KbChunk.embedding_model != s.embed_model)
                      .order_by(KbChunk.seq)).all()
    done = 0
    for i in range(0, len(rows), max(1, s.embed_batch_size)):
        batch = rows[i:i + s.embed_batch_size]
        emb = gateway.embed([f"{h}\n{t}" if h else t for _, h, t in batch])
        for (cid, _, _), vec in zip(batch, emb.vectors, strict=True):
            literal = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            db.execute(text("UPDATE kb_chunks SET embedding = CAST(:v AS vector), embedding_model = :m WHERE id = :id"),
                       {"v": literal, "m": emb.model, "id": cid})
        db.commit()
        done += len(batch)
    return done


def enqueue_embedding(db: Session, version_id: int, created_by: str) -> int | None:
    if db.get_bind().dialect.name != "postgresql" or not gateway.embedding_enabled():
        return None
    return jobs.enqueue(db, "kb.embed", {"version_id": version_id}, created_by=created_by, max_attempts=5)


def reembed_all(db: Session, created_by: str) -> int:
    ids = db.scalars(select(KbVersion.id).join(KbItem, KbItem.current_version_id == KbVersion.id)
                     .where(KbVersion.status == "published", KbItem.retired.is_(False))).all()
    return sum(1 for vid in ids if enqueue_embedding(db, vid, created_by))


@jobs.handler("kb.embed")
def _embed_job(payload: dict, ctx: jobs.JobContext) -> None:
    from ..core.db import SessionLocal

    with SessionLocal() as db:
        v = db.get(KbVersion, int(payload["version_id"]))
        if v is None or v.status != "published":
            return                                  # 已被替代或下线，不用再向量化
        try:
            embed_version(db, v.id)
        except gateway.BlockedError as e:
            raise jobs.JobError("切片含机密级内容，未向量化（需人工处理）", retry=False) from e
