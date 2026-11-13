"""项目区资料与证据编号。

上传客户资料（请求内，同步）：
1. 严格档项目一律拒绝（资料不出客户现场，只收去标识摘要卡，M4 接入）。
2. 按文件内容判断类型并解析出文字；扫描件、加密文件、超限文件直接拒绝（与知识投料共用 app/kb/parse.py）。
3. 文件名和全文过机密级检测（guard）；命中则**不存档**，只写审计（命中类别），返回打码的命中摘录。
4. 同一项目里同一文件（sha256 相同）不重复上传。
5. 原件存对象存储项目区，键为 <项目代号>/documents/<随机编号>/original.<扩展名>（不用原文件名）。
6. 分配证据编号 D-xx，排后台任务 project.parse 解析成章节，结果 JSON 存在原件旁边。

录入访谈纪要 / 现场观察：只写受访者角色、不写姓名；正文过机密级检测后存数据库项目区表，分配 I-xx / O-xx。

仅现场使用（M4 ③）：客户书面要求某份资料只在客户现场查阅时，项目经理把它标为仅现场使用（写客户通知的文号，
不写客户名称）。平台立即删除对象存储里的原件和解析结果、清空文件名，证据编号保留不复用；之后同一文件（sha256）
不能再上传，Agent 经 project.evidence 读不到，交付件写入时引用它会被拒绝，质量评审把已有引用判为阻断。
标注不可撤销（文件已删除）。告知书附件一第 4 条、合同条款：收到通知后 5 个工作日内落实。

审计只记项目、证据编号、类别、大小和命中类别，不记文件名、正文和文号。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core import audit
from ..core.permissions import PROJECT_MANAGER, membership
from ..core.db import SessionLocal, utcnow
from ..kb import parse
from ..kb.intake import clean_filename
from ..models import Evidence, Project, ProjectDocument, User
from ..models.evidence import EVIDENCE_PREFIX
from ..services import guard, jobs, storage

AREA = "project"
MAX_NOTE_CHARS = 100_000
READ_CHARS = 20_000            # 工具一次最多返回的正文字数，超出的用 offset 分段取
LOCAL_ONLY_NOTE = "已标为仅现场使用：平台没有原文，只能在客户现场查阅，不能引用"
STRICT_REFUSAL = "严格档项目的资料不出客户现场，平台不保存资料和纪要原文；摘要卡流程在 M4 接入"


class EvidenceError(ValueError):
    def __init__(self, status: int, detail):
        super().__init__(detail if isinstance(detail, str) else "被拒绝")
        self.status = status
        self.detail = detail


@dataclass
class UploadResult:
    evidence: Evidence
    document: ProjectDocument
    job_id: int


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _refuse_strict(db: Session, user: User, project: Project, action: str) -> None:
    if project.tier == "strict":
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     reason="strict_tier")
        raise EvidenceError(422, STRICT_REFUSAL)


def _guard(db: Session, user: User, project: Project, action: str, text: str, saved_note: str, **meta) -> None:
    result = guard.check(text)
    if result.blocked:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     reason="guard", categories=sorted({f.kind for f in result.findings}), **meta)
        raise EvidenceError(422, {"blocked": True, "summary": f"{result.summary()}，{saved_note}",
                                  "findings": [{"kind": f.kind, "label": f.label, "excerpt": f.excerpt}
                                               for f in result.findings[:10]]})


def _assign(db: Session, project: Project, kind: str, make) -> Evidence:
    """按项目、按类别分配下一个编号并保存。并发撞号时（唯一约束）重取。make(seq, code) 返回未保存的 Evidence。"""
    for _ in range(5):
        seq = (db.scalar(select(func.max(Evidence.seq))
                         .where(Evidence.project_id == project.id, Evidence.kind == kind)) or 0) + 1
        ev = make(seq, f"{EVIDENCE_PREFIX[kind]}-{seq:02d}")
        db.add(ev)
        try:
            db.commit()
            return ev
        except IntegrityError:
            db.rollback()
    raise EvidenceError(409, "证据编号分配冲突，请重试")


def upload_document(db: Session, user: User, project: Project, filename: str, data: bytes,
                    content_type: str = "", title: str = "") -> UploadResult:
    action = "project.document"
    _refuse_strict(db, user, project, action)
    filename = clean_filename(filename)
    try:
        doc = parse.parse(filename, data)
    except parse.ParseError as e:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     reason="unsupported", ext=_ext(filename), size=len(data))
        raise EvidenceError(422, str(e)) from e
    title = (title.strip() or filename)[:200]
    _guard(db, user, project, action, f"{filename}\n{title}\n{doc.full_text()}", "文件没有保存",
           ext=_ext(filename), size=len(data))

    if not storage.enabled():
        raise EvidenceError(503, "对象存储未启用，暂时不能上传资料")
    sha = hashlib.sha256(data).hexdigest()
    dup = db.scalar(select(Evidence).join(ProjectDocument, Evidence.document_id == ProjectDocument.id)
                    .where(ProjectDocument.project_id == project.id, ProjectDocument.sha256 == sha))
    if dup is not None and dup.document.local_only:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     reason="local_only", evidence=dup.code)
        raise EvidenceError(422, f"这份资料已标为仅现场使用（证据编号 {dup.code}），不能上传")
    if dup is not None:
        raise EvidenceError(409, f"这个文件已经上传过（证据编号 {dup.code}）")

    key = f"{project.code}/documents/{uuid.uuid4().hex}/original.{_ext(filename)}"
    storage.put(AREA, key, data, content_type or "application/octet-stream")
    d = ProjectDocument(project_id=project.id, object_key=key, filename=filename, content_type=content_type[:128],
                        size=len(data), sha256=sha, kind=doc.kind, status="received", uploaded_by=user.username)
    db.add(d)
    db.commit()
    ev = _assign(db, project, "document", lambda seq, code: Evidence(
        project_id=project.id, code=code, kind="document", seq=seq, title=title, document_id=d.id,
        created_by=user.username))
    job_id = jobs.enqueue(db, "project.parse", {"document_id": d.id}, created_by=user.username, max_attempts=3)
    d.job_id = job_id
    db.commit()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 evidence=ev.code, ext=_ext(filename), size=len(data), sha256=sha[:16], job_id=job_id)
    return UploadResult(ev, d, job_id)


def add_note(db: Session, user: User, project: Project, kind: str, title: str, content: str,
             interviewee_role: str = "", occurred_on: date | None = None) -> Evidence:
    """录入访谈纪要（interview）或现场观察（observation）。"""
    action = "project.note"
    if kind not in ("interview", "observation"):
        raise EvidenceError(422, "类别只能是 interview（访谈纪要）或 observation（现场观察）")
    _refuse_strict(db, user, project, action)
    title, content, role = title.strip(), content.strip(), interviewee_role.strip()
    if not title or not content:
        raise EvidenceError(422, "标题和正文不能为空")
    if kind == "interview" and not role:
        raise EvidenceError(422, "访谈纪要要写受访者角色（例如“信息科技部负责人”），不写姓名")
    if len(content) > MAX_NOTE_CHARS:
        raise EvidenceError(422, f"正文超过 {MAX_NOTE_CHARS // 10000} 万字，请分条录入")
    _guard(db, user, project, action, f"{title}\n{role}\n{content}", "纪要没有保存", kind=kind, chars=len(content))
    ev = _assign(db, project, kind, lambda seq, code: Evidence(
        project_id=project.id, code=code, kind=kind, seq=seq, title=title[:200],
        interviewee_role=role[:100] if kind == "interview" else "", occurred_on=occurred_on, content=content,
        char_count=len(content), created_by=user.username))
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 evidence=ev.code, kind=kind, chars=len(content))
    return ev


def mark_local_only(db: Session, user: User, project: Project, ev: Evidence, request_ref: str) -> Evidence:
    """把一份客户资料标为仅现场使用（项目经理）。先删对象存储里的文件，删成功才标注。"""
    action = "project.document.local_only"
    m = membership(db, project.id, user.id)
    if m is None or m.project_role != PROJECT_MANAGER:
        audit.record(db, actor=user.username, action=action, result="denied", project_code=project.code,
                     evidence=ev.code, reason="not_manager")
        raise EvidenceError(403, "只有本项目的项目经理能把资料标为仅现场使用")
    d = ev.document
    if ev.kind != "document" or d is None:
        raise EvidenceError(422, "只有客户资料（D 编号）能标为仅现场使用；纪要和观察请直接修改或不引用")
    if d.local_only:
        raise EvidenceError(409, f"{ev.code} 已经标为仅现场使用")
    ref = request_ref.strip()
    if not ref:
        raise EvidenceError(422, "请填写客户书面通知的文号或编号（不写客户名称）")
    result = guard.check(ref)
    if result.blocked:
        audit.record(db, actor=user.username, action=action, result="blocked", project_code=project.code,
                     evidence=ev.code, reason="guard", categories=sorted({f.kind for f in result.findings}))
        raise EvidenceError(422, "文号命中机密级检测，请只写编号")
    db.refresh(d, with_for_update=True)
    if d.local_only:
        raise EvidenceError(409, f"{ev.code} 已经标为仅现场使用")
    keys = [k for k in (d.object_key, d.parsed_key) if k]
    if not storage.enabled():
        raise EvidenceError(503, "对象存储未启用，暂时不能删除资料文件，请稍后再试")
    for k in keys:
        storage.delete(AREA, k)
    d.local_only, d.local_only_at, d.local_only_by, d.local_only_ref = True, utcnow(), user.username, ref[:64]
    d.parsed_key, d.filename, d.status, d.error = "", "", "local_only", ""
    db.commit()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 evidence=ev.code, files=len(keys))
    return ev


def local_only_codes(db: Session, project: Project) -> set[str]:
    """本项目已标为仅现场使用的证据编号。"""
    return set(db.scalars(select(Evidence.code).join(ProjectDocument, Evidence.document_id == ProjectDocument.id)
                          .where(Evidence.project_id == project.id, ProjectDocument.local_only.is_(True))))


def list_evidences(db: Session, project: Project) -> list[Evidence]:
    return list(db.scalars(select(Evidence).where(Evidence.project_id == project.id)
                           .order_by(Evidence.kind, Evidence.seq)))


def get_evidence(db: Session, project: Project, code: str) -> Evidence | None:
    return db.scalar(select(Evidence).where(Evidence.project_id == project.id, Evidence.code == code.strip().upper()))


def document_sections(d: ProjectDocument) -> list[dict] | None:
    """解析结果的章节 [{heading, text}]；还没解析完返回 None。"""
    if not d.parsed_key:
        return None
    return json.loads(storage.get(AREA, d.parsed_key)).get("sections", [])


def full_text(ev: Evidence) -> str | None:
    """证据正文：纪要 / 观察取数据库；文档取解析结果（按章节拼接）。文档还没解析完返回 None。"""
    if ev.kind != "document":
        return ev.content
    if ev.document is not None and ev.document.local_only:
        return None
    sections = document_sections(ev.document) if ev.document else None
    if sections is None:
        return None
    return "\n\n".join((f"## {s['heading']}\n" if s.get("heading") else "") + s.get("text", "") for s in sections)


def summary(ev: Evidence) -> dict:
    d = ev.document
    return {"code": ev.code, "kind": ev.kind, "title": ev.title, "interviewee_role": ev.interviewee_role,
            "occurred_on": ev.occurred_on.isoformat() if ev.occurred_on else None,
            "chars": d.char_count if d else ev.char_count,
            "status": d.status if d else "ready", **({"note": LOCAL_ONLY_NOTE} if d and d.local_only else {})}


def read_for_agent(db: Session, project: Project, code: str = "", offset: int = 0,
                   limit: int = READ_CHARS) -> dict:
    """工具 project.evidence 的实现：不带 code 返回证据清单；带 code 返回该证据正文（分段）。"""
    if project.tier == "strict":
        return {"evidences": [], "note": "严格档项目：平台没有资料和纪要原文，Agent 只能使用摘要卡（M4 接入）"}
    if not code:
        return {"evidences": [summary(e) for e in list_evidences(db, project)]}
    ev = get_evidence(db, project, code)
    if ev is None:
        return {"error": f"本项目没有证据 {code}"}
    if ev.document is not None and ev.document.local_only:
        return {**summary(ev), "text": "", "error": f"{ev.code} {LOCAL_ONLY_NOTE}"}
    text = full_text(ev)
    if text is None:
        return {**summary(ev), "text": "", "note": "文档还在解析，稍后再取"}
    offset = max(0, offset)
    limit = max(1, min(limit, READ_CHARS))
    part = text[offset:offset + limit]
    out = {**summary(ev), "text": part, "offset": offset, "total_chars": len(text)}
    if offset + len(part) < len(text):
        out["next_offset"] = offset + len(part)
    return out


@jobs.handler("project.parse")
def _parse_job(payload: dict, ctx: jobs.JobContext) -> None:
    with SessionLocal() as db:
        d = db.get(ProjectDocument, int(payload["document_id"]))
        if d is None:
            raise jobs.JobError("资料记录不存在", retry=False)
        if d.local_only:                       # 解析前已标为仅现场使用：文件已删，不再解析
            return
        data = storage.get(AREA, d.object_key)
        try:
            doc = parse.parse(d.filename, data)
        except parse.ParseError as e:
            d.status, d.error = "failed", str(e)[:200]
            db.commit()
            raise jobs.JobError(str(e), retry=False) from e
        db.refresh(d, with_for_update=True)     # 与“仅现场使用”标注串行：解析期间被标注的不写结果
        if d.local_only:
            db.commit()
            return
        key = d.object_key.rsplit("/", 1)[0] + "/parsed.json"
        storage.put(AREA, key, json.dumps(doc.to_json(), ensure_ascii=False).encode(), "application/json")
        d.parsed_key, d.status, d.error = key, "parsed", ""
        d.page_count, d.section_count, d.char_count = doc.pages, len(doc.sections), doc.chars
        d.parsed_at = utcnow()
        db.commit()
