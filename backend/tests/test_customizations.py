"""四期 A30 客户定制项：三个入口（D1 客户要求栏、对话转定制项确认卡、编辑器章节级一次性）汇成项目定制项；
优先级裁决（客户书面要求 > 项目定制项）；形式类、口径类顾问确认即生效，结构类须项目经理确认；
碰到必备章节或越过红线、资质边界标“不可接受”；质量门禁逐条核对（必备内容在不在）；
定制项变更按上游变更处理（受影响的章标“需更新”、评审后变更须重新评审）；权限与 404。虚拟客户，模型打桩。"""
import json

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Confirmation, Project
from app.project import customizations as cust

from .conftest import create_user, logged_in_client
from .test_a26_deliv import _new_project, _review, _url, model  # noqa: F401  （model 为打桩夹具）
from .test_forms import D1_VALUES


@pytest.fixture()
def team():
    """顾问（成员）、项目经理、非成员顾问。"""
    code = _new_project()
    users = [create_user(("consultant",), {code: "member"}), create_user(("consultant",), {code: "manager"}),
             create_user(("consultant",), {"MFG-C": "member"})]
    clients = [logged_in_client(app, u) for u in users]
    yield code, *clients
    for c in clients:
        c.__exit__(None, None, None)


def _base(code: str) -> str:
    return f"/api/projects/{code}/customizations"


def _items(c, code: str) -> list[dict]:
    r = c.get(_base(code))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _chat(c, code: str, text: str) -> list[dict]:
    r = c.post(_base(code), json={"source": "chat", "text": text})
    assert r.status_code == 201, r.text
    return r.json()["items"]


def _resolve(c, card_id: int, choice: str = "A"):
    return c.post(f"/api/confirmations/{card_id}/resolve", json={"choice": choice})


# ---- 三个入口汇聚 ----

def test_three_entries_converge(team):
    code, member, mgr, _ = team
    values = {**D1_VALUES, "client_requirements": "D4 统一称“安全运营中心”，不用“SOC”\n报告正文用小四号宋体"}
    assert member.post(f"/api/projects/{code}/deliverables/D1/form", json={"values": values}).status_code == 201
    chat = _chat(member, code, "D5 的整改建议按季度排期")
    assert chat[0]["status"] == "proposed" and chat[0]["card_id"]
    with SessionLocal() as db:
        card = db.get(Confirmation, chat[0]["card_id"])
        assert card.kind == cust.CARD_KIND and card.status == "open"
    assert _resolve(member, chat[0]["card_id"]).status_code == 200
    assert member.put(_url(code, "D4", "outline"), json={"titles": ["现状概述", "主要差距"]}).status_code == 200
    r = member.post(_base(code), json={"source": "editor", "text": "本章先写结论再写过程",
                                       "deliverable": "D4", "section": 2})
    assert r.status_code == 201, r.text
    items = _items(member, code)
    assert {i["source"] for i in items} == {"d1", "chat", "editor"}
    d1 = [i for i in items if i["source"] == "d1"]
    assert len(d1) == 2 and all(i["status"] == "active" and i["layer"] == "客户书面要求" for i in d1)
    assert {i["kind"] for i in d1} == {"wording", "form"}
    assert next(i for i in d1 if i["kind"] == "wording")["deliverable"] == "D4"
    ch = next(i for i in items if i["source"] == "chat")
    assert ch["status"] == "active" and ch["layer"] == "项目定制项" and ch["deliverable"] == "D5"
    ed = next(i for i in items if i["source"] == "editor")
    assert ed["status"] == "active" and ed["one_time"] and ed["section"] == 2
    # 审计不记原文
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action.like("customization.%"))).all()
        assert rows
        for a in rows:
            detail = a.detail if isinstance(a.detail, str) else json.dumps(a.detail, ensure_ascii=False)
            assert "安全运营中心" not in detail and "季度" not in detail and "先写结论" not in detail


def test_editor_entry_one_time_consumed_after_rewrite(team):
    code, member, _, _ = team
    member.put(_url(code, "D4", "outline"), json={"titles": ["现状概述", "主要差距"]})
    member.put(_url(code, "D4", "sections/1"), json={"body": "SOC 现有 6 人，全部白班 [推断]。"})
    item = member.post(_base(code), json={"source": "editor", "text": "本章补一段值班现状",
                                          "deliverable": "D4", "section": 1}).json()["items"][0]
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        assert "本章补一段值班现状" in cust.prompt_block(db, p, "D4", 1)
        assert "本章补一段值班现状" not in cust.prompt_block(db, p, "D4", 2)   # 只对这一章
    assert cust_status(member, code, item["id"]) == "active"
    member.put(_url(code, "D4", "sections/1"), json={"body": "SOC 现有 6 人，全部白班，夜间无人值守 [推断]。"})
    assert cust_status(member, code, item["id"]) == "used"
    # 编辑器入口只收形式类、口径类，结构类走对话
    r = member.post(_base(code), json={"source": "editor", "text": "删除《主要差距》一章", "deliverable": "D4",
                                       "section": 1})
    assert r.status_code == 422


def cust_status(c, code: str, cid: int) -> str:
    return next(i for i in _items(c, code) if i["id"] == cid)["status"]


# ---- 结构类须项目经理确认 ----

def test_structure_needs_manager(team):
    code, member, mgr, _ = team
    item = _chat(member, code, "D4 增加《外包管理》一章")[0]
    assert item["kind"] == "structure" and item["action"] == "add" and item["section_title"] == "外包管理"
    assert _resolve(member, item["card_id"]).status_code == 200
    item = next(i for i in _items(member, code) if i["id"] == item["id"])
    assert item["status"] == "pending_pm"
    pm_card = item["card_id"]
    with SessionLocal() as db:
        assert db.get(Confirmation, pm_card).kind == cust.PM_KIND
    assert _resolve(member, pm_card).status_code == 403                # 顾问不能代项目经理确认
    assert _resolve(mgr, pm_card).status_code == 200
    assert cust_status(member, code, item["id"]) == "active"


def test_form_and_wording_effective_on_consultant_confirm(team):
    code, member, _, _ = team
    items = _chat(member, code, "D4 用语统一写“安全运营中心”\nD4 表格加编号列")
    assert [i["kind"] for i in items] == ["wording", "form"]
    for i in items:
        assert _resolve(member, i["card_id"]).status_code == 200
    assert {cust_status(member, code, i["id"]) for i in items} == {"active"}
    rejected = _chat(member, code, "D4 附录放大字号")[0]
    assert _resolve(member, rejected["card_id"], "B").status_code == 200
    assert cust_status(member, code, rejected["id"]) == "rejected"


# ---- 不可接受 ----

def test_unacceptable_qualification_and_required_section(team):
    code, member, _, _ = team
    q = _chat(member, code, "D8 写已通过等保三级")[0]
    assert q["status"] == "unacceptable" and "资质" in q["reason"] and not q["card_id"]
    req = _chat(member, code, "D5 删除《目标运营模型》一章")[0]        # SOC 产品 D5 的必备章节
    assert req["status"] == "unacceptable" and "必备章节" in req["reason"]
    src = _chat(member, code, "D4 去掉来源标注")[0]
    assert src["status"] == "unacceptable"
    ok = _chat(member, code, "D5 删除《附录》一章")[0]
    assert ok["status"] == "proposed"


def test_guard_blocks_confidential(team):
    code, member, _, _ = team
    r = member.post(_base(code), json={"source": "chat", "text": "D4 附上管理口令 password=Abc12345!"})
    assert r.status_code == 422
    assert _items(member, code) == []


# ---- 优先级裁决 ----

def test_priority_client_written_over_project(team):
    code, member, mgr, _ = team
    values = {**D1_VALUES, "client_requirements": "D5 增加《外包管理》一章"}
    assert member.post(f"/api/projects/{code}/deliverables/D1/form", json={"values": values}).status_code == 201
    d1 = next(i for i in _items(member, code) if i["source"] == "d1")
    assert d1["status"] == "pending_pm"                                 # D1 里的结构类同样要项目经理确认
    assert _resolve(mgr, d1["card_id"]).status_code == 200
    chat = _chat(member, code, "D5 删除《外包管理》一章")[0]
    _resolve(member, chat["card_id"])
    pm_card = next(i for i in _items(member, code) if i["id"] == chat["id"])["card_id"]
    _resolve(mgr, pm_card)
    items = {i["id"]: i for i in _items(member, code)}
    assert items[chat["id"]]["status"] == "active" and items[chat["id"]]["superseded"]
    assert items[chat["id"]]["overridden_by"] == d1["id"] and not items[d1["id"]]["superseded"]
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        block = cust.prompt_block(db, p, "D5")
        assert "增加章节《外包管理》" in block and "删除章节《外包管理》" not in block
        assert block.index("红线与资质") < block.index("客户书面要求") < block.index("产品默认大纲")


# ---- 门禁逐条核对、上游变更 ----

def test_gate_checks_customization_items(team, model):  # noqa: F811  （打桩夹具）
    code, member, mgr, _ = team
    member.put(_url(code, "D4", "outline"), json={"titles": ["现状概述", "主要差距"]})
    for i in (1, 2):
        member.put(_url(code, "D4", f"sections/{i}"), json={"body": f"SOC 现有 6 人，全部白班（{i}）[推断]。"})
    item = _chat(member, code, "D4 增加《外包管理》一章")[0]
    _resolve(member, item["card_id"])
    _resolve(mgr, next(i for i in _items(member, code) if i["id"] == item["id"])["card_id"])
    _review(member, code, "D4")
    rid = member.get(_url(code, "D4", "reviews")).json()[0]["id"]
    issues = member.get(_url(code, "D4", f"reviews/{rid}")).json()["issues"]
    hits = [x for x in issues if x["dimension"] == cust.GATE_DIMENSION]
    assert hits and hits[0]["severity"] == "阻断" and "外包管理" in hits[0]["problem"]
    assert any("需更新" in x["problem"] for x in hits)                 # 已写的章在定制项生效前写入
    # 补上章节、重写各章后核对通过
    member.put(_url(code, "D4", "outline"), json={"titles": ["现状概述", "主要差距", "外包管理"]})
    for i in (1, 2, 3):
        member.put(_url(code, "D4", f"sections/{i}"), json={"body": f"SOC 值班安排与外包现状（{i}）[推断]。"})
    _review(member, code, "D4")
    rid = member.get(_url(code, "D4", "reviews")).json()[0]["id"]
    issues = member.get(_url(code, "D4", f"reviews/{rid}")).json()["issues"]
    assert not [x for x in issues if x["dimension"] == cust.GATE_DIMENSION]
    gate = member.get(_url(code, "D4", "signoff")).json()
    assert not any("定制项" in r for r in gate["reasons"])
    # 评审之后定制项有变更：按上游变更处理，须重新评审
    w = _chat(member, code, "D4 用语统一写“安全运营中心”")[0]
    _resolve(member, w["card_id"])
    gate = member.get(_url(code, "D4", "signoff")).json()
    assert any("定制项" in r and "重新评审" in r for r in gate["reasons"])
    needs = member.get(_base(code)).json()["needs_update"]
    assert needs.get("D4") == [1, 2, 3]
    assert member.post(f"{_base(code)}/{w['id']}/withdraw").status_code == 200
    assert cust_status(member, code, w["id"]) == "withdrawn"


# ---- 权限与 404 ----

def test_permissions_and_404(team):
    code, member, _, outsider = team
    item = _chat(member, code, "D4 表格加编号列")[0]
    assert outsider.get(_base(code)).status_code == 404
    assert outsider.post(_base(code), json={"source": "chat", "text": "D4 表格加编号列"}).status_code == 404
    assert outsider.post(f"{_base(code)}/{item['id']}/withdraw").status_code == 404
    assert outsider.post(f"/api/confirmations/{item['card_id']}/resolve", json={"choice": "A"}).status_code == 404
    other = _new_project()
    with SessionLocal() as db:
        from app.models import ProjectMember, User
        uid = db.scalar(select(User.id).join(ProjectMember, ProjectMember.user_id == User.id)
                        .join(Project, Project.id == ProjectMember.project_id).where(Project.code == code,
                                                                                    ProjectMember.project_role == "member"))
        pid = db.scalar(select(Project.id).where(Project.code == other))
        db.add(ProjectMember(project_id=pid, user_id=uid, project_role="member", added_by="test"))
        db.commit()
    assert member.post(f"{_base(other)}/{item['id']}/withdraw").status_code == 404   # 别的项目的定制项
    assert member.post(_base(code), json={"source": "nope", "text": "x"}).status_code == 422
