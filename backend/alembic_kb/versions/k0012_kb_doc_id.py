"""三期 B15：知识条目换编号（doc_id 改为不带分类含义的 KB-000123）与视图 kb_v_item_facets。

upgrade：
1. kb_items 加 seq（唯一），按 id 回填 seq = id；
2. 每个旧格式编号登记为别名（kind=legacy，已登记的不重复）；
3. doc_id = 'KB-' + 6 位补零的 seq（Python 拼字符串，不用方言函数）；条目自己名下与新编号相同的别名
   （上一次 downgrade 留下的）去掉，保持“别名与现行编号互斥”；
4. 建视图 kb_v_item_facets：每个条目一行，标签按维度用逗号拼接（PG string_agg / SQLite group_concat）。
downgrade：
1. 删视图；2. 有 legacy 别名的条目 doc_id 恢复为旧编号；3. 迁移后新建、没有旧编号的条目按主分类列
   （layer / domain / product / doc_type）现场分配旧格式编号，原新编号登记为 legacy 别名，回滚后引用照样能解析；
4. 删 seq。别名表里的 legacy 行不删（与恢复后的 doc_id 同名、指向同一条目，解析结果不变；k0010 的 downgrade 才删表）。
迁移前后各跑一次 kb-ref-check（--snapshot / --compare），必须零差异（方案第 5 节）。

Revision ID: k0012_kb_doc_id
Revises: k0011_kb_labels
Create Date: 2026-11-22
"""
import re
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "k0012_kb_doc_id"
down_revision: str | None = "k0011_kb_labels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW = re.compile(r"^KB-\d{6}$")
_LEGACY = re.compile(r"^(L[1-5]-[A-Z]{3}-[A-Z]{2,8}-[A-Z]{3}-)(\d{3,})$")
_FACETS = (("products", "product"), ("industries", "industry"), ("domains", "domain"),
           ("capabilities", "capability"), ("controls", "control"))


def _agg(dialect: str) -> str:
    return "string_agg(l.value, ',' ORDER BY l.value)" if dialect == "postgresql" else "group_concat(l.value, ',')"


def _view_sql(dialect: str) -> str:
    cols = ",\n  ".join(
        f"(SELECT {_agg(dialect)} FROM kb_item_labels l WHERE l.item_id = i.id AND l.kind = '{kind}') AS {name}"
        for name, kind in _FACETS)
    return f"CREATE VIEW kb_v_item_facets AS SELECT\n  i.id AS item_id, i.doc_id, i.layer, i.doc_type,\n  {cols}\n" \
           "FROM kb_items i"


def upgrade() -> None:
    conn = op.get_bind()
    with op.batch_alter_table("kb_items") as b:
        b.add_column(sa.Column("seq", sa.Integer(), nullable=True))
        b.create_unique_constraint("uq_kb_items_seq", ["seq"])
    now = datetime.now(timezone.utc)
    aliases = {a: i for a, i in conn.execute(sa.text("SELECT alias, item_id FROM kb_item_aliases"))}
    for iid, doc_id in conn.execute(sa.text("SELECT id, doc_id FROM kb_items ORDER BY id")).all():
        new = f"KB-{iid:06d}"
        if doc_id != new and doc_id not in aliases:
            conn.execute(sa.text("INSERT INTO kb_item_aliases (alias, item_id, kind, created_at) "
                                 "VALUES (:a, :i, 'legacy', :t)"), {"a": doc_id, "i": iid, "t": now})
        if aliases.get(new) not in (None, iid):
            raise RuntimeError(f"新编号 {new} 已是另一个条目的别名，先人工处理")
        conn.execute(sa.text("DELETE FROM kb_item_aliases WHERE alias = :a AND item_id = :i"), {"a": new, "i": iid})
        conn.execute(sa.text("UPDATE kb_items SET seq = :s, doc_id = :d WHERE id = :i"), {"s": iid, "d": new, "i": iid})
    op.execute(_view_sql(conn.dialect.name))


def downgrade() -> None:
    conn = op.get_bind()
    op.execute("DROP VIEW IF EXISTS kb_v_item_facets")
    legacy: dict[int, str] = {}
    taken: dict[str, int] = {}                      # 旧格式前缀 → 已用的最大序号（现行编号和全部别名）
    for alias, iid, kind in conn.execute(sa.text("SELECT alias, item_id, kind FROM kb_item_aliases ORDER BY alias")):
        m = _LEGACY.match(alias)
        if m:
            taken[m.group(1)] = max(taken.get(m.group(1), 0), int(m.group(2)))
            if kind == "legacy":
                legacy.setdefault(iid, alias)
    now = datetime.now(timezone.utc)
    rows = conn.execute(sa.text("SELECT id, doc_id, layer, domain, product, doc_type FROM kb_items ORDER BY id")).all()
    for iid, doc_id, layer, domain, product, doc_type in rows:
        if not _NEW.match(doc_id or ""):
            continue
        old = legacy.get(iid)
        if old is None:                             # 迁移后新建的条目：按主分类分配旧格式编号
            prefix = f"{layer}-{domain}-{product}-{doc_type}-"
            taken[prefix] = taken.get(prefix, 0) + 1
            old = f"{prefix}{taken[prefix]:03d}"
        conn.execute(sa.text("UPDATE kb_items SET doc_id = :d WHERE id = :i"), {"d": old, "i": iid})
        conn.execute(sa.text("INSERT INTO kb_item_aliases (alias, item_id, kind, created_at) "
                             "VALUES (:a, :i, 'legacy', :t)"), {"a": doc_id, "i": iid, "t": now})
    with op.batch_alter_table("kb_items") as b:
        b.drop_constraint("uq_kb_items_seq", type_="unique")
        b.drop_column("seq")
