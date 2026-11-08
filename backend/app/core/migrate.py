"""数据库迁移：启动时把库升级到最新版本。

- 新库：按 backend/alembic/versions 从头建表。
- M0 用自动建表建出的旧库（有 projects 表、没有 alembic_version 表）：表结构与基线迁移一致，
  先标记为基线版本，再升级，已有数据保留。
- PostgreSQL 上用 advisory lock 保证多个进程同时启动时只有一个在执行迁移。

常用命令（在 backend 目录下）：
  python -m app.core.migrate                    升级到最新（含上面的旧库处理），等价于 alembic upgrade head
  alembic revision --autogenerate --rev-id 0002_users -m "M1：用户与会话"
                                                根据 app/models 的改动生成迁移，文件名用顺序编号；生成后必须人工检查
  alembic check                                 检查模型与迁移是否一致（tests/test_migrations.py 也会检查）

alembic.ini 只能写 ASCII 字符：Windows 上 Alembic 按系统代码页读取它。
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from .db import engine as default_engine

BASELINE = "0001_m0_baseline"
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
_LOCK_ID = 774_201_001  # 迁移专用的 advisory lock 编号，与审计链的锁区分


def alembic_config(connection: Connection | None = None) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_INI.parent / "alembic"))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def _is_unversioned_m0(conn: Connection) -> bool:
    tables = set(inspect(conn).get_table_names())
    return "projects" in tables and "alembic_version" not in tables


def upgrade_to_head(engine: Engine | None = None) -> None:
    with (engine or default_engine).connect() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": _LOCK_ID})
        try:
            cfg = alembic_config(conn)
            if _is_unversioned_m0(conn):
                command.stamp(cfg, BASELINE)
            command.upgrade(cfg, "head")
            conn.commit()
        finally:
            if conn.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _LOCK_ID})
                conn.commit()


if __name__ == "__main__":
    upgrade_to_head()
    print("数据库已升级到最新版本")
