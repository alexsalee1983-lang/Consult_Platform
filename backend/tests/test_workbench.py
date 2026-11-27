"""工作台：汇总一个人各角色的待办；本人不能处理的置灰并写要谁处理。"""
import secrets
from datetime import date

from app.core.db import SessionLocal
from app.main import app
from app.models import Confirmation, Deliverable, Project, ProjectDeletion
from app.models.kb import KbUpload

from .conftest import create_user, logged_in_client


def _project() -> tuple[str, int]:
    code = f"WB-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code="D4", title="现状评估报告", status="review"),
                          Deliverable(code="D5", title="目标运营模型", status="drafting")]
        db.add(p)
        db.commit()
        return code, p.id


def test_workbench_aggregates_roles_and_marks_self_conflicts():
    code, pid = _project()
    other_code, other_pid = _project()
    multi = create_user(("consultant", "project_lead", "knowledge_expert", "knowledge_admin"), {code: "manager"})
    me = multi[0]
    with SessionLocal() as db:
        upload = KbUpload(object_key=f"wb/{secrets.token_hex(4)}", filename="x.docx", sha256=secrets.token_hex(32),
                          status="deid_pending", uploaded_by=me)
        db.add_all([upload,
            Confirmation(project_id=pid, kind="scope", title="确认值班模式", agent="方案设计 Agent"),
            Confirmation(project_id=other_pid, kind="scope", title="别的项目的卡", agent="方案设计 Agent"),
            ProjectDeletion(project_id=other_pid, kind="early", status="pending", requested_by=me, due_by=date.today()),
        ])
        db.commit()
        upload_id = upload.id
    with logged_in_client(app, multi) as c:
        r = c.get("/api/workbench")
        assert r.status_code == 200
        body = r.json()
        assert {"consultant", "project_lead", "knowledge_expert", "knowledge_admin"} <= set(body["roles"])
        todos = body["todos"]
        kinds = {(t["kind"], t["project_code"]) for t in todos}
        assert ("confirmation", code) in kinds and ("confirmation", other_code) not in kinds   # 非成员项目的卡不出现
        assert ("signoff", code) in kinds and sum(t["kind"] == "signoff" for t in todos) == 1
        dele = next(t for t in todos if t["kind"] == "deletion" and t["project_code"] == other_code)
        assert "另一位咨询业务负责人" in dele["blocked"]                                            # 发起人不能自己确认
        deid = next(t for t in todos if t["kind"] == "deid" and t["title"] == f"上传 #{upload_id}")
        assert "另一位知识管理员" in deid["blocked"]                                           # 上传者不能确认去标识
        assert "x.docx" not in r.text                                                           # 不给上传文件名
    consultant = create_user(("consultant",), {code: "member"})
    with logged_in_client(app, consultant) as c:
        todos = c.get("/api/workbench").json()["todos"]
        assert {t["kind"] for t in todos} == {"confirmation"}                                  # 不是经理、不是知识角色
