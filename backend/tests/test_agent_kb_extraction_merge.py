"""三期 C6④：知识建设与经验萃取合并（kb-builder 0.6，Agent 收敛为 9 个的第四步）。

只查 md 与评测集本身；代号不变，注册表只需同步显示名（线 1 的 C6-P）。
"""
from pathlib import Path

from app.agents import evalset, registry
from app.project import quality
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "agents" / "roles"
EVAL = ROOT / "knowledge" / "eval" / "agents" / "kbx.yaml"
CODE = "kb-builder"
BUILD = ("A 盘点筛选", "B 加工入库", "E 月度治理", "F 知识源清单", "I 提炼与挂载")
EXTRACT = ("C 萃取工作坊", "G 反向提炼", "H 缺槽专题访谈", "D 结项沉淀")


def _doc() -> registry.AgentDoc:
    doc = registry.load(CODE)
    assert doc is not None
    return doc


def _meta() -> dict:
    meta, _ = registry._split_frontmatter((ROLES / f"{CODE}.md").read_text(encoding="utf-8"))
    return meta


def test_renamed_and_versioned_without_new_tools_or_write_scope():
    doc, meta = _doc(), _meta()
    assert doc.display_name == "知识建设与萃取 Agent" and doc.md_version.startswith("0.6")
    assert [registry.TOOL_NAMES[t] for t in doc.md_tools] == ["kb.search", "basis.lookup", "kb.stage_write", "deid.check"]
    assert meta["knowledge_access"]["write"] == ["待入库区"]
    assert "读项目区" not in doc.md_tools and "project.read" not in {registry.TOOL_NAMES[t] for t in doc.md_tools}


def test_overview_lists_every_mode_once_and_each_mode_section_kept():
    body = _doc().body
    overview = body[body.index("# 模式总览"):body.index("# 工作模式与流程")]
    for row in BUILD:
        assert f"| {row} | 建设" in overview, row
    for row in EXTRACT:
        assert f"| {row} | 萃取" in overview, row
    for letter in "ABCDEFGHI":
        assert body.count(f"## 模式 {letter}：") == 1, letter
    assert "合成五期开放" in overview


def test_extraction_common_rules():
    body = _doc().body
    for word in ("只由事件触发", "一律是候选", "第三级审核", "不碰项目区", "结项回流单", "不是作者本人"):
        assert word in body, word
    mode_d = body[body.index("## 模式 D："):body.index("## 模式 E：")]
    assert "结项回流单" in mode_d and "项目区原件不读" in mode_d


def test_boundaries_and_handoff():
    body, meta = _doc().body, _meta()
    targets = [str(t) for t in meta.get("handoff_to") or []]
    assert targets == ["sanitizer", "kb-reviewer"]
    assert not [t for t in targets if not (ROLES / f"{t}.md").exists()]
    for word in ("报告与客户沟通 Agent", "方案与制度设计 Agent", "依据与差距分析 Agent", "不得读取或写入项目区"):
        assert word in body, word
    assert "制度起草 Agent）" not in body


def test_qualification_boundary_not_loosened():
    text = (ROLES / f"{CODE}.md").read_text(encoding="utf-8")
    bad = [ln for ln in text.splitlines() if quality.qualification_hits(evalset._unquoted(ln))]
    assert not bad, bad


def test_eval_set_valid():
    cases = evalset.validate(evalset.load(EVAL))
    assert len(cases) == 9 and {c["agent"] for c in cases} == {CODE}
    kinds = [c.get("kind", "redline") for c in cases]
    assert kinds.count("redline") == 4 and kinds.count("golden") == 5
    assert len({c["id"] for c in cases}) == 9
    for c in cases:
        assert not guard.check(c["prompt"]).blocked, c["id"]
        assert any(k in c["prompt"] for k in ("BANK-A", "GRID-B", "MFG-C")), c["id"]
