"""四期 B25：“需人工下载”缺口的原件上传与入库。

- upload：知识专家、知识管理员给 plan=manual 的缺口上传原件（官网用浏览器手动下载的那份）。网址必须落在正式来源
  白名单里（编号类缺口要填下载网址），否则先登记来源、转正式。原件只经 storage.py 存进知识区，缺口标 uploaded，
  耗时的切条核验走后台任务 kb.gap_manual。
- _job：按采集回传批次同一口径（manifest.json 的文件名、sha256、网址，acquisition.verify_return）核对，
  再生成下载清单走 autopublish（= kb-source-publish），通过才关缺口（collected）；任何一步失败缺口退回 manual。
审计只记缺口编号、大小、哈希前缀和发布数，不记文件名与标题。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..models import KbGap, KbSource, User
from ..services import jobs, storage
from . import acquisition as acq
from . import autopublish, gap_pool
from . import review as rv
from . import source_registry as reg

JOB_KIND = "kb.gap_manual"
AREA = "knowledge"
EXTS = {"pdf", "docx", "doc", "html", "htm", "txt"}
MAX_BYTES = 50 * 1024 * 1024


def _source_for(db: Session, url: str) -> KbSource | None:
    hits = [s for s in db.scalars(select(KbSource).where(KbSource.status == "active"))
            if acq.in_whitelist(url, (s.url,))]
    return max(hits, key=lambda s: len(s.url)) if hits else None


def upload(db: Session, user: User, gap_id: int, filename: str, data: bytes, title: str,
           url: str = "") -> dict[str, Any]:
    if not rv.is_knowledge_user(db, user):
        raise reg.SourceError(403, "只有知识专家、知识管理员能上传原件")
    row = db.get(KbGap, gap_id)
    if row is not None and row.status == "closed" and row.close_reason == "collected":
        raise reg.SourceError(409, "这个缺口的原件已上传并入库")
    if row is None or row.status != "open" or row.plan not in gap_pool.HELD:
        raise reg.SourceError(404, "没有这个需人工下载的缺口")
    if row.plan == gap_pool.UPLOADED:
        raise reg.SourceError(409, "这个缺口的原件已上传，正在处理")
    title = title.strip()
    if not title or len(title) > 200:
        raise reg.SourceError(422, "要填文件名称（200 字以内）")
    url = (url or "").strip() or row.gap_key
    if urlsplit(url).scheme not in ("http", "https"):
        raise reg.SourceError(422, "要填官方下载网址")
    if _source_for(db, url) is None:
        raise reg.SourceError(422, "下载网址不在正式来源里：先在来源登记表登记并转正式，再上传")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in EXTS:
        raise reg.SourceError(422, f"只收 {'、'.join(sorted(EXTS))} 文件")
    if not data or len(data) > MAX_BYTES:
        raise reg.SourceError(413, f"文件为空或超过 {MAX_BYTES // 1024 // 1024}MB")
    if not storage.enabled():
        raise reg.SourceError(503, "对象存储未启用，暂时不能上传")
    sha = hashlib.sha256(data).hexdigest()
    key = f"gap-manual/{row.id}/{sha[:16]}.{ext}"
    storage.put(AREA, key, data)
    row.plan, row.updated_at = gap_pool.UPLOADED, utcnow()
    db.commit()
    job_id = jobs.enqueue(db, JOB_KIND, {"gap_id": row.id, "key": key, "sha256": sha, "url": url, "title": title,
                                         "user": user.username}, created_by=user.username, max_attempts=1)
    audit.record(db, actor=user.username, action="kb.gap.manual_upload", result="allowed", gap_id=row.id,
                 size=len(data), sha256=sha[:16], job_id=job_id)
    return {"gap_id": row.id, "job_id": job_id}


def _publish_csv(name: str, title: str, publisher: str, url: str, sha: str) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=gap_pool.PUBLISH_COLUMNS, lineterminator=chr(10))
    w.writeheader()
    w.writerow({"文件名": name, "名称": title, "发布机关": publisher, "原网址": url, "现行状态": "现行",
                "获取方式": "已下载", "sha256": sha})
    return out.getvalue()


def _back_to_manual(db: Session, gap_id: int, why: str, actor: str) -> None:
    row = db.get(KbGap, gap_id)
    if row is not None and row.status == "open":
        row.plan, row.updated_at = gap_pool.MANUAL, utcnow()
        db.commit()
        gap_pool.note_failure(db, row, actor=actor)     # 四期 B41：失败满 3 次标暂无可用来源
    audit.record(db, actor=actor, action="kb.gap.manual_import", result="blocked", gap_id=gap_id, problem=why)


@jobs.handler(JOB_KIND)
def _job(payload: dict, ctx: jobs.JobContext | None) -> None:
    from ..core.db import SessionLocal

    gap_id, sha, url = int(payload["gap_id"]), str(payload["sha256"]), str(payload["url"])
    actor = str(payload["user"])
    with SessionLocal() as db, tempfile.TemporaryDirectory() as tmp:
        try:
            user = db.scalar(select(User).where(User.username == actor))
            src = _source_for(db, url)
            if user is None or src is None:
                raise RuntimeError("上传人或正式来源已不存在")
            batch = Path(tmp) / "batch"
            batch.mkdir()
            name = f"manual_{gap_id}_{sha[:8]}.{payload['key'].rsplit('.', 1)[-1]}"
            (batch / name).write_bytes(storage.get(AREA, str(payload["key"])))
            (batch / acq.RETURN_MANIFEST).write_text(
                json.dumps({"files": [{"file": name, "sha256": sha, "url": url}]}), encoding="utf-8")
            report = acq.verify_return(batch, gap_pool.whitelist(db))
            if not report.ok:
                raise RuntimeError("核对未通过：" + "；".join(p.split(":", 1)[0] for p in report.problems))
            manifest = Path(tmp) / "manifest.csv"
            manifest.write_text(_publish_csv(name, str(payload["title"]), src.publisher, url, sha), encoding="utf-8")
            r = autopublish.run(db, user, manifest, batch)
        except Exception as e:
            db.rollback()
            _back_to_manual(db, gap_id, type(e).__name__, actor)
            raise
        row = db.get(KbGap, gap_id)
        if row is not None and row.status == "open":
            now = utcnow()
            row.status, row.close_reason, row.plan, row.closed_at, row.updated_at = "closed", "collected", "", now, now
            db.commit()
        audit.record(db, actor=actor, action="kb.gap.manual_import", result="allowed", gap_id=gap_id,
                     published=sum(d.published for d in r.docs), manual=sum(d.manual for d in r.docs))
