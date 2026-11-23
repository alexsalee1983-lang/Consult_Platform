"""脱敏审查工具层检测（三期 C7，《4A 架构设计》“安全检查不做 Agent、做工具”）。

确定性检测，不调模型：
1. 机密级检测（services/guard.py，红线 3）：命中即“退回 / 高”，只给命中类别；
2. 直接标识规则（kb/deid.py）：替换后仍能识别出的客户、人员、系统、网络标识等，“有条件通过 / 中”，只给类别和个数。

结论只能更严：merge() 取 Agent 复查与工具检测中更严的一方，工具不会让 Agent 的结论变松；
Agent 未复查（演示模式、模型出错）时工具结论照样生效。输出不含原文，可以进报告和审计。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..services import guard
from . import deid
from .deid_agent import CONCLUSIONS, RISK_LEVELS, AgentReview


@dataclass
class DeidCheck:
    conclusion: str
    risk_level: str
    guard_labels: list[str] = field(default_factory=list)     # 机密级命中类别（中文说明）
    identifiers: dict[str, int] = field(default_factory=dict)   # 直接标识类别 → 个数
    manual_items: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"conclusion": self.conclusion, "risk_level": self.risk_level, "guard_labels": self.guard_labels,
                "identifiers": self.identifiers, "manual_items": self.manual_items}


def strictest(a: str, b: str, order: Sequence[str]) -> str:
    """按 order（宽 → 严）取更严的一个；空值或不认识的值不参与。"""
    known = [x for x in (a, b) if x in order]
    return max(known, key=order.index) if known else ""


def check(text: str, extra_terms: list[deid.Term] | None = None) -> DeidCheck:
    g = guard.check(text)
    if g.blocked:
        labels = sorted({f.label for f in g.findings})
        return DeidCheck("退回", "高", guard_labels=labels,
                         manual_items=[f"含机密级内容（{'、'.join(labels)}），不能入库，需删除后重新去标识"])
    r = deid.deidentify([("", text)], "x", extra_terms)
    ids = dict(sorted(Counter(x.category for x in r.replacements if x.count).items()))
    if ids:
        desc = "、".join(f"{k} {v} 处" for k, v in ids.items())
        return DeidCheck("有条件通过", "中", identifiers=ids,
                         manual_items=[f"仍有直接标识（{desc}），需补充替换词重跑或人工确认"])
    return DeidCheck("通过", "低")


@dataclass
class Merged:
    conclusion: str
    risk_level: str
    manual_items: list[str]


def merge(agent: AgentReview, tool: DeidCheck) -> Merged:
    conclusion = strictest(agent.conclusion, tool.conclusion, CONCLUSIONS)
    risk = strictest(agent.risk_level, tool.risk_level, RISK_LEVELS)
    items = list(agent.manual_items) + [x for x in tool.manual_items if x not in agent.manual_items]
    return Merged(conclusion, risk, items)
