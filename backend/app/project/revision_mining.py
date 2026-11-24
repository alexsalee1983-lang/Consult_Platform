"""三期 F4 最小版：改稿差异转候选（三期开发计划第三节，总体规划第六节“从改稿里挖”）。

程序对比每章“Agent 初稿”（该章第一个由 Agent 写入的版本）与“签署稿”（最近一次签署记录里的章节版本），
只用已有的章节版本表和签署记录，不读外部、不调模型。按行（段落、列表项、表格行）对齐，差异分三类：
删除 delete、改写 rewrite（相似度 ≥ REWRITE_MIN）、新增 add。

归纳：每章的删除和改写 → 一条“候选常见坑”（Agent 写了、专家改掉的）；新增 → 一条“候选评审项”（Agent 漏写、专家补上的）。
候选入库（四期 F4①）：本次全部候选拼成一份 Markdown，以“项目沉淀”经知识投料箱投料（来源项目 = 本项目），
之后照常走解析、去标识、起草（knowledge_kind=practice）、公司做法确认队列（B17）和专家拍板，不绕过任何一道。
同一份候选再挖一次内容相同，投料箱按哈希拒收（reason=duplicate）；对象存储未启用时只返回候选（reason=storage）。

边界：
- 开关 settings.f4_revision_mining 默认关；生产环境（APP_ENV=prod）一律不开，只在测试机、试用机虚拟数据上跑。
- 只有项目成员能触发（非成员 404）；严格档 422（资料不出现场）；客户不同意回流（flowback_allowed=False）409。
- 候选里的摘录先把项目代号换成【客户】，再过机密级检测，命中的整条候选丢弃只计数；摘录截断到 EXCERPT_MAX 字。
- 审计只记交付件编号、章节数、各类差异计数和候选数，不记正文。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..models import Deliverable, DeliverableSection, Project, SectionVersion, SignOff, User
from ..kb import intake
from ..services import guard

REWRITE_MIN = 0.5          # 两行相似度不低于它算改写，否则拆成删除 + 新增
EXCERPT_MAX = 200
MAX_CHANGES_PER_CANDIDATE = 10
KINDS = {"pitfall": "候选常见坑", "review_item": "候选评审项"}


class MiningError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


@dataclass
class Change:
    kind: str           # delete / rewrite / add
    before: str = ""
    after: str = ""
    ratio: float = 0.0  # 改写的相似度


def enabled() -> bool:
    s = get_settings()
    return bool(s.f4_revision_mining) and not s.is_prod


def _lines(text: str) -> list[str]:
    return [x.strip() for x in (text or "").splitlines() if x.strip()]


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def diff_lines(old: str, new: str) -> list[Change]:
    """按行对齐初稿与签署稿；替换块里两两按相似度配对成改写，配不上的拆成删除、新增。"""
    a, b = _lines(old), _lines(new)
    out: list[Change] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        olds, news = a[i1:i2], b[j1:j2]
        used: set[int] = set()
        for o in olds:
            best, best_r = -1, 0.0
            for k, n in enumerate(news):
                if k in used:
                    continue
                r = _sim(o, n)
                if r > best_r:
                    best, best_r = k, r
            if best >= 0 and best_r >= REWRITE_MIN:
                used.add(best)
                out.append(Change("rewrite", o, news[best], round(best_r, 4)))
            else:
                out.append(Change("delete", o))
        out.extend(Change("add", after=n) for k, n in enumerate(news) if k not in used)
    return out


def _signed_versions(db: Session, d: Deliverable) -> dict:
    row = db.scalar(select(SignOff).where(SignOff.deliverable_id == d.id, SignOff.action == "sign")
                    .order_by(SignOff.id.desc()))
    return dict(row.versions or {}) if row else {}


def _agent_draft(s: DeliverableSection) -> SectionVersion | None:
    return next((v for v in s.versions if v.agent_code), None)


def _version(s: DeliverableSection, n: int) -> SectionVersion | None:
    return next((v for v in s.versions if v.version == n), None)


def _clip(text: str, code: str) -> str:
    text = text.replace(code, "【客户】") if code else text
    return text if len(text) <= EXCERPT_MAX else text[:EXCERPT_MAX] + "……"


def _candidate(kind: str, s: DeliverableSection, changes: list[Change], code: str) -> dict:
    items = [{"kind": c.kind, "before": _clip(c.before, code), "after": _clip(c.after, code), "ratio": c.ratio}
             for c in changes[:MAX_CHANGES_PER_CANDIDATE]]
    title = _clip(s.title, code)
    verb = "Agent 初稿被删改" if kind == "pitfall" else "专家在签署稿中补充"
    return {"kind": kind, "kind_name": KINDS[kind], "section": s.seq, "section_title": title,
            "title": f"{KINDS[kind]}：《{title}》{verb} {len(changes)} 处", "changes": items,
            "change_count": len(changes)}


def _clean(cand: dict) -> bool:
    text = "\n".join([cand["title"]] + [c["before"] + "\n" + c["after"] for c in cand["changes"]])
    return not guard.check(text).blocked


def candidates_markdown(d: Deliverable, candidates: list[dict]) -> str:
    """投料用的 Markdown：每条候选一节，摘录已换掉项目代号、已过机密级检测。"""
    lines = [f"# 改稿差异候选：{d.code} {d.title}", "",
             "来源：项目沉淀（改稿差异转候选，三期 F4 最小版 / 四期 F4①），机器生成，待去标识与专家确认。", ""]
    for cand in candidates:
        lines += [f"## {cand['title']}", "", f"类型：{cand['kind_name']}；章节：第 {cand['section']} 章", ""]
        for c in cand["changes"]:
            if c["before"]:
                lines.append(f"- 初稿：{c['before']}")
            if c["after"]:
                lines.append(f"- 签署稿：{c['after']}")
        lines.append("")
    return "\n".join(lines)


def submit_to_practice_queue(db: Session, user: User, project: Project, d: Deliverable,
                             candidates: list[dict]) -> dict:
    """候选经投料箱以项目沉淀投料。返回 {"queued": 候选条数, "upload_id": 投料编号, "reason": 未投料原因}。"""
    if not candidates:
        return {"queued": 0, "upload_id": None, "reason": "no_candidates"}
    data = candidates_markdown(d, candidates).encode("utf-8")
    try:
        res = intake.submit(db, user, f"改稿候选-{d.code}.md", data, "text/markdown", project=project.code)
    except intake.IntakeError as e:
        reason = {409: "duplicate", 503: "storage"}.get(e.status, "rejected")
        return {"queued": 0, "upload_id": None, "reason": reason}
    return {"queued": len(candidates), "upload_id": res.upload.id, "reason": ""}


def mine(db: Session, user: User, project: Project, d: Deliverable) -> dict:
    if not enabled():
        raise MiningError(404, "改稿差异转候选未开启（只在测试机、试用机上开）")
    if project.tier == "strict":
        raise MiningError(422, "严格档项目资料不出现场，不做改稿差异挖掘")
    if not project.flowback_allowed:
        raise MiningError(409, "客户不同意回流，不从本项目生成知识候选")
    if d.status != "signed":
        raise MiningError(409, "交付件还没有签署，签署后才能对比初稿与签署稿")
    signed = _signed_versions(db, d)
    counts = {"sections": 0, "delete": 0, "rewrite": 0, "add": 0, "blocked": 0}
    candidates: list[dict] = []
    for s in d.sections:
        draft = _agent_draft(s)
        final = _version(s, int(signed.get(str(s.seq), 0) or 0))
        if draft is None or final is None or final.version <= draft.version:
            continue
        counts["sections"] += 1
        changes = diff_lines(draft.body, final.body)
        for c in changes:
            counts[c.kind] += 1
        groups = (("pitfall", [c for c in changes if c.kind in ("delete", "rewrite")]),
                  ("review_item", [c for c in changes if c.kind == "add"]))
        for kind, group in groups:
            if not group:
                continue
            cand = _candidate(kind, s, group, project.code)
            if _clean(cand):
                candidates.append(cand)
            else:
                counts["blocked"] += 1
    sub = submit_to_practice_queue(db, user, project, d, candidates)
    audit.record(db, actor=user.username, action="f4.revision_mining", result="allowed", project_code=project.code,
                 deliverable=d.code, candidates=len(candidates), queued=sub["queued"], upload_id=sub["upload_id"],
                 reason=sub["reason"], counts=counts)
    return {"available": True, **sub, "counts": counts, "candidates": candidates}
