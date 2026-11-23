"""三期 C6①：咨询协调 Agent 样板 md（咨询师 + 项目交付管理合并，Agent 收敛为 9 个的第一步）。

只查 md 与评测集本身；注册表、授权矩阵、旧名兼容的平台侧改动在线 1 的 C6-P。
"""
from pathlib import Path

from app.agents import evalset, registry
from app.project import quality
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "agents" / "roles"
COORD_EVAL = ROOT / "knowledge" / "eval" / "agents" / "coord.yaml"
CODE = "consult-coordinator"
OLD = ("consultant-lead", "delivery-pm")


def _doc() -> registry.AgentDoc:
    doc = registry.load(CODE)
    assert doc is not None, "agents/roles/consult-coordinator.md 应存在且 name 与代号一致"
    return doc


def test_coordinator_md_loads_with_standard_tier_and_known_tools():
    doc = _doc()
    assert "咨询协调" in doc.display_name
    assert doc.level == "standard" and doc.level_set
    assert doc.md_tools, "md 应写明工具"
    unknown = [t for t in doc.md_tools if t not in registry.TOOL_NAMES]
    assert not unknown, f"md 里的工具名不在注册表 TOOL_NAMES：{unknown}"
    # 合并后的工具是两个旧 Agent 的并集，不新增写入类工具
    assert {registry.TOOL_NAMES[t] for t in doc.md_tools} == {
        "kb.search", "project.read", "kb.mapping", "agent.handoff", "doc.generate"}


def test_coordinator_md_covers_both_old_agents_modes():
    body = _doc().body
    for word in ("分派", "【交接】", "项目启动", "范围确认单", "周报", "阶段确认", "范围变更", "会议纪要", "回流单"):
        assert word in body, f"咨询协调 md 缺少“{word}”"


def test_coordinator_md_respects_qualification_boundary_and_kb_boundary():
    body = _doc().body
    assert quality.qualification_hits(body) == 0, "md 正文不得出现未否定的测评、认证结论用语"
    assert "不写知识库" in body or "不能写知识库" in body
    assert "去标识" in body


def test_coordinator_handoff_targets_exist_and_old_md_kept_for_compat():
    text = (ROLES / f"{CODE}.md").read_text(encoding="utf-8")
    meta, _ = registry._split_frontmatter(text)
    targets = [str(t) for t in meta.get("handoff_to") or []]
    assert targets and CODE not in targets
    missing = [t for t in targets if not (ROLES / f"{t}.md").exists()]
    assert not missing, f"handoff_to 里的 Agent 没有 md：{missing}"
    assert not set(OLD) & set(targets), "不转交给被合并的旧 Agent"
    assert meta.get("replaces") == list(OLD)
    for old in OLD:                                  # 旧名兼容：进行中项目按立项冻结版本跑，旧 md 暂不删
        assert registry.load(old) is not None


def test_coord_eval_set_valid_and_covers_both_old_agents():
    cases = evalset.validate(evalset.load(COORD_EVAL))
    agents = {c["agent"] for c in cases}
    assert agents == set(OLD), "基线题按旧代号写，C6-P 登记新 Agent 后替换代号重跑做前后对比"
    kinds = {c.get("kind", "redline") for c in cases}
    assert {"redline", "golden"} <= kinds
    for old in OLD:
        assert sum(c["agent"] == old for c in cases) >= 3, old
    for c in cases:
        assert not guard.check(c["prompt"]).blocked, c["id"]
        assert any(k in c["prompt"] for k in ("BANK-A", "GRID-B", "MFG-C")), c["id"]
