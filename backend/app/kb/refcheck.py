"""知识引用校验基线 kb-ref-check（三期 B15，docs/三期方案/B15知识数据结构迁移方案.md 第 5 节）。

换编号、加别名等知识区迁移前后各跑一次快照并对比，证明主库里存的旧引用不改写也照样解析得到。
只读，不调模型，不联网。主库（交付件、对话）和知识区库（条目、版本）分开查，在内存里合并，不跨库 JOIN。

扫描范围（每个引用一条记录）：
- stored：章节版本 section_versions.sources.knowledge 里存的 ref；
- scanned：用 app/kb/refs.py 重扫章节正文得到的 ref（证明新正则对旧正文的抽取结果不变）；
- message：对话消息 messages.kb_refs[].doc_id；
- export：已签署交付件（最新一次签署的版本快照）的导出脚注标签 {ref: 标签} 的哈希（不生成 Word）。
已签署的章节版本 signed=true。--signed-only 只扫已签署交付件。

记录只含编号、项目和交付件的数字 id、章节号、版本号、条目 id 和状态，不含正文和标题（红线第 4 条）；
快照文件里有项目 id，只放在测试机或部署机本地，不提交，PR 评论只贴计数。审计 kb.ref_check 只记数量和快照哈希。

对比规则（compare）：之前解析得到（item_id 非空）→ 之后必须解析到同一个 item_id，且 retired、current_version 不变；
已签署章节版本的引用集合前后必须相等；导出标签哈希必须相同；违反的都算“退化”，退化 > 0 时命令退出码 2。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import utcnow
from ..models import Deliverable, DeliverableSection, SectionVersion
from ..models.conversation import Conversation, Message
from ..models.quality import SignOff
from . import refs

STORED, SCANNED, MESSAGE, EXPORT = "stored", "scanned", "message", "export"
FORMAT = 1


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _signed_versions(db: Session) -> dict[int, dict[str, int]]:
    """交付件 id → 最新一次签署的 {"章节号": 版本号}。"""
    out: dict[int, dict[str, int]] = {}
    for did, versions in db.execute(select(SignOff.deliverable_id, SignOff.versions)
                                    .where(SignOff.action == "sign").order_by(SignOff.id)).all():
        out[did] = {str(k): int(v or 0) for k, v in (versions or {}).items()}
    return out


def snapshot(db: Session, signed_only: bool = False) -> dict:
    from ..project.export import kb_labels          # 导出脚注的标签生成，和真正导出用同一个函数

    signed = _signed_versions(db)
    entries: list[dict] = []
    signed_stored: dict[tuple[int, int], set[str]] = defaultdict(set)   # (项目, 交付件) → 签署版本里存的 ref
    rows = db.execute(
        select(Deliverable.project_id, Deliverable.id, DeliverableSection.seq, SectionVersion.version,
               SectionVersion.body, SectionVersion.sources)
        .join(DeliverableSection, DeliverableSection.id == SectionVersion.section_id)
        .join(Deliverable, Deliverable.id == DeliverableSection.deliverable_id)
        .order_by(SectionVersion.id)).all()
    for pid, did, seq, version, body, sources in rows:
        is_signed = did in signed and signed[did].get(str(seq)) == version
        if signed_only and not is_signed:
            continue
        stored = list(dict.fromkeys(k.get("ref", "") for k in (sources or {}).get("knowledge", []) if k.get("ref")))
        base = {"project_id": pid, "deliverable_id": did, "section_seq": seq, "version": version,
                "message_id": None, "signed": is_signed}
        entries += [{"source_kind": STORED, "ref": r, **base} for r in stored]
        entries += [{"source_kind": SCANNED, "ref": r, **base} for r in refs.extract_refs(body)]
        if is_signed:
            signed_stored[(pid, did)] |= set(stored)
    if not signed_only:
        for mid, pid, kb_refs in db.execute(
                select(Message.id, Conversation.project_id, Message.kb_refs)
                .join(Conversation, Conversation.id == Message.conversation_id).order_by(Message.id)).all():
            for r in dict.fromkeys(x.get("doc_id", "") for x in (kb_refs or []) if isinstance(x, dict)):
                if r:
                    entries.append({"source_kind": MESSAGE, "ref": r, "project_id": pid, "deliverable_id": None,
                                    "section_seq": None, "version": None, "message_id": mid, "signed": False})

    resolved = refs.resolve(db, {e["ref"] for e in entries})
    for e in entries:
        r = resolved.get(e["ref"])
        e.update(item_id=r.item_id if r else None, current_doc_id=r.doc_id if r else None,
                 via=r.via if r else None, retired=r.retired if r else None,
                 current_version=r.current_version_id if r else None)
    for (pid, did), docs in sorted(signed_stored.items()):
        entries.append({"source_kind": EXPORT, "ref": "", "project_id": pid, "deliverable_id": did,
                        "section_seq": None, "version": None, "message_id": None, "signed": True,
                        "labels_sha": _sha(kb_labels(db, docs) if docs else {})})
    return {"format": FORMAT, "created_at": utcnow().isoformat(), "signed_only": signed_only,
            "counts": counts(entries), "entries": entries}


def counts(entries: list[dict]) -> dict:
    c: dict[str, int] = defaultdict(int)
    for e in entries:
        k = e["source_kind"]
        c[k] += 1
        if k != EXPORT:
            c["found" if e.get("item_id") is not None else "unresolved"] += 1
            if e.get("signed") and e.get("item_id") is None:
                c["signed_unresolved"] += 1
    return dict(c)


def snapshot_sha(snap: dict) -> str:
    return _sha(snap["entries"])


def _key(e: dict) -> tuple:
    return (e["source_kind"], e.get("project_id"), e.get("deliverable_id"), e.get("section_seq"), e.get("version"),
            e.get("message_id"), e.get("ref", ""))


@dataclass
class CompareResult:
    consistent: int = 0
    both_missing: int = 0             # 前后都解析不到（留给人工）
    regressed: int = 0                # 退化：之前解析得到、之后解析不到或条目变了；签署引用集合变了；导出标签变了
    new_found: int = 0
    removed: int = 0                  # 未签署的记录不见了（例如项目已删除），不算退化
    regressed_refs: list[str] = field(default_factory=list)
    missing_refs: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"consistent": self.consistent, "both_missing": self.both_missing, "regressed": self.regressed,
                "new_found": self.new_found, "removed": self.removed}


def compare(before: dict, after: dict) -> CompareResult:
    res = CompareResult()
    b_entries = before.get("entries", [])
    if after.get("signed_only"):
        b_entries = [e for e in b_entries if e.get("signed")]
    b = {_key(e): e for e in b_entries}
    a = {_key(e): e for e in after.get("entries", [])}
    regressed: set[str] = set()
    missing: set[str] = set()

    def bad(e: dict) -> None:
        res.regressed += 1
        regressed.add(e.get("ref") or f"导出:交付件{e.get('deliverable_id')}")

    for k, old in b.items():
        new = a.get(k)
        if new is None:
            if old.get("signed"):
                bad(old)
            else:
                res.removed += 1
        elif old["source_kind"] == EXPORT:
            if old.get("labels_sha") == new.get("labels_sha"):
                res.consistent += 1
            else:
                bad(old)
        elif old.get("item_id") is not None:
            same = (new.get("item_id") == old["item_id"] and new.get("retired") == old.get("retired")
                    and new.get("current_version") == old.get("current_version"))
            if same:
                res.consistent += 1
            else:
                bad(old)
        elif new.get("item_id") is not None:
            res.new_found += 1
        else:
            res.both_missing += 1
            missing.add(old["ref"])
    # 已签署章节版本的引用集合必须相等：之前就签署了的版本多出引用也算退化（正则抽取结果变了）
    signed_before = {(e.get("deliverable_id"), e.get("section_seq"), e.get("version"))
                     for e in b_entries if e.get("signed") and e["source_kind"] != EXPORT}
    for k, new in a.items():
        if k not in b and new.get("signed") and new["source_kind"] != EXPORT and (
                new.get("deliverable_id"), new.get("section_seq"), new.get("version")) in signed_before:
            bad(new)
    res.regressed_refs = sorted(regressed)
    res.missing_refs = sorted(missing)
    return res
