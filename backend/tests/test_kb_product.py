"""知识检索按产品（二期 B2）：本产品优先、再通用；模块 kb_tags 标签加权；依据查询按快照 basis 加权；
Agent 在项目里调用时 product 默认取项目配置快照；条目文件和导入支持 tags；评测按产品分组。"""
import secrets

from sqlalchemy import select

from app.agents import tools
from app.core.db import SessionLocal
from app.kb import evaluate, importer, search
from app.kb.files import format_entry, parse_entry
from app.kb.items import ItemMeta, check_search_tags
from app.models import AuditLog, KbItem, Project, User
from app.products import config
from app.project import configs

from .conftest import create_user
from .test_kb_import_eval import _entry
from .test_kb_search import _ids, _publish, _search, people  # noqa: F401  （夹具）

Q = "沙盘推演 复盘纪要 桌面演练"


def _corpus(people, tag: str):  # noqa: F811
    """GEN 条目全文命中多，SOC 条目命中少；另一条 SOC 条目命中更少但带标签。"""
    gen = _publish(people, f"通用演练-{tag}", "## 演练\n\n沙盘推演之后写复盘纪要，桌面演练每季度一次。",
                   product="GEN", stage="通用", layer="L1")
    soc = _publish(people, f"SOC 演练-{tag}", "## 演练\n\n沙盘推演和复盘纪要按值班组整理。")
    tagged = _publish(people, f"SOC 等保演练-{tag}", "## 演练\n\n沙盘推演按等保要求留痕。")
    with SessionLocal() as db:
        db.get(KbItem, tagged.item_id).tags = [f"等保{tag}"]
        db.commit()
    return gen, soc, tagged


def test_product_first_then_general_and_tag_boost(people):  # noqa: F811
    gen, soc, tagged = _corpus(people, "a")
    plain = _ids(_search(people, "consultant", Q, limit=20))
    assert plain.index(gen.doc_id) < plain.index(soc.doc_id)          # 不传产品：按相关度，通用条目在前

    out = _search(people, "consultant", Q, product="SOC", limit=20)
    ids = _ids(out)
    assert ids.index(soc.doc_id) < ids.index(gen.doc_id)              # 本产品优先
    assert ids.index(tagged.doc_id) < ids.index(gen.doc_id)
    scope = {h["doc_id"]: h["scope"] for h in out["items"]}
    assert scope[gen.doc_id] == "general" and scope[soc.doc_id] == "product"
    assert ids.index(soc.doc_id) < ids.index(tagged.doc_id)           # 没有标签加权时按相关度

    out = _search(people, "consultant", Q, product="SOC", boost_tags=["等保a"], limit=20)
    ids = _ids(out)
    assert ids.index(tagged.doc_id) < ids.index(soc.doc_id) < ids.index(gen.doc_id)
    assert {h["doc_id"]: h["boosted"] for h in out["items"]}[tagged.doc_id] is True

    gen_only = _ids(_search(people, "consultant", Q, product="SOC", include_general=False, limit=20))
    assert gen.doc_id not in gen_only


def test_basis_boosts_snapshot_standards(people):  # noqa: F811
    strong = _publish(people, "管理要求甲", "## 5.1 演练\n\n应定期开展应急演练并记录演练复盘结论。", layer="L4",
                      product="GEN", doc_type="REF", stage="通用", ref=("GB/T 99901", "2020", "current"))
    weak = _publish(people, "行业要求乙", "## 6.2 演练\n\n应开展应急演练。", layer="L4", product="GEN",
                    doc_type="REF", stage="通用", ref=("JR/T 88802", "2021", "current"))
    with SessionLocal() as db:
        u = db.merge(people["consultant"])
        q = "应急演练 复盘结论 记录"
        plain = _ids(search.basis(db, u, q, limit=20))
        assert plain.index(strong.doc_id) < plain.index(weak.doc_id)
        boosted = _ids(search.basis(db, u, q, limit=20, boost_refs=["JR/T 88802-2021"]))
        assert boosted.index(weak.doc_id) < boosted.index(strong.doc_id)


def _project_with_snapshot(kb_tags: list[str]) -> tuple[str, str]:
    code = f"KP-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        db.add(Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard"))
        db.commit()
    name, *_ = create_user(("consultant",), {code: "member"})
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        snap = config.compose_codes("SOC")
        snap["kb_tags"] = kb_tags
        configs._store(db, p, snap, "test")
        db.commit()
    return code, name


def test_tool_defaults_to_project_snapshot(people):  # noqa: F811
    gen, soc, tagged = _corpus(people, "b")
    code, name = _project_with_snapshot(["等保b"])
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        r = tools.invoke(db, user, "consultant-lead", "kb.search", {"query": Q, "limit": 20}, code)
        ids = _ids(r)
        assert ids.index(tagged.doc_id) < ids.index(soc.doc_id) < ids.index(gen.doc_id)
        r = tools.invoke(db, user, "consultant-lead", "kb.search", {"query": Q, "limit": 20, "product": "GEN"}, code)
        assert soc.doc_id not in _ids(r)                                  # 显式传入的以传入为准
        last = db.scalars(select(AuditLog).where(AuditLog.action == "kb.search", AuditLog.actor == name)
                          .order_by(AuditLog.id.desc())).first()
        assert '"product": "GEN"' in last.detail and Q not in last.detail


def test_entry_tags_and_reimport_updates_tags():
    e = parse_entry(_entry(key="b2-tags-entry", tags="[等保, 等保,  密评 ]"))
    e.meta.check()
    assert e.meta.tags == ["等保", "密评"]
    assert "tags:" in format_entry(e)
    try:
        check_search_tags(["x" * 21])
        raise AssertionError("超长标签应被拒")
    except Exception as ex:  # noqa: BLE001
        assert "检索标签" in str(ex)

    name, *_ = create_user(("knowledge_expert",))
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == name))
        key = f"b2-tags-{secrets.token_hex(3)}"
        r = importer.import_entry(db, u, parse_entry(_entry(key=key, tags="[等保]")))
        assert r.action == "created"
        item = db.scalar(select(KbItem).where(KbItem.import_key == key))
        assert item.tags == ["等保"]
        v = item.versions[-1]
        v.status = "published"            # 只为让下面走“与现行版本相同”的分支
        db.commit()
        r = importer.import_entry(db, u, parse_entry(_entry(key=key, tags="[密评]")))
        db.refresh(item)
        assert item.tags == ["密评"] and r.action == "unchanged" and "检索标签" in r.message


def test_eval_groups_by_product(people):  # noqa: F811
    gen, soc, _ = _corpus(people, "c")
    with SessionLocal() as db:
        qs = [{"id": "P1", "question": Q, "mode": "search", "product": "SOC", "expect": [soc.doc_id]},
              {"id": "P2", "question": Q, "mode": "search", "expect": [gen.doc_id]}]
        report = evaluate.run(db, db.merge(people["consultant"]), qs)
    assert set(report.by_product) == {"SOC", "不限"}
    assert report.by_product["SOC"]["questions"] == 1
    assert "按产品" in evaluate.format_report(report)


def test_item_meta_default_tags_empty():
    m = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="MTD", stage="设计", capabilities=["运营"])
    m.check()
    assert m.tags == []
