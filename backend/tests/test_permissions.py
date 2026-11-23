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

OUTLINE = {"titles": ["权限矩阵：现状", "权限矩阵：差距"]}
SECTION = {"body": "权限矩阵测试正文：值班台交接记录为纸质 [O-01]。"}
NOTE = {"kind": "observation", "title": "权限矩阵：值班室巡查", "content": "值班台有两块大屏，交接班记录为纸质。"}
# (方法, 路径, 请求体) → 每类账号的期望状态码，顺序同 COLUMNS
MATRIX = [
    (("GET", "/api/auth/me", None),                     (200, 200, 200, 200, 200)),
    (("GET", "/api/projects", None),                    (200, 200, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A", None),             (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/GRID-B", None),             (404, 404, 403, 403, 403)),
    # 项目总览（方案 A）：只给项目负责人；顾问 403（负责人 200 在 test_portfolio 里测）
    (("GET", "/api/portfolio", None),                   (403, 403, 403, 403, 403)),
    # 工作台：业务角色可用，只汇总本人能看到的待办
    (("GET", "/api/workbench", None),                   (200, 200, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/members", None),     (200, 404, 403, 403, 403)),
    # 网页立项（二期 A3）：要项目负责人角色，顾问（含项目经理）403
    (("GET", "/api/catalog/products", None),            (200, 200, 403, 403, 403)),
    (("POST", "/api/projects", {"code": "QA-MTX", "tier": "standard", "product": "SOC", "industry": "其他"}),
                                                        (403, 403, 403, 403, 403)),
    # 验收与保留期（M4 ①）：member 是 BANK-A 项目经理，验收日写在将来 → 422（不改 BANK-A）；非成员 404
    (("POST", "/api/projects/BANK-A/acceptance", {"accepted_on": "2999-01-01"}), (422, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/acceptance", {"accepted_on": "2026-01-01"}), (404, 404, 403, 403, 403)),
    # 删除（M4 ②）：确认队列只给项目负责人（矩阵里没有这个角色，负责人的 200 在 test_deletion 里测）；
    # member 是 BANK-A 项目经理：收到日期写在将来 → 422（不在 BANK-A 留下申请）；非成员 404
    (("GET", "/api/deletions", None),                   (403, 403, 403, 403, 403)),
    (("GET", "/api/deletions/999999", None),            (403, 403, 403, 403, 403)),
    (("POST", "/api/deletions/999999/confirm", {"flowback": "none"}), (403, 403, 403, 403, 403)),
    (("POST", "/api/deletions/999999/cancel", {"reason": "客户书面撤回"}), (403, 403, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deletions", None),   (200, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/deletions", {"request_ref": "函-1", "received_on": "2999-01-01"}),
     (422, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/deletions", {"request_ref": "函-1", "received_on": "2026-01-01"}),
     (404, 404, 403, 403, 403)),
    (("PUT", "/api/projects/GRID-B/flowback", {"allowed": False}), (404, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/evidences", None),   (200, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/notes", NOTE),      (201, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/evidences/O-01", None), (200, 404, 403, 403, 403)),   # 上一行建的第一条观察
    (("POST", "/api/projects/GRID-B/notes", NOTE),      (404, 404, 403, 403, 403)),
    # 纪要文件提取正文：member 不带文件 → 422（不保存任何东西）；非成员 404
    (("POST", "/api/projects/BANK-A/notes/extract", None), (422, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/notes/extract", None), (404, 404, 403, 403, 403)),
    # 仅现场使用（M4 ③）：member 是 BANK-A 项目经理，对观察 O-01 标注 → 422（只有资料能标，不改数据）；非成员 404
    (("POST", "/api/projects/BANK-A/evidences/O-01/local-only", {"request_ref": "函-1"}), (422, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/evidences/D-01/local-only", {"request_ref": "函-1"}), (404, 404, 403, 403, 403)),
    # 证据改名、删除：member 改名为空标题 → 422（不改数据）；删除不存在的 O-99 → 404；非成员 404
    (("PATCH", "/api/projects/BANK-A/evidences/O-01", {"title": ""}), (422, 404, 403, 403, 403)),
    (("PATCH", "/api/projects/GRID-B/evidences/D-01", {"title": "新标题"}), (404, 404, 403, 403, 403)),
    (("DELETE", "/api/projects/BANK-A/evidences/O-99", None), (404, 404, 403, 403, 403)),
    (("DELETE", "/api/projects/GRID-B/evidences/D-01", None), (404, 404, 403, 403, 403)),
    # 客户同意与摘要卡（M4 ④）：BANK-A 是标准档 → 登记 / 上传 / 模板 422，查看 200；GRID-B（严格档）非成员 404
    (("GET", "/api/projects/BANK-A/consents", None), (200, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/consents", {"doc_ref": "计划-1", "signed_on": "2026-01-01"}),
     (422, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/consents/999999/revoke", {"revoke_ref": "撤-1", "received_on": "2026-01-01"}),
     (404, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/summary-cards", None), (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/summary-cards/template", None), (422, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/summary-cards", {"cards": [{"item": "账号复核", "finding": "未复核"}]}),
     (422, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/summary-cards/S-01/withdraw", None), (404, 404, 403, 403, 403)),
    (("GET", "/api/projects/GRID-B/consents", None), (404, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/consents", {"doc_ref": "计划-1", "signed_on": "2026-01-01"}),
     (404, 404, 403, 403, 403)),
    (("GET", "/api/projects/GRID-B/summary-cards", None), (404, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/summary-cards", {"cards": [{"item": "账号复核", "finding": "未复核"}]}),
     (404, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7", None), (200, 404, 403, 403, 403)),
    (("PUT", "/api/projects/BANK-A/deliverables/D7/outline", OUTLINE), (200, 404, 403, 403, 403)),
    (("PUT", "/api/projects/BANK-A/deliverables/D7/sections/1", SECTION), (201, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7/sections/1", None), (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7/sections/1/diff?a=1&b=1", None), (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7/export", None), (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/GRID-B/deliverables/D2/export", None), (404, 404, 403, 403, 403)),
    # 改稿差异转候选（三期 F4）：开关默认关，成员也 404；非成员 404、非业务角色 403（开关打开的行为在 test_revision_mining）
    (("POST", "/api/projects/BANK-A/deliverables/D7/revision-candidates", None), (404, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/deliverables/D7/export?format=pptx", None), (200, 404, 403, 403, 403)),
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
    (("GET", "/api/projects/BANK-A/chain", None),       (200, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/runs/999999", None), (404, 404, 403, 403, 403)),
    (("POST", "/api/projects/GRID-B/runs", None),       (404, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/runs/999999/cancel", None), (404, 404, 403, 403, 403)),
    (("GET", "/api/confirmations", None),               (200, 200, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/confirmations", None), (200, 404, 403, 403, 403)),
    (("POST", "/api/gateway/check", {"text": "问题"}),  (200, 200, 403, 403, 403)),
    (("GET", "/api/audit", None),                       (403, 403, 403, 200, 403)),
    (("GET", "/api/audit/verify", None),                (403, 403, 403, 200, 403)),
    (("GET", "/api/admin/users", None),                 (403, 403, 200, 403, 403)),
    (("GET", "/api/admin/roles", None),                 (403, 403, 200, 403, 403)),
    (("GET", "/api/admin/usage", None),                 (403, 403, 200, 403, 403)),
    (("GET", "/api/admin/models", None),                (403, 403, 200, 403, 403)),
    (("GET", "/api/admin/agent-grants", None),          (403, 403, 200, 403, 403)),
    (("POST", "/api/admin/users", {"username": "matrix_new"}), (403, 403, 201, 403, 403)),
    (("POST", "/api/admin/users/matrix_target/reset-password", None), (403, 403, 200, 403, 403)),
    (("POST", "/api/admin/users/matrix_target/reset-totp", None),     (403, 403, 200, 403, 403)),
    (("GET", "/api/kb/intake", None),                   (200, 200, 403, 403, 403)),
    (("GET", "/api/kb/items", None),                    (200, 200, 403, 403, 403)),
    (("GET", "/api/kb/items/L4-NOPE/usage", None),      (403, 403, 403, 403, 403)),   # 引用计数只限知识专家、知识管理员（B4）
    (("GET", "/api/projects/BANK-A/knowledge-updates", None), (200, 404, 403, 403, 403)),   # B4：只给项目成员
    (("GET", "/api/projects/GRID-B/knowledge-updates", None), (404, 404, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/assessment", None), (200, 404, 403, 403, 403)),        # A10：只给项目成员
    (("GET", "/api/projects/GRID-B/assessment", None), (404, 404, 403, 403, 403)),
    (("GET", "/api/kb/search?q=值班", None),            (200, 200, 403, 403, 403)),
    (("GET", "/api/kb/sources", None),                  (403, 403, 403, 403, 403)),   # 来源登记表只限知识专家、知识管理员（B16）
    (("POST", "/api/kb/sources/SR-999999/status", {"to": "trial"}), (403, 403, 403, 403, 403)),
    (("GET", "/api/kb/sources/confirm-queue", None),    (403, 403, 403, 403, 403)),   # 来源确认队列（B25②），同上
    (("POST", "/api/kb/sources/confirm-queue/decide", {"gap_ids": [999999], "decision": "reject"}),
     (403, 403, 403, 403, 403)),
    (("GET", "/api/kb/meta", None),                     (200, 200, 403, 403, 403)),
    (("GET", "/api/kb/stats", None),                    (403, 403, 403, 403, 403)),   # 只限知识专家、知识管理员
    (("GET", "/api/kb/review-queue", None),             (403, 403, 403, 403, 403)),   # 只限知识专家、知识管理员
    (("GET", "/api/kb/practice-queue", None),           (403, 403, 403, 403, 403)),   # 公司做法确认队列（三期 B17），同上
    (("GET", "/api/kb/practice-queue/stats", None),     (403, 403, 403, 403, 403)),
    (("POST", "/api/kb/practice-queue/decide", {"items": [{"version_id": 999999, "decision": "adopt"}]}), (403, 403, 403, 403, 403)),
    (("GET", "/api/kb/samples", None),                  (403, 403, 403, 403, 403)),   # 抽检队列（二期 B10），同上
    (("POST", "/api/kb/versions/999999/sample", {"decision": "pass"}), (404, 404, 403, 403, 403)),
    (("GET", "/api/admin/jobs", None),                  (403, 403, 200, 403, 403)),
    (("POST", "/api/admin/jobs/selfcheck", None),       (403, 403, 202, 403, 403)),
    (("GET", "/api/users/lookup?q=u_", None),           (200, 200, 403, 403, 403)),
    (("POST", "/api/auth/password", {"old_password": "x", "new_password": "y"}),
                                                        (400, 400, 400, 400, 400)),
    (("GET", "/api/agents", None),                      (200, 200, 403, 403, 403)),
    (("GET", "/api/projects/BANK-A/conversations", None), (200, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/conversations", {"agent_code": "consultant-lead"}),
                                                        (201, 404, 403, 403, 403)),
    # 对话转交（二期 C2）：对话只有发起人能用，别人的或不存在的一律 404；正常转交在 test_handoff 里测
    (("POST", "/api/conversations/999999/handoff", {"message_id": 1, "agent_code": "research-interview"}),
                                                        (404, 404, 403, 403, 403)),
    (("POST", "/api/agents/consultant-lead/tools/project.read", {"project_code": "BANK-A"}),
                                                        (200, 404, 403, 403, 403)),
    (("POST", "/api/agents/kb-builder/tools/project.read", {"project_code": "BANK-A"}),
                                                        (403, 403, 403, 403, 403)),
    # 去标识检查（三期 C7，迁移 0048）：只授权 kb-builder，不涉及项目；交付侧 Agent 调用一律 403
    (("POST", "/api/agents/kb-builder/tools/deid.check", {"args": {"text": "告警分级按影响范围分三级。"}}),
                                                        (200, 200, 403, 403, 403)),
    (("POST", "/api/agents/report-writer/tools/deid.check", {"args": {"text": "告警分级"}}),
                                                        (403, 403, 403, 403, 403)),
    # 知识使用反馈（三期 B29）：member 不给引用位置 → 422（不留数据）；非成员 404；看板只给知识专家、知识管理员
    (("GET", "/api/projects/BANK-A/kb-feedback", None), (422, 404, 403, 403, 403)),
    (("POST", "/api/projects/BANK-A/kb-feedback", {"doc_id": "L4-NOPE", "rating": "useful"}),
                                                        (422, 404, 403, 403, 403)),
    (("GET", "/api/projects/GRID-B/kb-feedback?message_id=1", None), (404, 404, 403, 403, 403)),
    (("GET", "/api/kb-usage", None),                    (403, 403, 403, 403, 403)),
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


def test_kb_item_alias_same_access_as_doc_id(clients):
    """B15：按别名访问条目，403 / 404 与按现行编号访问完全一致（别名解析在可见性判断之前，不泄露别名存在）。"""
    import secrets

    from app.kb import refs
    from app.models import KbItem

    from .test_kb_refs import _publish

    shown, hidden = _publish("权限矩阵：别名条目"), _publish("权限矩阵：别名下线条目", retire=True)
    pairs = []
    with SessionLocal() as db:
        for doc_id in (shown, hidden):
            alias = f"L5-ALI-MTX-OLD-{secrets.randbelow(90000) + 10000}"
            refs.add_alias(db, alias, db.scalar(select(KbItem.id).where(KbItem.doc_id == doc_id)), "legacy")
            pairs.append((doc_id, alias))
        db.commit()
    expert = logged_in_client(app, create_user(("knowledge_expert",)))
    try:
        cs = {**clients, "expert": expert}
        for doc_id, alias in pairs:
            for suffix in ("", "/usage"):
                by_id = {n: c.get(f"/api/kb/items/{doc_id}{suffix}").status_code for n, c in cs.items()}
                by_alias = {n: c.get(f"/api/kb/items/{alias}{suffix}").status_code for n, c in cs.items()}
                assert by_alias == by_id, f"{suffix}：{by_id} vs {by_alias}"
        assert expert.get(f"/api/kb/items/{pairs[0][1]}").json()["doc_id"] == shown
        assert clients["member"].get(f"/api/kb/items/{pairs[1][1]}").status_code == 404   # 下线条目对顾问不可见
    finally:
        expert.__exit__(None, None, None)


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
    for argv in (["--help"], *([c, "--help"] for c in ("add-member", "create-project", "retention-report", "deletion-scan", "deletion-report", "kb-eval", "grant-role", "usage-report"))):
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


def test_create_project_via_cli():
    """一期没有网页立项：命令行建项目和 D1～D8，必须定档、指定首位项目经理；只有成员能看到。"""
    import secrets
    code = f"QA-{secrets.token_hex(2).upper()}"
    mgr = create_user((CONSULTANT,))
    other = create_user((CONSULTANT,))
    assert cli_main(["create-project", "--code", code, "--tier", "standard", "--manager", mgr[0],
                     "--industry", "银行"]) == 0
    assert cli_main(["create-project", "--code", code, "--tier", "standard", "--manager", mgr[0]]) == 1   # 重复
    assert cli_main(["create-project", "--code", "某银行", "--tier", "standard", "--manager", mgr[0]]) == 1
    admin, *_ = create_user(("sys_admin",))
    assert cli_main(["create-project", "--code", code + "X", "--tier", "strict", "--manager", admin]) == 1
    c, o = logged_in_client(app, mgr), logged_in_client(app, other)
    try:
        r = c.get(f"/api/projects/{code}")
        assert r.status_code == 200 and r.json()["tier"] == "standard"
        assert [d["code"] for d in r.json()["deliverables"]] == [f"D{i}" for i in range(1, 9)]
        assert o.get(f"/api/projects/{code}").status_code == 404
    finally:
        c.__exit__(None, None, None)
        o.__exit__(None, None, None)
    with SessionLocal() as db:
        row = db.scalars(select(AuditLog).where(AuditLog.action == "project.create")
                         .order_by(AuditLog.id.desc())).first()
    assert row.actor == "cli" and row.project_code == code


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


def test_forbidden_message_names_required_roles() -> None:
    import pytest as _pytest
    from fastapi import HTTPException

    from app.core.permissions import CONSULTANT, KNOWLEDGE_EXPERT, require_any

    dep = require_any(CONSULTANT, KNOWLEDGE_EXPERT)
    with _pytest.raises(HTTPException) as e:
        dep(roles=set())
    assert e.value.status_code == 403
    assert "顾问" in e.value.detail and "知识专家" in e.value.detail
