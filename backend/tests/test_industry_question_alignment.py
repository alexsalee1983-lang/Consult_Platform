"""行业包 × 题库对齐（三期 B14 × E1）：题库 industry_variants 的行业代码须是已有行业包，且该行业包覆盖题目所属产品。

题库（question_bank/*.yaml，#223）未合并前本测试跳过。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

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


# ---- 行业调研题 × 控制项检查点（三期 B14 × B19）----
# 行业调研题在私有资料仓 Consult_Knowledge 的 drafts/ind/ind-*-tpl-*-questions.md（知识内容不进代码仓），
# 每题在追问后写一行“挂：SOC.TEC.01.a、SOC.BNK.01.b”。资料仓不在本机（如 CI）时跳过；
# 资料仓位置取环境变量 CONSULT_KNOWLEDGE_DIR，默认与代码仓同级的 Consult_Knowledge。

_QNUM = re.compile(r"^\d+\.\s")
_HANG = re.compile(r"^\s+挂：(.+)$")


def _knowledge_dir() -> Path:
    return Path(os.environ.get("CONSULT_KNOWLEDGE_DIR") or REPO.parent / "Consult_Knowledge")


def _checkpoints() -> dict[str, tuple[str, list[str]]]:
    data = yaml.safe_load((REPO / "knowledge" / "controls" / "v0.yaml").read_text(encoding="utf-8"))
    out: dict[str, tuple[str, list[str]]] = {}
    for c in data["controls"]:
        for cp in c.get("checkpoints") or []:
            out[f"{c['id']}.{cp['id']}"] = (c["product"], list(c["industries"]))
    return out


def _question_refs(text: str) -> list[tuple[str, list[str]]]:
    """返回 [(题号行, [检查点编号…])]；只看“## 题目”一节。"""
    body = text.split("## 题目", 1)[1].split("\n## ", 1)[0]
    items: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        if _QNUM.match(line):
            items.append((line.split("：", 1)[0], []))
        elif (m := _HANG.match(line)) and items:
            items[-1][1].extend(x.strip() for x in m.group(1).split("、") if x.strip())
    return items


def test_industry_questions_hang_on_checkpoints() -> None:
    files = sorted((_knowledge_dir() / "drafts" / "ind").glob("ind-*-tpl-*-questions.md"))
    if not files:
        pytest.skip("资料仓不在本机")
    cps = _checkpoints()
    bad: list[str] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        meta = yaml.safe_load(text.split("---", 2)[1])
        product, industries = meta["product"], set(meta["industries"])
        items = _question_refs(text)
        if not items:
            bad.append(f"{f.name}：没有题目")
        for qid, refs in items:
            where = f"{f.name} {qid}"
            if not refs:
                bad.append(f"{where}：未挂检查点")
            for r in refs:
                if r not in cps:
                    bad.append(f"{where}：检查点 {r} 不在控制项清单")
                    continue
                p, inds = cps[r]
                if p != product:
                    bad.append(f"{where}：{r} 属产品 {p}，题目是 {product}")
                if "通用" not in inds and not industries & set(inds):
                    bad.append(f"{where}：{r} 是 {'、'.join(inds)} 专属项")
    assert bad == [], bad
