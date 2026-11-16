"""知识审核 Agent 与自动发布（二期 B11，二期开发计划第十二节第二级，决策记录 2026-11-15“自动化优先”）。

- 适用：来源为公开资料、无需去标识的解读、映射、方法（REF / MAP / MTD），以及只含公开资料的案例、异议应答
  （CAS / OBJ），执法案例、参考架构、术语对照（ENF / RAR / GLS，决策记录 2026-11-16；ENF 须写依据文件）。提交审核（review.submit）后排后台任务 kb.agent_review，不在请求里调模型。
- 先过程序规则（RULES）：引用都已核对到依据原文（B8）、没有测评 / 认证结论用语、没有机密级内容、元数据和适用条件齐全；
  再把条目、引用到的依据原文和相近条目交给知识审核 Agent（agents/roles/kb-reviewer.md，经网关 gateway.complete），
  按审核清单 CHECKS 逐项判断。Agent 不授权任何工具，只返回 JSON 审查意见。
- 程序规则全过、Agent 七项都 pass、没有阻断 / 重要问题、结论“通过”：平台自动发布，版本 auto_review=agent，
  审核记录 reviewer=agent:kb-reviewer、review_role=agent；按 KB_SAMPLE_RATE_AGENT 随机抽检（最近抽检不通过率超过
  上限一半时比例加倍），抽检与 B10 共用队列（autopublish.sample_check）。
- 否则留在人工队列（两项人工审核照常），审核记录 decision=flag，findings 里是没过的规则、逐项检查和问题清单，
  审核页只看标出的问题。没接模型、模型出错、返回无法解析时同样转人工。
- 第三级（二期 B12）：项目沉淀、专家萃取、内部制度改造且去标识已通过的版本（review.level3），同样先过程序规则、
  再交 Agent 审，但只把意见记在审核页（decision approve = 未见问题 / flag = 有问题），不自动发布、不抽检，
  由一位知识专家（不能是作者）拍板；不受 KB_AGENT_AUTO_PUBLISH 影响。
- 暂停：KB_AGENT_AUTO_PUBLISH=false，或 Agent 自动发布的最近 20 次抽检不通过率超过 KB_SAMPLE_FAIL_MAX。
- 审计 kb.agent_review 只记编号、版本、没过的规则、Agent、提示词版本、模型、结论和计数，不记正文和意见。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import call
from ..core import audit
from ..core.config import get_settings
from ..models import KbItem, KbReview, KbVersion
from ..services import gateway, guard, jobs
from . import autopublish, citations
from . import review as rv

AGENT = "kb-reviewer"
REVIEWER = f"agent:{AGENT}"
ROLE = "agent"
KIND = "agent"                       # KbVersion.auto_review
LEVEL = "standard"
TYPES = ("REF", "MAP", "MTD", "CAS", "OBJ", "ENF", "RAR", "GLS")
RULES = {"citations": "引用都已核对到依据原文", "boundary": "没有测评 / 认证结论用语",
         "confidential": "没有机密级内容", "metadata": "摘要、适用条件、依据文件齐全"}
CHECKS = {"faithful": "忠于原文", "no_addition": "不添加要求", "citation_fit": "引用对得上", "boundary": "资质边界",
          "confidential": "无客户与机密信息", "no_conflict": "不重复、不冲突", "applicability": "适用条件清楚"}
SEVERITIES = ("阻断", "重要", "建议")
MAX_BODY = 12_000
MAX_SOURCE = 2_000
MAX_SOURCES_TOTAL = 10_000

OUTPUT_RULES = """

---

# 平台调用说明

按上面的审核清单逐项判断，**只输出一个 JSON 对象**，不要输出其他文字：

{"checks": {"faithful": {"pass": true, "reason": "一句话理由"}, "no_addition": {...}, "citation_fit": {...},
            "boundary": {...}, "confidential": {...}, "no_conflict": {...}, "applicability": {...}},
 "issues": [{"severity": "阻断 | 重要 | 建议", "location": "位置", "problem": "问题", "suggestion": "修改建议"}],
 "conclusion": "通过 | 存疑"}

七项必须都给出；拿不准的 pass 写 false、理由以“存疑：”开头。"""


@dataclass
class Outcome:
    status: str                              # published / manual / skipped（不适用或已审过）
    rules_failed: list[str] = field(default_factory=list)
    agent: str = ""                          # ok / skipped / failed
    note: str = ""
    sampled: bool = False


def eligible(v: KbVersion) -> bool:
    return (v.item.doc_type in TYPES and v.source_type == "public" and v.deid_status == "not_needed")


def advisory(v: KbVersion) -> bool:
    """第三级（二期 B12）：项目沉淀、专家经验、内部制度改造，Agent 只出意见，由一位知识专家拍板。"""
    return rv.level3(v)


def enqueue(db: Session, v: KbVersion, created_by: str) -> int | None:
    """提交审核后调用：适用的版本排队交知识审核 Agent。"""
    if not eligible(v) and not advisory(v):
        return None
    return jobs.enqueue(db, "kb.agent_review", {"version_id": v.id}, created_by=created_by, max_attempts=3)


def rule_failures(db: Session, v: KbVersion) -> tuple[list[str], list[citations.Citation]]:
    cites = citations.for_version(db, v)
    text = f"{v.title}\n{v.summary}\n{v.applicability}\n{v.body}"
    failed = []
    if citations.pending(cites):
        failed.append("citations")
    from ..project.quality import qualification_hits

    if qualification_hits(text):
        failed.append("boundary")
    if guard.check(text).blocked:
        failed.append("confidential")
    if (not v.summary.strip() or not v.applicability.strip()
            or (v.item.doc_type in ("REF", "MAP", "ENF") and not v.ref_document.strip())):
        failed.append("metadata")
    return failed, cites


def _source_texts(db: Session, cites: list[citations.Citation]) -> list[str]:
    out, total, seen = [], 0, set()
    for c in cites:
        if not c.found or c.src_doc_id in seen:
            continue
        seen.add(c.src_doc_id)
        item = db.scalar(select(KbItem).where(KbItem.doc_id == c.src_doc_id))
        cur = db.get(KbVersion, item.current_version_id) if item is not None and item.current_version_id else None
        body = cur.body if cur is not None else ""
        if c.catalog:
            body = f"（条款目录，没有正文）{cur.title if cur is not None else ''}"
        piece = f"## {c.label()}（{c.src_doc_id}，{c.ref_status or '状态未知'}）\n{body[:MAX_SOURCE]}"
        if total + len(piece) > MAX_SOURCES_TOTAL:
            out.append(f"## 其余 {len(cites) - len(out)} 处引用的原文超出长度，未附")
            break
        out.append(piece)
        total += len(piece)
    return out


def _message(db: Session, v: KbVersion, cites: list[citations.Citation]) -> str:
    item = v.item
    similar = rv.similar_items(db, v)
    sim = "\n".join(f"- {s['doc_id']} {s['title']}" for s in similar) or "（没有找到相近条目）"
    sources = "\n\n".join(_source_texts(db, cites)) or "（正文没有可核对的引用）"
    return (f"# 待审条目\n交付物类型：{item.doc_type}　行业：{'、'.join(item.industries or [])}　服务产品：{item.product}\n"
            f"标题：{v.title}\n摘要：{v.summary}\n适用条件：{v.applicability}\n"
            f"依据文件：{v.ref_document} {v.ref_version} {v.ref_code}\n来源：{v.source_detail}\n\n"
            f"## 正文\n{v.body[:MAX_BODY]}\n\n# 引用到的依据原文\n{sources}\n\n# 相近条目\n{sim}")


def _clean(data: dict) -> tuple[dict, list[dict], str]:
    checks = {}
    raw = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    for k in CHECKS:
        c = raw.get(k) if isinstance(raw.get(k), dict) else {}
        checks[k] = {"pass": c.get("pass") is True, "reason": str(c.get("reason") or "没有给出")[:300]}
    issues = []
    for x in (data.get("issues") or [])[:30]:
        if isinstance(x, dict) and str(x.get("problem", "")).strip():
            sev = x.get("severity") if x.get("severity") in SEVERITIES else "重要"
            issues.append({"severity": sev, "location": str(x.get("location", ""))[:100],
                           "problem": str(x["problem"])[:300], "suggestion": str(x.get("suggestion", ""))[:300]})
    conclusion = data.get("conclusion") if data.get("conclusion") in ("通过", "存疑") else "存疑"
    return checks, issues, conclusion


def _agent(db: Session, v: KbVersion, cites: list[citations.Citation]) -> tuple[str, str, dict, str, str]:
    """返回 (状态 ok/skipped/failed, 说明, findings, 提示词版本, 模型)。"""
    try:
        doc = call.load(db, AGENT)
    except call.AgentUnavailable:
        return "skipped", "知识审核 Agent 未登记、已停用或 md 缺失", {}, "", ""
    try:
        r = call.call(db, AGENT, _message(db, v, cites), actor=v.submitted_by or AGENT, doc=doc, extra=OUTPUT_RULES,
                      level=LEVEL, parse=call.json_object)
    except gateway.BlockedError:
        return "failed", "审查材料含机密级内容，未发送给模型", {}, doc.version, ""
    except gateway.GatewayError:
        return "failed", "模型调用失败", {}, doc.version, ""
    except call.OutputInvalid as e:
        return "failed", "Agent 返回格式无法解析", {}, doc.version, e.completion.model
    out = r.completion
    if out.demo:
        return "skipped", "未接入模型（演示模式）", {}, doc.version, out.model
    checks, issues, conclusion = _clean(r.data)
    return "ok", "", {"checks": checks, "issues": issues, "conclusion": conclusion}, doc.version, out.model


def agent_passed(findings: dict) -> bool:
    return (findings.get("conclusion") == "通过"
            and all(c.get("pass") for c in findings.get("checks", {}).values())
            and len(findings.get("checks", {})) == len(CHECKS)
            and not any(i["severity"] in ("阻断", "重要") for i in findings.get("issues", [])))


def sample_rate(db: Session) -> float:
    s = autopublish.sample_stats(db, KIND)
    rate = get_settings().kb_sample_rate_agent
    if s["checked"] and s["failed"] / s["checked"] > s["fail_max"] / 2:
        rate *= 2
    return min(1.0, rate)


def run(db: Session, v: KbVersion) -> Outcome:
    if v.status != "in_review" or not (eligible(v) or advisory(v)):
        return Outcome("skipped", note="不在待审状态或不适用")
    if any(r.review_role == ROLE for r in v.reviews):
        return Outcome("skipped", note="已审过")
    failed, cites = rule_failures(db, v)
    status, note, findings, prompt_version, model = _agent(db, v, cites)
    findings = {**findings, "rules_failed": failed, "agent": status, "note": note}
    if advisory(v):
        return _advise(db, v, failed, status, findings, prompt_version, model)
    suspended = autopublish.suspended(db, KIND)
    ok = not failed and status == "ok" and agent_passed(findings) and not suspended
    if suspended and not failed and status == "ok" and agent_passed(findings):
        findings["note"] = f"Agent 审核通过，但自动发布已暂停（{suspended}），转人工审核"
    summary = ("知识审核 Agent：七项检查通过" if ok else
               "知识审核 Agent：存疑，转人工审核"
               + (f"；程序规则未过：{'、'.join(RULES[k] for k in failed)}" if failed else "")
               + (f"；{findings['note']}" if findings["note"] else ""))
    db.add(KbReview(version_id=v.id, reviewer=REVIEWER, review_role=ROLE, decision="approve" if ok else "flag",
                    comment=summary[:1000], findings=findings))
    out = Outcome("published" if ok else "manual", rules_failed=failed, agent=status, note=note)
    if ok:
        v.auto_review = KIND
        out.sampled = secrets.SystemRandom().random() < sample_rate(db)
        if out.sampled:
            v.sample_state = "pending"
        db.commit()
        rv.publish(db, v)
    else:
        db.commit()
    db.refresh(v)
    audit.record(db, actor=AGENT, action="kb.agent_review", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, outcome=out.status, rules_failed=failed, agent=AGENT, agent_status=status,
                 prompt_version=prompt_version, model=model, issues=len(findings.get("issues", [])),
                 sampled=out.sampled, suspended=bool(suspended))
    return out


def _advise(db: Session, v: KbVersion, failed: list[str], status: str, findings: dict, prompt_version: str,
            model: str) -> Outcome:
    """第三级：只把程序规则和 Agent 的意见记在审核页，不发布、不抽检；由一位知识专家（不能是作者）拍板。"""
    clean = not failed and status == "ok" and agent_passed(findings)
    findings["level"] = 3
    summary = ("知识审核 Agent（第三级，只出意见）：" + ("七项检查未见问题" if clean else "有问题，见下方清单")
               + (f"；程序规则未过：{'、'.join(RULES[k] for k in failed)}" if failed else "")
               + (f"；{findings['note']}" if findings["note"] else "") + "。由一位知识专家拍板发布")
    db.add(KbReview(version_id=v.id, reviewer=REVIEWER, review_role=ROLE, decision="approve" if clean else "flag",
                    comment=summary[:1000], findings=findings))
    db.commit()
    db.refresh(v)
    audit.record(db, actor=AGENT, action="kb.agent_review", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, outcome="advice", level=3, rules_failed=failed, agent=AGENT,
                 agent_status=status, prompt_version=prompt_version, model=model,
                 issues=len(findings.get("issues", [])))
    return Outcome("manual", rules_failed=failed, agent=status, note=findings.get("note", ""))


@jobs.handler("kb.agent_review")
def _job(payload: dict, ctx: jobs.JobContext) -> None:
    from ..core.db import SessionLocal

    with SessionLocal() as db:
        v = db.get(KbVersion, int(payload["version_id"]))
        if v is not None:
            run(db, v)
