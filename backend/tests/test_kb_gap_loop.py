"""四期 B41：知识缺口闭环补齐（app/kb/gap_loop.py）。

① 就绪度缺格 → 缺口；② 交付件引用库里没有 → 缺口；外部变化（法规换版）、生命周期（条目到期）信号；
③ 来源推荐的程序硬校验与命中率停用；④ 栏目页两级取正文（端到端：回传 → 解析 → 下一轮下发包 → 入库）；
⑤ 失败 3 次暂无可用来源、每月再试、健康格注明；⑥ 入库报警卡。全部虚拟数据。
"""
import hashlib
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app import cli
from app.core.config import get_settings
from app.core.db import SessionLocal, utcnow
from app.core.permissions import CONSULTANT, KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT
from app.kb import gap_loop, gap_pool, source_list
from app.kb.acquisition import ReturnReport, Signal
from app.main import app
from app.models import AuditLog, KbAlarm, KbGap, KbSource, KbVersion

from .conftest import create_user, logged_in_client

TODAY = date(2026, 12, 7)
HOST = "b41.example.gov.cn"
GP = f"https://{HOST}/law/"
SLOT = "L2-DSL-BANK-CHK-0001"
SRC_SLOT = "L4-DSL-LAW-SRC-001"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_sample_fail_max", 1.0)

    def wipe() -> None:
        with SessionLocal() as db:
            db.execute(delete(KbGap))
            db.execute(delete(KbAlarm))
            db.execute(delete(KbSource).where(KbSource.host == HOST))
            db.commit()

    wipe()
    yield
    wipe()


def _active_source(db, url: str = GP) -> None:
    now = utcnow()
    db.add(KbSource(code=f"SR-B{abs(hash(url)) % 9999:04d}", name="示范部法规库（虚拟）", url=url, host=HOST,
                    license="公开发布", authority_kind="法律", authority_level=1, status="active",
                    created_by="t", created_at=now, updated_at=now))
    db.commit()


def _gap(db, key: str) -> KbGap:
    return db.scalar(select(KbGap).where(KbGap.gap_key == key))


def _no_feeds(monkeypatch, *, readiness=None, cites=None, basis=None, docs=None) -> None:
    """把四路信号的数据源换成虚拟的（不建评测集、交付件、依据条目）。"""
    from app.kb import basis_change, citations, reg_lifecycle
    from app.kb import readiness as rd
    from app.services import indicators_v4

    monkeypatch.setattr(rd, "matrix", lambda db: {"evalsets": readiness or []})
    monkeypatch.setattr(indicators_v4, "readiness_grids",
                        lambda evalsets: [{"product": "DSM", "industry": "银行", "status": "below"}] if evalsets else [])
    monkeypatch.setattr(reg_lifecycle, "_deliverable_refs", lambda db: cites or [])
    monkeypatch.setattr(basis_change, "_basis_versions", lambda db: basis or [])
    monkeypatch.setattr(citations, "_sources", lambda db: docs if docs is not None else [])


def test_need_of_maps_key_to_material_kind():
    assert gap_loop.need_of("GB/T 22239-2019") == "标准"
    assert gap_loop.need_of(SRC_SLOT) == "法规"
    assert gap_loop.need_of("L2-DSL-BANK-TPL-0001") == "样例"
    assert gap_loop.need_of(SLOT) == "条目"


# ---- ① ② ----

def test_readiness_missing_slot_becomes_gap_and_closes_when_ready(monkeypatch):
    evalset = {"product": "DSM", "industry": "银行", "chapters": [{"slots": [
        {"key": SLOT, "state": "missing", "layer": "L2", "doc_type": "CHK"},
        {"key": SRC_SLOT, "state": "missing", "layer": "L4", "doc_type": "SRC"},
        {"key": "L2-DSL-BANK-CHK-0002", "state": "ready", "layer": "L2", "doc_type": "CHK"}]}]}
    _no_feeds(monkeypatch, readiness=[evalset])
    with SessionLocal() as db:
        out = gap_loop.feed(db, TODAY)
        assert out["readiness"] == 2
        g = _gap(db, SLOT)
        assert g.kinds == {"business": 1} and g.need == "条目" and _gap(db, SRC_SLOT).need == "法规"
        assert _gap(db, "L2-DSL-BANK-CHK-0002") is None
        gap_loop.feed(db, TODAY)                                     # 同月重复跑不重复计
        db.refresh(g)
        assert g.kinds == {"business": 1}
        monkeypatch.setattr(gap_loop, "_ready_keys", lambda db, keys: {SLOT} & set(keys))
        out = gap_loop.feed(db, TODAY)
        assert out["closed"] == 1
        db.refresh(g)
        assert g.status == "closed" and g.close_reason == "published"
        assert _gap(db, SRC_SLOT).status == "open"
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.feed").order_by(AuditLog.id.desc()))
        assert SLOT not in a.detail                                   # 审计只记数量


def test_citation_not_found_becomes_usage_gap_without_project_info(monkeypatch):
    ref = "L3-DSL-BANK-GLD-0007"
    cites = [(11, "draft", {"knowledge": [{"ref": ref, "found": False}, {"ref": SLOT, "found": True}]}),
             (12, "draft", {"knowledge": [{"ref": ref, "found": False}, {"ref": "不是编号", "found": False}]})]
    _no_feeds(monkeypatch, cites=cites)
    with SessionLocal() as db:
        out = gap_loop.feed(db, TODAY)
        assert out["citation"] == 1
        g = _gap(db, ref)
        assert g.kinds == {"usage": 2} and g.need == "样例"
        assert all(e.startswith("cite-") and "11" not in e[5:7] for e in g.events)
        assert _gap(db, SLOT) is None and _gap(db, "不是编号") is None


def test_basis_replaced_and_due_signals(monkeypatch):
    item = SimpleNamespace(doc_id=SRC_SLOT, layer="L4", doc_type="SRC")
    v = SimpleNamespace(id=5, ref_replaced_by="示范数据安全办法（2027 修订）", repeal_date=TODAY + timedelta(days=30),
                        ref_status="current", review_date=None)
    _no_feeds(monkeypatch, basis=[(item, v)])
    with SessionLocal() as db:
        out = gap_loop.feed(db, TODAY)
        assert out["external"] == 1 and out["lifecycle"] == 1
        ext = _gap(db, "示范数据安全办法（2027 修订）")
        assert ext.kinds == {"external": 1} and ext.need == "法规"
        assert _gap(db, SRC_SLOT).kinds == {"lifecycle": 1}
    # 替代文件入库、依据不再到期 → 两个缺口自动关闭
    v.ref_replaced_by, v.repeal_date = "", None
    _no_feeds(monkeypatch, basis=[(item, v)], docs=[("示范数据安全办法（2027 修订）", "", "current", "")])
    with SessionLocal() as db:
        assert gap_loop.feed(db, TODAY)["closed"] == 2
        assert _gap(db, SRC_SLOT).status == "closed"
        assert _gap(db, "示范数据安全办法（2027 修订）").status == "closed"


# ---- ⑤ ----

def test_three_failures_mark_unavailable_then_monthly_retry(monkeypatch):
    with SessionLocal() as db:
        _active_source(db)
        url = GP + "gone.html"
        gap_pool.record(db, [Signal(url, "external", "EV-B41-1")])
        g = _gap(db, url)
        assert gap_pool.note_failure(db, g) == ""
        assert gap_pool.note_failure(db, g) == ""
        assert gap_pool.note_failure(db, g) == gap_pool.UNAVAILABLE and g.unavailable_at is not None
        gap_pool.run_week(db, TODAY)
        db.refresh(g)
        assert g.plan == gap_pool.UNAVAILABLE                              # 每周计划不重排
        assert gap_loop.retry_unavailable(db, utcnow().date() + timedelta(days=10)) == 0
        assert gap_loop.retry_unavailable(db, utcnow().date() + timedelta(days=31)) == 1
        db.refresh(g)
        assert g.plan == "" and g.attempts == gap_pool.MAX_ATTEMPTS - 1
        gap_pool.run_week(db, TODAY)
        db.refresh(g)
        assert g.plan == "direct"
        assert gap_pool.note_failure(db, g) == gap_pool.UNAVAILABLE          # 再失败一次即重新标


def test_return_failed_three_times_marks_unavailable_and_readiness_notes_it(tmp_path, monkeypatch):
    with SessionLocal() as db:
        _active_source(db)
        gap_pool.record(db, [Signal(SLOT, "business", "EV-B41-2")])
        g = _gap(db, SLOT)
        g.source_url, g.source_via, g.attempts = GP + "slot.html", "listing", 2
        db.commit()
    b = tmp_path / "ret"
    b.mkdir()
    (b / "manifest.json").write_text(json.dumps({"files": [], "failed": [{"url": GP + "slot.html", "reason": "404"}]}),
                                     encoding="utf-8")
    with SessionLocal() as db:
        report, _ = gap_pool.import_return(db, b)
        assert report.unavailable == 1 and report.manual == 0
        assert _gap(db, SLOT).plan == gap_pool.UNAVAILABLE
        from app.kb import readiness

        assert readiness._slot_states(db, {SLOT})[SLOT] == {"state": "missing", "doc_type": "", "layer": "",
                                                             "reason": "unavailable"}


# ---- ③ ----

def _row(gid: int, url: str) -> source_list.Row:
    return source_list.Row(gap_id=gid, title="示范条例", publisher="示范部", source_code="", url=url, license="公开",
                           authority_kind="法律", priority="高", volume="1")


def test_recommend_hard_checks_urls_and_pauses_on_low_hit(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "kb_recommend_min_samples", 2)
    monkeypatch.setattr(get_settings(), "kb_recommend_min_hit", 0.5)
    with SessionLocal() as db:
        _active_source(db)
        for i in range(3):
            gap_pool.record(db, [Signal(f"示范条例{i}（虚拟）", "usage", f"EV-B41-R{i}")])
        gap_pool.run_week(db, TODAY, thresholds={"usage": 1})
        ids = [_gap(db, f"示范条例{i}（虚拟）").id for i in range(3)]
        assert {_gap(db, f"示范条例{i}（虚拟）").plan for i in range(3)} == {"confirm"}
    calls: list[list[int]] = []

    def fake_suggest(db, *, actor, gap_ids):
        calls.append(gap_ids)
        return source_list.Report(rows=[_row(ids[0], GP + "2026/a.html"),           # 通过
                                        _row(ids[1], "https://made-up.example.com/x.html"),   # 非官方
                                        _row(ids[2], GP)])                          # 来源栏目页本身
    monkeypatch.setattr(source_list, "suggest", fake_suggest)
    with SessionLocal() as db:
        out = gap_loop.recommend_week(db)
        assert out["asked"] == 3 and out["accepted"] == 1
        g0 = db.get(KbGap, ids[0])
        assert g0.source_url == GP + "2026/a.html" and g0.source_via == "agent" and g0.rec_total == 1
        assert db.get(KbGap, ids[1]).source_url == "" and db.get(KbGap, ids[2]).source_url == ""
        assert gap_loop.recommend_week(db)["asked"] == 0                       # 一周内不重复推荐
        gap_pool.run_week(db, TODAY, thresholds={"usage": 1})
        assert db.get(KbGap, ids[0]).plan == "direct"                           # 白名单内正文网址直接采集
        assert [t["url"] for t in gap_pool.dispatch_package(db)["tasks"]] == [GP + "2026/a.html"]
        st = gap_loop.recommend_stats(db)
        assert st["total"] == 3 and st["pending"] == 1 and st["hits"] == 0 and st["paused"]
        before = len(calls)
        for i in ids:
            db.get(KbGap, i).rec_at = utcnow() - timedelta(days=8)
        db.commit()
        out = gap_loop.recommend_week(db)
        assert out["paused"] and out["asked"] == 0 and len(calls) == before    # 低于门槛：不再调模型
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.recommend").order_by(AuditLog.id.desc()))
        assert a.result == "paused"


def test_recommend_skipped_without_model(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_base_url", "")
    monkeypatch.setattr(source_list, "suggest", lambda *a, **k: pytest.fail("没接模型不该调"))
    with SessionLocal() as db:
        gap_pool.record(db, [Signal("示范条例X（虚拟）", "usage", "EV-B41-N")])
        gap_pool.run_week(db, TODAY, thresholds={"usage": 1})
        assert gap_loop.recommend_week(db)["asked"] == 0


# ---- ④ 端到端：回传 → 解析 → 下一轮下发包 → 入库 ----

ARTICLE = ("<p>第一条 为了规范示范数据安全管理，保障示范数据安全，制定本办法。本办法适用于示范行业内的数据处理活动。</p>"
           "<p>第二条 运营者应当建立数据分类分级制度，明确数据安全负责人和管理机构，落实数据安全保护责任。</p>"
           "<p>第三条 运营者应当定期开展数据安全风险评估，并向主管部门报送风险评估报告。</p>"
           "<p>第四条 运营者应当对数据处理人员开展安全教育培训，建立数据安全事件应急预案并定期组织演练。</p>"
           "<p>第五条 本办法自公布之日起施行。</p>")


def _listing_html(n: int) -> bytes:
    links = "".join(f'<li><a href="2026/doc{i}.html">示范数据安全办法第{i}号（虚拟）</a></li>' for i in range(n))
    return (f'<html><head><meta charset="utf-8"><title>法规栏目</title></head><body><ul>{links}'
            '<li><a href="https://evil.example.com/x.html">外站链接文字</a></li>'
            '<li><a href="2027/">下一级栏目</a></li><li><a href="#top">顶部</a></li></ul></body></html>').encode()


def _body_html(i: int) -> bytes:
    return (f'<html><head><meta charset="utf-8"><title>站点</title></head><body><h1>示范数据安全办法第{i}号（虚拟）</h1>'
            f'{ARTICLE}<a href="index.html">返回</a></body></html>').encode()


def _batch(tmp_path, name: str, files: dict[str, tuple[bytes, str]]):
    b = tmp_path / name
    b.mkdir()
    rows = []
    for fname, (data, url) in files.items():
        (b / fname).write_bytes(data)
        rows.append({"file": fname, "sha256": hashlib.sha256(data).hexdigest(), "url": url})
    (b / "manifest.json").write_text(json.dumps({"files": rows}, ensure_ascii=False), encoding="utf-8")
    return b


def test_page_links_and_listing_detection():
    links, listing = gap_loop.page_links(_listing_html(6), GP + "index.html", (GP,))
    assert listing and links == [f"{GP}2026/doc{i}.html" for i in range(6)]
    links, listing = gap_loop.page_links(_body_html(1), GP + "2026/doc1.html", (GP,))
    assert not listing and links == [GP + "2026/index.html"]
    assert gap_loop.page_links(b"%PDF-1.4 x", GP + "a.pdf", (GP,)) == ([], False)
    assert gap_loop.page_title(_body_html(3)) == "示范数据安全办法第3号（虚拟）"


def test_listing_two_level_end_to_end(tmp_path):
    expert = create_user((KNOWLEDGE_EXPERT,))[0]
    index = GP + "index.html"
    with SessionLocal() as db:
        _active_source(db)
        gap_pool.record(db, [Signal(index, "external", "EV-B41-L", impact=3)])
        gap_pool.run_week(db, TODAY)
        assert [t["url"] for t in gap_pool.dispatch_package(db)["tasks"]] == [index]
    # 第一轮回传：栏目页 → 不入库，解析出 6 个白名单内正文链接进下一轮下发包
    b1 = _batch(tmp_path, "b1", {"list.html": (_listing_html(6), index)})
    assert cli.main(["kb-acquire-import", str(b1), "--auto-manifest", "--as", expert]) == 0
    bodies = [f"{GP}2026/doc{i}.html" for i in range(6)]
    with SessionLocal() as db:
        assert _gap(db, index).status == "closed"
        rows = [_gap(db, u) for u in bodies]
        assert all(r.plan == "direct" and r.source_via == "listing" for r in rows)
        assert _gap(db, "https://evil.example.com/x.html") is None
        assert sorted(t["url"] for t in gap_pool.dispatch_package(db)["tasks"]) == sorted(bodies)
        assert db.scalar(select(KbAlarm)) is None                    # 全是栏目页的批次不报警
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.listing").order_by(AuditLog.id.desc()))
        assert json.loads(a.detail)["next_round"] == 6 and "doc1" not in a.detail
    # 第二轮回传：正文页 → 入库（名称取页面标题）、缺口关闭
    b2 = _batch(tmp_path, "b2", {f"doc{i}.html": (_body_html(i), u) for i, u in enumerate(bodies[:2])})
    assert cli.main(["kb-acquire-import", str(b2), "--auto-manifest", "--as", expert]) == 0
    with SessionLocal() as db:
        assert _gap(db, bodies[0]).status == "closed" and _gap(db, bodies[2]).status == "open"
        stored = list(db.scalars(select(KbVersion).where(KbVersion.ref_document.like("示范数据安全办法第%号（虚拟）"))))
        assert len(stored) > 0
        assert db.scalar(select(KbAlarm).where(KbAlarm.status == "open")) is None


# ---- ⑥ ----

def test_intake_alarm_merges_and_auto_resolves(tmp_path):
    b1 = _batch(tmp_path, "a1", {"x.html": (b"<html><body>404</body></html>", GP + "x.html")})
    b2 = _batch(tmp_path, "a2", {"y.html": (b"<html><body>err</body></html>", GP + "y.html")})
    with SessionLocal() as db:
        a = gap_loop.check_intake(db, b1, ReturnReport(count=1, body=0), 0)
        assert a is not None and a.kind == "zero_intake" and a.count == 1
        assert gap_loop.check_intake(db, b2, ReturnReport(count=1, body=0), 0).id == a.id
        db.refresh(a)
        assert a.count == 2 and len(a.batches) == 2 and "x.html" not in json.dumps(a.counts)
        low = gap_loop.check_intake(db, b1, ReturnReport(count=4, body=1), None)
        assert low.kind == "low_body"
        assert gap_loop.check_intake(db, b2, ReturnReport(count=2, body=2), 2) is None
        assert db.scalar(select(KbAlarm).where(KbAlarm.status == "open")) is None


def test_alarm_api_and_my_tasks():
    users = {"admin": create_user((KNOWLEDGE_ADMIN,)), "expert": create_user((KNOWLEDGE_EXPERT,)),
             "consultant": create_user((CONSULTANT,))}
    with SessionLocal() as db:
        now = utcnow()
        a = KbAlarm(kind="zero_intake", status="open", count=1, batches=["abc123"], counts={"files": 1},
                    created_at=now, updated_at=now)
        db.add(a)
        db.commit()
        aid = a.id
    c = {k: logged_in_client(app, u) for k, u in users.items()}
    try:
        assert c["consultant"].get("/api/kb/sources/alarms").status_code == 403
        r = c["expert"].get("/api/kb/sources/alarms")
        assert r.status_code == 200 and [x["id"] for x in r.json()] == [aid] and r.json()[0]["label"] == "入库数为 0"
        assert c["expert"].post(f"/api/kb/sources/alarms/{aid}/resolve").status_code == 403
        tasks = c["admin"].get("/api/my-tasks").json()
        items = tasks if isinstance(tasks, list) else tasks.get("tasks", tasks.get("items", []))
        assert any(t.get("kind") == "kb_alarm" and t.get("ref_id") == aid for t in items)
        assert c["admin"].post(f"/api/kb/sources/alarms/{aid}/resolve").json() == {"id": aid, "status": "resolved"}
        assert c["admin"].post(f"/api/kb/sources/alarms/{aid}/resolve").status_code == 404
    finally:
        for x in c.values():
            x.__exit__(None, None, None)
