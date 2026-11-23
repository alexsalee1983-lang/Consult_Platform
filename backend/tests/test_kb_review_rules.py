"""三期 B20：审核规则调整——统计抽样定抽样量、停止规则攒够 20 个样本才启用、公开来源时效由程序判断。"""
import hashlib
from datetime import date, timedelta

from app.core.config import get_settings
from app.kb import autopublish, sources

LAW = "示范时效条例\n第一条 为了规范示范时效管理，制定本条例。\n第二条 本条例自公布之日起施行。\n"


def test_sample_size_hypergeometric():
    n = autopublish.sample_size
    assert n(0, 0.1, 0.8) == 0
    assert n(3, 0.1, 0.8) == 3                      # 小批：缺陷哪怕只有 1 条也要大概率抽到，只能全抽
    assert n(1000, 0.1, 0.8) == 16                  # 大批：不随批量线性涨（旧规则 5% 要抽 50）
    assert n(200, 0.1, 0.8) <= n(1000, 0.1, 0.8) <= n(1000, 0.1, 0.95)
    assert n(50, 0.1, 0.0) == 0                     # 置信度 0：不抽
    assert all(1 <= n(k, 0.1, 0.8) <= k for k in range(1, 300))


def test_batch_sample_count_uses_floor_rate(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "kb_sample_defect_max", 0.1)
    monkeypatch.setattr(s, "kb_sample_confidence", 0.8)
    monkeypatch.setattr(s, "kb_sample_rate_src", 0.0)
    assert autopublish.batch_sample_count(1000, partial=False) == 16
    monkeypatch.setattr(s, "kb_sample_rate_src_partial", 0.4)    # 负责人 2026-11-16：部分退回的份按比例兜底
    assert autopublish.batch_sample_count(1000, partial=True) == 400


def test_stop_rule_needs_20_samples():
    f = autopublish.stop_rule
    assert not f(checked=5, failed=1, fail_max=0.1)          # 样本不够 20，1/5 不暂停
    assert not f(checked=19, failed=2, fail_max=0.1)
    assert f(checked=5, failed=3, fail_max=0.1)              # 已经 3 条不通过：凑满 20 条也必超 10%，提前停
    assert f(checked=20, failed=3, fail_max=0.1)
    assert not f(checked=20, failed=2, fail_max=0.1)
    assert f(checked=1, failed=1, fail_max=0.0)              # 上限 0：一条不通过就停
    assert not f(checked=0, failed=0, fail_max=0.0)


def test_verify_timeliness():
    data = LAW.encode()
    base = dict(file="t.txt", title="示范时效条例", code="示范令第9号", url="https://www.gov.cn/t",
                sha256=hashlib.sha256(data).hexdigest())
    ok = sources.Doc(**base, effective=date.today())
    entries, rejected, _ = sources.split_doc(ok, data)
    assert sources.verify(ok, data, entries, rejected) == []
    future = sources.Doc(**base, effective=date.today() + timedelta(days=30))
    assert sources.verify(future, data, entries, rejected) == ["timely"]          # 未施行：人确认
    gone = sources.Doc(**base, status="repealed")
    assert sources.verify(gone, data, entries, rejected) == ["timely"]            # 已废止：人确认
    replaced = sources.Doc(**base, status="superseded", replaced_by="示范令第10号")
    assert sources.verify(replaced, data, entries, rejected) == ["timely"]
    assert "timely" in sources.VERIFY_RULES
