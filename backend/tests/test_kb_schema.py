"""知识库数据表与元数据规则（公共规范第 7 节）：取值、doc_id、复审日期、建条目与版本、中文全文索引。"""
from datetime import date

import pytest
from sqlalchemy import select, text

from app.core.db import SessionLocal
from app.kb import meta
from app.kb.items import ItemMeta, VersionContent, create_draft, create_item, next_doc_id
from app.models import KbChunk, KbItem, KbVersion

from .conftest import on_postgres


def _meta(**kw) -> ItemMeta:
    base = {"layer": "L2", "domain": "OPS", "product": "SOC", "doc_type": "MTD", "stage": "设计",
            "capabilities": ["运营"], "industries": ["通用"]}
    return ItemMeta(**{**base, **kw})


def _content(**kw) -> VersionContent:
    base = {"title": "SOC 告警分级方法", "body": "按业务影响和攻击阶段把告警分为四级。", "source_type": "expert",
            "effective_date": date(2026, 11, 9)}
    return VersionContent(**{**base, **kw})


# ---- 取值与 doc_id（公共规范第 7 节）----

def test_enumerations_match_common_spec():
    assert list(meta.LAYERS) == ["L1", "L2", "L3", "L4", "L5"]
    assert list(meta.DOMAINS) == ["GOV", "CMP", "DAT", "OPS", "RES", "ARC"]
    assert list(meta.DOC_TYPES) == ["TPL", "GLD", "POL", "MTD", "REF", "MAP", "CAS", "OBJ", "PIT"]
    assert meta.STAGES == ("启动", "调研", "分析", "设计", "落地辅导", "验收", "通用")
    assert meta.CAPABILITIES == ("管理", "运营", "技术支撑")
    assert meta.REVIEW_ROLES == {"content": "knowledge_expert", "compliance": "knowledge_admin"}


def test_doc_id_format_and_parse():
    assert meta.format_doc_id("L2", "OPS", "SOC", "TPL", 7) == "L2-OPS-SOC-TPL-007"
    assert meta.parse_doc_id("L2-OPS-SOC-TPL-007") == ("L2", "OPS", "SOC", "TPL", 7)
    for bad in ("L6-OPS-SOC-TPL-001", "L2-XXX-SOC-TPL-001", "L2-OPS-ABC-TPL-001", "L2-OPS-SOC-ZZZ-001",
                "L2-OPS-SOC-TPL-7", "l2-ops-soc-tpl-001"):
        with pytest.raises(meta.MetaError):
            meta.parse_doc_id(bad)


def test_review_date_defaults():
    assert meta.default_review_date("L2", date(2026, 11, 9)) == date(2027, 11, 9)
    assert meta.default_review_date("L4", date(2026, 11, 9)) == date(2027, 5, 9)     # 依据 6 个月
    assert meta.add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)                 # 月末对齐


@pytest.mark.parametrize(("kw", "msg"), [
    ({"stage": "汇报"}, "交付阶段"),
    ({"capabilities": []}, "能力域"),
    ({"capabilities": ["其他"]}, "能力域"),
    ({"industries": []}, "适用行业"),
    ({"classification": "secret"}, "密级"),
    ({"visible_roles": ["guest"]}, "可见角色"),
    ({"product": "ISO"}, "服务产品"),
])
def test_item_meta_rejects_bad_values(kw, msg):
    with pytest.raises(meta.MetaError, match=msg):
        _meta(**kw).check()


@pytest.mark.parametrize(("kw", "msg"), [
    ({"title": " "}, "标题"),
    ({"source_type": "web"}, "来源"),
    ({"source_type": "internal", "source_detail": ""}, "授权编号"),
])
def test_version_content_rejects_bad_values(kw, msg):
    with pytest.raises(meta.MetaError, match=msg):
        _content(**kw).check("L2")


def test_reference_layer_requires_document_version_status():
    with pytest.raises(meta.MetaError, match="文件名和版本号"):
        _content().check("L4")
    with pytest.raises(meta.MetaError, match="生效状态"):
        _content(ref_document="GB/T 22239", ref_version="2019", ref_status="valid").check("L4")
    _content(ref_document="GB/T 22239", ref_version="2019", ref_status="current").check("L4")


# ---- 建条目与版本 ----

def test_create_item_allocates_sequential_doc_ids_and_draft_v1():
    with SessionLocal() as db:
        m = _meta(doc_type="PIT", stage="通用", product="GEN", layer="L5")
        before = next_doc_id(db, m)
        v1 = create_item(db, m, _content(title="踩坑：交接班记录缺失"), created_by="expert1")
        v2 = create_item(db, m, _content(title="踩坑：告警阈值照抄"), created_by="expert1")
        i1, i2 = db.get(KbItem, v1.item_id), db.get(KbItem, v2.item_id)
        assert i1.doc_id == before
        assert meta.parse_doc_id(i2.doc_id)[4] == meta.parse_doc_id(i1.doc_id)[4] + 1
        assert v1.version == 1 and v1.status == "draft" and i1.current_version_id is None
        assert v1.review_date == date(2027, 11, 9)


def test_l4_reference_gets_six_month_review():
    with SessionLocal() as db:
        v = create_item(db, _meta(layer="L4", product="GEN", doc_type="REF", stage="通用", capabilities=["管理"]),
                        _content(title="安全管理中心要求解读", source_type="public", ref_document="GB/T 22239",
                                 ref_version="2019", ref_status="current"), created_by="expert1")
        assert v.review_date == date(2027, 5, 9)


def test_revision_creates_next_version_and_retired_item_cannot_be_revised():
    with SessionLocal() as db:
        v1 = create_item(db, _meta(), _content(), created_by="expert1")
        item = db.get(KbItem, v1.item_id)
        v2 = create_draft(db, item, _content(change_note="新增无夜班客户的适用说明"), created_by="expert2")
        assert v2.version == 2 and [v.version for v in item.versions] == [1, 2]
        item.retired = True
        db.commit()
        with pytest.raises(meta.MetaError, match="下线"):
            create_draft(db, item, _content(), created_by="expert2")


def test_invalid_meta_writes_nothing():
    with SessionLocal() as db:
        before = db.scalar(select(KbItem.id).order_by(KbItem.id.desc()).limit(1))
        with pytest.raises(meta.MetaError):
            create_item(db, _meta(stage="汇报"), _content(), created_by="expert1")
        assert db.scalar(select(KbItem.id).order_by(KbItem.id.desc()).limit(1)) == before


# ---- 中文全文索引（PostgreSQL）----

@on_postgres
def test_chunk_fulltext_index_generated():
    with SessionLocal() as db:
        v = create_item(db, _meta(), _content(title="值班管理规程"), created_by="expert1")
        db.add_all([KbChunk(version_id=v.id, seq=1, heading="值班管理规程 / 交接班",
                            text="交接班时必须核对未闭环的一级事件和升级记录。"),
                    KbChunk(version_id=v.id, seq=2, heading="值班管理规程 / 夜间",
                            text="无夜班客户由托管方初筛，一级事件电话升级轮值负责人。")])
        db.commit()
        hits = db.execute(text("SELECT seq FROM kb_chunks WHERE version_id = :v AND tsv @@ plainto_tsquery('zh', :q) "
                               "ORDER BY seq"), {"v": v.id, "q": "交接班 一级事件"}).scalars().all()
        assert hits == [1]
        assert db.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_kb_chunks_tsv'")).scalar() == 1


@on_postgres
def test_current_version_foreign_key_enforced():
    from sqlalchemy.exc import IntegrityError

    with SessionLocal() as db:
        v = create_item(db, _meta(), _content(), created_by="expert1")
        item = db.get(KbItem, v.item_id)
        item.current_version_id = 99_999_999
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        item = db.get(KbItem, v.item_id)
        item.current_version_id = v.id
        db.commit()
        assert db.get(KbVersion, item.current_version_id).title == "SOC 告警分级方法"
