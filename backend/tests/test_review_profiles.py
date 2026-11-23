"""三期 A13：按交付件读评审档案。产品配置 deliverables[].review_profile（full 全面 / light 只查红线和专项，
缺省 full）；light 档下评审 Agent 判的五类以外问题一律降为“建议”，不挡门禁；档案写进发给 Agent 的任务和审计。"""
import json

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import AuditLog
from app.products import config
from app.project import configs, quality, review_issues

from .test_quality import _draft, _review, model, team  # noqa: F401
from .test_review_issues import HEAD, ROW_A, ROW_B

ROW_QUAL = "| 3 | 第 1 章 | 范围与资质边界 | 重要 | 出现测评结论用语 | 改为差距分析 |\n"


def test_soc_d1_d2_light_others_full():
    snap = config.compose_codes("SOC")
    prof = {d["code"]: d["review_profile"] for d in snap["deliverables"]}
    assert prof["D1"] == prof["D2"] == "light"
    assert {v for k, v in prof.items() if k not in ("D1", "D2")} == {"full"}
    for p in config.catalog().products.values():
        assert {d.code: d.review_profile for d in p.deliverables}.get("D1") == "light", p.code


def test_unknown_profile_is_config_problem():
    assert review_issues.PROFILES.keys() == {"full", "light"}
    assert config.parse_review_profile("light") == ("light", "")
    assert config.parse_review_profile(None) == ("full", "")
    assert config.parse_review_profile("strict")[1]


def test_profile_of_old_snapshot_defaults_full():
    assert review_issues.profile_of({"deliverables": [{"code": "D4"}]}, "D4") == "full"
    assert review_issues.profile_of({"deliverables": [{"code": "D1", "review_profile": "light"}]}, "D1") == "light"
    assert review_issues.profile_of({}, "D9") == "full"


def test_light_caps_out_of_category_issues():
    issues = [{"by": "agent", "severity": "重要", "dimension": "建议质量", "problem": "建议缺少验收标准"},
              {"by": "agent", "severity": "重要", "dimension": "范围与资质边界", "problem": "出现测评结论用语"},
              {"by": "agent", "severity": "阻断", "dimension": "证据链", "problem": "对客结论没有证据编号"},
              {"by": "rule", "severity": "重要", "dimension": "其他", "problem": "缺章节"}]
    counts = {"阻断": 1, "重要": 2, "建议": 0}
    assert review_issues.apply_profile(issues, counts, "light") == 1
    assert [i["severity"] for i in issues] == ["建议", "重要", "阻断", "重要"]
    assert issues[0]["profile_capped"] and counts == {"阻断": 1, "重要": 1, "建议": 1}
    assert review_issues.apply_profile(issues, counts, "full") == 0


def test_light_profile_review_end_to_end(team, model, monkeypatch):  # noqa: F811
    code, member, *_ = team
    real = configs.snapshot

    def light_d4(db, project):
        snap = dict(real(db, project))
        snap["deliverables"] = [dict(d, review_profile="light") if d["code"] == "D4" else d
                                for d in snap["deliverables"]]
        return snap

    monkeypatch.setattr(quality.configs, "snapshot", light_d4)
    model.reply = HEAD.format(b=1, m=2) + ROW_A + ROW_B + ROW_QUAL
    _draft(member, code)
    rv = _review(member, code)
    sev = {i["problem"]: i["severity"] for i in rv["issues"] if i["by"] == "agent"}
    assert sev == {"夜间差距结论没有证据编号": "阻断", "建议缺少验收标准": "建议", "出现测评结论用语": "重要"}
    sent = json.dumps(model.requests[-1], ensure_ascii=False)
    assert "评审档案：只查红线和专项" in sent
    with SessionLocal() as db:
        log = db.scalars(select(AuditLog).where(AuditLog.project_code == code, AuditLog.action == "quality.review")
                         .order_by(AuditLog.id)).all()[-1]
        d = json.loads(log.detail)
        assert d["profile"] == "light" and d["profile_capped"] == 1


def test_full_profile_message(team, model):  # noqa: F811
    code, member, *_ = team
    _draft(member, code)
    _review(member, code)
    assert "评审档案：全面评审" in json.dumps(model.requests[-1], ensure_ascii=False)
