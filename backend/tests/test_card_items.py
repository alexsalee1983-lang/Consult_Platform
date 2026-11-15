"""严格档按产品（二期 A8）：摘要卡评估项随项目配置快照的 card_items。

清单非空时评估项只能取清单里的值（空白折叠后比对），不在清单整批拒收；清单为空不限；模板带下清单；
快照读不出来时拒收上传（503）、模板照旧可下载并注明；审计不记卡片内容。
"""
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import AuditLog, Project, ProjectConfig
from app.products import config
from app.project import configs

from .test_summary_cards import CONSENT, GOOD, strict  # noqa: F401  （夹具）


def _set_snapshot(code: str, card_items: list[str] | None, broken: bool = False) -> None:
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        snap = config.compose_codes("SOC")
        snap["card_items"] = card_items or []
        row = db.scalar(select(ProjectConfig).where(ProjectConfig.project_id == p.id))
        if row is None:
            row = configs._store(db, p, snap, "test")
        row.snapshot = {"format": 0} if broken else snap
        db.commit()


def _upload(c, code: str, cards: list[dict]):
    return c.post(f"/api/projects/{code}/summary-cards", json={"cards": cards})


def test_items_restricted_by_snapshot(strict):  # noqa: F811  （夹具来自 test_summary_cards）
    code, c, pm, _, _ = strict
    assert pm.post(f"/api/projects/{code}/consents", json=CONSENT).status_code == 201
    _set_snapshot(code, ["特权账号复核", "夜间告警响应", "等保整改"])

    tpl = c.get(f"/api/projects/{code}/summary-cards/template").json()
    assert tpl["items"] == ["特权账号复核", "夜间告警响应", "等保整改"] and "清单" in tpl["items_note"]

    bad = [GOOD[0], {**GOOD[1], "item": "资产台账"}]
    r = _upload(c, code, bad)
    assert r.status_code == 422
    assert r.json()["detail"]["problems"] == [{"card": 2, "field": "item", "kinds": ["评估项不在清单"]}]
    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code,
                                                 AuditLog.action == "project.summary_card")).all()
        assert logs and all("资产台账" not in x.detail for x in logs)

    ok = [GOOD[0], {**GOOD[1], "item": "  夜间告警响应 "}]          # 首尾空白不算
    r = _upload(c, code, ok)
    assert r.status_code == 201, r.text
    assert [x["code"] for x in r.json()] == ["S-01", "S-02"]


def test_empty_list_means_unrestricted(strict):  # noqa: F811
    code, c, pm, _, _ = strict
    assert pm.post(f"/api/projects/{code}/consents", json=CONSENT).status_code == 201
    _set_snapshot(code, [])
    assert c.get(f"/api/projects/{code}/summary-cards/template").json()["items"] == []
    assert _upload(c, code, [{**GOOD[0], "item": "随便什么评估项"}]).status_code == 201


def test_broken_snapshot_refuses_upload(strict):  # noqa: F811
    code, c, pm, _, _ = strict
    assert pm.post(f"/api/projects/{code}/consents", json=CONSENT).status_code == 201
    _set_snapshot(code, ["特权账号复核"], broken=True)
    tpl = c.get(f"/api/projects/{code}/summary-cards/template")
    assert tpl.status_code == 200 and tpl.json()["items"] == [] and "配置有问题" in tpl.json()["items_note"]
    r = _upload(c, code, GOOD)
    assert r.status_code == 503
    assert c.get(f"/api/projects/{code}/summary-cards").json() == []
