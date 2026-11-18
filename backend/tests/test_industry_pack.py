"""行业包（三期，4A v4.0 §2.4）：industries/*.yaml 的读取、校验、按立项行业匹配、并入立项快照和项目卡。"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from app.products import config
from tests.test_product_config import AGENTS, MODULE, PRODUCT, REPO, _write

BANK = {
    "code": "BANK", "name": "银行", "aliases": ["银行", "银行业"], "status": "ready", "level": "L1",
    "products": ["SOC"],
    "basis": ["JR/T 0071-2020"], "terms": ["核心系统：账务处理主系统"], "roles": ["信息科技部"],
    "threats": ["金融欺诈"], "instructions": {"gap": "补查行业监管检查要点。"},
    "gates": [{"id": "bank-basis", "kind": "min_sources", "deliverable": "D3", "min": 1, "level": "major",
               "message": "缺行业依据来源"}],
}
POWER = {"code": "POWER", "name": "电力", "aliases": ["电力"], "status": "ready", "level": "L0",
         "basis": ["电力监控系统安全防护规定"], "tier_hint": "涉及生产控制区时建议严格档"}


def _write_ind(base: Path, *inds: dict) -> Path:
    (base / "industries").mkdir(parents=True, exist_ok=True)
    for ind in inds:
        (base / "industries" / f"{ind.get('code', 'x').lower()}.yaml").write_text(
            yaml.safe_dump(ind, allow_unicode=True), encoding="utf-8")
    return base


def _cat(tmp_path: Path, *inds: dict) -> config.Catalog:
    return config.load(_write_ind(_write(tmp_path, PRODUCT, MODULE), *inds), agents=AGENTS)


def test_repo_industries_valid_and_pending() -> None:
    cat = config.load(REPO)
    assert cat.problems == []
    assert {"BANK", "POWER"} <= set(cat.industries)
    for ind in cat.industries.values():
        assert ind.status == "ready" and ind.expert_review == "pending"
        assert ind.basis, ind.code


def test_industry_loads(tmp_path: Path) -> None:
    cat = _cat(tmp_path, BANK, POWER)
    assert cat.problems == []
    bank = cat.industries["BANK"]
    assert bank.level == "L1" and bank.aliases == ("银行", "银行业") and bank.products == ("SOC",)
    assert bank.expert_review == "pending" and len(bank.version) == 12
    assert cat.industries["POWER"].products == ()


def test_industry_problems(tmp_path: Path) -> None:
    def probs(**kw: object) -> list[str]:
        return _cat(tmp_path / str(len(kw)) / next(iter(kw)), {**copy.deepcopy(BANK), **kw}).problems

    assert any("code" in p for p in probs(code="bank"))
    assert any("level" in p for p in probs(level="L9"))
    assert any("aliases" in p for p in probs(aliases=[]))
    assert any("产品 XYZ" in p for p in probs(products=["XYZ"]))
    assert any("步骤 nope" in p for p in probs(instructions={"nope": "x"}))
    dup = _cat(tmp_path / "dup", BANK, {**POWER, "aliases": ["电力", "银行"]}).problems
    assert any("银行" in p and "重复" in p for p in dup)


def test_resolve_by_project_industry(tmp_path: Path) -> None:
    cat = _cat(tmp_path, BANK, POWER)
    assert config.resolve_industry(cat, "银行", "SOC").code == "BANK"
    assert config.resolve_industry(cat, "银行业/金融", "SOC").code == "BANK"   # 立项下拉“A/B”任一部分
    assert config.resolve_industry(cat, " 电力 ", "SOC").code == "POWER"
    assert config.resolve_industry(cat, "保险", "SOC") is None
    assert config.resolve_industry(cat, "", "SOC") is None
    assert config.resolve_industry(cat, "银行", "IRE") is None                  # 行业包只挂 SOC


def test_compose_with_industry(tmp_path: Path) -> None:
    cat = _cat(tmp_path, BANK)
    prod, bank = cat.products["SOC"], cat.industries["BANK"]
    plain = config.compose(prod)
    assert "industry" not in plain                                   # 没有行业包的快照与原来一致
    snap = config.compose(prod, (), bank)
    assert snap["industry"] == {"code": "BANK", "name": "银行", "version": bank.version, "level": "L1",
                                "basis": ["JR/T 0071-2020"], "terms": ["核心系统：账务处理主系统"],
                                "roles": ["信息科技部"], "threats": ["金融欺诈"], "tier_hint": ""}
    assert snap["basis"][-1] == "JR/T 0071-2020"
    gap = next(s for s in snap["chain"] if s["key"] == "gap")
    assert "补查行业监管检查要点。" in gap["instruction"]
    assert any(g["id"] == "bank-basis" and g["source"] == "BANK" for g in snap["gates"])
    assert snap["expert_pending"] == ["SOC", "BANK"]
    config.check_snapshot(snap)


def test_compose_rejects_unready_or_foreign_industry(tmp_path: Path) -> None:
    import pytest

    cat = _cat(tmp_path, {**BANK, "status": "draft"})
    with pytest.raises(config.CatalogError):
        config.compose(cat.products["SOC"], (), cat.industries["BANK"])
    cat = _cat(tmp_path / "b", {**BANK, "products": ["IRE"]})
    with pytest.raises(config.CatalogError):
        config.compose(cat.products["SOC"], (), cat.industries["BANK"])


def test_compose_codes_picks_industry(tmp_path: Path, monkeypatch) -> None:
    cat = _cat(tmp_path, BANK, POWER)
    monkeypatch.setattr(config, "catalog", lambda: cat)
    assert config.compose_codes("SOC", [], industry="电力")["industry"]["code"] == "POWER"
    assert "industry" not in config.compose_codes("SOC", [], industry="保险")


def test_project_card_shows_industry_pack(monkeypatch) -> None:
    from sqlalchemy import select

    from app.agents import runtime
    from app.core.db import SessionLocal
    from app.models import Project, User
    from app.project import configs
    from tests.conftest import create_user

    name, _, _ = create_user(roles=("consultant",))
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == name))
        configs.create(db, actor=name, code="GRID-IP", tier="standard", manager=user, product="SOC", industry="电力")
        project = db.scalar(select(Project).where(Project.code == "GRID-IP"))
        assert configs.snapshot(db, project)["industry"]["code"] == "POWER"
        card = runtime.project_card(db, user, "consultant-lead", "GRID-IP")
    assert "行业包：电力" in card and "待专家复核" in card and "生产控制区" in card
