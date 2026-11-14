"""M5 ③ 分库：主库里的知识区数据搬到知识区库（kb-split），以及会话把知识区读写发往知识区库。"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core import kb_split
from app.core.db import Base, KbBase
from app.core.migrate import KbSplitPending, upgrade_to_head
from app.models import KbChunk, KbItem, KbUpload, KbVersion, Project


def _engines(tmp_path: Path):
    return create_engine(f"sqlite:///{tmp_path / 'main.db'}"), create_engine(f"sqlite:///{tmp_path / 'kb.db'}")


def _seed(main) -> None:
    upgrade_to_head(main)   # 同库：kb_ 表在主库
    with Session(main) as db:
        up = KbUpload(object_key="knowledge/u1", filename="样例.docx", sha256="a" * 64, uploaded_by="ke1")
        item = KbItem(doc_id="L2-OPS-SOC-TPL-901", layer="L2", domain="OPS", product="SOC", doc_type="TPL",
                      stage="S3", classification="internal", owner="ke1")
        db.add_all([up, item])
        db.flush()
        v = KbVersion(item_id=item.id, version=1, title="样例", body="正文", status="published",
                      source_type="public", upload_id=up.id)
        db.add(v)
        db.flush()
        item.current_version_id = v.id
        db.add(KbChunk(version_id=v.id, seq=0, heading="一", text="切片"))
        db.commit()


def test_split_copies_verifies_and_drops(tmp_path):
    main, kb = _engines(tmp_path)
    _seed(main)
    with pytest.raises(KbSplitPending):
        upgrade_to_head(main, kb)   # 数据还在主库：拒绝启动，但知识区库已建表

    r = kb_split.split(main, kb)
    assert r.copied and not r.dropped and r.counts["kb_chunks"] == 1
    S = sessionmaker(bind=main, binds={KbBase: kb})
    with S() as db:
        item = db.scalar(select(KbItem))
        assert item.current_version_id is not None and db.get(KbVersion, item.current_version_id).title == "样例"
    upgrade_to_head(main, kb)   # 两边都有：只告警，能启动

    again = kb_split.split(main, kb, drop_source=True)   # 重复执行不再复制，核对一致后删主库副本
    assert not again.copied and again.dropped
    assert not [t for t in inspect(main).get_table_names() if t.startswith("kb_")]
    with pytest.raises(kb_split.SplitError):
        kb_split.split(main, kb)
    upgrade_to_head(main, kb)
    main.dispose()
    kb.dispose()


def test_split_refuses_mismatched_target(tmp_path):
    main, kb = _engines(tmp_path)
    _seed(main)
    with pytest.raises(KbSplitPending):
        upgrade_to_head(main, kb)
    with Session(kb) as db:   # 知识区库里已有别的数据：不复制、不删
        db.add(KbUpload(object_key="knowledge/u9", filename="y.md", sha256="c" * 64, uploaded_by="ke1"))
        db.commit()
    with pytest.raises(kb_split.SplitError, match="不一致"):
        kb_split.split(main, kb, drop_source=True)
    assert "kb_items" in inspect(main).get_table_names()
    main.dispose()
    kb.dispose()


def test_session_routes_kb_tables(tmp_path):
    main, kb = _engines(tmp_path)
    upgrade_to_head(main, kb)
    S = sessionmaker(bind=main, binds={KbBase: kb})
    with S() as db:
        db.add(Project(code="BANK-A", service="差距分析", industry="银行", tier="standard"))
        db.add(KbUpload(object_key="knowledge/u2", filename="x.md", sha256="b" * 64, uploaded_by="ke1"))
        db.commit()
    assert "projects" not in inspect(kb).get_table_names()
    with kb.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM kb_uploads")).scalar_one() == 1
    with main.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM projects")).scalar_one() == 1
    assert Base.metadata.tables["kb_items"] is KbItem.__table__
    main.dispose()
    kb.dispose()
