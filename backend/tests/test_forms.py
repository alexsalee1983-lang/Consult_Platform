"""四期 A14：D1、D2 表单与自动成稿——表单定义可读、填表程序成稿（大纲 + 第 1 版）、空字段段落去掉、
资质边界固定段、必填与未知字段 422、已有正文 409、机密级整份拦截不留半份、严格档占位符、审计不记原文。"""

import pytest
from sqlalchemy import select

from app.main import app
from app.models import AuditLog, Deliverable, Project
from app.core.db import SessionLocal
from app.project import forms

from .conftest import create_user, logged_in_client
from .test_deliverables import _base, _new_project

D1_VALUES = {
    "goal": "建立 SOC 运营体系，覆盖监测、响应和值班。",
    "period": "2027-01-05   至 2027-03-31",
    "in_scope": "- 现状评估\n运营体系设计\n\n",
    "out_of_scope": "等保测评\n系统集成实施",
    "milestones": "启动｜1 月｜启动包\n调研｜2 月｜调研包",
    "frameworks": "SOC 能力框架\n等保 2.0",
    "survey_methods": "访谈\n文档审阅",
    "deliverables": "现状评估报告｜2 月底｜客户安全部负责人｜书面确认",
    "roles": "项目经理：统筹",
    "escalation": "项目经理\n双方项目负责人",
    "client_materials": "安全运营制度\n值班记录模板",
    "risks": "访谈排期紧｜提前锁定时间",
    "change_control": "书面提出，双方项目负责人确认后执行。",
    "agenda": "介绍范围\n确认计划",
}
D1_TITLES = ["项目概况", "范围确认", "工作方法与方法论", "工作分解与里程碑", "交付件清单与验收", "组织与沟通",
             "客户配合事项", "风险与变更管理", "客户数据保护安排", "启动会议程与下一步"]


@pytest.fixture()
def proj():
    code = _new_project()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    yield code, c
    c.__exit__(None, None, None)


def test_repo_forms_are_valid():
    for code in ("D1", "D2"):
        f = forms.load_form(code)
        assert f is not None and f.code == code and f.sections
    assert forms.load_form("D4") is None and forms.load_form("../x") is None
    assert "不出具" in forms.load_form("D1").sections[1].fixed          # 资质边界固定写入


def test_get_form_and_submit_d1(proj):
    code, c = proj
    r = c.get(f"{_base(code, 'D1')}/form")
    assert r.status_code == 200 and r.json()["writable"] is True
    keys = [f["key"] for s in r.json()["sections"] for f in s["fields"]]
    assert "out_of_scope" in keys and "escalation" in keys and "next_steps" in keys
    fields = {f["key"]: f for s in r.json()["sections"] for f in s["fields"]}
    assert fields["frameworks"]["default"] == "SOC 能力框架\n等保 2.0\n行业要求"        # 按产品配置预填
    assert fields["deliverables"]["default"].splitlines()[0] == "启动包"
    assert fields["goal"]["default"] == ""
    data = next(s for s in r.json()["sections"] if s["title"] == "客户数据保护安排")
    assert "不收集" in data["fixed"] and "标准档" in data["fixed"] and "严格档" not in data["fixed"]
    assert c.get(f"{_base(code, 'D4')}/form").status_code == 404

    r = c.post(f"{_base(code, 'D1')}/form", json={"values": D1_VALUES})
    assert r.status_code == 201, r.text
    d = r.json()
    assert [s["title"] for s in d["sections"]] == D1_TITLES
    assert all(s["current_version"] == 1 for s in d["sections"])
    body = c.get(f"{_base(code, 'D1')}/sections/2").json()["body"]
    assert "- 现状评估\n- 运营体系设计" in body and "- 等保测评" in body
    assert "前提与依赖" not in body                                    # 没填的段落整段去掉
    assert body.endswith("不出具等级保护测评结论、商用密码应用安全性评估结论或认证结论。")
    s1 = c.get(f"{_base(code, 'D1')}/sections/1").json()
    assert "2027-01-05 至 2027-03-31" in s1["body"] and "{" not in s1["body"]
    assert s1["versions"][0]["note"] == forms.NOTE
    s6 = c.get(f"{_base(code, 'D1')}/sections/6").json()["body"]
    assert "**问题升级路径**\n\n- 项目经理\n- 双方项目负责人" in s6
    s9 = c.get(f"{_base(code, 'D1')}/sections/9").json()["body"]       # 顾问没填另行约定，固定段仍写入
    assert s9.startswith("无论哪一档") and "保留 6 个月" in s9 and "严格档" not in s9
    s10 = c.get(f"{_base(code, 'D1')}/sections/10").json()["body"]
    assert "会后行动" not in s10                                       # 没填的会后行动整段去掉

    assert c.get(f"{_base(code, 'D1')}/form").json()["writable"] is False
    r = c.post(f"{_base(code, 'D1')}/form", json={"values": D1_VALUES})
    assert r.status_code == 409 and "编辑器" in r.json()["detail"]   # 已有正文不覆盖
    with SessionLocal() as db:
        logs = list(db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                      AuditLog.action == "deliverable.write")))
    assert len(logs) == 10 and all("SOC 运营体系" not in str(x.detail) for x in logs)


def test_d2_agent_section_only_in_outline(proj):
    code, c = proj
    r = c.post(f"{_base(code, 'D2')}/form", json={"values": {
        "departments": "安全部", "schedule": "两周", "documents": "组织架构图", "interviewees": "CISO｜第 1 天"}})
    assert r.status_code == 201, r.text
    secs = r.json()["sections"]
    assert secs[-1]["title"] == "访谈提纲" and secs[-1]["current_version"] == 0   # 留给调研访谈 Agent
    assert all(s["current_version"] == 1 for s in secs[:-1])


def test_validation(proj):
    code, c = proj
    r = c.post(f"{_base(code, 'D1')}/form", json={"values": {"goal": "目标"}})
    assert r.status_code == 422 and "不包含项" in r.json()["detail"]
    r = c.post(f"{_base(code, 'D1')}/form", json={"values": {**D1_VALUES, "evil": "x"}})
    assert r.status_code == 422 and "evil" in r.json()["detail"]
    r = c.post(f"{_base(code, 'D1')}/form", json={"values": {**D1_VALUES, "period": "x" * 201}})
    assert r.status_code == 422
    r = c.post(f"{_base(code, 'D1')}/form", json={"values": {**D1_VALUES, "goal": "{in_scope}"}})
    assert r.status_code == 201                                       # 用户填的花括号原样写入，不再展开
    assert c.get(f"{_base(code, 'D1')}/sections/1").json()["body"].startswith("**项目目标**\n\n{in_scope}")


def test_guard_blocks_whole_form(proj):
    code, c = proj
    r = c.post(f"{_base(code, 'D1')}/form", json={"values": {**D1_VALUES, "agenda": "核心交换机管理口令 Abc@12345"}})
    assert r.status_code == 422
    d = c.get(_base(code, "D1")).json()
    assert d["sections"] == [] and d["revision"] == 0                 # 整份拦下，不留半份


def test_strict_needs_placeholders():
    strict = _new_project("strict")
    user = create_user(("consultant",), {strict: "manager"})
    with logged_in_client(app, user) as c:
        assert c.post(f"/api/projects/{strict}/consents",
                      json={"doc_ref": "计划-01", "signed_on": "2026-01-05"}).status_code == 201
        r = c.post(f"{_base(strict, 'D1')}/form", json={"values": {**D1_VALUES, "goal": "江北农商银行 SOC 建设"}})
        assert r.status_code == 422 and r.json()["detail"]["kinds"] == ["机构名称"]
        assert c.post(f"{_base(strict, 'D1')}/form", json={"values": D1_VALUES}).status_code == 201
        s9 = c.get(f"{_base(strict, 'D1')}/sections/9").json()["body"]   # 严格档固定段按档位写入
        assert "严格档" in s9 and "摘要卡" in s9 and "标准档" not in s9


def test_signed_refused(proj):
    code, c = proj
    with SessionLocal() as db:
        db.scalar(select(Deliverable).join(Project).where(Project.code == code, Deliverable.code == "D1")).status = "signed"
        db.commit()
    assert c.post(f"{_base(code, 'D1')}/form", json={"values": D1_VALUES}).status_code == 409


def test_d1_ten_chapters_fixed_paragraphs():
    f = forms.load_form("D1")
    assert [s.title for s in f.sections] == D1_TITLES
    data = f.sections[8]
    assert set(data.fixed_by_tier) == {"standard", "strict"}
    for word in ("口令", "密钥", "漏洞细节", "拓扑", "IP 地址规划", "个人信息样本"):
        assert word in data.fixed                                     # 机密级五类与合同条款一致
    assert "摘要卡" in data.fixed_by_tier["strict"] and "6 个月" in data.fixed_by_tier["standard"]
    for s in f.sections:                                              # 资质边界：不出现结论性用语
        for text in (s.fixed, *s.fixed_by_tier.values()):
            assert "测评结论" not in text.replace("不出具等级保护测评结论", "")


def test_bad_definition_rejected():
    with pytest.raises(ValueError):
        forms._parse({"code": "D1", "sections": [{"title": "甲", "fields": [{"key": "a", "prefill": "x"}],
                                                  "template": "{a}"}]}, "D1")
    with pytest.raises(ValueError):
        forms._parse({"code": "D1", "sections": [{"title": "甲", "fixed_by_tier": {"gold": "x"}}]}, "D1")
    with pytest.raises(ValueError):
        forms._parse({"code": "D1", "sections": [{"title": "甲", "fields": [{"key": "a"}], "template": "{b}"}]}, "D1")
    with pytest.raises(ValueError):
        forms._parse({"code": "D1", "sections": [{"title": "甲", "fields": [{"key": "a", "type": "x"}],
                                                  "template": "{a}"}]}, "D1")
    with pytest.raises(ValueError):
        forms._parse({"code": "D2", "sections": []}, "D1")
