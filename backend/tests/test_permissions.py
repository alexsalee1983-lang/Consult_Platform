"""权限矩阵：角色 × 接口逐格断言，以及三权分立、项目成员管理的规则。

改动 app/core/permissions.py 或任何接口的权限时，必须同步更新这里。
"""
import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core import sessions
from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT, RoleRuleError, roles_of, set_roles
from app.main import app
from app.models import AuditLog, Project, ProjectMember, User

from .conftest import create_user, logged_in_client

# 账号类型 → (系统角色, 项目成员身份)
ACTORS = {
    "member":    ((CONSULTANT,), {"BANK-A": "manager"}),   # BANK-A 的项目经理
    "outsider":  ((CONSULTANT,), {"MFG-C": "member"}),     # 顾问，但不是 BANK-A 成员
    "sys_admin": (("sys_admin",), {}),
    "auditor":   (("security_auditor",), {}),
    "no_role":   ((), {}),
}
COLUMNS = list(ACTORS)

CHAT = {"project_code": "BANK-A", "message": "一期人力模型怎么选？"}
OUTLINE = {"titles": ["权限矩阵：现状", "权限矩阵：差距"]}
SECTION = {"body": "权限矩阵测试正文：值班台交接记录为纸质 [O-01]。"}
NOTE = {"kind": "observation", "title": "权限矩阵：值班室巡查", "content": "值班台有两块大屏，交接班记录为纸质。"}
# (方法, 路径, 请求体) → 每类账号的期望状态码，顺序同 COLUMNS
MATRIX = [
    (("GET", "/api/auth/me", None),                     (200, 200, 200, 200, 200)),
    (("GET", "/api/projects", None),                    (200, 200, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A", None),             (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/GRID-B", None),             (404, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/members", None),     (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/evidences", None),   (200, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/notes", NOTE),      (201, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/evidences/O-01", None), (200, 404, 403, 403, 403)),   # 上一行建的第一条观察
    (("POST", "/api/projects/GRID-B/notes", NOTE),      (404, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7", None), (200, 404, 403, 403, 403)),
    (("PUT", "/api/projects/BANK-A/deliverables/D7/outline", OUTLINE), (200, 404, 403, 403, 403)),
    (("PUT", "/api/projects/BANK-A/deliverables/D7/sections/1", SECTION), (201, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7/sections/1", None), (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7/sections/1/diff?a=1&b=1", None), (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7/export", None), (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/GRID-B/deliverables/D2/export", None), (404, 404, 403, 403, 403)),
    (("PUT", "/api/projects/GRID-B/deliverables/D2/outline", OUTLINE), (404, 404, 403, 403, 403)),
    # 质量门禁与签署（M3 ⑤）：成员可看；发起评审在 test_quality 里测（这里发起会留下排队的评审）。
    # member 是 BANK-A 项目经理：D7 没有评审，提交签署门禁不过 409；不在待签署状态，退回 409
    (("GET", "/api/projects/BANK-A/deliverables/D7/reviews", None), (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7/reviews/999999", None), (404, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/deliverables/D2/reviews", None), (404, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7/signoff", None), (200, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/deliverables/D7/signoff/submit", {}), (409, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/deliverables/D7/signoff/return", {"comment": "退回理由"}),
     (409, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/deliverables/D2/signoff/sign", {}), (404, 404, 403, 403, 403)),
    # 驳回评审 Agent 的阻断（M3 ⑧）：评审不存在 404；非成员 404；驳回规则在 test_quality 里测
    (("POST", "/api/projects/BANK-A/deliverables/D7/reviews/999999/waivers", {"issue": 0, "reason": "理由"}),
     (404, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/deliverables/D2/reviews/1/waivers", {"issue": 0, "reason": "理由"}),
     (404, 404, 403, 403, 403)),
    # 交付链运行（M3 ④）：成员可看；发起在 test_runs 里测（这里发起会在 BANK-A 留下排队的运行）
    (("GET", "/api/projects/BANK-A/runs", None),        (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/runs/999999", None), (404, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/runs", None),       (404, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/runs/999999/cancel", None), (404, 404, 403, 403, 403)),
    (("GET", "/api/confirmations", None),               (200, 200, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/confirmations", None), (200, 404, 403, 403, 403)),
    (("POST", "/api/chat", CHAT),                       (200, 404, 403, 403, 403)),
    (("POST", "/api/chat/stream", CHAT),                (200, 404, 403, 403, 403)),
    (("POST", "/api/gateway/check", {"text": "问题"}),  (200, 200, 403, 403, 403)),
    (("GET", "/api/audit", None),                       (403, 403, 403, 200, 403)),
    (("GET", "/api/audit/verify", None),                (403, 403, 403, 200, 403)),
    (("GET", "/api/admin/users", None),                 (403, 403, 200, 403, 403)),
    (("GET", "/api/admin/roles", None),                 (403, 403, 200, 403, 403)),
    (("GET", "/api/admin/usage", None),                 (403, 403, 200, 403, 403)),
    (("GET", "/api/admin/agent-grants", None),          (403, 403, 200, 403, 403)),
    (("POST", "/api/admin/users", {"username": "matrix_new"}), (403, 403, 201, 403, 403)),
    (("POST", "/api/admin/users/matrix_target/reset-password", None), (403, 403, 200, 403, 403)),
    (("POST", "/api/admin/users/matrix_target/reset-totp", None),     (403, 403, 200, 403, 403)),
    (("GET", "/api/kb/intake", None),                   (200, 200, 403, 403, 403)),
    (("GET", "/api/kb/items", None),                    (200, 200, 403, 403, 403)),
    (("GET", "/api/kb/search?q=值班", None),            (200, 200, 403, 403, 403)),
    (("GET", "/api/kb/meta", None),                     (200, 200, 403, 403, 403)),
    (("GET", "/api/kb/stats", None),                    (403, 403, 403, 403, 403)),   # 只限知识专家、知识管理员
    (("GET", "/api/kb/review-queue", None),             (403, 403, 403, 403, 403)),   # 只限知识专家、知识管理员
    (("GET", "/api/admin/jobs", None),                  (403, 403, 200, 403, 403)),
    (("POST", "/api/admin/jobs/selfcheck", None),       (403, 403, 202, 403, 403)),
    (("GET", "/api/users/lookup?q=u_", None),           (200, 200, 403, 403, 403)),
    (("POST", "/api/auth/password", {"old_password": "x", "new_password": "y"}),
                                                        (400, 400, 400, 400, 400)),
    (("GET", "/api/agents", None),                      (200, 200, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/conversations", None), (200, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/conversations", {"agent_code": "consultant-lead"}),
                                                        (201, 404, 403, 403, 403)),
    (("POST", "/api/agents/consultant-lead/tools/project.read", {"project_code": "BANK-A"}),
                                                        (200, 404, 403, 403, 403)),
    (("POST", "/api/agents/kb-builder/tools/project.read", {"project_code": "BANK-A"}),
                                                        (403, 403, 403, 403, 403)),
]


@pytest.fixture(scope="module")
def clients():
    create_user((CONSULTANT,), username="matrix_target")   # 重置口令、动态码的操作对象
    made = {name: logged_in_client(app, create_user(roles, projects)) for name, (roles, projects) in ACTORS.items()}
    yield made
    for c in made.values():
        c.__exit__(None, None, None)


@pytest.mark.parametrize(("request_", "expected"), MATRIX, ids=[f"{m} {p}" for (m, p, _), _ in MATRIX])
def test_permission_matrix(clients, request_, expected):
    method, path, body = request_
    got = tuple(clients[name].request(method, path, json=body).status_code for name in COLUMNS)
    assert got == expected, f"{method} {path}：{dict(zip(COLUMNS, got))}"


def test_non_member_cannot_resolve_confirmation(clients):
    bank_conf = next(c for c in clients["member"].get("/api/confirmations").json() if c["project_code"] == "BANK-A")
    r = clients["outsider"].post(f"/api/confirmations/{bank_conf['id']}/resolve", json={"resolution": "x"})
    assert r.status_code == 404


# ---- 三权分立 ----

@pytest.fixture()
def admin():
    c = logged_in_client(app, create_user(("sys_admin",)))
    yield c
    c.__exit__(None, None, None)


def test_sys_admin_grants_business_role_and_is_audited(admin):
    name, *_ = create_user()
    r = admin.put(f"/api/admin/users/{name}/roles", json={"roles": ["consultant", "project_lead"]})
    assert r.status_code == 200 and r.json()["roles"] == ["consultant", "project_lead"]
    with SessionLocal() as db:
        row = db.scalars(select(AuditLog).where(AuditLog.action == "user.roles").order_by(AuditLog.id.desc())).first()
    assert row.result == "allowed" and name in row.detail


def test_sys_admin_cannot_grant_auditor(admin):
    name, *_ = create_user((CONSULTANT,))
    r = admin.put(f"/api/admin/users/{name}/roles", json={"roles": ["security_auditor"]})
    assert r.status_code == 403
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == name))
        assert roles_of(db, u.id) == {CONSULTANT}
        row = db.scalars(select(AuditLog).where(AuditLog.action == "user.roles").order_by(AuditLog.id.desc())).first()
    assert row.result == "blocked" and "grant_auditor" in row.detail


def test_sys_admin_cannot_change_own_roles_or_disable_self(admin):
    me = admin.get("/api/auth/me").json()["username"]
    assert admin.put(f"/api/admin/users/{me}/roles", json={"roles": ["consultant"]}).status_code == 403
    assert admin.post(f"/api/admin/users/{me}/disable").status_code == 403


def test_sys_admin_cannot_touch_auditor_account(admin):
    name, *_ = create_user(("security_auditor",))
    assert admin.put(f"/api/admin/users/{name}/roles", json={"roles": []}).status_code == 403
    assert admin.post(f"/api/admin/users/{name}/disable").status_code == 403
    assert admin.post(f"/api/admin/users/{name}/unlock").status_code == 403


def test_exclusive_roles_rejected(admin):
    name, *_ = create_user()
    r = admin.put(f"/api/admin/users/{name}/roles", json={"roles": ["sys_admin", "consultant"]})
    assert r.status_code == 422
    with pytest.raises(RoleRuleError):
        with SessionLocal() as db:
            set_roles(db, db.scalar(select(User).where(User.username == name)), {"security_auditor", "sys_admin"}, "t")


def test_becoming_sys_admin_removes_project_memberships(admin):
    name, *_ = create_user((CONSULTANT,), {"MFG-C": "member"})
    assert admin.put(f"/api/admin/users/{name}/roles", json={"roles": ["sys_admin"]}).status_code == 200
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == name))
        assert db.scalar(select(ProjectMember).where(ProjectMember.user_id == u.id)) is None


def test_disable_user_ends_their_sessions(admin):
    user = create_user((CONSULTANT,), {"MFG-C": "member"})
    c = logged_in_client(app, user)
    try:
        assert c.get("/api/projects").status_code == 200
        assert admin.post(f"/api/admin/users/{user[0]}/disable").status_code == 200
        assert c.get("/api/projects").status_code == 401
    finally:
        c.__exit__(None, None, None)


def test_auditor_only_via_cli(capsys):
    name, *_ = create_user()
    assert cli_main(["grant-role", "--username", name, "--role", "security_auditor"]) == 0
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == name))
        assert roles_of(db, u.id) == {"security_auditor"}
        row = db.scalars(select(AuditLog).where(AuditLog.action == "user.role_grant")
                         .order_by(AuditLog.id.desc())).first()
    assert row.actor == "cli" and name in row.detail
    # 命令行同样执行互斥规则
    assert cli_main(["grant-role", "--username", name, "--role", "consultant"]) == 1
    assert cli_main(["revoke-role", "--username", name, "--role", "security_auditor"]) == 0


def test_role_change_via_cli_ends_sessions_for_exclusive_roles():
    user = create_user((CONSULTANT,), {"MFG-C": "member"})
    c = logged_in_client(app, user)
    try:
        token = c.cookies.get(sessions.COOKIE_NAME)
        assert cli_main(["revoke-role", "--username", user[0], "--role", "consultant"]) == 0
        assert cli_main(["grant-role", "--username", user[0], "--role", "sys_admin"]) == 0
        assert sessions.load(token) is None
    finally:
        c.__exit__(None, None, None)


def test_cli_help_works(capsys):
    """顶层和每个子命令的 --help 都能显示（help 文本里的 % 要写成 %%）。"""
    for argv in (["--help"], *([c, "--help"] for c in ("add-member", "kb-eval", "grant-role", "usage-report"))):
        with pytest.raises(SystemExit) as e:
            cli_main(argv)
        assert e.value.code == 0
    assert "add-member" in capsys.readouterr().out


def test_add_member_via_cli_bootstraps_project_without_manager():
    """没有项目经理的项目，网页上无人能加成员；命令行可指定首位经理，写审计，规则与网页一致。"""
    user = create_user((CONSULTANT,))
    c = logged_in_client(app, user)
    try:
        assert c.get("/api/projects/GRID-B").status_code == 404
        assert cli_main(["add-member", "--project", "GRID-B", "--username", user[0], "--role", "manager"]) == 0
        assert c.get("/api/projects/GRID-B").status_code == 200
        with SessionLocal() as db:
            row = db.scalars(select(AuditLog).where(AuditLog.action == "project.member")
                             .order_by(AuditLog.id.desc())).first()
        assert row.actor == "cli" and user[0] in row.detail
        # 唯一的项目经理不能被降级
        assert cli_main(["add-member", "--project", "GRID-B", "--username", user[0], "--role", "member"]) == 1
    finally:
        c.__exit__(None, None, None)
    # 没有业务角色的账号、不存在的项目都拒绝
    admin, *_ = create_user(("sys_admin",))
    assert cli_main(["add-member", "--project", "GRID-B", "--username", admin, "--role", "member"]) == 1
    assert cli_main(["add-member", "--project", "NOPE-X", "--username", user[0], "--role", "member"]) == 1


# ---- 项目成员管理：只有本项目的项目经理能调整 ----

def test_manager_adds_member_who_then_sees_project():
    manager = logged_in_client(app, create_user((CONSULTANT,), {"GRID-B": "manager"}))
    newcomer_user = create_user((CONSULTANT,))
    newcomer = logged_in_client(app, newcomer_user)
    try:
        assert newcomer.get("/api/projects/GRID-B").status_code == 404
        r = manager.put(f"/api/projects/GRID-B/members/{newcomer_user[0]}", json={"project_role": "member"})
        assert r.status_code == 200
        assert newcomer.get("/api/projects/GRID-B").status_code == 200
        # 普通成员不能再加人
        third, *_ = create_user((CONSULTANT,))
        assert newcomer.put(f"/api/projects/GRID-B/members/{third}", json={"project_role": "member"}).status_code == 403
        # 移出后又看不到
        assert manager.delete(f"/api/projects/GRID-B/members/{newcomer_user[0]}").status_code == 200
        assert newcomer.get("/api/projects/GRID-B").status_code == 404
    finally:
        manager.__exit__(None, None, None)
        newcomer.__exit__(None, None, None)


def test_last_manager_cannot_leave_and_admins_cannot_join():
    with SessionLocal() as db:  # 独立的虚拟项目，不受其他测试加入的成员影响
        db.add(Project(code="TEST-LM", service="SOC 运营体系设计", industry="测试", tier="standard",
                       phase=0, week=1, total_weeks=12, next_milestone=""))
        db.commit()
    user = create_user((CONSULTANT,), {"TEST-LM": "manager"})
    c = logged_in_client(app, user)
    try:
        assert c.delete(f"/api/projects/TEST-LM/members/{user[0]}").status_code == 422
        assert c.put(f"/api/projects/TEST-LM/members/{user[0]}", json={"project_role": "member"}).status_code == 422
        sysadmin, *_ = create_user(("sys_admin",))
        assert c.put(f"/api/projects/TEST-LM/members/{sysadmin}", json={"project_role": "member"}).status_code == 422
    finally:
        c.__exit__(None, None, None)
