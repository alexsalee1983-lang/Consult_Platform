"""四期 E2①：调研助手平台侧规则（表见 app/models/survey.py，迁移 0056）。

- 题库：E1 题库文件导入（knowledge/question_bank/*.yaml 格式；正文由线 2 出、在资料仓），按题号增改；
  封闭题选项锚定可观察状态，X 保留给“不清楚”；评分等级、锚点不入库、不进调研包；题面和选项过 guard；problem 题停用。
- 选题：启用的题里产品代码对上项目服务产品，按题号排序；项目行业有变体时换成行业题面。
- 调研包：JSON（项目号、档位、题目），包哈希 = 去掉 pack_hash 后规范化 JSON 的 SHA-256；
  标准档带画像（服务产品、行业），严格档不带任何客户信息字段。
- 回传：核对包哈希、题号属于本包、选项合法；任何一条不过整批拒收（不落库）。
  封闭题程序直转“待确认事实”，“不清楚”只给题目计数；开放题过 guard，严格档还须有有效客户同意、
  按摘要卡规则检查（受访角色同样检查）。同一题不同角色答案不一致标“矛盾”。
- 确认单：确认一条事实只记确认人角色；同题其他条转“被取代”，矛盾解除。
- 答案池（四期 E3①，迁移 0057）：每条答案记来源渠道（资料预填 / 客户自评包 / 现场题卡 / 开放讨论）和可信度来源
  （现场证据 > 客户资料 > 现场口述 > 客户自评）。自动确认只在：非关键控制项、同题无矛盾、有高可信来源（客户资料或现场证据）；
  取可信度最高的一条为 auto，其余一致的转“被取代”。只有客户自评或现场口述的、矛盾的、关键控制项的留给人。
  已自动确认的题来了不一致答案 → 退回待确认并标矛盾。自动确认按 spot_rate() 比例进抽检队列，抽检不通过退回人工。
审计只记计数、包号、哈希和答案编号，不记答案原文。
"""
import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..core.db import utcnow
from ..models import Project, User
from ..models.survey import SurveyAnswer, SurveyPack, SurveyQuestion
from ..services import guard
from ..agents.knowledge import product_code
from . import cards

QID_RE = re.compile(r"[A-Z]{2,8}-[A-Z]{2,8}-\d{3}")
UNCLEAR = "X"                     # E1 题库约定：选项 X = 不清楚，平台加进调研包、只计数不成事实
UNCLEAR_OPTION = {"key": UNCLEAR, "label": "不清楚"}
INDUSTRY_CODES = {"BANK": "银行", "POWER": "电力"}   # E1 行业变体代码 → 项目行业
KINDS = ("closed", "open")
MAX_RESPONSES = 50
PACK_VERSION = 1
# 答案池：渠道 → 允许的可信度来源（第一个是默认）；可信度数值越大越可信
CHANNELS = {"prefill": ("document",), "self_assess": ("self",), "onsite": ("oral", "evidence"),
            "discussion": ("oral",)}
CREDIBILITY = {"evidence": 4, "document": 3, "oral": 2, "self": 1}
HIGH_SOURCES = {"evidence", "document"}


class SurveyError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class AnswerIn:
    qid: str
    value: str = ""
    text: str = ""
    source: str = ""


@dataclass
class ResponseIn:
    respondent_role: str
    answers: list[AnswerIn]


# ---------- 题库 ----------

def _validate(q: dict) -> dict:
    """E1 题库格式（knowledge/question_bank/*.yaml）→ 入库字段。评分等级、锚点、预填来源是内部用的，不入调研包。"""
    qid = str(q.get("id", "")).strip()
    if not QID_RE.fullmatch(qid):
        raise SurveyError(422, f"题号格式不对：{qid[:32]}（应为 产品-域-序号，如 SOC-ORG-001）")
    kind = q.get("kind")
    if kind not in KINDS:
        raise SurveyError(422, f"{qid} 题型只能是 closed 或 open")
    controls = [str(c) for c in q.get("control_ids") or []]
    text, product = str(q.get("stem", "")).strip(), str(q.get("product", "")).strip()
    if not controls or not text or not product:
        raise SurveyError(422, f"{qid} 缺控制项、题面或产品")
    options: list[dict] = []
    if kind == "closed":
        raw = [(str(o.get("key", "")).strip(), str(o.get("text", "")).strip()) for o in q.get("options") or []]
        options = [{"key": k, "label": t} for k, t in raw if k != UNCLEAR]
        if len(options) < 2:
            raise SurveyError(422, f"{qid} 封闭题至少两个选项")
        keys = [o["key"] for o in options]
        if len(set(keys)) != len(keys) or not all(re.fullmatch(r"[A-W]", k) for k in keys):
            raise SurveyError(422, f"{qid} 选项键须唯一的大写字母（X 保留给“不清楚”）")
        if not all(o["label"] for o in options):
            raise SurveyError(422, f"{qid} 选项须写可观察状态")
    variants = {}
    for v in q.get("industry_variants") or []:
        name = INDUSTRY_CODES.get(str(v.get("industry", "")))
        if name is None or not str(v.get("stem", "")).strip():
            raise SurveyError(422, f"{qid} 行业变体的行业代码不认识或缺题面")
        variants[name] = str(v["stem"]).strip()
    if guard.check("\n".join([text, *variants.values(), *(o["label"] for o in options)])).blocked:
        raise SurveyError(422, f"{qid} 题面或选项含机密级内容")
    return {"qid": qid, "control": ",".join(controls)[:64], "kind": kind, "text": text, "options": options,
            "products": [product], "industries": [], "variants": variants,
            "enabled": not q.get("problem", False), "key_control": bool(q.get("key_control", False))}


def import_bank(db: Session, questions: list[dict]) -> tuple[int, int]:
    """整批校验后按题号增改，返回（新增, 更新）。任何一题不过整批不改。"""
    clean = [_validate(q) for q in questions]
    added = updated = 0
    for q in clean:
        row = db.scalar(select(SurveyQuestion).where(SurveyQuestion.qid == q["qid"]))
        if row is None:
            db.add(SurveyQuestion(**q, updated_at=utcnow()))
            added += 1
        else:
            for k, v in q.items():
                setattr(row, k, v)
            row.updated_at = utcnow()
            updated += 1
    db.commit()
    audit.record(db, actor="cli", action="survey.bank_import", result="ok", added=added, updated=updated)
    return added, updated


def load_bank_file(path: Path) -> list[dict]:
    """读一个 E1 题库文件，返回原始题目（导入时再校验）。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list):
        raise SurveyError(422, "题库文件应有 questions 列表（E1 题库格式）")
    return questions


def select_questions(db: Session, project: Project) -> list[SurveyQuestion]:
    """按项目画像选题：产品对上（服务产品名 → 产品代码），行业由题目的行业变体换题面。"""
    product = product_code(project.service)
    rows = db.scalars(select(SurveyQuestion).where(SurveyQuestion.enabled.is_(True)).order_by(SurveyQuestion.qid))
    return [q for q in rows if product in q.products
            and (not q.industries or project.industry in q.industries)]


# ---------- 调研包 ----------

def pack_hash(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def question_json(q: SurveyQuestion, industry: str = "") -> dict:
    text = (q.variants or {}).get(industry, q.text)
    out: dict = {"qid": q.qid, "control": q.control, "kind": q.kind, "text": text}
    if q.kind == "closed":
        out["options"] = [*q.options, UNCLEAR_OPTION]
    return out


def _payload(project: Project, code: str, questions: list[SurveyQuestion]) -> dict:
    payload: dict = {"version": PACK_VERSION, "project": project.code, "code": code, "tier": project.tier,
                     "questions": [question_json(q, project.industry) for q in questions]}
    if project.tier != "strict":
        payload["profile"] = {"service": project.service, "industry": project.industry}
    return payload


def build_pack(db: Session, user: User, project: Project) -> dict:
    questions = select_questions(db, project)
    if not questions:
        raise SurveyError(422, "题库里没有适用本项目画像的题目")
    cards._lock(db, project)
    n = db.scalar(select(func.count()).select_from(SurveyPack).where(SurveyPack.project_id == project.id)) or 0
    code = f"P-{n + 1:02d}"
    payload = _payload(project, code, questions)
    h = pack_hash(payload)
    db.add(SurveyPack(project_id=project.id, code=code, pack_hash=h, tier=project.tier,
                      qids=[q.qid for q in questions], created_by=user.username))
    db.commit()
    audit.record(db, actor=user.username, action="survey.pack", result="ok", project_code=project.code,
                 pack=code, pack_hash=h, questions=len(questions))
    return {**payload, "pack_hash": h}


def list_packs(db: Session, project: Project) -> list[SurveyPack]:
    return list(db.scalars(select(SurveyPack).where(SurveyPack.project_id == project.id).order_by(SurveyPack.id)))


# ---------- 回传 ----------

def _reject(db: Session, user: User, project: Project, reason: str, detail: str) -> SurveyError:
    audit.record(db, actor=user.username, action="survey.results", result="blocked", project_code=project.code,
                 reason=reason)
    return SurveyError(422, detail)


def _check_text(db: Session, user: User, project: Project, text: str, what: str) -> None:
    if guard.check(text).blocked:
        raise _reject(db, user, project, "confidential", f"{what}含机密级内容，整批未收")
    if project.tier == "strict":
        problems = cards.card_problems(text)
        if problems:
            raise _reject(db, user, project, "card_rules",
                          f"严格档只收摘要卡格式：{what}命中 " + "、".join(problems) + "，整批未收")


def _flag_conflicts(db: Session, project: Project) -> int:
    rows = list(db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == project.id,
                                                      SurveyAnswer.fact_status.in_(("pending", "auto")))))
    by_q: dict[str, list[SurveyAnswer]] = {}
    for a in rows:
        by_q.setdefault(a.qid, []).append(a)
    conflicts = 0
    for group in by_q.values():
        clash = len({a.value for a in group}) > 1 and len({(a.respondent_role, a.channel) for a in group}) > 1
        for a in group:
            a.conflict = clash
            if clash and a.fact_status == "auto":          # 自动确认后来了不一致答案：退回人工
                a.fact_status, a.spot_check = "pending", ""
        conflicts += clash
    return conflicts


def spot_rate(db: Session, project: Project | None) -> float:
    """自动确认进抽检队列的比例。F5（#473）自适应调比替换这里，调用方不变。"""
    return get_settings().survey_spot_rate


def _auto_confirm(db: Session, user: User, project: Project) -> int:
    """按答案池规则自动确认，返回本次新自动确认的题数；审计只记题号、答案编号和抽检数。"""
    rows = list(db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == project.id,
                                                      SurveyAnswer.kind == "closed",
                                                      SurveyAnswer.fact_status.in_(("pending", "auto")))
                           .order_by(SurveyAnswer.id)))
    by_q: dict[str, list[SurveyAnswer]] = {}
    for a in rows:
        by_q.setdefault(a.qid, []).append(a)
    key = set(db.scalars(select(SurveyQuestion.qid).where(SurveyQuestion.qid.in_(by_q),
                                                         SurveyQuestion.key_control.is_(True))))
    rate = spot_rate(db, project)
    picked: list[SurveyAnswer] = []
    for qid, group in by_q.items():
        if (qid in key or not {a.source for a in group} & HIGH_SOURCES
                or any(a.conflict or a.spot_check == "failed" for a in group)):   # 抽检不通过的题不再自动确认
            continue
        best = next((a for a in group if a.fact_status == "auto"), None)
        if best is None:
            best = max(group, key=lambda a: CREDIBILITY.get(a.source, 0))
            best.fact_status, best.confirmed_at = "auto", utcnow()
            best.spot_check = "pending" if random.random() < rate else ""
            picked.append(best)
        for a in group:
            if a is not best:
                a.fact_status = "superseded"
    if picked:
        db.flush()
        audit.record(db, actor=user.username, action="survey.auto_confirm", result="ok", project_code=project.code,
                     answer_ids=[a.id for a in picked], qids=[a.qid for a in picked],
                     sampled=sum(a.spot_check == "pending" for a in picked))
    return len(picked)


def import_results(db: Session, user: User, project: Project, pack_code: str, given_hash: str,
                   responses: list[ResponseIn], channel: str = "onsite") -> dict:
    pack = db.scalar(select(SurveyPack).where(SurveyPack.project_id == project.id, SurveyPack.code == pack_code))
    if pack is None:
        raise SurveyError(404, "调研包不存在")
    if given_hash != pack.pack_hash:
        raise _reject(db, user, project, "hash_mismatch", "调研包哈希不符：回传的不是本平台下发的这份包")
    if channel not in CHANNELS:
        raise SurveyError(422, "来源渠道只能是 prefill / self_assess / onsite / discussion")
    if not responses or len(responses) > MAX_RESPONSES:
        raise SurveyError(422, f"回传须有 1～{MAX_RESPONSES} 份答卷")
    if project.tier == "strict" and cards.active_consent(db, project) is None:
        raise _reject(db, user, project, "no_consent", "严格档须先登记客户书面同意，才能接收调研摘要")
    qs = {q.qid: q for q in db.scalars(select(SurveyQuestion).where(SurveyQuestion.qid.in_(pack.qids)))}
    rows: list[SurveyAnswer] = []
    unclear: list[str] = []
    for r in responses:
        role = r.respondent_role.strip()
        if not role or len(role) > 32:
            raise SurveyError(422, "受访角色必填，不超过 32 字（写角色不写姓名）")
        _check_text(db, user, project, role, "受访角色")
        for a in r.answers:
            q = qs.get(a.qid)
            if q is None:
                raise _reject(db, user, project, "unknown_question", f"{a.qid[:32]} 不在本调研包里")
            source = a.source or CHANNELS[channel][0]
            if source not in CHANNELS[channel]:
                raise SurveyError(422, f"渠道 {channel} 的来源只能是 " + " / ".join(CHANNELS[channel]))
            base = {"project_id": project.id, "pack_id": pack.id, "qid": q.qid, "control": q.control,
                    "respondent_role": role, "kind": q.kind, "channel": channel, "source": source}
            if q.kind == "closed":
                if a.value == UNCLEAR:
                    unclear.append(q.qid)
                    continue
                if a.value not in {o["key"] for o in q.options}:
                    raise _reject(db, user, project, "bad_option", f"{q.qid} 的选项 {a.value[:8]} 不存在")
                rows.append(SurveyAnswer(**base, value=a.value, fact_status="pending"))
            else:
                text = a.text.strip()
                if not text:
                    continue
                _check_text(db, user, project, text, f"{q.qid} 的回答")
                rows.append(SurveyAnswer(**base, text=text[:4000]))
    db.add_all(rows)
    for qid in unclear:
        qs[qid].unclear_count += 1
    pack.returned += 1
    db.flush()
    conflicts = _flag_conflicts(db, project)
    auto = _auto_confirm(db, user, project)
    db.commit()
    out = {"facts": sum(a.kind == "closed" for a in rows), "unclear": len(unclear),
           "open": sum(a.kind == "open" for a in rows), "conflicts": conflicts, "auto_confirmed": auto}
    audit.record(db, actor=user.username, action="survey.results", result="ok", project_code=project.code,
                 pack=pack.code, pack_hash=pack.pack_hash, responses=len(responses), channel=channel, **out)
    return out


# ---------- 事实确认单 ----------

def list_answers(db: Session, project: Project) -> list[SurveyAnswer]:
    return list(db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == project.id)
                           .order_by(SurveyAnswer.qid, SurveyAnswer.id)))


def option_label(db: Session, qid: str, value: str) -> str:
    q = db.scalar(select(SurveyQuestion).where(SurveyQuestion.qid == qid))
    return next((o["label"] for o in (q.options if q else []) if o["key"] == value), "")


def get_answer(db: Session, project: Project, answer_id: int) -> SurveyAnswer | None:
    return db.scalar(select(SurveyAnswer).where(SurveyAnswer.project_id == project.id, SurveyAnswer.id == answer_id))


def confirm_fact(db: Session, user: User, project: Project, fact: SurveyAnswer, confirmed_role: str) -> SurveyAnswer:
    if fact.kind != "closed" or fact.fact_status == "superseded":
        raise SurveyError(422, "只有待确认或已确认的封闭题事实能确认")
    role = confirmed_role.strip()
    if not role or len(role) > 32 or cards.card_problems(role):
        raise SurveyError(422, "确认人只写角色（如“信息科技部负责人”），不写姓名")
    others = db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == project.id,
                                                   SurveyAnswer.qid == fact.qid, SurveyAnswer.kind == "closed",
                                                   SurveyAnswer.id != fact.id))
    for o in others:
        o.fact_status, o.conflict = "superseded", False
    fact.fact_status, fact.conflict = "confirmed", False
    if fact.spot_check == "pending":                       # 人工确认后不用再抽检
        fact.spot_check = ""
    fact.confirmed_role, fact.confirmed_at = role, utcnow()
    db.commit()
    audit.record(db, actor=user.username, action="survey.confirm", result="ok", project_code=project.code,
                 answer_id=fact.id, qid=fact.qid)
    return fact


# ---------- 答案池与抽检 ----------

def pool(db: Session, project: Project) -> list[dict]:
    """按题号汇聚答案。题状态：conflict 冲突 > confirmed 人工确认 > auto 自动确认 > pending 待确认；开放题为空。"""
    by_q: dict[str, list[SurveyAnswer]] = {}
    for a in list_answers(db, project):
        by_q.setdefault(a.qid, []).append(a)
    key = set(db.scalars(select(SurveyQuestion.qid).where(SurveyQuestion.qid.in_(by_q),
                                                         SurveyQuestion.key_control.is_(True))))
    out = []
    for qid, group in by_q.items():
        closed = [a for a in group if a.kind == "closed"]
        status = next((s for s in ("conflict", "confirmed", "auto", "pending")
                       if any(a.conflict if s == "conflict" else a.fact_status == s for a in closed)), "")
        fact = next((a for a in closed if a.fact_status in ("confirmed", "auto")), None)
        out.append({"qid": qid, "control": group[0].control, "key_control": qid in key, "status": status,
                    "value": fact.value if fact and status != "conflict" else "", "answers": group})
    return out


def spot_queue(db: Session, project: Project) -> list[SurveyAnswer]:
    return list(db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == project.id,
                                                      SurveyAnswer.fact_status == "auto",
                                                      SurveyAnswer.spot_check == "pending")
                           .order_by(SurveyAnswer.qid)))


def spot_verdict(db: Session, user: User, project: Project, fact: SurveyAnswer, passed: bool) -> SurveyAnswer:
    """抽检结论：通过保持自动确认；不通过退回待确认，同题被它取代的答案一并退回，由人确认。"""
    if fact.fact_status != "auto" or fact.spot_check != "pending":
        raise SurveyError(422, "只有待抽检的自动确认事实能下抽检结论")
    fact.spot_check = "passed" if passed else "failed"
    if not passed:
        fact.fact_status = "pending"
        for o in db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == project.id,
                                                       SurveyAnswer.qid == fact.qid,
                                                       SurveyAnswer.fact_status == "superseded")):
            o.fact_status = "pending"
        db.flush()
        _flag_conflicts(db, project)
    db.commit()
    audit.record(db, actor=user.username, action="survey.spot_check", result="ok" if passed else "failed",
                 project_code=project.code, answer_id=fact.id, qid=fact.qid)
    return fact


def delete_for_project(db: Session, project_id: int) -> int:
    n = db.execute(delete(SurveyAnswer).where(SurveyAnswer.project_id == project_id)).rowcount or 0
    db.execute(delete(SurveyPack).where(SurveyPack.project_id == project_id))
    return n
