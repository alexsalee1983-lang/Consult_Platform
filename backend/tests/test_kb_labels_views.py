"""知识条目多值标签的读取侧（三期 B15 第二步 c，方案 4.2 剩余与 8.2）：
知识地图和 /api/kb/stats 按标签计数（一个条目算进多个产品）；条目列表的产品过滤读标签；
审核查重按标签找同产品条目；labels.match / labels.coverage 给 A12 装配与就绪度用（只读、口径同检索）。"""
import secrets
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import kbmap, labels
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.main import app
from app.models import KbItem, KbVersion

from .conftest import create_user, logged_in_client
from .test_kb_search import _publish, people  # noqa: F401  （夹具）


def _multi(people, title: str, products: list[str], **kw):  # noqa: F811
    hit = _publish(people, title, f"## 要点\n\n{title}的要点。", **kw)
    with SessionLocal() as db:
        labels.set_products(db, db.get(KbItem, hit.item_id), products, actor="tester")
        db.commit()
    return hit


def test_matrix_counts_item_under_every_product(monkeypatch):
    monkeypatch.setattr(kbmap, "_catalog", lambda: None)
    monkeypatch.setattr(kbmap.meta, "PRODUCTS", {"SOC": "安全运营", "ISO": "体系建设", "GEN": "通用"})
    live = [(NS(id=1, product="SOC", stage="设计", tags=[]), NS()), (NS(id=2, product="ISO", stage="调研", tags=[]), NS())]
    rows = {r["code"]: r for r in kbmap.coverage_rows(live, {1: ["SOC", "ISO"]})}
    assert rows["SOC"]["total"] == 1 and rows["ISO"]["total"] == 2 and rows["ISO"]["cells"]["设计"] == 1
    rows = {r["code"]: r for r in kbmap.coverage_rows(live)}                 # 不传标签时按主产品列（旧口径）
    assert rows["ISO"]["total"] == 1


def test_list_filter_and_stats_read_labels(people):  # noqa: F811
    tag = secrets.token_hex(4)
    hit = _multi(people, f"多产品列表-{tag}", ["SOC", "ISO"])
    c = logged_in_client(app, create_user(("knowledge_expert",)))
    try:
        ids = [i["doc_id"] for i in c.get("/api/kb/items", params={"product": "ISO", "q": tag}).json()]
        assert hit.doc_id in ids
        assert hit.doc_id not in [i["doc_id"] for i in c.get("/api/kb/items",
                                                              params={"product": "DSM", "q": tag}).json()]
        s = c.get("/api/kb/stats").json()
        assert s["coverage"]["ISO"]["设计"] >= 1
        iso = next(r for r in s["matrix"] if r["code"] == "ISO")
        assert iso["cells"]["设计"] >= 1
    finally:
        c.close()


def test_match_products_industries_and_visibility(people):  # noqa: F811
    tag = secrets.token_hex(4)
    multi = _multi(people, f"装配多产品-{tag}", ["SOC", "ISO"])
    general = _publish(people, f"装配通用-{tag}", "## 通用\n\n通用要点。", product="GEN", stage="通用", layer="L1")
    hidden = _multi(people, f"装配受限-{tag}", ["ISO"], classification="restricted", visible=["admin"])
    other = _publish(people, f"装配其他-{tag}", "## 其他\n\n其他要点。", product="DSM")
    with SessionLocal() as db:
        u = db.merge(people["consultant"])
        got = {m.doc_id: m for m in labels.match(db, u, products=["ISO"])}
        assert multi.doc_id in got and general.doc_id in got                  # GEN 自动带上
        assert hidden.doc_id not in got and other.doc_id not in got           # 不可见、别的产品不返回
        m = got[multi.doc_id]
        assert m.matched["product"] == ["ISO"] and m.matched["industry"] == ["通用"] and m.layer == "L2"
        assert m.version >= 1
        assert multi.doc_id not in {x.doc_id for x in labels.match(db, u, products=["ISO"], industries=["银行"],
                                                                     layers=["L1"])}
        assert multi.doc_id in {x.doc_id for x in labels.match(db, u, products=["ISO"], industries=["银行"])}
        assert multi.doc_id not in {x.doc_id for x in labels.match(db, u, products=["ISO"], controls=["X-NOPE"])}
        exp = db.merge(people["expert"])
        assert hidden.doc_id in {x.doc_id for x in labels.match(db, exp, products=["ISO"])}
        cov = labels.coverage(db, products=["ISO", "DSM"], industries=["通用"], controls=["X-NOPE"])
        assert cov["product:ISO"] >= 2 and cov["product:DSM"] >= 1 and cov["control:X-NOPE"] == 0
        assert cov["industry:通用"] >= 4
        with pytest.raises(labels.meta.MetaError):
            labels.match(db, u, products=[])


def test_similar_items_by_product_label(people):  # noqa: F811
    with SessionLocal() as db:
        if db.get_bind(KbItem).dialect.name != "postgresql":
            pytest.skip("查重只在 PostgreSQL 上可用")
    tag = secrets.token_hex(4)
    multi = _multi(people, f"查重值守交接规程{tag}", ["SOC", "ISO"])
    with SessionLocal() as db:
        u = db.merge(people["author"])
        v = rv.new_item(db, u, ItemMeta(layer="L2", domain="OPS", product="ISO", doc_type="MTD", stage="设计",
                                        capabilities=["运营"]),
                        VersionContent(title=f"查重值守交接规程{tag}", body="## 值守\n\n交接。", source_type="public",
                                       source_detail="公开资料"))
        db.commit()
        v = db.scalar(select(KbVersion).where(KbVersion.id == v.id))
        assert multi.doc_id in [s["doc_id"] for s in rv.similar_items(db, v, limit=20)]
