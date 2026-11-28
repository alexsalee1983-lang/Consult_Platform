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
- “不重复、不冲突”先过规则预判（四期试用缺陷，负责人 2026-11-25 同意，app/kb/conflict_rules.py）：真重复的第二级版本
  直接退回、不调模型；同一依据文件的不同条款、较新版本、原文，管辖地或行业不同的判不冲突；依据版本较旧的判不过转人工；
  其余相近条目连同依据文件、版本、行业和摘要交 Agent。规则判掉全部相近条目时这一项以规则为准。
- 积压重跑：python -m app.cli kb-agent-review-rerun，对规则预判上线前“不重复、不冲突”没过的待审版本各重跑一次（rerun）。
- 收录标准与判定（四期 B43，app/kb/intake_criteria.py）：Agent 同时按 6 条收录标准和按类型的固定问题给出四种判定之一，
  记在 findings.intake / findings.verdict；只有判定“收录”的才可能自动发布。KB_INTAKE_VERDICT_LIVE 开启后，“不收 /
  合并为印证 / 退回补充”由平台直接退回（status=rejected），关闭时只转人工。规则判真重复记“合并为印证”。
- 公开资料合规关（四期 B43，app/kb/compliance_auto.py）：第二级版本审完即按程序规则 + Agent 意见判合规，通过的平台记
  合规审核通过（内容存疑的只剩内容审核），例外才留给知识管理员。
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import call
from ..core import audit
from ..core.config import get_settings
from ..core.db import utcnow
from ..models import KbItem, KbReview, KbVersion
from ..services import gateway, guard, jobs
from . import autopublish, candidate, citations, compliance_auto, conflict_rules
from . import intake_criteria as ic
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
 "issues": [{"severity": "阻断 | 重要 | 建议", "check": "对应的检查项键，如 citation_fit", "location": "位置", "problem": "问题", "suggestion": "修改建议"}],
 "conclusion": "通过 | 存疑"}

七项必须都给出；拿不准的 pass 写 false、理由以“存疑：”开头。
no_conflict 只对照“相近条目”一节逐条列出摘要的条目判断：与其中某条内容重复或要求矛盾才写 false，理由里写出对方的 KB 编号；
“已由规则判定”的条目不再判断；没有列出相近条目时写 true。
在同一个 JSON 对象里""" + ic.OUTPUT_FIELDS


@dataclass
class Outcome:
    status: str                              # published / manual / rejected（规则判真重复）/ skipped（不适用或已审过）
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


def expert_input(v: KbVersion) -> bool:
    """专家输入（四期 B38①）：人工作者的版本，即第三级（项目沉淀、专家萃取、内部制度改造，含专家工作台答题生成的版本）。
    其余（公开资料由 Agent 起草）保持严格规则。"""
    return rv.level3(v)


# 专家输入时 Agent 的问题和检查项只有这三类保持“阻断”，其余降为建议
EXPERT_BLOCKING = ("citation_fit", "boundary", "confidential")


def tidy_format(text: str) -> str:
    """专家输入的程序化整理：空白、全半角、条款编号；不改语义。"""
    table = {**{0xFF10 + i: 0x30 + i for i in range(10)}, **{0xFF21 + i: 0x41 + i for i in range(26)},
             **{0xFF41 + i: 0x61 + i for i in range(26)}, 0x3000: 0x20, 0xFF0E: 0x2E}
    s = str(text or "").translate(table)
    s = re.sub(r"第\s*([0-9]+|[一二三四五六七八九十百千零〇两]+)\s*(条|款|章|节|项)", r"第\1\2", s)
    s = re.sub(r"(?<=[一-鿿]),[ ]*", "，", s)
    s = re.sub(r"(?<=[一-鿿]);[ ]*", "；", s)
    s = re.sub(r"(?<=[一-鿿]):(?!\d|//)[ ]*", "：", s)
    s = re.sub(r"(?<=[一-鿿])\?", "？", s)
    s = re.sub(r"(?<=[一-鿿])!", "！", s)
    s = "\n".join(line.rstrip().replace("\t", " ") for line in s.splitlines())
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def tidy_version(v: KbVersion) -> bool:
    """整理专家输入版本的标题、摘要、适用条件、正文；有改动返回 True（调用方提交事务）。"""
    changed = False
    for f in ("title", "summary", "applicability", "body"):
        old = getattr(v, f) or ""
        new = tidy_format(old)
        if new != old:
            setattr(v, f, new)
            changed = True
    return changed


def _metadata_missing(v: KbVersion) -> bool:
    return (not v.summary.strip() or not v.applicability.strip()
            or (v.item.doc_type in ("REF", "MAP", "ENF") and not v.ref_document.strip()))


def rule_advice(v: KbVersion, cites: list[citations.Citation]) -> list[str]:
    """专家输入时降为建议的程序规则（不阻断）：元数据缺项、引用尚未核对（库里没有对应依据原文）。"""
    out = []
    if any(not c.found and not c.doc_known for c in cites):
        out.append("citations")
    if _metadata_missing(v):
        out.append("metadata")
    return out


def rule_failures(db: Session, v: KbVersion) -> tuple[list[str], list[citations.Citation]]:
    cites = citations.for_version(db, v)
    text = f"{v.title}\n{v.summary}\n{v.applicability}\n{v.body}"
    failed = []
    expert = expert_input(v)
    # 专家输入只拦“引用与原文对不上”；尚未核对（库里没有该文件）给建议
    if any(not c.found and c.doc_known for c in cites) if expert else citations.pending(cites):
        failed.append("citations")
    from ..project.quality import qualification_hits

    if qualification_hits(text):
        failed.append("boundary")
    if guard.check(text).blocked:
        failed.append("confidential")
    if not expert and _metadata_missing(v):
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


def _message(db: Session, v: KbVersion, cites: list[citations.Citation],
             pre: conflict_rules.Prejudge | None = None) -> str:
    item = v.item
    sim = conflict_rules.agent_block(pre if pre is not None else conflict_rules.prejudge(db, v))
    sources = "\n\n".join(_source_texts(db, cites)) or "（正文没有可核对的引用）"
    return (f"# 待审条目\n交付物类型：{item.doc_type}　行业：{'、'.join(item.industries or [])}　服务产品：{item.product}\n"
            f"标题：{v.title}\n摘要：{v.summary}\n适用条件：{v.applicability}\n"
            f"依据文件：{v.ref_document} {v.ref_version} {v.ref_code}\n来源：{v.source_detail}\n\n"
            f"## 正文\n{v.body[:MAX_BODY]}\n\n# 引用到的依据原文\n{sources}\n\n# 相近条目\n{sim}\n\n"
            f"{ic.prompt_block(item.doc_type, item.knowledge_kind)}")


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
            issues.append({"severity": sev, "check": str(x.get("check", ""))[:30], "location": str(x.get("location", ""))[:100],
                           "problem": str(x["problem"])[:300], "suggestion": str(x.get("suggestion", ""))[:300]})
    conclusion = data.get("conclusion") if data.get("conclusion") in ("通过", "存疑") else "存疑"
    return checks, issues, conclusion


def _agent(db: Session, v: KbVersion, cites: list[citations.Citation],
           pre: conflict_rules.Prejudge | None = None) -> tuple[str, str, dict, str, str]:
    """返回 (状态 ok/skipped/failed, 说明, findings, 提示词版本, 模型)。
    三期 G2：以 role="review" 调网关，排除条目起草模型的家族（G2③ 起记在版本上）；没记的网关按生成首选的家族排除；
    配置了模型目录时 findings 记 model_family 和 degraded（同家族降级）。"""
    try:
        doc = call.load(db, AGENT)
    except call.AgentUnavailable:
        return "skipped", "知识审核 Agent 未登记、已停用或 md 缺失", {}, "", ""
    try:
        r = call.call(db, AGENT, _message(db, v, cites, pre), actor=v.submitted_by or AGENT, doc=doc, extra=OUTPUT_RULES,
                      level=LEVEL, parse=call.json_object, role="review",
                      exclude_families=[v.model_family] if v.model_family else ())
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
    findings = {"checks": checks, "issues": issues, "conclusion": conclusion,
                "intake": ic.clean(r.data, v.item.doc_type, v.item.knowledge_kind)}
    if out.family:
        findings |= {"model_family": out.family, "degraded": out.degraded}
    return "ok", "", findings, doc.version, out.model


def _hetero(findings: dict) -> dict:
    """审计只多记家族和是否降级（只在配置了模型目录时有）。"""
    return {k: findings[k] for k in ("model_family", "degraded") if k in findings}


def relax_expert(findings: dict) -> dict:
    """专家输入：只有 citation_fit / boundary / confidential 保持阻断；其他问题的阻断与重要降为建议并记原级别，
    其他检查项没过也只记建议。"""
    issues = []
    for i in findings.get("issues", []):
        if i["severity"] in ("阻断", "重要") and i.get("check") not in EXPERT_BLOCKING:
            i = {**i, "severity": "建议", "original_severity": i["severity"]}
        issues.append(i)
    checks = {k: ({**c, "advisory": True} if not c.get("pass") and k not in EXPERT_BLOCKING else c)
              for k, c in findings.get("checks", {}).items()}
    return {**findings, "issues": issues, "checks": checks, "relaxed": True}


def expert_passed(findings: dict) -> bool:
    checks = findings.get("checks", {})
    return (len(checks) == len(CHECKS)
            and all(checks[k].get("pass") for k in EXPERT_BLOCKING)
            and not any(i["severity"] in ("阻断", "重要") for i in findings.get("issues", [])))


def agent_passed(findings: dict) -> bool:
    if findings.get("relaxed"):
        return expert_passed(findings)
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


def run(db: Session, v: KbVersion, rerun: bool = False) -> Outcome:
    if v.status != "in_review" or not (eligible(v) or advisory(v)):
        return Outcome("skipped", note="不在待审状态或不适用")
    if any(r.review_role == ROLE for r in v.reviews) and not (rerun and conflict_rules.rerun_due(v)):
        return Outcome("skipped", note="已审过")
    tidied = False
    if expert_input(v):
        tidied = tidy_version(v)
        if tidied:
            db.commit()
    failed, cites = rule_failures(db, v)
    pre = conflict_rules.prejudge(db, v)
    if pre.verdict == "duplicate" and not advisory(v):
        return _duplicate(db, v, failed, pre, rerun)
    status, note, findings, prompt_version, model = _agent(db, v, cites, pre)
    findings = conflict_rules.apply({**findings, "rules_failed": failed, "agent": status, "note": note}, pre)
    if expert_input(v):
        findings = relax_expert(findings) | {"rules_advice": rule_advice(v, cites),
                                              "format_tidied": tidied}
        if tidied:
            findings["note"] = (findings["note"] + "；" if findings["note"] else "") + "已自动整理格式"
    live = get_settings().kb_intake_verdict_live
    decision = ic.decide(findings.get("intake", {}), v.item.doc_type, v.item.knowledge_kind, rules_failed=failed,
                         checks_passed=status == "ok" and agent_passed(findings), live=live)
    findings["verdict"] = decision.to_json()
    if advisory(v):
        return _advise(db, v, failed, status, findings, prompt_version, model)
    if live and status == "ok" and decision.verdict != "include":
        return _verdict_return(db, v, failed, findings, decision, prompt_version, model, rerun)
    suspended = autopublish.suspended(db, KIND)
    ok = (not failed and status == "ok" and agent_passed(findings) and decision.verdict == "include"
          and not suspended)
    if suspended and not failed and status == "ok" and agent_passed(findings):
        findings["note"] = f"Agent 审核通过，但自动发布已暂停（{suspended}），转人工审核"
    summary = ("知识审核 Agent：七项检查通过，收录判定：收录" if ok else
               "知识审核 Agent：存疑，转人工审核"
               + (f"；程序规则未过：{'、'.join(RULES[k] for k in failed)}" if failed else "")
               + (f"；收录判定：{decision.label}（{'、'.join(decision.reasons[:4])}）"
                  if status == "ok" and decision.verdict != "include" else "")
               + (f"；{findings['note']}" if findings["note"] else ""))
    _save(db, v, decision="approve" if ok else "flag", comment=summary[:1000], findings=findings)
    compliance_auto.run(db, v, findings)        # 四期 B43：合规关自动为主，例外才留给知识管理员
    out = Outcome("published" if ok else "manual", rules_failed=failed, agent=status, note=note)
    if ok:
        v.auto_review = KIND
        # 同家族降级（G2）的审核独立性不足，一律进抽检
        out.sampled = bool(findings.get("degraded")) or secrets.SystemRandom().random() < sample_rate(db)
        if out.sampled:
            v.sample_state = "pending"
        db.commit()
        if candidate.enabled():      # 候选影子期（三期 B24①）：先当候选，达标后晋级
            candidate.stage(db, v, AGENT)
        else:
            rv.publish(db, v)
    else:
        db.commit()
    db.refresh(v)
    audit.record(db, actor=AGENT, action="kb.agent_review", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, outcome=out.status, rules_failed=failed, agent=AGENT, agent_status=status,
                 prompt_version=prompt_version, model=model, issues=len(findings.get("issues", [])),
                 sampled=out.sampled, suspended=bool(suspended), rerun=rerun, prejudge=_counts(pre),
                 verdict=decision.verdict, **_hetero(findings))
    return out


def _verdict_return(db: Session, v: KbVersion, failed: list[str], findings: dict, decision: ic.Decision,
                    prompt_version: str, model: str, rerun: bool) -> Outcome:
    """KB_INTAKE_VERDICT_LIVE 开启：不收、合并为印证、退回补充由平台直接退回（同人工驳回，改了再建新版本提交）。"""
    merge_with = findings.get("intake", {}).get("merge_with", "")
    _save(db, v, decision="reject",
          comment=(f"知识审核 Agent：收录判定“{decision.label}”，平台退回（{'、'.join(decision.reasons[:6])}）"
                   + (f"；印证 {merge_with}" if decision.verdict == "merge" and merge_with else ""))[:1000],
          findings=findings)
    v.status = "rejected"
    db.commit()
    db.refresh(v)
    audit.record(db, actor=AGENT, action="kb.agent_review", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, outcome="verdict_return", verdict=decision.verdict, rules_failed=failed,
                 agent=AGENT, agent_status="ok", prompt_version=prompt_version, model=model,
                 issues=len(findings.get("issues", [])), rerun=rerun, **_hetero(findings))
    return Outcome("rejected", rules_failed=failed, agent="ok", note=decision.label)


def _save(db: Session, v: KbVersion, decision: str, comment: str, findings: dict) -> None:
    """记 Agent 审核意见；积压重跑（rerun）时覆盖原来那条（每个版本一条 Agent 记录），原结论记在 findings.previous。"""
    old = next((r for r in v.reviews if r.review_role == ROLE and r.reviewer == REVIEWER), None)
    if old is None:
        v.reviews.append(KbReview(reviewer=REVIEWER, review_role=ROLE, decision=decision, comment=comment,
                                  findings=findings))       # 挂到关系上，同一会话里发布时读得到（会话提交后不过期）
        return
    findings = {**findings, "previous": {"decision": old.decision, "comment": (old.comment or "")[:300]}}
    old.decision, old.comment, old.findings, old.created_at = decision, comment, findings, utcnow()


def _counts(pre: conflict_rules.Prejudge) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in pre.relations:
        out[r.kind] = out.get(r.kind, 0) + 1
    return out


def _duplicate(db: Session, v: KbVersion, failed: list[str], pre: conflict_rules.Prejudge, rerun: bool) -> Outcome:
    """规则判真重复（正文规范化后与已发布条目相同）：本版本不发布，退回（同人工驳回），不调模型。"""
    ids = [r.doc_id for r in pre.of("duplicate")]
    findings = {"checks": {"no_conflict": pre.check()}, "issues": [], "conclusion": "存疑", "rules_failed": failed,
                "agent": "skipped", "note": "规则预判真重复，未交 Agent", "prejudge": pre.summary(), "duplicate_of": ids,
                "verdict": ic.Decision("merge", ["规则预判真重复"], False).to_json()}
    _save(db, v, decision="reject",
          comment=(f"知识审核 Agent（规则预判）：与已发布的 {'、'.join(ids)} 正文相同，真重复，"
                   "收录判定“合并为印证”，退回、不发布")[:1000],
          findings=findings)
    v.status = "rejected"
    db.commit()
    db.refresh(v)
    audit.record(db, actor=AGENT, action="kb.agent_review", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, outcome="duplicate", duplicate_of=ids, rules_failed=failed, agent=AGENT,
                 agent_status="skipped", rerun=rerun, prejudge=_counts(pre))
    return Outcome("rejected", rules_failed=failed, agent="skipped", note="真重复")


def _advise(db: Session, v: KbVersion, failed: list[str], status: str, findings: dict, prompt_version: str,
            model: str) -> Outcome:
    """第三级：只把程序规则和 Agent 的意见记在审核页，不发布、不抽检；由一位知识专家（不能是作者）拍板。"""
    clean = not failed and status == "ok" and agent_passed(findings)
    findings["level"] = 3
    summary = ("知识审核 Agent（第三级，只出意见）：" + ("七项检查未见问题" if clean else "有问题，见下方清单")
               + (f"；程序规则未过：{'、'.join(RULES[k] for k in failed)}" if failed else "")
               + (f"；{findings['note']}" if findings["note"] else "") + "。由一位知识专家拍板发布")
    _save(db, v, decision="approve" if clean else "flag", comment=summary[:1000], findings=findings)
    db.commit()
    db.refresh(v)
    audit.record(db, actor=AGENT, action="kb.agent_review", result="allowed", doc_id=v.item.doc_id,
                 version=v.version, outcome="advice", level=3, rules_failed=failed, agent=AGENT,
                 agent_status=status, prompt_version=prompt_version, model=model,
                 issues=len(findings.get("issues", [])), advice=len(findings.get("rules_advice", [])),
                 tidied=bool(findings.get("format_tidied")), prejudge=findings.get("prejudge", {}).get("verdict", ""),
                 **_hetero(findings))
    return Outcome("manual", rules_failed=failed, agent=status, note=findings.get("note", ""))


@jobs.handler("kb.agent_review")
def _job(payload: dict, ctx: jobs.JobContext) -> None:
    from ..core.db import SessionLocal

    with SessionLocal() as db:
        v = db.get(KbVersion, int(payload["version_id"]))
        if v is not None:
            run(db, v, rerun=bool(payload.get("rerun")))
