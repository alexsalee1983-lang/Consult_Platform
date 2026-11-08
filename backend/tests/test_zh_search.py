"""中文全文检索（zhparser）：只在 PostgreSQL 上运行（CI 用平台数据库镜像）。"""
from sqlalchemy import text

from app.core.db import SessionLocal

from .conftest import on_postgres

pytestmark = on_postgres


def _tokens(sql: str, **params) -> list:
    with SessionLocal() as db:
        return db.execute(text(sql), params).scalar()


def test_zh_config_segments_chinese():
    lexemes = _tokens("SELECT array(SELECT unnest(tsvector_to_array(to_tsvector('zh', :t))))",
                      t="安全运营中心的值班制度需要整改")
    for word in ("安全", "运营", "值班", "制度", "整改"):
        assert word in lexemes, lexemes


def test_zh_query_matches_document():
    hit = _tokens("SELECT to_tsvector('zh', :doc) @@ plainto_tsquery('zh', :q)",
                  doc="事件响应流程应规定一级事件的响应时限和升级路径", q="响应时限")
    miss = _tokens("SELECT to_tsvector('zh', :doc) @@ plainto_tsquery('zh', :q)",
                   doc="事件响应流程应规定一级事件的响应时限和升级路径", q="备份恢复")
    assert hit is True and miss is False


def test_vector_and_zhparser_extensions_installed():
    names = _tokens("SELECT array(SELECT extname FROM pg_extension)")
    assert {"vector", "zhparser"} <= set(names)
