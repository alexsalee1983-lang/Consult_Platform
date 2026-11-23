"""三期 B15：知识条目多值标签表 kb_item_labels，从 kb_items 旧列回填。

kind：product / industry / control / capability / domain / tag；回填的 source='legacy'。
回填逐行用 Python 处理，不写方言 SQL；回填后自检：每个条目至少 1 个 product、1 个 industry 标签，否则迁移失败回滚。
行业取值不在 meta.INDUSTRIES 里的照样回填（不阻断），由 kb-ref-check --facets 出报告。
downgrade：把标签回写到旧列（多产品的条目只保留 product 列原有的主产品，多出来的产品丢失，打印受影响条目数），再删表。

Revision ID: k0011_kb_labels
Revises: k0010_kb_aliases
Create Date: 2026-11-22
"""
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0011_kb_labels"
down_revision: str | None = "k0010_kb_aliases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIST_COLS = (("industries", "industry"), ("capabilities", "capability"), ("tags", "tag"), ("control_ids", "control"))


def _as_list(v: object) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        v = json.loads(v)
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def upgrade() -> None:
    op.create_table(
        "kb_item_labels",
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.ForeignKeyConstraint(["item_id"], ["kb_items.id"], name="fk_kb_item_labels_item_id", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id", "kind", "value", name="pk_kb_item_labels"),
    )
    op.create_index("ix_kb_item_labels_kind_value", "kb_item_labels", ["kind", "value", "item_id"])

    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, product, domain, industries, capabilities, tags, control_ids FROM kb_items ORDER BY id")).mappings()
    out: list[dict] = []
    bad_product, bad_industry = [], []
    for r in rows:
        seen: set[tuple[str, str]] = set()
        pairs = [("product", (r["product"] or "").strip()), ("domain", (r["domain"] or "").strip())]
        for col, kind in _LIST_COLS:
            vals = _as_list(r[col])
            if kind == "control":
                vals = [v.upper() for v in vals]
            pairs += [(kind, v) for v in vals]
        for kind, value in pairs:
            if value and (kind, value[:64]) not in seen:
                seen.add((kind, value[:64]))
                out.append({"item_id": r["id"], "kind": kind, "value": value[:64], "source": "legacy"})
        if not any(k == "product" for k, _ in seen):
            bad_product.append(r["id"])
        if not any(k == "industry" for k, _ in seen):
            bad_industry.append(r["id"])
    if bad_product or bad_industry:
        raise RuntimeError(f"k0011 回填自检失败：缺产品标签的条目 {len(bad_product)} 个、缺行业标签的条目 "
                           f"{len(bad_industry)} 个（条目 id 前 10 个：{(bad_product + bad_industry)[:10]}）")
    table = sa.table("kb_item_labels", sa.column("item_id"), sa.column("kind"), sa.column("value"),
                     sa.column("source"))
    for i in range(0, len(out), 1000):
        op.bulk_insert(table, out[i:i + 1000])


def downgrade() -> None:
    conn = op.get_bind()
    labels: dict[int, dict[str, list[str]]] = {}
    for item_id, kind, value in conn.execute(sa.text(
            "SELECT item_id, kind, value FROM kb_item_labels ORDER BY item_id, kind, value")):
        labels.setdefault(item_id, {}).setdefault(kind, []).append(value)
    items = sa.table("kb_items", sa.column("id", sa.Integer), *(sa.column(c, sa.JSON) for c, _ in _LIST_COLS))
    lost = 0
    for item_id, by_kind in labels.items():
        if len(by_kind.get("product", [])) > 1:
            lost += 1
        conn.execute(items.update().where(items.c.id == item_id)
                     .values({col: by_kind.get(kind, []) for col, kind in _LIST_COLS}))
    if lost:
        print(f"k0011 downgrade：{lost} 个条目挂了多个产品，只保留 product 列的主产品")  # noqa: T201
    op.drop_index("ix_kb_item_labels_kind_value", table_name="kb_item_labels")
    op.drop_table("kb_item_labels")
