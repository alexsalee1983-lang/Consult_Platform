"""三期 B35 最小版：公司做法、黄金样例两类知识类型（知识区迁移 k0017）。

- 条目级字段 knowledge_kind（"" 通用 / practice 公司做法 / golden 黄金样例）和 slot（C10 章节槽位，选填）；
  不填时按交付物类型和来源推断：GLD 一律黄金样例，模板、方法、案例、踩坑等非公开来源为公司做法。
- 校验：黄金样例只能是 GLD、GLD 只能是黄金样例；公司做法只能是 PRACTICE_TYPES；两类都不收公开资料。
- 审核路径：必须有作者以外的知识专家人工内容审核通过才能发布（进检索），Agent、平台自动发布都不行。
"""
import uuid

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import meta
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent, create_item
from app.models import KbReview, User

from .conftest import create_user


def test_derive_kind():
    assert meta.derive_kind("GLD", "expert") == "golden"
    assert meta.derive_kind("GLD", "public") == "golden"
    assert meta.derive_kind("TPL", "project") == "practice"
    assert meta.derive_kind("PIT", "internal") == "practice"
    assert meta.derive_kind("TPL", "public") == ""
    assert meta.derive_kind("REF", "expert") == ""


@pytest.mark.parametrize("kind,doc_type,slot", [
    ("golden", "TPL", ""), ("", "GLD", ""), ("practice", "REF", ""), ("unknown", "TPL", ""),
    ("golden", "GLD", "含 空格"), ("practice", "TPL", "x" * 65),
])
def test_check_kind_rejects(kind, doc_type, slot):
    with pytest.raises(meta.MetaError):
        meta.check_kind(kind, doc_type, slot)


def test_check_kind_accepts():
    meta.check_kind("golden", "GLD", "SOC.D5.ch3")
    meta.check_kind("practice", "MTD", "")
    meta.check_kind("", "REF", "")


def _people():
    names = [create_user(("knowledge_expert",))[0], create_user(("knowledge_expert",))[0]]
    with SessionLocal() as db:
        return [db.scalar(select(User).where(User.username == n)) for n in names]


def _draft(db, author: User, doc_type: str, source: str, **kw):
    m = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type=doc_type, stage="设计", capabilities=["运营"], **kw)
    c = VersionContent(title=f"示范{doc_type}{uuid.uuid4().hex[:6]}", body="## 做法\n\n按示范步骤开展。",
                       source_type=source, source_detail="专家萃取：示范" if source != "public" else "公开资料：示范")
    v = create_item(db, m, c, created_by=author.username)
    if source != "public":                          # 模拟投料去标识已确认（只在测试里直接改取值）
        v.deid_status = "passed"
    else:
        v.deid_status = "not_needed"
    db.commit()
    return v


def test_practice_needs_human_expert():
    author, expert = _people()
    with SessionLocal() as db:
        author, expert = db.merge(author), db.merge(expert)
        v = _draft(db, author, "TPL", "expert", slot="SOC.D5.ch3")
        assert v.item.knowledge_kind == "practice" and v.item.slot == "SOC.D5.ch3"
        rv.submit(db, author, v)
        db.add(KbReview(version_id=v.id, reviewer="agent:kb-reviewer", review_role="agent", decision="approve"))
        db.commit()
        with pytest.raises(rv.ReviewError) as e:      # 只有 Agent 意见，平台不能发布
            rv.publish(db, v)
        assert e.value.status == 409
        db.refresh(v)
        assert v.status == "in_review"
        rv.review(db, expert, v, "content", "approve")
        db.refresh(v)
        assert v.status == "published" and v.item.current_version_id == v.id


def test_golden_rejects_public_source():
    author, _ = _people()
    with SessionLocal() as db:
        author = db.merge(author)
        v = _draft(db, author, "GLD", "public")
        assert v.item.knowledge_kind == "golden"
        with pytest.raises(rv.ReviewError) as e:
            rv.submit(db, author, v)
        assert e.value.status == 422


def test_explicit_kind_mismatch_rejected():
    author, _ = _people()
    with SessionLocal() as db:
        author = db.merge(author)
        with pytest.raises(meta.MetaError):
            _draft(db, author, "REF", "expert", knowledge_kind="practice")
