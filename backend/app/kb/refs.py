"""知识编号的统一识别与解析（三期 B15 第一步，docs/三期方案/B15知识数据结构迁移方案.md 第 3 节）。

正文里引用知识库条目的标注只在这里识别：交付件来源解析（project/deliverables.py）、导出脚注（project/export.py）、
严格档占位符检查前的剔除（project/cards.py）都引用本模块，前端 frontend/src/kbRef.js 复制同一个模式字符串
（tests/test_kb_refs.py 校验两边一致，并禁止别处再写死编号正则）。

- 旧格式：L2-OPS-SOC-TPL-007（层级-目录-产品-类型-序号，中间 3～4 段）。现有数据全部是这种。
- 新格式：KB-000123（不带分类含义的稳定序号），B15 第三步换编号后才会出现，这里先预留识别。
- 标注写法：[编号]、【依据：…｜编号】、|编号】 等，前面是 [ ｜ |，后面是 ] 】。

resolve() 先按 kb_items.doc_id 批量核对，查不到的再按别名表 kb_item_aliases 批量查（B15 第二步，知识区迁移 k0010）；
两次查询都在知识区库里，不跨库 JOIN。别名只经 add_alias 写入：与现行编号互斥、不重复，永不删除（不提供删除函数）。
可见性不在这里判断，调用方照旧按 visible_roles 过滤。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KbItem, KbItemAlias

LEGACY_PATTERN = r"L[1-5](?:-[A-Z0-9]{2,8}){3,4}-\d{3,5}"
NEW_PATTERN = r"KB-\d{6}"
REF_PATTERN = r"(?:" + LEGACY_PATTERN + r"|" + NEW_PATTERN + r")"
# 正文里的引用标注；第 1 组是编号
MARK_PATTERN = r"[\[｜|]\s*(" + REF_PATTERN + r")\s*[\]】]"

MARK_RE = re.compile(MARK_PATTERN)
_REF_FULL = re.compile(REF_PATTERN)
_LEGACY_FULL = re.compile(LEGACY_PATTERN)
BATCH = 500
ALIAS_KINDS = ("legacy", "transfer", "merge")   # 换号 / 跨环境迁移时对方的编号 / 合并重复条目


class AliasError(ValueError):
    pass


def extract_refs(text: str) -> list[str]:
    """正文里引用的知识编号，按出现顺序去重。"""
    return list(dict.fromkeys(m.group(1) for m in MARK_RE.finditer(text or "")))


def is_ref(s: str) -> bool:
    return bool(_REF_FULL.fullmatch(s or ""))


def is_legacy(s: str) -> bool:
    return bool(_LEGACY_FULL.fullmatch(s or ""))


@dataclass(frozen=True)
class Resolved:
    item_id: int
    doc_id: str                      # 现行编号
    via: str                         # doc_id / alias
    retired: bool
    current_version_id: int | None
    layer: str

    @property
    def usable(self) -> bool:
        """写入核对、导出脚注、签署门禁认的“核对得上”：未下线且有现行版本。"""
        return not self.retired and self.current_version_id is not None


def _chunks(xs: list[str]):
    for i in range(0, len(xs), BATCH):
        yield xs[i:i + BATCH]


_COLS = (KbItem.id, KbItem.doc_id, KbItem.retired, KbItem.current_version_id, KbItem.layer)


def resolve(db: Session, refs: Iterable[str]) -> dict[str, Resolved]:
    """编号 → 条目。先按现行编号，查不到的再按别名；解析不到的不出现在结果里。按 BATCH 个一批 IN 查询。"""
    want = list(dict.fromkeys(r for r in refs if r))
    out: dict[str, Resolved] = {}
    for chunk in _chunks(want):
        for item_id, doc_id, retired, cur, layer in db.execute(select(*_COLS).where(KbItem.doc_id.in_(chunk))).all():
            out[doc_id] = Resolved(item_id=item_id, doc_id=doc_id, via="doc_id", retired=bool(retired),
                                   current_version_id=cur, layer=layer)
    rest = [r for r in want if r not in out]
    if rest:
        by_alias: dict[str, int] = {}
        for chunk in _chunks(rest):
            by_alias.update(db.execute(select(KbItemAlias.alias, KbItemAlias.item_id)
                                       .where(KbItemAlias.alias.in_(chunk))).tuples().all())
        items: dict[int, tuple] = {}
        ids = sorted(set(by_alias.values()))
        for i in range(0, len(ids), BATCH):
            items.update({row[0]: row for row in db.execute(
                select(*_COLS).where(KbItem.id.in_(ids[i:i + BATCH]))).all()})
        for alias, item_id in by_alias.items():
            if item_id in items:
                _, doc_id, retired, cur, layer = items[item_id]
                out[alias] = Resolved(item_id=item_id, doc_id=doc_id, via="alias", retired=bool(retired),
                                      current_version_id=cur, layer=layer)
    return out


def canonical(db: Session, refs: Iterable[str]) -> dict[str, str]:
    """编号 → 现行编号，用来聚合计数。"""
    return {ref: r.doc_id for ref, r in resolve(db, refs).items()}


def aliases_of(db: Session, item_id: int) -> list[str]:
    return list(db.scalars(select(KbItemAlias.alias).where(KbItemAlias.item_id == item_id)
                           .order_by(KbItemAlias.alias)))


def add_alias(db: Session, alias: str, item_id: int, kind: str) -> KbItemAlias:
    """登记别名（只 flush，调用方提交）。必须是编号格式、条目存在、不与任何现行编号或已有别名重复。"""
    alias = (alias or "").strip()
    if kind not in ALIAS_KINDS:
        raise AliasError(f"别名类型只能是 {'/'.join(ALIAS_KINDS)}")
    if not is_ref(alias):
        raise AliasError("别名必须是知识编号格式")
    if db.get(KbItem, item_id) is None:
        raise AliasError("条目不存在")
    if db.scalar(select(KbItem.id).where(KbItem.doc_id == alias)) is not None:
        raise AliasError("别名与现行编号冲突")
    if db.get(KbItemAlias, alias) is not None:
        raise AliasError("别名已登记")
    row = KbItemAlias(alias=alias, item_id=item_id, kind=kind)
    db.add(row)
    db.flush()
    return row
