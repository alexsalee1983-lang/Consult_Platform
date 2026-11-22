"""知识批次页（三期 B18①）：页面发起 kb-import --submit、kb-eval，只排后台任务；列表含状态、计数、发起人、时间。"""
import json
import secrets
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import batches
from app.main import app
from app.models import AuditLog, Job, KbItem
from app.services import jobs

from .conftest import create_user, logged_in_client

REPO = Path(__file__).resolve().parents[2]
SAMPLES = REPO / "knowledge" / "samples"
QUESTIONS = REPO / "knowledge" / "eval" / "samples.yaml"


@pytest.fixture()
def batch_root(tmp_path, monkeypatch):
    """资料目录：一个导入批次（样例条目，key 换成本测试专用）、一个评测题目文件、一个下划线开头的隐藏目录。"""
    tag = "bt" + secrets.token_hex(3)
    d = tmp_path / "downloads" / "2026-11-samples"
    shutil.copytree(SAMPLES, d)
    for f in d.glob("*.md"):
        f.write_text(f.read_text(encoding="utf-8").replace("key: ", f"key: {tag}-", 1), encoding="utf-8")
    (tmp_path / "downloads" / "eval").mkdir()
    shutil.copy(QUESTIONS, tmp_path / "downloads" / "eval" / "samples.yaml")
    (tmp_path / "downloads" / "_rejected").mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setattr(get_settings(), "kb_batch_dir", str(tmp_path / "downloads"))
    return tmp_path, tag


@pytest.fixture()
def kb_admin():
    user = create_user(("knowledge_admin",))
    c = logged_in_client(app, user)
    yield c, user[0]
    c.__exit__(None, None, None)


def _drain() -> None:
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=[batches.IMPORT_JOB, batches.EVAL_JOB]):
            pass


def test_sources_lists_dirs_and_question_files(batch_root, kb_admin):
    c, _ = kb_admin
    r = c.get("/api/kb/batches/sources")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "2026-11-samples" in body["dirs"] and "eval" in body["dirs"]
    assert body["questions"] == ["eval/samples.yaml"]
    assert not any(p.startswith("_") for p in body["dirs"])          # 下划线开头的不列
    assert "root" not in body                                         # 不暴露部署路径


def test_page_import_is_queued_then_worker_imports_and_lists_counts(batch_root, kb_admin):
    _, tag = batch_root
    c, username = kb_admin
    r = c.post("/api/kb/batches", json={"kind": "import", "path": "2026-11-samples", "submit": True})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    with SessionLocal() as db:                                         # 只排队，请求里不导入
        assert db.get(Job, job_id).status == jobs.QUEUED
        assert db.scalar(select(KbItem).where(KbItem.import_key == f"{tag}-soc-night-duty")) is None
    listed = next(b for b in c.get("/api/kb/batches").json() if b["job_id"] == job_id)
    assert (listed["kind"], listed["name"], listed["status"], listed["source"], listed["created_by"]) == \
        ("import", "导入", "queued", "page", username)

    _drain()
    with SessionLocal() as db:
        item = db.scalar(select(KbItem).where(KbItem.import_key == f"{tag}-soc-night-duty"))
        assert item is not None and item.versions[-1].status == "in_review"     # --submit：已提交审核
        rows = [json.loads(a.detail) for a in db.scalars(select(AuditLog).where(AuditLog.action == batches.ACTION))]
    done = next(d for d in rows if d.get("job_id") == job_id)
    assert done["counts"]["created"] == 10 and done["counts"]["submit"] is True
    assert "正文" not in json.dumps(done, ensure_ascii=False) and "body" not in done   # 审计只记计数

    listed = next(b for b in c.get("/api/kb/batches").json() if b["job_id"] == job_id)
    assert listed["status"] == "done" and listed["counts"]["created"] == 10 and listed["finished_at"]


def test_page_eval_is_queued_and_records_accuracy(batch_root, kb_admin):
    c, _ = kb_admin
    r = c.post("/api/kb/batches", json={"kind": "eval", "path": "eval/samples.yaml"})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    _drain()
    listed = next(b for b in c.get("/api/kb/batches").json() if b["job_id"] == job_id)
    assert listed["status"] == "done" and listed["name"] == "评测"
    counts = listed["counts"]
    assert counts["total"] > 0 and counts["compare"] is False and 0 <= counts["accuracy"] <= 1
    assert set(counts) == {"total", "compare", "accuracy", "passed"}   # 不带题目、答案


@pytest.mark.parametrize(("body", "status"), [
    ({"kind": "import", "path": "../outside"}, 400),
    ({"kind": "import", "path": "/etc"}, 400),
    ({"kind": "import", "path": "C:/Windows"}, 400),
    ({"kind": "import", "path": "2026-11-samples/../../outside"}, 400),
    ({"kind": "import", "path": "no-such-batch"}, 404),
    ({"kind": "import", "path": "eval/samples.yaml"}, 404),            # 导入要目录
    ({"kind": "eval", "path": "2026-11-samples"}, 404),                # 评测要 .yaml 文件
    ({"kind": "draft", "path": "2026-11-samples"}, 400),               # 页面只能发起导入、评测
])
def test_bad_paths_and_kinds_rejected(batch_root, kb_admin, body, status):
    c, _ = kb_admin
    with SessionLocal() as db:
        before = db.scalar(select(Job.id).order_by(Job.id.desc()).limit(1)) or 0
    assert c.post("/api/kb/batches", json=body).status_code == status
    with SessionLocal() as db:
        assert (db.scalar(select(Job.id).order_by(Job.id.desc()).limit(1)) or 0) == before   # 没排任务


@pytest.mark.parametrize("roles", [("knowledge_expert",), ("consultant",)])
def test_only_knowledge_admin(batch_root, roles):
    c = logged_in_client(app, create_user(roles))
    try:
        assert c.get("/api/kb/batches").status_code == 403
        assert c.get("/api/kb/batches/sources").status_code == 403
        assert c.post("/api/kb/batches", json={"kind": "import", "path": "2026-11-samples"}).status_code == 403
    finally:
        c.__exit__(None, None, None)


def test_post_requires_csrf(batch_root, kb_admin):
    c, _ = kb_admin
    token = c.headers.pop("X-CSRF-Token")
    try:
        assert c.post("/api/kb/batches", json={"kind": "import", "path": "2026-11-samples"}).status_code == 403
    finally:
        c.headers["X-CSRF-Token"] = token


def test_job_fails_without_retry_when_starter_lost_admin_role(batch_root, kb_admin):
    from app.core.permissions import set_roles
    from app.models import User

    c, username = kb_admin
    job_id = c.post("/api/kb/batches", json={"kind": "import", "path": "2026-11-samples"}).json()["job_id"]
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == username))
        set_roles(db, u, ["knowledge_expert"], granted_by="test")
        db.commit()
    _drain()
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == jobs.FAILED and job.attempts == 1 and "知识管理员" in job.last_error


def test_cli_import_also_listed(batch_root, kb_admin):
    root, _ = batch_root
    c, username = kb_admin
    assert cli_main(["kb-import", str(root / "downloads" / "2026-11-samples"), "--as", username]) == 0
    b = next(b for b in c.get("/api/kb/batches").json() if b["source"] == "cli" and b["created_by"] == username)
    assert (b["kind"], b["status"], b["path"], b["job_id"]) == ("import", "done", "2026-11-samples", None)
    assert b["counts"]["total"] == 10
