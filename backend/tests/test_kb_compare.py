"""有无知识库对比评测（二期 B1 补充）：核对文件名和条款号、编造条号、达标门禁暂停自动发布。"""
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import autopublish, compare, importer, sources
from app.kb import review as rv
from app.models import AuditLog, KbItem, User
from app.services import gateway

from .conftest import create_user

LAW = "第一条 为了保障示范对比安全，制定本法。\n第二条 运营者应当留存网络日志不少于六个月。\n"


@pytest.fixture()
def lib():
    names = [create_user(r)[0] for r in (("knowledge_expert",), ("knowledge_expert",), ("knowledge_admin",))]
    with SessionLocal() as db:
        author, expert, admin = [db.scalar(select(User).where(User.username == n)) for n in names]
        title = f"示范对比法{uuid.uuid4().hex[:6]}"
        entries = sources.split_doc(sources.Doc(file="c.txt", title=title, code="主席令第8号", version="2029"),
                                    LAW.encode())[0]
        for e in entries:
            importer.import_entry(db, author, e)
            v = db.scalar(select(KbItem).where(KbItem.import_key == e.key)).versions[-1]
            rv.submit(db, author, v)
            rv.review(db, expert, v, "content", "approve")
            rv.review(db, admin, v, "compliance", "approve")
    yield expert.username, title
    with SessionLocal() as db:
        for e in entries:
            db.scalar(select(KbItem).where(KbItem.import_key == e.key)).retired = True
        db.commit()


@pytest.fixture()
def model(monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_sample_fail_max", 1.0)       # 其他用例留下的抽检结果不影响

    def use(bare: str, with_kb: str) -> list:
        seen: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "deepseek-v4.1-flash"}]})
            msgs = json.loads(request.content)["messages"]
            seen.append(msgs)
            text = with_kb if "只能根据" in msgs[0]["content"] else bare
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {}})
        monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
        gateway.set_transport(httpx.MockTransport(handler))
        return seen
    yield use
    gateway.set_transport(None)


def _qs(title: str) -> list[dict]:
    return [{"id": "C1", "category": "clause", "question": "网络日志要留存多久？", "expect_refs": [f"《{title}》第二条"]},
            {"id": "C2", "question": "只在普通评测里用的题", "expect": []}]


def test_compare_grades_and_gates(lib, model):
    username, title = lib
    seen = model(f"依据《{title}》第九十九条，至少留存一年。", f"依据《{title}》第二条，不少于六个月。")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        r = compare.run(db, user, _qs(title))
        assert r.total == 1 and r.passed, r.reasons                            # 不带 expect_refs 的题不参与
        assert (r.no_kb["accuracy"], r.no_kb["fabricated"]) == (0.0, 1)
        assert (r.with_kb["accuracy"], r.with_kb["fabricated"]) == (1.0, 0)
        assert r.results[0].no_kb.fabricated == [f"{title} 第九十九条"]
        assert r.by_category["clause"]["with_kb"]["accuracy"] == 1.0
        assert "知识库条目" in seen[1][-1]["content"] and "知识库条目" not in seen[0][-1]["content"]
        log = db.scalar(select(AuditLog).where(AuditLog.action == "kb.eval_compare").order_by(AuditLog.id.desc()))
        d = json.loads(log.detail)
        assert d["passed"] is True and d["with_kb_accuracy"] == 1.0 and "留存" not in log.detail
        assert compare.gate_reason(db) == "" and autopublish.suspended(db) == ""
        assert "达标" in compare.format_report(r)

    model(f"依据《{title}》第二条。", f"依据《{title}》第二条，另见《{title}》第九十九条。")    # 用知识库仍编造：未达标
    with SessionLocal() as db:
        user = db.merge(user)
        r = compare.run(db, user, _qs(title))
        assert not r.passed and r.with_kb["fabricated"] == 1 and "编造条号 1 次" in "；".join(r.reasons)
        assert "对比评测未达标" in autopublish.suspended(db) and "对比评测" in autopublish.suspended(db, "agent")
        get_settings().kb_eval_gate = False
        try:
            assert compare.gate_reason(db) == ""
        finally:
            get_settings().kb_eval_gate = True

    model(f"依据《{title}》第二条。", f"依据《{title}》第二条。")                 # 再跑一次达标后恢复
    with SessionLocal() as db:
        assert compare.run(db, db.merge(user), _qs(title)).passed
        assert autopublish.suspended(db) == ""


def test_lower_than_bare_and_outside(lib, model):
    username, title = lib
    model(f"依据《{title}》第二条。", "依据《外部示范规定》第三条，依据待核实。")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        r = compare.run(db, user, _qs(title))
        assert not r.passed and r.with_kb["outside"] == 1 and r.with_kb["fabricated"] == 0
        assert any("低于不用知识库" in x for x in r.reasons)
    model(f"依据《{title}》第二条。", f"依据《{title}》第二条。")
    with SessionLocal() as db:
        assert compare.run(db, db.merge(user), _qs(title)).passed               # 不留下未达标记录影响其他用例


def test_demo_and_bad_questions(lib):
    username, title = lib
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        n = db.query(AuditLog).filter(AuditLog.action == "kb.eval_compare").count()
        r = compare.run(db, user, _qs(title))
        assert r.demo and not r.passed and "演示模式" in r.reasons[-1]
        assert db.query(AuditLog).filter(AuditLog.action == "kb.eval_compare").count() == n
    for bad in ({"id": "X", "question": "q", "expect_refs": []},
                {"id": "X", "question": "q", "expect_refs": ["没有条款号的写法"]},
                {"id": "X", "question": "q", "expect_refs": ["《示范法》第一条"], "category": "other"}):
        with pytest.raises(compare.CompareError):
            compare.select_questions([bad])
    with pytest.raises(compare.CompareError, match="没有带 expect_refs"):
        compare.select_questions([{"id": "Y", "question": "q"}])
