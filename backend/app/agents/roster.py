"""三期 C6-P：Agent 收敛为 9 个的平台侧名册与旧名兼容（总体规划第三节 1、决策记录 2026-11-21 ②）。

- NINE：收敛后的 9 个 Agent（代号、名称、所在区）。所在区决定硬规则：项目区 = 交付侧 delivery，
  知识区 = 知识侧 knowledge，采集机区 = collector（不接任何平台工具，app/agents/tools.py 的硬规则）。
- ALIASES：旧代号 → 新代号。C6 逐个合并 Agent 时（md 经负责人确认），旧代号在 agents 表里停用，
  调用方（对话、交付链、工具、转交）仍用旧代号的，自动转到新代号；旧代号还启用时照旧用旧的，
  所以本模块合并前后行为不变，合并一步切一步。已有对话、运行记录里存的代号不改写（按立项时的版本追溯）。
- md frontmatter 的 replaces 必须与 ALIASES 一致（tests/test_roster.py 校验），md 不写也以本表为准。
- 降为工具层的（脱敏审查 → deid.check）和储备的（售前、带教）不在 9 个里，也不做别名。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Agent

DELIVERY, KNOWLEDGE, COLLECTOR = "delivery", "knowledge", "collector"

NINE: tuple[tuple[str, str, str], ...] = (   # 代号, 名称, 所在区（agents.side）
    ("consult-coordinator", "咨询协调", DELIVERY),
    ("research-interview", "调研访谈", DELIVERY),
    ("compliance-basis", "依据与差距分析", DELIVERY),
    ("solution-designer", "方案与制度设计", DELIVERY),
    ("report-writer", "报告与客户沟通", DELIVERY),
    ("qa-reviewer", "质量评审", DELIVERY),
    ("kb-collector", "知识采集", COLLECTOR),
    ("kb-builder", "知识建设与萃取", KNOWLEDGE),
    ("kb-reviewer", "知识审核", KNOWLEDGE),
)
NINE_CODES = tuple(c for c, _, _ in NINE)

ALIASES: dict[str, str] = {
    "consultant-lead": "consult-coordinator",    # C6①：咨询师 + 项目交付管理 → 咨询协调
    "delivery-pm": "consult-coordinator",
    "policy-drafter": "solution-designer",       # C6②：方案设计 + 制度起草 → 方案与制度设计
}
TOOL_LAYER = {"sanitizer": "deid.check"}         # 脱敏审查降为工具层（决策记录 2026-11-18）
RESERVED = ("coach", "presales")                 # 售前、带教暂作储备


def canonical(code: str) -> str:
    """名册上的代号（不看数据库）：旧代号给新代号，其余原样。"""
    return ALIASES.get(code, code)


def replaced_by(code: str) -> str | None:
    return ALIASES.get(code)


def resolve(db: Session, code: str) -> str:
    """实际运行用的代号：旧代号已停用、新代号已启用时转到新代号；否则原样（含旧代号仍启用、新代号未登记）。"""
    target = ALIASES.get(code)
    if target is None:
        return code
    old, new = db.get(Agent, code), db.get(Agent, target)
    if (old is None or not old.enabled) and new is not None and new.enabled:
        return target
    return code
