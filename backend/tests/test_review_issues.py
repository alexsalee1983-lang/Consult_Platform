"""三期 A13：评审问题固定编号（跨轮次沿用、消失记已解决、再出现重新打开）、封闭五类阻断（五类外降为重要）、
已驳回不重提、评审 Agent 写已有编号可对上、审计只记计数不记原文。"""
import json

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import AuditLog, Deliverable, Project, ReviewIssue
from app.project import review_issues

from .test_quality import BODY2, _draft, _review, _url, model, team  # noqa: F401

HEAD = "# 评审意见\n评审结论：不通过\n统计：阻断 {b} 项，重要 {m} 项，建议 0 项\n\n" \
       "| # | 位置 | 维度 | 严重度 | 问题 | 修改建议 |\n|---|---|---|---|---|---|\n"
ROW_A = "| 1 | 第 2 章 | 证据链 | 阻断 | 夜间差距结论没有证据编号 | 补证据 |\n"
ROW_B = "| 2 | 第 1 章 | 建议质量 | 重要 | 建议缺少验收标准 | 补充验收标准 |\n"
ROW_C = "| 3 | 第 2 章 | 一致性 | 重要 | 人数与第 1 章不一致 | 统一口径 |\n"
ROW_OUT = "| 1 | 第 2 章 | 业务关联 | 阻断 | 托管初筛成本未论证 | 补充成本比较 |\n"


def _ledger(code: str) -> dict[str, str]:
    with SessionLocal() as db:
        d = db.scalar(select(Deliverable).join(Project).where(Project.code == code, Deliverable.code == "D4"))
        return {x.code: x.status for x in review_issues.rows(db, d)}


def _codes(rv: dict) -> dict[str, str]:
    return {i["problem"]: i.get("code", "") for i in rv["issues"] if i["by"] == "agent"}


def test_closed_five_categories_downgrade():
    issues = [{"by": "agent", "severity": "阻断", "dimension": "业务关联", "problem": "成本未论证"},
              {"by": "agent", "severity": "阻断", "dimension": "范围与资质边界", "problem": "出现测评结论用语"},
              {"by": "agent", "severity": "阻断", "dimension": "一致性", "problem": "与确认卡 C-02 矛盾"},
              {"by": "rule", "severity": "阻断", "dimension": "其他", "problem": "缺正文"}]
    counts = {"阻断": 3, "重要": 0, "建议": 0}
    assert review_issues.apply_closed_categories(issues, counts) == 1
    assert [i["severity"] for i in issues] == ["重要", "阻断", "阻断", "阻断"]
    assert issues[0]["downgraded"] and issues[1]["category"] == "资质用语"
    assert issues[2]["category"] == "与已确认事项矛盾"
    assert counts == {"阻断": 2, "重要": 1, "建议": 0}


def test_out_of_category_blocker_does_not_close_gate(team, model):  # noqa: F811
    code, member, *_ = team
    model.reply = HEAD.format(b=1, m=0) + ROW_OUT
    _draft(member, code)
    rv = _review(member, code)
    i = next(i for i in rv["issues"] if i["by"] == "agent")
    assert i["severity"] == "重要" and i["downgraded"] and not i["waivable"]
    assert rv["blocking"] == 0 and rv["major"] == 1 and rv["conclusion"] == "pass"
    st = member.get(_url(code, "signoff")).json()
    assert st["gate"], st["reasons"]


def test_codes_fixed_across_rounds_resolve_and_reopen(team, model):  # noqa: F811
    code, member, *_ = team
    model.reply = HEAD.format(b=1, m=1) + ROW_A + ROW_B
    _draft(member, code)
    c1 = _codes(_review(member, code))
    qa, qb = c1["夜间差距结论没有证据编号"], c1["建议缺少验收标准"]
    assert {qa, qb} == {"Q-001", "Q-002"}

    # 第二轮：A 措辞变了但写了原编号，B 不再出现 → B 已解决；C 是新问题
    model.reply = HEAD.format(b=1, m=1) + ROW_A.replace("夜间差距结论没有证据编号", f"{qa} 仍缺证据编号") + ROW_C
    assert member.put(_url(code, "sections/2"), json={"body": BODY2 + "补充。"}).status_code == 201
    assert member.put(_url(code, "sections/1"), json={"body": "两章都改过：全文评审（A13② 增量只在部分章节改动时）。"}
                      ).status_code == 201
    c2 = _codes(_review(member, code))
    assert c2[f"{qa} 仍缺证据编号"] == qa and c2["人数与第 1 章不一致"] == "Q-003"
    assert _ledger(code) == {qa: "open", qb: "resolved", "Q-003": "open"}

    # 第三轮：B 又出现 → 同一编号重新打开
    model.reply = HEAD.format(b=0, m=1) + ROW_B
    assert member.put(_url(code, "sections/2"), json={"body": BODY2}).status_code == 201
    c3 = _codes(_review(member, code))
    assert c3["建议缺少验收标准"] == qb
    assert _ledger(code) == {qa: "resolved", qb: "open", "Q-003": "resolved"}

    with SessionLocal() as db:
        logs = [x for x in db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                               AuditLog.action == "quality.review"))]
        last = json.loads(logs[-1].detail)
        assert (last["issues_reopened"], last["issues_resolved"], last["issues_new"]) == (1, 2, 0)
        dump = json.dumps([x.detail for x in logs], ensure_ascii=False)
        assert "验收标准" not in dump and "证据编号" not in dump
        rows = db.scalars(select(ReviewIssue)).all()
        assert all(len(r.fingerprint) == 16 for r in rows)
