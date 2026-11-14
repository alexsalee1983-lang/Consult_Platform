"""知识区迁移链（M5 ③ 分库）：只管 kb_ 开头的表，版本记在 alembic_version_kb。

分库时跑在知识区数据库（KB_DATABASE_URL），同库时与主迁移链跑在同一个库。
由 app.core.migrate 调用；命令行：alembic -n kb revision --autogenerate --rev-id k0002_xxx -m "说明"。
"""
from alembic import context
from sqlalchemy import Connection

from app import models  # noqa: F401  注册全部表结构
from app.core.db import Base, kb_engine

VERSION_TABLE = "alembic_version_kb"
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    table = name if type_ == "table" else getattr(getattr(obj, "table", None), "name", "")
    return bool(table) and table.startswith("kb_")


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        version_table=VERSION_TABLE,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    with kb_engine.connect() as conn:
        _run(conn)
        conn.commit()


if context.is_offline_mode():
    raise SystemExit("知识区迁移链不支持离线模式")
run_migrations_online()
