"""交付件分章节与版本（M3）：大纲、章节版本只追加、版本对比、并发冲突、来源标注核对、机密级拦截、
严格档和已签署拒绝、成员隔离、审计不记正文、工具 doc.generate。"""
import secrets

import pytest
from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.main import app
from app.models import AuditLog, Deliverable, Project, SectionVersion, User
from app.seed import DELIVERABLES

from .conftest import create_user, logged_in_client


def _new_project(tier: str = "standard") -> str:
    code = f"DLV-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier=tier)
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    return code


@pytest.fixture()
def proj():
    code = _new_project()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    yield code, c, user[0]
    c.__exit__(None, None, None)


def _base(code: str, d: str = "D4") -> str:
    return f"/api/projects/{code}/deliverables/{d}"


def _audits(action: str) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


def test_outline_then_versions_are_append_only(proj):
    code, c, name = proj
    r = c.put(f"{_base(code)}/outline", json={"titles": ["现状", "差距分析", "整改建议"]})
    assert r.status_code == 200
    d = r.json()
    assert [s["seq"] for s in d["sections"]] == [1, 2, 3] and d["status"] == "drafting" and d["revision"] == 1
    assert all(s["current_version"] == 0 for s in d["sections"])

    v1 = c.put(f"{_base(code)}/sections/1", json={"body": "SOC 现有 6 人，全部白班。[推断]", "note": "初稿"})
    assert v1.status_code == 201 and v1.json()["version"] == 1 and v1.json()["author"] == name
    v2 = c.put(f"{_base(code)}/sections/1", json={"body": "SOC 现有 6 人，全部白班，夜间无人值守。[推断]",
                                                   "base_version": 1})
    assert v2.status_code == 201 and v2.json()["current_version"] == 2
    assert [m["version"] for m in v2.json()["versions"]] == [2, 1]           # 新的在前，旧版本保留

    old = c.get(f"{_base(code)}/sections/1", params={"version": 1}).json()
    assert old["version"] == 1 and "夜间" not in old["body"] and old["note"] == "初稿"
    diff = c.get(f"{_base(code)}/sections/1/diff", params={"a": 1, "b": 2}).json()["diff"]
    assert any(line.startswith("+") and "夜间无人值守" in line for line in diff)
    assert c.get(f"{_base(code)}/sections/1", params={"version": 9}).status_code == 404
    assert c.get(_base(code)).json()["revision"] == 3

    # 基于旧版本改：冲突；内容没变：不新建版本
    stale = c.put(f"{_base(code)}/sections/1", json={"body": "另一个人的改法", "base_version": 1})
    assert stale.status_code == 409 and "v2" in stale.json()["detail"]
    same = c.put(f"{_base(code)}/sections/1", json={"body": "SOC 现有 6 人，全部白班，夜间无人值守。[推断]"})
    assert same.status_code == 409
    assert c.put(f"{_base(code)}/sections/7", json={"body": "没有这一章"}).status_code == 404


def test_outline_rename_grow_and_shrink_rules(proj):
    code, c, _ = proj
    c.put(f"{_base(code)}/outline", json={"titles": ["一", "二", "三"]})
    c.put(f"{_base(code)}/sections/2", json={"body": "第二章正文"})
    r = c.put(f"{_base(code)}/outline", json={"titles": ["一（改）", "二", "三", "四"]})
    assert [s["title"] for s in r.json()["sections"]] == ["一（改）", "二", "三", "四"]
    assert r.json()["sections"][1]["current_version"] == 1                  # 改大纲不影响已有正文
    assert c.put(f"{_base(code)}/outline", json={"titles": ["一"]}).status_code == 409   # 第 2 章有正文，不能删
    r = c.put(f"{_base(code)}/outline", json={"titles": ["一", "二"]})        # 删没有正文的 3、4 章可以
    assert r.status_code == 200 and len(r.json()["sections"]) == 2
    assert c.put(f"{_base(code)}/outline", json={"titles": [" "]}).status_code == 422
    assert c.get(_base(code, "D9")).status_code == 404


def _publish_kb(title: str) -> str:
    expert, expert2, admin = (create_user(r)[0] for r in (("knowledge_expert",), ("knowledge_expert",),
                                                           ("knowledge_admin",)))
    with SessionLocal() as db:
        u = {n: db.scalar(select(User).where(User.username == n)) for n in (expert, expert2, admin)}
        v = rv.new_item(db, u[expert], ItemMeta(layer="L4", domain="OPS", product="GEN", doc_type="REF", stage="通用",
                                                capabilities=["运营"], visible_roles=["consultant"]),
                        VersionContent(title=title, body="## 8.1.5 集中管控\n\n应集中分析。", source_type="public",
                                       source_detail="公开资料", ref_document="GB/T 22239", ref_version="2019",
                                       ref_status="current"))
        rv.submit(db, u[expert], v)
        rv.review(db, u[expert2], v, "content", "approve")
        rv.review(db, u[admin], v, "compliance", "approve")
        db.refresh(v)
        return v.item.doc_id


def test_sources_are_parsed_and_checked(proj):
    code, c, _ = proj
    note = c.post(f"/api/projects/{code}/notes", json={"kind": "interview", "title": "值班访谈",
                                                       "interviewee_role": "SOC 负责人", "content": "全部白班。"})
    assert note.status_code == 201 and note.json()["code"] == "I-01"
    doc_id = _publish_kb("交付件来源测试依据")
    c.put(f"{_base(code)}/outline", json={"titles": ["现状"]})
    body = (f"SOC 全部白班 [I-01-03]。夜间升级做法 [{doc_id}]，依据【依据：GB/T 22239 2019 8.1.5｜{doc_id}】。"
            "值班记录缺失 [D-07]，引用不存在的条目 [L4-OPS-GEN-REF-99999]。二期扩编 [推断]。")
    s = c.put(f"{_base(code)}/sections/1", json={"body": body}).json()["sources"]
    assert s["counts"] == {"evidence": 2, "knowledge": 2, "inferred": 1}
    assert {e["ref"]: e["found"] for e in s["evidence"]} == {"I-01-03": True, "D-07": False}
    assert {k["ref"]: k["found"] for k in s["knowledge"]} == {doc_id: True, "L4-OPS-GEN-REF-99999": False}
    assert next(k for k in s["knowledge"] if k["ref"] == doc_id)["layer_name"] == "依据"
    assert sorted(s["unresolved"]) == ["D-07", "L4-OPS-GEN-REF-99999"]
    d = c.get(_base(code)).json()
    assert d["totals"] == {"evidence": 2, "knowledge": 2, "inferred": 1, "unresolved": 2}
    assert d["sections"][0]["unresolved"] == s["unresolved"]


def test_guard_blocks_and_audit_has_no_text(proj):
    code, c, _ = proj
    c.put(f"{_base(code)}/outline", json={"titles": ["现状"]})
    r = c.put(f"{_base(code)}/sections/1", json={"body": "核心交换机管理口令 Abc@12345，独特正文甲乙丙"})
    assert r.status_code == 422 and r.json()["detail"]["blocked"]
    with SessionLocal() as db:
        assert db.scalar(select(SectionVersion.id).join(SectionVersion.section)
                         .where(SectionVersion.body.contains("独特正文甲乙丙"))) is None
    blocked = _audits("deliverable.write")[-1]
    assert blocked.result == "blocked" and "Abc@12345" not in blocked.detail and "独特正文" not in blocked.detail
    c.put(f"{_base(code)}/sections/1", json={"body": "正常正文丁戊己 [推断]"})
    ok = _audits("deliverable.write")[-1]
    assert ok.result == "allowed" and "丁戊己" not in ok.detail and '"version": 1' in ok.detail
    assert c.put(f"{_base(code)}/outline", json={"titles": ["口令 Abc@12345"]}).status_code == 422


def test_strict_placeholder_and_signed_refused():
    strict = _new_project("strict")
    user = create_user(("consultant",), {strict: "manager"})
    with logged_in_client(app, user) as c:
        r = c.put(f"{_base(strict)}/outline", json={"titles": ["现状"]})
        assert r.status_code == 422 and "同意" in r.json()["detail"]                 # 严格档没有客户同意不收
        assert c.post(f"/api/projects/{strict}/consents",
                      json={"doc_ref": "计划-01", "signed_on": "2026-01-05"}).status_code == 201
        r = c.put(f"{_base(strict)}/outline", json={"titles": ["江北农商银行现状"]})
        assert r.status_code == 422 and r.json()["detail"]["kinds"] == ["机构名称"]   # 严格档只收占位符版本
        assert c.put(f"{_base(strict)}/outline", json={"titles": ["现状"]}).status_code == 200
    std = _new_project()
    user = create_user(("consultant",), {std: "member"})
    with logged_in_client(app, user) as c:
        c.put(f"{_base(std)}/outline", json={"titles": ["现状"]})
        with SessionLocal() as db:
            db.scalar(select(Deliverable).join(Project).where(Project.code == std, Deliverable.code == "D4")).status = "signed"
            db.commit()
        assert c.put(f"{_base(std)}/sections/1", json={"body": "签署后再改"}).status_code == 409
        assert c.put(f"{_base(std)}/outline", json={"titles": ["改"]}).status_code == 409


def test_non_member_gets_404(proj):
    code, _, _ = proj
    other = create_user(("consultant",), {"MFG-C": "member"})
    with logged_in_client(app, other) as c:
        assert c.get(_base(code)).status_code == 404
        assert c.put(f"{_base(code)}/outline", json={"titles": ["x"]}).status_code == 404


def test_doc_generate_tool(proj):
    code, _, name = proj
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        out = tools.invoke(db, user, "report-writer", "doc.generate",
                           {"action": "outline", "deliverable": "d4", "sections": ["现状", "差距"]}, code)
        assert [s["seq"] for s in out["sections"]] == [1, 2]
        w = tools.invoke(db, user, "report-writer", "doc.generate",
                         {"action": "write", "deliverable": "D4", "section": 2, "body": "差距一 [推断]" * 3,
                          "note": "Agent 初稿"}, code)
        assert w["version"] == 1 and w["sources"]["inferred"] == 3
        r = tools.invoke(db, user, "report-writer", "doc.generate",
                         {"action": "read", "deliverable": "D4", "section": 2, "limit": 4}, code)
        assert r["body"] == "差距一 " and r["has_more"] and r["total_chars"] == len("差距一 [推断]" * 3)
        assert tools.invoke(db, user, "report-writer", "doc.generate", {"deliverable": "D4"}, code)["sections"][1][
            "current_version"] == 1
        v = db.scalar(select(SectionVersion).where(SectionVersion.note == "Agent 初稿").order_by(SectionVersion.id.desc()))
        assert v.agent_code == "report-writer" and v.author == name
        with pytest.raises(tools.ToolError) as e:                                # 机密级：工具报错，不保存
            tools.invoke(db, user, "report-writer", "doc.generate",
                         {"action": "write", "deliverable": "D4", "section": 1, "body": "口令 Abc@12345"}, code)
        assert e.value.status == 422 and e.value.detail["blocked"] and e.value.detail["findings"]
        with pytest.raises(tools.ToolError) as e:
            tools.invoke(db, user, "report-writer", "doc.generate", {"action": "delete", "deliverable": "D4"}, code)
        assert e.value.status == 422
        with pytest.raises(tools.ToolError) as e:                                # 业务状态码原样透出，不统一成 400
            tools.invoke(db, user, "report-writer", "doc.generate", {"deliverable": "D9"}, code)
        assert e.value.status == 404
        with pytest.raises(tools.ToolError) as e:
            tools.invoke(db, user, "report-writer", "doc.generate",
                         {"action": "write", "deliverable": "D4", "section": 2, "body": "新的差距", "base_version": 0},
                         code)
        assert e.value.status == 409
        for bad in ([{"title": "现状梳理"}], ["现状", 2], [["差距"]]):            # 标题只能是字符串，不能 str() 成 Python 写法
            with pytest.raises(tools.ToolError) as e:
                tools.invoke(db, user, "report-writer", "doc.generate",
                             {"action": "outline", "deliverable": "D4", "sections": bad}, code)
            assert e.value.status == 422
        assert [s["title"] for s in tools.invoke(db, user, "report-writer", "doc.generate",
                                                  {"deliverable": "D4"}, code)["sections"]] == ["现状", "差距"]
        with pytest.raises(tools.ToolDenied):                                    # 咨询师 Agent 没有“文档生成”授权
            tools.invoke(db, user, "consultant-lead", "doc.generate", {"deliverable": "D4"}, code)
