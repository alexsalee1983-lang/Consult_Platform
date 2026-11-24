"""四期 B23①：专家工作台二批——审核问答化、按类型填空卡、大白话界面（在 B17 确认队列上扩展，无迁移）。

- 问答卡只列本人能做内容审核的待审版本（知识专家、不是作者），按“我的任务”的影响 × 不确定度排序、带每日额度；
- 每张卡把审核拆成几道“对 / 不对 / 不确定”的问题：值不值得收、依据文件等字段对不对、Agent 提的问题认不认；
- 回答换成审核决定：值得收 = 否 → 驳回；有改正或填空卡改了正文 → 修改后通过；全“对” → 通过；
  “值不值得收”答不确定 → 不拍板；认可 Agent 提的问题却没改正文 → 422；
- 按类型填空卡：按知识类型、交付物类型给几格短填空（可直接贴零散文字），拼成正文并过机密级检测；
- 审计只记编号、结果和计数，不记回答原文。
"""
import uuid

from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import expert_bench as eb
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent, create_item
from app.main import app
from app.models import AuditLog, KbPracticeDecision, KbReview, KbVersion, User

from .conftest import create_user, logged_in_client

BODY = "## 做法\n\n按示范步骤开展值班交接。\n\n## 适用\n\n中型 SOC。"


def _user(*roles: str) -> tuple[User, tuple]:
    creds = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == creds[0])), creds


def _version(author: User, kind: str = "practice", doc_type: str = "TPL", agent: dict | None = None,
             ref_document: str = "示范制度", applicability: str = "") -> int:
    with SessionLocal() as db:
        author = db.merge(author)
        m = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type=doc_type, stage="设计", capabilities=["运营"],
                     knowledge_kind=kind)
        c = VersionContent(title=f"示范条目{uuid.uuid4().hex[:6]}", body=BODY, source_type="expert",
                           source_detail="专家萃取：示范", ref_document=ref_document, applicability=applicability)
        v = create_item(db, m, c, created_by=author.username)
        v.deid_status = "passed"                     # 模拟投料去标识已确认（只在测试里直接改）
        db.commit()
        rv.submit(db, author, v)
        db.add(KbReview(version_id=v.id, reviewer="agent:kb-reviewer", review_role="agent", decision="approve",
                        comment="依据：专家萃取访谈记录", findings=agent or {}))
        db.commit()
        return v.id


def _card(db, user: User, vid: int) -> dict | None:
    return next((c for c in eb.cards(db, user)["cards"] if c["version_id"] == vid), None)


def test_cards_questions_plain_words_and_author_excluded():
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    vid = _version(author, agent={"confidence": 0.6, "issues": ["缺适用规模"], "uncertain_fields": ["ref_document"]})
    with SessionLocal() as db:
        assert _card(db, db.merge(author), vid) is None           # 作者不能审自己的
        card = _card(db, db.merge(expert), vid)
    assert card is not None and card["kind_plain"] == "公司做法"
    qs = {q["id"]: q for q in card["questions"]}
    assert qs["keep"]["text"].startswith("这条值得收进知识库吗")
    assert "示范制度" in qs["field:ref_document"]["text"] and qs["field:ref_document"]["uncertain"] is True
    assert qs["field:applicability"]["empty"] is True               # 空着的字段问“知道就填”
    assert qs["issue:0"]["text"].endswith("缺适用规模") and qs["issue:0"]["kind"] == "issue"
    assert card["template"]["key"] == "practice" and card["template"]["slots"][0]["required"] is True
    assert card["confidence_plain"] == "Agent 有六成把握"
    assert "in_review" in eb.PLAIN and eb.PLAIN["in_review"] == "等你看"


def test_answers_all_yes_approves_and_records_decision():
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    vid = _version(author)
    with SessionLocal() as db:
        out = eb.answer(db, db.merge(expert), vid, [{"id": "keep", "choice": "yes"},
                                                    {"id": "field:ref_document", "choice": "yes"}], elapsed_ms=4000)
        assert out == {"outcome": "approve", "version_status": "published", "changed": []}
        d = db.scalar(select(KbPracticeDecision).where(KbPracticeDecision.version_id == vid))
        assert d.decision == "adopt" and d.elapsed_ms == 4000
        log = db.scalars(select(AuditLog).where(AuditLog.action == "kb.bench_answer")).all()[-1]
        assert "示范" not in log.detail                  # 审计不记原文


def test_corrections_and_fill_card_modify_then_approve():
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    vid = _version(author, kind="", doc_type="PIT")
    fill = {"symptom": "交接班漏记告警", "cause": "  值班表\n\n没写交接项 ", "avoid": "交接单加一栏未闭环告警"}
    with SessionLocal() as db:
        out = eb.answer(db, db.merge(expert), vid,
                        [{"id": "keep", "choice": "yes"},
                         {"id": "field:ref_document", "choice": "no", "value": "示范运营规范"},
                         {"id": "field:applicability", "choice": "yes", "value": "7×24 值班的 SOC"},
                         {"id": "field:review_date", "choice": "yes", "value": "2027-06-30"}], fill=fill)
        assert out["outcome"] == "modify" and out["version_status"] == "published"
        assert set(out["changed"]) == {"ref_document", "applicability", "review_date", "body"}
        v = db.get(KbVersion, vid)
        assert v.ref_document == "示范运营规范" and v.applicability == "7×24 值班的 SOC"
        assert str(v.review_date) == "2027-06-30"
        assert "## 会出现什么问题\n\n交接班漏记告警" in v.body and "值班表\n没写交接项" in v.body


def test_reject_undecided_and_errors():
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    a, b, c = (_version(author, agent={"issues": ["依据已废止"]}) for _ in range(3))
    with SessionLocal() as db:
        ex = db.merge(expert)
        assert eb.answer(db, ex, a, [{"id": "keep", "choice": "no", "value": "不是公司做法"}])["outcome"] == "reject"
        assert db.get(KbVersion, a).status == "rejected"
        assert eb.answer(db, ex, b, [{"id": "keep", "choice": "unsure"}])["outcome"] == "undecided"
        assert db.get(KbVersion, b).status == "in_review"
        for answers, fill, code in (
                ([{"id": "keep", "choice": "yes"}, {"id": "issue:0", "choice": "yes"}], None, 422),   # 认可问题却没改
                ([{"id": "keep", "choice": "yes"}, {"id": "field:ref_document", "choice": "no"}], None, 422),
                ([{"id": "keep", "choice": "yes"}, {"id": "field:review_date", "choice": "yes", "value": "明年"}],
                 None, 422),
                ([{"id": "keep", "choice": "yes"}], {"how": "密码 Passw0rd!2026 写在交接单上"}, 422),   # 机密级
                ([{"id": "bogus", "choice": "yes"}], None, 422)):
            try:
                eb.answer(db, ex, c, answers, fill=fill)
                raise AssertionError("应当报错")
            except rv.ReviewError as e:
                assert e.status == code, (answers, e)
                db.rollback()
        assert db.get(KbVersion, c).status == "in_review" and db.get(KbVersion, c).ref_document == "示范制度"
        try:
            eb.answer(db, db.merge(author), c, [{"id": "keep", "choice": "yes"}])     # 作者：不在本人卡片里
            raise AssertionError("应当 404")
        except rv.ReviewError as e:
            assert e.status == 404


def test_fill_compose_requires_required_slots():
    body = eb.compose("BMK", {"value": "每 2 万 EPS 配 1 名分析员", "scope": "中型 SOC"})
    assert body.startswith("## 数值或区间\n\n每 2 万 EPS")
    try:
        eb.compose("BMK", {"scope": "中型 SOC"})
        raise AssertionError("缺必填")
    except rv.ReviewError as e:
        assert e.status == 422 and "数值或区间" in str(e)
    assert eb.template_for("", "ZZZ")["key"] == "default"


def test_api_permissions_and_flow():
    author, _ = _user("knowledge_expert")
    _, ex_creds = _user("knowledge_expert")
    _, adm_creds = _user("knowledge_admin")
    _, con_creds = _user("consultant")
    vid = _version(author)
    ex, adm, con = (logged_in_client(app, c) for c in (ex_creds, adm_creds, con_creds))
    try:
        r = ex.get("/api/expert-bench/cards")
        assert r.status_code == 200 and vid in [c["version_id"] for c in r.json()["cards"]]
        assert r.json()["plain"]["in_review"] == "等你看"
        assert adm.get("/api/expert-bench/cards").status_code == 403
        assert con.get("/api/expert-bench/cards").status_code == 403
        t = ex.get("/api/expert-bench/templates")
        assert t.status_code == 200 and "practice" in {x["key"] for x in t.json()}
        p = ex.post("/api/expert-bench/fill", json={"template": "practice", "fill": {"how": "按示范步骤交接"}})
        assert p.status_code == 200 and "按示范步骤交接" in p.json()["body"]
        assert con.post("/api/expert-bench/fill", json={"template": "practice", "fill": {}}).status_code == 403
        r = ex.post(f"/api/expert-bench/cards/{vid}/answer", json={"answers": [{"id": "keep", "choice": "yes"}]})
        assert r.status_code == 200 and r.json()["outcome"] == "approve"
        r = ex.post("/api/expert-bench/cards/999999/answer", json={"answers": [{"id": "keep", "choice": "yes"}]})
        assert r.status_code == 404
    finally:
        for c in (ex, adm, con):
            c.__exit__(None, None, None)
