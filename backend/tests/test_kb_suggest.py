"""四期 A32③：编辑器知识侧栏。按当前章节（交付件名 + 章节标题，可加顾问补充的词）复用知识检索
（app/kb/search.py，不另建索引）推荐相关条款（L4 依据）、公司做法、黄金样例，每条给出可一键插入的文字，
插入文字带出处（依据用【依据：…｜编号】，其余用 [编号]）；只读，不写知识库（红线 6）；非项目成员 404。"""
import secrets

import pytest
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Deliverable, KbItem, KbVersion, Project, User
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.seed import DELIVERABLES

from .conftest import create_user, logged_in_client

TAG = f"侧栏{secrets.token_hex(2)}"


def _publish(people: dict, title: str, body: str, *, layer: str = "L2", doc_type: str = "MTD", kind: str = "",
             ref: tuple | None = None) -> str:
    with SessionLocal() as db:
        u = {k: db.scalar(select(User).where(User.username == v)) for k, v in people.items()}
        content = VersionContent(title=f"{TAG}{title}", body=f"{body}{TAG}", source_type="public", source_detail="公开资料")
        if ref:
            content.ref_document, content.ref_version, content.ref_status = ref
        v = rv.new_item(db, u["author"], ItemMeta(layer=layer, domain="OPS", product="GEN", doc_type=doc_type,
                                                  stage="通用", capabilities=["运营"],
                                                  visible_roles=["consultant"]), content)
        rv.submit(db, u["author"], v)
        rv.review(db, u["expert"], v, "content", "approve")
        rv.review(db, u["admin"], v, "compliance", "approve")
        db.refresh(v)
        assert v.status == "published"
        if kind:                                   # 测试捷径：公司做法 / 黄金样例的发布守卫另有测试，这里只改标记
            v.item.knowledge_kind = kind
            if kind == "golden":
                v.item.doc_type = "GLD"
            db.commit()
        return v.item.doc_id


@pytest.fixture(scope="module")
def corpus():
    people = {k: create_user(r)[0] for k, r in {"author": ("knowledge_expert",), "expert": ("knowledge_expert",),
                                                "admin": ("knowledge_admin",)}.items()}
    return {
        "basis": _publish(people, "值班监测要求解读", "## 8.1.5 集中管控\n\n应对值班监测告警进行集中分析。",
                          layer="L4", doc_type="REF", ref=("GB/T 22239", "2019", "current")),
        "practice": _publish(people, "值班监测公司做法", "## 做法\n\n值班监测按三班轮换，交接核对未闭环事件。",
                             doc_type="MTD", kind="practice"),
        "golden": _publish(people, "值班监测黄金样例", "## 样例\n\n值班监测现状：白班 6 人，夜间托管初筛。",
                           layer="L5", doc_type="MTD", kind="golden"),
    }


@pytest.fixture()
def proj(corpus):
    code = f"KBS-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行")
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    user = create_user(("consultant",), {code: "member"})
    c = logged_in_client(app, user)
    assert c.put(f"/api/projects/{code}/deliverables/D4/outline", json={"titles": ["值班监测"]}).status_code == 200
    yield code, c, corpus
    c.__exit__(None, None, None)


def _counts() -> tuple[int, int]:
    with SessionLocal() as db:
        return db.scalar(select(func.count(KbItem.id))), db.scalar(select(func.count(KbVersion.id)))


def test_suggest_groups_with_sources_and_read_only(proj, monkeypatch):
    # 只验分组、出处与只读，不验排名：全量跑时别的测试留下的公司做法（如 test_expert_bench）可能排在前面
    monkeypatch.setattr("app.project.kb_suggest.PER_GROUP", 20)
    code, c, corpus = proj
    before = _counts()
    r = c.get(f"/api/projects/{code}/deliverables/D4/sections/1/kb-suggest", params={"q": TAG})
    assert r.status_code == 200, r.text
    out = r.json()
    by = {g: [x["doc_id"] for x in out[g]] for g in ("clauses", "practices", "golden")}
    assert corpus["basis"] in by["clauses"]
    assert corpus["practice"] in by["practices"] and corpus["practice"] not in by["clauses"]
    assert corpus["golden"] in by["golden"]
    clause = next(x for x in out["clauses"] if x["doc_id"] == corpus["basis"])
    assert clause["insert"].endswith(f"｜{corpus['basis']}】") and "GB/T 22239" in clause["insert"]
    gold = next(x for x in out["golden"] if x["doc_id"] == corpus["golden"])
    assert gold["insert"].endswith(f"[{corpus['golden']}]") and gold["title"].endswith("值班监测黄金样例")
    assert _counts() == before                                            # 交付侧不写知识库
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "kb.suggest")).all()[-1]
        assert TAG not in a.detail                                        # 审计不记检索词


def test_suggest_guard_and_missing_section(proj):
    code, c, _ = proj
    r = c.get(f"/api/projects/{code}/deliverables/D4/sections/1/kb-suggest", params={"q": "口令 Abc@12345"})
    assert r.status_code == 422
    assert c.get(f"/api/projects/{code}/deliverables/D4/sections/9/kb-suggest").status_code == 404


def test_non_member_404(proj):
    code, _, _ = proj
    outsider = logged_in_client(app, create_user(("consultant",)))
    try:
        assert outsider.get(f"/api/projects/{code}/deliverables/D4/sections/1/kb-suggest").status_code == 404
    finally:
        outsider.__exit__(None, None, None)
