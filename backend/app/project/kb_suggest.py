"""四期 A32③：编辑器知识侧栏（平台规划刷新设计第十二节）。

按当前章节推荐相关条款、公司做法和黄金样例，顾问一键插入、带出处：
1. 检索词 = 交付件名称 + 章节标题（写入时已过机密级检测）+ 顾问补充的词（search 里再过机密级检测，命中 422）；
   复用 app/kb/search.py（全文 + 向量 + 重排，只查已发布、未下线、调用人看得到的现行版本），不另建索引。
   产品取项目配置快照（读不出来不按产品过滤），本产品与通用 GEN 都推荐。
2. 三组：条款 = L4 依据；黄金样例 = 交付物类型 GLD；公司做法 = 知识类型 practice。
3. 每条给 insert：摘录 + 出处，依据用检索给出的引用格式【依据：…｜编号】，其余用 [编号]；插入由前端放到光标处，
   保存仍走交付件写入接口（来源核对、机密级检测、签署门禁照旧）。
只读：不写知识库（红线 6：交付侧不能写知识库）、不建表；审计由 search 记（purpose kb.suggest，只记长度和摘要哈希）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..kb import search
from ..models import Deliverable, DeliverableSection, KbItem, Project, User

PURPOSE = "kb.suggest"
PER_GROUP = 3
EXCERPT_CHARS = 200
MAX_EXTRA = 100


def _product(db: Session, project: Project) -> str:
    from ..products import config
    from . import configs

    try:
        return str(configs.snapshot(db, project).get("product") or "")
    except config.CatalogError:
        return ""


def _excerpt(snippet: str) -> str:
    lines = [x.strip() for x in snippet.splitlines() if x.strip() and not x.lstrip().startswith("#")]
    text = " ".join(lines)
    return text[:EXCERPT_CHARS].rstrip() + ("…" if len(text) > EXCERPT_CHARS else "")


def _item(h: dict) -> dict:
    excerpt = _excerpt(h.get("snippet", ""))
    source = h["citation"] if h.get("layer") == "L4" and h.get("citation") else f"[{h['doc_id']}]"
    return {"doc_id": h["doc_id"], "title": h["title"], "heading": h.get("heading", ""), "layer": h["layer"],
            "doc_type": h.get("doc_type", ""), "excerpt": excerpt, "citation": source,
            "authority_label": h.get("authority_label", ""), "insert": f"{excerpt}{source}"}


def suggest(db: Session, user: User, project: Project, d: Deliverable, s: DeliverableSection, extra: str = "") -> dict:
    query = " ".join(x for x in (d.title, s.title, (extra or "").strip()[:MAX_EXTRA]) if x)
    product = _product(db, project)

    def run(**kw) -> list[dict]:
        return search.search(db, user, query, product=product, purpose=PURPOSE, **kw)["items"]

    clauses = run(layer="L4", limit=PER_GROUP)
    golden = run(doc_type="GLD", limit=PER_GROUP)
    pool = run(limit=search.MAX_LIMIT)
    kinds = dict(db.execute(select(KbItem.doc_id, KbItem.knowledge_kind)
                            .where(KbItem.doc_id.in_({h["doc_id"] for h in pool}))).all()) if pool else {}
    practices = [h for h in pool if kinds.get(h["doc_id"]) == "practice" and h["layer"] != "L4"
                 and h.get("doc_type") != "GLD"][:PER_GROUP]
    return {"query_length": len(query), "product": product,
            "clauses": [_item(h) for h in clauses], "practices": [_item(h) for h in practices],
            "golden": [_item(h) for h in golden]}
