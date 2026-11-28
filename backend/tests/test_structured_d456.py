"""四期 A36：D4 / D5 结构化与固定段、D6 公文格式——
D4 发现表、成熟度初判、热力图由程序从发现表（真源）写入，模型只交总体结论与分域分析；
D5 测算算式由程序校验并出表；D6 按公文骨架（第一章 / 第一条）编号，出配套表单、新旧对照与差距映射；
结论用语守资质边界。"""
import secrets

import pytest
from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.models import Deliverable, Evidence, Project, User
from app.project import assessment, findings, structured
from app.seed import DELIVERABLES

from .conftest import create_user


def _finding(fid: str, domain: str, **kw) -> dict:
    return {"fid": fid, "domain": domain, "item": f"{domain}要求", "current": "现状说明", "conclusion": "部分符合",
            "evidence": ["I-01"], "impact": "高", "likelihood": "中", "current_score": 2.0, "target_score": 3.0,
            "status": "confirmed", **kw}


def _d4(**kw) -> dict:
    return {"summary": "整体处于起步阶段，监测覆盖与响应流程是主要差距。",
            "domains": [{"domain": "监测", "analysis": "夜间无人值守。", "impact": "告警积压。", "findings": ["F-01"]},
                        {"domain": "响应", "analysis": "无分级。", "impact": "处置慢。", "findings": ["F-02"]}],
            "findings": [_finding("F-01", "监测"), _finding("F-02", "响应", conclusion="不符合", impact="中")], **kw}


def test_d4_fixed_sections_from_findings():
    r = structured.check("D4", _d4())
    assert not r.errors and not r.warnings
    secs = dict(structured.render("D4", r.model))
    assert list(secs) == structured.TITLES["D4"]
    assert "| F-01 | 监测 |" in secs["发现表"] and "[I-01]" in secs["发现表"]
    scores = assessment.parse(secs["成熟度初判"])["scores"]          # 签署后驾驶舱从 D4 取分数，表头须对得上
    assert {s["domain"]: (s["current"], s["target"]) for s in scores} == {"监测": (2.0, 3.0), "响应": (2.0, 3.0)}
    assert "| 高 | 0 | 1 | 0 |" in secs["差距热力图"]                 # 影响 × 可能性计数
    assert "夜间无人值守" in secs["分域分析"] and "[F-01]" in secs["分域分析"]


def test_d4_rules_unknown_finding_uncovered_draft_and_qualification():
    data = _d4(domains=[{"domain": "监测", "analysis": "已通过等保测评。", "impact": "", "findings": ["F-09"]}])
    data["findings"][1]["status"] = "draft"
    r = structured.check("D4", data)
    joined = "\n".join(r.errors)
    assert "F-09" in joined and "测评" in joined                    # 引用不存在的发现、资质边界用语：硬错误
    w = "\n".join(r.warnings)
    assert "F-02" in w                                               # 未被分析覆盖 / 未经顾问确认：提示


def _d5(**kw) -> dict:
    return {"model": [{"title": "运营模式", "text": "7×24 分级值守，夜间初筛加白班分析 [O-01]。"}],
            "staffing": [{"role": "一线分析", "seats": 2, "shifts": 3, "factor": 1.5, "headcount": 9,
                          "basis": "三班倒，含休假轮换"}],
            "processes": [{"id": "P-01", "name": "告警研判", "owner": "SOC 一线", "trigger": "告警", "output": "工单"}],
            "alert_levels": [{"level": "P1", "definition": "核心业务中断", "response_minutes": 15, "escalation": "主管"},
                             {"level": "P2", "definition": "单点失陷", "response_minutes": 60, "escalation": "组长"}],
            "metrics": [{"id": "K-01", "name": "平均响应时长", "formula": "响应时长之和 / 告警数",
                         "target": "≤30 分钟", "frequency": "月"}], **kw}


def test_d5_formula_and_levels_checked():
    r = structured.check("D5", _d5())
    assert not r.errors
    secs = dict(structured.render("D5", r.model))
    assert list(secs) == structured.TITLES["D5"]
    assert "| 一线分析 | 2 | 3 | 1.5 | 9 |" in secs["组织与人员测算"] and "合计 9 人" in secs["组织与人员测算"]
    assert "| P1 |" in secs["告警分级"] and "| K-01 |" in secs["指标体系"]
    bad = _d5()
    bad["staffing"][0]["headcount"] = 6
    bad["alert_levels"][1]["response_minutes"] = 5
    joined = "\n".join(structured.check("D5", bad).errors)
    assert "应为 9" in joined and "P2" in joined                    # 算式不对、分级响应时限倒挂


def _d6() -> dict:
    return {"policies": [
        {"id": "R-01", "title": "安全事件管理办法", "level": "二级", "action": "新建",
         "chapters": [{"title": "总则", "articles": ["为规范安全事件处置，制定本办法。", "本办法适用于全行。"]},
                      {"title": "职责", "articles": ["安全运营部负责事件研判。"]}],
         "gaps": ["F-02"], "forms": ["T-01"]},
        {"id": "R-02", "title": "安全监测规程", "level": "三级", "action": "修订", "old_title": "安全监控细则",
         "chapters": [{"title": "总则", "articles": ["为规范监测，制定本规程。"]}], "gaps": ["F-01"]},
        {"id": "R-03", "title": "信息安全总体方针", "level": "一级", "action": "保留", "old_title": "信息安全总体方针"}],
        "forms": [{"id": "T-01", "name": "安全事件报告单", "policy": "R-01", "fields": ["事件编号", "发现时间", "级别"]}]}


def test_d6_official_numbering_forms_and_mapping():
    r = structured.check("D6", _d6())
    assert not r.errors
    secs = dict(structured.render("D6", r.model))
    assert list(secs) == structured.TITLES["D6"]
    body = secs["制度文件"]
    assert "第一章 总则" in body and "第二章 职责" in body
    assert "第三条 安全运营部负责事件研判。" in body                  # 条号跨章连续
    assert "信息安全总体方针" not in body                             # 保留的不重写正文
    assert "| T-01 | 安全事件报告单 | R-01 | 事件编号、发现时间、级别 |" in secs["配套表单"]
    assert "| 安全监控细则 | 安全监测规程 | 修订 |" in secs["新旧制度对照"]
    assert "| F-02 | R-01 安全事件管理办法 |" in secs["制度与差距映射"]
    bad = _d6()
    bad["policies"][1]["old_title"] = ""
    bad["policies"][0]["chapters"] = []
    bad["forms"][0]["policy"] = "R-09"
    joined = "\n".join(structured.check("D6", bad).errors)
    assert "R-02" in joined and "R-01" in joined and "R-09" in joined
    assert structured.chinese_numeral(10) == "十" and structured.chinese_numeral(23) == "二十三"


def test_d4_tool_fills_findings_from_true_source():
    code = f"STR-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.flush()
        db.add(Evidence(project_id=p.id, code="I-01", kind="interview", seq=1, title="SOC 主管访谈", created_by="t"))
        pid = p.id
        db.commit()
    name = create_user(("consultant",), {code: "member"})[0]
    gap = ("| 编号 | 能力域 | 要求要点 | 现状 | 证据编号 | 符合性初判 | 影响 | 可能性 |\n|---|---|---|---|---|---|---|---|\n"
           "| C-1 | 监测 | 7×24 监测 | 夜间无人 | [I-01] | 不符合 | 高 | 高 |\n")
    with SessionLocal() as db:
        p = db.get(Project, pid)
        findings.sync_from_gap(db, p, gap)
        db.commit()
        user = db.scalar(select(User).where(User.username == name))
        data = {"summary": "起步阶段。", "findings": [_finding("F-77", "伪造")],   # 模型给的发现表被真源覆盖
                "domains": [{"domain": "监测", "analysis": "夜间无人值守。", "impact": "积压。", "findings": ["F-01"]}]}
        out = tools.invoke(db, user, "report-writer", "doc.generate",
                           {"action": "structured", "deliverable": "D4", "data": data}, code)
        assert out["written"] == [1, 2, 3, 4, 5]
        assert any("确认" in w for w in out["warnings"])                # 发现还是草稿：提示
        saved = structured.load(db, pid, "D4")
        assert [f["fid"] for f in saved["findings"]] == ["F-01"]
        other = Project(code=f"{code}X", service="SOC 运营体系设计", industry="银行", tier="standard")
        db.add(other)
        db.flush()
        with pytest.raises(structured.StructuredError):                  # 发现表为空时不能结构化写 D4
            structured.check("D4", structured.with_findings(db, other, {"summary": "x", "domains": []}))
