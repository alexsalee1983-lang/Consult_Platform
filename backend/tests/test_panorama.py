"""四期 A25①：项目全景按产品配置生成（docs/驾驶舱改版方案.md 第三节、第八节第 1 项）。

五个产品各立一个虚拟项目，断言：列 = 产品配置里交付件的阶段（按出现顺序，IRE、DSM 有“落地辅导”），
行 = 客户 / 顾问 / 项目经理 / AI·平台 四条泳道；节点从配置生成（每份交付件、每个交付链步骤都有节点），
状态随交付件状态、确认卡变化；非成员 404、无业务角色 403。
"""
from __future__ import annotations

import secrets

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT
from app.main import app
from app.models import Confirmation, Deliverable, Project, User
from app.products import config
from app.project import configs

from .conftest import create_user, logged_in_client

CASES = [("SOC", "BANK"), ("ISO", "GRID"), ("DSM", "BANK"), ("NSG", "MFG"), ("IRE", "MFG")]
LANES = ["client", "consultant", "manager", "ai"]


def _stages(product: str) -> list[str]:
    return list(dict.fromkeys(d.stage for d in config.product(product).deliverables))


@pytest.fixture(scope="module")
def setup():
    """项目经理一人，五个产品各立一个虚拟项目（代号带随机后缀，免得和其他测试撞）。"""
    pm = create_user(roles=(CONSULTANT,))
    tag = secrets.token_hex(2).upper()
    codes: dict[str, str] = {}
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == pm[0]))
        for i, (product, prefix) in enumerate(CASES):
            code = f"{prefix}-P{i}{tag}"
            configs.create(db, actor=pm[0], code=code, tier="standard", manager=user, product=product)
            codes[product] = code
    c = logged_in_client(app, pm)
    yield c, codes
    c.close()


def _get(c, code: str) -> dict:
    r = c.get(f"/api/projects/{code}/panorama")
    assert r.status_code == 200, r.text
    return r.json()


def _set_status(code: str, dcode: str, status: str) -> None:
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        d = db.scalar(select(Deliverable).where(Deliverable.project_id == p.id, Deliverable.code == dcode))
        d.status = status
        db.commit()


@pytest.mark.parametrize(("product", "_prefix"), CASES)
def test_columns_and_nodes_come_from_config(setup, product, _prefix):
    c, codes = setup
    data = _get(c, codes[product])
    assert data["product"] == product
    assert [col["name"] for col in data["columns"]] == _stages(product)
    assert [lane["id"] for lane in data["lanes"]] == LANES
    has_coaching = "落地辅导" in [col["name"] for col in data["columns"]]
    if product in {"IRE", "DSM"}:
        assert has_coaching
    if product in {"SOC", "NSG"}:
        assert not has_coaching
    # 新项目：第一列为当前阶段，其余为下一阶段 / 以后
    assert [col["status"] for col in data["columns"]][:2] == ["cur", "next"]
    assert all(col["status"] == "later" for col in data["columns"][2:])

    ids = {n["id"] for n in data["nodes"]}
    cols = {col["id"] for col in data["columns"]}
    prod = config.product(product)
    for d in prod.deliverables:
        assert f"deliv:{d.code}" in ids and f"sign:{d.code}" in ids
    for s in prod.chain:
        assert f"step:{s.key}" in ids
    for n in data["nodes"]:
        assert n["lane"] in LANES and n["col"] in cols
        assert n["status"] in {"ok", "ai", "cur", "blk", "todo"}
        assert n["target"].startswith(("drawer:", "/project/"))
    # 交付件节点落在配置声明的阶段
    by_id = {n["id"]: n for n in data["nodes"]}
    col_name = {col["id"]: col["name"] for col in data["columns"]}
    for d in prod.deliverables:
        assert col_name[by_id[f"deliv:{d.code}"]["col"]] == d.stage
    # 交付链：步骤 → 产出交付件；交付件 → 签署
    edges = {(e["from"], e["to"]) for e in data["edges"]}
    for s in prod.chain:
        if s.output:
            assert (f"step:{s.key}", f"deliv:{s.output}") in edges
    assert all(e["from"] in ids and e["to"] in ids for e in data["edges"])


def test_node_status_follows_deliverable_status(setup):
    c, codes = setup
    code = codes["IRE"]
    first = _stages("IRE")[0]
    first_codes = [d.code for d in config.product("IRE").deliverables if d.stage == first]

    def node(data, nid):
        return next(n for n in data["nodes"] if n["id"] == nid)

    data = _get(c, code)
    assert node(data, "deliv:D3")["status"] == "todo"
    _set_status(code, "D3", "drafting")
    assert node(_get(c, code), "deliv:D3")["status"] == "cur"
    _set_status(code, "D3", "review")
    data = _get(c, code)
    assert node(data, "deliv:D3")["status"] == "ok"
    assert node(data, "sign:D3")["status"] == "cur"
    _set_status(code, "D3", "signed")
    assert node(_get(c, code), "sign:D3")["status"] == "ok"

    # 第一阶段的交付件全部签署后，第一列完成，第二列成为当前
    for dc in first_codes:
        _set_status(code, dc, "signed")
    cols = _get(c, code)["columns"]
    assert [x["status"] for x in cols[:3]] == ["done", "cur", "next"]


def test_open_card_blocks_node(setup):
    c, codes = setup
    code = codes["NSG"]
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == code))
        card = Confirmation(project_id=p.id, kind="证据不足", title="全景测试：D4 证据不足", agent="测试",
                            deliverable_code="D4")
        db.add(card)
        db.commit()
        cid = card.id
    data = _get(c, code)
    blk = [n for n in data["nodes"] if n["status"] == "blk"]
    assert any(n["id"] == f"card:{cid}" and n["target"] == f"drawer:card:{cid}" for n in blk)
    assert any(n["id"] == "deliv:D4" for n in blk)
    with SessionLocal() as db:
        db.get(Confirmation, cid).status = "resolved"
        db.commit()
    data = _get(c, code)
    assert all(n["id"] != f"card:{cid}" for n in data["nodes"])


def test_mine_flags_by_project_role(setup):
    c, codes = setup
    data = _get(c, codes["SOC"])
    assert data["my_lanes"] == ["consultant", "manager"]      # 项目经理：顾问 + 项目经理两条泳道


def test_non_member_404_and_no_role_403(setup):
    _, codes = setup
    outsider = logged_in_client(app, create_user(roles=(CONSULTANT,)))
    nobody = logged_in_client(app, create_user())
    try:
        assert outsider.get(f"/api/projects/{codes['SOC']}/panorama").status_code == 404
        assert outsider.get("/api/projects/NOPE-X/panorama").status_code == 404
        assert nobody.get(f"/api/projects/{codes['SOC']}/panorama").status_code == 403
    finally:
        outsider.close()
        nobody.close()
