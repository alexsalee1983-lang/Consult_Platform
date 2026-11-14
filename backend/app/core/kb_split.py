"""把知识区数据从主库搬到知识区数据库（M5 ③ 分库，一次性运维操作）。

用法（backend 目录或容器里）：
  python -m app.cli kb-split                 复制：知识区库必须是空的；复制后逐表核对行数
  python -m app.cli kb-split --drop-source   核对两边行数一致后，删掉主库里的知识区表（删数据，先经负责人同意）

只搬数据库里的行；对象存储知识区的文件本来就在单独的桶里，不动。向量列按文本形式搬，不重新向量化。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, Table, inspect, insert, select, text

from ..models import KbChunk, KbDeidReport, KbItem, KbReview, KbUpload, KbVersion
from .migrate import KB_TABLES, drop_main_kb_tables, kb_row_count

# 按外键依赖排序；kb_items.current_version_id 指向 kb_versions，先置空，版本搬完再补
COPY_ORDER: tuple[Table, ...] = tuple(m.__table__ for m in (KbUpload, KbDeidReport, KbItem, KbVersion, KbChunk, KbReview))
_BATCH = 500


class SplitError(RuntimeError):
    pass


@dataclass
class SplitResult:
    counts: dict[str, int] = field(default_factory=dict)
    copied: bool = False
    dropped: bool = False


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()  # noqa: S608  # nosec B608  表名是常量
                for t in KB_TABLES if t in names}


def _copy(main: Engine, kb: Engine) -> None:
    postgres = main.dialect.name == "postgresql" and kb.dialect.name == "postgresql"
    with main.connect() as src, kb.begin() as dst:
        for table in COPY_ORDER:
            rows = src.execute(select(table).order_by(table.c.id)).mappings()
            batch: list[dict] = []
            for r in rows:
                row = dict(r)
                if table.name == "kb_items":
                    row["current_version_id"] = None
                batch.append(row)
                if len(batch) >= _BATCH:
                    dst.execute(insert(table), batch)
                    batch = []
            if batch:
                dst.execute(insert(table), batch)
        items = KbItem.__table__
        for item_id, cv in src.execute(select(items.c.id, items.c.current_version_id)
                                       .where(items.c.current_version_id.is_not(None))):
            dst.execute(items.update().where(items.c.id == item_id).values(current_version_id=cv))
        if postgres:
            # 向量列不在模型里（迁移 0014 / k0001 用原生 SQL 加的），按文本搬；tsv 是生成列，自动重算
            for cid, model, vec in src.execute(text(
                    "SELECT id, embedding_model, embedding::text FROM kb_chunks WHERE embedding IS NOT NULL")):
                dst.execute(text("UPDATE kb_chunks SET embedding = CAST(:v AS vector), embedding_model = :m "
                                 "WHERE id = :id"), {"v": vec, "m": model, "id": cid})
            for t in KB_TABLES:
                dst.execute(text(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "  # nosec B608  表名是常量
                                 f"coalesce((SELECT max(id) FROM {t}), 0) + 1, false)"))


def split(main: Engine, kb: Engine, drop_source: bool = False) -> SplitResult:
    if main is kb or main.url == kb.url:
        raise SplitError("没有配置单独的知识区数据库（KB_DATABASE_URL）")
    src = _counts(main)
    if not src:
        raise SplitError("主库里已经没有知识区表，不需要搬")
    with kb.connect() as conn:
        kb_rows = kb_row_count(conn)
    if kb_rows is None:
        raise SplitError("知识区数据库还没建表：先执行 python -m app.core.migrate")
    result = SplitResult(counts=src)
    if kb_rows == 0 and sum(src.values()) > 0:
        _copy(main, kb)
        result.copied = True
    dst = _counts(kb)
    diff = {t: (n, dst.get(t, 0)) for t, n in src.items() if dst.get(t, 0) != n}
    if diff:
        detail = "；".join(f"{t} 主库 {a} 行、知识区库 {b} 行" for t, (a, b) in diff.items())
        raise SplitError(f"两边行数不一致，没有删除任何数据：{detail}")
    if drop_source:
        with main.begin() as conn:
            drop_main_kb_tables(conn)
        result.dropped = True
    return result
