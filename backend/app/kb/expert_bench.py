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
"""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy.orm import Session

from ..core import audit
from ..core.permissions import KNOWLEDGE_EXPERT, roles_of
from ..models import KbVersion, User
from ..project import my_tasks as mt
from . import meta
from . import practice_queue as pq
from . import review as rv
from .items import VersionContent, replace_mapping_rows

CHOICES = ("yes", "no", "unsure")
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


def _questions(v: KbVersion, unsure: set[str], issues: list[str]) -> list[dict]:
    kind = meta.KNOWLEDGE_KINDS.get(v.item.knowledge_kind, "") or "知识"
    qs = [{"id": "keep", "kind": "keep", "text": f"这条值得收进知识库吗？（{kind}）", "empty": False,
           "uncertain": False, "value": ""}]
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
    tasks = [t for t in mt.collect(db, user) if t.kind == "kb_review" and not t.blocked]
    ranked = mt.rank(tasks, w)
    folded = mt.apply_quota(ranked, w.quota)
    out = []
    for t in ranked:
        v = db.get(KbVersion, t.ref_id)
        if v is None or not _content_pending(v):
            continue
        _, unsure = mt._kb_uncertainty(v)
        basis, confidence, issues = pq._agent_opinion(v)
        out.append({"version_id": v.id, "doc_id": v.item.doc_id, "version": v.version, "title": v.title,
                    "summary": v.summary, "body": v.body, "status_plain": PLAIN["in_review"],
                    "kind_plain": meta.KNOWLEDGE_KINDS.get(v.item.knowledge_kind, "") or "通用",
                    "doc_type": v.item.doc_type, "source_detail": v.source_detail, "basis": basis,
                    "confidence_plain": _confidence_plain(confidence), "issues": issues,
                    "score": t.score, "folded": t.folded,
                    "questions": _questions(v, unsure, issues),
                    "template": template_for(v.item.knowledge_kind, v.item.doc_type)})
    return {"quota": w.quota, "folded": folded, "plain": PLAIN, "cards": out}


def _parse_answers(v: KbVersion, answers: list[dict], issues: list[str]) -> tuple[dict, dict, str, list[int]]:
    """返回 (keep 回答, 字段改正, 驳回理由, 认可的问题序号)；不合法 422。"""
    valid = {"keep"} | {f"field:{n}" for n, _ in FIELDS} | {f"issue:{i}" for i in range(len(issues))}
    keep: dict = {}
    changes: dict = {}
    agreed: list[int] = []
    for a in answers:
        qid, choice, value = str(a.get("id", "")), str(a.get("choice", "")), _tidy(a.get("value", ""))[:MAX_VALUE]
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
    return keep, changes, keep.get("value", ""), agreed


def answer(db: Session, user: User, version_id: int, answers: list[dict], fill: dict[str, str] | None = None,
           comment: str = "", elapsed_ms: int = 0) -> dict:
    if not any(c["version_id"] == version_id for c in cards(db, user)["cards"]):
        raise rv.ReviewError(404, "没有这张卡")
    v = db.get(KbVersion, version_id)
    if v is None:
        raise rv.ReviewError(404, "没有这张卡")
    _, _, issues = pq._agent_opinion(v)
    keep, changes, reason, agreed = _parse_answers(v, answers, issues)
    if not keep:
        raise rv.ReviewError(422, "请先回答“这条值得收进知识库吗”")
    body = compose(template_for(v.item.knowledge_kind, v.item.doc_type)["key"], fill) if fill else ""
    note = (comment or reason)[:1000]
    if keep["choice"] == "unsure":
        outcome = "undecided"
    elif keep["choice"] == "no":
        outcome = "reject"
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
    audit.record(db, actor=user.username, action="kb.bench_answer", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, outcome=outcome, changed=changed, answers=len(answers), agreed=len(agreed),
                 elapsed_ms=max(0, min(int(elapsed_ms or 0), pq.MAX_ELAPSED_MS)))
    return {"outcome": outcome, "version_status": status, "changed": changed}
