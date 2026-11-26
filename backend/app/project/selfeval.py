"""四期 E3②：客户自评包（表 survey_selfeval_packs，迁移 0058；方案见 docs/驾驶舱改版方案.md 第五节）。

- 生成：从调研包按受访角色派生，只含封闭事实题（题面 + 可观察状态选项 + “不清楚”，不含评分锚定）。
  单个离线网页（selfeval_page.html，CSP 禁止一切网络请求）内嵌加密题单，加密格式同现场工具
  （field-tool/README.md：PBKDF2-SHA256 60 万次 → AES-256-GCM，AAD = cp-field-package:1:selfeval）；
  随机口令只在生成响应里返回一次，不落库、不进审计和日志，由顾问另行告知客户。Excel 模板兜底（XlsxWriter，已装）。
  同一调研包同一角色重新生成 → 版本 +1，未导入的旧版本作废（superseded）。严格档不在平台生成（在现场工具里导入）。
- 导入：客户导出的加密答卷（kind = selfeval-answers）在浏览器本机解密，口令不上传；平台收到的是解密后的答案。
  校验包号与版本、包哈希、题号属于本包、重复导入、机密级（备注和表外内容过 guard，命中整批拒收）；
  全过直接进答案池（渠道 self_assess、来源 self，走 E3① 可信度与自动确认规则）；未答过半、与资料大面积不一致时
  返回 409 要人确认后再导。备注与 Excel 表外内容不入池，列出由顾问决定。可撤销本次导入（已人工确认的不能撤）。
- Excel：标准库 zipfile + xml 读第一张表；第 1 行包号 / 版本 / 校验码，第 2 行表头，第 3 行起 题号、题目、选项、回答、备注。
  回答认选项键（不分大小写）或选项原文。文件不保存。
- 提醒：worker 每次扫描（scan_reminders）按截止日推进提醒阶段：前一天 1、当天 2、逾期 2 天 3（升级项目经理）；
  可重复执行，每升一级记一条审计 survey.selfeval_remind_auto；“我的任务”按阶段列给发包人，阶段 3 另列给项目经理。
审计只记包号、版本、题数、长度和哈希，不记口令、答案和备注原文。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import secrets
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.parsers import expat

import xlsxwriter
from Crypto.Cipher import AES  # nosec B413  pycryptodome（已装，core/secretbox.py 同用）
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..models import Project, User
from ..models.survey import SelfEvalPack, SurveyAnswer, SurveyPack, SurveyQuestion
from ..services import guard
from . import cards, survey
from .survey import SurveyError

ITERATIONS = 600_000
KIND, ANSWERS_KIND = "selfeval", "selfeval-answers"
MARK_START, MARK_END = "/*CP-SELFEVAL-PACK*/", "/*END-CP-SELFEVAL-PACK*/"
PAGE = Path(__file__).with_name("selfeval_page.html")
PW_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"       # 去掉易混的 I L O 0 1
OPEN = ("issued", "sent")
UNANSWERED_RATIO = 0.5                                   # 未答超过一半要人确认
MISMATCH_RATIO, MISMATCH_MIN = 0.5, 3                    # 与资料、现场证据不一致超过一半（至少 3 题可比）要人确认
XLSX_MAX, XLSX_UNZIPPED_MAX = 2 * 1024 * 1024, 20 * 1024 * 1024
MAX_DUE_DAYS = 90
STAGE_TEXT = {1: "明天到期，提醒客户交回", 2: "今天到期，提醒客户交回", 3: "逾期未交回，已升级项目经理"}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass
class Answer:
    qid: str
    value: str = ""
    note: str = ""


# ---------- 加密 ----------

def password() -> str:
    raw = "".join(secrets.choice(PW_ALPHABET) for _ in range(20))
    return "-".join(raw[i:i + 5] for i in range(0, 20, 5))


def encrypt(body: dict, pw: str, kind: str) -> dict:
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    key = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, ITERATIONS)
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    cipher.update(f"cp-field-package:1:{kind}".encode())
    ct, tag = cipher.encrypt_and_digest(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return {"format": "cp-field-package", "version": 1, "kind": kind,
            "kdf": {"name": "PBKDF2", "hash": "SHA-256", "iterations": ITERATIONS, "salt": b64(salt)},
            "cipher": {"name": "AES-GCM", "iv": b64(iv)}, "data": b64(ct + tag)}


def page(envelope: dict) -> str:
    data = json.dumps(envelope, ensure_ascii=False).replace("</", "<\\/")
    html = PAGE.read_text(encoding="utf-8")
    start, end = html.index(MARK_START) + len(MARK_START), html.index(MARK_END)
    return html[:start] + data + html[end:]


# ---------- 生成 ----------

def _questions(db: Session, qids: list[str]) -> list[SurveyQuestion]:
    rows = db.scalars(select(SurveyQuestion).where(SurveyQuestion.qid.in_(qids), SurveyQuestion.kind == "closed")
                      .order_by(SurveyQuestion.qid))
    return list(rows)


def _question(q: SurveyQuestion, industry: str) -> dict:
    j = survey.question_json(q, industry)
    return {"qid": j["qid"], "text": j["text"], "options": j["options"]}


def _payload(project: Project, sp: SelfEvalPack, pack: SurveyPack, questions: list[SurveyQuestion]) -> dict:
    return {"format": "cp-selfeval", "version": 1, "project": project.code, "code": sp.code,
            "selfeval_version": sp.version, "pack": pack.code, "respondent_role": sp.respondent_role,
            "due_date": sp.due_date.isoformat(), "questions": [_question(q, project.industry) for q in questions]}


def _clean_role(role: str) -> str:
    role = role.strip()
    if not role or len(role) > 32 or cards.card_problems(role):
        raise SurveyError(422, "受访角色必填，不超过 32 字，只写角色不写姓名、联系方式")
    return role


def mail_text(sp: SelfEvalPack) -> str:
    return (f"您好：\n\n附件是本次安全咨询的客户自评问卷（{sp.code} 第 {sp.version} 版，受访角色：{sp.respondent_role}）。"
            "问卷是单个离线网页，双击用浏览器打开即可填写，不联网、不上传任何内容；打开口令另行告知。\n"
            f"请在 {sp.due_date.isoformat()} 前填写完毕，点“导出加密答卷”，把导出的答卷文件作为附件回复本邮件。"
            "中途可导出答卷，下次打开网页时导入继续填。\n"
            "网页无法使用时，可改填附件 Excel 模板（只填“回答”“备注”两列，不要改题号和第一行）。\n"
            "请不要在问卷中填写口令、密钥、IP 地址、网络拓扑、个人信息或业务数据样本。\n")


def generate(db: Session, user: User, project: Project, pack_code: str, role: str, due: date) -> dict:
    if project.tier == "strict":
        raise SurveyError(422, "严格档的客户自评在现场工具里导入，平台只收摘要卡")
    pack = db.scalar(select(SurveyPack).where(SurveyPack.project_id == project.id, SurveyPack.code == pack_code))
    if pack is None:
        raise SurveyError(404, "调研包不存在")
    role = _clean_role(role)
    today = date.today()
    if due < today or due > today + timedelta(days=MAX_DUE_DAYS):
        raise SurveyError(422, f"截止日须在今天到 {MAX_DUE_DAYS} 天之内")
    questions = _questions(db, pack.qids)
    if not questions:
        raise SurveyError(422, "调研包里没有客户能自答的封闭题")
    cards._lock(db, project)
    same = list(db.scalars(select(SelfEvalPack).where(SelfEvalPack.pack_id == pack.id,
                                                      SelfEvalPack.respondent_role == role)))
    for old in same:
        if old.status in OPEN:
            old.status = "superseded"
    n = db.scalar(select(func.count()).select_from(SelfEvalPack).where(SelfEvalPack.project_id == project.id)) or 0
    sp = SelfEvalPack(project_id=project.id, pack_id=pack.id, code=f"S-{n + 1:02d}",
                      version=max((o.version for o in same), default=0) + 1, respondent_role=role, pack_hash="",
                      qids=[q.qid for q in questions], due_date=due, status="issued", answer_ids=[], unclear_qids=[],
                      created_by=user.username)
    payload = _payload(project, sp, pack, questions)
    sp.pack_hash = survey.pack_hash(payload)
    db.add(sp)
    db.commit()
    pw = password()
    html = page(encrypt({**payload, "pack_hash": sp.pack_hash}, pw, KIND))
    audit.record(db, actor=user.username, action="survey.selfeval_pack", result="ok", project_code=project.code,
                 pack=pack.code, selfeval=sp.code, version=sp.version, questions=len(questions),
                 due_date=due.isoformat(), pack_hash=sp.pack_hash)
    return {"code": sp.code, "version": sp.version, "pack_hash": sp.pack_hash, "due_date": due, "password": pw,
            "html": html, "filename": f"{project.code}-{sp.code}-v{sp.version}-客户自评.html", "mail_text": mail_text(sp)}


def list_packs(db: Session, project: Project) -> list[SelfEvalPack]:
    return list(db.scalars(select(SelfEvalPack).where(SelfEvalPack.project_id == project.id)
                           .order_by(SelfEvalPack.id)))


def get(db: Session, project: Project, code: str) -> SelfEvalPack:
    sp = db.scalar(select(SelfEvalPack).where(SelfEvalPack.project_id == project.id, SelfEvalPack.code == code))
    if sp is None:
        raise SurveyError(404, "自评包不存在")
    return sp


def mark_sent(db: Session, user: User, project: Project, sp: SelfEvalPack) -> SelfEvalPack:
    if sp.status not in OPEN:
        raise SurveyError(409, "这份自评包已导入或已作废")
    sp.status, sp.sent_at = "sent", sp.sent_at or utcnow()
    db.commit()
    audit.record(db, actor=user.username, action="survey.selfeval_sent", result="ok", project_code=project.code,
                 selfeval=sp.code, version=sp.version)
    return sp


def template_xlsx(db: Session, project: Project, sp: SelfEvalPack) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("自评")
    ws.write_row(0, 0, ["包号", sp.code, "版本", sp.version, "校验码", sp.pack_hash])
    ws.write_row(1, 0, ["题号", "题目", "选项", "回答", "备注"])
    for i, q in enumerate(_questions(db, sp.qids)):
        j = _question(q, project.industry)
        ws.write_row(2 + i, 0, [j["qid"], j["text"], "；".join(f"{o['key']}：{o['label']}" for o in j["options"])])
    ws.set_column(0, 0, 18)
    ws.set_column(1, 2, 50)
    ws.set_column(3, 4, 20)
    help_ = wb.add_worksheet("填写说明")
    for i, line in enumerate(["“回答”列填选项字母（如 B），不清楚填 X；“备注”列可写补充说明。",
                              "不要改题号和第一行（包号、版本、校验码），不要增删列。",
                              "不要填写口令、密钥、IP 地址、网络拓扑、个人信息或业务数据样本。"]):
        help_.write(i, 0, line)
    wb.close()
    return buf.getvalue()


# ---------- 导入 ----------

def _reject(db: Session, user: User, project: Project, reason: str, status: int, detail: str) -> SurveyError:
    audit.record(db, actor=user.username, action="survey.selfeval_import", result="blocked",
                 project_code=project.code, reason=reason)
    return SurveyError(status, detail)


def _mismatch(db: Session, project: Project, answers: list[Answer]) -> tuple[int, int]:
    """与资料、现场证据答案可比的题数和不一致题数。"""
    high: dict[str, set[str]] = {}
    for a in db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == project.id,
                                                   SurveyAnswer.kind == "closed",
                                                   SurveyAnswer.source.in_(survey.HIGH_SOURCES),
                                                   SurveyAnswer.fact_status != "superseded")):
        high.setdefault(a.qid, set()).add(a.value)
    comparable = [a for a in answers if a.value != survey.UNCLEAR and a.qid in high]
    return len(comparable), sum(a.value not in high[a.qid] for a in comparable)


def import_answers(db: Session, user: User, project: Project, sp: SelfEvalPack, version: int, given_hash: str,
                   answers: list[Answer], extras: list[dict], confirm: bool = False, via: str = "page") -> dict:
    """extras：表外内容（Excel 多出的题、认不出的回答等），和备注一起过 guard、列出给顾问，不入池。"""
    if project.tier == "strict":
        raise SurveyError(422, "严格档的客户自评在现场工具里导入，平台只收摘要卡")
    if sp.status == "imported":
        raise SurveyError(409, "这份自评包已导入；要重导先撤销本次导入")
    if sp.status == "superseded":
        raise SurveyError(409, "这份自评包已有新版本，旧版本答卷不能导入")
    if version != sp.version or given_hash != sp.pack_hash:
        raise _reject(db, user, project, "hash_mismatch", 422, "包号、版本或校验码不符：不是本平台下发的这份自评包")
    seen: set[str] = set()
    for a in answers:
        if a.qid not in sp.qids:
            raise _reject(db, user, project, "unknown_question", 422, f"{a.qid[:32]} 不在本自评包里")
        if a.qid in seen:
            raise _reject(db, user, project, "duplicate_question", 422, f"{a.qid[:32]} 重复作答")
        seen.add(a.qid)
    notes = [*({"qid": a.qid, "text": a.note.strip()[:500]} for a in answers if a.note.strip()),
             *({**e, "text": str(e.get("text", ""))[:500]} for e in extras)]
    texts = "\n".join(e["text"] for e in notes)
    if texts and guard.check(texts).blocked:
        raise _reject(db, user, project, "confidential", 422, "备注或表外内容含机密级内容，整批未收")
    answered = [a for a in answers if a.value]
    unanswered = len(sp.qids) - len(answered)
    comparable, mismatch = _mismatch(db, project, answered)
    if not confirm and (unanswered > len(sp.qids) * UNANSWERED_RATIO
                        or (comparable >= MISMATCH_MIN and mismatch > comparable * MISMATCH_RATIO)):
        raise SurveyError(409, {"needs_confirm": True, "total": len(sp.qids), "unanswered": unanswered,
                                "comparable": comparable, "mismatch": mismatch,
                                "message": "未答过半或与资料大面积不一致，请核对后确认导入"})
    if not answered:
        raise SurveyError(422, "答卷里没有可导入的回答")
    pack = db.get(SurveyPack, sp.pack_id)

    def mark(rows: list[SurveyAnswer], unclear: list[str]) -> None:
        sp.status, sp.imported_at = "imported", utcnow()
        sp.answer_ids, sp.unclear_qids = [r.id for r in rows], list(unclear)

    cards._lock(db, project)
    db.refresh(sp)
    if sp.status not in OPEN:
        raise SurveyError(409, "这份自评包已导入或已作废")
    responses = [survey.ResponseIn(sp.respondent_role, [survey.AnswerIn(a.qid, a.value) for a in answered])]
    out = survey.import_results(db, user, project, pack.code, pack.pack_hash, responses, "self_assess", on_rows=mark)
    audit.record(db, actor=user.username, action="survey.selfeval_import", result="ok", project_code=project.code,
                 selfeval=sp.code, version=sp.version, via=via, answered=len(answered), unanswered=unanswered,
                 mismatch=mismatch, confirmed_by_user=confirm, extras=len(notes), extras_len=len(texts),
                 extras_sha256=hashlib.sha256(texts.encode("utf-8")).hexdigest() if texts else "")
    return {**out, "unanswered": unanswered, "mismatch": mismatch, "extras": notes}


def undo_import(db: Session, user: User, project: Project, sp: SelfEvalPack) -> dict:
    if sp.status != "imported":
        raise SurveyError(422, "这份自评包还没有导入")
    cards._lock(db, project)
    rows = list(db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == project.id,
                                                      SurveyAnswer.id.in_(sp.answer_ids or []))))
    if any(r.fact_status == "confirmed" for r in rows):
        raise SurveyError(409, "本次导入的答案已有人工确认，不能撤销")
    for r in rows:
        db.delete(r)
    for q in db.scalars(select(SurveyQuestion).where(SurveyQuestion.qid.in_(sp.unclear_qids or []))):
        q.unclear_count = max(0, q.unclear_count - 1)
    pack = db.get(SurveyPack, sp.pack_id)
    if pack is not None:
        pack.returned = max(0, pack.returned - 1)
    sp.status = "sent" if sp.sent_at else "issued"
    sp.imported_at, sp.answer_ids, sp.unclear_qids = None, [], []
    db.flush()
    survey._flag_conflicts(db, project)             # 撤掉的答案引起的矛盾解除，规则重新判自动确认
    survey._auto_confirm(db, user, project)
    db.commit()
    audit.record(db, actor=user.username, action="survey.selfeval_undo", result="ok", project_code=project.code,
                 selfeval=sp.code, version=sp.version, answers=len(rows))
    return {"removed": len(rows)}


# ---------- Excel ----------

def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):  # type: ignore[union-attr]
        n = n * 26 + ord(ch) - 64
    return n - 1


def _no_dtd(raw: bytes) -> None:
    """先用 expat 过一遍：出现 DOCTYPE 或实体声明即拒绝（按文件自身编码判断，UTF-16 也绕不过）。"""
    def stop(*_: object) -> None:
        raise ValueError("DTD")

    p = expat.ParserCreate()
    p.StartDoctypeDeclHandler = stop
    p.EntityDeclHandler = stop
    p.Parse(raw, True)


def read_xlsx(data: bytes) -> list[list[str]]:
    """读第一张表为字符串二维表；不是 xlsx、过大、含 DTD 的一律 SurveyError 422。"""
    bad = SurveyError(422, "不是有效的 Excel（.xlsx）文件")
    if len(data) > XLSX_MAX:
        raise SurveyError(422, "Excel 文件不超过 2MB")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        if sum(i.file_size for i in z.infolist()) > XLSX_UNZIPPED_MAX:
            raise bad
        names = z.namelist()
        sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        sheet = "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in names else (sheets or [""])[0]
        if not sheet:
            raise bad

        def parse(name: str) -> ET.Element:
            raw = z.read(name)
            _no_dtd(raw)
            return ET.fromstring(raw)  # nosec B314  _no_dtd 已拒绝 DTD 与实体声明（防 XXE、实体膨胀）

        shared = ["".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t"))
                  for si in parse("xl/sharedStrings.xml").findall("m:si", NS)] if "xl/sharedStrings.xml" in names else []
        out: list[list[str]] = []
        for row in parse(sheet).iter(f"{{{NS['m']}}}row"):
            idx = int(row.get("r", len(out) + 1)) - 1
            while len(out) <= idx:
                out.append([])
            cells = out[idx]
            for c in row.findall("m:c", NS):
                t, v = c.get("t", "n"), c.find("m:v", NS)
                if t == "s" and v is not None:
                    val = shared[int(v.text or 0)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
                else:
                    val = (v.text or "") if v is not None else ""
                    val = val[:-2] if re.fullmatch(r"-?\d+\.0", val) else val
                col = _col(c.get("r", "A"))
                while len(cells) <= col:
                    cells.append("")
                cells[col] = val.strip()
        return out
    except SurveyError:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError, ET.ParseError, expat.ExpatError, IndexError, AttributeError) as e:
        raise bad from e


def from_xlsx(db: Session, sp: SelfEvalPack, rows: list[list[str]]) -> tuple[int, str, list[Answer], list[dict]]:
    """Excel 行 → (版本, 校验码, 答案, 表外内容)。包号不对 422。"""
    meta = (rows[0] if rows else []) + [""] * 6
    if meta[1] != sp.code:
        raise SurveyError(422, f"Excel 第一行包号不是 {sp.code}，请用这份自评包的模板")
    try:
        version = int(meta[3])
    except ValueError:
        version = -1
    opts = {q.qid: [*q.options, survey.UNCLEAR_OPTION] for q in _questions(db, sp.qids)}
    answers: list[Answer] = []
    extras: list[dict] = []
    for n, r in enumerate(rows[2:], start=3):
        r = r + [""] * 5
        qid, value, note, more = r[0], r[3], r[4], [x for x in r[5:] if x]
        if not any(r):
            continue
        if qid not in opts:
            extras.append({"row": n, "qid": qid[:32], "text": "；".join(x for x in r if x)})
            continue
        key = next((o["key"] for o in opts[qid] if value.upper() == o["key"] or value == o["label"]), "")
        if value and not key:
            extras.append({"row": n, "qid": qid, "text": f"回答认不出：{value}"})
        answers.append(Answer(qid, key, "；".join(x for x in [note, *more] if x)))
    return version, meta[5], answers, extras


# ---------- 提醒与升级 ----------

def stage_for(due: date, today: date) -> int:
    if today >= due + timedelta(days=2):
        return 3
    if today >= due:
        return 2
    return 1 if today >= due - timedelta(days=1) else 0


def scan_reminders(db: Session, today: date | None = None) -> list[tuple[str, str, int]]:
    """推进未交回自评包的提醒阶段（可重复执行），返回 (项目, 包号, 新阶段)。"""
    today = today or date.today()
    made: list[tuple[str, str, int]] = []
    rows = db.execute(select(SelfEvalPack, Project.code).join(Project, Project.id == SelfEvalPack.project_id)
                      .where(SelfEvalPack.status.in_(OPEN), Project.deleted_at.is_(None))).all()
    for sp, code in rows:
        stage = stage_for(sp.due_date, today)
        if stage > sp.remind_stage:
            sp.remind_stage = stage
            made.append((code, sp.code, stage))
    if made:
        db.commit()
        for code, sc, stage in made:
            audit.record(db, actor="platform", action="survey.selfeval_remind_auto", result="ok", project_code=code,
                         selfeval=sc, stage=stage, escalated=stage == 3)
    return made
