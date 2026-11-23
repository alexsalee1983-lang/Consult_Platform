"""三期 B25①：缺口池合并、打分、阈值、周上限，下发包检查，回传批次哈希核对（纯逻辑，不建表）。"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from app.kb import acquisition as acq

TODAY = date(2026, 12, 1)


def sig(key: str, kind: str = "schedule", event: str = "ev-1", **kw: object) -> acq.Signal:
    return acq.Signal(key=key, kind=kind, event_ref=event, **kw)  # type: ignore[arg-type]


def test_same_key_signals_merge_into_one_gap() -> None:
    gaps = acq.merge_signals([
        sig("SRC-1", "external", "ev-a", impact=2),
        sig("SRC-1", "usage", "ev-b", impact=5, due=date(2027, 1, 1)),
        sig("SRC-1", "usage", "ev-b"),  # 同一事件重复上报只算一次
        sig("SRC-2", "schedule", "ev-c"),
    ])
    assert [g.key for g in gaps] == ["SRC-1", "SRC-2"]
    g = gaps[0]
    assert g.signal_count == 2
    assert g.events == ["ev-a", "ev-b"]
    assert g.kinds == {"external": 1, "usage": 1}
    assert g.impact == 5 and g.due == date(2027, 1, 1)


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValueError):
        acq.merge_signals([sig("X", "rumour")])


def test_event_ref_must_be_reference_not_text() -> None:
    with pytest.raises(ValueError):
        acq.merge_signals([sig("X", "usage", "顾问在对话里问了某银行的数据分类分级怎么做" * 3)])


@pytest.mark.parametrize("factor", ["impact", "urgency", "authority", "signals"])
def test_each_factor_raises_score(factor: str) -> None:
    base = acq.Gap(key="K", kinds={"schedule": 1}, events=["e"], impact=0, due=None, authority=3)
    better = acq.Gap(key="K", kinds={"schedule": 1}, events=["e"], impact=0, due=None, authority=3)
    if factor == "impact":
        better.impact = 10
    elif factor == "urgency":
        better.due = date(2026, 12, 20)
    elif factor == "authority":
        better.authority = 1
    else:
        better.kinds = {"schedule": 4}
        better.events = ["e1", "e2", "e3", "e4"]
    assert acq.score(better, TODAY) > acq.score(base, TODAY)


def test_urgency_tiers_90_and_30_days() -> None:
    far = acq.urgency(date(2027, 6, 1), TODAY)
    d90 = acq.urgency(date(2027, 2, 1), TODAY)
    d30 = acq.urgency(date(2026, 12, 15), TODAY)
    past = acq.urgency(date(2026, 11, 1), TODAY)
    assert far == 0 < d90 < d30 == past == 1


def test_weights_configurable() -> None:
    g = acq.Gap(key="K", kinds={"schedule": 1}, events=["e"], impact=10, due=None, authority=3)
    w = acq.Weights(impact=0, urgency=0, authority=0, signals=0)
    assert acq.score(g, TODAY, w) == 0


def test_below_threshold_accumulates_but_no_list() -> None:
    gaps = acq.merge_signals([sig("A", "usage", "u1"), sig("B", "usage", "u1"), sig("B", "usage", "u2")])
    plan = acq.plan_week(gaps, TODAY, thresholds={"usage": 2}, whitelist=(), cap=10)
    assert [g.key for g in plan.confirm] == ["B"]
    assert [g.key for g in plan.waiting] == ["A"]


def test_whitelisted_revision_goes_direct_others_to_confirm() -> None:
    gaps = acq.merge_signals([
        sig("https://www.gov.cn/zhengce/a", "external", "x1"),
        sig("https://new.example.org/law", "external", "x2"),
    ])
    plan = acq.plan_week(gaps, TODAY, thresholds={}, whitelist=("https://www.gov.cn/zhengce/",), cap=10)
    assert [g.key for g in plan.direct] == ["https://www.gov.cn/zhengce/a"]
    assert [g.key for g in plan.confirm] == ["https://new.example.org/law"]


def test_weekly_cap_defers_without_losing() -> None:
    gaps = acq.merge_signals([sig(f"K{i}", "schedule", f"e{i}", impact=i) for i in range(5)])
    plan = acq.plan_week(gaps, TODAY, thresholds={}, whitelist=(), cap=2)
    assert [g.key for g in plan.confirm] == ["K4", "K3"]
    assert sorted(g.key for g in plan.deferred) == ["K0", "K1", "K2"]
    assert len(plan.confirm) + len(plan.deferred) + len(plan.direct) + len(plan.waiting) == 5


# ---- 下发包 ----

WL = ("https://www.gov.cn/zhengce/", "https://csrc.nist.gov/pubs/")


def test_dispatch_package_only_urls_and_rules() -> None:
    pkg = acq.build_dispatch(["https://www.gov.cn/zhengce/a", "https://csrc.nist.gov/pubs/sp/800/53"], WL)
    assert acq.check_dispatch(pkg, WL) == []
    assert set(pkg) == {"format", "tasks"}
    assert set(pkg["tasks"][0]) == {"url", "rule"}


def test_dispatch_rejects_url_outside_whitelist() -> None:
    with pytest.raises(ValueError):
        acq.build_dispatch(["https://evil.example.com/x"], WL)


@pytest.mark.parametrize("mutate", [
    lambda p: p["tasks"][0].__setitem__("body", "第一条 为了规范……"),
    lambda p: p.__setitem__("entries", []),
    lambda p: p["tasks"][0]["rule"].__setitem__("note", "x" * 300),
    lambda p: p["tasks"][0].__setitem__("url", "https://evil.example.com/a"),
    lambda p: p["tasks"][0]["rule"].__setitem__("selector", "正文" * 200),
])
def test_check_dispatch_catches_tampering(mutate: object) -> None:
    pkg = acq.build_dispatch(["https://www.gov.cn/zhengce/a"], WL)
    mutate(pkg)  # type: ignore[operator]
    assert acq.check_dispatch(pkg, WL)


# ---- 回传批次 ----

def _batch(tmp: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        (tmp / name).write_bytes(data)
    manifest = {"files": [{"file": n, "sha256": hashlib.sha256(d).hexdigest(),
                           "url": "https://www.gov.cn/zhengce/" + n} for n, d in files.items()]}
    (tmp / acq.RETURN_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    return tmp


def test_return_batch_ok(tmp_path: Path) -> None:
    d = _batch(tmp_path, {"a.html": b"<p>a</p>", "b.pdf": b"%PDF"})
    r = acq.verify_return(d, WL)
    assert r.ok and r.count == 2 and r.problems == []


def test_return_batch_detects_changed_missing_extra_and_offlist(tmp_path: Path) -> None:
    d = _batch(tmp_path, {"a.html": b"<p>a</p>", "b.pdf": b"%PDF", "c.txt": b"c"})
    (d / "a.html").write_bytes(b"<p>tampered</p>")
    (d / "b.pdf").unlink()
    (d / "zz.bin").write_bytes(b"?")
    m = json.loads((d / acq.RETURN_MANIFEST).read_text(encoding="utf-8"))
    m["files"][2]["url"] = "https://evil.example.com/c"
    (d / acq.RETURN_MANIFEST).write_text(json.dumps(m), encoding="utf-8")
    r = acq.verify_return(d, WL)
    assert not r.ok
    kinds = {p.split(":")[0] for p in r.problems}
    assert kinds == {"sha256", "missing", "extra", "offlist"}
    # 问题只给文件名，不给内容
    assert all("tampered" not in p for p in r.problems)


def test_return_batch_rejects_path_escape(tmp_path: Path) -> None:
    d = _batch(tmp_path, {"a.html": b"a"})
    m = json.loads((d / acq.RETURN_MANIFEST).read_text(encoding="utf-8"))
    m["files"][0]["file"] = "../a.html"
    (d / acq.RETURN_MANIFEST).write_text(json.dumps(m), encoding="utf-8")
    r = acq.verify_return(d, WL)
    assert not r.ok and any(p.startswith("path") for p in r.problems)
