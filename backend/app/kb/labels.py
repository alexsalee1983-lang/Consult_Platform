"""知识条目多值标签（三期 B15，知识区迁移 k0011，表 kb_item_labels）。

kind：product 服务产品 / industry 行业 / control 控制项 / capability 能力域 / domain 一级目录 / tag 检索标签。
规则：
- 产品取值在 meta.PRODUCTS 里，可以多个；GEN 表示通用，是显式取值，不能和具体产品同时出现。
- 行业取值在 meta.INDUSTRIES 里，可以多个；“通用”不能和具体行业同时出现。
- 标签是条目级的，不进版本内容、不走审核；产品和行业改动记审计 kb.labels（只记 kind、取值和条目编号）。
- 只有本模块写 kb_item_labels；读取以本表为准，kb_items 上的 product、domain、industries 等旧列只双写，
  给回滚和旧报表用（product 列 = 主产品，即产品列表的第一个）。
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..core import audit
from ..models import KbItem, KbItemLabel
from . import meta

KINDS = ("product", "industry", "control", "capability", "domain", "tag")
GENERAL_PRODUCT, GENERAL_INDUSTRY = "GEN", "通用"


def _clean(values: list[str] | tuple[str, ...] | None) -> list[str]:
    return list(dict.fromkeys(str(v).strip() for v in values or [] if str(v).strip()))


def check_products(primary: str, products: list[str] | None) -> list[str]:
    """主产品在前、去重；取值必须在 PRODUCTS 里，GEN 不能和具体产品同时出现。返回规整后的列表。"""
    out = _clean([primary, *(products or [])])
    bad = [p for p in out if p not in meta.PRODUCTS]
    if bad:
        raise meta.MetaError(f"服务产品取值不对：{'、'.join(bad)}，可选 {', '.join(meta.PRODUCTS)}")
    if GENERAL_PRODUCT in out and len(out) > 1:
        raise meta.MetaError("服务产品 GEN（通用）不能和具体产品同时选")
    return out


def check_industries(industries: list[str] | None) -> list[str]:
    """去空白、去重；取值必须在 INDUSTRIES 里，“通用”不能和具体行业同时出现。返回规整后的列表。"""
    out = _clean(industries)
    if not out:
        raise meta.MetaError("适用行业至少填一个；通用的填“通用”")
    bad = [i for i in out if i not in meta.INDUSTRIES]
    if bad:
        raise meta.MetaError(f"适用行业取值不对：{'、'.join(bad)}，可选 {'、'.join(meta.INDUSTRIES)}")
    if GENERAL_INDUSTRY in out and len(out) > 1:
        raise meta.MetaError("适用行业“通用”不能和具体行业同时选")
    return out


def _wanted(item: KbItem, products: list[str]) -> set[tuple[str, str]]:
    pairs = [("product", p) for p in products] + [("domain", item.domain)]
    pairs += [("industry", v) for v in item.industries or []] + [("capability", v) for v in item.capabilities or []]
    pairs += [("tag", v) for v in item.tags or []] + [("control", v) for v in item.control_ids or []]
    return {(k, str(v)[:64]) for k, v in pairs if str(v).strip()}


def products_of(db: Session, item_ids: list[int] | set[int]) -> dict[int, list[str]]:
    """条目 → 产品列表（主产品在前）。没有标签的条目不出现在结果里。"""
    if not item_ids:
        return {}
    out: dict[int, list[str]] = {}
    primary = dict(db.execute(select(KbItem.id, KbItem.product).where(KbItem.id.in_(item_ids))).all())
    for iid, value in db.execute(select(KbItemLabel.item_id, KbItemLabel.value)
                                 .where(KbItemLabel.item_id.in_(item_ids), KbItemLabel.kind == "product")
                                 .order_by(KbItemLabel.item_id, KbItemLabel.value)):
        out.setdefault(iid, []).append(value)
    for iid, vals in out.items():
        if primary.get(iid) in vals:
            vals.remove(primary[iid])
            vals.insert(0, primary[iid])
    return out


def sync(db: Session, item: KbItem, products: list[str] | None = None, *, source: str = "manual",
         actor: str = "") -> set[str]:
    """按条目旧列（和传入的产品列表）重写该条目的标签，返回发生变化的 kind。

    products 为 None 时沿用已有的产品标签（没有就取主产品列）。产品或行业有变化、且传了 actor 时记审计。
    调用方负责提交；本函数会 flush。"""
    db.flush()
    have = {(r.kind, r.value): r for r in db.scalars(select(KbItemLabel).where(KbItemLabel.item_id == item.id))}
    if products is None:
        products = products_of(db, [item.id]).get(item.id) or [item.product]
    products = check_products(products[0], products)
    item.product = products[0]
    want = _wanted(item, products)
    gone, new = set(have) - want, want - set(have)
    if gone:
        for kind, value in gone:
            db.execute(delete(KbItemLabel).where(KbItemLabel.item_id == item.id, KbItemLabel.kind == kind,
                                                 KbItemLabel.value == value))
    db.add_all(KbItemLabel(item_id=item.id, kind=k, value=v, source=source) for k, v in sorted(new))
    db.flush()
    changed = {k for k, _ in gone | new}
    if actor and changed & {"product", "industry"} and have:
        audit.record(db, actor=actor, action="kb.labels", result="allowed", doc_id=item.doc_id,
                     products=sorted(products), industries=sorted(v for k, v in want if k == "industry"))
    return changed


def set_products(db: Session, item: KbItem, products: list[str], *, actor: str) -> set[str]:
    """改条目的产品（主产品在前），记审计。调用方负责提交。"""
    return sync(db, item, check_products(products[0] if products else "", products), actor=actor)
