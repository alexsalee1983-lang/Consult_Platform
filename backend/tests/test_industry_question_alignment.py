"""行业包 × 题库对齐（三期 B14 × E1）：题库 industry_variants 的行业代码须是已有行业包，且该行业包覆盖题目所属产品。

题库（question_bank/*.yaml，#223）未合并前本测试跳过。
"""
from __future__ import annotations

import pytest
import yaml

from app.products import config
from tests.test_product_config import REPO


def test_question_bank_variants_match_industry_packs() -> None:
    files = sorted((REPO / "question_bank").glob("*.yaml"))
    if not files:
        pytest.skip("题库未合并")
    cat = config.load(REPO)
    bad: list[str] = []
    for f in files:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        items = data["questions"]
        for q in items:
            product = q.get("product", data.get("product"))
            for v in q.get("industry_variants", []):
                ind = cat.industries.get(v["industry"])
                if ind is None:
                    bad.append(f"{q['id']}：行业 {v['industry']} 没有行业包")
                elif product not in ind.products:
                    bad.append(f"{q['id']}：行业包 {v['industry']} 未覆盖产品 {product}")
    assert bad == [], bad
