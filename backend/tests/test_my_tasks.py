"""三期 B17②“我的任务”：七类待办合一，按影响 × 不确定度排序，每日额度，拿不准的字段标黄，退回排队由 Agent 重写。"""
import secrets

from sqlalchemy import select

from app.core.db import SessionLocal
from app.main import app
from app.models import Confirmation, Deliverable, Project, TaskReturn
from app.models.job import Job
from app.project import my_tasks as mt
from app.services import jobs

from .conftest import create_user, logged_in_client


def _project(review: bool = True) -> tuple[str, int]:
    code = f"MT-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code="D4", title="现状评估报告", status="review" if review else "drafting")]
        db.add(p)
        db.commit()
        return code, p.id


# ---- 纯逻辑：评分、排序、额度 ----

def _t(key: str, impact: float, unc: float, blocked: str = "", age: int = 0) -> mt.Task:
    return mt.Task(kind="confirmation", ref_id=hash(key) % 10000, title=key, link="/", impact=impact,
                   uncertainty=unc, blocked=blocked, created_ts=float(age))


def test_score_is_impact_times_uncertainty_with_floor():
    w = mt.Weights(impact=1.0, uncertainty=1.0, quota=10)
    assert mt.score(_t("a", 0, 0), w) == 1.0                         # 拿不到的按 0，仍可排序
    assert mt.score(_t("b", 3, 1), w) == (1 + 3) * (1 + 1)
    assert mt.score(_t("c", 3, 1), mt.Weights(impact=2.0, uncertainty=0.0, quota=10)) == (1 + 6) * 1


def test_rank_orders_by_score_then_age_and_blocked_last():
    w = mt.Weights(impact=1.0, uncertainty=1.0, quota=10)
    tasks = [_t("low", 0, 0, age=1), _t("high", 5, 1), _t("mid", 2, 0, age=5), _t("mid-old", 2, 0, age=2),
             _t("blocked-high", 9, 1, blocked="需另一位知识专家处理")]
    ranked = mt.rank(tasks, w)
    assert [t.title for t in ranked] == ["high", "mid-old", "mid", "low", "blocked-high"]


def test_daily_quota_folds_overflow_and_ignores_blocked():
    w = mt.Weights(impact=1.0, uncertainty=1.0, quota=2)
    ranked = mt.rank([_t("a", 3, 0), _t("b", 2, 0), _t("c", 1, 0), _t("x", 9, 9, blocked="别人")], w)
    mt.apply_quota(ranked, w.quota)
    assert [(t.title, t.folded) for t in ranked] == [("a", False), ("b", False), ("c", True), ("x", False)]


def test_weights_come_from_settings(monkeypatch):
    monkeypatch.setenv("TASKS_IMPACT_WEIGHT", "2.5")
    monkeypatch.setenv("TASKS_DAILY_QUOTA", "7")
    from app.core.config import get_settings
    get_settings.cache_clear()
    try:
        w = mt.weights()
        assert (w.impact, w.quota) == (2.5, 7)
    finally:
        get_settings.cache_clear()


# ---- 接口 ----

def test_my_tasks_lists_only_actionable_kinds_ranked_and_flags_uncertain_fields():
    code, pid = _project()
    other_code, other_pid = _project()
    pm = create_user(("consultant",), {code: "manager"})
    with SessionLocal() as db:
        db.add_all([
            Confirmation(project_id=pid, kind="scope", title="确认值班模式", agent="方案设计 Agent",
                         recommendation="A", deliverable_code="D4"),
            Confirmation(project_id=pid, kind="scope", title="证据不足待定", agent="方案设计 Agent", recommendation=""),
            Confirmation(project_id=other_pid, kind="scope", title="别的项目的卡", agent="方案设计 Agent"),
        ])
        db.commit()
    with logged_in_client(app, pm) as c:
        r = c.get("/api/my-tasks")
        assert r.status_code == 200
        body = r.json()
        assert set(body["kinds"]) == set(mt.KINDS)                   # 七类都有位置，没接上的为空不报错
        assert body["kinds"]["practice"]["available"] is False and body["kinds"]["source_confirm"]["available"] is False
        mine = [t for t in body["tasks"] if t["project_code"] in (code, other_code)]
        titles = [t["title"] for t in mine]
        assert "别的项目的卡" not in titles                             # 非成员项目不出现
        assert any(t["kind"] == "deliverable_review" and t["project_code"] == code for t in mine)
        unsure = next(t for t in mine if t["title"] == "证据不足待定")
        sure = next(t for t in mine if t["title"] == "确认值班模式")
        assert unsure["uncertainty"] > sure["uncertainty"]            # Agent 没给推荐 = 存疑
        assert titles.index("证据不足待定") < titles.index("确认值班模式") or unsure["score"] >= sure["score"]
        rec = next(f for f in unsure["fields"] if f["name"] == "recommendation")
        assert rec["uncertain"] is True                               # 拿不准的字段标黄
        assert next(f for f in sure["fields"] if f["name"] == "recommendation")["uncertain"] is False
        assert body["quota"] >= 1 and "weights" in body


def test_daily_quota_folds_via_api(monkeypatch):
    code, pid = _project(review=False)
    pm = create_user(("consultant",), {code: "manager"})
    with SessionLocal() as db:
        db.add_all([Confirmation(project_id=pid, kind="scope", title=f"额度卡{i}", agent="方案设计 Agent",
                                 recommendation="A") for i in range(3)])
        db.commit()
    monkeypatch.setenv("TASKS_DAILY_QUOTA", "1")
    from app.core.config import get_settings
    get_settings.cache_clear()
    try:
        with logged_in_client(app, pm) as c:
            body = c.get("/api/my-tasks").json()
        live = [t for t in body["tasks"] if not t["blocked"]]
        assert sum(not t["folded"] for t in live) == 1 and body["folded"] == len(live) - 1
    finally:
        get_settings.cache_clear()


def test_return_records_event_enqueues_rewrite_and_hides_task():
    code, pid = _project(review=False)
    pm = create_user(("consultant",), {code: "manager"})
    outsider = create_user(("consultant",), {})
    with SessionLocal() as db:
        card = Confirmation(project_id=pid, kind="scope", title="退回测试卡", agent="方案设计 Agent")
        db.add(card)
        db.commit()
        card_id = card.id
    with logged_in_client(app, outsider) as c:
        r = c.post("/api/my-tasks/return", json={"kind": "confirmation", "ref_id": card_id, "reason": "依据不对"})
        assert r.status_code == 404                                   # 不是项目成员：看不到
    with logged_in_client(app, pm) as c:
        assert c.post("/api/my-tasks/return",
                      json={"kind": "deliverable_review", "ref_id": 1, "reason": "x"}).status_code == 422
        r = c.post("/api/my-tasks/return", json={"kind": "confirmation", "ref_id": card_id, "reason": "依据不对，请重写"})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued" and body["job_id"]
        titles = [t["title"] for t in c.get("/api/my-tasks").json()["tasks"]]
        assert "退回测试卡" not in titles                              # 已退回：等 Agent 重写后再排
        assert c.post("/api/my-tasks/return",
                      json={"kind": "confirmation", "ref_id": card_id, "reason": "再退"}).status_code == 409
    with SessionLocal() as db:
        ret = db.scalar(select(TaskReturn).where(TaskReturn.kind == "confirmation", TaskReturn.ref_id == card_id))
        assert ret is not None and ret.status == "queued"
        job = db.get(Job, body["job_id"])
        assert job.kind == mt.REWRITE_JOB and "依据不对" not in job.payload   # 任务参数只放编号
        assert jobs.run(db, job) == jobs.DONE                         # 走派发约定 fn(payload, ctx)
        db.refresh(ret)
        assert ret.status == "waiting_agent"


def test_return_reason_goes_through_guard():
    code, pid = _project(review=False)
    pm = create_user(("consultant",), {code: "manager"})
    with SessionLocal() as db:
        card = Confirmation(project_id=pid, kind="scope", title="机密检测卡", agent="方案设计 Agent")
        db.add(card)
        db.commit()
        card_id = card.id
    with logged_in_client(app, pm) as c:
        r = c.post("/api/my-tasks/return",
                   json={"kind": "confirmation", "ref_id": card_id, "reason": "password=Abcd1234!xyz"})
        assert r.status_code == 422


def test_knowledge_tasks_use_agent_flag_and_mark_uncertain_fields():
    from app.models.kb import KbItem, KbReview, KbVersion
    expert, other = create_user(("knowledge_expert",)), create_user(("knowledge_expert",))
    tag = secrets.token_hex(2).upper()
    with SessionLocal() as db:
        ids = {}
        for n, (sample, decision, author) in {"flag": ("pending", "flag", other[0]),
                                              "plain": ("pending", "approve", other[0]),
                                              "mine": ("pending", "approve", expert[0])}.items():
            item = KbItem(doc_id=f"L2-OPS-SOC-TPL-{tag}{n[:1].upper()}", layer="L2", domain="OPS", product="SOC",
                          doc_type="TPL", stage="S3", classification="internal", owner=author)
            db.add(item)
            db.flush()
            v = KbVersion(item_id=item.id, version=1, title=f"我的任务{tag}{n}", body="正文", status="published",
                          source_type="public", created_by=author, submitted_by=author, sample_state=sample,
                          ref_document="GB/T 22239-2019", ref_version="")
            db.add(v)
            db.flush()
            item.current_version_id = v.id
            db.add(KbReview(version_id=v.id, reviewer="kb-reviewer", review_role="agent", decision=decision,
                            findings={"uncertain_fields": ["ref_document"]} if n == "flag" else {}))
            ids[n] = v.id
        db.commit()
    with logged_in_client(app, expert) as c:
        tasks = [t for t in c.get("/api/my-tasks").json()["tasks"] if tag in t["title"] and t["kind"] == "sample"]
    by = {t["ref_id"]: t for t in tasks}
    flag, plain, mine = by[ids["flag"]], by[ids["plain"]], by.get(ids["mine"])
    assert flag["uncertainty"] == 1.0 and plain["uncertainty"] == 0.0 and flag["score"] > plain["score"]
    f = {x["name"]: x["uncertain"] for x in flag["fields"]}
    assert f["ref_document"] is True and f["ref_version"] is True           # Agent 标存疑、空着的都标黄
    assert {x["name"]: x["uncertain"] for x in plain["fields"]}["ref_document"] is False
    assert mine is None                                                       # A29：别人的卡隐藏，不灰显
