"""三期 B27 最小版：来源权威分级推断、检索同分高等级优先、三级标“参考资料”。"""

from __future__ import annotations

import pytest

from app.kb import authority

from .test_kb_search import _publish, _search, people  # noqa: F401, F811（复用检索测试的账号夹具）


@pytest.mark.parametrize(("doc", "code", "src", "want"), [
    ("《中华人民共和国网络安全法》", "", "public", 1),
    ("关键信息基础设施安全保护条例", "国务院令第745号", "public", 1),
    ("银行保险机构信息科技外包风险监管办法（2021 年修订）", "", "public", 1),
    ("关于网络安全事件的通报", "", "public", 1),
    ("网络安全等级保护基本要求", "GB/T 22239-2019", "public", 2),
    ("GB/T 35273-2020 个人信息安全规范", "", "public", 2),
    ("金融行业网络安全等级保护实施指引", "JR/T 0071-2020", "public", 2),
    ("ISO/IEC 27001", "ISO/IEC 27001:2022", "public", 2),
    ("零信任安全白皮书", "", "public", 3),
    ("某厂商 SOC 产品手册", "", "public", 3),
    ("威胁建模方法", "", "public", 3),        # “方法”不是法律
    ("", "", "public", 3),                    # 公开资料无依据字段：参考资料
    ("", "", "expert", None),                 # 专家经验不分级
    ("", "", "internal", None),
])
def test_infer_level(doc, code, src, want):
    assert authority.infer_level(ref_document=doc, ref_code=code, source_type=src) == want


def test_label_and_sort_rank():
    assert authority.label(1) == "法律法规与监管文件"
    assert authority.label(3) == "参考资料"
    assert authority.label(None) == ""
    assert [authority.sort_rank(x) for x in (1, 2, 3, None)] == [1, 2, 3, 4]


def test_search_tie_prefers_higher_authority_and_labels_reference(people):  # noqa: F811
    """正文相同（全文同分）的三条：先发布的是白皮书，检索时法律、标准排在前面；结果带等级与“参考资料”标签。"""
    body = "## 巡检台账\n\n量子密钥分发设备巡检台账保存三年，便于复盘。"
    common = {"layer": "L4", "product": "GEN", "doc_type": "REF", "stage": "通用"}
    paper = _publish(people, "巡检白皮书解读", body, ref=("巡检台账白皮书", "2024", "current"), **common)
    std = _publish(people, "巡检标准解读", body, ref=("GB/T 88001 巡检台账规范", "2020", "current"), **common)
    law = _publish(people, "巡检法律解读", body, ref=("中华人民共和国网络安全法", "2017", "current"), **common)
    assert paper.id < std.id < law.id   # 不加权时按编号排，白皮书会在最前

    items = [h for h in _search(people, "consultant", "量子密钥分发设备巡检台账", product="GEN", limit=10)["items"]
             if h["doc_id"] in {paper.doc_id, std.doc_id, law.doc_id}]
    assert [h["doc_id"] for h in items] == [law.doc_id, std.doc_id, paper.doc_id]
    assert [h["authority_level"] for h in items] == [1, 2, 3]
    assert [h["authority_label"] for h in items] == ["法律法规与监管文件", "标准", "参考资料"]
