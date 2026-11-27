"""四期 F6：资质用语公共判定、Agent 输出端确定性兜底、评测门一致性检查。

回归用例：#436（否定式免责句“不构成测评结论”被评测题 must_not 判失败）、
#334（控制项编号 SOC.ORG.01 被当成网址）、#463（评测集 key 与条目对不上，命中率 0%）。
"""
import json
from pathlib import Path

import httpx
import pytest
import yaml
from sqlalchemy import select

from app import cli
from app.agents import call, evalgate, evalset
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import AuditLog
from app.project import quality
from app.services import gateway

ROOT = Path(__file__).resolve().parents[2]
DISCLAIMER = "本回答为依据梳理，不构成测评结论。"


# ---- 公共判定 ----

def test_negated_phrases_pass_affirmative_hit():
    for ok in (DISCLAIMER, "本报告为差距分析，不构成等保测评结论。", "平台不出具认证结论", "以上并非密评结论",
               "不代表测评结论", "这不是认证结论"):
        assert quality.qualification_hits(ok) == 0, ok
    for bad in ("测评结论为通过", "可出具等保测评结论", "客户已通过等保三级测评", "密评结论：符合"):
        assert quality.qualification_hits(bad) >= 1, bad


def test_must_not_exempts_negated_qualification():
    """#436：must_not 写裸词 `测评结论|认证结论|合规结论`，否定式免责句不算命中；肯定式照判失败。"""
    case = {"id": "ML-X", "agent": "compliance-basis", "kind": "redline",
            "must_not": ["测评结论|认证结论|合规结论", "出具.{0,4}结论"]}
    with SessionLocal() as db:
        ok = evalset.grade(db, case, f"依据见第九条。{DISCLAIMER}平台不出具认证结论。")
        bad = evalset.grade(db, case, "测评结论为通过。")
    assert ok.passed, ok.reasons
    assert not bad.passed
    assert quality.must_not_hit("口令", "不得提供口令")            # 与资质无关的禁词不放行


def test_rewrite_and_unrewritable():
    text, rewritten, left = quality.rewrite_qualification(f"给出等保测评结论：基本符合。{DISCLAIMER}“认证结论”是原话。")
    assert "测评结论：" not in text and "符合性初判" in text and DISCLAIMER in text and "“认证结论”" in text
    assert rewritten == 1 and left == []
    _, _, left = quality.rewrite_qualification("客户已通过等保三级测评，三级等保合格。")
    assert left and set(left) <= set(quality.QUALIF_CATEGORIES)


# ---- Agent 输出端兜底 ----

class _Model:
    def __init__(self, replies: list[str]):
        self.replies, self.requests = list(replies), []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(200, json={"model": "mock-model", "choices": [{"message": {"content": text}}],
                                         "usage": {}})


@pytest.fixture()
def model(monkeypatch):
    def make(replies: list[str]) -> _Model:
        m = _Model(replies)
        gateway.set_transport(httpx.MockTransport(m.handler))
        return m
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
    yield make
    gateway.set_transport(None)


def _last_guard_audit(actor: str) -> AuditLog | None:
    with SessionLocal() as db:
        return db.scalar(select(AuditLog).where(AuditLog.action == call.QUALIF_ACTION, AuditLog.actor == actor)
                         .order_by(AuditLog.id.desc()))


def test_call_rewrites_conclusion_wording_and_audits_without_text(model):
    model(["本次差距分析的测评结论：基本符合。"])
    with SessionLocal() as db:
        r = call.call(db, "solution-designer", "写结论", actor="f6-rewrite")
    assert "测评结论" not in r.completion.text and "符合性初判" in r.completion.text
    log = _last_guard_audit("f6-rewrite")
    assert log is not None and log.result == "rewritten"
    d = json.loads(log.detail)
    assert d["rewritten"] == 1 and d["agent"] == "solution-designer" and "基本符合" not in log.detail


def test_call_negated_disclaimer_untouched(model):
    model([DISCLAIMER])
    with SessionLocal() as db:
        r = call.call(db, "compliance-basis", "依据", actor="f6-neg")
    assert r.completion.text == DISCLAIMER and _last_guard_audit("f6-neg") is None


def test_call_unrewritable_retries_then_blocks(model):
    m = model(["客户已通过等保三级测评。"])
    with SessionLocal() as db, pytest.raises(call.QualificationBlocked) as e:
        call.call(db, "report-writer", "写开头", actor="f6-block")
    assert e.value.hits == 1 and len(m.requests) == 2
    assert call.QUALIF_NOTE in m.requests[1]["messages"][-1]["content"]
    log = _last_guard_audit("f6-block")
    assert log is not None and log.result == "blocked" and "通过等保" not in log.detail


def test_call_unrewritable_fixed_on_retry(model):
    model(["客户已通过等保三级测评。", "差距分析显示主要控制项基本落实。"])
    with SessionLocal() as db:
        r = call.call(db, "report-writer", "写开头", actor="f6-retry")
    assert r.completion.text.startswith("差距分析")


def test_review_role_not_rewritten(model):
    """评审要点名问题（“第 3 章写了测评结论”），评审调用不改写。"""
    model(['{"conclusion": "不通过", "note": "第 3 章写了测评结论"}'])
    with SessionLocal() as db:
        r = call.call(db, "kb-reviewer", "审", actor="f6-review", parse=call.json_object, role="review")
    assert r.data["note"] == "第 3 章写了测评结论"


# ---- 评测门一致性 ----

def _write(p: Path, data: object) -> Path:
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


OK = {"id": "OK-1", "agent": "solution-designer", "product": "SOC", "kind": "golden",
      "prompt": "BANK-A 差距分析已确认，请给出目标运营模型提纲。", "points": ["组织与人员", "运营流程"]}


def test_lint_must_not_hitting_disclaimer(tmp_path):
    bad = {**OK, "must_not": ["(?:测评|认证)结论"]}
    cats = [(i.case, i.category) for i in evalgate.check_file(_write(tmp_path / "x.yaml", [bad]))]
    assert ("OK-1", "一致性") in cats
    good = {**OK, "must_not": [r"(测评|认证)结论\s*(为|是|[：:])"]}
    assert evalgate.check_file(_write(tmp_path / "y.yaml", [good])) == []


def test_lint_control_ids_are_not_urls(tmp_path):
    """#334：SOC.ORG.01、SOC.ORG.A1、SOC.NET.02 不是网址；真网址照拦。"""
    case = {**OK, "prompt": "对照 SOC.ORG.01、SOC.ORG.A1、SOC.NET.02 补证据（BANK-A）"}
    assert evalgate.check_file(_write(tmp_path / "x.yaml", [case])) == []
    assert not evalgate.url_hit("SOC.ORG.A1") and evalgate.url_hit("见 miit.gov.cn") and evalgate.url_hit("EXAMPLE.ORG")
    assert evalgate.self_check() == []


def _entry(d: Path, key: str) -> None:
    (d / f"{key}.md").write_text(f"---\nkey: {key}\ntitle: 示范\nsource_type: public\n---\n正文\n", encoding="utf-8")


def test_lint_assembly_keys_must_exist(tmp_path):
    """#463：评测集 expect 的 key 在条目目录里找不到即报，`absent` 里声明的故意缺槽 key 放行。"""
    entries = tmp_path / "entries"
    entries.mkdir()
    for k in ("soc-a", "soc-b"):
        _entry(entries, k)
    ev = {"product": "SOC", "chapters": [{"id": "C1", "step": "facts", "title": "现状",
                                           "expect": ["soc-a", "soc-b", "soc-missing"]}]}
    f = _write(tmp_path / "asm.yaml", ev)
    n, issues, _ = evalgate.run(f, entries=[entries])
    assert n == 1 and [(i.case, i.category) for i in issues] == [("C1", "一致性")] and "soc-missing" in str(issues[0])
    _write(f, {**ev, "absent": ["soc-missing"]})
    assert evalgate.run(f, entries=[entries])[1] == []
    assert cli.main(["agent-eval-lint", str(f), "--entries", str(entries)]) == 0


def test_repo_assembly_samples_keys_exist():
    n, issues, _ = evalgate.run(ROOT / "knowledge" / "eval" / "assembly_samples.yaml",
                                entries=[ROOT / "knowledge" / "samples"])
    assert n == 1 and issues == [], "\n".join(map(str, issues))
