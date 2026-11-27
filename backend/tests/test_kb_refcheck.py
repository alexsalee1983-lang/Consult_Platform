"""知识引用校验基线 kb-ref-check（三期 B15 第一步，app/kb/refcheck.py）：扫已签署交付件（存的引用 + 按正文重扫）、
导出脚注标签哈希、对话引用；快照对比，原来解析得到、现在解析不到 → 非零退出；快照和审计不含正文。"""
import json
import secrets

import pytest
from sqlalchemy import select, update

from app import cli
from app.core.db import SessionLocal
from app.kb import refcheck
from app.models import AuditLog, Deliverable, KbItem, Project, User
from app.models.quality import SignOff
from app.project import deliverables as dl
from app.seed import DELIVERABLES

from .conftest import create_user
from .test_kb_refs import _publish

SENTINEL = "夜班无人值守示范句"


@pytest.fixture(scope="module")
def signed():
    live, other = _publish(f"校验测试-现行{secrets.token_hex(4)}"), _publish("校验测试-另一条")
    gone = _publish("校验测试-下线", retire=True)
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
        dl.set_outline(db, u, p, d, ["现状", "建议"])
        dl.write_section(db, u, p, d, 1, f"{SENTINEL} [{live}]，【依据：示范｜{other}】，旧条目 [{gone}]。")
        dl.write_section(db, u, p, d, 2, f"建议 [{live}] 与不存在的 [L2-OPS-SOC-TPL-99998]。")
        db.add(SignOff(project_id=p.id, deliverable_id=d.id, action="sign", revision=1,
                       versions={"1": 1, "2": 1}, actor=member))
        db.commit()
        pid, did = p.id, d.id
    return {"live": live, "other": other, "gone": gone, "project_id": pid, "deliverable_id": did}


def _snap():
    with SessionLocal() as db:
        return refcheck.snapshot(db)


def test_snapshot_covers_signed_stored_scanned_and_export(signed):
    snap = _snap()
    mine = [e for e in snap["entries"] if e.get("deliverable_id") == signed["deliverable_id"]]
    kinds = {(e["source_kind"], e["ref"]) for e in mine if e["source_kind"] != "export"}
    for ref in (signed["live"], signed["other"], signed["gone"], "L2-OPS-SOC-TPL-99998"):
        assert ("stored", ref) in kinds and ("scanned", ref) in kinds
    assert all(e["signed"] for e in mine)
    by_ref = {e["ref"]: e for e in mine if e["source_kind"] == "stored"}
    assert by_ref[signed["live"]]["item_id"] and by_ref[signed["live"]]["current_doc_id"] == signed["live"]
    assert by_ref[signed["gone"]]["retired"] is True
    assert by_ref["L2-OPS-SOC-TPL-99998"]["item_id"] is None
    exp = [e for e in mine if e["source_kind"] == "export"]
    assert len(exp) == 1 and len(exp[0]["labels_sha"]) == 64
    assert SENTINEL not in json.dumps(snap, ensure_ascii=False)


def test_compare_identical_is_clean(signed, tmp_path):
    before = tmp_path / "before.json"
    assert cli.main(["kb-ref-check", "--snapshot", str(before)]) == 0
    assert SENTINEL not in before.read_text(encoding="utf-8")
    assert cli.main(["kb-ref-check", "--compare", str(before), "--signed-only"]) == 0
    r = refcheck.compare(json.loads(before.read_text(encoding="utf-8")), _snap())
    assert r.regressed == 0 and r.consistent > 0 and r.both_missing > 0


def test_regression_exits_nonzero(signed, tmp_path):
    before = tmp_path / "before.json"
    assert cli.main(["kb-ref-check", "--snapshot", str(before)]) == 0
    renamed = f"L2-OPS-SOC-TPL-{secrets.randbelow(9000) + 90000}"
    with SessionLocal() as db:
        db.execute(update(KbItem).where(KbItem.doc_id == signed["other"]).values(doc_id=renamed))
        db.commit()
    try:
        r = refcheck.compare(json.loads(before.read_text(encoding="utf-8")), _snap())
        assert r.regressed >= 3          # 存的引用、重扫的引用、导出标签哈希
        assert signed["other"] in r.regressed_refs
        assert cli.main(["kb-ref-check", "--compare", str(before)]) != 0
    finally:
        with SessionLocal() as db:
            db.execute(update(KbItem).where(KbItem.doc_id == renamed).values(doc_id=signed["other"]))
            db.commit()
    assert cli.main(["kb-ref-check", "--compare", str(before)]) == 0


def test_rename_with_alias_is_not_regression(signed, tmp_path):
    """换了编号、旧编号登记为别名：通过别名解析到同一个 item_id，不算退化（B15 第二步）。"""
    from sqlalchemy import delete

    from app.kb import refs
    from app.models import KbItemAlias

    before = tmp_path / "before.json"
    assert cli.main(["kb-ref-check", "--snapshot", str(before)]) == 0
    renamed = f"L2-OPS-SOC-TPL-{secrets.randbelow(9000) + 90000}"
    with SessionLocal() as db:
        item = db.scalar(select(KbItem).where(KbItem.doc_id == signed["other"]))
        item.doc_id = renamed
        db.flush()
        refs.add_alias(db, signed["other"], item.id, "legacy")
        db.commit()
    try:
        after = _snap()
        r = refcheck.compare(json.loads(before.read_text(encoding="utf-8")), after)
        assert r.regressed == 0 and r.consistent > 0
        mine = [e for e in after["entries"] if e.get("ref") == signed["other"] and e["source_kind"] == "stored"]
        assert mine and all(e["via"] == "alias" and e["current_doc_id"] == renamed for e in mine)
        assert cli.main(["kb-ref-check", "--compare", str(before)]) == 0
    finally:
        with SessionLocal() as db:     # 测试数据复原（平台不提供删除别名的函数）
            db.execute(delete(KbItemAlias).where(KbItemAlias.alias == signed["other"]))
            db.execute(update(KbItem).where(KbItem.doc_id == renamed).values(doc_id=signed["other"]))
            db.commit()


def test_signed_set_change_is_regression(signed):
    before = _snap()
    after = json.loads(json.dumps(before))
    after["entries"] = [e for e in after["entries"] if not (e.get("deliverable_id") == signed["deliverable_id"]
                                                            and e["source_kind"] == "scanned"
                                                            and e["ref"] == signed["live"])]
    assert refcheck.compare(before, after).regressed >= 1


def test_audit_has_counts_only(signed, tmp_path):
    assert cli.main(["kb-ref-check", "--snapshot", str(tmp_path / "s.json")]) == 0
    with SessionLocal() as db:
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.ref_check").order_by(AuditLog.id.desc()))
    detail = json.loads(a.detail)
    assert detail["refs"] > 0 and len(detail["snapshot_sha"]) == 64
    assert SENTINEL not in a.detail and signed["live"] not in a.detail
