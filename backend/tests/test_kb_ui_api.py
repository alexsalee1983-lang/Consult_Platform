"""知识库界面用的接口：取值表、版本列表、知识地图统计。"""
from app.main import app

from .conftest import create_user, logged_in_client


def test_meta_options_for_forms():
    c = logged_in_client(app, create_user(("consultant",)))
    try:
        m = c.get("/api/kb/meta").json()
        assert m["layers"]["L4"] == "依据" and "SOC" in m["products"] and "设计" in m["stages"]
        assert m["source_types"]["public"] == "公开资料" and m["doc_types"]["MTD"] == "方法说明"
    finally:
        c.__exit__(None, None, None)


def test_versions_list_and_stats():
    expert = logged_in_client(app, create_user(("knowledge_expert",)))
    consultant = logged_in_client(app, create_user(("consultant",)))
    try:
        body = {"meta": {"layer": "L2", "domain": "OPS", "product": "SOC", "doc_type": "TPL", "stage": "调研",
                         "capabilities": ["管理"]},
                "content": {"title": "界面测试-访谈提纲模板", "source_type": "public", "body": "## 提纲\n\n访谈要点。"}}
        v = expert.post("/api/kb/items", json=body).json()
        mine = expert.get("/api/kb/versions", params={"status": "draft", "mine": True}).json()
        assert v["id"] in [x["id"] for x in mine] and all(x["status"] == "draft" for x in mine)
        assert consultant.get("/api/kb/versions").status_code == 403
        s = expert.get("/api/kb/stats").json()
        assert s["drafts"] >= 1 and isinstance(s["coverage"], dict) and "zero_hit_searches_30d" in s
        consultant.get("/api/kb/search", params={"q": "完全不会命中的词甲乙丙"})
        assert expert.get("/api/kb/stats").json()["zero_hit_searches_30d"] >= 1
        assert consultant.get("/api/kb/stats").status_code == 403
    finally:
        expert.__exit__(None, None, None)
        consultant.__exit__(None, None, None)
