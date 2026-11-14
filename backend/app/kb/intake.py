"""知识投料：上传 → 解析检查 → 机密级检测 → 原件存知识区 → 后台解析切片。

入口（请求内，同步）：
1. 按文件内容判断类型并解析出文字；扫描件、加密文件、超限文件直接拒绝。
2. 对文件名和全文做机密级检测（guard）；命中则**不存档**，只写审计（命中类别），返回打码的命中摘录。
3. 同一文件（sha256 相同）已上传过则拒绝重复投料。
4. 原件存入对象存储知识区，键为 intake/<随机编号>/original.<扩展名>（不用原文件名，文件名可能含客户信息）。
5. 排后台任务 kb.parse。

后台任务 kb.parse（worker）：重新解析原件、按章节切片，结果 JSON 存到 intake/<编号>/parsed.json，更新记录。
任务可重复执行。投料只是“原料”，进入知识库还要经过去标识、双人审核（后续 PR）。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import SessionLocal, utcnow
from ..models import KbUpload, User
from ..services import guard, jobs, storage
from . import parse

AREA = "knowledge"


class IntakeError(ValueError):
    def __init__(self, status: int, detail):
        super().__init__(detail if isinstance(detail, str) else "投料被拒绝")
        self.status = status
        self.detail = detail


@dataclass
class IntakeResult:
    upload: KbUpload
    job_id: int


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def clean_filename(name: str) -> str:
    """只保留文件名本身（去掉路径），限制长度，去掉控制字符。"""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[-200:] or "未命名"


def submit(db: Session, user: User, filename: str, data: bytes, content_type: str = "") -> IntakeResult:
    filename = clean_filename(filename)
    try:
        doc = parse.parse(filename, data)
    except parse.ParseError as e:
        audit.record(db, actor=user.username, action="kb.intake", result="blocked", reason="unsupported",
                     ext=_ext(filename), size=len(data))
        raise IntakeError(422, str(e)) from e

    result = guard.check(filename + "\n" + doc.full_text())
    if result.blocked:
        kinds = sorted({f.kind for f in result.findings})
        audit.record(db, actor=user.username, action="kb.intake", result="blocked", reason="guard",
                     categories=kinds, ext=_ext(filename), size=len(data))
        raise IntakeError(422, {"blocked": True, "summary": result.summary() + "，文件没有保存",
                                "findings": [{"kind": f.kind, "label": f.label, "excerpt": f.excerpt}
                                             for f in result.findings[:10]]})

    if not storage.enabled():
        raise IntakeError(503, "对象存储未启用，暂时不能投料")
    sha = hashlib.sha256(data).hexdigest()
    dup = db.scalar(select(KbUpload).where(KbUpload.sha256 == sha))
    if dup is not None:
        raise IntakeError(409, f"这个文件已经投料过（投料编号 {dup.id}）")

    key = f"intake/{uuid.uuid4().hex}/original.{_ext(filename)}"
    storage.put(AREA, key, data, content_type or "application/octet-stream")
    up = KbUpload(object_key=key, filename=filename, content_type=content_type[:128], size=len(data), sha256=sha,
                  status="received", kind=doc.kind, uploaded_by=user.username)
    db.add(up)
    db.commit()
    job_id = jobs.enqueue(db, "kb.parse", {"upload_id": up.id}, created_by=user.username, max_attempts=3)
    up.job_id = job_id
    db.commit()
    audit.record(db, actor=user.username, action="kb.intake", result="allowed", upload_id=up.id, ext=_ext(filename),
                 size=len(data), sha256=sha[:16], job_id=job_id)
    return IntakeResult(up, job_id)


def parsed_result(up: KbUpload) -> dict | None:
    if not up.parsed_key:
        return None
    return json.loads(storage.get(AREA, up.parsed_key))


@jobs.handler("kb.parse")
def _parse_job(payload: dict, ctx: jobs.JobContext) -> None:
    with SessionLocal() as db:
        up = db.get(KbUpload, int(payload["upload_id"]))
        if up is None:
            return                      # 投料已删除（去标识确认后清理），任务无事可做
        data = storage.get(AREA, up.object_key)
        try:
            doc = parse.parse(up.filename, data)
        except parse.ParseError as e:
            up.status, up.error = "failed", str(e)[:200]
            db.commit()
            raise jobs.JobError(str(e), retry=False) from e
        chunks = parse.chunk(doc.sections)
        body = doc.to_json()
        body["chunks"] = [{"heading": c.heading, "text": c.text} for c in chunks]
        key = up.object_key.rsplit("/", 1)[0] + "/parsed.json"
        storage.put(AREA, key, json.dumps(body, ensure_ascii=False).encode(), "application/json")
        up.parsed_key, up.status, up.error = key, "parsed", ""
        up.page_count, up.section_count, up.chunk_count, up.char_count = (
            doc.pages, len(doc.sections), len(chunks), doc.chars)
        up.parsed_at = utcnow()
        db.commit()
        # 接着自动去标识（规则 + 脱敏审查 Agent），结果待知识管理员确认
        jobs.enqueue(db, "kb.deid", {"upload_id": up.id}, created_by=up.uploaded_by, max_attempts=2)
