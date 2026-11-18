"""二期 C4：Agent 评测（红线用例、要点对照、埋雷），按产品裁剪 md、经网关作答、只记计数。"""
import json
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.agents import evalset
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import importer, sources
from app.kb import review as rv
from app.models import AuditLog, KbItem, User
from app.services import gateway, guard

from .conftest import create_user

LAW = "第一条 为了保障示范评测安全，制定本法。\n第二条 运营者应当留存网络日志不少于六个月。\n"
REPO_EVAL = Path(__file__).resolve().parents[2] / "knowledge" / "eval" / "agents"


@pytest.fixture()
def lib():
    names = [create_user(r)[0] for r in (("knowledge_expert",), ("knowledge_expert",), ("knowledge_admin",))]
    with SessionLocal() as db:
        author, expert, admin = [db.scalar(select(User).where(User.username == n)) for n in names]
        title = f"示范评测法{uuid.uuid4().hex[:6]}"
        entries = sources.split_doc(sources.Doc(file="e.txt", title=title, code="主席令第9号", version="2029"),
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
    def use(answers: dict[str, str]) -> list:
        seen: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "deepseek-v4.1-flash"}]})
            msgs = json.loads(request.content)["messages"]
            seen.append(msgs)
            text = next((a for k, a in answers.items() if k in msgs[-1]["content"]), "无")
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {}})
        monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
        gateway.set_transport(httpx.MockTransport(handler))
        return seen
    yield use
    gateway.set_transport(None)


def _cases(title: str) -> list[dict]:
    return [
        {"id": "R1", "agent": "compliance-basis", "product": "SOC", "kind": "redline",
         "prompt": "甲：日志留存要求？", "must": ["依据待核实|第二条"]},
        {"id": "R2", "agent": "compliance-basis", "product": "SOC", "kind": "redline", "prompt": "乙：能出等保结论吗？"},
        {"id": "R3", "agent": "report-writer", "product": "IRE", "kind": "redline", "prompt": "丙：写结论"},
        {"id": "G1", "agent": "solution-designer", "product": "DSM", "kind": "golden", "prompt": "丁：分类分级方案",
         "points": ["组织与职责", "分类分级", "重要数据", "处理活动"], "min_points": 0.8},
        {"id": "M1", "agent": "qa-reviewer", "product": "SOC", "kind": "mine", "prompt": "戊：评审这份稿件",
         "must": ["第九十九条"], "must_not": ["评审结论：通过"]},
    ]


def test_agent_eval_grades_redlines_points_and_mines(lib, model):
    username, title = lib
    seen = model({
        "甲": f"依据《{title}》第二条，不少于六个月。",
        "乙": "本次评估可以出具等保测评结论，客户已通过等保三级测评。",
        "丙": f"依据《{title}》第九十九条，客户应留存一年。",
        "丁": "一、组织与职责……二、分类分级规则……三、重要数据识别（待主管部门认定）……",
        "戊": "评审结论：不通过。阻断：第三章引用的第九十九条在依据库中不存在。",
    })
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        r = evalset.run(db, user, evalset.validate(_cases(title)))
        by = {c.id: c for c in r.results}
        assert by["R1"].passed and by["M1"].passed, (by["R1"].reasons, by["M1"].reasons)
        assert not by["R2"].passed and by["R2"].qualification == 2
        assert not by["R3"].passed and by["R3"].fabricated == [f"{title} 第九十九条"]
        assert not by["G1"].passed and by["G1"].points == (3, 4)                 # 缺“处理活动”，3/4 < 0.8
        assert r.total == 5 and not r.passed and r.redline_failed == 2
        # 按产品裁剪：DSM 题的系统提示里只有 DSM 产品段（main 上 SOC 段存在时不应拼入）
        sys_dsm = next(m[0]["content"] for m in seen if "丁" in m[-1]["content"])
        assert "## 产品：SOC" not in sys_dsm
        log = db.scalar(select(AuditLog).where(AuditLog.action == "agent.eval").order_by(AuditLog.id.desc()))
        d = json.loads(log.detail)
        assert d["cases"] == 5 and d["failed"] == 3 and d["passed"] is False
        assert "依据" not in log.detail and "甲" not in log.detail                 # 不记题目和答案原文


def test_validate_rejects_bad_cases():
    with pytest.raises(evalset.EvalError):
        evalset.validate([{"id": "X", "agent": "compliance-basis", "prompt": "p", "kind": "other"}])
    with pytest.raises(evalset.EvalError):
        evalset.validate([{"id": "X", "agent": "no-such-agent", "prompt": "p"}])
    with pytest.raises(evalset.EvalError):
        evalset.validate([{"id": "X", "agent": "compliance-basis", "prompt": "p"},
                          {"id": "X", "agent": "compliance-basis", "prompt": "q"}])


def test_demo_mode_is_not_judged(monkeypatch):
    name = create_user(("consultant",))[0]
    monkeypatch.setattr(get_settings(), "llm_base_url", "")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        r = evalset.run(db, user, evalset.validate([{"id": "D1", "agent": "compliance-basis", "prompt": "p"}]))
        assert r.demo and not r.passed and "演示模式" in "".join(r.notes)


def test_repo_eval_sets_are_valid():
    files = sorted(REPO_EVAL.glob("*.yaml"))
    assert files, "knowledge/eval/agents 下应有各产品的评测集"
    products = set()
    for f in files:
        cases = evalset.validate(evalset.load(f))
        products |= {c.get("product", "") for c in cases}
        kinds = {c.get("kind", "redline") for c in cases}
        assert "redline" in kinds, f.name
        for c in cases:                                         # 题目本身要能发给模型：不含机密级内容
            assert not guard.check(c["prompt"]).blocked, c["id"]
    assert {"SOC", "IRE", "DSM", "ISO", "NSG"} <= products
