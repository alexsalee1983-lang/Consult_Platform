"""三期 B16：来源登记表（知识区 k0014）。生命周期、许可与权威等级必填、批准规则、采集机导出。"""
import json

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT, KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT
from app.main import app
from app.models import AuditLog, KbSource, KbSourceEvent

from .conftest import create_user, logged_in_client

GOOD = {"name": "测试来源：国家标准全文公开系统", "url": "https://openstd.example.org/b16-a",
        "publisher": "测试发布机关", "license": "公开发布，可内部引用并注明出处", "authority_kind": "国家标准"}


@pytest.fixture(scope="module")
def people():
    users = {"expert": create_user((KNOWLEDGE_EXPERT,)), "admin": create_user((KNOWLEDGE_ADMIN,)),
             "admin2": create_user((KNOWLEDGE_ADMIN,)), "consultant": create_user((CONSULTANT,))}
    made = {k: logged_in_client(app, u) for k, u in users.items()}
    made["_names"] = {k: u[0] for k, u in users.items()}
    yield made
    for k, c in made.items():
        if k != "_names":
            c.__exit__(None, None, None)


def _new(c, **kw):
    body = {**GOOD, **kw}
    return c.post("/api/kb/sources", json=body)


def test_create_requires_license_and_authority(people):
    c = people["expert"]
    assert _new(c, url="https://x.example.org/b16-1", license="").status_code == 422
    assert _new(c, url="https://x.example.org/b16-2", authority_kind="").status_code == 422
    assert _new(c, url="https://x.example.org/b16-3", authority_kind="论坛帖子").status_code == 422
    assert _new(c, url="ftp://x.example.org/b16-4").status_code == 422
    r = _new(c, url="https://x.example.org/b16-5")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "candidate" and out["authority_level"] == 2 and out["host"] == "x.example.org"
    assert out["code"].startswith("SR-")
    assert _new(c, url="https://x.example.org/b16-5").status_code == 409   # 同一地址不重复登记


def test_authority_levels_three_tiers(people):
    c = people["expert"]
    levels = {k: _new(c, url=f"https://lv.example.org/{i}", authority_kind=k).json()["authority_level"]
              for i, k in enumerate(("法律", "行政法规", "监管通报", "国家标准", "行业标准", "白皮书", "厂商资料"))}
    assert levels == {"法律": 1, "行政法规": 1, "监管通报": 1, "国家标准": 2, "行业标准": 2, "白皮书": 3, "厂商资料": 3}


def test_guard_blocks_secret_in_note(people):
    r = _new(people["expert"], url="https://g.example.org/1", note="登录口令 password=Zx9#kLm2Qw8!")
    assert r.status_code == 422


def test_lifecycle_and_approval(people):
    e, a, a2 = people["expert"], people["admin"], people["admin2"]
    code = _new(a, url="https://life.example.org/1").json()["code"]
    move = lambda c, to, reason="": c.post(f"/api/kb/sources/{code}/status", json={"to": to, "reason": reason})  # noqa: E731
    assert move(e, "active").status_code == 409            # 候选不能直接转正式
    assert move(e, "trial").status_code == 200
    assert move(e, "active").status_code == 403            # 转正式只限知识管理员
    assert move(a, "active").status_code == 403            # 登记人本人不能批准
    r = move(a2, "active")
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["approved_by"]
    assert move(a2, "demoted").status_code == 422           # 降级、停用必须写理由
    assert move(a2, "demoted", "近一年未更新").status_code == 200
    assert move(e, "retired", "发布机关撤站").status_code == 200
    assert move(e, "trial").status_code == 409              # 停用后只能重新登记为候选
    assert move(e, "candidate", "站点恢复").status_code == 200
    ev = e.get(f"/api/kb/sources/{code}/events").json()
    assert [x["to_status"] for x in ev] == ["candidate", "trial", "active", "demoted", "retired", "candidate"]
    with SessionLocal() as db:
        actions = set(db.scalars(select(AuditLog.action).where(AuditLog.action.like("kb.source%"))))
        assert {"kb.source.create", "kb.source.status"} <= actions
        detail = db.scalars(select(AuditLog.detail).where(AuditLog.action == "kb.source.status")).all()
        assert all("近一年未更新" not in d for d in detail)   # 审计只记理由长度


def test_update_fields_and_visibility(people):
    e = people["expert"]
    code = _new(e, url="https://upd.example.org/1").json()["code"]
    r = e.patch(f"/api/kb/sources/{code}", json={"license": "", "authority_kind": "法律"})
    assert r.status_code == 422
    r = e.patch(f"/api/kb/sources/{code}", json={"authority_kind": "法律", "note": "改为法律库"})
    assert r.status_code == 200 and r.json()["authority_level"] == 1
    assert people["consultant"].get("/api/kb/sources").status_code == 403
    rows = e.get("/api/kb/sources?status=candidate").json()
    assert code in {x["code"] for x in rows} and all(x["status"] == "candidate" for x in rows)
    assert e.get("/api/kb/sources/SR-999999/events").status_code == 404


def test_export_for_collector(people, capsys):
    e, a, a2 = people["expert"], people["admin"], people["admin2"]
    code = _new(e, url="https://exp.example.org/1").json()["code"]
    e.post(f"/api/kb/sources/{code}/status", json={"to": "trial"})
    a.post(f"/api/kb/sources/{code}/status", json={"to": "active"})
    cand = _new(a2, url="https://exp.example.org/2").json()["code"]
    assert cli_main(["kb-source-registry", "--json"]) == 0
    rows = {x["code"]: x for x in json.loads(capsys.readouterr().out)}
    assert rows[code]["status"] == "active" and rows[code]["license"] and rows[code]["authority_level"] == 2
    assert cand not in rows                                  # 候选不下发采集机
    with SessionLocal() as db:
        assert db.scalar(select(KbSource).where(KbSource.code == code)).approved_by
        assert db.scalars(select(KbSourceEvent).where(KbSourceEvent.source_id.is_not(None))).first()


def test_official_host_auto_activates(people):
    """四期 B43：官方域名白名单内、等级 1/2、许可写明的来源登记即自动转正式；改到白名单外退回试用。"""
    e = people["expert"]
    out = _new(e, url="https://auto.example.gov.cn/b43-1").json()
    assert out["status"] == "active" and out["approved_by"] == "platform:source-rules"
    ref = _new(e, url="https://auto.example.gov.cn/b43-2", authority_kind="白皮书").json()
    assert ref["status"] == "candidate"                      # 参考资料类照旧人工批准
    other = _new(e, url="https://auto.example.org/b43-3").json()
    assert other["status"] == "candidate"                    # 白名单外照旧
    r = e.patch(f"/api/kb/sources/{out['code']}", json={"url": "https://auto.example.org/b43-4"})
    assert r.status_code == 200 and r.json()["status"] == "trial"   # 改到白名单外退回试用
    ev = e.get(f"/api/kb/sources/{out['code']}/events").json()
    assert [x["to_status"] for x in ev if x["actor"] == "platform:source-rules"] == ["active", "trial"]
