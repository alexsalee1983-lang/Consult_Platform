"""三期 C10：SOC 的 D4、D5 八个关键章节槽位规格 v0（公开方法版，全部待专家复核）。

- 槽位写在交付链步骤的 slots 里（draft → D4，design_doc → D5），每个槽位带 chapter（章节槽位键，
  与知识条目的 slot 字段同一写法，例 SOC.D5.ch2）和 expert_review（pending / done）；
- v0 全部 expert_review: pending、required: false（专家勾选前不发“知识不足”卡）；
- 每章至少一个方法或模板类槽位；D4 的依据说明一章有依据类槽位（SRC / REF，就绪度目标 ≥95% 的口径）。
"""
from __future__ import annotations

from collections import defaultdict

from app.kb import meta
from app.products import config

BASIS_TYPES = {"SRC", "REF", "MAP"}
METHOD_TYPES = {"MTD", "TPL", "GLD", "RAR"}


def _check(raw):
    out: list[str] = []
    return config.parse_slots(config._P("t", out), raw), out


def test_slot_keeps_chapter_and_expert_review():
    ok, errs = _check([{"id": "a", "type": "MTD", "query": "q", "chapter": "SOC.D5.ch1", "expert_review": "pending"},
                       {"id": "b", "type": "TPL", "query": "q"}])
    assert not errs
    assert ok[0]["chapter"] == "SOC.D5.ch1" and ok[0]["expert_review"] == "pending"
    assert "chapter" not in ok[1] and "expert_review" not in ok[1]
    assert _check([{"id": "a", "type": "MTD", "query": "q", "expert_review": "ok"}])[1]
    assert _check([{"id": "a", "type": "MTD", "query": "q", "chapter": "第 1 章"}])[1]


def _chapters(step_key: str) -> dict[str, list[dict]]:
    step = next(s for s in config.product("SOC").chain if s.key == step_key)
    by: dict[str, list[dict]] = defaultdict(list)
    for s in step.slots:
        by[s["chapter"]].append(s)
    return by


def test_soc_d4_d5_eight_chapters_pending():
    assert not config.validate()
    d4, d5 = _chapters("draft"), _chapters("design_doc")
    assert sorted(d4) == [f"SOC.D4.ch{i}" for i in range(1, 5)]
    assert sorted(d5) == [f"SOC.D5.ch{i}" for i in range(1, 5)]
    for chapters in (d4, d5):
        for key, slots in chapters.items():
            assert meta.SLOT_RE.match(key)
            assert all(s["expert_review"] == "pending" and s["required"] is False for s in slots), key
            assert {s["type"] for s in slots} & METHOD_TYPES, key
    assert {s["type"] for s in d4["SOC.D4.ch3"]} & BASIS_TYPES


def test_soc_slots_reach_snapshot():
    snap = config.compose_codes("SOC")
    draft = next(s for s in snap["chain"] if s["key"] == "draft")
    assert all(s["chapter"].startswith("SOC.D4.") for s in draft["slots"])


# 四期 #461：D3、D6、D7、D8 章节清单与槽位规格 v0（D1、D2 由表单成稿，不在交付链里，只配章节清单）
STEP_OF = {"D3": "facts", "D6": "policy", "D7": "roadmap", "D8": "briefing"}


def test_soc_every_deliverable_has_outline():
    prod = config.product("SOC")
    for d in prod.deliverables:
        if d.code != "D4":   # D4 章节由槽位 chapter SOC.D4.ch1～4 给出，大纲另议
            assert len(d.outline) >= 3, d.code


def test_soc_d3_d6_d7_d8_slots_follow_outline():
    prod = config.product("SOC")
    outlines = {d.code: d.outline for d in prod.deliverables}
    for code, step_key in STEP_OF.items():
        by = _chapters(step_key)
        assert by, code
        n = len(outlines[code])
        for key, slots in by.items():
            assert key.startswith(f"SOC.{code}.ch"), key
            assert 1 <= int(key.rsplit("ch", 1)[1]) <= n, key       # 章号落在大纲范围内
            assert all(s["expert_review"] == "pending" and s["required"] is False for s in slots), key
        assert any({s["type"] for s in slots} & METHOD_TYPES for slots in by.values()), code
        assert len(by) >= 3, code                                    # 至少覆盖三章
