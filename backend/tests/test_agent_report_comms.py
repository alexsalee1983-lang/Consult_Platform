"""三期 C6③：报告与客户沟通 Agent md（报告与汇报 + 质量评审拆出的异议应答，Agent 收敛为 9 个的第三步）。

只查 md 与评测集本身；注册表、授权矩阵、旧名兼容的平台侧改动在线 1 的 C6-P。
"""
from pathlib import Path

from app.agents import evalset, registry
from app.project import quality
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "agents" / "roles"
EVAL = ROOT / "knowledge" / "eval" / "agents" / "rcomm.yaml"
CODE = "report-comms"


def _doc(code: str = CODE) -> registry.AgentDoc:
    doc = registry.load(code)
    assert doc is not None, f"agents/roles/{code}.md 应存在且 name 与代号一致"
    return doc


def _meta(code: str) -> dict:
    meta, _ = registry._split_frontmatter((ROLES / f"{code}.md").read_text(encoding="utf-8"))
    return meta


def test_md_loads_with_union_tools_and_no_write_tool():
    doc = _doc()
    assert "报告与客户沟通" in doc.display_name
    unknown = [t for t in doc.md_tools if t not in registry.TOOL_NAMES]
    assert not unknown, f"md 里的工具名不在注册表 TOOL_NAMES：{unknown}"
    # 报告与汇报的工具 + 异议应答要用的依据查询；不新增写入类工具
    old = {registry.TOOL_NAMES[t] for t in _doc("report-writer").md_tools}
    assert {registry.TOOL_NAMES[t] for t in doc.md_tools} == old | {"basis.lookup"}
    assert _meta(CODE)["knowledge_access"]["write"] == []


def test_md_keeps_report_writer_product_sections_and_rules():
    body = _doc().body
    old = _doc("report-writer").body
    for code in ("SOC", "IRE", "DSM", "ISO", "NSG"):
        head = f"## 产品：{code}"
        assert head in body, code
        sec = lambda t: t[t.index(head):t.index("\n#", t.index(head) + 1)]  # noqa: E731
        assert sec(body) == sec(old), f"{code} 产品段应原样搬入"
    soc, _ = registry.select_body(body, "SOC")
    assert "模式 B：异议应答" in soc and "## 产品：SOC" in soc and "## 产品：ISO" not in soc
    for word in ("结论先行", "发现卡片", "四要素", "对客稿只写给客户看的内容",
                 "客户原话属于资质用语，不能写入正文，已改写为差距分析和整改建议的说法。"):
        assert word in body, word


def test_md_takes_over_objection_mode_from_qa_reviewer():
    body = _doc().body
    for word in ("模式 A", "模式 B", "认同点", "澄清", "依据和数据", "折中方案", "下一步",
                 "口头版", "书面版", "新异议，建议入库", "角色模拟要点", "没有替公司做出承诺"):
        assert word in body, f"报告与客户沟通 md 缺少“{word}”"
    assert "不写知识库" in body and "不评审" in body


def test_qa_reviewer_only_reviews_now():
    qa = _doc("qa-reviewer")
    assert qa.display_name == "质量评审 Agent" and qa.md_version.startswith("0.2")
    assert "模式二" not in qa.body and "口头版" not in qa.body and "书面版" not in qa.body
    for word in ("评审维度", "严重程度", "客户可能的挑战", "评审结论的判定规则", "报告与客户沟通 Agent"):
        assert word in qa.body, word
    targets = [str(t) for t in _meta("qa-reviewer").get("handoff_to") or []]
    assert CODE in targets and not [t for t in targets if not (ROLES / f"{t}.md").exists()]


def test_qualification_boundary_not_loosened():
    for code in (CODE, "qa-reviewer"):
        text = (ROLES / f"{code}.md").read_text(encoding="utf-8")
        bad = [ln for ln in text.splitlines() if quality.qualification_hits(evalset._unquoted(ln))]
        assert not bad, (code, bad)


def test_handoff_targets_exist_and_old_md_kept_for_compat():
    meta = _meta(CODE)
    targets = [str(t) for t in meta.get("handoff_to") or []]
    assert targets and CODE not in targets and "report-writer" not in targets
    assert not [t for t in targets if not (ROLES / f"{t}.md").exists()]
    assert meta.get("replaces") == ["report-writer"]
    assert registry.load("report-writer") is not None   # 旧名兼容：进行中项目按立项冻结版本跑，旧 md 暂不删


def test_eval_set_valid_and_after_codes():
    cases = evalset.validate(evalset.load(EVAL))
    assert {c["agent"] for c in cases} == {"report-writer", "qa-reviewer"}, "基线按旧代号写"
    assert {c["after"] for c in cases} == {CODE, "qa-reviewer"}
    assert all(registry.load(c["after"]) is not None for c in cases)
    objection = [c for c in cases if c["agent"] == "qa-reviewer" and c["after"] == CODE]
    assert len(objection) >= 3 and sum(c["agent"] == "report-writer" for c in cases) >= 3
    assert any(c["after"] == "qa-reviewer" for c in cases), "留一道评审题验证质量评审不退化"
    assert {"redline", "golden"} <= {c.get("kind", "redline") for c in cases}
    for c in cases:
        assert not guard.check(c["prompt"]).blocked, c["id"]
        assert any(k in c["prompt"] for k in ("BANK-A", "GRID-B", "MFG-C")), c["id"]
