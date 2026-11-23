"""三期 B26 法规生命周期（app/kb/reg_lifecycle.py）：依据记生效日、废止日、替代 / 部分修订关系；每日按日期自动切换
现行状态（即将实施 → 现行 → 已废止），新依据施行时把它替代的旧依据标为已被替代；建新版本、平台核对后发布、旧版本保留
（B4 提醒、B9 门禁照旧）；列出引用它的知识条目和在办交付件，已签署的只记提示；有草稿的不动、转人工；可重复执行。"""
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.kb import reg_lifecycle as rl
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.models import AuditLog, KbItem, KbReview, KbVersion, User

from .conftest import create_user

TAG = uuid.uuid4().hex[:6].upper()
OLD = f"示范数据安全办法{TAG}"
NEW = f"示范数据安全条例{TAG}"
GONE = f"示范临时通知{TAG}"
AMENDED = f"示范管理规定{TAG}"
D0, D1 = date(2031, 3, 1), date(2031, 6, 1)


def _user(roles):
    name, *_ = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


@pytest.fixture(scope="module")
def people():
    return {k: _user(r) for k, r in {"author": ("knowledge_expert",), "expert": ("knowledge_expert",),
                                     "admin": ("knowledge_admin",)}.items()}


def _publish(people, title, *, layer="L4", document="", status="current", body="", **kw):
    with SessionLocal() as db:
        u = {k: db.merge(v) for k, v in people.items()}
        doc_type = "REF" if layer == "L4" else "MTD"
        m = ItemMeta(layer=layer, domain="CMP", product="GEN", doc_type=doc_type, stage="通用", capabilities=["管理"])
        ref = dict(ref_document=document, ref_version="2031", ref_status=status) if layer == "L4" else {}
        c = VersionContent(title=title, body=body or f"## {title}\n\n示范条目正文。", source_type="public",
                           source_detail="公开资料：示范", **ref, **kw)
        v = rv.new_item(db, u["author"], m, c)
        rv.submit(db, u["author"], v)
        rv.review(db, u["expert"], v, "content", "approve")
        rv.review(db, u["admin"], v, "compliance", "approve")
        return v.item.doc_id


@pytest.fixture(scope="module")
def lib(people):
    return {
        "old": _publish(people, f"{OLD} 第一条", document=OLD),
        "new": _publish(people, f"{NEW} 第一条", document=NEW, status="upcoming", effective_date=D1,
                        ref_replaces=OLD),
        "gone": _publish(people, f"{GONE} 全文", document=GONE, repeal_date=D1),
        "amended": _publish(people, f"{AMENDED} 第三条", document=AMENDED),
        "amender": _publish(people, f"{NEW} 附则", document=NEW, status="upcoming", effective_date=D1,
                            ref_amends=AMENDED),
        "guide": _publish(people, f"数据分类做法{TAG}", layer="L2", body=f"## 做法\n\n依据《{OLD}》第一条开展分类。"),
    }


def _cur(doc_id):
    with SessionLocal() as db:
        item = db.scalar(select(KbItem).where(KbItem.doc_id == doc_id))
        return db.get(KbVersion, item.current_version_id)


def _deliv(monkeypatch, cited: dict[str, list[tuple[int, str]]]):
    """在办交付件：不造项目，直接替换读取函数（交付件计数另有纯函数测试）。"""
    monkeypatch.setattr(rl, "_deliverable_refs", lambda db: [(d, st, {"knowledge": [{"ref": ref, "found": True}]})
                                                             for ref, ds in cited.items() for d, st in ds])


def test_due_status_rules():
    v = KbVersion(ref_status="upcoming", effective_date=D1)
    assert rl.due_status(v, D0) is None and rl.due_status(v, D1) == "current"
    v = KbVersion(ref_status="current", repeal_date=D1)
    assert rl.due_status(v, D0) is None and rl.due_status(v, D1) == "repealed"
    v = KbVersion(ref_status="current", repeal_date=D1, ref_replaced_by="新办法")
    assert rl.due_status(v, D1) == "superseded"
    assert rl.due_status(KbVersion(ref_status="repealed", repeal_date=D0), D1) is None
    assert rl.due_status(KbVersion(ref_status="upcoming", effective_date=D1, repeal_date=D1), D1) == "repealed"


def test_deliverable_counts_split_signed():
    refs = [(1, "drafting", {"knowledge": [{"ref": "X", "found": True}]}),
            (1, "drafting", {"knowledge": [{"ref": "X", "found": True}]}),
            (2, "signed", {"knowledge": [{"ref": "X", "found": True}]}),
            (3, "review", {"knowledge": [{"ref": "X", "found": False}]}),
            (4, "review", {"knowledge": [{"ref": "Y", "found": True}]})]
    assert rl.deliverable_counts(refs, "X") == {"open": 1, "signed": 1, "sections": 3}


def test_before_date_nothing_changes(lib, monkeypatch):
    _deliv(monkeypatch, {})
    with SessionLocal() as db:
        r = rl.run(db, D0, only=set(lib.values()))
    assert not [c for c in r.changes if c.doc_id in lib.values()]


def test_switch_on_date_replace_chain_and_affected(people, lib, monkeypatch):
    _deliv(monkeypatch, {lib["old"]: [(11, "drafting"), (12, "signed")]})
    olds = {k: _cur(lib[k]).version for k in ("old", "new", "gone")}
    with SessionLocal() as db:
        r = rl.run(db, D1, only=set(lib.values()))
    ch = {c.doc_id: c for c in r.changes}
    assert ch[lib["new"]].new == "current" and ch[lib["new"]].applied
    assert ch[lib["amender"]].new == "current"
    assert ch[lib["gone"]].new == "repealed"
    assert ch[lib["old"]].new == "superseded" and ch[lib["old"]].reason.startswith("被替代")
    assert lib["amended"] not in ch                                    # 部分修订不改状态，只进受影响清单
    old_v, new_v, gone_v = _cur(lib["old"]), _cur(lib["new"]), _cur(lib["gone"])
    assert old_v.ref_status == "superseded" and NEW in old_v.ref_replaced_by
    assert new_v.ref_status == "current" and new_v.ref_replaces == OLD and new_v.effective_date == D1
    assert gone_v.ref_status == "repealed" and gone_v.repeal_date == D1
    assert old_v.version == olds["old"] + 1 and new_v.version == olds["new"] + 1   # 旧版本保留
    with SessionLocal() as db:
        assert db.scalar(select(KbReview.reviewer).where(KbReview.version_id == old_v.id)) == rl.PLATFORM_REVIEWER
    aff = {a.doc_id: a for a in r.affected}
    assert aff[lib["old"]].items == [lib["guide"]]                     # 正文写了《旧办法》的条目
    assert aff[lib["old"]].deliverables == {"open": 1, "signed": 1, "sections": 2}
    assert aff[lib["amended"]].kind == "amended"
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "kb.reg_lifecycle")
                       .order_by(AuditLog.id.desc())).first()
        assert OLD not in str(a.detail) and lib["old"] in str(a.detail)   # 审计只记编号和计数，不记文件名称


def test_rerun_is_noop(lib, monkeypatch):
    _deliv(monkeypatch, {})
    with SessionLocal() as db:
        r = rl.run(db, D1, only=set(lib.values()))
    assert not [c for c in r.changes if c.doc_id in lib.values()]


def test_open_draft_goes_to_review(people, monkeypatch):
    _deliv(monkeypatch, {})
    doc = _publish(people, f"示范草稿依据{TAG}", document=f"示范草稿依据{TAG}", repeal_date=D1)
    with SessionLocal() as db:
        item = db.scalar(select(KbItem).where(KbItem.doc_id == doc))
        cur = db.get(KbVersion, item.current_version_id)
        u = db.merge(people["author"])
        rv.new_version(db, u, item, VersionContent(title=cur.title, body=cur.body + "\n补充。", source_type="public",
                                                   source_detail="公开资料：示范", ref_document=cur.ref_document,
                                                   ref_version="2031", ref_status="current"))
        r = rl.run(db, D1, only={doc})
    c = next(c for c in r.changes if c.doc_id == doc)
    assert not c.applied and "草稿" in c.skipped
    assert _cur(doc).ref_status == "current"


def test_dry_run_and_cli(people, monkeypatch, capsys):
    _deliv(monkeypatch, {})
    doc = _publish(people, f"示范试运行依据{TAG}", document=f"示范试运行依据{TAG}", repeal_date=D1)
    with SessionLocal() as db:
        r = rl.run(db, D1, apply=False, only={doc})
    assert next(c for c in r.changes if c.doc_id == doc).applied is False and _cur(doc).ref_status == "current"
    assert cli_main(["kb-reg-lifecycle", "--date", D1.isoformat(), "--doc", doc]) == 0
    assert doc in capsys.readouterr().out and _cur(doc).ref_status == "repealed"
