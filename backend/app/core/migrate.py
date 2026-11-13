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

知识区迁移链（M5 ③ 分库，backend/alembic_kb，版本表 alembic_version_kb）：只管 kb_ 开头的表。
- 同库（没配 KB_DATABASE_URL）：主链建好 kb_ 表后，知识区链在同一个库上标记基线再升级。
- 分库：知识区链在知识区数据库上从基线建表；主库里由旧迁移建出的 kb_ 表如果是空的就删掉，
  有数据而知识区库还是空的就拒绝启动，提示先执行 python -m app.cli kb-split 搬数据。
  分库后不支持把主链降级到 0030 以前。
  alembic -n kb revision --autogenerate --rev-id k0002_xxx -m "说明"   知识区表的改动只写到这条链

alembic.ini 只能写 ASCII 字符：Windows 上 Alembic 按系统代码页读取它。
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from .db import engine as default_engine
from .db import kb_engine as default_kb_engine

BASELINE = "0001_m0_baseline"
KB_BASELINE = "k0001_kb_baseline"
KB_TABLES = ("kb_reviews", "kb_chunks", "kb_versions", "kb_deid_reports", "kb_uploads", "kb_items")  # 可按此顺序删
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
_LOCK_ID = 774_201_001  # 迁移专用的 advisory lock 编号，与审计链的锁区分
log = logging.getLogger(__name__)


class KbSplitPending(RuntimeError):
    """已配置分库，但知识区数据还在主库、知识区库是空的。"""


def alembic_config(connection: Connection | None = None, chain: str = "main") -> Config:
    cfg = Config(str(_ALEMBIC_INI), ini_section="kb" if chain == "kb" else "alembic")
    cfg.set_main_option("script_location", str(_ALEMBIC_INI.parent / ("alembic_kb" if chain == "kb" else "alembic")))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def _is_unversioned_m0(conn: Connection) -> bool:
    tables = set(inspect(conn).get_table_names())
    return "projects" in tables and "alembic_version" not in tables


def kb_row_count(conn: Connection) -> int | None:
    """连接上 kb_ 表的总行数；一张都没有返回 None。"""
    tables = set(inspect(conn).get_table_names())
    present = [t for t in KB_TABLES if t in tables]
    if not present:
        return None
    return sum(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in present)  # noqa: S608  表名是常量


def _upgrade_kb_chain(conn: Connection) -> None:
    tables = set(inspect(conn).get_table_names())
    cfg = alembic_config(conn, "kb")
    if "kb_items" in tables and "alembic_version_kb" not in tables:   # 主链建好的 kb_ 表（同库）
        command.stamp(cfg, KB_BASELINE)
    command.upgrade(cfg, "head")


def drop_main_kb_tables(conn: Connection) -> None:
    for t in KB_TABLES:
        conn.execute(text(f"DROP TABLE IF EXISTS {t}"))


def _locked(conn: Connection) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": _LOCK_ID})


def _unlock(conn: Connection) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _LOCK_ID})
        conn.commit()


def upgrade_to_head(engine: Engine | None = None, kb_engine: Engine | None = None) -> None:
    """主链升级到最新，再升级知识区链。只传 engine 时按同库处理（测试用）。"""
    main = engine or default_engine
    kb = kb_engine or (main if engine is not None else default_kb_engine)
    with main.connect() as conn:
        _locked(conn)
        try:
            cfg = alembic_config(conn)
            if _is_unversioned_m0(conn):
                command.stamp(cfg, BASELINE)
            command.upgrade(cfg, "head")
            conn.commit()
            if kb is main:
                _upgrade_kb_chain(conn)
                conn.commit()
            else:
                with kb.connect() as kconn:
                    _locked(kconn)
                    try:
                        _upgrade_kb_chain(kconn)
                        kconn.commit()
                        kb_rows = kb_row_count(kconn) or 0
                    finally:
                        _unlock(kconn)
                main_rows = kb_row_count(conn)
                if main_rows == 0:      # 旧迁移在主库建出的空表，分库后不再使用
                    drop_main_kb_tables(conn)
                    conn.commit()
                elif main_rows and not kb_rows:
                    raise KbSplitPending("已配置知识区数据库（KB_DATABASE_URL），但知识区数据还在主库："
                                         "请先执行 python -m app.cli kb-split")
                elif main_rows:
                    log.warning("主库里还留着知识区表的旧副本（%s 行）：核对无误后执行 "
                                "python -m app.cli kb-split --drop-source 删除", main_rows)
        finally:
            _unlock(conn)


if __name__ == "__main__":
    upgrade_to_head()
    print("数据库已升级到最新版本")
