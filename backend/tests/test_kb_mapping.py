"""控制映射矩阵（二期 B3，app/kb/mapping.py）：正文表格解析、建版本 / 改草稿时写映射行、kb.mapping 查询
（只查已发布现行版本、可见范围、标准号前缀匹配、反向查、条款号含下级、说明检索）、机密级拦截、审计不记说明。"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.kb import files, mapping, meta
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.models import AuditLog, KbItem, KbMapping, KbVersion, User

from .conftest import create_user

SRC, DST = "ISO/IEC 27001:2022", "GB/T 22239-2019"
NOTE = "映射测试说明：方针与制度"
BODY = f"""## 映射：{SRC} → {DST}

| 说明 | 不是映射表 |
|---|---|
| 这张表 | 第三列不是“关系”，跳过 |

| {SRC} | {DST} | 关系 | 说明 |
|---|---|---|---|
| A.5.1 | 第 8.1.6.1 条、第8.1.6.2条 | 部分覆盖 | {NOTE} |
| A.6.3 | 第 8.1.8.3 条 | 等同 | 安全意识教育和培训 |
| A.5.24 | 第 8.1.10.12 条 | related | 事件管理 |
"""


def _user(roles):
    name, *_ = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


@pytest.fixture(scope="module")
def people():
    return {k: _user(r) for k, r in {"author": ("knowledge_expert",), "expert": ("knowledge_expert",),
                                     "admin": ("knowledge_admin",), "consultant": ("consultant",)}.items()}


def _meta(**kw) -> ItemMeta:
    return ItemMeta(layer="L4", domain="CMP", product="GEN", doc_type="MAP", stage="通用", capabilities=["管理"], **kw)


def _content(body=BODY, title="ISO 27001 与等保 2.0 管理要求映射（测试）") -> VersionContent:
    return VersionContent(title=title, body=body, source_type="public", source_detail="公开资料：标准条款号",
                          ref_document="映射测试：两份标准", ref_version="2022；2019", ref_status="current")


def _approve(db, u, v):
    rv.submit(db, u["author"], v)
    rv.review(db, u["expert"], v, "content", "approve")
    rv.review(db, u["admin"], v, "compliance", "approve")
    db.refresh(v)
    assert v.status == "published"


def _publish(people, body=BODY, **meta_kw) -> SimpleNamespace:
    with SessionLocal() as db:
        u = {k: db.merge(v) for k, v in people.items()}
        v = rv.new_item(db, u["author"], _meta(**meta_kw), _content(body))
        _approve(db, u, v)
        return SimpleNamespace(id=v.id, item_id=v.item_id, doc_id=v.item.doc_id)


def _lookup(user, source, **kw):
    with SessionLocal() as db:
        return mapping.lookup(db, db.merge(user), source, **kw)


def test_parse_rows_expands_and_validates():
    rows = mapping.parse_rows(BODY)
    assert [(r.source_ref, r.target_ref, r.relation) for r in rows] == [
        ("A.5.1", "8.1.6.1", "partial"), ("A.5.1", "8.1.6.2", "partial"),
        ("A.6.3", "8.1.8.3", "equivalent"), ("A.5.24", "8.1.10.12", "related")]
    assert rows[0].source_std == SRC and rows[0].target_std == DST and rows[0].note == NOTE
    bad = [
        "只有文字，没有表格",
        f"| {SRC} | {DST} | 关系 |\n|---|---|---|\n| A.5.1 | 8.1.6.1 | 包含 |",        # 关系取值不对
        f"| {SRC} | {DST} | 关系 |\n|---|---|---|\n| A.5.1 |  | 等同 |",                # 目标条款为空
        f"|  | {DST} | 关系 |\n|---|---|---|\n| A.5.1 | 8.1.6.1 | 等同 |",               # 表头缺源标准
        f"| {SRC} | {DST} | 关系 | 说明 |\n|---|---|---|---|\n| A.5.1 | 8.1.6.1 | 等同 | {'长' * 501} |",
    ]
    for b in bad:
        with pytest.raises(meta.MetaError):
            mapping.parse_rows(b)


def test_std_and_ref_matching():
    assert mapping.std_matches(SRC, "ISO 27001") and mapping.std_matches(SRC, "iso/iec 27001")
    assert mapping.std_matches(DST, "GB/T 22239") and mapping.std_matches(DST, "GB／T 22239-2019")
    assert not mapping.std_matches("GB/T 222390-2020", "GB/T 22239")
    assert not mapping.std_matches(DST, "GB/T 25070")
    assert mapping._refs("第 8.1.6.1 条、A.5.1，8.1.6") == ["8.1.6.1", "A.5.1", "8.1.6"]
    assert mapping.ref_matches("8.1.6.1", "8.1.6") and mapping.ref_matches("A.5.1", "a.5.1")
    assert not mapping.ref_matches("8.1.6.1", "8.1.6.10") and not mapping.ref_matches("A.5.15", "A.5.1")


def test_import_file_rejects_bad_mapping_table():
    head = ("---\nkey: map-bad\ntitle: 坏映射\nlayer: L4\ndomain: CMP\nproduct: GEN\ndoc_type: MAP\nstage: 通用\n"
            "capabilities: [管理]\nsource_type: public\nsource_detail: 公开资料\nref_document: 某标准\n"
            "ref_version: '2022'\nref_status: current\n---\n")
    with pytest.raises(files.EntryError, match="映射表"):
        files.parse_entry(head + "只有文字")
    assert files.parse_entry(head + BODY).meta.doc_type == "MAP"


def test_lookup_only_published_current_version(people):
    with SessionLocal() as db:
        u = {k: db.merge(v) for k, v in people.items()}
        draft = rv.new_item(db, u["author"], _meta(), _content(BODY.replace("A.6.3", "A.6.9")))
        assert db.scalar(select(KbMapping.id).where(KbMapping.version_id == draft.id))   # 草稿也写映射行
    c = people["consultant"]
    assert _lookup(c, SRC, control="A.6.9")["total"] == 0                  # 草稿查不到

    p = _publish(people)
    out = _lookup(c, "ISO 27001", target="GB/T 22239", control="A.5.1")
    mine = [i for i in out["items"] if i["doc_id"] == p.doc_id]
    assert [(i["source_ref"], i["target_ref"], i["relation"]) for i in mine] == [
        ("A.5.1", "8.1.6.1", "partial"), ("A.5.1", "8.1.6.2", "partial")]
    assert mine[0]["note"] == NOTE and mine[0]["version"] == 1 and mine[0]["reversed"] is False
    assert mine[0]["relation_name"] == "部分覆盖"

    rev = _lookup(c, "GB/T 22239", control="8.1.6")                     # 反过来查，含下级条款
    got = {(i["source_ref"], i["target_ref"]) for i in rev["items"] if i["doc_id"] == p.doc_id}
    assert got == {("8.1.6.1", "A.5.1"), ("8.1.6.2", "A.5.1")}
    assert all(i["reversed"] and i["source_std"] == DST for i in rev["items"] if i["doc_id"] == p.doc_id)

    q = _lookup(c, SRC, query="方针 制度")
    assert {i["source_ref"] for i in q["items"] if i["doc_id"] == p.doc_id} == {"A.5.1"}
    none = _lookup(c, "NIST SP 800-53")
    assert none["total"] == 0 and "依据待核实" in none["note"]

    with SessionLocal() as db:                                           # 修订发布后只查新版本
        u = {k: db.merge(v) for k, v in people.items()}
        item = db.get(KbItem, p.item_id)
        v2 = rv.new_version(db, u["author"], item, _content(BODY.replace("8.1.8.3", "8.1.8.4")))
        _approve(db, u, v2)
    refs = {i["target_ref"] for i in _lookup(c, SRC, control="A.6.3")["items"] if i["doc_id"] == p.doc_id}
    assert refs == {"8.1.8.4"}

    with SessionLocal() as db:                                           # 下线后查不到
        u = {k: db.merge(v) for k, v in people.items()}
        rv.retire(db, u["admin"], db.get(KbItem, p.item_id), "测试下线")
    assert not [i for i in _lookup(c, SRC)["items"] if i["doc_id"] == p.doc_id]


def test_restricted_mapping_follows_visible_roles(people):
    p = _publish(people, BODY.replace("A.5.24", "A.5.99"), classification="restricted", visible_roles=["presales"])
    assert not [i for i in _lookup(people["consultant"], SRC, control="A.5.99")["items"] if i["doc_id"] == p.doc_id]
    assert [i for i in _lookup(people["expert"], SRC, control="A.5.99")["items"] if i["doc_id"] == p.doc_id]


def test_edit_draft_reparses_rows(people):
    with SessionLocal() as db:
        u = {k: db.merge(v) for k, v in people.items()}
        v = rv.new_item(db, u["author"], _meta(), _content())
        with pytest.raises(rv.ReviewError):
            rv.edit_draft(db, u["author"], v, {"body": "没有表格"})
        v = db.get(KbVersion, v.id)
        rv.edit_draft(db, u["author"], v, {"body": BODY.replace("A.6.3", "A.6.7")})
        refs = set(db.scalars(select(KbMapping.source_ref).where(KbMapping.version_id == v.id)))
        assert refs == {"A.5.1", "A.6.7", "A.5.24"}
        with pytest.raises(rv.ReviewError):                               # 映射矩阵建版本时正文必须有映射表
            rv.new_item(db, u["author"], _meta(), _content("只有文字"))


def test_tool_guard_and_audit(people):
    _publish(people)
    c = people["consultant"]
    with SessionLocal() as db:
        out = tools.invoke(db, db.merge(c), "compliance-basis", "kb.mapping", {"source": SRC, "limit": 1})
        assert len(out["items"]) == 1 and out["total"] >= 3
        with pytest.raises(tools.ToolError):
            tools.invoke(db, db.merge(c), "compliance-basis", "kb.mapping", {"source": ""})
        with pytest.raises(tools.ToolError):                              # control 只收条款号
            tools.invoke(db, db.merge(c), "compliance-basis", "kb.mapping", {"source": SRC, "control": "随便 写"})
        assert tools.invoke(db, db.merge(c), "compliance-basis", "kb.mapping",
                            {"source": DST, "control": "8.1.8.3"})["total"] >= 1   # 四级条款号不当 IP 拦
        with pytest.raises(tools.ToolError):                              # 机密级：密钥
            tools.invoke(db, db.merge(c), "compliance-basis", "kb.mapping",
                         {"source": SRC, "query": "sk-abcdefghijklmnopqrstuvwxyz123456"})
        with pytest.raises(tools.ToolDenied):                             # 没有授权
            tools.invoke(db, db.merge(c), "report-writer", "kb.mapping", {"source": SRC})
        logs = list(db.scalars(select(AuditLog).where(AuditLog.action == "kb.mapping")))
    assert {a.result for a in logs} >= {"allowed", "blocked"}
    assert all(NOTE not in (a.detail or "") and "sk-abc" not in (a.detail or "") for a in logs)
    allowed = json.loads(next(a for a in logs if a.result == "allowed").detail)
    assert allowed["source"] and "total" in allowed


def test_v0_matrix_file_parses_and_passes_guard():
    """knowledge/mappings 里的第一份映射矩阵（v0 草稿）能解析、能过机密级检测。"""
    from pathlib import Path

    from app.services import guard

    path = Path(__file__).resolve().parents[2] / "knowledge" / "mappings" / "map-iso27001-djbh-mgmt.md"
    text = path.read_text(encoding="utf-8")
    e = files.parse_entry(text)
    rows = mapping.parse_rows(e.content.body)
    assert len(rows) >= 50 and {r.source_std for r in rows} == {SRC} and {r.target_std for r in rows} == {DST}
    assert all(not r.target_ref.startswith("第") for r in rows)
    assert not guard.check(e.content.title + "\n" + e.content.body).blocked
