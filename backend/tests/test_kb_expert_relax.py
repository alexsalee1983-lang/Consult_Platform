"""四期 B38①：专家输入只拦三类（引用对不上、资质用语、机密），其余给建议，格式自动修；Agent 起草保持严格。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import agent_review
from app.kb import review as rv
from app.models import AuditLog

from .test_kb_agent_review import BODY, PASS, _submitted, lib, model  # noqa: F401


def _issue(sev: str, check: str) -> dict:
    return {"severity": sev, "check": check, "location": "第二条", "problem": "有问题", "suggestion": "改"}


def test_expert_missing_metadata_is_advice_not_block(lib, model):  # noqa: F811
    (author, _, _), law = lib
    model(PASS)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY, level3=True, applicability="")
        failed, cites = agent_review.rule_failures(db, v)
        assert failed == [] and "metadata" in agent_review.rule_advice(v, cites)
        out = agent_review.run(db, v)
        r = next(x for x in v.reviews if x.review_role == "agent")
        assert out.rules_failed == [] and r.decision == "approve" and "metadata" in r.findings["rules_advice"]


def test_agent_draft_missing_metadata_still_blocks(lib, model):  # noqa: F811
    (author, _, _), law = lib
    model(PASS)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY, applicability="")
        failed, _ = agent_review.rule_failures(db, v)
        assert "metadata" in failed


def test_expert_mismatched_citation_blocks_unchecked_does_not(lib, model):  # noqa: F811
    (author, _, _), law = lib
    model(PASS)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, "## 第九十九条\n\n依据第九十九条，应当每年评估。", level3=True)
        failed, _ = agent_review.rule_failures(db, v)
        assert "citations" in failed                       # 文件在库里、条款不存在：对不上
        v2 = _submitted(db, db.merge(author), "库里没有的某法", BODY.replace("第二条", "第三条"), level3=True,
                        ref_document="库里没有的某法")
        failed2, cites2 = agent_review.rule_failures(db, v2)
        assert "citations" not in failed2                  # 尚未核对：只是建议


def test_expert_qualification_and_confidential_still_block(lib, model):  # noqa: F811
    (author, _, _), law = lib
    model(PASS)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY + "\n\n经评估，本系统已通过等保三级测评。", level3=True)
        assert "boundary" in agent_review.rule_failures(db, v)[0]
        with pytest.raises(rv.ReviewError):                # 机密在保存时就被 guard 拦下，到不了审核
            _submitted(db, db.merge(author), law, BODY + "\n\n管理员口令：password=Abc12345xyz", level3=True)


def test_expert_agent_issues_only_three_keep_block(lib, model):  # noqa: F811
    (author, _, _), law = lib
    reply = {**PASS, "checks": {**PASS["checks"], "no_addition": {"pass": False, "reason": "存疑"}},
             "issues": [_issue("阻断", "no_addition"), _issue("重要", "applicability"), _issue("阻断", "citation_fit")],
             "conclusion": "存疑"}
    model(reply)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY, level3=True)
        agent_review.run(db, v)
        r = next(x for x in v.reviews if x.review_role == "agent")
        iss = r.findings["issues"]
        assert iss[0]["severity"] == "建议" and iss[0]["original_severity"] == "阻断"
        assert iss[1]["severity"] == "建议" and iss[1]["original_severity"] == "重要"
        assert iss[2]["severity"] == "阻断"
        assert r.decision == "flag"                         # 还有保持阻断的一项


def test_expert_only_relaxed_issues_is_clean(lib, model):  # noqa: F811
    (author, _, _), law = lib
    reply = {**PASS, "checks": {**PASS["checks"], "no_addition": {"pass": False, "reason": "存疑"}},
             "issues": [_issue("阻断", "no_addition")], "conclusion": "存疑"}
    model(reply)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY, level3=True)
        agent_review.run(db, v)
        r = next(x for x in v.reviews if x.review_role == "agent")
        assert r.decision == "approve" and r.findings["issues"][0]["original_severity"] == "阻断"


def test_agent_draft_issue_stays_strict(lib, model):  # noqa: F811
    (author, _, _), law = lib
    reply = {**PASS, "issues": [_issue("重要", "applicability")]}
    model(reply)
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, BODY)
        out = agent_review.run(db, v)
        assert out.status == "manual" and v.status == "in_review"
        r = next(x for x in v.reviews if x.review_role == "agent")
        assert r.findings["issues"][0]["severity"] == "重要"


def test_expert_format_tidied_and_noted_without_original_in_audit(lib, model):  # noqa: F811
    (author, _, _), law = lib
    model(PASS)
    messy = "## 第 二 条 风险评估\n\n依据第 二 条,运营者应当每年开展１次风险评估;可结合年度检查一并进行。   \n\n\n\n补充：见 8．1．4"
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, messy, level3=True)
        agent_review.run(db, v)
        assert "第二条，" in v.body and "；可结合" in v.body and "1次" in v.body and "8.1.4" in v.body
        assert "\n\n\n" not in v.body and not v.body.splitlines()[2].endswith(" ")
        r = next(x for x in v.reviews if x.review_role == "agent")
        assert "已自动整理格式" in r.comment and r.findings["format_tidied"] is True
        log = db.scalar(select(AuditLog).where(AuditLog.action == "kb.agent_review").order_by(AuditLog.id.desc()))
        assert "风险评估" not in log.detail and '"tidied": true' in log.detail


def test_tidy_does_not_change_agent_draft(lib, model):  # noqa: F811
    (author, _, _), law = lib
    model(PASS)
    body = BODY + "   \n\n\n\n尾部,保持"
    with SessionLocal() as db:
        v = _submitted(db, db.merge(author), law, body)
        agent_review.run(db, v)
        assert "尾部,保持" in v.body and rv.level3(v) is False
