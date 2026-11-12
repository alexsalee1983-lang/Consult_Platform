"""知识检索：全文 + 向量混合检索（RRF 融合），只查已发布、未下线的现行版本。

- 全文：PostgreSQL 中文全文索引（zh），查询分词后任一词命中即可，按 ts_rank_cd 排序。
- 向量：查询经网关 embed 转成向量，按余弦距离在 HNSW 索引上找最近的切片；向量服务没启用或不可用时只用全文。
- 融合：两路各取前 40 个切片，按 RRF（1 / (60 + 名次)）相加，同一条目只保留得分最高的切片。
- 相似度下限：只有向量命中、全文没命中的切片，余弦相似度低于下限就丢掉（向量检索总会返回“最近的”结果）。
  依据查询更严（strict）：**所有结果**（含全文命中的）都要达到 KB_BASIS_MIN_SIMILARITY——只靠一个常见词
  撞上全文检索的弱相关条款也不能当依据（测试 #27：“外汇交易头寸的监管上限”因“监管”一词召回了无关依据）。
  向量服务不可用时无法判断相似度，依据查询照常只用全文，结果里 vector 字段会注明。
- 过滤：层级、服务产品（默认同时包含 GEN 通用知识）、阶段、类型；受限条目按可见角色（与 app/kb/review.py 一致）。
- 查询先过机密级检测；审计只记查询长度和摘要哈希，不记原文。
- 重排（决策记录 2026-11-11）：融合后的前 KB_RERANK_CANDIDATES 个切片经网关 rerank 用交叉编码模型重新打分，
  按重排分数排序；依据查询的“够不够格”改用重排分数门槛 KB_BASIS_MIN_RERANK（取代上面的余弦相似度下限），
  普通检索也有一个低得多的下限 KB_SEARCH_MIN_RERANK，只挡掉趋近 0 分的无关结果。
  重排服务没启用或不可用时退回 RRF 排序和余弦相似度门槛，结果里 rerank 字段会注明。
- 标准号 / 条款号：查询写了标准号（GB/T 22239 8.1.4）或法规条号（网络安全法第二十一条）时先按元数据精确匹配，
  文件和条款都对上的排最前、不受门槛限制；依据查询只对上文件的，结果限定在该文件内。
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
# 向量化时片段文本的组织方式；改了组织方式就升版本号，kb-reembed 会把旧向量全部重算。
# t2：标题 + 摘要 + 章节 + 正文（短条目只有正文时彼此区分度太低，测试 #27 评测发现）
EMBED_TEXT_VERSION = "t2"
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
    rerank_score: float | None = None  # 重排分数 0～1（没重排时为空）；重排时 score 等于它，否则是 RRF 分数
    rank_before_rerank: int | None = None   # 重排前在 RRF 候选里的名次，评测看正确条目有没有进前 N

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


def embed_marker(model: str) -> str:
    """kb_chunks.embedding_model 里记的值：模型名 + 片段文本组织方式版本。"""
    return f"{model}#{EMBED_TEXT_VERSION}"


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _similarity_of(db: Session, vec: list[float], model: str, ids: list[int]) -> dict[int, float]:
    """指定切片与查询向量的余弦相似度（全文命中但不在向量前 40 名里的切片用）。"""
    if not ids:
        return {}
    rows = db.execute(text("""SELECT c.id, 1 - (c.embedding <=> CAST(:vec AS vector)) FROM kb_chunks c
        WHERE c.id = ANY(:ids) AND c.embedding IS NOT NULL AND c.embedding_model = :model"""),
                      {"vec": _vec_literal(vec), "ids": ids, "model": embed_marker(model)})
    return {r[0]: float(r[1]) for r in rows}


def _vector_pg(db: Session, vec: list[float], model: str, where: str, params: dict) -> list[tuple[int, float]]:
    """返回 [(切片编号, 余弦相似度)]，按相似度从高到低。"""
    rows = db.execute(text(f"""SELECT c.id, 1 - (c.embedding <=> CAST(:vec AS vector)) AS sim {_BASE}
        WHERE c.embedding IS NOT NULL AND c.embedding_model = :model {where}
        ORDER BY c.embedding <=> CAST(:vec AS vector) LIMIT {POOL}"""),
                      {**params, "vec": _vec_literal(vec), "model": embed_marker(model)})
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


# ---- 标准号 / 条款号精确匹配（依据库 L4）----
# 查询里写了标准号（如“GB/T 22239 8.1.4”）或法规条号（如“网络安全法第二十一条”）时，先按条目元数据精确匹配：
# 文件和条款都对上的片段排在最前，不受重排门槛限制；依据查询只对上文件的，结果限定在该文件内（再按重排排序）。
_STD_RE = re.compile(r"(GB/T|GB/Z|GB|JR/T|YD/T|GM/T|GA/T|DL/T|JT/T|WS/T|SJ/T)\s*(\d{3,6}(?:\.\d+)?)", re.I)
_CLAUSE_RE = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d{1,3}){1,4})(?![\d.])")
_ARTICLE_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百零〇两]{1,6})\s*条")
_CN_DIGITS = "零一二三四五六七八九"


def _cn_number(n: int) -> str:
    """1～999 的阿拉伯数字转成条号常用的中文写法（21 → 二十一，105 → 一百零五）。"""
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    if n < 100:
        return _CN_DIGITS[n // 10] + "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    rest = n % 100
    if rest == 0:
        tail = ""
    elif rest < 10:
        tail = "零" + _CN_DIGITS[rest]
    elif rest < 20:
        tail = "一" + _cn_number(rest)
    else:
        tail = _cn_number(rest)
    return _CN_DIGITS[n // 100] + "百" + tail


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").upper()


@dataclass
class _Exact:
    doc_chunks: set[int]          # 文件对上的切片
    pinned: list[int]             # 文件和条款都对上的切片（排最前）


def _exact_match(db: Session, query: str, where: str, params: dict) -> _Exact | None:
    codes = {_norm(m.group(1) + m.group(2)) for m in _STD_RE.finditer(query)}
    clauses = set(_CLAUSE_RE.findall(_STD_RE.sub(" ", query)))
    articles = {_cn_number(int(a)) if a.isdigit() and 0 < int(a) < 1000 else a for a in _ARTICLE_RE.findall(query)}
    if not codes and not articles:
        return None
    rows = db.execute(text(f"SELECT c.id, c.heading, v.ref_document {_BASE} WHERE i.layer = 'L4' {where} "
                           "ORDER BY c.id"), params).all()
    q = _norm(query)

    def doc_hit(ref: str) -> bool:
        r = _norm(ref)
        if not r:
            return False
        if codes:
            return any(code in r for code in codes)
        short = r.removeprefix("中华人民共和国")       # 法规条号：查询里要写到法规名（可省略“中华人民共和国”）
        return len(short) >= 4 and short in q

    docs = [(cid, heading or "") for cid, heading, ref in rows if doc_hit(ref)]
    if not docs:
        return None

    def clause_hit(heading: str) -> bool:
        h = _norm(heading)
        if any(re.match(re.escape(c) + r"(?![\d.])", h) for c in clauses):
            return True
        return any(f"第{a}条" in h for a in articles)

    pinned = [cid for cid, heading in docs if (clauses or articles) and clause_hit(heading)]
    return _Exact(doc_chunks={cid for cid, _ in docs}, pinned=pinned)


def _rerank_text(c: KbChunk, title: str) -> str:
    return "\n".join(x for x in (title, c.heading, c.text) if x)


def search(db: Session, user: User, query: str, *, layer: str = "", product: str = "", stage: str = "",
           doc_type: str = "", limit: int = 8, include_general: bool = True, purpose: str = "kb.search",
           min_similarity: float | None = None, strict: bool = False, min_rerank: float | None = None) -> dict:
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
    s = get_settings()
    limit = max(1, min(limit, MAX_LIMIT))
    where, params = _filters(layer, product, stage, doc_type, include_general)
    postgres = db.get_bind().dialect.name == "postgresql"
    full = (_fulltext_pg if postgres else _fulltext_simple)(db, query, where, params)

    # 两套候选：*_loose 给重排用（依据查询不按余弦相似度预先过滤，够不够格由重排分数判断）；
    # *_strict 是不启用重排或重排不可用时的老办法（余弦相似度门槛）。普通检索两套相同。
    vec_loose: list[int] = []
    full_strict: list[int] = full
    vec_strict: list[int] = []
    sims: dict[int, float] = {}
    vector_status = "not enabled"
    if postgres and gateway.embedding_enabled():
        try:
            emb = gateway.embed([query])
            usage.record(db, username=user.username, project_code="", level="embed", model=emb.model, status="ok",
                         stream=False, attempts=emb.attempts)
            floor = s.kb_vector_min_similarity if min_similarity is None else min_similarity
            full_set_pre = set(full)
            nearest = _vector_pg(db, emb.vectors[0], emb.model, where, params)
            sims = dict(nearest)
            # 全文也命中的切片照常参与融合；只有向量命中的，相似度够高才要
            vec_strict = [cid for cid, sim in nearest if cid in full_set_pre or sim >= floor]
            vec_loose = vec_strict
            if strict:   # 依据查询（老办法）：全文命中的也要达到下限
                sims.update(_similarity_of(db, emb.vectors[0], emb.model, [c for c in full if c not in sims]))
                full_strict = [cid for cid in full if sims.get(cid, 0.0) >= floor]
                vec_strict = [cid for cid in vec_strict if sims.get(cid, 0.0) >= floor]
                vec_loose = [cid for cid, _ in nearest]
            vector_status = "ok"
        except gateway.GatewayError as e:         # 向量服务不可用：退回只用全文
            vector_status = f"unavailable: {str(e)[:60]}"

    exact = _exact_match(db, query, where, params)
    pinned = set(exact.pinned) if exact else set()

    def candidates(full_ids: list[int], vec_ids: list[int]) -> dict[int, float]:
        scores = _rrf(full_ids, vec_ids)
        if exact and strict:   # 依据查询点名了某份标准 / 法规：只在它里面找
            scores = {cid: v for cid, v in scores.items() if cid in exact.doc_chunks}
            for cid in sorted(exact.doc_chunks)[:POOL]:
                scores.setdefault(cid, 0.0)
        for cid in exact.pinned if exact else ():   # 点到的条款一律加进来（普通检索也排最前）
            scores.setdefault(cid, 0.0)
        return scores

    scores = candidates(full, vec_loose)
    chunks: dict[int, KbChunk] = {}
    rr: dict[int, float] = {}
    rank_before: dict[int, int] = {}
    rerank_status = "not enabled"
    if not scores and gateway.rerank_enabled():
        rerank_status = "no candidates"           # 已启用但没有候选可重排（和“未启用”区分开）
    if scores and gateway.rerank_enabled():
        order = sorted(scores, key=lambda cid: (-scores[cid], cid))
        top = order[:max(1, s.kb_rerank_candidates)]
        chunks = {c.id: c for c in db.scalars(select(KbChunk).where(KbChunk.id.in_(top)))}
        titles = dict(db.execute(select(KbVersion.id, KbVersion.title).where(
            KbVersion.id.in_({c.version_id for c in chunks.values()}))).all())
        top = [cid for cid in top if cid in chunks]
        try:
            r = gateway.rerank(query, [_rerank_text(chunks[cid], titles.get(chunks[cid].version_id, "")) for cid in top])
            usage.record(db, username=user.username, project_code="", level="rerank", model=r.model, status="ok",
                         stream=False, attempts=r.attempts)
            rr = dict(zip(top, r.scores, strict=True))
            rank_before = {cid: n for n, cid in enumerate(order, 1)}
            rerank_status = "ok"
        except gateway.GatewayError as e:          # 重排不可用：退回 RRF 排序和余弦相似度门槛
            rerank_status = f"unavailable: {str(e)[:60]}"

    if rerank_status == "ok":
        # 重排分数要达到门槛：依据查询用 KB_BASIS_MIN_RERANK，普通检索用更低的 KB_SEARCH_MIN_RERANK（只挡掉趋近 0 分的
        # 无关结果，测试 #34：S011 返回了 0.0003～0.0008 分的条目）。点到条款的不受限；依据查询点名了文件但没点到条款的，
        # 在该文件内按重排排序、不受门槛限制。
        floor = min_rerank if min_rerank is not None else (s.kb_basis_min_rerank if strict else s.kb_search_min_rerank)
        keep = [cid for cid in rr if cid in pinned or (strict and exact is not None and not pinned) or rr[cid] >= floor]
        keep += [cid for cid in pinned if cid not in rr]   # 点到的条款没进重排候选时也保留
        ordered = sorted(keep, key=lambda cid: (cid not in pinned, -rr.get(cid, 0.0), -scores[cid], cid))
        vec_shown = vec_loose
    else:
        if strict:
            scores = candidates(full_strict, vec_strict)
        ordered = sorted(scores, key=lambda cid: (cid not in pinned, -scores[cid], cid))
        vec_shown = vec_strict if strict else vec_loose

    full_set, vec_set = set(full), set(vec_shown)
    missing = [cid for cid in ordered if cid not in chunks]
    if missing:
        chunks.update({c.id: c for c in db.scalars(select(KbChunk).where(KbChunk.id.in_(missing)))})
    seen: set[int] = set()
    hits: list[Hit] = []
    today = date.today()
    for cid in ordered:
        c = chunks.get(cid)
        if c is None or c.version_id in seen:
            continue
        seen.add(c.version_id)                       # 同一条目只保留排得最靠前的切片
        v = db.get(KbVersion, c.version_id)
        item: KbItem = v.item
        if not review.can_view(db, user, item, v):
            continue
        how = [x for x, ok in (("精确", cid in pinned), ("全文", cid in full_set), ("向量", cid in vec_set)) if ok]
        h = Hit(doc_id=item.doc_id, version=v.version, title=v.title, layer=item.layer, domain=item.domain,
                product=item.product, stage=item.stage, doc_type=item.doc_type, heading=c.heading,
                snippet=c.text[:400], score=round(rr.get(cid, scores.get(cid, 0.0)), 5),
                matched="+".join(how) or "标准号",
                review_date=v.review_date.isoformat() if v.review_date else None,
                similarity=round(sims[cid], 4) if cid in sims else None,
                rerank_score=round(rr[cid], 4) if cid in rr else None,
                rank_before_rerank=rank_before.get(cid))
        if item.layer == "L4":
            h.ref_document, h.ref_version, h.ref_status = v.ref_document, v.ref_version, v.ref_status
            h.needs_update_check = bool(v.review_date and v.review_date < today)
            h.citation = f"【依据：{v.ref_document} {v.ref_version} {c.heading}｜{item.doc_id}】".replace("  ", " ")
        hits.append(h)
        if len(hits) >= limit:
            break

    audit.record(db, actor=user.username, action=purpose, result="allowed", length=len(query),
                 query_sha=audit.digest(query), results=len(hits), vector=vector_status.split(":")[0],
                 rerank=rerank_status.split(":")[0],
                 filters={k: val for k, val in (("layer", layer), ("product", product), ("stage", stage),
                                                ("doc_type", doc_type)) if val})
    return {"query_length": len(query), "vector": vector_status, "rerank": rerank_status,
            "items": [h.to_json() for h in hits]}


def basis(db: Session, user: User, query: str, *, ref_document: str = "", limit: int = 5,
          min_rerank: float | None = None) -> dict:
    """依据查询：只查 L4 依据库。查不到时提示按公共规范第 3 节标注【依据待核实】。"""
    q = f"{ref_document} {query}".strip() if ref_document else query
    out = search(db, user, q, layer="L4", limit=limit, include_general=True, purpose="kb.basis",
                 min_similarity=get_settings().kb_basis_min_similarity, strict=True, min_rerank=min_rerank)
    if ref_document:
        out["items"] = [h for h in out["items"] if ref_document in h["ref_document"]] or out["items"]
    if not out["items"]:
        out["note"] = "依据库中没有找到相关条目：请按公共规范第 3 节标注【依据待核实】，不得凭记忆补写条款号或原文"
    return out


# ---- 向量化（后台任务）----

def embed_version(db: Session, version_id: int) -> int:
    """给一个已发布版本中尚未向量化（或模型、文本组织方式已更换）的切片生成向量。返回处理的切片数。
    向量化的文本 = 标题 + 摘要 + 章节标题 + 正文，让短条目之间也有足够的区分度。"""
    s = get_settings()
    marker = embed_marker(s.embed_model)
    v = db.get(KbVersion, version_id)
    prefix = "\n".join(x for x in ((v.title, v.summary) if v else ()) if x)
    rows = db.execute(select(KbChunk.id, KbChunk.heading, KbChunk.text)
                      .where(KbChunk.version_id == version_id, KbChunk.embedding_model != marker)
                      .order_by(KbChunk.seq)).all()
    done = 0
    for i in range(0, len(rows), max(1, s.embed_batch_size)):
        batch = rows[i:i + s.embed_batch_size]
        emb = gateway.embed(["\n".join(x for x in (prefix, h, t) if x) for _, h, t in batch])
        for (cid, _, _), vec in zip(batch, emb.vectors, strict=True):
            db.execute(text("UPDATE kb_chunks SET embedding = CAST(:v AS vector), embedding_model = :m WHERE id = :id"),
                       {"v": _vec_literal(vec), "m": embed_marker(emb.model), "id": cid})
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
