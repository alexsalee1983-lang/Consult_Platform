"""Alembic 运行环境。数据库地址取自应用配置（环境变量 DATABASE_URL），不写在 alembic.ini。"""
from alembic import context
from sqlalchemy import Connection

from app import models  # noqa: F401  注册全部表结构
from app.core.db import Base, engine

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    """M5 ③ 起知识区表（kb_）归知识区迁移链 alembic_kb 管，本链自动生成时不看它们。"""
    table = name if type_ == "table" else getattr(getattr(obj, "table", None), "name", "")
    return not (table or "").startswith("kb_") and table != "alembic_version_kb"


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite 改表需要批处理模式
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=str(engine.url), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:  # 由 app.core.migrate 传入，复用同一连接和锁
        _run(connection)
        return
    with engine.connect() as conn:
        _run(conn)
        conn.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
