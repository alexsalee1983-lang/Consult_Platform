"""依据变更登记（二期 B5，app/kb/basis_change.py）：季度核对清单与库内依据比对归类、--apply 自动登记时效变更
（建新版本、平台核对后发布、旧版本保留）、只有知识管理员能登记、重复登记无变化、审计不记文件名称。"""
import json
import uuid

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.kb import basis_change as bc
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.models import AuditLog, KbItem, KbReview, KbVersion, User

from .conftest import create_user

TAG = uuid.uuid4().hex[:6].upper()
LAW = f"示范安全法{TAG}"
STD = f"GB/T 9{TAG[:4]}"


def _user(roles):
    name, *_ = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


@pytest.fixture(scope="module")
def people():
    return {k: _user(r) for k, r in {"author": ("knowledge_expert",), "expert": ("knowledge_expert",),
                                     "admin": ("knowledge_admin",)}.items()}


def _body(title: str, doc_type: str) -> str:
    if doc_type == "MAP":  # B3：映射矩阵正文必须有映射表
        return f"## {title}\n\n| 示范标准一 | 示范标准二 | 关系 | 说明 |\n|---|---|---|---|\n| A.1 | B.1 | 部分覆盖 | 示范 |\n"
    return f"## {title}\n\n示范条目正文。"


def _publish(people, title, *, document, code="", version="2020", status="current", doc_type="REF"):
    with SessionLocal() as db:
        u = {k: db.merge(v) for k, v in people.items()}
        m = ItemMeta(layer="L4", domain="CMP", product="GEN", doc_type=doc_type, stage="通用", capabilities=["管理"])
        c = VersionContent(title=title, body=_body(title, doc_type), source_type="public",
                           source_detail="公开资料：示范", ref_document=document, ref_version=version,
                           ref_status=status, ref_code=code)
        v = rv.new_item(db, u["author"], m, c)
        rv.submit(db, u["author"], v)
        rv.review(db, u["expert"], v, "content", "approve")
        rv.review(db, u["admin"], v, "compliance", "approve")
        return v.item.doc_id


def _manifest(*rows: str) -> bytes:
    return ("名称,文号/标准号,版本,现行状态,替代文件,施行日期\n" + "\n".join(rows) + "\n").encode("utf-8")


@pytest.fixture(scope="module")
def library(people):
    return {
        "law": _publish(people, f"{LAW} 第一条", document=LAW, version="2029"),
        "std": _publish(people, f"{STD}-2020 示范目录", document="示范技术要求", code=f"{STD}-2020", doc_type="SRC"),
        "old": _publish(people, f"{STD}-2015 示范解读", document="示范技术要求", code=f"{STD}-2015", version="2015"),
        "map": _publish(people, "示范映射", document=f"示范标准甲{TAG}；示范标准乙{TAG}", doc_type="MAP"),
        "keep": _publish(people, f"示范办法{TAG}", document=f"示范办法{TAG}", version="2021"),
    }


MANIFEST = _manifest(
    f"{LAW},,2029,已废止,,",                                      # 2 时效变更：废止
    f"示范技术要求,{STD}-2020,2020,已被替代,示范技术要求 {STD}-2030,",  # 3 时效变更：被替代
    f"示范技术要求,{STD}-2015,2019,现行,,",                        # 4 版本不同
    f"示范标准乙{TAG},,,已废止,,",                                  # 5 多文件条目 → 待复核
    f"示范新规{TAG},,2030,现行,,",                                  # 6 库内没有
    f"示范办法{TAG},,2021,现行,,",                                  # 7 一致
    "示范缺替代,,,已被替代,,",                                        # 8 跳过：被替代没写替代文件
    f"示范乱写{TAG},,,作废了,,",                                       # 9 跳过：状态认不出
)


def _kinds(report) -> dict[int, tuple[str, list[str]]]:
    return {f.line: (f.kind, f.doc_ids) for f in report.findings}


def test_compare_classifies_rows(people, library):
    with SessionLocal() as db:
        r = bc.check(db, db.merge(people["expert"]), MANIFEST)          # 只比对：知识专家也可
    k = _kinds(r)
    assert k[2] == (bc.KIND_STATUS, [library["law"]])
    assert k[3] == (bc.KIND_STATUS, [library["std"]])
    assert k[4] == (bc.REVISED, [library["old"]])
    assert k[5] == (bc.REVIEW, [library["map"]])
    assert k[6] == (bc.NEW, [])
    assert k[7] == (bc.UNCHANGED, [library["keep"]])
    assert len(r.skipped) == 2 and r.rows == 6 and not r.applied
    with SessionLocal() as db:                                         # 只比对不改库
        item = db.scalar(select(KbItem).where(KbItem.doc_id == library["law"]))
        assert db.get(KbVersion, item.current_version_id).ref_status == "current"


def test_apply_publishes_status_versions(people, library):
    with SessionLocal() as db:
        with pytest.raises(bc.BasisChangeError):                       # 登记只有知识管理员
            bc.check(db, db.merge(people["expert"]), MANIFEST, apply=True)
        r = bc.check(db, db.merge(people["admin"]), MANIFEST, apply=True)
    assert [f.applied for f in r.findings if f.kind == bc.KIND_STATUS] == [[f"{library['law']} v2"],
                                                                          [f"{library['std']} v2"]]
    with SessionLocal() as db:
        for key, status in (("law", "repealed"), ("std", "superseded")):
            item = db.scalar(select(KbItem).where(KbItem.doc_id == library[key]))
            versions = {v.version: v for v in item.versions}
            cur = db.get(KbVersion, item.current_version_id)
            assert cur.version == 2 and cur.status == "published" and cur.ref_status == status
            assert versions[1].status == "superseded" and versions[1].ref_status == "current"   # 旧版本保留
            assert cur.body == versions[1].body and "核对清单" in cur.change_note
            assert [(x.reviewer, x.decision) for x in db.scalars(select(KbReview).where(KbReview.version_id == cur.id))] \
                == [(bc.PLATFORM_REVIEWER, "approve")]
        std = db.scalar(select(KbItem).where(KbItem.doc_id == library["std"]))
        assert db.get(KbVersion, std.current_version_id).ref_replaced_by == f"示范技术要求 {STD}-2030"
        for key in ("old", "map", "keep"):                               # 其他类不自动改
            assert db.scalar(select(KbItem).where(KbItem.doc_id == library[key])).versions[-1].version == 1
        logs = list(db.scalars(select(AuditLog).where(AuditLog.action == "kb.basis_change")))
    assert all(LAW not in (a.detail or "") and "示范技术要求" not in (a.detail or "") for a in logs)
    applied = [json.loads(a.detail) for a in logs if json.loads(a.detail)["applied"]]
    assert applied and applied[-1]["counts"][bc.KIND_STATUS] == 2 and library["law"] in applied[-1]["doc_ids"]

    with SessionLocal() as db:                                         # 再登记一次：已一致，不再建版本
        r2 = bc.check(db, db.merge(people["admin"]), MANIFEST, apply=True)
    assert _kinds(r2)[2] == (bc.UNCHANGED, [library["law"]]) and r2.counts()[bc.KIND_STATUS] == 0


def test_open_draft_goes_to_review(people):
    title = f"示范草稿规范{TAG}"
    doc_id = _publish(people, title, document=title, version="2022")
    with SessionLocal() as db:
        item = db.scalar(select(KbItem).where(KbItem.doc_id == doc_id))
        rv.new_version(db, db.merge(people["author"]), item, VersionContent(
            title=title, body="", source_type="public", source_detail="公开资料：示范", ref_document=title,
            ref_version="2022", ref_status="current"))
        r = bc.check(db, db.merge(people["admin"]), _manifest(f"{title},,2022,已废止,,"), apply=True)
    assert _kinds(r)[2] == (bc.REVIEW, [doc_id]) and not r.findings[0].applied


def test_cli_dry_run_and_out(people, library, tmp_path, capsys):
    path = tmp_path / "check.csv"
    path.write_bytes(_manifest(f"示范新规{TAG},,2030,现行,,"))
    out = tmp_path / "r.json"
    assert cli_main(["kb-basis-check", str(path), "--as", people["expert"].username, "--out", str(out)]) == 0
    assert "库内没有" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["counts"][bc.NEW] == 1
    assert cli_main(["kb-basis-check", str(path), "--as", people["expert"].username, "--apply"]) == 1
