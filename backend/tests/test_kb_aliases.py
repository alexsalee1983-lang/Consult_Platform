"""知识条目别名表（三期 B15 第二步，知识区迁移 k0010，app/kb/refs.py）：
旧编号 / 跨环境编号 / 合并前编号登记为别名，doc_id 查不到的按别名解析（via=alias），下线条目照样解析并标 retired；
别名与 kb_items.doc_id 互斥、不重复、永不删除；交付件来源、知识更新提醒、签署门禁、使用反馈、条目接口经别名都能找到条目。"""
import itertools
import secrets
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select

from app.core.db import SessionLocal, utcnow
from app.core.migrate import alembic_config, upgrade_to_head
from app.kb import meta, recheck, refs
from app.main import app
from app.models import Deliverable, KbItem, KbItemAlias, Project, User
from app.project import deliverables as dl
from app.project import kb_impact, quality
from app.seed import DELIVERABLES

from .conftest import create_user, logged_in_client
from .test_kb_refs import _publish

_seq = itertools.count(secrets.randbelow(3000) + 80000)


def _alias() -> str:
    """一个没人用的旧格式编号（专用前缀，不影响其他条目的编号顺延）。"""
    return f"L5-ALI-SOC-OLD-{next(_seq)}"


def _item_id(doc_id: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(KbItem.id).where(KbItem.doc_id == doc_id))


def _add(alias: str, doc_id: str, kind: str = "legacy") -> None:
    with SessionLocal() as db:
        refs.add_alias(db, alias, _item_id(doc_id), kind)
        db.commit()


# ---- 迁移 ----

def test_k0010_round_trip(tmp_path: Path):
    main = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    kb = create_engine(f"sqlite:///{tmp_path / 'kb.db'}")
    upgrade_to_head(main, kb)
    assert "kb_item_aliases" in inspect(kb).get_table_names()
    cols = {c["name"]: c for c in inspect(kb).get_columns("kb_item_aliases")}
    assert set(cols) == {"alias", "item_id", "kind", "created_at"}
    assert inspect(kb).get_pk_constraint("kb_item_aliases")["constrained_columns"] == ["alias"]
    with kb.begin() as conn:
        cfg = alembic_config(conn, "kb")
        command.downgrade(cfg, "k0009_kb_upload_project")
        assert "kb_item_aliases" not in inspect(conn).get_table_names()
        command.upgrade(cfg, "head")
    assert "kb_item_aliases" in inspect(kb).get_table_names()
    main.dispose()
    kb.dispose()


# ---- 解析与写入 ----

def test_resolve_via_alias_and_canonical():
    live = _publish(f"别名测试-现行{secrets.token_hex(2)}")
    old = _alias()
    _add(old, live)
    with SessionLocal() as db:
        r = refs.resolve(db, [old, live, "L2-OPS-SOC-TPL-99997"])
        assert set(r) == {old, live}
        assert r[old].via == "alias" and r[old].doc_id == live and r[old].item_id == r[live].item_id
        assert r[live].via == "doc_id" and r[old].usable
        assert refs.canonical(db, [old, live]) == {old: live, live: live}
        assert refs.aliases_of(db, r[live].item_id) == [old]


def test_retired_item_resolves_via_alias_as_retired():
    gone = _publish("别名测试-下线", retire=True)
    old = _alias()
    _add(old, gone)
    with SessionLocal() as db:
        r = refs.resolve(db, [old])[old]
    assert r.via == "alias" and r.retired and not r.usable and r.doc_id == gone


def test_alias_batches_over_500():
    live = _publish("别名测试-批量")
    old = _alias()
    _add(old, live)
    with SessionLocal() as db:
        r = refs.resolve(db, [f"L2-OPS-SOC-TPL-{n:05d}" for n in range(60000, 61200)] + [old])
    assert set(r) == {old}


def test_add_alias_rejects_doc_id_duplicate_and_bad_input():
    a, b = _publish("别名测试-互斥一"), _publish("别名测试-互斥二")
    old = _alias()
    _add(old, a)
    with SessionLocal() as db:
        ia, ib = _item_id(a), _item_id(b)
        for alias, item, kind in ((b, ia, "merge"),           # 与现行编号冲突
                                  (old, ib, "transfer"),      # 别名重复
                                  (old, ia, "legacy"),        # 同一条重复登记
                                  (_alias(), ia, "rename"),   # kind 不认识
                                  ("随便写的", ia, "legacy"),  # 不是编号格式
                                  (_alias(), 99_999_999, "legacy")):   # 条目不存在
            with pytest.raises(refs.AliasError):
                refs.add_alias(db, alias, item, kind)
        assert db.scalar(select(KbItemAlias).where(KbItemAlias.alias == old)).item_id == ia
    assert not hasattr(refs, "remove_alias") and not hasattr(refs, "delete_alias")   # 别名永不删除


def test_new_doc_id_skips_alias_numbers():
    """互斥的另一面：新条目生成编号时不占用已登记为别名的旧编号。"""
    first = _publish("别名测试-编号顺延")
    seq = int(first[3:])
    taken = meta.format_doc_id(seq + 1)
    _add(taken, first)
    nxt = _publish("别名测试-编号顺延二")
    assert nxt != taken and int(nxt[3:]) > seq + 1


# ---- 使用点 ----

@pytest.fixture(scope="module")
def cited():
    """BANK-A 虚拟项目一章引用两条：一条按别名（现行）、一条按别名（已下线）。"""
    live, gone = _publish(f"别名测试-引用现行{secrets.token_hex(2)}"), _publish("别名测试-引用下线", retire=True)
    a_live, a_gone = _alias(), _alias()
    _add(a_live, live)
    _add(a_gone, gone)
    code = f"BANK-A-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    member, *_ = create_user(("consultant",), {code: "member"})
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        u = db.scalar(select(User).where(User.username == member))
        d = next(x for x in p.deliverables if x.code == "D4")
        dl.set_outline(db, u, p, d, ["现状"])
        v = dl.write_section(db, u, p, d, 1, f"夜班交接 [{a_live}]，旧做法 [{a_gone}]。")
        sources = v.version.sources
    return {"code": code, "member": member, "live": live, "gone": gone, "a_live": a_live, "a_gone": a_gone,
            "sources": sources}


def test_sources_record_current_doc_id(cited):
    k = {x["ref"]: x for x in cited["sources"]["knowledge"]}
    assert k[cited["a_live"]]["found"] is True and k[cited["a_live"]]["doc_id"] == cited["live"]
    assert k[cited["a_gone"]]["found"] is False and k[cited["a_gone"]]["doc_id"] == cited["gone"]
    assert cited["a_live"] not in cited["sources"]["unresolved"]


def test_kb_impact_and_usage_via_alias(cited):
    with SessionLocal() as db:
        u = kb_impact.usage_counts(db, cited["live"])
        p = db.scalar(select(Project).where(Project.code == cited["code"]))
        d = next(x for x in p.deliverables if x.code == "D4")
        # 写入后条目下线：知识更新提醒经别名找到条目，doc_id 给现行编号
        from app.kb import review as rv
        admin, *_ = create_user(("knowledge_admin",))
        rv.retire(db, db.scalar(select(User).where(User.username == admin)),
                  db.scalar(select(KbItem).where(KbItem.doc_id == cited["live"])), "别名测试下线")
        ch = kb_impact.deliverable_updates(db, d)
    assert u == {"projects": 1, "deliverables": 1, "sections": 1}
    assert ch[1] == [{"doc_id": cited["live"], "ref": cited["a_live"], "change": "retired",
                      "current_version": ch[1][0]["current_version"]}]


def test_quality_states_via_alias():
    live, gone = _publish("别名测试-门禁现行"), _publish("别名测试-门禁下线", retire=True)
    a_live, a_gone = _alias(), _alias()
    _add(a_live, live)
    _add(a_gone, gone)
    with SessionLocal() as db:
        st = quality.kb_ref_states(db, {a_live, a_gone, "L2-OPS-SOC-TPL-99996"})
    assert st == {a_gone: quality.KB_GONE, "L2-OPS-SOC-TPL-99996": quality.KB_GONE}


def test_recheck_usage_merges_alias_with_current():
    item = _publish(f"别名测试-复查{secrets.token_hex(2)}")
    old = _alias()
    _add(old, item)
    code = f"BANK-A-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    member, *_ = create_user(("consultant",), {code: "member"})
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        u = db.scalar(select(User).where(User.username == member))
        d = next(x for x in p.deliverables if x.code == "D4")
        dl.set_outline(db, u, p, d, ["现状"])
        dl.write_section(db, u, p, d, 1, f"Agent 写的 [{old}]。", agent_code="report-writer")
        dl.write_section(db, u, p, d, 1, f"顾问改成现行编号 [{item}]。")      # 同一条，不算删掉
        usage = recheck.usage(db, utcnow() - timedelta(days=1))
    assert old not in usage
    assert len(usage[item].cited) == 1 and not usage[item].removed


def test_api_item_by_alias_and_search():
    live = _publish(f"别名测试-接口{secrets.token_hex(2)}")
    old = _alias()
    _add(old, live)
    expert = create_user(("knowledge_expert",))
    c = logged_in_client(app, expert)
    try:
        r = c.get(f"/api/kb/items/{old}")
        assert r.status_code == 200 and r.json()["doc_id"] == live and r.json()["aliases"] == [old]
        assert c.get(f"/api/kb/items/{old}/usage").status_code == 200
        hits = [x["doc_id"] for x in c.get("/api/kb/items", params={"q": old}).json()]
        assert hits == [live]
    finally:
        c.__exit__(None, None, None)
