"""脱敏审查 Agent 复查（agents/roles/sanitizer.md 第 1～4 步）：找规则漏掉的名称，判断组合推断风险，给结论。

- 只把**规则替换之后**的文本发给模型，并且只经模型网关（gateway.complete，含机密级检测、外部模型保护）。
- 模型按 JSON 返回：residual_terms（漏网的名称，平台拿来自动替换，**不保存**）、combination_risks、
  manual_items、conclusion、risk_level。组合风险描述会再过一遍替换，避免把名称写进报告。
- 没接模型（演示模式）、模型出错、返回无法解析时：不阻断流程，记为 skipped / failed，并加一条“需人工确认”。
- 用量和审计照常记录（不记文本内容）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..agents import registry
from ..core import audit
from ..models import Agent
from ..services import gateway, usage
from .deid import CATEGORIES, Term

AGENT = "sanitizer"
LEVEL = "standard"
MAX_CHARS_PER_CALL = 12_000
MAX_CALLS = 3
CONCLUSIONS = ("通过", "有条件通过", "退回")
RISK_LEVELS = ("低", "中", "高")

OUTPUT_RULES = """

---

# 平台调用说明（本次只做审查，不输出脱敏后正文）

输入的文本已由平台按规则做过第一轮替换（【客户1】【人员2】这类占位符是已处理的内容）。请按上面的工作流程复查，
**只输出一个 JSON 对象**，不要输出其他文字：

{"conclusion": "通过 | 有条件通过 | 退回",
 "risk_level": "低 | 中 | 高",
 "residual_terms": [{"term": "仍能识别客户、人员、系统的原词", "category": "客户名称 | 人名 | 系统名称 | 网络标识 | 编号 | 金额 | 联系方式 | 其他"}],
 "combination_risks": [{"description": "风险描述（用占位符或泛称，不要写出原词）", "handling": "建议的泛化方式"}],
 "manual_items": ["需要人工确认的事项（不要写出原词）"]}

residual_terms 只供平台自动替换，不会保存，也不会出现在报告里；其他字段会进入审查报告，不得包含原词。"""


@dataclass
class AgentReview:
    status: str                          # ok / skipped / failed
    note: str = ""                       # skipped / failed 的原因
    conclusion: str = ""
    risk_level: str = ""
    residual_terms: list[Term] = field(default_factory=list)      # 只在内存中使用
    combination_risks: list[dict] = field(default_factory=list)
    manual_items: list[str] = field(default_factory=list)
    model: str = ""
    calls: int = 0


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no json")
    return json.loads(m.group(0))


def _pieces(text: str) -> list[str]:
    out, i = [], 0
    while i < len(text) and len(out) < MAX_CALLS:
        out.append(text[i:i + MAX_CHARS_PER_CALL])
        i += MAX_CHARS_PER_CALL
    return out


def review(db: Session, text: str, actor: str, upload_id: int) -> AgentReview:
    # 不用 runtime.ready_doc：那是顾问对话的可用性判断（知识侧 Agent 不在对话里开放），这里只要求已登记、已启用
    agent = db.get(Agent, AGENT)
    try:
        if agent is None or not agent.enabled:
            raise LookupError("脱敏审查 Agent 未登记或已停用")
        doc = registry.load(AGENT)
    except Exception as e:  # noqa: BLE001  md 缺失或格式不对
        return AgentReview("skipped", note=str(e)[:200], manual_items=["脱敏审查 Agent 不可用，需人工逐段复核"])
    system = f"{doc.common.strip()}\n\n---\n\n{doc.body.strip()}{OUTPUT_RULES}"
    r = AgentReview("ok")
    order = {c: i for i, c in enumerate(CONCLUSIONS)}
    for piece in _pieces(text):
        try:
            out = gateway.complete(piece, tier="standard", project_code="", level=LEVEL, system=system)
        except gateway.BlockedError:
            # 规则替换后仍命中机密级内容（不应出现）：不发模型，交人工
            audit.record(db, actor=actor, action="kb.deid_agent", result="blocked", upload_id=upload_id)
            return AgentReview("failed", note="复查文本仍含机密级内容，未发送给模型",
                               manual_items=["复查文本仍含机密级内容，需人工处理后重新去标识"])
        except gateway.GatewayError as e:
            usage.record(db, username=actor, project_code="", level=LEVEL, model=gateway.model_for(LEVEL),
                         status="error", stream=False, attempts=e.attempts, agent_code=AGENT)
            audit.record(db, actor=actor, action="kb.deid_agent", result="error", upload_id=upload_id,
                         error=str(e)[:100])
            return AgentReview("failed", note="模型调用失败", manual_items=["脱敏审查 Agent 调用失败，需人工逐段复核"])
        usage.record(db, username=actor, project_code="", level=LEVEL, model=out.model, status="ok", stream=False,
                     completion=out, agent_code=AGENT)
        audit.record(db, actor=actor, action="kb.deid_agent", result="allowed", upload_id=upload_id,
                     model=out.model, demo=out.demo, external=out.external, length=len(piece))
        r.calls += 1
        r.model = out.model
        if out.demo:
            return AgentReview("skipped", note="未接入模型（演示模式）", model=out.model, calls=r.calls,
                               manual_items=["未接入模型，脱敏审查 Agent 没有复查，需人工逐段复核"])
        try:
            data = _parse(out.text)
        except (ValueError, json.JSONDecodeError):
            return AgentReview("failed", note="Agent 返回格式无法解析", model=out.model, calls=r.calls,
                               manual_items=["脱敏审查 Agent 返回格式无法解析，需人工逐段复核"])
        c = data.get("conclusion", "")
        if c in order and (not r.conclusion or order[c] > order[r.conclusion]):   # 取最严的结论
            r.conclusion = c
        lvl = data.get("risk_level", "")
        if lvl in RISK_LEVELS and (not r.risk_level or RISK_LEVELS.index(lvl) > RISK_LEVELS.index(r.risk_level)):
            r.risk_level = lvl
        for t in data.get("residual_terms") or []:
            if isinstance(t, dict) and str(t.get("term", "")).strip():
                cat = t.get("category") if t.get("category") in CATEGORIES else "其他"
                r.residual_terms.append(Term(str(t["term"]).strip()[:100], cat))
        for x in data.get("combination_risks") or []:
            if isinstance(x, dict):
                r.combination_risks.append({"description": str(x.get("description", ""))[:300],
                                            "handling": str(x.get("handling", ""))[:300]})
        r.manual_items += [str(x)[:300] for x in (data.get("manual_items") or []) if str(x).strip()]
    if len(text) > MAX_CHARS_PER_CALL * MAX_CALLS:
        r.manual_items.append(f"正文较长，Agent 只复查了前 {MAX_CHARS_PER_CALL * MAX_CALLS} 字，其余需人工复核")
    if not r.conclusion:
        r.conclusion = "有条件通过"
        r.manual_items.append("Agent 没有给出明确结论")
    return r
