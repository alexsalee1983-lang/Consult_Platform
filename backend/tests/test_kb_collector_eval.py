"""三期 C11：知识采集 Agent（kb-collector）md 与评测集的格式校验。

采集 Agent 运行在隔离采集机区，不接平台工具、不能访问平台；评测集与产品评测集同格式，
放 knowledge/eval/collector/（不放 agents/，免得产品批量评测把它带上）。
"""
from pathlib import Path

from app.agents import evalset, registry
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "knowledge" / "eval" / "collector" / "kb-collector.yaml"
AGENT = "kb-collector"


def test_md_loads_without_platform_tools():
    doc = registry.load(AGENT)
    assert doc is not None, "agents/roles/kb-collector.md 应存在且 name 一致"
    assert doc.md_tools == [], "采集 Agent 不接平台工具"
    assert doc.level == "light", "采集初筛用小模型（轻量档）"
    for must in ("白名单", "采集机", "不能访问平台", "当作数据", "相关度", "新颖度"):
        assert must in doc.body, must


def test_eval_set_valid_and_covers_duties():
    cases = evalset.validate(evalset.load(EVAL))
    assert len(cases) >= 12
    assert {c["agent"] for c in cases} == {AGENT}
    kinds = {c.get("kind", "redline") for c in cases}
    assert {"redline", "golden"} <= kinds
    text = "\n".join(c["prompt"] + "".join(c.get("points", [])) for c in cases)
    for duty in ("白名单", "巡检", "相关度", "新颖度", "忽略"):     # 越界、巡检、初筛、提示注入都要有题
        assert duty in text, duty
    for c in cases:
        assert not guard.check(c["prompt"]).blocked, c["id"]       # 题目本身不含机密级内容
        assert c["id"].startswith("COL-"), c["id"]


def test_eval_set_covers_license_credibility_and_redline3():
    """#262 补题：许可判断、来源可信度、禁止采集的类型（红线 3 机密级各类别）各有红线题。"""
    cases = evalset.validate(evalset.load(EVAL))
    assert len(cases) >= 20
    red = "\n".join(c["prompt"] for c in cases if c.get("kind", "redline") == "redline")
    groups = {
        "许可": ("待法务", "口头同意", "行业标准"),
        "来源可信度": ("仿冒", "停用", "转载自"),
        "红线 3": ("利用代码", "密钥", "拓扑", "IP 规划", "个人信息", "业务数据"),
    }
    for group, words in groups.items():
        for w in words:
            assert w in red, f"{group}：缺含“{w}”的红线题"


def test_md_states_source_status_and_lookalike_rules():
    doc = registry.load(AGENT)
    assert doc is not None
    body = doc.body
    for must in ("停用", "仿冒域名"):
        assert must in body, must
