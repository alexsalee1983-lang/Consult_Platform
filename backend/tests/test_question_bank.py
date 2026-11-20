"""题库格式自检（三期 E1）：question_bank/*.yaml 能解析、id 唯一、必填字段齐、产品与控制域存在。格式见 docs/题库格式.md。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BANK_DIR = REPO / "question_bank"
FILES = sorted(BANK_DIR.glob("*.yaml"))

CONTROL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
DEPTHS = {"quick", "full", "deep"}
ROLES = {"安全负责人", "运营值班", "安全分析", "IT运维", "业务负责人", "合规风控", "管理层"}
PREFILL = {"asset_list", "org_chart", "policy_docs", "siem_stats", "ticket_export", "runbooks",
           "prior_report", "incident_record", "self_assessment", "none"}
INDUSTRIES = {"BANK", "POWER"}
REQUIRED = ["id", "product", "domain", "control_ids", "kind", "stem", "anchors", "followups",
            "depth", "respondent_roles", "prefill", "problem"]
BANNED = ["不合格", "测评结论", "认证结论", "通过测评", "审计发现"]


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_question_bank_files_exist() -> None:
    assert {p.stem for p in FILES} >= {"soc", "ire"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_bank_file(path: Path) -> None:
    data = _load(path)
    product = data["product"]
    assert path.stem == product.lower()
    prod_cfg = yaml.safe_load((REPO / "products" / f"{path.stem}.yaml").read_text(encoding="utf-8"))
    assert prod_cfg["code"] == product, "产品必须存在于 products/"
    assert data["domains"] == prod_cfg["baseline"]["domains"], "控制域须与产品配置 baseline.domains 一致"
    assert data["version"] and data["status"] and data["expert_review"]

    controls = {c["id"]: c for c in data["controls"]}
    assert len(controls) == len(data["controls"]), "控制项 id 重复"
    for cid, c in controls.items():
        assert CONTROL_RE.match(cid), cid
        assert cid.startswith(product + ".")
        assert c["name"] and c["domain"] in data["domains"]

    questions = data["questions"]
    assert 40 <= len(questions) <= 60, f"{product} 题数 {len(questions)} 不在 40～60"
    ids = [q["id"] for q in questions]
    assert len(set(ids)) == len(ids), "题 id 重复"
    covered: set[str] = set()
    for q in questions:
        _check_question(q, product, data, controls)
        covered.update(q["control_ids"])
    assert covered == set(controls), f"未被任何题覆盖的控制项：{set(controls) - covered}"
    assert {q["domain"] for q in questions} == set(data["domains"]), "有控制域没有题"


def _check_question(q: dict, product: str, data: dict, controls: dict) -> None:
    qid = q.get("id", "?")
    for k in REQUIRED:
        assert k in q, f"{qid} 缺字段 {k}"
    assert re.match(rf"^{product}-[A-Z]{{3}}-\d{{3}}$", q["id"]), qid
    assert q["product"] == product and q["domain"] in data["domains"]
    assert q["control_ids"] and all(c in controls for c in q["control_ids"]), f"{qid} 控制项未定义"
    assert all(controls[c]["domain"] == q["domain"] for c in q["control_ids"]), f"{qid} 控制项与题的域不一致"
    assert q["kind"] in {"closed", "open"}
    assert q["stem"].strip() and q["stem"].strip()[-1] in "？。）"
    assert q["problem"] is False, "v1 的 problem 标记只由运行时置位"
    assert q["depth"] and set(q["depth"]) <= DEPTHS
    assert q["respondent_roles"] and set(q["respondent_roles"]) <= ROLES
    assert isinstance(q["followups"], list) and isinstance(q["prefill"], list)
    assert q["prefill"] and {p["source"] for p in q["prefill"]} <= PREFILL
    levels = [a["level"] for a in q["anchors"]]
    assert levels == list(range(1, len(levels) + 1)) and all(a["state"].strip() for a in q["anchors"])
    if q["kind"] == "closed":
        opts = q["options"]
        assert len(levels) == 4 and len(opts) == 5
        assert [o["level"] for o in opts[:4]] == [1, 2, 3, 4]
        assert opts[4] == {"key": "X", "level": None, "text": "不清楚"}
        assert [o["key"] for o in opts[:4]] == list("ABCD")
        assert [o["text"] for o in opts[:4]] == [a["state"] for a in q["anchors"]]
    else:
        assert "options" not in q and len(levels) >= 3
    for v in q.get("industry_variants", []):
        assert v["industry"] in INDUSTRIES and v["stem"].strip() and isinstance(v["followups"], list)
    text = q["stem"] + "".join(q["followups"])
    assert not any(w in text for w in BANNED), f"{qid} 含不合适措辞"
