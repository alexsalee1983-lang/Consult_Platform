"""多语言与海外法规最小版（四期 B28①，三期开发计划 B28 最小版）：原文存档、译文标“非官方译本”、引用以原文为准。

- 条目级字段（知识区迁移 k0021）：language 原文语种、jurisdiction 辖区（取值都是配置 KB_LANGUAGES、KB_JURISDICTIONS，
  不写死）；译文条目 translation_of 挂原文条目编号、translation_notice 必须是“非官方译本”。
- 原文：原文语种的条目本身就是存档（正文进知识区，原件走投料箱 / 依据原文入库，文件经 storage.py），译文是挂在它上面的
  另一条目；译文不能再挂译文，原文必须存在、未下线，语种与译文不同。
- 拦截：导入与页面建条目（ItemMeta.check + review.new_item）、提交审核（review.submit）、发布（review.publish）都拦，
  缺标记或原文不对的译文进不了现行知识。
- 回溯（trace）：交付件、对话引用译文条目时，给出原文条目编号和原文标题（标题带条款号）；导出脚注据此旁注
  “非官方译本，以原文为准”（app/project/export.py kb_labels）。
- 不调模型、不联网、不做自动翻译（翻译如需要只经 gateway，另立工作项）。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models.kb import KbItem, KbVersion
from . import meta

TRANSLATION_NOTICE = "非官方译本"
EXPORT_NOTE = "非官方译本，以原文为准"


def _codes(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x.strip() for x in raw.split(",") if x.strip()))


def languages() -> tuple[str, ...]:
    return _codes(get_settings().kb_languages)


def jurisdictions() -> tuple[str, ...]:
    return _codes(get_settings().kb_jurisdictions)


def check_meta(language: object, jurisdiction: object, translation_of: object,
               notice: object) -> tuple[str, str, str, str]:
    """不碰库的校验，返回规整后的四个字段；不合格抛 MetaError。"""
    lang, jur = str(language or "zh").strip(), str(jurisdiction or "CN").strip().upper()
    orig, note = str(translation_of or "").strip(), str(notice or "").strip()
    if lang not in languages():
        raise meta.MetaError(f"语种取值不对：{lang}，可选 {'、'.join(languages())}（配置 KB_LANGUAGES）")
    if jur not in jurisdictions():
        raise meta.MetaError(f"辖区取值不对：{jur}，可选 {'、'.join(jurisdictions())}（配置 KB_JURISDICTIONS）")
    if orig and note != TRANSLATION_NOTICE:
        raise meta.MetaError(f"译文条目必须标“{TRANSLATION_NOTICE}”（translation_notice），引用条款以原文为准")
    if note and not orig:
        raise meta.MetaError("标了“非官方译本”的条目必须写对应的原文条目编号（translation_of）")
    return lang, jur, orig, note


def check_link(db: Session, translation_of: str, language: str, self_doc_id: str = "") -> None:
    """译文挂的原文：必须存在、未下线、本身不是译文、语种与译文不同。"""
    if not translation_of:
        return
    from . import refs

    r = refs.resolve(db, [translation_of]).get(translation_of)
    orig = db.get(KbItem, r.item_id) if r else None
    if orig is None or orig.retired:
        raise meta.MetaError(f"译文挂的原文条目 {translation_of} 不存在或已下线：先入库原文再导入译文")
    if orig.doc_id == self_doc_id or orig.translation_of:
        raise meta.MetaError(f"{translation_of} 本身是译文，译文必须直接挂原文条目")
    if orig.language == language:
        raise meta.MetaError(f"译文语种（{language}）与原文 {orig.doc_id} 相同，不是译文请去掉 translation_of")


def check_item(db: Session, item: KbItem) -> None:
    """提交、发布前对条目整体复核（防止绕过导入直接改库）。"""
    check_meta(item.language, item.jurisdiction, item.translation_of, item.translation_notice)
    check_link(db, item.translation_of, item.language, item.doc_id)


@dataclass(frozen=True)
class Trace:
    doc_id: str                 # 被引用的译文条目（现行编号）
    original_doc_id: str
    original_title: str         # 原文现行版本标题（含条款号）
    original_document: str      # 原文文件名（依据类有）
    original_code: str          # 原文文号或标准号


def trace(db: Session, docs: Iterable[str]) -> dict[str, Trace]:
    """引用编号 → 原文回溯；只返回译文条目（原文条目、解析不到的不出现在结果里）。"""
    from . import refs

    found = refs.resolve(db, docs)
    if not found:
        return {}
    links: dict[int, str] = dict(db.execute(select(KbItem.id, KbItem.translation_of).where(
        KbItem.id.in_({r.item_id for r in found.values()}), KbItem.translation_of != "")).tuples().all())
    if not links:
        return {}
    origs = refs.resolve(db, set(links.values()))
    vids = {o.current_version_id for o in origs.values() if o.current_version_id}
    vers = {v.id: v for v in db.scalars(select(KbVersion).where(KbVersion.id.in_(vids)))} if vids else {}
    out: dict[str, Trace] = {}
    for ref, r in found.items():
        orig_ref = links.get(r.item_id)
        o = origs.get(orig_ref) if orig_ref else None
        if o is None:
            continue
        v = vers.get(o.current_version_id or 0)
        out[ref] = Trace(doc_id=r.doc_id, original_doc_id=o.doc_id, original_title=v.title if v else "",
                         original_document=v.ref_document if v else "", original_code=v.ref_code if v else "")
    return out
