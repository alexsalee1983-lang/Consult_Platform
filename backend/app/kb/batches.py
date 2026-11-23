"""知识批次（三期 B18①）：常用 kb 命令页面化的第一步。

- 页面发起：知识管理员在“知识批次”页选资料目录发起 `kb-import --submit`，或选题目文件发起 `kb-eval`（可选 --compare）；
  只排后台任务（`kb.batch_import` / `kb.batch_eval`，`jobs.enqueue()`），不在请求里同步跑。
- 路径只能在 `KB_BATCH_DIR`（默认 /srv/knowledge/downloads，部署机拉下的私有资料仓，只读挂载）之下，
  请求里只收相对路径，解析后仍须落在根目录里（不收绝对路径、..、符号链接逃逸）。
- 结果：处理器跑完写一条审计 `kb.batch`（只记批次种类、相对路径、计数和结论，不记条目正文、题目和答案）；
  命令行的切条、导入、起草、评测、依据核对、自动核验发布跑完也写同一条（source=cli），批次页一并列出。
- 不加表：排队中、运行中、失败看 jobs 表，计数看审计；任务参数只放相对路径、开关和用户编号。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..core.db import SessionLocal
from ..core.permissions import KNOWLEDGE_ADMIN, roles_of
from ..models import AuditLog, Job, User
from ..services import jobs

ACTION = "kb.batch"
START_ACTION = "kb.batch_start"
IMPORT_JOB, EVAL_JOB = "kb.batch_import", "kb.batch_eval"
JOB_KINDS = {IMPORT_JOB: "import", EVAL_JOB: "eval"}
# 批次种类 → 页面名称；页面能发起的只有 import、eval
KIND_NAMES = {"split": "切条", "import": "导入", "draft": "起草", "eval": "评测", "basis": "依据核对",
              "source_publish": "自动核验发布"}
PAGE_KINDS = ("import", "eval")
LIST_LIMIT = 100
SOURCE_LIMIT = 200
QUESTION_SUFFIXES = (".yaml", ".yml")


class BatchError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def root() -> Path:
    return Path(get_settings().kb_batch_dir)


def is_admin(db: Session, user: User) -> bool:
    return KNOWLEDGE_ADMIN in roles_of(db, user.id)


def resolve(rel: str, *, want_dir: bool) -> Path:
    """相对路径 → 根目录下的真实路径；不合法 400，不存在 404。"""
    rel = (rel or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ":" in rel or any(p in ("..", "") for p in rel.split("/")):
        raise BatchError(400, "路径只能写资料目录下的相对路径，例如 2026-11-law")
    base = root().resolve()
    path = (base / rel).resolve()
    if not path.is_relative_to(base):
        raise BatchError(400, "路径不在资料目录里")
    if want_dir and not path.is_dir():
        raise BatchError(404, "资料目录不存在")
    if not want_dir and not (path.is_file() and path.suffix.lower() in QUESTION_SUFFIXES):
        raise BatchError(404, "题目文件不存在（只收 .yaml / .yml）")
    return path


def sources() -> dict:
    """资料目录下可选的目录（导入用，两层以内）和题目文件（评测用）。根目录不存在时为空。"""
    base = root()
    dirs: list[str] = []
    questions: list[str] = []
    if base.is_dir():
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(base)
            if any(part.startswith((".", "_")) for part in rel.parts):
                continue
            if p.is_dir() and len(rel.parts) <= 2 and len(dirs) < SOURCE_LIMIT:
                dirs.append(rel.as_posix())
            elif p.is_file() and p.suffix.lower() in QUESTION_SUFFIXES and len(questions) < SOURCE_LIMIT:
                questions.append(rel.as_posix())
    return {"root": str(base), "dirs": dirs, "questions": questions}


def start(db: Session, user: User, kind: str, rel: str, *, submit: bool = False, compare: bool = False) -> int:
    """页面发起一个批次：校验后排后台任务，返回任务编号。"""
    if kind not in PAGE_KINDS:
        raise BatchError(400, "页面只能发起导入或评测")
    path = resolve(rel, want_dir=kind == "import")
    rel = path.relative_to(root().resolve()).as_posix()
    if kind == "import":
        job_kind, payload = IMPORT_JOB, {"path": rel, "submit": bool(submit), "user_id": user.id}
    else:
        job_kind, payload = EVAL_JOB, {"path": rel, "compare": bool(compare), "user_id": user.id}
    job_id = jobs.enqueue(db, job_kind, payload, created_by=user.username, max_attempts=2)
    audit.record(db, actor=user.username, action=START_ACTION, result="allowed", job_id=job_id, kind=kind,
                 path=rel, submit=bool(submit), compare=bool(compare))
    return job_id


def record(db: Session, *, actor: str, kind: str, counts: dict, path: str = "", source: str = "cli",
           job_id: int | None = None, result: str = "allowed") -> None:
    """一个批次跑完：写审计 kb.batch（只记种类、相对路径、计数）。命令行和后台任务共用。"""
    audit.record(db, actor=actor, action=ACTION, result=result, kind=kind, source=source, job_id=job_id,
                 path=path[:200], counts=counts)


def record_cli(kind: str, actor: str, counts: dict, path: str = "") -> None:
    """命令行跑完一个批次：自开会话写审计；写不进去不影响命令本身的结果。"""
    try:
        with SessionLocal() as db:
            record(db, actor=actor, kind=kind, counts=counts, path=Path(path).name if path else "")
    except Exception:  # noqa: BLE001
        pass


# ---- 列表 ----

@dataclass
class Batch:
    kind: str
    name: str
    status: str                      # queued 排队中 / running 运行中 / done 完成 / failed 失败
    source: str                      # page 页面发起 / cli 命令行
    created_by: str
    created_at: datetime
    finished_at: datetime | None = None
    job_id: int | None = None
    path: str = ""
    counts: dict = field(default_factory=dict)
    error: str = ""


def _detail(row: AuditLog) -> dict:
    try:
        d = json.loads(row.detail or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def list_batches(db: Session, limit: int = LIST_LIMIT) -> list[Batch]:
    rows = list(db.scalars(select(AuditLog).where(AuditLog.action == ACTION)
                           .order_by(AuditLog.id.desc()).limit(limit * 2)))
    done_by_job: dict[int, tuple[AuditLog, dict]] = {}
    out: list[Batch] = []
    for row in rows:
        d = _detail(row)
        if d.get("job_id"):
            done_by_job.setdefault(int(d["job_id"]), (row, d))   # 同一任务重跑时取最近一条
            continue
        kind = str(d.get("kind", ""))
        out.append(Batch(kind=kind, name=KIND_NAMES.get(kind, kind), status="done" if row.result != "error" else "failed",
                         source=str(d.get("source", "cli")), created_by=row.actor, created_at=row.at,
                         finished_at=row.at, path=str(d.get("path", "")), counts=d.get("counts") or {}))
    for job in db.scalars(select(Job).where(Job.kind.in_(list(JOB_KINDS))).order_by(Job.id.desc()).limit(limit)):
        kind = JOB_KINDS[job.kind]
        try:
            payload = json.loads(job.payload or "{}")
        except ValueError:
            payload = {}
        b = Batch(kind=kind, name=KIND_NAMES[kind], status=job.status, source="page", created_by=job.created_by,
                  created_at=job.created_at, finished_at=job.finished_at, job_id=job.id,
                  path=str(payload.get("path", "")), error=job.last_error)
        if job.id in done_by_job:
            row, d = done_by_job[job.id]
            b.counts = d.get("counts") or {}
            if job.status == jobs.DONE and row.result == "error":
                b.status = "failed"
        out.append(b)
    out.sort(key=lambda b: jobs._as_aware(b.created_at) or b.created_at, reverse=True)
    return out[:limit]


# ---- 后台任务 ----

def _job_user(db: Session, payload: dict) -> User:
    user = db.get(User, int(payload.get("user_id") or 0))
    if user is None or not user.is_active or not is_admin(db, user):
        raise jobs.JobError("发起人不存在、已停用或不再是知识管理员", retry=False)
    return user


@jobs.handler(IMPORT_JOB)
def _run_import(payload: dict, ctx: jobs.JobContext) -> None:
    """页面发起的 kb-import：可重复执行（同一目录重导入为“无变化”）。"""
    from . import importer

    with SessionLocal() as db:
        user = _job_user(db, payload)
        try:
            path = resolve(str(payload.get("path", "")), want_dir=True)
        except BatchError as e:
            raise jobs.JobError(e.detail, retry=False) from e
        results = importer.import_dir(db, user, path, submit=bool(payload.get("submit")))
        counts = {k: sum(1 for r in results if r.action == k)
                  for k in ("created", "revised", "unchanged", "skipped", "error")}
        counts["total"] = len(results)
        counts["submit"] = bool(payload.get("submit"))
        record(db, actor=user.username, kind="import", counts=counts, path=str(payload.get("path", "")),
               source="page", job_id=ctx.job_id, result="error" if counts["error"] else "allowed")


@jobs.handler(EVAL_JOB)
def _run_eval(payload: dict, ctx: jobs.JobContext) -> None:
    """页面发起的 kb-eval（可选 --compare）：只记计数和是否达标。"""
    from . import compare, evaluate

    with SessionLocal() as db:
        user = _job_user(db, payload)
        try:
            path = resolve(str(payload.get("path", "")), want_dir=False)
            questions = evaluate.load_questions(path)
        except BatchError as e:
            raise jobs.JobError(e.detail, retry=False) from e
        except (evaluate.EvalError, ValueError) as e:
            raise jobs.JobError("题目文件格式不对", retry=False) from e
        if payload.get("compare"):
            cmp = compare.run(db, user, questions)
            counts = {"total": cmp.total, "compare": True, "passed": cmp.passed, "demo": cmp.demo,
                      "accuracy_with_kb": cmp.with_kb.get("accuracy"), "accuracy_no_kb": cmp.no_kb.get("accuracy")}
        else:
            rep = evaluate.run(db, user, questions)
            counts = {"total": rep.total, "compare": False, "accuracy": round(rep.accuracy, 4),
                      "passed": rep.accuracy >= get_settings().kb_eval_min_accuracy}
        record(db, actor=user.username, kind="eval", counts=counts, path=str(payload.get("path", "")),
               source="page", job_id=ctx.job_id)
