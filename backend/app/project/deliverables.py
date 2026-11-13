"""交付件分章节与版本（M3 第 ② 个 PR）。

交付件（D1～D8）按章节存储：先写大纲（章节标题和顺序），再逐章写正文。每次写正文都新建一个版本，
旧版本保留（只追加），可以按版本查看和对比。Agent 经工具 doc.generate 写，顾问经接口改，两者走同一套规则：

1. 严格档项目（M4 ④b）：交付件是占位符版本，只能基于摘要卡；正文和标题除机密级检测外还按摘要卡规则检查
   （机构名、人名、联系方式、网络标识、系统名、原话，见 app/project/cards.py placeholder_problems），命中不保存；
   来源标注写摘要卡编号 [S-02]，核对未撤下的摘要卡。顾问回到客户现场用现场工具按本地对照表回填真名。
2. 已签署的交付件不能再改；待签署（review）时改了内容，状态回到起草中（签署流程见 app/project/quality.py）。
3. 正文和章节标题先过机密级检测（guard），命中则不保存，只写审计（命中类别）。
4. 来源标注（公共规范 4.1）：正文里用方括号标注来源——客户证据 [I-02] [I-02-05] [D-12] [O-05]、
   知识库条目 [L4-OPS-GEN-REF-001]（含依据引用【依据：…｜L4-…】）、模型推断 [推断]。写入时逐个核对：
   证据编号要在本项目里存在，知识库条目要已发布且未下线。核对不上的不拒绝保存（草稿允许先写后补），
   但会列在 unresolved 里，界面和质量评审据此提示。
5. 并发：写入时可带 base_version（基于哪个版本改的），与当前版本不一致返回冲突，避免互相覆盖。

审计只记项目、交付件编号、章节号、版本号、字数、摘要哈希和来源计数，不记正文和章节标题。
"""
from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..kb.meta import LAYERS
from ..models import Deliverable, DeliverableSection, Evidence, KbItem, Project, SectionVersion, User
from ..services import guard
from . import cards
from . import evidence as evidence_svc

MAX_SECTIONS = 50
MAX_TITLE = 200
MAX_BODY_CHARS = 50_000
MAX_NOTE = 200
READ_CHARS = 20_000                 # 工具一次最多返回的正文字数，超出的用 offset 分段取
PLACEHOLDER_HINT = "严格档交付件是占位符版本：用【客户】【人员1】【系统A】等占位符，不写真名、IP、系统名和原话"

_EVIDENCE_REF = re.compile(r"\[((?:I|D|O|S)-\d{2,4})(-\d{1,3})?\]")
_KB_REF = re.compile(r"[\[｜|]\s*(L[1-5](?:-[A-Z0-9]{2,8}){3,4}-\d{3,5})\s*[\]】]")
_INFERRED = re.compile(r"\[(?:推断|模型推断)\]")


class DeliverableError(ValueError):
    def __init__(self, status: int, detail):
        super().__init__(detail if isinstance(detail, str) else "被拒绝")
        self.status = status
        self.detail = detail


@dataclass
class WriteResult:
    section: DeliverableSection
    version: SectionVersion


# ---- 查找 ----

def get_deliverable(db: Session, project: Project, code: str) -> Deliverable | None:
    code = (code or "").strip().upper()
    return db.scalar(select(Deliverable).where(Deliverable.project_id == project.id, Deliverable.code == code))


def require_deliverable(db: Session, project: Project, code: str) -> Deliverable:
    d = get_deliverable(db, project, code)
    if d is None:
        raise DeliverableError(404, "交付件不存在（编号为 D1～D8）")
    return d


def get_section(d: Deliverable, seq: int) -> DeliverableSection | None:
    return next((s for s in d.sections if s.seq == seq), None)


def require_section(d: Deliverable, seq: int) -> DeliverableSection:
    s = get_section(d, seq)
    if s is None:
        raise DeliverableError(404, f"第 {seq} 章不存在，请先写大纲")
    return s


def get_version(s: DeliverableSection, version: int | None = None) -> SectionVersion | None:
    want = s.current_version if version is None else version
    return next((v for v in s.versions if v.version == want), None)


# ---- 来源标注 ----

def parse_sources(db: Session, project: Project, body: str) -> dict:
    """解析正文里的来源标注并核对。返回：
    {"evidence": [{"ref", "code", "found"}], "knowledge": [{"ref", "found", "layer", "layer_name"}],
     "inferred": 次数, "counts": {"evidence", "knowledge", "inferred"}, "unresolved": [标注]}"""
    ev_refs: dict[str, str] = {}
    for m in _EVIDENCE_REF.finditer(body):
        ev_refs.setdefault(m.group(1) + (m.group(2) or ""), m.group(1))
    kb_refs = list(dict.fromkeys(m.group(1) for m in _KB_REF.finditer(body)))
    codes = set(ev_refs.values())
    existing = set(db.scalars(select(Evidence.code).where(Evidence.project_id == project.id,
                                                          Evidence.code.in_(codes)))) if codes else set()
    if any(c.startswith("S-") for c in codes):     # 严格档摘要卡（撤下的不算）
        existing |= cards.active_card_codes(db, project) & codes
    layers: dict[str, str] = {}
    if kb_refs:
        layers = dict(db.execute(select(KbItem.doc_id, KbItem.layer).where(
            KbItem.doc_id.in_(kb_refs), KbItem.retired.is_(False), KbItem.current_version_id.is_not(None))).all())
    evidence = [{"ref": ref, "code": code, "found": code in existing} for ref, code in ev_refs.items()]
    knowledge = [{"ref": ref, "found": ref in layers, "layer": layers.get(ref, ""),
                  "layer_name": LAYERS.get(layers.get(ref, ""), "")} for ref in kb_refs]
    inferred = len(_INFERRED.findall(body))
    return {"evidence": evidence, "knowledge": knowledge, "inferred": inferred,
            "counts": {"evidence": len(evidence), "knowledge": len(knowledge), "inferred": inferred},
            "unresolved": [x["ref"] for x in evidence + knowledge if not x["found"]]}


# ---- 写入规则 ----

def _check_writable(db: Session, user: User, project: Project, d: Deliverable, action: str) -> None:
    reason = None
    if d.status == "signed":
        reason, status, detail = "signed", 409, f"{d.code} 已签署，不能再修改"
    elif project.tier == "strict" and cards.active_consent(db, project) is None:
        reason, status, detail = "no_consent", 422, ("严格档项目没有有效的客户书面同意（未登记或已撤回），"
                                                     "平台不收交付件内容，工作在客户现场完成")
    if reason:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     deliverable=d.code, reason=reason)
        raise DeliverableError(status, detail)


def _guard(db: Session, user: User, project: Project, d: Deliverable, action: str, text: str, **meta) -> None:
    result = guard.check(text)
    if result.blocked:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     deliverable=d.code, reason="guard", categories=sorted({f.kind for f in result.findings}), **meta)
        raise DeliverableError(422, {"blocked": True, "summary": f"{result.summary()}，没有保存",
                                     "findings": [{"kind": f.kind, "label": f.label, "excerpt": f.excerpt}
                                                  for f in result.findings[:10]]})


def _placeholders(db: Session, user: User, project: Project, d: Deliverable, action: str, text: str, **meta) -> None:
    """严格档：占位符检查（M4 ④b）。命中不保存，只记类别。"""
    if project.tier != "strict":
        return
    kinds = cards.placeholder_problems(text)
    if kinds:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     deliverable=d.code, reason="placeholder", categories=kinds, **meta)
        raise DeliverableError(422, {"blocked": True, "summary": f"严格档内容含{'、'.join(kinds)}，没有保存。"
                                                                 f"{PLACEHOLDER_HINT}", "kinds": kinds})


def _touch(d: Deliverable, user: User) -> None:
    d.revision += 1
    d.updated_at = utcnow()
    d.updated_by = user.username
    if d.status in ("not_started", "review"):    # 待签署时改了内容：提交作废，要重新评审、重新提交（M3 ⑤）
        d.status = "drafting"


def set_outline(db: Session, user: User, project: Project, d: Deliverable, titles: list[str],
                agent_code: str = "") -> Deliverable:
    """设置大纲：titles 是完整的章节标题列表（按顺序）。已有的章按位置改标题，多出的新建；
    缩短大纲时，被去掉的章必须还没有正文（有正文的章不能从大纲里删，避免丢版本）。"""
    action = "deliverable.outline"
    _check_writable(db, user, project, d, action)
    if any(not isinstance(t, str) for t in titles):   # 防止对象被 str() 成 Python 写法写进标题（测试 #34）
        raise DeliverableError(422, "sections 只能是章节标题字符串列表，例如 [\"现状梳理\", \"差距分析\"]")
    titles = [t.strip() for t in titles]
    if not titles or len(titles) > MAX_SECTIONS:
        raise DeliverableError(422, f"大纲要有 1～{MAX_SECTIONS} 章")
    if any(not t or len(t) > MAX_TITLE for t in titles):
        raise DeliverableError(422, f"章节标题不能为空，且不超过 {MAX_TITLE} 字")
    _guard(db, user, project, d, action, "\n".join(titles))
    _placeholders(db, user, project, d, action, "\n".join(titles))
    removed = [s for s in d.sections if s.seq > len(titles)]
    kept_content = [s.seq for s in removed if s.current_version]
    if kept_content:
        raise DeliverableError(409, f"第 {'、'.join(map(str, kept_content))} 章已有正文，不能从大纲里删去")
    now = utcnow()
    for s in removed:
        d.sections.remove(s)
    db.flush()
    for i, title in enumerate(titles, 1):
        s = get_section(d, i)
        if s is None:
            d.sections.append(DeliverableSection(seq=i, title=title, created_by=user.username, created_at=now,
                                                 updated_at=now))
        elif s.title != title:
            s.title, s.updated_at = title, now
    _touch(d, user)
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 deliverable=d.code, sections=len(titles), removed=len(removed), agent=agent_code or None)
    db.commit()
    db.refresh(d)
    return d


def write_section(db: Session, user: User, project: Project, d: Deliverable, seq: int, body: str, *,
                  note: str = "", agent_code: str = "", base_version: int | None = None) -> WriteResult:
    action = "deliverable.write"
    _check_writable(db, user, project, d, action)
    s = require_section(d, seq)
    body = (body or "").strip()
    note = (note or "").strip()
    if not body:
        raise DeliverableError(422, "正文不能为空")
    if len(body) > MAX_BODY_CHARS:
        raise DeliverableError(422, f"一章正文不超过 {MAX_BODY_CHARS} 字，请拆成多章")
    if len(note) > MAX_NOTE:
        raise DeliverableError(422, f"修改说明不超过 {MAX_NOTE} 字")
    _guard(db, user, project, d, action, f"{body}\n{note}", section=seq)
    _placeholders(db, user, project, d, action, f"{body}\n{note}", section=seq)
    if base_version is not None and base_version != s.current_version:
        raise DeliverableError(409, f"第 {seq} 章已更新到 v{s.current_version}（你基于 v{base_version} 修改），"
                                    "请先查看最新版本再改")
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    current = get_version(s)
    if current is not None and current.body_sha == sha:
        raise DeliverableError(409, f"第 {seq} 章内容与当前版本 v{s.current_version} 相同，没有新建版本")
    sources = parse_sources(db, project, body)
    local = sorted({x["code"] for x in sources["evidence"]} & evidence_svc.local_only_codes(db, project))
    if local:                                      # 仅现场使用的资料不能引用（M4 ③）
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     deliverable=d.code, section=seq, reason="local_only", evidence=local)
        raise DeliverableError(422, f"{'、'.join(local)} 已标为仅现场使用，不能在交付件里引用，请删去这些标注和据此写的内容")
    v = SectionVersion(version=s.current_version + 1, body=body, char_count=len(body), body_sha=sha,
                       sources=sources, note=note, author=user.username, agent_code=agent_code)
    s.versions.append(v)
    s.current_version, s.updated_at = v.version, utcnow()
    _touch(d, user)
    try:
        db.flush()
    except IntegrityError as e:                    # 两个人同时写同一章：后到的冲突
        db.rollback()
        raise DeliverableError(409, f"第 {seq} 章正在被同时修改，请刷新后重试") from e
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 deliverable=d.code, section=seq, version=v.version, length=len(body), body_sha=sha[:16],
                 sources=sources["counts"], unresolved=len(sources["unresolved"]), agent=agent_code or None)
    db.commit()
    db.refresh(v)
    return WriteResult(section=s, version=v)


# ---- 读取 ----

def totals(d: Deliverable) -> dict:
    """整份交付件当前版本的来源计数和核对不上的标注。"""
    out = {"evidence": 0, "knowledge": 0, "inferred": 0, "unresolved": 0}
    for s in d.sections:
        v = get_version(s)
        if v is None:
            continue
        for k in ("evidence", "knowledge", "inferred"):
            out[k] += (v.sources or {}).get("counts", {}).get(k, 0)
        out["unresolved"] += len((v.sources or {}).get("unresolved", []))
    return out


def outline(d: Deliverable) -> list[dict]:
    rows = []
    for s in d.sections:
        v = get_version(s)
        rows.append({"seq": s.seq, "title": s.title, "current_version": s.current_version,
                     "chars": v.char_count if v else 0, "updated_at": s.updated_at,
                     "sources": (v.sources or {}).get("counts", {}) if v else {},
                     "unresolved": (v.sources or {}).get("unresolved", []) if v else []})
    return rows


def diff(s: DeliverableSection, a: int, b: int) -> list[str]:
    va, vb = get_version(s, a), get_version(s, b)
    if va is None or vb is None:
        raise DeliverableError(404, "版本不存在")
    return list(difflib.unified_diff(va.body.splitlines(), vb.body.splitlines(), f"v{a}", f"v{b}", lineterm="", n=2))


def read_for_agent(db: Session, project: Project, args: dict) -> dict:
    """工具 doc.generate 的 read：不带 deliverable 返回交付件清单；不带 section 返回大纲；
    带 section 返回该章当前版本正文（可分段）。"""
    if not str(args.get("deliverable", "")).strip():
        ds = db.scalars(select(Deliverable).where(Deliverable.project_id == project.id).order_by(Deliverable.code))
        return {"deliverables": [{"deliverable": d.code, "title": d.title, "status": d.status, "revision": d.revision,
                                  "sections": len(d.sections)} for d in ds]}
    d = require_deliverable(db, project, str(args.get("deliverable", "")))
    head = {"deliverable": d.code, "title": d.title, "status": d.status, "revision": d.revision}
    if args.get("section") in (None, ""):
        return {**head, "sections": [{k: r[k] for k in ("seq", "title", "current_version", "chars", "unresolved")}
                                     for r in outline(d)]}
    try:
        seq = int(args["section"])
        offset = max(0, int(args.get("offset", 0)))
        limit = max(1, min(int(args.get("limit", READ_CHARS)), READ_CHARS))
    except (TypeError, ValueError) as e:
        raise DeliverableError(422, "section、offset、limit 必须是整数") from e
    s = require_section(d, seq)
    v = get_version(s)
    if v is None:
        return {**head, "section": seq, "section_title": s.title, "version": 0, "body": "", "note": "该章还没有正文"}
    part = v.body[offset:offset + limit]
    return {**head, "section": seq, "section_title": s.title, "version": v.version, "body": part,
            "offset": offset, "total_chars": v.char_count, "has_more": offset + len(part) < v.char_count,
            "unresolved": (v.sources or {}).get("unresolved", [])}


def generate_for_agent(db: Session, user: User, project: Project, agent_code: str, args: dict) -> dict:
    """工具 doc.generate：action = read（默认）/ outline / write。"""
    action = str(args.get("action", "read"))
    if action == "read":
        return read_for_agent(db, project, args)
    d = require_deliverable(db, project, str(args.get("deliverable", "")))
    if action == "outline":
        sections = args.get("sections")
        if not isinstance(sections, list):
            raise DeliverableError(422, "outline 需要 sections：章节标题列表")
        d = set_outline(db, user, project, d, sections, agent_code=agent_code)
        return {"deliverable": d.code, "revision": d.revision,
                "sections": [{"seq": s.seq, "title": s.title, "current_version": s.current_version} for s in d.sections]}
    if action == "write":
        try:
            seq = int(args.get("section"))
            base = None if args.get("base_version") in (None, "") else int(args["base_version"])
        except (TypeError, ValueError) as e:
            raise DeliverableError(422, "section、base_version 必须是整数") from e
        r = write_section(db, user, project, d, seq, str(args.get("body", "")), note=str(args.get("note", "")),
                          agent_code=agent_code, base_version=base)
        return {"deliverable": d.code, "section": seq, "version": r.version.version, "revision": d.revision,
                "sources": r.version.sources["counts"], "unresolved": r.version.sources["unresolved"]}
    raise DeliverableError(422, "action 只能是 read、outline 或 write")
