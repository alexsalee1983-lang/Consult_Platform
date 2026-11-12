"""知识检索：只查已发布可见内容、过滤、依据查询与引用格式、机密级拦截、审计不记原文；
PostgreSQL 上：发布后后台向量化、全文 + 向量 RRF 融合、向量服务不可用时退回全文。"""
import hashlib
import json
import math
from types import SimpleNamespace
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select, text

from app.agents import tools
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import review as rv
from app.kb import search
from app.kb.items import ItemMeta, VersionContent
from app.models import AuditLog, KbItem, KbVersion, User  # noqa: F401
from app.services import gateway, jobs

from .conftest import create_user, on_postgres

TAG = "检索测试"


def _user(roles):
    name, *_ = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


@pytest.fixture(scope="module")
def people():
    return {k: _user(r) for k, r in {"author": ("knowledge_expert",), "expert": ("knowledge_expert",),
                                     "admin": ("knowledge_admin",), "consultant": ("consultant",)}.items()}


def _publish(people, title, body, *, layer="L2", product="SOC", doc_type="MTD", stage="设计", ref=None,
             classification="internal", visible=None) -> SimpleNamespace:
    with SessionLocal() as db:
        u = {k: db.merge(v) for k, v in people.items()}
        content = VersionContent(title=f"{TAG}-{title}", body=body, source_type="public", source_detail="公开资料")
        if ref:
            content.ref_document, content.ref_version, content.ref_status = ref
        v = rv.new_item(db, u["author"], ItemMeta(layer=layer, domain="OPS", product=product, doc_type=doc_type,
                                                  stage=stage, capabilities=["运营"], classification=classification,
                                                  visible_roles=visible or ["consultant", "admin"]), content)
        rv.submit(db, u["author"], v)
        rv.review(db, u["expert"], v, "content", "approve")
        rv.review(db, u["admin"], v, "compliance", "approve")
        db.refresh(v)
        assert v.status == "published"
        return SimpleNamespace(id=v.id, item_id=v.item_id, doc_id=v.item.doc_id)


def _search(people, who, q, **kw):
    with SessionLocal() as db:
        return search.search(db, db.merge(people[who]), q, **kw)


@pytest.fixture(scope="module")
def corpus(people):
    return {
        "duty": _publish(people, "值班交接规程", "## 交接班\n\n交接班时必须核对未闭环的一级事件和升级记录。"),
        "alert": _publish(people, "告警分级方法", "## 分级\n\n告警按业务影响分为四级，一级告警十五分钟内响应。"),
        "general": _publish(people, "事件响应通用流程", "## 响应\n\n一级事件立即升级值班负责人。", product="GEN",
                            stage="通用", layer="L1"),
        "basis": _publish(people, "安全管理中心要求解读", "## 8.1.5 集中管控\n\n应对安全事件进行集中分析和告警。",
                          layer="L4", product="GEN", doc_type="REF", stage="通用",
                          ref=("GB/T 22239", "2019", "current")),
        "restricted": _publish(people, "受限异议应答", "## 应答\n\n值班人力不足时的应答口径。", doc_type="OBJ",
                               classification="restricted", visible=["admin"]),
    }


def _ids(out):
    return [h["doc_id"] for h in out["items"]]


def test_search_finds_published_and_respects_visibility(people, corpus):
    out = _search(people, "consultant", "值班 一级事件 交接班")
    docs = _ids(out)
    assert corpus["duty"].doc_id in docs
    assert corpus["restricted"].doc_id not in docs                     # 受限条目顾问看不到
    assert corpus["restricted"].doc_id in _ids(_search(people, "expert", "值班 应答口径"))
    hit = next(h for h in out["items"] if h["doc_id"] == corpus["duty"].doc_id)
    assert hit["heading"] == "交接班" and "全文" in hit["matched"] and hit["title"].endswith("值班交接规程")


def test_drafts_and_retired_not_searchable(people, corpus):
    with SessionLocal() as db:
        u = db.merge(people["author"])
        rv.new_item(db, u, ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="TPL", stage="设计",
                                    capabilities=["运营"]),
                    VersionContent(title=f"{TAG}-草稿不可检索", body="## 草稿\n\n草稿里的独特词语甲乙丙丁。",
                                   source_type="public"))
    assert _ids(_search(people, "expert", "独特词语甲乙丙丁")) == []
    v = _publish(people, "将下线条目", "## 下线\n\n下线前的独特词语戊己庚辛。")
    assert v.doc_id in _ids(_search(people, "consultant", "独特词语戊己庚辛"))
    with SessionLocal() as db:
        rv.retire(db, db.merge(people["admin"]), db.get(KbItem, v.item_id), "测试")
    assert _ids(_search(people, "consultant", "独特词语戊己庚辛")) == []


def test_filters_include_general_knowledge(people, corpus):
    docs = _ids(_search(people, "consultant", "一级事件 升级", product="SOC", stage="设计", limit=20))
    assert corpus["general"].doc_id in docs                            # GEN / 通用阶段默认带上
    docs = _ids(_search(people, "consultant", "一级事件 升级", product="SOC", include_general=False, limit=20))
    assert corpus["general"].doc_id not in docs
    found = _ids(_search(people, "consultant", "告警", doc_type="MTD", layer="L2"))
    assert corpus["alert"].doc_id in found                                  # 其他测试也可能发布同类条目


def test_basis_lookup_citation_and_stale_flag(people, corpus):
    with SessionLocal() as db:
        out = search.basis(db, db.merge(people["consultant"]), "安全事件 集中分析")
    hit = next(h for h in out["items"] if h["doc_id"] == corpus["basis"].doc_id)   # 其他测试也可能发布依据条目
    assert hit["layer"] == "L4" and hit["citation"] == f"【依据：GB/T 22239 2019 8.1.5 集中管控｜{corpus['basis'].doc_id}】"
    assert hit["needs_update_check"] is False
    with SessionLocal() as db:
        db.get(KbVersion, corpus["basis"].id).review_date = date.today() - timedelta(days=1)
        db.commit()
        again = search.basis(db, db.merge(people["consultant"]), "集中分析", limit=20)["items"]
        assert next(h for h in again if h["doc_id"] == corpus["basis"].doc_id)["needs_update_check"] is True
        empty = search.basis(db, db.merge(people["consultant"]), "完全不存在的法规条款名称壬癸")
    assert empty["items"] == [] and "依据待核实" in empty["note"]


def test_query_guard_and_audit_without_text(people, corpus):
    with pytest.raises(search.SearchError) as e:
        _search(people, "consultant", "核心交换机 10.1.20.1 口令 Abc@12345")
    assert e.value.status == 422 and "10.1.20.1" not in json.dumps(e.value.detail, ensure_ascii=False)
    _search(people, "consultant", "值班交接里的独特问句子丑寅")
    with SessionLocal() as db:
        details = [a.detail for a in db.scalars(select(AuditLog).where(AuditLog.action.in_(("kb.search", "kb.basis"))))]
    assert details and all("独特问句子丑寅" not in d and "Abc@12345" not in d for d in details)


def test_tool_layer_kb_search_and_basis(people, corpus):
    with SessionLocal() as db:
        member = db.merge(people["consultant"])
        r = tools.invoke(db, member, "consultant-lead", "kb.search", {"query": "交接班 一级事件"}, "BANK-A")
        assert corpus["duty"].doc_id in _ids(r)
        r = tools.invoke(db, member, "compliance-basis", "basis.lookup", {"query": "集中分析"}, "BANK-A")
        assert r["items"][0]["layer"] == "L4"
        with pytest.raises(tools.ToolDenied):                               # 咨询师 Agent 没有依据查询授权
            tools.invoke(db, member, "consultant-lead", "basis.lookup", {"query": "集中分析"}, "BANK-A")


def test_api_endpoints(people, corpus):
    from fastapi.testclient import TestClient

    from app.main import app

    from .conftest import logged_in_client

    c = logged_in_client(app, create_user(("consultant",)))
    try:
        r = c.get("/api/kb/search", params={"q": "交接班", "product": "SOC"})
        assert r.status_code == 200 and corpus["duty"].doc_id in _ids(r.json())
        assert c.get("/api/kb/search", params={"q": ""}).status_code == 422
        assert c.get("/api/kb/basis", params={"q": "集中分析"}).json()["items"][0]["citation"].startswith("【依据：")
    finally:
        c.__exit__(None, None, None)
    with TestClient(app) as anon:
        assert anon.get("/api/kb/search", params={"q": "交接班"}).status_code == 401


# ---- 向量（PostgreSQL + 模拟向量服务）----

def _fake_vector(t: str) -> list[float]:
    """按二字词哈希到 512 维再归一化：有共同二字词的文本向量相近。"""
    v = [0.0] * gateway.EMBED_DIM
    for i in range(len(t) - 1):
        v[int(hashlib.md5(t[i:i + 2].encode()).hexdigest(), 16) % gateway.EMBED_DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class _Embedder:
    def __init__(self, fail=False, dim=gateway.EMBED_DIM):
        self.fail, self.dim, self.calls = fail, dim, 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": get_settings().embed_model}]})
        if self.fail:
            return httpx.Response(503)
        self.calls += 1
        inputs = json.loads(request.content)["input"]
        self.inputs = getattr(self, "inputs", []) + list(inputs)
        return httpx.Response(200, json={"data": [{"index": i, "embedding": _fake_vector(t)[:self.dim]}
                                                  for i, t in enumerate(inputs)]})


@pytest.fixture()
def embedder(monkeypatch):
    def use(**kw) -> _Embedder:
        e = _Embedder(**kw)
        monkeypatch.setattr(get_settings(), "embed_base_url", "http://embed.internal/v1")
        monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
        gateway.set_transport(httpx.MockTransport(e.handler))
        return e
    yield use
    gateway.set_transport(None)


def test_embed_guard_and_dimension(embedder):
    embedder()
    with pytest.raises(gateway.BlockedError):
        gateway.embed(["口令 Abc@12345"])
    embedder(dim=128)
    with pytest.raises(gateway.GatewayError, match="维度"):
        gateway.embed(["值班"])


@on_postgres
def test_publish_embeds_and_hybrid_search(people, embedder):
    e = embedder()
    v = _publish(people, "夜间值守向量测试", "## 夜间\n\n夜间值守由托管方初筛告警并电话升级。")
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=["kb.embed"]):
            pass
        n = db.execute(text("SELECT count(*) FROM kb_chunks WHERE version_id = :v AND embedding IS NOT NULL "
                            "AND embedding_model = :m"), {"v": v.id, "m": search.embed_marker(get_settings().embed_model)}).scalar()
    assert n >= 1 and e.calls >= 1
    out = _search(people, "consultant", "托管方 夜间值守")
    assert out["vector"] == "ok"
    hit = next(h for h in out["items"] if h["doc_id"] == v.doc_id)
    assert hit["matched"] == "全文+向量"


@on_postgres
def test_vector_service_down_falls_back_to_fulltext(people, embedder):
    embedder(fail=True)
    out = _search(people, "consultant", "托管方 夜间值守")
    assert out["vector"].startswith("unavailable") and out["items"]


@on_postgres
def test_unrelated_query_gets_no_vector_only_hits(people, corpus, embedder):
    """向量检索总会返回最近的切片；相似度下限保证毫不相关的问题查不到依据，照常提示【依据待核实】。"""
    embedder()
    with SessionLocal() as db:
        for vid in (corpus["basis"].id, corpus["duty"].id):
            search.embed_version(db, vid)
        member = db.merge(people["consultant"])
        out = search.basis(db, member, "量子纠缠遥感卫星编队")
        assert out["items"] == [] and "依据待核实" in out["note"]
        assert search.search(db, member, "zzzxxxqqq")["items"] == []
        hit = search.basis(db, member, "安全管理中心 安全事件 集中分析")["items"]   # 相关的照常命中
        assert corpus["basis"].doc_id in [h["doc_id"] for h in hit]



@on_postgres
def test_basis_drops_fulltext_hits_below_similarity(people, embedder):
    """测试 #27：只靠一个常见词撞上全文检索的弱相关依据，不能出现在依据查询里；普通检索不受影响。"""
    e = embedder()
    v = _publish(people, "监管报送时限要求", "## 第七条 报送\n\n重要事项应当在规定时限内向监管部门报送材料。", layer="L4",
                 product="GEN", doc_type="REF", stage="通用", ref=("某某监管办法", "2020", "current"))
    with SessionLocal() as db:
        search.embed_version(db, v.id)
        assert any("监管报送时限要求" in x for x in e.inputs)                 # 向量化文本带上了标题
        member = db.merge(people["consultant"])
        q = "外汇交易头寸需要报送哪些材料"          # 只靠“报送”“材料”撞上全文，向量相似度低
        assert v.doc_id not in [h["doc_id"] for h in search.basis(db, member, q)["items"]]
        loose = search.search(db, member, q, layer="L4", limit=20)["items"]
        assert v.doc_id in [h["doc_id"] for h in loose]                     # 普通检索照常按全文召回
        assert v.doc_id in [h["doc_id"] for h in search.basis(db, member, "监管部门 报送材料 时限")["items"]]


@on_postgres
def test_reembed_after_text_version_change(people, embedder):
    embedder()
    v = _publish(people, "向量版本测试", "## 内容\n\n向量化文本组织方式变化后应重新向量化。")
    with SessionLocal() as db:
        assert search.embed_version(db, v.id) >= 1
        assert search.embed_version(db, v.id) == 0                          # 已是当前版本，不重复
        db.execute(text("UPDATE kb_chunks SET embedding_model = :m WHERE version_id = :v"),
                   {"m": get_settings().embed_model, "v": v.id})             # 模拟旧版本（不带 #t2）
        db.commit()
        assert search.embed_version(db, v.id) >= 1


# ---- 重排（模拟重排服务；SQLite 和 PostgreSQL 都跑）----

class _Reranker:
    """按关键词打分：候选里含 good 中任一词给 0.9，否则 0.01。fail=True 模拟服务不可用。"""

    def __init__(self, good=(), fail=False):
        self.good, self.fail, self.calls, self.docs = tuple(good), fail, 0, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": get_settings().rerank_model}]})
        if self.fail:
            return httpx.Response(503)
        self.calls += 1
        body = json.loads(request.content)
        self.docs = body["documents"]
        return httpx.Response(200, json={"results": [
            {"index": i, "relevance_score": 0.9 if any(g in d for g in self.good) else 0.01}
            for i, d in enumerate(body["documents"])][::-1]})               # 乱序返回，网关按 index 对回去


@pytest.fixture()
def reranker(monkeypatch):
    def use(**kw) -> _Reranker:
        r = _Reranker(**kw)
        monkeypatch.setattr(get_settings(), "rerank_base_url", "http://rerank.internal/v1")
        monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
        gateway.set_transport(httpx.MockTransport(r.handler))
        return r
    yield use
    gateway.set_transport(None)


def test_cn_number():
    assert [search._cn_number(n) for n in (7, 10, 15, 21, 100, 105, 110, 230)] == \
        ["七", "十", "十五", "二十一", "一百", "一百零五", "一百一十", "二百三十"]


def test_gateway_rerank_guard_order_and_errors(reranker, monkeypatch):
    with pytest.raises(gateway.RerankDisabled):
        gateway.rerank("值班", ["交接"])
    r = reranker(good=("交接",))
    with pytest.raises(gateway.BlockedError):                               # 查询含机密级内容：不发出
        gateway.rerank("口令 Abc@12345", ["交接"])
    out = gateway.rerank("值班", ["无关内容", "交接班记录", "服务器口令 Abc@12345"])
    assert out.scores == [0.01, 0.9, 0.0]                                   # 乱序返回也按输入顺序对上
    assert all("Abc@12345" not in d for d in r.docs) and len(r.docs) == 2  # 含机密级的候选不发出、记 0 分
    monkeypatch.setattr(get_settings(), "kb_rerank_max_chars", 5)
    gateway.rerank("值班", ["一二三四五六七八"])
    assert r.docs == ["一二三四五"]                                          # 按字数上限截断
    assert gateway.rerank_availability() == "ok"
    reranker(fail=True)
    with pytest.raises(gateway.GatewayError):
        gateway.rerank("值班", ["交接"])


def test_rerank_reorders_search_results(people, reranker):
    a = _publish(people, "壬午演练甲", "## 演练\n\n壬午演练的准备工作清单。")
    b = _publish(people, "壬午演练乙", "## 演练\n\n壬午演练的复盘报告写法，复盘要点。")
    r = reranker(good=("复盘",))
    out = _search(people, "consultant", "壬午演练")
    assert out["rerank"] == "ok" and r.calls == 1
    ids = _ids(out)
    assert ids.index(b.doc_id) < ids.index(a.doc_id)
    top = out["items"][0]
    assert top["doc_id"] == b.doc_id and top["rerank_score"] == 0.9 and top["rank_before_rerank"] >= 1
    assert any("壬午演练乙" in d for d in r.docs)                            # 候选带上了标题


def test_plain_search_drops_near_zero_rerank(people, reranker, monkeypatch):
    v = _publish(people, "癸未巡检记录", "## 巡检\n\n癸未机房巡检的记录要点。")
    reranker(good=("不会出现的词",))                                         # 全部 0.01 分
    assert v.doc_id in _ids(_search(people, "consultant", "癸未巡检"))      # 默认下限 0.005：0.01 保留
    monkeypatch.setattr(get_settings(), "kb_search_min_rerank", 0.05)
    out = _search(people, "consultant", "癸未巡检")
    assert out["rerank"] == "ok" and v.doc_id not in _ids(out)            # 低于下限：挡掉


def test_rerank_down_falls_back_to_rrf(people, reranker):
    _publish(people, "癸未巡检", "## 巡检\n\n癸未巡检的检查表。")
    reranker(fail=True)
    out = _search(people, "consultant", "癸未巡检")
    assert out["rerank"].startswith("unavailable") and out["items"]
    assert out["items"][0]["rerank_score"] is None


def test_rerank_status_no_candidates(people, reranker):
    """已启用重排但没有任何候选：状态是 no candidates，不是 not enabled，也不调用重排服务。"""
    r = reranker()
    out = _search(people, "consultant", "壬癸子丑寅卯辰")
    assert out["items"] == [] and out["rerank"] == "no candidates" and r.calls == 0


def test_basis_uses_rerank_threshold(people, reranker, monkeypatch):
    v = _publish(people, "甲申报送办法", "## 第七条 报送\n\n甲申事项应当在五日内向主管部门报送。", layer="L4",
                 product="GEN", doc_type="REF", stage="通用", ref=("甲申监管办法", "2020", "current"))
    reranker(good=("五日内",))
    with SessionLocal() as db:
        member = db.merge(people["consultant"])
        hit = search.basis(db, member, "甲申事项 报送时限")
        assert hit["rerank"] == "ok" and v.doc_id in [h["doc_id"] for h in hit["items"]]
    reranker(good=("不会出现的词",))                                         # 重排认为不相关：挡掉
    with SessionLocal() as db:
        member = db.merge(people["consultant"])
        out = search.basis(db, member, "甲申事项 报送时限")
        assert v.doc_id not in [h["doc_id"] for h in out["items"]]
        loose = search.basis(db, member, "甲申事项 报送时限", min_rerank=0.0)   # 评测取分数用
        assert v.doc_id in [h["doc_id"] for h in loose["items"]]
        monkeypatch.setattr(get_settings(), "kb_basis_min_rerank", 0.005)
        assert v.doc_id in [h["doc_id"] for h in search.basis(db, member, "甲申事项 报送时限")["items"]]


def test_exact_standard_and_article_match(people, corpus, reranker):
    law = _publish(people, "乙酉条例解读", "## 第二十一条 值守\n\n乙酉单位应当安排专人值守。\n\n## 第二十二条 记录\n\n应当留存值守记录。",
                   layer="L4", product="GEN", doc_type="REF", stage="通用",
                   ref=("中华人民共和国乙酉安全条例", "2021", "current"))
    reranker(good=("不会出现的词",))                                         # 重排全给低分：只有精确匹配能出结果
    with SessionLocal() as db:
        member = db.merge(people["consultant"])
        out = search.basis(db, member, "GB/T 22239 8.1.5 有什么要求")
        exact = [h for h in out["items"] if "精确" in h["matched"]]             # 其他测试也可能发布同一条款
        assert exact and out["items"][:len(exact)] == exact and corpus["basis"].doc_id in [h["doc_id"] for h in exact]
        assert all(h["heading"].startswith("8.1.5") for h in exact)
        art = search.basis(db, member, "乙酉安全条例第21条怎么规定")["items"]  # 阿拉伯数字条号、省略“中华人民共和国”
        assert art and art[0]["doc_id"] == law.doc_id and art[0]["heading"].startswith("第二十一条")
        only_doc = search.basis(db, member, "GB/T22239 里讲了什么")["items"]   # 只点名文件：限定在该文件内
        assert only_doc and all(h["ref_document"] == "GB/T 22239" for h in only_doc)
        assert search.basis(db, member, "GB/T 99999 8.1.5")["items"] == []   # 库里没有的标准：照常走门槛


def test_exact_clause_in_plain_search_does_not_hide_other_layers(people, corpus, reranker):
    reranker(good=("集中",))
    out = _search(people, "consultant", "GB/T 22239 8.1.5 集中管控 告警")
    assert "精确" in out["items"][0]["matched"]
    assert any(h["layer"] != "L4" for h in out["items"])                  # 普通检索不只限定在该标准内
