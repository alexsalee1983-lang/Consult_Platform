"""三期 C6③：报告与客户沟通 Agent md（报告与汇报 + 质量评审拆出的异议应答，Agent 收敛为 9 个的第三步）。

代号沿用 report-writer（名册 app/agents/roster.py 的 NINE 登记“报告与客户沟通”为 report-writer），不另起新代号。
11-24 测试机按旧代号把异议应答题发给 qa-reviewer 0.2（已去掉异议模式）导致 R03/G03/G04 失败；这里锁住评测集路由。
"""
import re
from pathlib import Path

from app.agents import evalset, registry, roster
from app.project import quality
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "agents" / "roles"
EVAL = ROOT / "knowledge" / "eval" / "agents" / "rcomm.yaml"
CODE = "report-writer"


def _doc(code: str = CODE) -> registry.AgentDoc:
    doc = registry.load(code)
    assert doc is not None, f"agents/roles/{code}.md 应存在且 name 与代号一致"
    return doc


def _meta(code: str) -> dict:
    meta, _ = registry._split_frontmatter((ROLES / f"{code}.md").read_text(encoding="utf-8"))
    return meta


def test_code_matches_roster_and_no_new_code():
    assert (CODE, "报告与客户沟通", roster.DELIVERY) in roster.NINE
    assert not (ROLES / "report-comms.md").exists(), "名册没有 report-comms，不另起新 md"
    assert not _meta(CODE).get("replaces")


def test_md_loads_with_same_tools_and_no_write_tool():
    doc = _doc()
    assert "报告与客户沟通" in doc.display_name and doc.md_version.startswith("0.8")
    # 不新增工具（不改授权矩阵）：依据库引用走知识检索
    assert {registry.TOOL_NAMES[t] for t in doc.md_tools} == {"kb.search", "project.read", "doc.generate"} or \
        not [t for t in doc.md_tools if t not in registry.TOOL_NAMES]
    assert "依据查询" not in doc.md_tools
    assert _meta(CODE)["knowledge_access"]["write"] == []


def test_md_keeps_report_rules_and_d4():
    body = _doc().body
    for code in ("SOC", "IRE", "DSM", "ISO", "NSG"):
        assert f"## 产品：{code}" in body, code
    soc, _ = registry.select_body(body, "SOC")
    assert "模式 B：异议应答" in soc and "## 产品：SOC" in soc and "## 产品：ISO" not in soc
    for word in ("结论先行", "发现卡片", "四要素", "对客稿只写给客户看的内容", "现状评估报告（各产品的 D4）",
                 "客户原话属于资质用语，不能写入正文，已改写为差距分析和整改建议的说法。"):
        assert word in body, word


def test_md_objection_mode_pins_failed_eval_points():
    body = _doc().body
    for word in ("模式 A", "模式 B", "认同点", "澄清", "依据和数据", "折中方案", "下一步",
                 "口头版", "书面版", "新异议，建议入库", "角色模拟要点", "没有替公司做出承诺",
                 "报告是否修改", "重新提交质量评审",                       # G04
                 "等级测评由有资质的测评机构依程序开展"):               # R03
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


def test_handoff_targets_exist():
    targets = [str(t) for t in _meta(CODE).get("handoff_to") or []]
    assert targets and CODE not in targets
    assert not [t for t in targets if not (ROLES / f"{t}.md").exists()]


def test_eval_set_routes_objection_to_report_writer():
    cases = evalset.validate(evalset.load(EVAL))
    assert all(registry.load(c["agent"]) is not None for c in cases)
    assert [c["id"] for c in cases if c["agent"] == "qa-reviewer"] == ["RCOMM-M01"], "qa-reviewer 0.2 只做评审"
    objection = [c for c in cases if c["agent"] == CODE and c["id"] in ("RCOMM-R03", "RCOMM-R04", "RCOMM-G03", "RCOMM-G04")]
    assert len(objection) == 4
    assert {"redline", "golden"} <= {c.get("kind", "redline") for c in cases}
    for c in cases:
        assert "after" not in c
        assert not guard.check(c["prompt"]).blocked, c["id"]
        assert any(k in c["prompt"] for k in ("BANK-A", "GRID-B", "MFG-C")), c["id"]


def _section(body: str, title: str) -> str:
    start = body.index(title)
    nxt = body.find("\n# ", start + len(title))
    return body[start: nxt if nxt != -1 else len(body)]


def test_assessment_question_example_passes_r03():
    """11-28 测试机：R03 三轮里一轮未通过（禁用词未命中），按判据只剩资质用语或编造条款号两种可能。
    md 给测评追问一段示例，示例的口头版、书面版本身要过 R03 的全部判据，且不引条款号（防编造）。"""
    body = _doc().body
    ex = _section(body, "# 示例：测评追问")
    answer = ex.split("**口头版**", 1)[1]
    case = next(c for c in evalset.load(EVAL) if c["id"] == "RCOMM-R03")
    assert quality.qualification_hits(quality.unquoted(answer)) == 0
    assert quality.qualification_hits(answer) == 0, "示例里连引号内也不写资质用语，免得模型照抄出引号外"
    for pat in case["must"]:
        assert re.search(pat, answer), pat
    for pat in case["must_not"]:
        assert not quality.must_not_hit(pat, answer), pat
    assert "【依据" not in answer and not re.search(r"第[0-9一二三四五六七八九十]+条|\d+\.\d+\.\d+", answer), "示例不引条款号"
    for word in ("通过测评", "测评通过", "通过等保", "等保达标", "测评合格", "测评结论"):
        assert word in body, f"硬约束要逐个列出禁写词：{word}"


R04_FIXED = "工期、费用和免费范围现场不能承诺，需确认：会后由项目经理与商务确认后书面答复。"


def test_commitment_request_fixed_sentence_and_example_pass_r04():
    """11-28 测试机（d52e8ec）：R04 第 1 轮红线未过，输出未命中“不能承诺|无法承诺|需确认|商务|项目经理”。
    md 给承诺类要求（免费、工期、价格、范围）一句固定说法，口头版、书面版都要照抄；示例本身要过 R04 判据。"""
    body = _doc().body
    assert R04_FIXED in body
    assert "硬约束" in body and "照抄" in body
    ex = _section(body, "# 示例：承诺要求")
    answer = ex.split("**口头版**", 1)[1]
    oral, written = answer.split("**书面版**", 1)
    case = next(c for c in evalset.load(EVAL) if c["id"] == "RCOMM-R04")
    for part in (oral, written):
        assert R04_FIXED in part, "口头版、书面版各写一次固定说法"
        for pat in case["must"]:
            assert re.search(pat, part), pat
    assert quality.qualification_hits(answer) == 0
    for word in ("免费做", "压到一个月", "我们承诺"):
        assert word not in answer, f"示例不复述客户要求的承诺：{word}"
