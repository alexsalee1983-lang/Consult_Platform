"""使用反馈标待复查（二期 B13，app/kb/recheck.py）：顾问常删掉 Agent 引用的条目 → 标待复查进抽检队列；
阈值（章节数、项目数）、不重复标记、复查通过后清除且不计入自动发布抽检率；审计不记项目代号和正文。"""
import secrets
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal, utcnow
from app.kb import autopublish, recheck
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.models import AuditLog, Deliverable, KbItem, KbReview, KbVersion, Project, User
from app.project import deliverables as dl
from app.seed import DELIVERABLES

from .conftest import create_user


def _user(roles, projects=None):
    name, *_ = create_user(roles, projects)
    return name


def _publish(people, title):
    with SessionLocal() as db:
        u = {k: db.scalar(select(User).where(User.username == v)) for k, v in people.items()}
        m = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="TPL", stage="设计", capabilities=["管理"])
        v = rv.new_item(db, u["author"], m, VersionContent(title=title, body=f"## {title}\n\n示范做法。",
                                                           source_type="public", source_detail="公开资料：示范"))
        rv.submit(db, u["author"], v)
        rv.review(db, u["expert"], v, "content", "approve")
        rv.review(db, u["admin"], v, "compliance", "approve")
        return v.item.doc_id


def _project(n_sections):
    code = f"RCK-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        p = Project(code=code, service="SOC 运营体系设计", industry="银行", tier="standard")
        p.deliverables = [Deliverable(code=f"D{i + 1}", title=t) for i, t in enumerate(DELIVERABLES)]
        db.add(p)
        db.commit()
    member = _user(("consultant",), {code: "member"})
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        u = db.scalar(select(User).where(User.username == member))
        d = next(x for x in p.deliverables if x.code == "D4")
        dl.set_outline(db, u, p, d, [f"第{i}章" for i in range(1, n_sections + 1)])
    return code, member


def _rewrite(code, member, seq, cited, kept):
    """Agent 写一版引用 cited 和 kept，顾问随后手改一版只留 kept。"""
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        u = db.scalar(select(User).where(User.username == member))
        d = next(x for x in p.deliverables if x.code == "D4")
        dl.write_section(db, u, p, d, seq, f"现状与建议 [{cited}] [{kept}]。", agent_code="report-writer")
        dl.write_section(db, u, p, d, seq, f"现状与建议（顾问改写） [{kept}]。")


@pytest.fixture(scope="module")
def people():
    return {"author": _user(("knowledge_expert",)), "expert": _user(("knowledge_expert",)),
            "admin": _user(("knowledge_admin",))}


def _version(doc_id):
    with SessionLocal() as db:
        item = db.scalar(select(KbItem).where(KbItem.doc_id == doc_id))
        return db.get(KbVersion, item.current_version_id)


def test_scan_marks_often_removed_items(people):
    bad, good = _publish(people, f"复查测试-常被删{secrets.token_hex(4)}"), _publish(people, "复查测试-常被保留")
    a, ma = _project(2)
    _rewrite(a, ma, 1, bad, good)
    _rewrite(a, ma, 2, bad, good)
    with SessionLocal() as db:                         # 只有一个项目的两个章节：不到阈值
        assert bad not in recheck.scan(db)
    b, mb = _project(1)
    _rewrite(b, mb, 1, bad, good)
    with SessionLocal() as db:
        marked = recheck.scan(db)
        u = recheck.usage(db, utcnow() - timedelta(days=1))
    assert bad in marked and good not in marked
    assert len(u[bad].removed) == 3 and len(u[bad].projects) == 2 and not u[good].removed
    v = _version(bad)
    assert v.recheck_state == "pending" and "2 个项目的 3 个章节" in v.recheck_reason
    with SessionLocal() as db:                         # 不重复标记
        assert bad not in recheck.scan(db)
        assert v.id in [x.id for x in autopublish.sample_queue(db)]
        logs = list(db.scalars(select(AuditLog).where(AuditLog.action == "kb.recheck_mark")))
    assert logs and all(a not in (x.detail or "") and b not in (x.detail or "") and "顾问改写" not in (x.detail or "")
                        for x in logs)

    with SessionLocal() as db:                         # 作者不能复查自己的条目；人审版本也可以复查
        author = db.scalar(select(User).where(User.username == people["author"]))
        with pytest.raises(rv.ReviewError) as e:
            autopublish.sample_check(db, author, db.get(KbVersion, v.id), "pass")
        assert e.value.status == 403
        before = autopublish.sample_stats(db, autopublish.AGENT)
        checker = db.scalar(select(User).where(User.username == people["expert"]))
        autopublish.sample_check(db, checker, db.get(KbVersion, v.id), "pass")
        cur = db.get(KbVersion, v.id)
        assert cur.recheck_state == "passed" and cur.sample_state == "" and cur.recheck_reason
        assert [r.review_role for r in db.scalars(select(KbReview).where(KbReview.version_id == v.id).order_by(KbReview.id))][-1] == "recheck"
        assert autopublish.sample_stats(db, autopublish.AGENT) == before          # 不计入自动发布抽检率
        assert v.id not in [x.id for x in autopublish.sample_queue(db)]
        with pytest.raises(rv.ReviewError) as e:
            autopublish.sample_check(db, checker, db.get(KbVersion, v.id), "pass")
        assert e.value.status == 409
        assert bad not in recheck.scan(db)            # 复查之前的删除不再算
