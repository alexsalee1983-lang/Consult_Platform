"""四期 B23①：专家工作台二批——审核问答化、按类型填空卡、大白话界面（总体规划第三节第 6 条；在 B17 确认队列上扩展）。

- 问答卡：本人能做内容审核的待审版本（知识专家、不是作者、内容审核还没人通过），沿用“我的任务”的
  影响 × 不确定度排序和每日额度。每张卡把审核拆成几道“对 / 不对 / 不确定”的问题：
  keep 值不值得收；field:<字段> 依据文件、版本、文号、适用条件、复查日期写得对不对（空着的问“知道就填”）；
  issue:<n> 知识审核 Agent 提的问题认不认。
- 回答换成审核决定（规则全部沿用 review.review / practice_queue.decide，不另立审核规则）：
  keep=no → 驳回；keep=unsure → 不拍板（只记审计）；有字段改正或填空卡改了正文 → 修改后通过；其余 → 通过。
  认可 Agent 提的问题却没改正文 → 422（请用填空卡改正文或驳回）；答“不对”却没给改正 → 422。
- 填空卡：按知识类型和交付物类型给几格短填空，专家可直接贴零散文字；拼成“## 标题 + 内容”的正文，过机密级检测。
- 大白话：状态、字段、把握程度一律换成日常说法（PLAIN），界面只显示这些说法。
- 审计只记编号、结果、改了哪些字段和回答计数，不记回答原文。无迁移。

四期 B23②：冲突处理卡、去标识预览（无迁移）。
- 冲突处理卡：知识审核 Agent 的“不重复、不冲突”一项没过时，卡上列出可能冲突的已发布条目（理由里写到的 KB 编号，
  PostgreSQL 上再加同产品相近条目前 3 条），多一道必答题 conflict：
  both 不冲突、两条都留（照常拍板）；new 以这条为准（照常拍板，收下后把已有条目的现行版本标待复查，复用 B13）；
  old 以已有的为准（这条退回）；unsure 拿不准（不拍板）。
- 去标识预览：专家自己写的文字（填空卡、不收的理由、备注）先按投料去标识的规则（app/kb/deid.py）换占位符给专家看，
  只在内存里做，不保存、不写审计；机密级命中只返回类别。交上去时这些文字里还有可识别信息，要专家看过预览、
  确认不是客户信息（deid_ack）才收，否则 409；审计只记命中个数。
"""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import KNOWLEDGE_EXPERT, roles_of
from ..models import KbItem, KbVersion, User
from ..project import my_tasks as mt
from ..services import guard
from . import deid, meta, refs
from . import practice_queue as pq
from . import review as rv
from .items import VersionContent, replace_mapping_rows
from .recheck import OPEN_STATES

CHOICES = ("yes", "no", "unsure")
CONFLICT_CHOICES = {"both": "不冲突，两条都留", "new": "以这条为准，已有的标待复查", "old": "以已有的为准，这条不收",
                    "unsure": "拿不准，先不定"}
_REF_RE = re.compile(refs.REF_PATTERN)                # 知识编号正则只在 refs.py 里写
MAX_CONFLICTS = 5
MAX_PREVIEW_KEYS = 12
MAX_VALUE = 500

PLAIN: dict[str, str] = {
    "draft": "草稿，还没交",
    "in_review": "等你看",
    "published": "已生效，顾问能检索到",
    "rejected": "已退回",
    "approve": "收下",
    "modify": "改了再收",
    "reject": "不收，退回",
    "undecided": "先不定，留着",
    "ref_document": "依据的文件",
    "ref_version": "文件版本",
    "ref_code": "文号或标准号",
    "applicability": "什么情况下适用",
    "review_date": "下次复查日期",
    "source_detail": "从哪来的",
    "confidence": "Agent 的把握",
    "issues": "Agent 觉得还缺的",
}

# 问答卡里要核对的字段：(字段名, 大白话标签)
FIELDS: tuple[tuple[str, str], ...] = tuple((n, PLAIN[n]) for n in
                                           ("ref_document", "ref_version", "ref_code", "applicability", "review_date"))
FIELD_LIMITS = {"ref_document": 200, "ref_version": 64, "ref_code": 100, "applicability": 500}

# 填空卡：key → (名称, [(格子键, 标题, 提示, 必填)])
TEMPLATES: dict[str, tuple[str, list[tuple[str, str, str, bool]]]] = {
    "practice": ("公司做法", [("how", "公司怎么做", "一两句话或几条要点，直接贴零散文字也行", True),
                            ("why", "为什么这样做", "选填", False),
                            ("except", "什么情况下例外", "选填", False),
                            ("pitfall", "容易踩的坑", "选填", False)]),
    "golden": ("黄金样例", [("good", "这份样例好在哪", "", True),
                          ("scene", "用在什么场景", "", True),
                          ("avoid", "要避开的写法", "选填", False)]),
    "PIT": ("常见坑", [("symptom", "会出现什么问题", "", True),
                     ("cause", "为什么会这样", "", False),
                     ("avoid", "怎么避免", "", True),
                     ("scope", "什么情况下适用", "选填", False)]),
    "MTD": ("方法", [("steps", "怎么做（步骤）", "一行一步", True),
                   ("input", "要准备什么", "选填", False),
                   ("output", "做完得到什么", "选填", False),
                   ("note", "要注意什么", "选填", False)]),
    "CAS": ("案例", [("background", "背景", "只写虚拟客户代号，不写真实客户", True),
                   ("action", "怎么做的", "", True),
                   ("result", "结果怎样", "", False),
                   ("reuse", "能复用的经验", "选填", False)]),
    "BMK": ("经验数值", [("value", "数值或区间", "例如 每 2 万 EPS 配 1 名分析员", True),
                       ("scope", "什么情况下适用", "", True),
                       ("basis", "依据或来源", "选填", False)]),
    "default": ("通用", [("points", "要点", "", True),
                       ("scope", "什么情况下适用", "选填", False),
                       ("basis", "依据或来源", "选填", False)]),
}
_CN_DIGITS = "零一二三四五六七八九十"


def template_for(knowledge_kind: str, doc_type: str) -> dict:
    key = ("golden" if knowledge_kind == "golden" or doc_type == "GLD" else doc_type if doc_type in TEMPLATES
           else "practice" if knowledge_kind == "practice" else "default")
    name, slots = TEMPLATES[key]
    return {"key": key, "name": name,
            "slots": [{"key": k, "label": label, "hint": hint, "required": req} for k, label, hint, req in slots]}


def _tidy(text: str) -> str:
    """零散文字整理：去掉首尾空白和空行，保留换行。"""
    return "\n".join(line.strip() for line in str(text or "").splitlines() if line.strip())


def compose(template: str, fill: dict[str, str]) -> str:
    if template not in TEMPLATES:
        raise rv.ReviewError(422, "没有这个填空卡")
    _, slots = TEMPLATES[template]
    missing = [label for k, label, _, req in slots if req and not _tidy(fill.get(k, ""))]
    if missing:
        raise rv.ReviewError(422, f"还要填：{'、'.join(missing)}")
    parts = [f"## {label}\n\n{_tidy(fill[k])}" for k, label, _, _ in slots if _tidy(fill.get(k, ""))]
    body = "\n\n".join(parts)
    rv._check_text("", body)                 # 机密级检测，命中 422
    return body


def _confidence_plain(raw: str) -> str:
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return f"Agent 的把握：{raw}" if raw else "Agent 没说有几成把握"
    if not 0 <= x <= 1:
        return "Agent 没说有几成把握"
    n = round(x * 10)
    return "Agent 有十足把握" if n == 10 else f"Agent 有{_CN_DIGITS[n]}成把握"


def _no_conflict_failed(v: KbVersion) -> tuple[bool, str]:
    agent = [r for r in v.reviews if r.review_role == "agent"]
    if not agent:
        return False, ""
    f = max(agent, key=lambda x: x.id).findings
    c = ((f or {}).get("checks") or {}).get("no_conflict") if isinstance(f, dict) else None
    if not isinstance(c, dict) or c.get("pass", True):
        return False, ""
    return True, str(c.get("reason") or "")[:300]


def _live(db: Session, doc_id: str) -> tuple[KbItem | None, KbVersion | None]:
    item = db.query(KbItem).filter(KbItem.doc_id == doc_id).one_or_none()
    cur = db.get(KbVersion, item.current_version_id) if item and item.current_version_id else None
    if item is None or item.retired or cur is None or cur.status != "published":
        return item, None
    return item, cur


def conflict_of(db: Session, v: KbVersion) -> dict | None:
    """Agent 判“可能重复或冲突”时给冲突处理卡：理由 + 可能冲突的已发布条目（编号、标题、摘要）。"""
    failed, reason = _no_conflict_failed(v)
    if not failed:
        return None
    found = refs.resolve(db, _REF_RE.findall(reason))         # 旧编号、别名换成现行编号
    ids = list(dict.fromkeys(r.doc_id for r in found.values()))
    ids += [x["doc_id"] for x in rv.similar_items(db, v, limit=3) if x["doc_id"] not in ids]
    items = []
    for doc_id in ids:
        item, cur = _live(db, doc_id)
        if cur is None or item.id == v.item_id:
            continue
        items.append({"doc_id": doc_id, "version": cur.version, "title": cur.title,
                      "summary": cur.summary or cur.body[:200]})
        if len(items) >= MAX_CONFLICTS:
            break
    return {"reason": reason, "items": items}


def _questions(v: KbVersion, unsure: set[str], issues: list[str], conflict: dict | None = None) -> list[dict]:
    kind = meta.KNOWLEDGE_KINDS.get(v.item.knowledge_kind, "") or "知识"
    qs = [{"id": "keep", "kind": "keep", "text": f"这条值得收进知识库吗？（{kind}）", "empty": False,
           "uncertain": False, "value": ""}]
    if conflict is not None:
        n = len(conflict["items"])
        qs.append({"id": "conflict", "kind": "conflict", "empty": False, "uncertain": True, "value": "",
                   "text": f"Agent 觉得这条和已有的 {n} 条可能重复或矛盾，怎么处理？" if n
                   else "Agent 觉得这条和已有条目可能重复或矛盾，怎么处理？", "options": CONFLICT_CHOICES})
    for name, label in FIELDS:
        val = getattr(v, name)
        text = "" if val is None else str(val)
        q = f"{label}写的是“{text}”，对吗？" if text else f"{label}还空着，知道的话填一下（不知道就选“不确定”）"
        qs.append({"id": f"field:{name}", "kind": "field", "text": q, "empty": not text,
                   "uncertain": name in unsure or not text, "value": text})
    for i, s in enumerate(issues):
        qs.append({"id": f"issue:{i}", "kind": "issue", "text": f"Agent 觉得有问题，你认可吗：{s}", "empty": False,
                   "uncertain": False, "value": ""})
    return qs


def _content_pending(v: KbVersion) -> bool:
    return "content" in rv.required_roles(v) and not any(
        r.review_role == "content" and r.decision == "approve" for r in v.reviews)


def require_expert(db: Session, user: User) -> None:
    if KNOWLEDGE_EXPERT not in roles_of(db, user.id):
        raise rv.ReviewError(403, "专家工作台只给知识专家")


def cards(db: Session, user: User) -> dict:
    require_expert(db, user)
    w = mt.weights()
    tasks = [t for t in mt.collect(db, user) if t.kind == "kb_content" and not t.blocked]
    ranked = mt.rank(tasks, w)
    folded = mt.apply_quota(ranked, w.quota)
    out = []
    for t in ranked:
        v = db.get(KbVersion, t.ref_id)
        if v is None or not _content_pending(v):
            continue
        _, unsure = mt._kb_uncertainty(v)
        basis, confidence, issues = pq._agent_opinion(v)
        conflict = conflict_of(db, v)
        out.append({"version_id": v.id, "doc_id": v.item.doc_id, "version": v.version, "title": v.title,
                    "summary": v.summary, "body": v.body, "status_plain": PLAIN["in_review"],
                    "kind_plain": meta.KNOWLEDGE_KINDS.get(v.item.knowledge_kind, "") or "通用",
                    "doc_type": v.item.doc_type, "source_detail": v.source_detail, "basis": basis,
                    "confidence_plain": _confidence_plain(confidence), "issues": issues,
                    "score": t.score, "folded": t.folded,
                    "conflict": conflict, "questions": _questions(v, unsure, issues, conflict),
                    "template": template_for(v.item.knowledge_kind, v.item.doc_type)})
    return {"quota": w.quota, "folded": folded, "plain": PLAIN, "cards": out}


def _parse_answers(v: KbVersion, answers: list[dict], issues: list[str],
                   has_conflict: bool = False) -> tuple[dict, dict, str, list[int], str]:
    """返回 (keep 回答, 字段改正, 驳回理由, 认可的问题序号, 冲突回答)；不合法 422。"""
    valid = {"keep"} | {f"field:{n}" for n, _ in FIELDS} | {f"issue:{i}" for i in range(len(issues))}
    keep: dict = {}
    changes: dict = {}
    agreed: list[int] = []
    conflict = ""
    for a in answers:
        qid, choice, value = str(a.get("id", "")), str(a.get("choice", "")), _tidy(a.get("value", ""))[:MAX_VALUE]
        if qid == "conflict" and has_conflict:
            if choice not in CONFLICT_CHOICES:
                raise rv.ReviewError(422, "冲突处理只能选：都留、以这条为准、以已有的为准、拿不准")
            conflict = choice
            continue
        if qid not in valid or choice not in CHOICES:
            raise rv.ReviewError(422, "回答里有不认识的问题或选项")
        if qid == "keep":
            keep = {"choice": choice, "value": value}
        elif qid.startswith("issue:"):
            if choice == "yes":
                agreed.append(int(qid.split(":")[1]))
        else:
            name = qid.split(":", 1)[1]
            if choice == "no" and not value:
                raise rv.ReviewError(422, f"“{PLAIN[name]}”答了不对，请写上对的")
            if choice == "unsure" or not value:
                continue
            old = getattr(v, name)
            if choice == "yes" and old not in (None, ""):
                continue                                  # 对，不改
            if name == "review_date":
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise rv.ReviewError(422, "下次复查日期请写成 2027-06-30 这样")
                try:
                    changes[name] = date.fromisoformat(value)
                except ValueError as e:
                    raise rv.ReviewError(422, "下次复查日期不是有效日期") from e
            else:
                changes[name] = value[:FIELD_LIMITS[name]]
    if has_conflict and not conflict:
        raise rv.ReviewError(422, "请先回答冲突处理：和已有条目重复或矛盾时怎么办")
    return keep, changes, keep.get("value", ""), agreed, conflict


def deid_preview(texts: dict[str, str]) -> dict:
    """专家自己写的文字先去标识给专家看（同一实体同一占位符）；只在内存里做，不保存、不写审计。
    机密级命中只回类别、不回文字（这些不能入库，要删掉重写）。"""
    keys = [k for k in texts if _tidy(texts[k])]
    g = guard.check("\n".join(texts[k] for k in keys))
    if g.blocked:
        return {"blocked": True, "guard_labels": sorted({f.label for f in g.findings}), "texts": {}, "hits": [],
                "total": 0}
    r = deid.deidentify([("", texts[k]) for k in keys], "x")
    out = {k: body for k, (_, body) in zip(keys, r.sections, strict=True)}
    hits = [{"category": x.category, "placeholder": x.placeholder, "count": x.count} for x in r.replacements if x.count]
    return {"blocked": False, "guard_labels": [], "texts": out, "hits": hits, "total": r.total}


def _deid_hits(texts: list[str]) -> list[dict]:
    texts = [t for t in texts if _tidy(t)]
    if not texts:
        return []
    r = deid.deidentify([("", t) for t in texts], "x")
    return [{"category": x.category, "count": x.count} for x in r.replacements if x.count]


def _mark_recheck(db: Session, doc_ids: list[str], new_doc: str) -> list[str]:
    """“以这条为准”：已有条目的现行版本标待复查（复用 B13，进抽检队列），已在队列里的不重复标。"""
    marked = []
    for doc_id in doc_ids:
        _, cur = _live(db, doc_id)
        if cur is None or cur.recheck_state in OPEN_STATES:
            continue
        cur.recheck_state, cur.recheck_at = "pending", utcnow()
        cur.recheck_reason = f"专家工作台：与 {new_doc} 冲突，专家定以 {new_doc} 为准"[:200]
        marked.append(doc_id)
    if marked:
        db.commit()
    return marked


def answer(db: Session, user: User, version_id: int, answers: list[dict], fill: dict[str, str] | None = None,
           comment: str = "", elapsed_ms: int = 0, deid_ack: bool = False) -> dict:
    card = next((c for c in cards(db, user)["cards"] if c["version_id"] == version_id), None)
    v = db.get(KbVersion, version_id) if card else None
    if v is None:
        raise rv.ReviewError(404, "没有这张卡")
    _, _, issues = pq._agent_opinion(v)
    conflict_card = card["conflict"]
    keep, changes, reason, agreed, conflict = _parse_answers(v, answers, issues, conflict_card is not None)
    if not keep:
        raise rv.ReviewError(422, "请先回答“这条值得收进知识库吗”")
    body = compose(template_for(v.item.knowledge_kind, v.item.doc_type)["key"], fill) if fill else ""
    hits = _deid_hits([*(fill or {}).values(), comment, reason])
    if hits and not deid_ack:
        desc = "、".join(f"{h['category']} {h['count']} 处" for h in hits)
        raise rv.ReviewError(409, f"你写的文字里可能有能认出客户的信息（{desc}），请先看去标识预览：换成占位符，"
                                  "或确认不是客户信息再交")
    old_ids = [x["doc_id"] for x in conflict_card["items"]] if conflict_card else []
    if conflict == "old":
        reason = reason or f"与已有条目{'、'.join(old_ids)}重复或矛盾，以已有的为准"
    note = (comment or reason)[:1000]
    if keep["choice"] == "no" or (keep["choice"] == "yes" and conflict == "old"):
        outcome = "reject"
    elif keep["choice"] == "unsure" or conflict == "unsure":
        outcome = "undecided"
    else:
        if agreed and not body:
            raise rv.ReviewError(422, "你认可 Agent 提的问题：请用填空卡改正文，或选“不收，退回”")
        outcome = "modify" if changes or body else "approve"
    changed = sorted(changes) + (["body"] if body and outcome == "modify" else [])
    if outcome in ("approve", "modify"):
        if rv.level3(v) and not any(r.review_role == "agent" for r in v.reviews):
            raise rv.ReviewError(409, "知识审核 Agent 的意见还没出来，请稍后再拍板")
    if outcome == "modify":
        for k, val in changes.items():
            setattr(v, k, val)
        new_body = body or v.body
        try:
            VersionContent(title=v.title, body=new_body, source_type=v.source_type, source_detail=v.source_detail,
                           ref_document=v.ref_document, ref_version=v.ref_version,
                           ref_status=v.ref_status).check(v.item.layer)
            rv._check_text(v.title, "\n".join(str(x) for x in changes.values()))
        except meta.MetaError as e:
            db.rollback()
            raise rv.ReviewError(422, str(e)) from e
        except rv.ReviewError:
            db.rollback()
            raise
    status = v.status
    if outcome != "undecided":
        if v.item.knowledge_kind == "practice":
            decision = {"approve": "adopt", "modify": "modify", "reject": "reject"}[outcome]
            v = pq.decide(db, user, v.id, decision, body=body or v.body, comment=note, elapsed_ms=elapsed_ms)
        else:
            if body and outcome == "modify":
                v.body = body
                replace_mapping_rows(db, v)
            v = rv.review(db, user, v, "content", "reject" if outcome == "reject" else "approve", note)
        status = v.status
    recheck = _mark_recheck(db, old_ids, v.item.doc_id) if conflict == "new" and status == "published" else []
    audit.record(db, actor=user.username, action="kb.bench_answer", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, outcome=outcome, changed=changed, answers=len(answers), agreed=len(agreed),
                 conflict=conflict, recheck=recheck, deid_hits=sum(h["count"] for h in hits), deid_ack=bool(deid_ack),
                 elapsed_ms=max(0, min(int(elapsed_ms or 0), pq.MAX_ELAPSED_MS)))
    return {"outcome": outcome, "version_status": status, "changed": changed, "recheck": recheck}
