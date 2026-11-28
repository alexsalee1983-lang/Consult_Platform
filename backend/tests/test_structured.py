"""四期 A34：D3 / D7 结构化输出与程序渲染——校验（证据、四要素、阶段与月份、依赖、差距映射）、
渲染成固定章节表格（Word 导出复用 Markdown 表格）、经 doc.generate structured 写入并可取回真源、
敏感表态剔除记录不收原文。"""
import io
import secrets

import docx
import pytest
from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.models import Deliverable, Evidence, Project, User
from app.project import export, structured
from app.seed import DELIVERABLES

from .conftest import create_user


def _fact(i: int, **kw) -> dict:
    return {"id": f"F-{i:02d}", "statement": f"SOC 现有值班安排第 {i} 条", "kind": "事实", "role": "SOC 主管",
            "evidence": ["I-01-02"], "confidence": "高", **kw}


def _measure(i: int, **kw) -> dict:
    return {"id": f"M-{i:02d}", "title": f"措施 {i}", "priority": "高", "owner": "安全运营部", "period": "3 个月",
            "acceptance": "制度发布并试运行一个月", "phase": "短期", "start_month": 1, "months": 3,
            "gaps": ["G-01"], "resources": "2 人月", **kw}


def test_d3_validate_and_render():
    data = {"facts": [_fact(1), _fact(2, kind="判断", evidence=[]), _fact(3, confidence="低"),
                      _fact(4, evidence=[], confirmed=True)],
            "gaps": [{"item": "夜班交接记录", "need": "近三个月交接单", "request_to": "SOC 主管"}],
            "excluded": [{"category": "对上级的评价", "role": "SOC 主管"}]}
    r = structured.check("D3", data)
    assert any("F-04" in e and "证据" in e for e in r.errors)       # 事实没有证据编号：硬错误
    data["facts"][3]["evidence"] = ["D-03"]
    r = structured.check("D3", data)
    assert not r.errors and any("F-03" in w for w in r.warnings)    # 单一低可信来源：提示待顾问确认
    secs = structured.render("D3", r.model)
    assert [t for t, _ in secs] == structured.TITLES["D3"]
    facts_body = dict(secs)["事实清单"]
    assert "| F-01 |" in facts_body and "[I-01-02]" in facts_body and "F-02" not in facts_body  # 判断不进事实表
    assert "F-02" in dict(secs)["顾问判断与待确认"] and "F-03" in dict(secs)["顾问判断与待确认"]
    assert "对上级的评价" in dict(secs)["敏感表态剔除记录"]


def test_d3_excluded_refuses_original_text():
    data = {"facts": [_fact(1)], "excluded": [{"category": "抱怨", "role": "运维", "text": "原话"}]}
    with pytest.raises(structured.StructuredError):
        structured.check("D3", data)


def test_d7_rules_four_elements_phase_depends_gaps():
    bad = {"measures": [_measure(1, owner=""), _measure(2, phase="短期", start_month=10),
                        _measure(3, depends=["M-09"]), _measure(4, gaps=[])],
           "gap_list": [{"id": "G-01", "title": "夜间无人值守"}, {"id": "G-02", "title": "无告警分级"}]}
    with pytest.raises(structured.StructuredError):                  # 四要素缺一：schema 拒绝
        structured.check("D7", bad)
    bad["measures"][0]["owner"] = "安全运营部"
    r = structured.check("D7", bad)
    joined = "\n".join(r.errors)
    assert "M-02" in joined and "M-03" in joined and "M-04" in joined
    good = {"measures": [_measure(1), _measure(2, phase="中期", start_month=7, months=6, depends=["M-01"])],
            "gap_list": [{"id": "G-01", "title": "夜间无人值守"}, {"id": "G-02", "title": "无告警分级"}]}
    r = structured.check("D7", good)
    assert not r.errors and any("G-02" in w for w in r.warnings)    # 差距没有措施覆盖：提示
    secs = dict(structured.render("D7", r.model))
    assert list(secs) == structured.TITLES["D7"]
    assert "| M-01 | 措施 1 | 高 | 安全运营部 | 3 个月 | 制度发布并试运行一个月 |" in secs["措施清单"]
    assert "■" in secs["短中长期路线图"] and "Q3" in secs["短中长期路线图"]
    assert "G-02" in secs["措施与差距映射"] and "未覆盖" in secs["措施与差距映射"]


def test_structured_tool_writes_sections_and_reloads():
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
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        data = {"facts": [_fact(1)], "gaps": [], "excluded": []}
        out = tools.invoke(db, user, "research-interview", "doc.generate",
                           {"action": "structured", "deliverable": "D3", "data": data, "note": "A34"}, code)
        assert out["written"] == [1, 2, 3, 4] and out["errors"] == []
        assert out["unresolved"] == []                                   # I-01-02 对得上 I-01
        again = tools.invoke(db, user, "research-interview", "doc.generate",
                             {"action": "structured", "deliverable": "D3", "data": data}, code)
        assert again["written"] == []                                    # 没变化：不新建版本
        assert structured.load(db, pid, "D3")["facts"][0]["id"] == "F-01"
        p = db.get(Project, pid)                                         # Word 先行：表格进 docx
        d3 = db.scalar(select(Deliverable).where(Deliverable.project_id == pid, Deliverable.code == "D3"))
        doc = docx.Document(io.BytesIO(export.export_deliverable(db, user, p, d3).data))
        assert any(c.text == "F-01" for t in doc.tables for row in t.rows for c in row.cells)
        with pytest.raises(tools.ToolError) as e:                       # 校验不过：不写
            tools.invoke(db, user, "research-interview", "doc.generate",
                         {"action": "structured", "deliverable": "D3",
                          "data": {"facts": [_fact(1, evidence=[])]}}, code)
        assert e.value.status == 422
        with pytest.raises(tools.ToolError):                             # 只支持已定义 schema 的交付件
            tools.invoke(db, user, "research-interview", "doc.generate",
                         {"action": "structured", "deliverable": "D1", "data": {}}, code)
