"""三期 C7：脱敏审查工具层检测（接 guard，只能更严）。

红线 3：机密级五类（口令密钥、可利用漏洞细节、网络拓扑与 IP 规划、业务数据与个人信息样本、证件卡号）命中必须退回，
工具结论只能让去标识报告更严，不能让 Agent 的结论变松。工具输出不含原文。
"""
import itertools
import json

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import deid_check
from app.kb.deid_agent import CONCLUSIONS, RISK_LEVELS, AgentReview
from app.models import AuditLog

from .conftest import on_s3
from .test_kb_deid import _client, _upload_and_process

# 全部是虚构样本；云密钥样例运行时拼接，源码里不出现完整的密钥格式，不靠 .gitleaksignore 放行
FAKE_CLOUD_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
GUARD_SAMPLES = {
    "口令": "运维账号 admin 的密码：Xk9#mP2$vL7q",
    "密钥": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    "云密钥": f"访问密钥 {FAKE_CLOUD_KEY} 已配置",
    "身份证": "联系人证件号 11010519491231002X",
    "手机号": "值班电话 13800138000",
}


@pytest.mark.parametrize("name", sorted(GUARD_SAMPLES))
def test_guard_hit_forces_reject(name):
    r = deid_check.check(GUARD_SAMPLES[name])
    assert r.conclusion == "退回" and r.risk_level == "高"
    assert r.guard_labels
    dumped = json.dumps(r.to_json(), ensure_ascii=False)
    for secret in ("Xk9#mP2$vL7q", FAKE_CLOUD_KEY, "11010519491231002X", "13800138000", "MIIEow"):
        assert secret not in dumped                      # 只给类别和计数，不回显原文


def test_residual_direct_identifiers_need_manual_check():
    r = deid_check.check("江南农商行的告警分级，请王经理负责。")
    assert r.conclusion == "有条件通过" and r.risk_level == "中"
    assert r.identifiers.get("客户名称") == 1 and r.identifiers.get("人名") == 1
    dumped = json.dumps(r.to_json(), ensure_ascii=False)
    assert "江南农商行" not in dumped and "王经理" not in dumped
    assert any("直接标识" in m for m in r.manual_items)


def test_clean_placeholder_text_passes():
    r = deid_check.check("【客户1】的安全运营中心由【人员1】牵头，告警分为三级。")
    assert r.conclusion == "通过" and r.risk_level == "低" and not r.manual_items


def test_strictest_never_loosens():
    for a, b in itertools.product(("", *CONCLUSIONS), CONCLUSIONS):
        s = deid_check.strictest(a, b, CONCLUSIONS)
        assert s in CONCLUSIONS
        for x in (a, b):
            if x:
                assert CONCLUSIONS.index(s) >= CONCLUSIONS.index(x)
    assert deid_check.strictest("低", "高", RISK_LEVELS) == "高"


@pytest.mark.parametrize("agent_conclusion", ["", *CONCLUSIONS])
def test_merge_tool_reject_overrides_agent(agent_conclusion):
    agent = AgentReview("ok", conclusion=agent_conclusion, risk_level="低" if agent_conclusion else "")
    tool = deid_check.check(GUARD_SAMPLES["口令"])
    m = deid_check.merge(agent, tool)
    assert m.conclusion == "退回" and m.risk_level == "高"
    assert any("机密级" in x for x in m.manual_items)


def test_merge_keeps_agent_when_stricter():
    agent = AgentReview("ok", conclusion="退回", risk_level="高", manual_items=["组合推断风险需人工确认"])
    m = deid_check.merge(agent, deid_check.check("【客户1】的安全运营中心。"))
    assert m.conclusion == "退回" and m.risk_level == "高" and m.manual_items == ["组合推断风险需人工确认"]


def test_merge_agent_skipped_still_uses_tool():
    """演示模式或模型出错时，Agent 没有结论，工具结论照样生效。"""
    agent = AgentReview("skipped", note="未接入模型", manual_items=["需人工逐段复核"])
    m = deid_check.merge(agent, deid_check.check("江南农商行的告警分级。"))
    assert m.conclusion == "有条件通过" and "需人工逐段复核" in m.manual_items


@pytest.mark.parametrize("status", ["skipped", "failed"])
def test_merge_agent_not_reviewed_caps_at_conditional(status):
    """Agent 未复查（跳过或失败）时，即使工具“通过”，合并结论最多“有条件通过 / 中”，转人工确认。"""
    m = deid_check.merge(AgentReview(status), deid_check.check("[客户A] 的告警分级。"))
    assert m.conclusion == "有条件通过" and m.risk_level == "中"
    assert any("未复查" in x for x in m.manual_items)


def test_merge_agent_ok_tool_pass_stays_pass():
    m = deid_check.merge(AgentReview("ok", conclusion="通过", risk_level="低"), deid_check.check("[客户A] 的告警分级。"))
    assert m.conclusion == "通过" and m.risk_level == "低"


# ---- 流程：工具结论并入去标识报告（接真实对象存储）----

@on_s3
def test_flow_tool_reject_blocks_pass_even_without_model(monkeypatch):
    """没接模型（Agent 跳过）时，工具检测出机密级内容 → 报告结论“退回”，复核人不能直接通过。"""
    from app.kb import deid_flow
    monkeypatch.setattr(deid_flow.deid_check, "check",
                        lambda text, extra_terms=None: deid_check.DeidCheck(
                            "退回", "高", guard_labels=["口令"], manual_items=["含机密级内容（口令），不能入库"]))
    uploader, admin2 = _client(("knowledge_admin",)), _client(("knowledge_admin",))
    uid = _upload_and_process(uploader, "# 值班复盘\n\n告警分为三级。\n", "值班复盘C7.md")
    rep = uploader.get(f"/api/kb/intake/{uid}/deid").json()
    assert rep["agent_status"] == "skipped"
    assert rep["conclusion"] == "退回" and rep["risk_level"] == "高"
    assert any("机密级" in x for x in rep["manual_items"])
    assert admin2.post(f"/api/kb/intake/{uid}/deid/decision", json={"decision": "pass"}).status_code == 409
    with SessionLocal() as db:
        log = db.scalars(select(AuditLog).where(AuditLog.action == "kb.deid").order_by(AuditLog.id.desc())).first()
        d = json.loads(log.detail)
        assert d["tool_conclusion"] == "退回" and d["tool_guard"] == 1
