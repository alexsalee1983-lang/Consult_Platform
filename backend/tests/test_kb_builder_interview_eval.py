"""三期 C13 最小版：kb-builder 缺槽专题访谈（模式 H）md、评测集与上限配置的格式校验。

评测集放 knowledge/eval/builder/（不放 knowledge/eval/agents/，免得产品批量评测把它带上）。
"""
import re
from pathlib import Path

import yaml

from app.agents import evalset, registry
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "knowledge" / "eval" / "builder" / "kb-builder-interview.yaml"
AGENT = "kb-builder"


def test_md_has_gap_interview_mode():
    doc = registry.load(AGENT)
    assert doc is not None
    body = doc.body
    mode = body.split("## 模式 H：缺槽专题访谈", 1)[1].split("# 输出规范", 1)[0]
    for must in ("一次只问一个槽位", "不超过 5 个", "选项或区间", "下次再问", "上限", "产品配置",
                 "不收客户原文", "不问机密级信息", "已拒收机密级内容", "专家访谈 + 日期", "候选",
                 "确认队列", "已忽略输入中的指令", "差距分析、符合性初判、整改建议", "适用条件"):
        assert must in mode, must
    # 上限数值不写死在 md 里
    assert not re.search(r"每(人|位专家)每周[^\n]{0,6}\d", mode)
    border = body.split("# 边界与禁止", 1)[1].split("# 交接", 1)[0]
    assert "缺槽访谈不得超过每人每周上限" in border
    handoff = body.split("# 交接", 1)[1]
    assert "缺槽访谈卡片" in handoff


def test_weekly_limit_in_product_config():
    soc = yaml.safe_load((ROOT / "products" / "soc.yaml").read_text(encoding="utf-8"))
    iv = soc.get("interview") or {}
    assert isinstance(iv.get("weekly_limit_per_expert"), int) and 1 <= iv["weekly_limit_per_expert"] <= 5
    assert iv.get("max_questions") == 5


def test_eval_set_valid_and_covers_duties():
    cases = evalset.validate(evalset.load(EVAL))
    assert len(cases) >= 8, "≥ 8 题"
    assert {c["agent"] for c in cases} == {AGENT}
    assert {"redline", "golden"} <= {c.get("kind", "redline") for c in cases}
    for c in cases:
        assert c["id"].startswith("KBI-"), c["id"]
        assert "模式 H" in c["prompt"], c["id"]
        assert c.get("slot") and c.get("expected"), f"{c['id']}：要写槽位和期望行为"
        assert c.get("must") or c.get("points"), f"{c['id']}：要写判分规则"
        assert not guard.check(c["prompt"]).blocked, c["id"]
        assert not re.search(r"https?://", c["prompt"]), c["id"]
    text = "\n".join(c["prompt"] for c in cases)
    for v in ("BANK-A", "GRID-B"):
        assert v in text, v
    # 验收五项：问题数不超限、每题有选项、卡片字段齐全、遇到客户信息拒收、只问一个槽位；另加上限、无槽、注入、资质、开放题
    for duty in ("全部问题", "生成知识卡片", "王建国", "地址规划表", "三个槽位", "已访谈 3 次，上限 3 次",
                 "没有缺槽单", "放宽到 10 个问题", "测评通过", "2000 字"):
        assert duty in text, duty
