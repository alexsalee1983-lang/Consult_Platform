"""三期 B19：控制项中枢（知识区 k0013 的 kb_frameworks / kb_controls / kb_control_checkpoints / kb_control_mappings）。

- import_catalog：导入 knowledge/controls 的控制项清单（#237 格式）与映射（#245 格式），按（编号, 目录版本）幂等；
- applies：适用性规则判定（行业、等保级别、辖区、部署形态；参照 800-53B 基线、OSCAL Profile）；
- supersede_framework：标准换版，旧版标 superseded，返回受影响的映射与控制项（模板、在办项目的影响由 B15/A12 接上后补）。

关系类型与 C12 同一套取值（NIST IR 8477、SCF STRM）；只存条款号与简短理由，不存标准原文。
控制项是差距分析的评估维度，不是测评或认证的判定项：理由里出现结论性用语直接拒收（资质边界）。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KbControl, KbControlCheckpoint, KbControlMapping, KbFramework

RELATIONS = ("等价", "子集", "超集", "交集")
METHODS = ("examine", "interview", "test")          # SP 800-53A：检查、访谈、测试
RULE_KEYS = {"industries": "industry", "mlps_levels": "mlps_level", "jurisdictions": "jurisdiction",
             "deployments": "deployment"}
FORBIDDEN = ("测评结论", "密评结论", "认证结论", "通过测评", "符合认证要求", "可通过审核", "满足等保", "符合等保", "达到三级")


class ControlDataError(ValueError):
    """控制项清单或映射数据不合格。"""


def _check_text(where: str, text: str) -> None:
    hit = next((w for w in FORBIDDEN if w in text), None)
    if hit:
        raise ControlDataError(f"{where}：出现资质结论性用语“{hit}”")


def _validate_controls(catalog: Mapping[str, Any]) -> set[str]:
    codes: set[str] = set()
    for c in catalog.get("controls", []):
        for key in ("id", "name", "product", "domain"):
            if not c.get(key):
                raise ControlDataError(f"控制项缺字段 {key}：{c.get('id', '?')}")
        if c["id"] in codes:
            raise ControlDataError(f"控制项编号重复：{c['id']}")
        codes.add(c["id"])
        unknown = set(c.get("applicability") or {}) - set(RULE_KEYS)
        if unknown:
            raise ControlDataError(f"{c['id']} 适用性规则维度未知：{sorted(unknown)}")
        for cp in c.get("checkpoints") or []:
            if not cp.get("id") or not cp.get("objective"):
                raise ControlDataError(f"{c['id']} 检查点缺 id 或 objective")
            bad = set(cp.get("methods") or []) - set(METHODS)
            if bad:
                raise ControlDataError(f"{c['id']}.{cp['id']} 检查方法未知：{sorted(bad)}")
            _check_text(f"{c['id']}.{cp['id']}", cp["objective"])
    return codes


def _validate_mappings(codes: set[str], mappings: Mapping[str, Any]) -> None:
    frameworks = mappings.get("frameworks") or {}
    for code, rows in (mappings.get("mappings") or {}).items():
        if code not in codes:
            raise ControlDataError(f"映射的控制项不在清单里：{code}")
        for r in rows:
            where = f"{code} → {r.get('fw')} {r.get('clause')}"
            if r.get("fw") not in frameworks:
                raise ControlDataError(f"{where}：框架未登记")
            if r.get("rel") not in RELATIONS:
                raise ControlDataError(f"{where}：关系类型须为 {'/'.join(RELATIONS)}")
            s = r.get("strength")
            if isinstance(s, bool) or not isinstance(s, int) or not 1 <= s <= 10:
                raise ControlDataError(f"{where}：强度须为 1～10 的整数")
            if not str(r.get("clause") or "").strip():
                raise ControlDataError(f"{where}：缺条款号")
            _check_text(where, str(r.get("why") or ""))


def _import_controls(db: Session, catalog: Mapping[str, Any], added: dict[str, int]) -> dict[str, KbControl]:
    version = str(catalog["version"])
    by_code: dict[str, KbControl] = {}
    for c in catalog.get("controls", []):
        row = db.scalar(select(KbControl).where(KbControl.code == c["id"], KbControl.catalog_version == version))
        if row is None:
            row = KbControl(code=c["id"], catalog_version=version, name=c["name"], product=c["product"],
                            domain=c["domain"], industries=list(c.get("industries") or []),
                            applicability=dict(c.get("applicability") or {}), params=list(c.get("params") or []),
                            status=c.get("status", "draft"))
            db.add(row)
            db.flush()
            added["controls"] += 1
        by_code[c["id"]] = row
        for seq, cp in enumerate(c.get("checkpoints") or [], start=1):
            cp_code = f"{c['id']}.{cp['id']}"
            if db.scalar(select(KbControlCheckpoint.id).where(KbControlCheckpoint.control_id == row.id,
                                                              KbControlCheckpoint.code == cp_code)) is None:
                db.add(KbControlCheckpoint(control_id=row.id, code=cp_code, seq=seq, objective=cp["objective"],
                                           methods=list(cp.get("methods") or [])))
                added["checkpoints"] += 1
    return by_code


def import_catalog(db: Session, catalog: Mapping[str, Any],
                   mappings: Mapping[str, Any] | None = None) -> dict[str, int]:
    """导入清单与映射，返回新增条数；已存在的（同编号同版本）不改，内容修订走新目录版本。调用方负责 commit。"""
    codes = _validate_controls(catalog)
    if mappings:
        _validate_mappings(codes, mappings)
    added = {"controls": 0, "checkpoints": 0, "mappings": 0, "frameworks": 0}
    by_code = _import_controls(db, catalog, added)
    if mappings:
        mver = str(mappings.get("catalog_version") or catalog["version"])
        fws: dict[str, KbFramework] = {}
        for code, fw in (mappings.get("frameworks") or {}).items():
            fv = str(fw["version"])
            row = db.scalar(select(KbFramework).where(KbFramework.code == code, KbFramework.version == fv))
            if row is None:
                row = KbFramework(code=code, version=fv, name=fw["name"])
                db.add(row)
                db.flush()
                added["frameworks"] += 1
            fws[code] = row
        for code, rows in (mappings.get("mappings") or {}).items():
            ctl = by_code[code]
            for r in rows:
                fw = fws[r["fw"]]
                clause = str(r["clause"])
                exists = db.scalar(select(KbControlMapping.id).where(
                    KbControlMapping.control_id == ctl.id, KbControlMapping.framework_id == fw.id,
                    KbControlMapping.clause == clause, KbControlMapping.mapping_version == mver))
                if exists is None:
                    db.add(KbControlMapping(control_id=ctl.id, framework_id=fw.id, clause=clause, relation=r["rel"],
                                            strength=r["strength"], rationale=str(r.get("why") or "")[:300],
                                            mapping_version=mver))
                    added["mappings"] += 1
    db.flush()
    return added


def applies(rule: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """适用性规则：每个给了取值范围的维度，上下文给出该维度时须落在范围内；空规则或上下文未给的维度不排除。"""
    for key, ctx_key in RULE_KEYS.items():
        allowed = rule.get(key)
        if allowed and ctx_key in context and context[ctx_key] not in allowed:
            return False
    return True


def supersede_framework(db: Session, code: str, version: str, *, new_version: str, new_name: str) -> dict[str, Any]:
    """标准换版：登记新版、旧版标 superseded，返回挂在旧版上的映射与控制项清单（供重新映射）。调用方负责 commit。"""
    old = db.scalar(select(KbFramework).where(KbFramework.code == code, KbFramework.version == version))
    if old is None or old.status != "current":
        raise ControlDataError(f"{code} {version} 不存在或已不是现行版本")
    new = db.scalar(select(KbFramework).where(KbFramework.code == code, KbFramework.version == new_version))
    if new is None:
        new = KbFramework(code=code, version=new_version, name=new_name)
        db.add(new)
        db.flush()
    old.status = "superseded"
    old.superseded_by_id = new.id
    rows = db.execute(select(KbControl.code, KbControlMapping.clause, KbControlMapping.relation)
                      .join(KbControl, KbControl.id == KbControlMapping.control_id)
                      .where(KbControlMapping.framework_id == old.id)
                      .order_by(KbControl.code, KbControlMapping.clause)).all()
    db.flush()
    return {"framework": f"{code} {version}", "superseded_by": f"{code} {new_version}",
            "mappings": [{"control": r.code, "clause": r.clause, "relation": r.relation} for r in rows],
            "controls": sorted({r.code for r in rows})}
