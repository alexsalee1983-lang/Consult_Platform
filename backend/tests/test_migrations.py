"""迁移脚本与表结构的一致性：S1 起表结构只通过迁移变更，模型改了而没写迁移时这里会失败。"""
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, insert, inspect, select

from app import models
from app.core.db import Base
from app.core.migrate import BASELINE, alembic_config, upgrade_to_head


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
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diffs == [], f"app/models 与迁移脚本不一致，请补迁移：{diffs}"


def test_unversioned_m0_database_is_stamped_and_keeps_data(engine):
    # 模拟 M0：只有 M0 的 4 张表（自动建表）、已有数据、没有 alembic_version
    m0_tables = [Base.metadata.tables[t] for t in ("projects", "deliverables", "confirmations", "audit_logs")]
    Base.metadata.create_all(engine, tables=m0_tables)
    with engine.begin() as conn:
        conn.execute(insert(models.Project.__table__).values(
            code="BANK-A", service="SOC 运营体系设计", industry="城商行", tier="standard", phase=0,
            week=1, total_weeks=12, next_milestone="", created_at=models.project._now()))

    upgrade_to_head(engine)

    assert _current(engine) == _head()
    with engine.connect() as conn:
        assert conn.scalar(select(models.Project.code)) == "BANK-A"


def test_downgrade_to_base_and_back(engine):
    upgrade_to_head(engine)
    with engine.begin() as conn:
        cfg = alembic_config(conn)
        command.downgrade(cfg, "base")
        assert "projects" not in inspect(conn).get_table_names()
        command.upgrade(cfg, "head")
    assert _current(engine) == _head()


def test_baseline_revision_exists():
    assert ScriptDirectory.from_config(alembic_config()).get_revision(BASELINE) is not None
