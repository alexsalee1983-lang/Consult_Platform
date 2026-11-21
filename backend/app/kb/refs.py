"""知识编号的统一识别与解析（三期 B15 第一步，docs/三期方案/B15知识数据结构迁移方案.md 第 3 节）。

正文里引用知识库条目的标注只在这里识别：交付件来源解析（project/deliverables.py）、导出脚注（project/export.py）、
严格档占位符检查前的剔除（project/cards.py）都引用本模块，前端 frontend/src/kbRef.js 复制同一个模式字符串
（tests/test_kb_refs.py 校验两边一致，并禁止别处再写死编号正则）。

- 旧格式：L2-OPS-SOC-TPL-007（层级-目录-产品-类型-序号，中间 3～4 段）。现有数据全部是这种。
- 新格式：KB-000123（不带分类含义的稳定序号），B15 第三步换编号后才会出现，这里先预留识别。
- 标注写法：[编号]、【依据：…｜编号】、|编号】 等，前面是 [ ｜ |，后面是 ] 】。

resolve() 只按 kb_items.doc_id 批量核对（知识区库，不跨库 JOIN）；别名表由 B15 第二、三步加入后在这里补第二次查询。
可见性不在这里判断，调用方照旧按 visible_roles 过滤。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KbItem

LEGACY_PATTERN = r"L[1-5](?:-[A-Z0-9]{2,8}){3,4}-\d{3,5}"
NEW_PATTERN = r"KB-\d{6}"
REF_PATTERN = r"(?:" + LEGACY_PATTERN + r"|" + NEW_PATTERN + r")"
# 正文里的引用标注；第 1 组是编号
MARK_PATTERN = r"[\[｜|]\s*(" + REF_PATTERN + r")\s*[\]】]"

MARK_RE = re.compile(MARK_PATTERN)
_REF_FULL = re.compile(REF_PATTERN)
_LEGACY_FULL = re.compile(LEGACY_PATTERN)
BATCH = 500


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
    via: str                         # doc_id / alias（别名表在 B15 第二、三步加入）
    retired: bool
    current_version_id: int | None
    layer: str

    @property
    def usable(self) -> bool:
        """写入核对、导出脚注、签署门禁认的“核对得上”：未下线且有现行版本。"""
        return not self.retired and self.current_version_id is not None


def resolve(db: Session, refs: Iterable[str]) -> dict[str, Resolved]:
    """编号 → 条目。解析不到的不出现在结果里。按 BATCH 个一批 IN 查询。"""
    want = list(dict.fromkeys(r for r in refs if r))
    out: dict[str, Resolved] = {}
    for i in range(0, len(want), BATCH):
        chunk = want[i:i + BATCH]
        for item_id, doc_id, retired, cur, layer in db.execute(
                select(KbItem.id, KbItem.doc_id, KbItem.retired, KbItem.current_version_id, KbItem.layer)
                .where(KbItem.doc_id.in_(chunk))).all():
            out[doc_id] = Resolved(item_id=item_id, doc_id=doc_id, via="doc_id", retired=bool(retired),
                                   current_version_id=cur, layer=layer)
    return out


def canonical(db: Session, refs: Iterable[str]) -> dict[str, str]:
    """编号 → 现行编号，用来聚合计数。"""
    return {ref: r.doc_id for ref, r in resolve(db, refs).items()}
