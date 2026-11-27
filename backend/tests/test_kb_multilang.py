"""四期 B28①：多语言与海外法规最小版（平台侧）——语种与辖区字段、译文缺“非官方译本”标记拦截、引用回溯原文。"""
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import importer, multilang
from app.kb import review as rv
from app.kb.files import EntryError, format_entry, parse_entry
from app.models import KbItem, User
from app.project.export import kb_labels

from .conftest import create_user


def _user():
    name, *_ = create_user(["knowledge_expert", "knowledge_admin"])
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


def _entry(key: str, title: str = "第十二条 个人数据处理", **extra) -> str:
    head = {"key": key, "title": title, "layer": "L2", "domain": "GOV", "product": "GEN", "doc_type": "MTD",
            "stage": "通用", "capabilities": "[管理]", "source_type": "public", **extra}
    return "---\n" + "\n".join(f"{k}: {v}" for k, v in head.items()) + "\n---\n\n## 正文\n\n虚拟法规样本。\n"


def _import(db, user, text: str):
    r = importer.import_entry(db, user, parse_entry(text))
    return db.scalar(select(KbItem).where(KbItem.import_key == r.key))


def _publish_original(db, user, key: str = "sn-loi-12") -> KbItem:
    item = _import(db, user, _entry(key, language="fr", jurisdiction="SN"))
    rv.publish(db, item.versions[-1])
    return item


# ---- 元数据校验（不碰库） ----

def test_defaults_are_chinese_mainland():
    e = parse_entry(_entry("cn-default"))
    assert (e.meta.language, e.meta.jurisdiction, e.meta.translation_of) == ("zh", "CN", "")
    assert "language" not in format_entry(e)          # 默认值不写回文件，旧条目文件往返不变


def test_translation_without_notice_rejected():
    with pytest.raises(EntryError, match="非官方译本"):
        parse_entry(_entry("sn-zh-12", translation_of="KB-000001", jurisdiction="SN"))


def test_notice_without_original_rejected():
    with pytest.raises(EntryError, match="原文"):
        parse_entry(_entry("sn-zh-13", translation_notice="非官方译本", jurisdiction="SN"))


def test_unknown_jurisdiction_and_language_rejected():
    with pytest.raises(EntryError, match="辖区"):
        parse_entry(_entry("xx-1", jurisdiction="ZZ"))
    with pytest.raises(EntryError, match="语种"):
        parse_entry(_entry("xx-2", language="klingon"))


def test_regions_cover_owner_list():
    js = multilang.jurisdictions()
    for code in ("CN", "HK", "MO", "TH", "ID", "VN", "SG", "MY", "EG", "CI", "SN", "ZA", "AO", "ET", "NG"):
        assert code in js


def test_regions_are_config(monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_jurisdictions", "CN,KE")
    assert multilang.jurisdictions() == ("CN", "KE")
    parse_entry(_entry("ke-1", jurisdiction="KE"))
    with pytest.raises(EntryError, match="辖区"):
        parse_entry(_entry("sn-1", jurisdiction="SN"))


# ---- 导入、提交、发布三处拦截 ----

def test_import_translation_links_original():
    user = _user()
    with SessionLocal() as db:
        orig = _publish_original(db, user)
        tr = _import(db, user, _entry("sn-zh-12", language="zh", jurisdiction="SN", translation_of=orig.doc_id,
                                      translation_notice="非官方译本"))
        assert tr.translation_of == orig.doc_id and tr.translation_notice == "非官方译本"


def test_import_translation_of_missing_original_rejected():
    user = _user()
    with SessionLocal() as db, pytest.raises(rv.ReviewError, match="原文"):
        _import(db, user, _entry("sn-zh-99", language="zh", jurisdiction="SN", translation_of="KB-999999",
                                 translation_notice="非官方译本"))


def test_translation_of_translation_rejected():
    user = _user()
    with SessionLocal() as db:
        orig = _publish_original(db, user, "sn-loi-20")
        tr = _import(db, user, _entry("sn-zh-20", language="zh", jurisdiction="SN", translation_of=orig.doc_id,
                                      translation_notice="非官方译本"))
        with pytest.raises(rv.ReviewError, match="原文"):
            _import(db, user, _entry("sn-en-20", language="en", jurisdiction="SN", translation_of=tr.doc_id,
                                     translation_notice="非官方译本"))


def test_submit_and_publish_block_missing_notice():
    user = _user()
    with SessionLocal() as db:
        orig = _publish_original(db, user, "sn-loi-30")
        tr = _import(db, user, _entry("sn-zh-30", language="zh", jurisdiction="SN", translation_of=orig.doc_id,
                                      translation_notice="非官方译本"))
        tr.translation_notice = ""            # 模拟绕过导入校验（例如直接改库）
        db.commit()
        v = tr.versions[-1]
        with pytest.raises(rv.ReviewError, match="非官方译本"):
            rv.submit(db, user, v)
        with pytest.raises(rv.ReviewError, match="非官方译本"):
            rv.publish(db, v)
        assert v.status == "draft"


def test_reimport_dropping_notice_rejected():
    user = _user()
    with SessionLocal() as db:
        orig = _publish_original(db, user, "sn-loi-40")
        orig_id = orig.doc_id
        _import(db, user, _entry("sn-zh-40", language="zh", jurisdiction="SN", translation_of=orig_id,
                                 translation_notice="非官方译本"))
    # 重复导入时条目级字段也要过校验：文件里去掉标记，解析即拦下
    with pytest.raises(EntryError, match="非官方译本"):
        parse_entry(_entry("sn-zh-40", language="zh", jurisdiction="SN", translation_of=orig_id))


# ---- 引用回溯原文 ----

def test_trace_and_export_label_point_to_original():
    user = _user()
    with SessionLocal() as db:
        orig = _publish_original(db, user, "sn-loi-50")
        tr = _import(db, user, _entry("sn-zh-50", title="第十二条 个人数据处理（中文译本）", language="zh",
                                      jurisdiction="SN", translation_of=orig.doc_id, translation_notice="非官方译本"))
        rv.publish(db, tr.versions[-1])
        t = multilang.trace(db, {tr.doc_id, orig.doc_id})
        assert orig.doc_id not in t                      # 原文自己不需要回溯
        assert t[tr.doc_id].original_doc_id == orig.doc_id
        assert t[tr.doc_id].original_title == "第十二条 个人数据处理"
        label = kb_labels(db, {tr.doc_id})[tr.doc_id]
        assert "非官方译本，以原文为准" in label and orig.doc_id in label and "第十二条" in label
        assert "非官方译本" not in kb_labels(db, {orig.doc_id})[orig.doc_id]
