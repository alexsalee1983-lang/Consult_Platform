"""依据时效门禁（二期 B9）：质量评审按知识库当前状态核对交付件引用的条目——已下线、依据已废止 → 阻断（平台规则，
不能驳回）；即将实施 → 重要；评审之后条目下线或依据废止，签署门禁实时拦住。"""
from datetime import date, timedelta

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import KbItem, KbVersion
from app.project import quality

from .test_kb_search import _publish, people  # noqa: F401  （夹具）
from .test_quality import _review, _url, model, team  # noqa: F401  （夹具）


def _basis(people, title: str, status: str):  # noqa: F811
    return _publish(people, title, "## 6.1 监测\n\n应开展 7×24 安全监测。", layer="L4", product="GEN", doc_type="REF",
                    stage="通用", ref=(f"JR/T 9{len(title)}01", "2023", status))


def _draft(c, code: str, refs: tuple[str, str]) -> None:
    assert c.put(f"/api/projects/{code}/deliverables/D4/outline", json={"titles": ["依据", "差距"]}).status_code == 200
    for i, ref in enumerate(refs, 1):
        body = f"夜间无人值守，与 7×24 监测要求存在差距【依据：某办法 2023 第 6.1 条｜{ref}】[{ref}]。"
        r = c.put(f"/api/projects/{code}/deliverables/D4/sections/{i}", json={"body": body})
        assert r.status_code == 201 and r.json()["sources"]["unresolved"] == [], r.text


def _rules(rv: dict) -> list[dict]:
    return [i for i in rv["issues"] if i["by"] == "rule" and i["dimension"] == "证据链 / 依据"]


def test_repealed_blocks_upcoming_is_major(team, model, people):  # noqa: F811
    code, member, *_ = team
    ok, repealed = _basis(people, "现行办法甲", "current"), _basis(people, "已废止办法乙", "repealed")
    _draft(member, code, (ok.doc_id, repealed.doc_id))
    rv = _review(member, code)
    rules = _rules(rv)
    assert rv["conclusion"] == "fail"
    assert [(i["severity"], i["location"][:5]) for i in rules] == [("阻断", "第 2 章")]
    assert repealed.doc_id in rules[0]["problem"] and "已废止" in rules[0]["problem"]
    assert not quality.waivable(rules[0])                               # 平台规则判的阻断不能驳回
    assert member.post(_url(code, "signoff/submit"), json={}).status_code == 409

    with SessionLocal() as db:                                         # 依据改为即将实施：降为重要
        db.get(KbVersion, repealed.id).ref_status = "upcoming"
        db.commit()
    rules = _rules(_review(member, code))
    assert [i["severity"] for i in rules] == ["重要"] and "尚未施行" in rules[0]["problem"]


def test_states_and_gate_after_review(team, model, people):  # noqa: F811
    code, member, mgr, _ = team
    a, b = _basis(people, "现行办法丙", "current"), _basis(people, "现行办法丁丁", "current")
    with SessionLocal() as db:
        db.get(KbVersion, b.id).effective_date = date.today() + timedelta(days=30)
        db.commit()
        states = quality.kb_ref_states(db, {a.doc_id, b.doc_id, "L4-OPS-GEN-REF-999"})
    assert states == {b.doc_id: quality.KB_UPCOMING, "L4-OPS-GEN-REF-999": quality.KB_GONE}

    _draft(member, code, (a.doc_id, a.doc_id))
    assert _rules(_review(member, code)) == []
    with SessionLocal() as db:                                         # 评审之后条目下线：提交被拦
        db.scalar(select(KbItem).where(KbItem.doc_id == a.doc_id)).retired = True
        db.commit()
    r = member.post(_url(code, "signoff/submit"), json={})
    assert r.status_code == 409 and any(a.doc_id in x and "下线" in x for x in r.json()["detail"]["reasons"])
    rules = _rules(_review(member, code))
    assert {i["severity"] for i in rules} == {"阻断"} and all("已下线" in i["problem"] for i in rules)
