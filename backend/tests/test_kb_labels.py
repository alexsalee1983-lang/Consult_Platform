"""知识条目多值标签（三期 B15 第二步 b，知识区迁移 k0011，app/kb/labels.py）：
产品、行业、控制项、能力域、一级目录、检索标签统一进 kb_item_labels；一个条目可以挂多个产品；
GEN 和具体产品互斥、“通用”和具体行业互斥、行业取值必须在 meta.INDUSTRIES 里；
检索按产品过滤读标签表（多产品条目在每个产品下都能检到）；条目文件 product 接受字符串或列表；
迁移 k0011 从旧列回填（source=legacy），回填后自检，downgrade 回写旧列。"""
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text

from app.core.db import SessionLocal
from app.core.migrate import alembic_config, upgrade_to_head
from app.kb import importer, labels, meta
from app.kb.files import format_entry, parse_entry
from app.kb.items import ItemMeta
from app.main import app
from app.models import AuditLog, KbItem, KbItemLabel, User

from .conftest import create_user, logged_in_client
from .test_kb_import_eval import _entry
from .test_kb_search import _ids, _publish, _search, people  # noqa: F401  （夹具）


def _labels(item_id: int) -> set[tuple[str, str]]:
    with SessionLocal() as db:
        return {(r.kind, r.value) for r in db.scalars(select(KbItemLabel).where(KbItemLabel.item_id == item_id))}


# ---- 规则 ----

def test_products_primary_first_and_gen_exclusive():
    m = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="MTD", stage="设计", capabilities=["运营"],
                 products=["ISO", "SOC", " ISO "])
    m.check()
    assert m.products == ["SOC", "ISO"] and m.product == "SOC"
    with pytest.raises(meta.MetaError, match="GEN"):
        ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="MTD", stage="设计", capabilities=["运营"],
                 products=["GEN"]).check()
    with pytest.raises(meta.MetaError, match="服务产品"):
        ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="MTD", stage="设计", capabilities=["运营"],
                 products=["XYZ"]).check()


def test_industries_table_and_general_exclusive():
    ok = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="MTD", stage="设计", capabilities=["运营"],
                  industries=["银行", " 电力", "银行"])
    ok.check()
    assert ok.industries == ["银行", "电力"]
    with pytest.raises(meta.MetaError, match="通用"):
        ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="MTD", stage="设计", capabilities=["运营"],
                 industries=["通用", "银行"]).check()
    with pytest.raises(meta.MetaError, match="行业"):
        ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="MTD", stage="设计", capabilities=["运营"],
                 industries=["火星"]).check()


def test_entry_product_accepts_list_and_roundtrips():
    e = parse_entry(_entry(key="b15-labels-list", product="[SOC, ISO]"))
    assert e.meta.product == "SOC" and e.meta.products == ["SOC", "ISO"]
    again = parse_entry(format_entry(e))
    assert again.meta.product == "SOC" and again.meta.products == ["SOC", "ISO"]
    single = parse_entry(_entry(key="b15-labels-one"))
    assert single.meta.products == ["SOC"] and "products" not in format_entry(single)
    assert "product: SOC\n" in format_entry(single)


# ---- 写入与检索 ----

def test_new_item_writes_all_label_kinds(people):  # noqa: F811
    hit = _publish(people, f"标签写入-{secrets.token_hex(2)}", "## 标签\n\n写入测试。")
    got = _labels(hit.item_id)
    assert ("product", "SOC") in got and ("industry", "通用") in got
    assert ("capability", "运营") in got and ("domain", "OPS") in got
    with SessionLocal() as db:
        assert {r.source for r in db.scalars(select(KbItemLabel).where(KbItemLabel.item_id == hit.item_id))} \
            == {"manual"}
        assert labels.products_of(db, [hit.item_id]) == {hit.item_id: ["SOC"]}


def test_item_detail_lists_products_and_meta_has_industries(people):  # noqa: F811
    hit = _publish(people, f"标签接口-{secrets.token_hex(2)}", "## 接口\n\n详情带产品。")
    with SessionLocal() as db:
        labels.set_products(db, db.get(KbItem, hit.item_id), ["SOC", "NSG"], actor="tester")
        db.commit()
    c = logged_in_client(app, create_user(("consultant",)))
    try:
        assert c.get(f"/api/kb/items/{hit.doc_id}").json()["products"] == ["SOC", "NSG"]
        assert "银行" in c.get("/api/kb/meta").json()["industries"]
    finally:
        c.close()


def test_multi_product_item_found_under_each_product(people):  # noqa: F811
    tag = secrets.token_hex(2)
    q = f"多产品值守交接{tag}"
    hit = _publish(people, f"多产品-{tag}", f"## 值守\n\n{q}要点。")
    with SessionLocal() as db:
        labels.set_products(db, db.get(KbItem, hit.item_id), ["SOC", "ISO"], actor="tester")
        db.commit()
    assert hit.doc_id in _ids(_search(people, "consultant", q, product="ISO", include_general=False, limit=20))
    assert hit.doc_id in _ids(_search(people, "consultant", q, product="SOC", include_general=False, limit=20))
    assert hit.doc_id not in _ids(_search(people, "consultant", q, product="DSM", include_general=False, limit=20))
    out = _search(people, "consultant", q, product="ISO", limit=20)
    assert {h["doc_id"]: h["scope"] for h in out["items"]}[hit.doc_id] == "product"
    with SessionLocal() as db:
        assert db.get(KbItem, hit.item_id).product == "SOC"          # 主产品列不变（只给回滚和旧报表）
        a = db.scalars(select(AuditLog).where(AuditLog.action == "kb.labels")
                       .order_by(AuditLog.id.desc())).first()
        assert a is not None and hit.doc_id in a.detail and "ISO" in a.detail


def test_reimport_updates_products_and_industries():
    name, *_ = create_user(("knowledge_expert",))
    key = f"b15-lab-{secrets.token_hex(3)}"
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == name))
        assert importer.import_entry(db, u, parse_entry(_entry(key=key))).action == "created"
        item = db.scalar(select(KbItem).where(KbItem.import_key == key))
        item.versions[-1].status = "published"
        db.commit()
        r = importer.import_entry(db, u, parse_entry(_entry(key=key, product="[SOC, DSM]", industries="[银行]")))
        assert r.action == "unchanged" and "产品" in r.message
        item_id = item.id
    got = _labels(item_id)
    assert {v for k, v in got if k == "product"} == {"SOC", "DSM"}
    assert {v for k, v in got if k == "industry"} == {"银行"}


# ---- 迁移 k0011 ----

def _insert_item(conn, doc_id: str, **cols) -> int:
    now = datetime.now(timezone.utc)
    row = {"doc_id": doc_id, "layer": "L2", "domain": "OPS", "product": "SOC", "doc_type": "MTD", "stage": "设计",
           "capabilities": '["运营"]', "industries": '["银行", " 电力 ", "银行"]', "tags": '["等保"]',
           "control_ids": '["SOC-01"]', "classification": "internal", "visible_roles": '["consultant"]',
           "owner": "", "retired": False, "created_by": "t", "created_at": now, "updated_at": now, **cols}
    conn.execute(text(f"INSERT INTO kb_items ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})"), row)
    return conn.execute(text("SELECT id FROM kb_items WHERE doc_id = :d"), {"d": doc_id}).scalar_one()


def test_k0011_backfill_and_round_trip(tmp_path: Path):
    main = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    kb = create_engine(f"sqlite:///{tmp_path / 'kb.db'}")
    upgrade_to_head(main, kb)
    with kb.begin() as conn:
        cfg = alembic_config(conn, "kb")
        command.downgrade(cfg, "k0010_kb_aliases")
        assert "kb_item_labels" not in inspect(conn).get_table_names()
        iid = _insert_item(conn, "L2-OPS-SOC-MTD-001")
        command.upgrade(cfg, "head")
        got = {(k, v, s) for k, v, s in conn.execute(
            text("SELECT kind, value, source FROM kb_item_labels WHERE item_id = :i"), {"i": iid})}
        assert got == {("product", "SOC", "legacy"), ("industry", "银行", "legacy"), ("industry", "电力", "legacy"),
                       ("capability", "运营", "legacy"), ("domain", "OPS", "legacy"), ("tag", "等保", "legacy"),
                       ("control", "SOC-01", "legacy")}
        conn.execute(text("INSERT INTO kb_item_labels (item_id, kind, value, source) "
                          "VALUES (:i, 'product', 'ISO', 'manual')"), {"i": iid})
        command.downgrade(cfg, "k0010_kb_aliases")
        assert "kb_item_labels" not in inspect(conn).get_table_names()
        prod, inds = conn.execute(text("SELECT product, industries FROM kb_items WHERE id = :i"), {"i": iid}).one()
        assert prod == "SOC" and set(json.loads(inds)) == {"银行", "电力"}   # 多出来的 ISO 丢失，主产品保留
        command.upgrade(cfg, "head")
    main.dispose()
    kb.dispose()


def test_k0011_self_check_rejects_item_without_industry(tmp_path: Path):
    main = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    kb = create_engine(f"sqlite:///{tmp_path / 'kb.db'}")
    upgrade_to_head(main, kb)
    with kb.connect() as conn:
        cfg = alembic_config(conn, "kb")
        command.downgrade(cfg, "k0010_kb_aliases")
        conn.commit()
        _insert_item(conn, "L2-OPS-SOC-MTD-002", industries="[]")
        conn.commit()
        with pytest.raises(RuntimeError, match="行业"):
            command.upgrade(cfg, "head")
    main.dispose()
    kb.dispose()
