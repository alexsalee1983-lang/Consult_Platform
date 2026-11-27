"""四期 B44：知识专家便利性（平台规划刷新设计第十三节第 1～4 项；在 B23 专家工作台上扩展）。

- 对照审核：条目与所引依据原文并排，出入处标出；知识审核 Agent 意见并列，存疑处标出；
- 修改后认可：专家在线改完即认可，改动另建一版（专家版本），原 AI 稿留作历史，认可人署名；
- 分组、按影响排序、成批认可：按产品与章节分组，按“认可后知识健康提升”排序，同组一次认可（有存疑的跳过）；
- 按专长派卡：专家档案（擅长产品、行业），按专长派、按工作量均衡，没有匹配的发全体专家；
- 非知识专家不能认可；审计不记正文。
"""
import json
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.db import SessionLocal
from app.kb import expert_ux as ux
from app.kb import importer, sources
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent, create_item
from app.main import app
from app.models import AuditLog, KbExpertProfile, KbItem, KbReview, KbReviewAssignment, KbVersion, User
from app.project import my_tasks as mt

from .conftest import create_user, logged_in_client

LAW_TITLE = "中华人民共和国示范对照法"
LAW = "第一条 为了保障示范对照安全，制定本法。\n第二条 运营者应当每年至少开展一次应急演练并留存记录。\n"


def _user(*roles: str) -> tuple[User, tuple]:
    creds = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == creds[0])), creds


def _version(author: User, body: str, product: str = "SOC", kind: str = "golden", slot: str = "",
             agent: dict | None = None, industries: list[str] | None = None, key: str = "") -> int:
    with SessionLocal() as db:
        author = db.merge(author)
        m = ItemMeta(layer="L2", domain="OPS", product=product, doc_type="GLD" if kind == "golden" else "TPL",
                     stage="设计", capabilities=["运营"], knowledge_kind=kind, slot=slot,
                     industries=industries or ["通用"])
        c = VersionContent(title=f"示范样例{uuid.uuid4().hex[:6]}", body=body, source_type="expert",
                           source_detail="AI 基线：示范")
        v = create_item(db, m, c, created_by=author.username)
        v.deid_status = "virtual"                    # AI 基线草稿（虚拟数据，非生产）
        if key:
            v.item.import_key = key
        db.commit()
        rv.submit(db, author, v)
        db.add(KbReview(version_id=v.id, reviewer="agent:kb-reviewer", review_role="agent",
                        decision=(agent or {}).pop("decision", "approve"), comment="依据：示范",
                        findings=agent or {}))
        db.commit()
        return v.id


@pytest.fixture()
def law():
    people = [create_user(r)[0] for r in (("knowledge_expert",), ("knowledge_expert",), ("knowledge_admin",))]
    doc = sources.Doc(file="law.txt", title=LAW_TITLE, code="主席令第9号", version="2029")
    entries = sources.split_doc(doc, LAW.encode())[0]
    with SessionLocal() as db:
        author, expert, admin = (db.scalar(select(User).where(User.username == n)) for n in people)
        for e in entries:
            r = importer.import_entry(db, author, e)
            if r.action == "unchanged":
                db.scalar(select(KbItem).where(KbItem.import_key == e.key)).retired = False
                db.commit()
                continue
            v = db.scalar(select(KbItem).where(KbItem.import_key == e.key)).versions[-1]
            rv.submit(db, author, v)
            rv.review(db, expert, v, "content", "approve")
            rv.review(db, admin, v, "compliance", "approve")
    yield
    with SessionLocal() as db:
        for e in entries:
            item = db.scalar(select(KbItem).where(KbItem.import_key == e.key))
            if item is not None:
                item.retired = True
        db.commit()


@pytest.fixture()
def profiles():
    made: list[str] = []
    yield made
    with SessionLocal() as db:                      # 档案只在本用例有效，免得把其他用例的待审卡派走
        db.execute(delete(KbExpertProfile).where(KbExpertProfile.username.in_(made)))
        db.execute(delete(KbReviewAssignment).where(KbReviewAssignment.assignee.in_(made)))
        db.commit()


# ---- 1. 对照审核 ----

def test_compare_side_by_side_highlights_and_flags(law):
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    body = ("## 演练\n\n依照《示范对照法》第二条，运营者应当每两年开展一次应急演练。\n\n"
            "## 适用\n\n另见《示范对照法》第九条。")
    vid = _version(author, body, agent={"issues": ["演练频次与依据不符"], "uncertain_fields": ["applicability"],
                                        "confidence": 0.5})
    with SessionLocal() as db:
        out = ux.compare(db, db.merge(expert), vid)
    c0, c1 = out["citations"]
    assert c0["found"] and "每年至少开展一次应急演练" in c0["source"]
    assert "每两年" in c0["entry"]
    assert any(not s["same"] for s in c0["entry_segments"]) and any(s["same"] for s in c0["entry_segments"])
    assert "".join(s["text"] for s in c0["source_segments"]) == c0["source"]
    assert any("两" in s["text"] for s in c0["entry_segments"] if not s["same"])     # 出入处高亮
    assert not c1["found"] and c1["flag"]                                             # 依据里找不到 → 标出
    assert out["agent"]["issues"] == ["演练频次与依据不符"]
    wheres = {f["where"] for f in out["flags"]}
    assert {"citation:1", "agent:issue:0", "field:applicability"} <= wheres
    assert out["flagged"] is True


def test_compare_only_for_experts():
    author, _ = _user("knowledge_expert")
    admin, _ = _user("knowledge_admin")
    vid = _version(author, "## 做法\n\n示范。")
    with SessionLocal() as db, pytest.raises(rv.ReviewError) as e:
        ux.compare(db, db.merge(admin), vid)
    assert e.value.status == 403


# ---- 2. 修改后认可 ----

def test_revise_creates_signed_expert_version_and_keeps_ai_draft():
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    old_body = "## 做法\n\n每两年演练一次。"
    vid = _version(author, old_body)
    with SessionLocal() as db:
        out = ux.revise(db, db.merge(expert), vid, "## 做法\n\n每年至少演练一次并留存记录。", comment="频次改正",
                        elapsed_ms=60_000)
        old = db.get(KbVersion, vid)
        new = db.get(KbVersion, out["version_id"])
        assert old.status == "superseded" and old.body == old_body                 # AI 原稿留作历史
        assert new.status == "published" and new.version == old.version + 1
        assert new.item.current_version_id == new.id and "每年至少" in new.body
        assert expert.username in new.change_note and "专家修订" in new.change_note
        r = next(r for r in new.reviews if r.review_role == "content")
        assert r.reviewer == expert.username and r.decision == "approve"
        assert r.findings["expert_revision"] is True and r.findings["revised_from"] == old.version
        assert r.findings["edited"] is True
        assert out["approved_by"] == expert.username and out["expert_revision"] is True
        assert new.item.owner in (expert.username, author.username) or new.item.owner
        logs = db.scalars(select(AuditLog).where(AuditLog.action == "kb.expert_revise",
                                                 AuditLog.actor == expert.username)).all()
        assert logs and "每年至少" not in str(logs[-1].detail)                     # 审计不记正文


def test_revise_rules():
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    consultant, _ = _user("consultant")
    vid = _version(author, "## 做法\n\n示范。")
    with SessionLocal() as db:
        for who, body, status in ((author, "## 做法\n\n改了。", 403), (consultant, "## 做法\n\n改了。", 403),
                                  (expert, "## 做法\n\n示范。", 422), (expert, "## 做法\n\n口令：P@ssw0rd!2026", 422)):
            with pytest.raises(rv.ReviewError) as e:
                ux.revise(db, db.merge(who), vid, body)
            assert e.value.status == status, (who.username, body)
        assert db.get(KbVersion, vid).status == "in_review"


# ---- 3. 分组、按影响排序、成批认可 ----

def _evalset(tmp_path, keys: list[str]) -> list:
    p = tmp_path / "es.yaml"
    p.write_text("product: IRE\nchapters:\n  - id: IRE-T-01\n    step: facts\n    title: 示范章\n    query: 示范\n"
                 f"    expect: [{', '.join(keys)}]\n", encoding="utf-8")
    return [p]


def test_groups_sorted_by_health_gain_and_batch_approve(tmp_path):
    author, _ = _user("knowledge_expert")
    expert, _ = _user("knowledge_expert")
    tag = uuid.uuid4().hex[:6]
    slot = f"IRE.T{tag}"
    keys = [f"b44-{tag}-{i}" for i in range(3)]
    paths = _evalset(tmp_path, keys)
    hi = _version(author, "## 做法\n\n示范甲。", product="IRE", slot=slot, key=keys[0])
    lo = _version(author, "## 做法\n\n示范乙。", product="IRE", slot=slot)
    flagged = _version(author, "## 做法\n\n示范丙。", product="IRE", slot=slot, key=keys[1],
                       agent={"decision": "flag", "issues": ["存疑"]})
    other = _version(author, "## 做法\n\n示范丁。", product="DSM", slot=f"DSM.T{tag}")
    with SessionLocal() as db:
        g = ux.groups(db, db.merge(expert), paths=paths)
        grp = next(x for x in g["groups"] if x["slot"] == slot)
        assert grp["product"] == "IRE" and grp["label"].startswith("事件后")
        ids = [i["version_id"] for i in grp["items"]]
        assert set(ids) == {hi, lo, flagged} and ids.index(hi) < ids.index(lo)
        assert grp["items"][ids.index(hi)]["gain"] > 0 and grp["items"][ids.index(lo)]["gain"] == 0
        assert grp["items"][ids.index(flagged)]["flagged"] is True
        with SessionLocal() as db2, pytest.raises(rv.ReviewError) as e:      # 不同组不能一起认可
            ux.batch_approve(db2, db2.merge(expert), [hi, other], paths=paths)
        assert e.value.status == 422
        out = ux.batch_approve(db, db.merge(expert), [hi, lo, flagged], comment="成批", paths=paths)
    assert sorted(out["approved"]) == sorted([hi, lo]) and [s["version_id"] for s in out["skipped"]] == [flagged]
    with SessionLocal() as db:
        assert db.get(KbVersion, hi).status == "published" and db.get(KbVersion, lo).status == "published"
        assert db.get(KbVersion, flagged).status == "in_review"
        log = db.scalars(select(AuditLog).where(AuditLog.action == "kb.expert_batch_approve",
                                                AuditLog.actor == expert.username)).all()[-1]
        detail = json.loads(log.detail)
        assert detail["approved"] == 2 and detail["skipped"] == 1
        per = db.scalars(select(AuditLog).where(AuditLog.action == "kb.review", AuditLog.actor == expert.username,
                                                AuditLog.result == "allowed")).all()
        assert len(per) >= 2                                                 # 逐条仍走 review.review 留审计


def test_batch_approve_needs_expert_and_not_author():
    author, _ = _user("knowledge_expert")
    admin, _ = _user("knowledge_admin")
    vid = _version(author, "## 做法\n\n示范。", product="IRE", slot=f"IRE.X{uuid.uuid4().hex[:6]}")
    with SessionLocal() as db:
        with pytest.raises(rv.ReviewError) as e:
            ux.batch_approve(db, db.merge(admin), [vid])
        assert e.value.status == 403
        with pytest.raises(rv.ReviewError) as e:
            ux.batch_approve(db, db.merge(author), [vid])                 # 作者看不到自己的卡
        assert e.value.status == 404
        assert db.get(KbVersion, vid).status == "in_review"


# ---- 4. 按专长派卡 ----

def test_assign_by_expertise_and_balance_load(profiles):
    author, _ = _user("knowledge_expert")
    a, _ = _user("knowledge_expert")
    b, _ = _user("knowledge_expert")
    c, _ = _user("knowledge_expert")
    profiles += [a.username, b.username, c.username]
    with SessionLocal() as db:
        ux.set_profile(db, db.merge(a), ["NSG"], ["电力"])
        ux.set_profile(db, db.merge(b), ["NSG"], [])
        ux.set_profile(db, db.merge(c), ["ISO"], [])
    power = _version(author, "## 做法\n\n电力示范。", product="NSG", industries=["电力"])
    plain = [_version(author, f"## 做法\n\n示范{i}。", product="NSG") for i in range(4)]
    nomatch = _version(author, "## 做法\n\n通用示范。", product="GEN")
    with SessionLocal() as db:
        amap = ux.assignee_map(db)
        assert amap[power] == a.username                               # 行业也对上的优先
        load = ux.loads(db)
        assert abs(load[a.username] - load[b.username]) <= 1           # 工作量均衡
        assert {amap[v] for v in plain} == {a.username, b.username}
        assert nomatch not in amap                                     # 没有匹配的发全体
        visible = {u: {t.ref_id for t in mt.collect(db, db.merge(u)) if t.kind == "kb_content"} for u in (a, b, c)}
    owner = amap[plain[0]]
    others = [u for u in (a, b, c) if u.username != owner]
    assert plain[0] in visible[next(u for u in (a, b) if u.username == owner)]
    assert all(plain[0] not in visible[u] for u in others)             # 派给别人的不在我的任务里
    assert all(nomatch in visible[u] for u in (a, b, c))
    with SessionLocal() as db:
        db.execute(delete(KbExpertProfile).where(KbExpertProfile.username == owner))   # 档案撤了 → 收回重派
        db.commit()
        assert ux.assignee_map(db).get(plain[0]) != owner


def test_profile_validation_and_role(profiles):
    expert, _ = _user("knowledge_expert")
    consultant, _ = _user("consultant")
    profiles.append(expert.username)
    with SessionLocal() as db:
        with pytest.raises(rv.ReviewError) as e:
            ux.set_profile(db, db.merge(expert), ["XXX"], [])
        assert e.value.status == 422
        with pytest.raises(rv.ReviewError) as e:
            ux.set_profile(db, db.merge(consultant), ["SOC"], [])
        assert e.value.status == 403
        p = ux.set_profile(db, db.merge(expert), ["SOC", "ISO"], ["银行"])
        assert p == {"username": expert.username, "products": ["SOC", "ISO"], "industries": ["银行"]}


# ---- 接口 ----

def test_api_endpoints(profiles):
    author, _ = _user("knowledge_expert")
    expert, creds = _user("knowledge_expert")
    _, ccreds = _user("consultant")
    profiles.append(expert.username)
    vid = _version(author, "## 做法\n\n示范接口。", product="IRE", slot=f"IRE.A{uuid.uuid4().hex[:6]}")
    client, con = logged_in_client(app, creds), logged_in_client(app, ccreds)
    try:
        assert client.get(f"/api/expert-bench/compare/{vid}").status_code == 200
        assert client.get("/api/expert-bench/groups").status_code == 200
        r = client.put("/api/expert-bench/profile", json={"products": ["IRE"], "industries": []})
        assert r.status_code == 200, r.text
        assert client.get("/api/expert-bench/profile").json()["mine"]["products"] == ["IRE"]
        r = client.post(f"/api/expert-bench/cards/{vid}/revise", json={"body": "## 做法\n\n示范接口（专家改）。"})
        assert r.status_code == 200, r.text
        assert r.json()["approved_by"] == expert.username
        r = client.post("/api/expert-bench/batch-approve", json={"version_ids": [vid]})
        assert r.status_code == 404                                    # 已认可，不在卡里
        assert con.post("/api/expert-bench/batch-approve", json={"version_ids": [vid]}).status_code == 403
        assert con.get(f"/api/expert-bench/compare/{vid}").status_code == 403
    finally:
        for c in (client, con):
            c.__exit__(None, None, None)
