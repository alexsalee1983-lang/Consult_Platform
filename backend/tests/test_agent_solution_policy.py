"""三期 C6②：方案与制度设计 Agent md（方案设计 + 制度起草合并，Agent 收敛为 9 个的第二步）。

只查 md 与评测集本身；注册表、授权矩阵、旧名兼容的平台侧改动在线 1 的 C6-P。
"""
from pathlib import Path

from app.agents import evalset, registry
from app.project import quality
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "agents" / "roles"
EVAL = ROOT / "knowledge" / "eval" / "agents" / "solpol.yaml"
CODE = "solution-policy-designer"
OLD = ("solution-designer", "policy-drafter")


def _doc() -> registry.AgentDoc:
    doc = registry.load(CODE)
    assert doc is not None, "agents/roles/solution-policy-designer.md 应存在且 name 与代号一致"
    return doc


def test_md_loads_with_reasoning_tier_and_union_tools():
    doc = _doc()
    assert "方案与制度设计" in doc.display_name
    assert doc.level_set and doc.level == registry.load("solution-designer").level
    unknown = [t for t in doc.md_tools if t not in registry.TOOL_NAMES]
    assert not unknown, f"md 里的工具名不在注册表 TOOL_NAMES：{unknown}"
    # 合并后的工具是两个旧 Agent 的并集，不新增写入类工具
    assert {registry.TOOL_NAMES[t] for t in doc.md_tools} == {
        "kb.search", "basis.lookup", "project.read", "kb.mapping", "doc.generate"}


def test_md_keeps_product_sections_and_service_modules():
    doc = _doc()
    old = registry.load("solution-designer")
    assert doc.service_modules == old.service_modules
    for code in ["未就绪", *old.service_modules]:
        assert f"## 产品：{code}" in doc.body, code
    # 平台按项目只拼入本产品段，模式 D（制度起草）属通用部分，任何产品都在
    soc, picked = registry.select_body(doc.body, "SOC")
    assert "模式 D" in soc and "## 产品：SOC" in soc and "## 产品：ISO" not in soc, picked


def test_md_covers_both_old_agents_modes():
    body = _doc().body
    for word in ("模式 A", "模式 B", "模式 C", "模式 D", "确认卡", "设计决定表",
                 "制度母版", "裁剪", "四级文件", "RACI", "落地风险提示", "四件套", "度量指标"):
        assert word in body, f"方案与制度设计 md 缺少“{word}”"


def test_md_respects_qualification_boundary_and_kb_boundary():
    body = _doc().body
    # 原方案设计 md 的禁用语清单（“不写‘认证通过’‘测评合格’……”）否定词超出检测窗口，计 2 处；合并不得新增
    inherited = quality.qualification_hits(registry.load("solution-designer").body)
    assert quality.qualification_hits(body) <= inherited <= 2, "md 正文不得新增未否定的测评、认证结论用语"
    assert "不写知识库" in body
    assert "制度起草 Agent" not in body and "方案设计 Agent" not in body, "旧 Agent 名已并入，不再转交给自己"


def test_handoff_targets_exist_and_old_md_kept_for_compat():
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


def test_eval_set_valid_and_covers_both_old_agents():
    cases = evalset.validate(evalset.load(EVAL))
    assert {c["agent"] for c in cases} == set(OLD), "基线题按旧代号写，C6-P 登记新 Agent 后替换代号重跑"
    assert {"redline", "golden"} <= {c.get("kind", "redline") for c in cases}
    for old in OLD:
        assert sum(c["agent"] == old for c in cases) >= 3, old
    for c in cases:
        assert not guard.check(c["prompt"]).blocked, c["id"]
        assert any(k in c["prompt"] for k in ("BANK-A", "GRID-B", "MFG-C")), c["id"]
