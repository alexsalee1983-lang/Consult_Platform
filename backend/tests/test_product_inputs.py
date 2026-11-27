"""产品输入配置自检（四期 C21）：资料需求清单、受访角色画像、客户自评问卷。

字段见 docs/接口约定.md 第二节“inputs”与 issue #471 评论；全景客户行、答案池（A25/E3）按它取数。
受访角色与预填来源和题库（docs/题库格式.md）共用同一词表。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.test_question_bank import DEPTHS, PREFILL, ROLES

REPO = Path(__file__).resolve().parents[2]
PRODUCTS = ["soc", "ire", "dsm", "iso", "nsg"]


def _load(stem: str) -> dict:
    return yaml.safe_load((REPO / "products" / f"{stem}.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("stem", PRODUCTS)
def test_inputs(stem: str) -> None:
    cfg = _load(stem)
    inputs = cfg.get("inputs")
    assert inputs, f"{stem} 缺 inputs"
    steps = {s["key"] for s in cfg["chain"]}
    deliverables = {d["code"] for d in cfg["deliverables"]}
    domains = set(cfg["baseline"]["domains"])

    personas = inputs["personas"]
    assert [p["id"] for p in personas] == [f"P-{i:02d}" for i in range(1, len(personas) + 1)]
    roles = [p["role"] for p in personas]
    assert len(set(roles)) == len(roles) and set(roles) <= ROLES, roles
    for p in personas:
        assert p["focus"] and set(p["focus"]) <= domains, p["id"]
        assert p["depth"] and set(p["depth"]) <= DEPTHS
        assert isinstance(p["minutes"], int) and 15 <= p["minutes"] <= 120
    assert set().union(*(p["focus"] for p in personas)) == domains, "每个控制域至少有一个受访角色"

    materials = inputs["materials"]
    assert len(materials) >= 5
    assert [m["id"] for m in materials] == [f"M-{i:02d}" for i in range(1, len(materials) + 1)]
    for m in materials:
        assert m["name"].strip() and m["provider"] in roles, m["id"]
        assert m["blocks"]["step"] in steps, m["id"]
        assert m["blocks"]["deliverables"] and set(m["blocks"]["deliverables"]) <= deliverables, m["id"]
        assert m["prefill"] in PREFILL - {"none", "self_assessment"}, m["id"]
        assert isinstance(m["required"], bool)
        # 红线 3：资料需求只要统计与配置摘要，不索取机密级内容
        assert not re.search(r"口令|密钥|漏洞细节|完整拓扑|IP ?规划|样本", m["name"]), m["id"]

    q = inputs["questionnaire"]
    assert q["channels"] == ["self"] and q["kinds"] == ["closed"]
    assert q["domains"] and set(q["domains"]) <= domains
    assert 10 <= q["max_questions"] <= 60 and 1 <= q["due_days"] <= 7
    assert "anchors" not in q, "评分锚定不进自评包"
