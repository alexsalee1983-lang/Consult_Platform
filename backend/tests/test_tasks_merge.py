"""四期试用反馈：工作台并入“我的任务”。一个待办入口、一套计数口径；/api/workbench 保留兼容，口径同“我的任务”。"""
import secrets
from datetime import date

from app.core.db import SessionLocal
from app.main import app
from app.models import Confirmation, Deliverable, Project, ProjectDeletion
from app.models.kb import KbItem, KbUpload, KbVersion
from app.project import my_tasks as mt

from .conftest import create_user, logged_in_client

FILTER_KINDS = ("confirmation", "deliverable_review", "kb_content", "kb_compliance", "recheck", "deid", "deletion")


def _project() -> tuple[str, int]:
    code = f"TM-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code="D4", title="现状评估报告", status="review")]
        db.add(p)
        db.commit()
        return code, p.id


def _version(tag: str, author: str, status: str = "in_review", single: bool = False) -> int:
    with SessionLocal() as db:
        item = KbItem(doc_id=f"L2-OPS-SOC-TPL-{tag}", layer="L2", domain="OPS", product="SOC", doc_type="TPL",
                      stage="S3", classification="internal", owner=author)
        db.add(item)
        db.flush()
        v = KbVersion(item_id=item.id, version=1, title=f"合并{tag}", body="正文", status=status, source_type="public",
                      created_by=author, submitted_by=author, single_review=single)
        db.add(v)
        db.flush()
        if status == "published":
            item.current_version_id = v.id
        db.commit()
        return v.id


def test_my_tasks_covers_all_former_workbench_kinds_with_one_count():
    code, pid = _project()
    other_code, other_pid = _project()
    multi = create_user(("consultant", "project_lead", "knowledge_expert", "knowledge_admin"), {code: "manager"})
    me = multi[0]
    writer = create_user(("knowledge_expert",))[0]
    tag = secrets.token_hex(2).upper()
    vid = _version(f"{tag}A", writer)
    recheck_id = _version(f"{tag}R", writer, status="published", single=True)
    with SessionLocal() as db:
        upload = KbUpload(object_key=f"tm/{secrets.token_hex(4)}", filename="y.docx", sha256=secrets.token_hex(32),
                          status="deid_pending", uploaded_by=me)
        dele = ProjectDeletion(project_id=other_pid, kind="early", status="pending", requested_by=me,
                               due_by=date.today())
        db.add_all([upload, dele, Confirmation(project_id=pid, kind="scope", title="合并确认卡", agent="方案设计 Agent")])
        db.commit()
        upload_id, dele_id = upload.id, dele.id
    with logged_in_client(app, multi) as c:
        body = c.get("/api/my-tasks").json()
        wb = c.get("/api/workbench").json()
    assert set(FILTER_KINDS) <= set(body["kinds"])                       # 筛选条七类都有位置
    assert body["member_projects"] >= 1
    tasks = body["tasks"]
    by = {(t["kind"], t["ref_id"]): t for t in tasks}
    assert ("kb_content", vid) in by and ("kb_compliance", vid) in by     # 一人兼两角色：内容、合规各一项
    assert by[("kb_content", vid)]["link"] == f"/kb/bench?v={vid}"        # 内容审核一键进专家工作台对应卡
    assert by[("kb_compliance", vid)]["link"] == f"/kb/review?version={vid}"
    assert by[("recheck", recheck_id)]["link"] == "/kb/review?tab=recheck"  # 保留 #465 的页签链接
    # A29：按规则须由别人处理的（上传者确认去标识、发起人确认删除）直接隐藏，不灰显
    assert ("deid", upload_id) not in by and ("deletion", dele_id) not in by
    assert all(not t["blocked"] for t in tasks)
    assert "y.docx" not in str(body)                                    # 不给上传文件名
    for k in FILTER_KINDS:                                               # 计数口径：kinds 计数 = 本类任务数
        assert body["kinds"][k]["count"] == sum(t["kind"] == k for t in tasks)
    # 兼容接口与“我的任务”同一口径：本人可处理数一致（侧栏角标只用一种算法）
    assert sum(not t["blocked"] for t in wb["todos"]) == sum(not t["blocked"] for t in tasks)
    assert len(wb["todos"]) == len(tasks) and wb["member_projects"] == body["member_projects"]
    assert ("signoff", code) in {(t["kind"], t["project_code"]) for t in wb["todos"]}  # 旧类名照旧


def test_return_kb_content_hides_both_review_roles_of_that_version():
    multi = create_user(("knowledge_expert", "knowledge_admin"))
    writer = create_user(("knowledge_expert",))[0]
    vid = _version(f"{secrets.token_hex(2).upper()}B", writer)
    with logged_in_client(app, multi) as c:
        r = c.post("/api/my-tasks/return", json={"kind": "kb_content", "ref_id": vid, "reason": "依据不对"})
        assert r.status_code == 202, r.text
        left = {(t["kind"], t["ref_id"]) for t in c.get("/api/my-tasks").json()["tasks"]}
    assert ("kb_content", vid) not in left and ("kb_compliance", vid) not in left
    assert {"kb_content", "kb_compliance"} <= mt.RETURNABLE


def test_member_projects_zero_for_new_user():
    u = create_user(("consultant",))
    with logged_in_client(app, u) as c:
        body = c.get("/api/my-tasks").json()
    assert body["member_projects"] == 0
