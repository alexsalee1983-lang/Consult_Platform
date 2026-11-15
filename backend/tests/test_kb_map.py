"""知识地图按产品（二期 B6，app/kb/kbmap.py）：覆盖矩阵按产品配置和模块标签、缺口 TOP 按零命中排序、
贡献统计；零命中只按过滤条件和摘要哈希归类，返回体不含检索词。"""
from types import SimpleNamespace as NS

from sqlalchemy import select

from app.core import audit
from app.core.db import SessionLocal
from app.kb import kbmap
from app.main import app
from app.models import User

from .conftest import create_user, logged_in_client


def _live(*specs):
    return [(NS(product=p, stage=s, tags=list(t)), NS(created_by="", source_type="public", published_at=None))
            for p, s, t in specs]


def _cat():
    return NS(products={"SOC": NS(code="SOC", name="安全运营", status="ready"),
                        "ISO": NS(code="ISO", name="体系建设", status="draft")},
              modules={"DJBH": NS(code="DJBH", name="等保模块", status="ready", kb_tags=("等保", "DJBH"))})


def test_matrix_rows_products_modules_general(monkeypatch):
    monkeypatch.setattr(kbmap, "_catalog", _cat)
    live = _live(*[("SOC", "设计", ())] * 8, ("SOC", "调研", ("等保",)), ("GEN", "通用", ("DJBH",)),
                 ("ISO", "调研", ()))
    rows = {r["code"]: r for r in kbmap.coverage_rows(live)}
    assert [r["kind"] for r in kbmap.coverage_rows(live)] == ["product", "product", "general", "module"]
    assert rows["SOC"]["cells"]["设计"] == 8 and rows["SOC"]["total"] == 9 and rows["ISO"]["status"] == "draft"
    assert rows["GEN"]["cells"]["通用"] == 1
    assert rows["DJBH"]["cells"]["调研"] == 1 and rows["DJBH"]["cells"]["通用"] == 1 and rows["DJBH"]["total"] == 2
    gaps = kbmap.gaps(list(rows.values()), {("SOC", "验收"): 3})
    assert gaps[0]["product"] == "SOC" and gaps[0]["stage"] == "验收" and gaps[0]["zero_hits_30d"] == 3
    assert len(gaps) == kbmap.GAP_LIMIT and all(g["items"] < 8 and g["product"] != "GEN" for g in gaps)
    assert not any(g["product"] == "SOC" and g["stage"] == "设计" for g in gaps)       # 充足的格子不列
    assert gaps[1]["items"] == 0                                                       # 同为 0 次零命中时条目少的在前


def test_matrix_without_catalog(monkeypatch):
    monkeypatch.setattr(kbmap, "_catalog", lambda: None)
    rows = kbmap.coverage_rows(_live(("SOC", "设计", ())))
    assert [(r["code"], r["status"]) for r in rows] == [("SOC", "unconfigured"), ("GEN", "ready")]


def test_stats_api_gaps_contributors():
    expert = logged_in_client(app, create_user(("knowledge_expert",)))
    admin = logged_in_client(app, create_user(("knowledge_admin",)))
    consultant = logged_in_client(app, create_user(("consultant",)))
    try:
        body = {"meta": {"layer": "L2", "domain": "OPS", "product": "SOC", "doc_type": "TPL", "stage": "验收",
                         "capabilities": ["管理"]},
                "content": {"title": "地图测试-验收清单模板", "source_type": "public", "body": "## 清单\n\n验收要点。"}}
        v = expert.post("/api/kb/items", json=body).json()
        assert expert.post(f"/api/kb/versions/{v['id']}/submit").status_code == 200
        reviewer = logged_in_client(app, create_user(("knowledge_expert",)))
        try:
            assert reviewer.post(f"/api/kb/versions/{v['id']}/review",
                                 json={"role": "content", "decision": "approve"}).status_code == 200
        finally:
            reviewer.__exit__(None, None, None)
        assert admin.post(f"/api/kb/versions/{v['id']}/review",
                          json={"role": "compliance", "decision": "approve"}).status_code == 200
        before = expert.get("/api/kb/stats").json()
        q = "地图测试完全不会命中的词甲乙丙丁"
        with SessionLocal() as db:            # 与 search.py 写的审计格式相同；直接写，避免其他测试的条目让检索有命中
            for _ in range(2):
                audit.record(db, actor="map-test", action="kb.search", result="allowed", length=len(q),
                             query_sha=audit.digest(q), results=0, filters={"product": "SOC", "stage": "验收"})
        s = expert.get("/api/kb/stats").json()
        assert q not in str(s)
        soc = next(r for r in s["matrix"] if r["code"] == "SOC")
        assert soc["kind"] == "product" and soc["status"] == "ready" and soc["cells"]["验收"] >= 1
        assert any(r["code"] == "GEN" for r in s["matrix"]) and s["stages"][-1] == "通用"
        assert s["product_zero_hits_30d"].get("SOC", 0) >= before["product_zero_hits_30d"].get("SOC", 0) + 2
        assert s["repeated_zero_hit_questions_30d"] >= 1
        if soc["cells"]["验收"] < 8:
            cell = [g for g in s["gaps"] if (g["product"], g["stage"]) == ("SOC", "验收")]
            assert cell and cell[0]["zero_hits_30d"] >= 2 or len(s["gaps"]) == kbmap.GAP_LIMIT
        assert all(not c["username"].startswith("platform") for c in s["contributors"])
        assert len(s["contributors"]) <= kbmap.CONTRIB_LIMIT
    finally:
        for c in (expert, admin, consultant):
            c.__exit__(None, None, None)


def test_contributors_counts_and_names():
    from datetime import UTC, datetime, timedelta

    name, *_ = create_user(("knowledge_expert",))
    now = datetime.now(UTC)
    live = [(NS(), NS(created_by=name, source_type=src, published_at=at))
            for src, at in [("public", now)] * 20 + [("project", now - timedelta(days=60))]]
    with SessionLocal() as db:
        out = kbmap.contributors(db, live, now - timedelta(days=30))
        display = db.scalar(select(User.display_name).where(User.username == name))
    me = out[0]
    assert me["username"] == name and me["published"] == 21 and me["published_30d"] == 20
    assert me["by_source"] == {"public": 20, "project": 1} and me["display_name"] == (display or name)
