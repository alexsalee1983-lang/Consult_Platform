"""四期 A40：合同要点卡规则检查（只显示例外）、需求澄清清单与纪要回填、变更单草稿。

方案 `docs/交付链输入输出方案.md` 4.1、4.7；规则与题库 `products/contract/`（C23，草稿待产品专家复核）。

- 规则检查（check）：程序按 checks.yaml 逐条查要点卡，通过的不列出，只给一行“其余自动检查已通过”；
  `block`（只有 K-01 资质边界）命中即不能保存；`clarify` 转澄清题；`pm` 进“待我处理”（不出题）。
  `pm_only` 字段（付款、授权、保密）未写明时也只进“待我处理”。
- 出题（clarifications）：每个缺口一道单选题，选项先取产品文件同字段题，没有再用通用题（K-02/K-05/K-06 用规则题干）；
  按影响的交付件数、关口先后排序，同一字段合并为一题；出题后过 guard 与 K-01，命中的选项或题剔除。
  题库本身就是黄金样例整理出的选项，程序直接出题，不调模型（自动化优先，人只在会上问、会后确认冲突）。
- 澄清清单 Word（clarification_docx）：一页，可直接作会议议程。
- 回填（backfill）：会上直接勾选，或导入会议纪要由交付项目经理 Agent 对题抽答案（后台任务，经 agents/call.py）；
  答案明确且不冲突的自动回填，含糊或冲突的出确认，没答到的留在清单（到关口按推荐项起草并标“按默认写法”），
  客户新提且超出合同的起草变更单（项目经理确认，涉及商务走补充协议，签后才转为定制项）。要点卡本身不改。
- 审计只记项目、题数、字数和摘要哈希，不记要点卡、纪要原文和答案。纪要正文暂存在项目区，抽完即删。
"""
from __future__ import annotations

import io
import json
import re
import uuid
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from docx import Document
from docx.shared import Pt
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import call
from ..core import audit
from ..core.db import SessionLocal
from ..core.permissions import membership
from ..models import Job, Project, User
from ..products import config
from ..services import gateway, guard, jobs, storage
from . import configs, evidence

UNWRITTEN = ("合同未写明", "待澄清")
GATES = ("启动", "调研", "分析", "设计", "验收")
PASSED_NOTE = "其余自动检查已通过"
DEFAULT_MARK = "按默认写法"
CHANGE_NOTE = "项目经理确认后生效；涉及费用或合同条款的走补充协议，补充协议签后才转为定制项"
MAX_VALUE = 4000
JOB_KIND = "project.contract_minutes"
ACTION = "contract.minutes"
AGENT = "delivery-pm"
KINDS = ("docx", "markdown", "text")
KINDS_MSG = " Word（.docx）、Markdown（.md）和纯文本（.txt）"
MAX_MINUTES_CHARS = 40_000

_DATE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")
_WEEKS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:个)?(周|星期|个月|月|天|日|工作日)")
_DCODE = re.compile(r"\bD(\d{1,2})\b")
_BOUND = re.compile(r"验收|签收|签署|签字|交付|提交|D\d")
_FOREVER = re.compile(r"永久|无限期|长期保存|不予删除|不删除|不销毁")
_COMMERCIAL = re.compile(r"费用|报价|价格|金额|付款|追加|合同条款")


class ContractError(ValueError):
    def __init__(self, status: int, detail: object):
        super().__init__(str(detail))
        self.status, self.detail = status, detail


# ---- 配置 ----

def _base() -> Path:
    return config.catalog_dir() / "products" / "contract"


@lru_cache(maxsize=16)
def _yaml(path: str, mtime: float) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _read(name: str) -> dict:
    p = _base() / f"{name}.yaml"
    if not p.is_file():
        return {}
    return _yaml(str(p), p.stat().st_mtime)


def rules() -> dict:
    data = _read("checks")
    if not data.get("checks"):
        raise ContractError(503, "要点卡检查规则未配置（products/contract/checks.yaml）")
    return data


def product_rules(product: str) -> dict:
    return _read(product.lower()) if product else {}


def _check(data: dict, cid: str) -> dict:
    return next((c for c in data["checks"] if c["id"] == cid), {})


# ---- 要点卡 ----

def normalize_card(card: dict[str, Any], data: dict) -> dict[str, dict[str, str]]:
    """要点卡 {字段: {value, status}}；只收 checks.yaml 里的字段，值截断，状态不认识的当“已核对”。"""
    status = set(data.get("status") or ())
    out: dict[str, dict[str, str]] = {}
    for f in data.get("fields") or ():
        raw = card.get(f["key"]) or {}
        if isinstance(raw, str):
            raw = {"value": raw}
        value = str(raw.get("value") or "").strip()[:MAX_VALUE]
        st = str(raw.get("status") or "")
        out[f["key"]] = {"value": value, "status": st if st in status else ("已核对" if value else "合同未写明")}
    return out


def _weeks(text: str) -> float | None:
    dates = _DATE.findall(text)
    if len(dates) >= 2:
        try:
            a, b = (date(int(y), int(m), int(d)) for y, m, d in dates[:2])
        except ValueError:
            return None
        return (b - a).days / 7
    m = _WEEKS.search(text)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    return n * {"周": 1, "星期": 1, "个月": 4.33, "月": 4.33, "天": 1 / 7, "日": 1 / 7, "工作日": 0.2}[unit]


def _labels(data: dict) -> dict[str, dict]:
    return {f["key"]: f for f in data.get("fields") or ()}


def check(card: dict[str, Any], *, product: str, tier: str, deliverables: list[dict] | None = None) -> dict:
    """规则检查。返回 blocked（资质边界命中，不能保存）、exceptions（只列例外）、passed（通过条数）、note。"""
    data = rules()
    prod = product_rules(product)
    fields = normalize_card(card, data)
    meta = _labels(data)
    hits: list[dict] = []

    def hit(c: dict, field: str, **extra: Any) -> None:
        action = "pm" if c["action"] == "clarify" and meta.get(field, {}).get("pm_only") else c["action"]
        hits.append({"check": c["id"], "name": c["name"], "field": field, "label": meta.get(field, {}).get("label", field),
                     "action": action, "message": c.get("message", ""), "fix": c.get("fix", ""), **extra})

    for c in data["checks"]:
        targets = list(fields) if "*" in c.get("fields", []) else [k for k in c.get("fields", []) if k in fields]
        cid = c["id"]
        if cid == "K-01":
            pat = re.compile(c["match"])
            for k in targets:
                if (m := pat.search(fields[k]["value"])):
                    hit(c, k, excerpt=m.group(0))
        elif cid == "K-02":
            v = fields.get("acceptance", {}).get("value", "")
            if v and (m := re.search(c["match"], v)):
                hit(c, "acceptance", excerpt=m.group(0))
        elif cid == "K-03":
            pay = fields.get("payment", {}).get("value", "")
            if pay and not _BOUND.search(pay):
                hit(c, "payment")
        elif cid == "K-04":
            w = _weeks(fields.get("period", {}).get("value", ""))
            floor = (prod.get("weeks") or {}).get("min")
            if w is not None and floor and w < float(floor):
                hit(c, "period", weeks=round(w, 1), min_weeks=floor)
        elif cid == "K-05":
            v = fields.get("deliverables", {}).get("value", "")
            need = set(prod.get("deliverables_min") or ())
            if v and need:
                got = {f"D{n}" for n in _DCODE.findall(v)}
                got |= {d["code"] for d in deliverables or () if d.get("title") and d["title"] in v}
                known = {d["code"] for d in deliverables or ()} | need
                missing, extra = sorted(need - got), sorted(x for x in got - known)
                if missing or extra:
                    hit(c, "deliverables", missing=missing, extra=extra)
        elif cid == "K-06":
            want = (c.get("params") or {}).get("when_tier", "standard")
            text = " ".join(fields.get(k, {}).get("value", "") for k in targets)
            if tier == want and (m := re.search(c["match"], text)):
                hit(c, "tier", excerpt=m.group(0))
        elif cid == "K-07":
            ret = fields.get("retention", {}).get("value", "")
            conf = fields.get("confidentiality", {}).get("value", "")
            if _FOREVER.search(ret) or _durations_conflict(ret, conf):
                hit(c, "retention")
        elif cid == "K-08":
            for k in targets:
                f = fields[k]
                if f["status"] in UNWRITTEN or (meta.get(k, {}).get("required") and not f["value"]):
                    hit(c, k)
    total = len(data["checks"])
    failed = len({h["check"] for h in hits})
    return {"blocked": any(h["action"] == "block" for h in hits), "exceptions": hits,
            "passed": total - failed, "note": PASSED_NOTE if total > failed else ""}


def _durations_conflict(a: str, b: str) -> bool:
    ya = re.findall(r"(\d+)\s*年", a)
    yb = re.findall(r"(\d+)\s*年", b)
    return bool(ya and yb and ya[0] != yb[0])


# ---- 出题 ----

def _clean(text: str, k01: re.Pattern) -> bool:
    return not k01.search(text) and not guard.check(text).blocked


def _options(opts: list[dict], k01: re.Pattern) -> list[dict]:
    out = [{"text": o["text"], "recommended": bool(o.get("recommended")), "reason": o.get("reason", ""),
            "golden": list(o.get("golden") or ())} for o in opts or () if _clean(f"{o['text']} {o.get('reason', '')}", k01)]
    if out and not any(o["recommended"] for o in out):
        out[0]["recommended"] = True
    return out


def _bank(data: dict, prod: dict) -> dict[str, list[dict]]:
    bank: dict[str, list[dict]] = {}
    for q in prod.get("questions") or ():
        bank.setdefault(q["field"], []).append(q)
    for q in data.get("questions") or ():
        bank.setdefault(q["field"], []).append(q)      # 产品题在前，通用题兜底
    return bank


K05_OPTIONS = [
    {"text": "按本产品默认组成补齐缺少的交付件，多出的写进范围确认单", "recommended": True,
     "reason": "交付件与输出包组件一致，验收逐件对照"},
    {"text": "按合同清单执行，缺少的写进不包含项"},
]


def clarifications(result: dict, *, product: str) -> list[dict]:
    """规则检查结果 → 澄清题。每个 clarify 例外一道题，同字段合并；按影响排序。"""
    data = rules()
    prod = product_rules(product)
    bank = _bank(data, prod)
    k01 = re.compile(_check(data, "K-01").get("match") or r"(?!x)x")
    seen: dict[str, dict] = {}
    for h in result["exceptions"]:
        if h["action"] != "clarify" or h["field"] in seen:
            continue
        c = _check(data, h["check"])
        field = h["field"]
        cands = bank.get(field, [])
        base = cands[0] if cands else {}
        if h["check"] == "K-05":
            ask, opts = c.get("question", ""), K05_OPTIONS
        elif h["check"] == "K-02":
            ask, opts = c["question"].replace("{原文}", h.get("excerpt", "")), base.get("options", [])
        elif h["check"] == "K-06":
            ask, opts = c.get("question", ""), base.get("options", [])
        else:
            ask = base.get("ask") or c.get("question", "").replace("{字段}", h["label"])
            opts = base.get("options", [])
        options = _options(opts, k01)
        if not ask or len(options) < 2 or not _clean(ask, k01):
            continue
        seen[field] = {"id": base.get("id") or f"{h['check']}-{field}", "field": field, "label": h["label"],
                       "ask": ask, "options": options, "affects": list(base.get("affects") or ()),
                       "gate": base.get("gate") or "启动", "check": h["check"], "message": h["message"]}
    qs = list(seen.values())
    qs.sort(key=lambda q: (-len(q["affects"]), GATES.index(q["gate"]) if q["gate"] in GATES else 9, q["id"]))
    for i, q in enumerate(qs, 1):
        q["no"] = i
    return qs


def todo(result: dict) -> list[dict]:
    """只需项目经理知道的提示（进“待我处理”），同规则同字段只留一条。"""
    out, seen = [], set()
    for h in result["exceptions"]:
        if h["action"] == "pm" and (h["check"], h["field"]) not in seen:
            seen.add((h["check"], h["field"]))
            out.append(h)
    return out


def evaluate(db: Session, project: Project, card: dict[str, Any]) -> dict:
    """接口用：项目快照定产品与交付件，档位按项目。"""
    snap = configs.snapshot(db, project)
    res = check(card, product=snap["product"], tier=project.tier, deliverables=snap.get("deliverables"))
    return {"blocked": res["blocked"],
            "blocks": [h for h in res["exceptions"] if h["action"] == "block"],
            "questions": clarifications(res, product=snap["product"]),
            "todo": todo(res), "passed": res["passed"], "note": res["note"]}


# ---- 澄清清单 Word ----

def clarification_docx(project: Project, questions: list[dict]) -> bytes:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.size = Pt(10.5)
    doc.add_heading(f"{project.code} 需求澄清清单", level=1)
    doc.add_paragraph("会上请客户逐题选择（可勾选或另写）；结论写进 D1 范围确认单，客户签字后固定。")
    for q in questions:
        head = f"{q['no']}. {q['ask']}（{q['label']}；影响 {'、'.join(q['affects']) or '—'}；{q['gate']}关口）"
        doc.add_paragraph().add_run(head).bold = True
        for o in q["options"]:
            tag = f"（推荐：{o['reason']}）" if o["recommended"] and o["reason"] else ("（推荐）" if o["recommended"] else "")
            doc.add_paragraph(f"☐ {o['text']}{tag}")
        doc.add_paragraph("☐ 其他：____________________")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---- 回填与变更单 ----

def change_order(request: dict, deliverables: list[dict]) -> dict:
    """合同外需求 → 变更单草稿：影响哪些交付件、工期、组件；状态 draft，等项目经理确认。"""
    titles = {d["code"]: d.get("title", d["code"]) for d in deliverables}
    text = " ".join(str(request.get("text") or "").split())[:500]
    affects = [c for c in dict.fromkeys(str(x).upper() for x in request.get("deliverables") or ()) if c in titles]
    weeks = request.get("weeks")
    weeks = int(weeks) if isinstance(weeks, (int, float)) and 0 <= weeks <= 52 else None
    return {"request": text, "affects": affects, "components": [titles[c] for c in affects],
            "weeks": weeks, "schedule": f"工期约增加 {weeks} 周" if weeks else "工期影响待项目经理估算",
            "commercial": bool(_COMMERCIAL.search(text)), "status": "draft", "note": CHANGE_NOTE}


def backfill(questions: list[dict], answers: list[dict], *, requests: list[dict] | None = None,
             deliverables: list[dict] | None = None, gates_due: set[str] | frozenset[str] = frozenset(),
             source: str = "会议纪要") -> dict:
    """对题回填。answers：[{question, option（从 1 起）, text, clear, conflict, reason}]。
    明确且不冲突 → filled；含糊或冲突 → confirm（出确认卡）；没答到 → pending，关口已到的按推荐项起草（defaulted）。"""
    by_id = {q["id"]: q for q in questions}
    got: dict[str, dict] = {}
    for a in answers or ():
        if isinstance(a, dict) and a.get("question") in by_id and a["question"] not in got:
            got[a["question"]] = a
    filled, confirm, pending, defaulted = [], [], [], []
    for q in questions:
        a = got.get(q["id"])
        if a is None:
            rec = next((o for o in q["options"] if o["recommended"]), q["options"][0])
            row = {"question": q["id"], "field": q["field"], "ask": q["ask"]}
            if q["gate"] in gates_due:
                defaulted.append({**row, "answer": rec["text"], "mark": DEFAULT_MARK})
            else:
                pending.append(row)
            continue
        opt = a.get("option")
        chosen = q["options"][opt - 1]["text"] if isinstance(opt, int) and 1 <= opt <= len(q["options"]) else ""
        answer = chosen or " ".join(str(a.get("text") or "").split())[:500]
        row = {"question": q["id"], "field": q["field"], "ask": q["ask"], "answer": answer, "source": source}
        if answer and chosen and a.get("clear", True) and not a.get("conflict"):
            filled.append(row)
        else:
            confirm.append({**row, "reason": " ".join(str(a.get("reason") or "").split())[:200]
                            or ("与合同或售前材料矛盾" if a.get("conflict") else "答案含糊，需项目经理确认")})
    orders = [change_order(r, deliverables or []) for r in requests or ()
              if isinstance(r, dict) and r.get("text") and not r.get("in_contract")]
    return {"filled": filled, "confirm": confirm, "pending": pending, "defaulted": defaulted, "change_orders": orders}


def _due_gates(gates: list[str]) -> frozenset[str]:
    return frozenset(g for g in gates if g in GATES)


def answer(db: Session, user: User, project: Project, card: dict[str, Any], answers: list[dict],
           requests: list[dict], gates_due: list[str]) -> dict:
    """会上直接勾选：不调模型，程序回填。"""
    ev = evaluate(db, project, card)
    if ev["blocked"]:
        raise ContractError(422, "要点卡含测评、密评或认证结论类说法，先改为差距分析、符合性初判口径")
    texts = "\n".join(str(x.get("text") or "") for x in [*answers, *requests] if isinstance(x, dict))
    if texts.strip() and guard.check(texts).blocked:
        raise ContractError(422, "答案或新需求命中机密级检测，未回填")
    snap = configs.snapshot(db, project)
    out = backfill(ev["questions"], answers, requests=requests, deliverables=snap.get("deliverables"),
                   gates_due=_due_gates(gates_due), source="会上勾选")
    audit.record(db, actor=user.username, action="contract.answer", result="allowed", project_code=project.code,
                 questions=len(ev["questions"]), filled=len(out["filled"]), change_orders=len(out["change_orders"]))
    db.commit()
    return out


TASK = """

---

## 本次任务：会议纪要对题抽答案

下面给你一份需求澄清清单（每题带编号与选项）和一份会议纪要（已经过机密级检测）。只做对题抽答案，不起草交付件：
- answers：每个在纪要里答到的题一条 {"question": 题号, "option": 选中的选项序号（从 1 起，没对上任何选项给 null）,
  "text": 纪要里的答案要点, "clear": 答案是否明确, "conflict": 是否与合同要点或售前材料矛盾, "reason": 含糊或矛盾的原因}；
  纪要没答到的题不要列。
- requests：客户在会上新提的要求 {"text": 要求要点, "deliverables": 影响的交付件代号, "in_contract": 是否在合同范围内,
  "weeks": 估计增加的工期周数（估不出给 null）}。
只输出一个 JSON 对象 {"answers": [...], "requests": [...]}；不写口令、密钥、IP、个人姓名与联系方式，人员只写岗位；
不出具或暗示测评、认证结论。
"""


def _message(questions: list[dict], card: dict[str, dict[str, str]], text: str) -> str:
    qs = "\n".join(f"- {q['id']}：{q['ask']}  选项：" + "；".join(f"{i}. {o['text']}" for i, o in enumerate(q["options"], 1))
                   for q in questions)
    known = "\n".join(f"- {k}：{v['value'][:300]}" for k, v in card.items() if v["value"])
    return f"澄清清单：\n{qs}\n\n合同要点（已核对）：\n{known or '（无）'}\n\n会议纪要：\n{text[:MAX_MINUTES_CHARS]}"


def _prefix(project: Project, token: str) -> str:
    return f"{project.code}/contract_minutes/{token}/"


def start_minutes(db: Session, user: User, project: Project, card: dict[str, Any], gates_due: list[str],
                  filename: str, data: bytes) -> int:
    ev = evaluate(db, project, card)
    if not ev["questions"]:
        raise ContractError(422, "要点卡没有需要澄清的题，不用导入纪要")
    try:
        text = evidence.extract_text(db, user, project, filename, data, action=ACTION, label="会议纪要",
                                     kinds=KINDS, kinds_msg=KINDS_MSG)
    except evidence.EvidenceError as e:
        raise ContractError(e.status, e.detail) from None
    if not storage.enabled():
        raise ContractError(503, "对象存储未启用，暂时不能导入纪要")
    prefix = _prefix(project, uuid.uuid4().hex)
    card_n = normalize_card(card, rules())
    blob = {"text": text, "card": card_n, "questions": ev["questions"], "gates_due": list(_due_gates(gates_due))}
    storage.put(evidence.AREA, prefix + "input.json", json.dumps(blob, ensure_ascii=False).encode(), "application/json")
    job_id = jobs.enqueue(db, JOB_KIND, {"project": project.code, "user": user.username, "prefix": prefix},
                          created_by=user.username, project_code=project.code, max_attempts=2)
    audit.record(db, actor=user.username, action=ACTION, result="queued", project_code=project.code,
                 chars=len(text), digest=evidence.text_digest(text), questions=len(ev["questions"]), job=job_id)
    db.commit()
    return job_id


def run_minutes(db: Session, user: User, project: Project, prefix: str) -> dict:
    blob = json.loads(storage.get(evidence.AREA, prefix + "input.json"))
    res = call.call(db, AGENT, _message(blob["questions"], blob["card"], blob["text"]), actor=user.username,
                    extra=TASK, project=project, parse=call.json_object)
    data = res.data if isinstance(res.data, dict) else {}
    answers = [a for a in data.get("answers") or () if isinstance(a, dict)]
    requests = [r for r in data.get("requests") or () if isinstance(r, dict)]
    snap = configs.snapshot(db, project)
    out = backfill(blob["questions"], answers, requests=requests, deliverables=snap.get("deliverables"),
                   gates_due=frozenset(blob.get("gates_due") or ()))
    dump = json.dumps(out, ensure_ascii=False)
    if guard.check(dump).blocked:
        raise jobs.JobError("回填结果命中机密级检测，未回填", retry=False)
    storage.put(evidence.AREA, prefix + "result.json", dump.encode(), "application/json")
    storage.delete(evidence.AREA, prefix + "input.json")          # 纪要正文不留存
    return {"filled": len(out["filled"]), "confirm": len(out["confirm"]), "change_orders": len(out["change_orders"]),
            "demo": bool(res.completion.demo)}


def minutes_state(db: Session, project: Project) -> dict:
    job = db.scalars(select(Job).where(Job.kind == JOB_KIND, Job.project_code == project.code)
                     .order_by(Job.id.desc()).limit(1)).first()
    if job is None:
        return {"status": "", "error": "", "result": None}
    result = None
    if job.status == "done":
        try:
            prefix = json.loads(job.payload or "{}")["prefix"]
            result = json.loads(storage.get(evidence.AREA, prefix + "result.json"))
        except (storage.StorageError, KeyError, ValueError):
            return {"status": "failed", "error": "回填结果已不可用，请重新导入", "result": None}
    return {"status": job.status, "error": job.last_error, "result": result}


@jobs.handler(JOB_KIND)
def _job(payload: dict, ctx: jobs.JobContext) -> None:
    with SessionLocal() as db:
        project = db.scalar(select(Project).where(Project.code == payload.get("project", "")))
        user = db.scalar(select(User).where(User.username == payload.get("user", "")))
        if project is None or user is None or not user.is_active or membership(db, project.id, user.id) is None:
            raise jobs.JobError("发起人已不是项目成员或项目不存在", retry=False)
        try:
            out = run_minutes(db, user, project, str(payload.get("prefix", "")))
        except (gateway.GatewayError, call.OutputInvalid, call.AgentUnavailable, call.QualificationBlocked) as e:
            db.rollback()
            audit.record(db, actor=user.username, action=ACTION, result="error", project_code=project.code,
                         reason=type(e).__name__)
            db.commit()
            raise jobs.JobError(f"抽取失败（{type(e).__name__}），可重新导入或会上直接勾选", retry=False) from e
        audit.record(db, actor=user.username, action=ACTION, result="allowed", project_code=project.code, **out)
        db.commit()
