"""三期 C12①：kb-builder 模式 I（提炼与挂载）md 草稿与评测集最小样例。

只查 md 与评测集本身；kb-builder 读 B19 控制项、OLIR / CTID 导入器等平台侧改动在 C12-P（线 1）。
"""
from pathlib import Path

from app.agents import evalset, registry
from app.project import quality
from app.services import guard

ROOT = Path(__file__).resolve().parents[2]
EXTRACT_EVAL = ROOT / "knowledge" / "eval" / "agents" / "extract.yaml"
IMPORT_DOC = ROOT / "docs" / "权威映射导入说明.md"
CODE = "kb-builder"
RELATIONS = ("等价", "子集", "超集", "交集")          # 与 B19 app/kb/controls.RELATIONS 同一套（#288）


def _mode_i() -> str:
    doc = registry.load(CODE)
    assert doc is not None
    body = doc.body
    assert "## 模式 I：提炼与挂载" in body
    return body.split("## 模式 I：提炼与挂载", 1)[1].split("\n# ", 1)[0]


def test_mode_i_has_fixed_structure_and_four_merge_verdicts():
    part = _mode_i()
    for t in ("REF", "MTD", "TPL", "MAP", "PIT", "OBJ"):
        assert f"| {t} " in part, f"固定结构表缺类型 {t}"
    for v in ("重复", "补充", "冲突", "替代"):
        assert f"| {v} |" in part, f"合并结论缺“{v}”"
    assert "不自行取舍" in part
    assert "提炼与挂载单" in part


def test_mode_i_mapping_uses_b19_relations_confidence_and_cross_check():
    part = _mode_i()
    for r in RELATIONS:
        assert f"**{r}**" in part
    for word in ("NIST IR 8477", "OLIR", "CTID", "置信度", "交叉验证", "强度"):
        assert word in part, word
    assert "部分覆盖" not in part, "不再用二期 kb_mappings 的旧取值"


def test_mode_i_mounts_to_controls_without_inventing_codes_and_keeps_boundaries():
    part = _mode_i()
    assert "控制项" in part and "缺控制项" in part and "不新造编号" in part
    assert quality.qualification_hits(_mode_i()) == 0, "模式 I 不得出现未否定的测评、认证结论用语"
    doc = registry.load(CODE)
    assert doc is not None
    assert set(doc.md_tools) <= set(registry.TOOL_NAMES), "md 工具名须在注册表里"


def test_extract_eval_set_valid_virtual_and_covers_verdicts():
    cases = evalset.validate(evalset.load(EXTRACT_EVAL))
    assert {c["agent"] for c in cases} == {CODE}
    assert {c.get("kind", "redline") for c in cases} >= {"redline", "golden"}
    joined = "\n".join(c["prompt"] for c in cases)
    for word in ("模式 I", "SOC.ORG.01", "SOC.PRC.02"):
        assert word in joined
    golden = "\n".join("|".join(c.get("points", [])) for c in cases)
    for v in ("补充", "替代", "重复"):
        assert v in golden, f"黄金题未覆盖合并结论“{v}”"
    assert any("冲突" in "|".join(c.get("must", [])) for c in cases), "红线题未覆盖冲突"
    for c in cases:                                  # 题目本身要能发给模型：不含机密级内容
        assert not guard.check(c["prompt"]).blocked, c["id"]


def test_import_doc_states_formats_and_keeps_originals_out_of_repo():
    text = IMPORT_DOC.read_text(encoding="utf-8")
    for word in ("OLIR", "CTID", "Consult_Knowledge", "等价", "子集", "超集", "交集", "置信度"):
        assert word in text, word
    assert quality.qualification_hits(text) == 0


def test_mode_i_refuses_conclusion_wording_and_quotes_when_restating():
    """#334 测试机 EXT-R04：用户要求写“满足等保三级、通过测评”时，模型复述成引号外的结论用语。"""
    part = _mode_i()
    assert "整段加引号" in part, "复述用户要求的结论用语须整段加引号"
    assert "差距分析" in part and "符合性初判" in part and "整改建议" in part
    assert "改写为" in part, "须给出可直接用的替代写法"
