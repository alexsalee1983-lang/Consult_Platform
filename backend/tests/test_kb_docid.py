"""知识条目换编号（三期 B15 第三步，知识区迁移 k0012，docs/三期方案/B15知识数据结构迁移方案.md 4.3）：
新 doc_id 是不带分类含义的稳定序号 KB-000123（seq 列），旧编号全部登记成 legacy 别名、照样能解析；
k0012 upgrade / downgrade 往返（含迁移后新建条目的回滚）；视图 kb_v_item_facets；跨环境迁移按别名对应。"""
import secrets
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.migrate import alembic_config, upgrade_to_head
from app.kb import meta, refs
from app.kb.items import ItemMeta, VersionContent, create_item, next_doc_id
from app.models import KbItemAlias


def test_meta_formats():
    assert meta.format_doc_id(123) == "KB-000123"
    assert meta.DOC_ID_RE.match("KB-000123") and not meta.DOC_ID_RE.match("L2-OPS-SOC-TPL-007")
    assert meta.LEGACY_DOC_ID_RE.match("L2-OPS-SOC-TPL-007") and not meta.LEGACY_DOC_ID_RE.match("KB-000123")
    assert meta.format_legacy_doc_id("L2", "OPS", "SOC", "TPL", 7) == "L2-OPS-SOC-TPL-007"
    assert meta.parse_legacy_doc_id("L2-OPS-SOC-TPL-007") == ("L2", "OPS", "SOC", "TPL", 7)
    assert refs.is_ref(meta.format_doc_id(1))


def _meta() -> ItemMeta:
    return ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="MTD", stage="设计",
                    capabilities=["运营"], industries=["通用"])


def test_new_items_get_sequential_kb_ids():
    with SessionLocal() as db:
        v1 = create_item(db, _meta(), VersionContent(title=f"编号-{secrets.token_hex(3)}", body="正文。", source_type="expert"), "tester")
        v2 = create_item(db, _meta(), VersionContent(title=f"编号-{secrets.token_hex(3)}", body="正文。", source_type="expert"), "tester")
        i1, i2 = v1.item, v2.item
        assert meta.DOC_ID_RE.match(i1.doc_id) and i1.doc_id == meta.format_doc_id(i1.seq)
        assert i2.seq == i1.seq + 1
        # 别名占用的新格式编号不再分配
        refs.add_alias(db, meta.format_doc_id(i2.seq + 1), i1.id, "transfer")
        db.commit()
        assert next_doc_id(db) == meta.format_doc_id(i2.seq + 2)


# ---- 迁移 k0012 ----

def _insert_item(conn, doc_id: str, **cols) -> int:
    now = datetime.now(timezone.utc)
    row = {"doc_id": doc_id, "layer": "L2", "domain": "OPS", "product": "SOC", "doc_type": "MTD", "stage": "设计",
           "capabilities": '["运营"]', "industries": '["银行"]', "tags": "[]", "control_ids": '["SOC-01"]',
           "classification": "internal", "visible_roles": '["consultant"]', "owner": "", "retired": False,
           "created_by": "t", "created_at": now, "updated_at": now, **cols}
    conn.execute(text(f"INSERT INTO kb_items ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})"), row)
    return conn.execute(text("SELECT id FROM kb_items WHERE doc_id = :d"), {"d": doc_id}).scalar_one()


def _label(conn, iid: int, kind: str, value: str) -> None:
    conn.execute(text("INSERT INTO kb_item_labels (item_id, kind, value, source) VALUES (:i, :k, :v, 'legacy')"),
                 {"i": iid, "k": kind, "v": value})


def _doc_id(conn, iid: int) -> str:
    return conn.execute(text("SELECT doc_id FROM kb_items WHERE id = :i"), {"i": iid}).scalar_one()


def test_k0012_round_trip(tmp_path: Path):
    main = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    kb = create_engine(f"sqlite:///{tmp_path / 'kb.db'}")
    upgrade_to_head(main, kb)
    with kb.begin() as conn:
        cfg = alembic_config(conn, "kb")
        command.downgrade(cfg, "k0011_kb_labels")
        old = "L2-OPS-SOC-MTD-004"
        iid = _insert_item(conn, old)
        for k, v in (("product", "SOC"), ("product", "ISO"), ("industry", "银行"), ("domain", "OPS"),
                     ("capability", "运营"), ("control", "SOC-01")):
            _label(conn, iid, k, v)
        command.upgrade(cfg, "head")
        new = _doc_id(conn, iid)
        assert new == meta.format_doc_id(iid)
        assert conn.execute(text("SELECT seq FROM kb_items WHERE id = :i"), {"i": iid}).scalar_one() == iid
        assert conn.execute(text("SELECT item_id, kind FROM kb_item_aliases WHERE alias = :a"),
                            {"a": old}).one() == (iid, "legacy")
        with Session(bind=conn) as db:
            got = refs.resolve(db, [old, new])
            assert got[old].doc_id == new and got[old].via == "alias" and got[new].via == "doc_id"
        f = conn.execute(text("SELECT * FROM kb_v_item_facets WHERE item_id = :i"), {"i": iid}).mappings().one()
        assert f["doc_id"] == new and f["layer"] == "L2" and f["doc_type"] == "MTD"
        assert set(f["products"].split(",")) == {"SOC", "ISO"} and f["industries"] == "银行"
        assert f["domains"] == "OPS" and f["capabilities"] == "运营" and f["controls"] == "SOC-01"

        # 迁移后新建的条目：回滚时按主分类现场分配旧格式编号，原新编号登记为别名
        later = _insert_item(conn, "KB-999999", seq=999999)
        command.downgrade(cfg, "k0011_kb_labels")
        assert _doc_id(conn, iid) == old
        legacy_later = _doc_id(conn, later)
        assert legacy_later == "L2-OPS-SOC-MTD-005"              # 同前缀已有 004
        assert conn.execute(text("SELECT item_id FROM kb_item_aliases WHERE alias = 'KB-999999'")).scalar_one() == later
        # legacy 别名不删（与恢复后的 doc_id 同名、指向同一条目）
        assert conn.execute(text("SELECT item_id FROM kb_item_aliases WHERE alias = :a"), {"a": old}).scalar_one() == iid

        # 再升级：编号回到 seq = id，不与自己的别名冲突
        command.upgrade(cfg, "head")
        assert _doc_id(conn, iid) == new and _doc_id(conn, later) == meta.format_doc_id(later)
        aliases = {a for (a,) in conn.execute(text("SELECT alias FROM kb_item_aliases"))}
        docs = {d for (d,) in conn.execute(text("SELECT doc_id FROM kb_items"))}
        assert not aliases & docs and {old, legacy_later, "KB-999999"} <= aliases
    main.dispose()
    kb.dispose()


def test_transfer_import_keeps_refs_via_aliases():
    from app.kb import transfer
    with SessionLocal() as db:
        v = create_item(db, _meta(), VersionContent(title=f"迁移-{secrets.token_hex(3)}", body="正文。", source_type="expert"), "tester")
        item = v.item
        legacy = f"L2-OPS-SOC-MTD-{900 + secrets.randbelow(99000)}"
        free_new = meta.format_doc_id(item.seq + 500000)
        transfer.register_source_ids(db, item, legacy, [])
        transfer.register_source_ids(db, item, free_new, [])
        db.commit()
        assert item.doc_id != legacy
        got = {a.alias: a.kind for a in db.scalars(select(KbItemAlias).where(KbItemAlias.item_id == item.id))}
        assert got[legacy] == "legacy" and got[free_new] == "transfer"
        # 另一个条目带来同一个旧编号：已被占用，不重复登记、不报错
        v2 = create_item(db, _meta(), VersionContent(title=f"迁移-{secrets.token_hex(3)}", body="正文。", source_type="expert"), "tester")
        transfer.register_source_ids(db, v2.item, legacy, [legacy])
        db.commit()
        assert db.get(KbItemAlias, legacy).item_id == item.id


@pytest.mark.parametrize("bad", ["L2-OPS-SOC-TPL-7", "KB-12345", "kb-000001"])
def test_bad_ids_rejected(bad: str):
    assert not meta.DOC_ID_RE.match(bad)
