"""题库格式自检（三期 E1）。格式见 docs/题库格式.md。

题库正文放资料仓 Consult_Knowledge 的 question_bank/（决策记录 2026-11-16 知识内容不进代码仓），
代码仓只留 knowledge/question_bank/ 的样例。默认校验样例；设 QUESTION_BANK_DIR 指向资料仓的
question_bank 目录即校验正文（含“通用控制项全覆盖”）：
    QUESTION_BANK_DIR=C:/Alex_Work/Consult_Knowledge/question_bank pytest -q tests/test_question_bank.py
题目挂 B19 控制项中枢（knowledge/controls/v0.yaml）的控制项与检查点（kb_control_checkpoints.code，形如 SOC.ORG.01.a）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BANK_DIR = Path(os.environ.get("QUESTION_BANK_DIR") or REPO / "knowledge" / "question_bank")
FILES = sorted(BANK_DIR.glob("*.yaml"))
CATALOG = yaml.safe_load((REPO / "knowledge" / "controls" / "v0.yaml").read_text(encoding="utf-8"))
CONTROLS = {c["id"]: c for c in CATALOG["controls"]}
CHECKPOINTS = {f"{c['id']}.{cp['id']}": c["id"] for c in CATALOG["controls"] for cp in c.get("checkpoints") or []}

DEPTHS = {"quick", "full", "deep"}
ROLES = {"安全负责人", "运营值班", "安全分析", "IT运维", "业务负责人", "合规风控", "管理层", "数据负责人", "法务", "人力行政"}
PREFILL = {"asset_list", "org_chart", "policy_docs", "siem_stats", "ticket_export", "runbooks",
           "prior_report", "incident_record", "self_assessment", "data_inventory", "risk_register", "none"}
CHANNELS = {"prefill", "self", "onsite", "discussion"}  # 资料预填 / 客户自评 / 现场题卡 / 开放讨论
INDUSTRIES = {"BANK", "POWER"}
REQUIRED = ["id", "product", "domain", "control_ids", "checkpoints", "kind", "stem", "anchors", "followups",
            "depth", "respondent_roles", "prefill", "channel", "problem"]
BANNED = ["不合格", "测评结论", "认证结论", "通过测评", "审计发现"]


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_question_bank_files_exist() -> None:
    assert {p.stem for p in FILES} >= {"soc", "ire"}, f"{BANK_DIR} 下缺 soc.yaml / ire.yaml"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_bank_file(path: Path) -> None:
    data = _load(path)
    product = data["product"]
    assert path.stem == product.lower()
    prod_cfg = yaml.safe_load((REPO / "products" / f"{path.stem}.yaml").read_text(encoding="utf-8"))
    assert prod_cfg["code"] == product, "产品必须存在于 products/"
    assert data["domains"] == prod_cfg["baseline"]["domains"], "控制域须与产品配置 baseline.domains 一致"
    assert data["domains"] == CATALOG["domains"][product], "控制域须与控制项清单一致"
    assert data["version"] and data["status"] and data["expert_review"]
    assert data["catalog"] == CATALOG["version"], "catalog 须写所引用的控制项清单版本"
    assert data["scope"] in {"full", "sample"}
    assert "controls" not in data, "控制项以 B19 清单为准，题库只引用编号"

    questions = data["questions"]
    if data["scope"] == "full":
        assert 40 <= len(questions) <= 80, f"{product} 题数 {len(questions)} 不在 40～80"
    ids = [q["id"] for q in questions]
    assert len(set(ids)) == len(ids), "题 id 重复"
    covered: set[str] = set()
    for q in questions:
        _check_question(q, product, data)
        covered.update(q["control_ids"])
    if data["scope"] == "full":
        generic = {c["id"] for c in CATALOG["controls"] if c["product"] == product and c["industries"] == ["通用"]}
        assert not generic - covered, f"未被任何题覆盖的通用控制项：{sorted(generic - covered)}"
        assert {q["domain"] for q in questions} == set(data["domains"]), "有控制域没有题"


def _check_question(q: dict, product: str, data: dict) -> None:
    qid = q.get("id", "?")
    for k in REQUIRED:
        assert k in q, f"{qid} 缺字段 {k}"
    assert re.match(rf"^{product}-[A-Z]{{3}}-\d{{3}}$", q["id"]), qid
    assert q["product"] == product and q["domain"] in data["domains"]
    assert q["control_ids"] and all(c in CONTROLS for c in q["control_ids"]), f"{qid} 控制项不在清单里"
    assert all(CONTROLS[c]["product"] == product for c in q["control_ids"]), f"{qid} 控制项不属于 {product}"
    assert all(CONTROLS[c]["domain"] == q["domain"] for c in q["control_ids"]), f"{qid} 控制项与题的域不一致"
    cps = q["checkpoints"]
    assert cps and len(set(cps)) == len(cps), f"{qid} 检查点为空或重复"
    assert all(cp in CHECKPOINTS for cp in cps), f"{qid} 检查点不在清单里：{[c for c in cps if c not in CHECKPOINTS]}"
    owners = {CHECKPOINTS[cp] for cp in cps}
    assert owners == set(q["control_ids"]), f"{qid} 检查点与 control_ids 不对应（每个控制项至少挂一个检查点）"
    assert q["kind"] in {"closed", "open"}
    assert q["stem"].strip() and q["stem"].strip()[-1] in "？。）"
    assert q["problem"] is False, "v1 的 problem 标记只由运行时置位"
    assert q["depth"] and set(q["depth"]) <= DEPTHS
    assert q["respondent_roles"] and set(q["respondent_roles"]) <= ROLES
    assert isinstance(q["followups"], list) and isinstance(q["prefill"], list)
    assert q["prefill"] and {p["source"] for p in q["prefill"]} <= PREFILL
    assert q["channel"] in CHANNELS, f"{qid} 获取渠道不对"
    assert (q["kind"] == "open") == (q["channel"] == "discussion"), f"{qid} 开放题且只有开放题走开放讨论"
    if q["channel"] == "prefill":
        assert {p["source"] for p in q["prefill"]} - {"none", "self_assessment"}, f"{qid} 资料预填题须有资料来源"
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
    text = q["stem"] + "".join(q["followups"]) + "".join(v["stem"] + "".join(v["followups"])
                                                       for v in q.get("industry_variants", []))
    assert not any(w in text for w in BANNED), f"{qid} 含不合适措辞"


def test_control_ids_follow_kb_rule() -> None:
    """B15 对齐：题目控制项编号过知识库同一规则（items.check_control_ids）规整后不变。"""
    from app.kb.items import check_control_ids

    for path in FILES:
        for q in _load(path)["questions"]:
            assert check_control_ids(q["control_ids"]) == q["control_ids"], f"{q['id']} 控制项编号需规整（大写、去重、≤20）"
