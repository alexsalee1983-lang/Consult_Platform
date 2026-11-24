"""三期 A24：章节级装配评测（评测集格式、就绪度、命中率、缺槽、和基线比较、命令行）。"""
import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.kb import assembly_eval as ae
from app.kb import importer
from app.kb import review as rv
from app.models import KbItem, User

from .conftest import create_user

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "knowledge" / "samples"
EVALSET = ROOT / "knowledge" / "eval" / "assembly_samples.yaml"
PREFIX = "ae-"


def _user(roles):
    name, *_ = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


def _evalset(prefix: str = PREFIX) -> dict:
    data = ae.load(EVALSET)
    for ch in data["chapters"]:
        ch["expect"] = [prefix + k for k in ch["expect"]]
    return data


@pytest.fixture(scope="module")
def published(tmp_path_factory):
    """把样例条目换成本测试专用 key 导入并走完审核发布。"""
    d = tmp_path_factory.mktemp("ae")
    for f in SAMPLES.glob("*.md"):
        (d / f.name).write_text(f.read_text(encoding="utf-8").replace("key: ", f"key: {PREFIX}", 1), encoding="utf-8")
    expert, expert2, admin = _user(("knowledge_expert",)), _user(("knowledge_expert",)), _user(("knowledge_admin",))
    with SessionLocal() as db:
        for r in importer.import_dir(db, db.merge(expert), d, submit=True):
            v = db.get(KbItem, db.scalar(select(KbItem.id).where(KbItem.import_key == r.key))).versions[-1]
            rv.review(db, db.merge(expert2), v, "content", "approve")
            rv.review(db, db.merge(admin), v, "compliance", "approve")
    yield _user(("consultant",))
    with SessionLocal() as db:                 # 停用本测试的条目，免得影响后面按检索结果断言的测试
        for item in db.scalars(select(KbItem).where(KbItem.import_key.like(f"{PREFIX}%"))):
            item.retired = True
        db.commit()


def test_sample_evalset_valid():
    data = ae.load(EVALSET)
    sample_keys = {f.stem for f in SAMPLES.glob("*.md")}
    keys = {k for ch in data["chapters"] for k in ch["expect"]}
    assert keys - sample_keys == {"soc-ransomware-playbook"}          # 只有故意缺的那条不在样例里
    assert all(ae.MIN_EXPECT <= len(ch["expect"]) <= ae.MAX_EXPECT for ch in data["chapters"])


@pytest.mark.parametrize("text,msg", [
    ("chapters: []", "product"),
    ("product: SOC\nchapters: []", "chapters"),
    ("product: SOC\nchapters:\n- {id: A, step: facts, title: t, expect: [a, b]}", "3～5"),
    ("product: SOC\nchapters:\n- {id: A, step: facts, title: t, expect: [a, a, b]}", "3～5"),
    ("product: SOC\nchapters:\n- {id: A, step: facts, title: t, expect: [a, b, c, d, e, f]}", "3～5"),
    ("product: SOC\nchapters:\n- {id: A, step: facts, title: t, expect: [a, b, c]}\n"
     "- {id: A, step: facts, title: t, expect: [a, b, c]}", "重复"),
    ("product: SOC\nchapters:\n- {id: A, title: t, expect: [a, b, c]}", "step"),
])
def test_bad_evalset_rejected(tmp_path, text, msg):
    p = tmp_path / "e.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ae.AssemblyEvalError, match=msg):
        ae.load(p)


def test_unknown_step_rejected(published):
    data = _evalset()
    data["chapters"][0]["step"] = "no-such-step"
    with SessionLocal() as db, pytest.raises(ae.AssemblyEvalError, match="no-such-step"):
        ae.run(db, db.merge(published), data)


def test_baseline_reports_readiness_hits_and_missing(published):
    with SessionLocal() as db:
        rep = ae.run(db, db.merge(published), _evalset())
    assert rep.strategy == "baseline" and rep.expected == 13 and len(rep.chapters) == 4
    d502 = next(c for c in rep.chapters if c.id == "SOC-D5-02")
    assert d502.missing == [PREFIX + "soc-ransomware-playbook"]       # 库里没有 → 缺槽，不算就绪
    assert rep.readiness == round(12 / 13, 4)
    for c in rep.chapters:
        assert set(c.hit) <= set(c.ready) <= set(c.expect)
    # 基线同一步的两章查询相同，装配结果一样
    d501 = next(c for c in rep.chapters if c.id == "SOC-D5-01")
    assert d501.got == d502.got
    js = rep.to_json()
    assert js["chapters"][3]["missing"] == [PREFIX + "soc-ransomware-playbook"]
    text = ae.format_report(rep)
    assert "命中率" in text and "就绪度" in text and "soc-ransomware-playbook" in text


def test_retired_item_not_ready(published):
    with SessionLocal() as db:
        item = db.scalar(select(KbItem).where(KbItem.import_key == PREFIX + "soc-metrics"))
        item.retired = True
        db.commit()
        try:
            rep = ae.run(db, db.merge(published), _evalset())
        finally:
            item.retired = False
            db.commit()
    gap = next(c for c in rep.chapters if c.id == "SOC-GAP-01")
    assert PREFIX + "soc-metrics" in gap.missing


def test_chapter_strategy_and_compare(published):
    with SessionLocal() as db:
        base = ae.run(db, db.merge(published), _evalset())
        chap = ae.run(db, db.merge(published), _evalset(), strategy="chapter")
    assert chap.strategy == "chapter"
    ok, msg = ae.compare(base, base.to_json())
    assert ok and "基线" in msg
    ok, _ = ae.compare(base, base.to_json(), min_gain=0.01)          # 持平不算“高于”
    assert not ok
    worse = {**base.to_json(), "hit_rate": 1.0}
    assert not ae.compare(chap, worse)[0]
    with pytest.raises(ae.AssemblyEvalError, match="基线是"):
        ae.compare(base, {**base.to_json(), "product": "ISO"})
    with pytest.raises(ae.AssemblyEvalError, match="策略"):
        with SessionLocal() as db:
            ae.run(db, db.merge(published), _evalset(), strategy="nope")


def test_cli(published, tmp_path, capsys):
    with SessionLocal() as db:
        name = db.merge(published).username
        assert db.scalar(select(User).where(User.username == name))
    src = _evalset()
    p = tmp_path / "e.yaml"
    p.write_text(yaml.safe_dump(src, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "base.json"
    assert cli_main(["kb-assembly-eval", str(p), "--as", name, "--out", str(out)]) == 0
    assert "章节级装配评测" in capsys.readouterr().out
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["strategy"] == "baseline" and "hit_rate" in saved
    assert cli_main(["kb-assembly-eval", str(p), "--as", name, "--baseline", str(out)]) == 0
    assert cli_main(["kb-assembly-eval", str(p), "--as", name, "--baseline", str(out), "--min-gain", "0.01"]) == 3
    assert cli_main(["kb-assembly-eval", str(p), "--as", "nobody-x"]) == 1
    bad = tmp_path / "bad.yaml"
    bad.write_text("chapters: []", encoding="utf-8")
    assert cli_main(["kb-assembly-eval", str(bad), "--as", name]) == 1


# ---- A12 程序门（评测门 C8 的一部分，CI 跑，不连模型）----

def test_slots_override_validated(tmp_path):
    base = "product: SOC\nchapters:\n- {id: A, step: facts, title: t, expect: [a, b, c]}\n"
    for extra, msg in (("slots: [1]", "步骤 key"), ("slots:\n  facts: []", "不能为空"),
                       ("slots:\n  facts:\n  - {id: x, type: NOPE, query: q}", "条目类型")):
        p = tmp_path / "e.yaml"
        p.write_text(base + extra, encoding="utf-8")
        with pytest.raises(ae.AssemblyEvalError, match=msg):
            ae.load(p)
    assert ae.load(EVALSET)["slots"]["design_doc"][0]["id"] == "duty"


def test_slots_strategy_keeps_step_retrieval_as_floor(published):
    with SessionLocal() as db:
        base = ae.run(db, db.merge(published), _evalset())
        slot = ae.run(db, db.merge(published), _evalset(), strategy="slots")
    assert slot.strategy == "slots"
    for b, s in zip(base.chapters, slot.chapters, strict=True):
        assert set(b.got) <= set(s.got) and set(b.hit) <= set(s.hit)     # 步骤检索作底，只多不少


def test_a12_gate_beats_fixed_query_baseline(published):
    """A12 合并条件（总体规划 A12“A24 命中率高于基线才合并”）：按槽位装配命中率必须严格高于固定查询基线。"""
    with SessionLocal() as db:
        ok, line, base, slot = ae.gate(db, db.merge(published), _evalset())
    assert ok, line
    assert slot.hit_rate > base.hit_rate and "通过" in line


def _no_product_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """只看评测集覆盖的槽位，屏蔽产品配置（SOC 自 C10 起已配槽位），让门的判定逻辑可单独测。"""
    monkeypatch.setattr(ae, "step_slots", lambda evalset, snap: dict(evalset.get("slots") or {}))


def test_a12_gate_fails_without_slots_or_without_gain(published, monkeypatch):
    _no_product_slots(monkeypatch)
    with SessionLocal() as db:
        ok, line, *_ = ae.gate(db, db.merge(published), {**_evalset(), "slots": {}})
        assert not ok and "没有配置知识槽位" in line
        same = {**_evalset(), "slots": {"facts": ({"id": "x", "type": "GLD", "query": "不存在的类型", "need": 1,
                                                   "required": False},)}}
        ok, line, base, slot = ae.gate(db, db.merge(published), same)
    assert not ok and slot.hit_rate == base.hit_rate and "未通过" in line   # 持平不算高于


def test_cli_gate(published, tmp_path, capsys, monkeypatch):
    _no_product_slots(monkeypatch)
    with SessionLocal() as db:
        name = db.merge(published).username
    p = tmp_path / "e.yaml"
    p.write_text(yaml.safe_dump({**_evalset(), "slots": {k: [dict(s) for s in v]
                                                         for k, v in _evalset()["slots"].items()}},
                                allow_unicode=True), encoding="utf-8")
    assert cli_main(["kb-assembly-eval", str(p), "--as", name, "--gate"]) == 0
    assert "A12 门" in capsys.readouterr().out
    q = tmp_path / "none.yaml"
    q.write_text(yaml.safe_dump({k: v for k, v in _evalset().items() if k != "slots"}, allow_unicode=True),
                 encoding="utf-8")
    assert cli_main(["kb-assembly-eval", str(q), "--as", name, "--gate"]) == 3
