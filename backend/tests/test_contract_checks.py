"""合同要点卡检查规则与澄清题选项自检（四期 C23，草稿待产品专家复核）。

方案见 docs/交付链输入输出方案.md 4.1；字段说明见 docs/接口约定.md 第二节“合同要点卡检查与澄清题”。
A40 按 products/contract/checks.yaml 做规则检查（只显示例外），按 products/contract/<产品>.yaml 给交付项目经理 Agent
出题时的候选选项；选项取自同产品黄金样例（资料仓 Consult_Knowledge drafts/golden，按 golden_id 引用）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "products" / "contract"
PRODUCTS = ["soc", "ire", "dsm", "iso", "nsg"]
ACTIONS = {"block", "clarify", "pm"}          # 阻断保存 / 转澄清题 / 进“待我处理”
STATUS = {"已核对", "合同未写明", "待澄清", "合同外新增"}
GATES = ["启动", "调研", "分析", "设计", "验收"]
QUALIF = re.compile(r"测评结论|密评结论|认证结论|通过认证|合规结论|保证通过|确保通过")
REDLINE = re.compile(r"口令|密钥|漏洞细节|完整拓扑|IP ?规划|样本数据")
REAL_NAME = re.compile(r"银行股份|电网公司|集团有限|有限公司")   # 只用虚拟客户代号


def _load(name: str) -> dict:
    return yaml.safe_load((BASE / f"{name}.yaml").read_text(encoding="utf-8"))


def _deliverables(stem: str) -> set[str]:
    cfg = yaml.safe_load((REPO / "products" / f"{stem}.yaml").read_text(encoding="utf-8"))
    return {d["code"] for d in cfg["deliverables"]}


def _clean(text: str, where: str) -> None:
    assert not REDLINE.search(text), f"{where} 触及红线 3"
    assert not REAL_NAME.search(text), f"{where} 疑似真实客户名称"


def test_fields_and_checks() -> None:
    cfg = _load("checks")
    assert set(cfg["status"]) == STATUS
    keys = [f["key"] for f in cfg["fields"]]
    assert len(set(keys)) == len(keys)
    for f in cfg["fields"]:
        assert f["label"].strip() and isinstance(f.get("required", False), bool), f["key"]
    for must in ("goal", "in_scope", "out_of_scope", "deliverables", "acceptance", "period", "payment", "tier",
                 "retention", "kb_consent", "confidentiality", "roles"):
        assert must in keys, f"要点卡缺 {must}（方案 4.1 表）"

    checks = cfg["checks"]
    assert [c["id"] for c in checks] == [f"K-{i:02d}" for i in range(1, len(checks) + 1)]
    for c in checks:
        assert c["action"] in ACTIONS, c["id"]
        assert c["name"].strip() and c["message"].strip(), c["id"]
        assert set(c["fields"]) <= {*keys, "*"}, c["id"]
        assert set(c) <= {"id", "name", "fields", "action", "match", "params", "message", "fix", "question"}, c["id"]
        if "match" in c:
            re.compile(c["match"])
        if c["action"] == "clarify":
            assert c.get("question", "").endswith("？"), f"{c['id']} 转澄清题须给题干"
        _clean(c["message"], c["id"])
    blocks = [c for c in checks if c["action"] == "block"]
    assert [c["id"] for c in blocks] == ["K-01"], "只有资质边界一条阻断保存（方案 4.1）"
    k01 = re.compile(blocks[0]["match"])
    for bad in ("出具等保测评结论", "密评结论", "协助客户通过认证", "认证结论"):
        assert k01.search(bad), bad
    for ok in ("差距分析", "符合性初判", "整改建议", "认证准备路线图"):
        assert not k01.search(ok), ok
    names = " ".join(c["name"] for c in checks)
    for topic in ("资质边界", "验收", "付款", "周期", "交付件", "档位", "保留期", "未写明"):
        assert topic in names, f"缺体检项：{topic}"


def _check_questions(qs: list[dict], prefix: str, keys: set[str], dcodes: set[str], golden: str | None) -> None:
    assert [q["id"] for q in qs] == [f"{prefix}{i:02d}" for i in range(1, len(qs) + 1)]
    asks = [q["ask"] for q in qs]
    assert len(set(asks)) == len(asks), f"{prefix} 题干重复"
    for q in qs:
        where = q["id"]
        assert set(q) <= {"id", "field", "ask", "when", "affects", "gate", "options"}, where
        assert q["field"] in keys, where
        assert q["ask"].endswith("？") and len(q["ask"]) <= 60, where
        assert q["gate"] in GATES, where
        assert q["affects"] and set(q["affects"]) <= dcodes, where
        opts = q["options"]
        assert 2 <= len(opts) <= 3, f"{where} 每题 2～3 个选项"
        rec = [o for o in opts if o.get("recommended")]
        assert len(rec) == 1, f"{where} 有且只有一个推荐项"
        assert rec[0].get("reason", "").strip(), f"{where} 推荐项须写理由"
        texts = [o["text"] for o in opts]
        assert len(set(texts)) == len(texts), where
        for o in opts:
            assert set(o) <= {"text", "recommended", "reason", "golden"}, where
            assert o["text"].strip() and len(o["text"]) <= 60, where
            assert not QUALIF.search(o["text"] + o.get("reason", "")), f"{where} 越过资质边界"
            _clean(o["text"] + o.get("reason", ""), where)
            if golden:
                for g in o.get("golden", []):
                    assert re.fullmatch(rf"{golden}-G\d{{2}}", g), f"{where} 黄金样例编号 {g}"
            else:
                assert not o.get("golden"), f"{where} 通用题不引用单产品样例"
        if golden:
            assert rec[0].get("golden"), f"{where} 推荐项须引同产品黄金样例（取自黄金样例）"
        _clean(q["ask"], where)


def test_common_questions() -> None:
    cfg = _load("checks")
    keys = {f["key"] for f in cfg["fields"]}
    every = {"D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"}
    _check_questions(cfg["questions"], "Q-", keys, every, None)
    asked = {q["field"] for q in cfg["questions"]}
    for f in cfg["fields"]:
        if f.get("required"):
            assert f["key"] in asked or f.get("pm_only"), f"必填字段 {f['key']} 未写明时没有兜底题"


@pytest.mark.parametrize("stem", PRODUCTS)
def test_product_questions(stem: str) -> None:
    cfg = _load(stem)
    code = stem.upper()
    assert cfg["product"] == code
    keys = {f["key"] for f in _load("checks")["fields"]}
    w = cfg["weeks"]
    assert 0 < w["min"] <= w["typical"] <= 52 and w["source"].strip(), "工期对照（K-04）"
    assert set(cfg["deliverables_min"]) <= _deliverables(stem) and "D1" in cfg["deliverables_min"]
    qs = cfg["questions"]
    assert 5 <= len(qs) <= 12, "每个产品 5～12 道产品题，避免清单过长"
    _check_questions(qs, f"{code}-Q", keys, _deliverables(stem), code)
    assert len({q["affects"][0] for q in qs}) >= 3, "题目应覆盖多个交付件"
