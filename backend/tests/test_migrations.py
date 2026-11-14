"""迁移脚本与表结构的一致性：S1 起表结构只通过迁移变更，模型改了而没写迁移时这里会失败。"""
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, insert, inspect, select, text

from app import models
from app.core.db import Base
from app.core.migrate import BASELINE, KB_TABLES, KbSplitPending, alembic_config, upgrade_to_head


@pytest.fixture()
def engine(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path / 'migrate.db'}")
    yield eng
    eng.dispose()


def _head() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _current(eng) -> str | None:
    with eng.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def test_fresh_database_upgrades_to_head(engine):
    upgrade_to_head(engine)
    assert _current(engine) == _head()
    assert {"projects", "deliverables", "confirmations", "audit_logs"} <= set(inspect(engine).get_table_names())


def test_models_match_migrations(engine):
    upgrade_to_head(engine)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_object": lambda o, n, t, r, c: n != "alembic_version_kb"})
        diffs = compare_metadata(ctx, Base.metadata)
    assert diffs == [], f"app/models 与迁移脚本不一致，请补迁移：{diffs}"


def _kb_engine(tmp_path: Path):
    return create_engine(f"sqlite:///{tmp_path / 'kb.db'}")


def test_kb_chain_stamped_on_shared_database(engine):
    upgrade_to_head(engine)
    with engine.connect() as conn:
        assert MigrationContext.configure(conn, opts={"version_table": "alembic_version_kb"}).get_current_revision()             == ScriptDirectory.from_config(alembic_config(chain="kb")).get_current_head()


def test_split_fresh_databases(engine, tmp_path):
    """分库：知识区表只在知识区库，主库里旧迁移建出的空 kb_ 表被删掉；两边结构都与模型一致。"""
    kb = _kb_engine(tmp_path)
    upgrade_to_head(engine, kb)
    main_tables = set(inspect(engine).get_table_names())
    kb_tables = set(inspect(kb).get_table_names())
    assert not {t for t in main_tables if t.startswith("kb_")}
    assert set(KB_TABLES) <= kb_tables and "projects" not in kb_tables
    with kb.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_object": lambda o, n, t, r, c: (
            (n if t == "table" else o.table.name).startswith("kb_"))})
        assert compare_metadata(ctx, Base.metadata) == []
    upgrade_to_head(engine, kb)   # 再启动一次不出错
    kb.dispose()


def test_split_refuses_when_data_still_in_main(engine, tmp_path):
    upgrade_to_head(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO kb_uploads (object_key, filename, content_type, size, sha256, status, "
                          "guard_categories, error, uploaded_by, created_at, kind, parsed_key, page_count, "
                          "section_count) VALUES ('k1', 'f', '', 0, 'x', 'received', '[]', '', 'u', "
                          "'2026-11-13 00:00:00', '', '', 0, 0)"))
    kb = _kb_engine(tmp_path)
    with pytest.raises(KbSplitPending):
        upgrade_to_head(engine, kb)
    assert "kb_uploads" in inspect(engine).get_table_names()   # 不会删有数据的表
    kb.dispose()


def test_unversioned_m0_database_is_stamped_and_keeps_data(engine):
    # 模拟 M0：只有 M0 的 4 张表（结构即基线迁移）、已有数据和审计记录、没有 alembic_version
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), BASELINE)
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(insert(models.Project.__table__).values(
            code="BANK-A", service="SOC 运营体系设计", industry="城商行", tier="standard", phase=0,
            week=1, total_weeks=12, next_milestone="", created_at=models.project._now()))
        conn.execute(text("INSERT INTO audit_logs (at, actor, project_code, action, result, detail) "
                          "VALUES ('2026-11-01 08:00:00.000000', 'dev', '', 'llm.chat', 'allowed', '{}')"))

    upgrade_to_head(engine)

    assert _current(engine) == _head()
    with engine.connect() as conn:
        assert conn.scalar(select(models.Project.code)) == "BANK-A"
    # 旧审计记录保留为链前记录，新链从创世记录开始且完整
    from sqlalchemy.orm import Session

    from app.core.audit import verify_chain
    with Session(engine) as db:
        r = verify_chain(db)
    assert r.ok and r.legacy == 1 and r.checked == 1


def test_downgrade_to_base_and_back(engine):
    upgrade_to_head(engine)
    with engine.begin() as conn:
        cfg = alembic_config(conn)
        command.downgrade(cfg, "base")
        assert "projects" not in inspect(conn).get_table_names()
        command.upgrade(cfg, "head")
    assert _current(engine) == _head()


def test_0007_fixes_old_solution_design_code(engine):
    """跑过旧 0006（代号 solution-design）的库，升级到 0007 后改为 solution-designer，授权随之迁移。"""
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "0006_agents_tools")
        conn.execute(text("INSERT INTO agents VALUES ('solution-design', '方案设计', 'delivery', '一期', 1)"))
        conn.execute(text("DELETE FROM agent_tool_grants WHERE agent_code = 'solution-designer'"))
        conn.execute(text("DELETE FROM agents WHERE code = 'solution-designer'"))
        conn.execute(text("INSERT INTO agent_tool_grants VALUES ('solution-design', 'kb.search', 'old', '2026-11-07')"))
        command.upgrade(alembic_config(conn), "0007_conversations")   # 之后的迁移会再加授权（如 0017），只看 0007
        codes = {r[0] for r in conn.execute(text("SELECT code FROM agents"))}
        grants = {r[0] for r in conn.execute(text("SELECT tool_code FROM agent_tool_grants "
                                                  "WHERE agent_code = 'solution-designer'"))}
    assert "solution-designer" in codes and "solution-design" not in codes
    assert grants == {"kb.search"}


def test_baseline_revision_exists():
    assert ScriptDirectory.from_config(alembic_config()).get_revision(BASELINE) is not None
