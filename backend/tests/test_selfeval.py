"""四期 E3②：客户自评包（app/project/selfeval.py，迁移 0058）。

从调研包按受访角色生成：加密离线网页（PBKDF2-SHA256 + AES-256-GCM，与现场工具同一格式）+ Excel 模板；
随机口令只在生成时返回一次，不落库、不进审计。导入（浏览器本机解密后的答卷或 Excel）进答案池，渠道“客户自评”，
走 E3① 可信度与自动确认规则；校验包号与版本、完整性、机密级、重复导入；未答过多时要人确认；可撤销本次导入。
到期前一天、当天提醒，逾期 2 天升级项目经理，由 worker 扫描推进，进“我的任务”。"""
import base64
import io
import json
import secrets
import zipfile
from datetime import date, timedelta

import pytest
import xlsxwriter
from Crypto.Cipher import AES
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog, Project
from app.models.survey import SelfEvalPack, SurveyAnswer
from app.project import selfeval, survey

from .conftest import create_user, logged_in_client

TAG = "".join(secrets.choice("ABCDEFGHJKLMNP") for _ in range(4))
Q1, Q2, QO = f"SOC-S{TAG}-001", f"SOC-S{TAG}-002", f"SOC-S{TAG}-003"
OPTS = [{"key": "A", "text": "没有分级表"}, {"key": "B", "text": "有分级表且工单计时"}, {"key": "X", "text": "不清楚"}]
BANK = [
    {"id": Q1, "product": "SOC", "control_ids": ["SOC.PRC.01"], "kind": "closed", "stem": "告警是否分级？", "options": OPTS},
    {"id": Q2, "product": "SOC", "control_ids": ["SOC.ORG.02"], "kind": "closed", "stem": "交接是否有记录？", "options": OPTS},
    {"id": QO, "product": "SOC", "control_ids": ["SOC.ORG.03"], "kind": "open", "stem": "值班交接的主要痛点？"},
]
ROLE = "安全运营负责人"


@pytest.fixture(scope="module", autouse=True)
def _bank():
    with SessionLocal() as db:
        survey.import_bank(db, BANK)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(get_settings(), "survey_spot_rate", 0.0)
    monkeypatch.setattr(selfeval, "ITERATIONS", 1000)          # 测试里降迭代次数，格式不变


def _project(tier: str = "standard") -> str:
    code = f"SE-{secrets.token_hex(3).upper()}"
    with SessionLocal() as db:
        db.add(Project(code=code, service="SOC 运营体系设计", industry="银行", tier=tier))
        db.commit()
    return code


def _setup(tier: str = "standard", due: date | None = None):
    code = _project(tier)
    user = create_user(("consultant",), {code: "manager"})
    c = logged_in_client(app, user)
    pack = c.post(f"/api/projects/{code}/survey/packs").json()
    due = due or date.today() + timedelta(days=7)
    r = c.post(f"/api/projects/{code}/survey/selfeval",
               json={"pack": pack["code"], "respondent_role": ROLE, "due_date": due.isoformat()})
    return code, c, pack, r


def _decrypt(env: dict, password: str) -> dict:
    import hashlib

    assert env["format"] == "cp-field-package" and env["version"] == 1
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(env["kdf"]["salt"]),
                              env["kdf"]["iterations"])
    raw = base64.b64decode(env["data"])
    cipher = AES.new(key, AES.MODE_GCM, nonce=base64.b64decode(env["cipher"]["iv"]))
    cipher.update(f"cp-field-package:1:{env['kind']}".encode())
    return json.loads(cipher.decrypt_and_verify(raw[:-16], raw[-16:]))


def _envelope(html: str) -> dict:
    start = html.index(selfeval.MARK_START) + len(selfeval.MARK_START)
    return json.loads(html[start:html.index(selfeval.MARK_END)])


def _import(c, code, s, body: dict, status: int = 201) -> dict:
    r = c.post(f"/api/projects/{code}/survey/selfeval/{s['code']}/import", json=body)
    assert r.status_code == status, r.text
    return r.json()


def _body(s: dict, answers: list[dict], **kw) -> dict:
    return {"version": s["version"], "pack_hash": s["pack_hash"], "answers": answers, **kw}


def _full(s: dict, **over: dict) -> list[dict]:
    """答全本包所有题（题库是全局的，别的测试的题也在包里）；over 按题号覆盖。"""
    qs = _decrypt(_envelope(s["html"]), s["password"])["questions"]
    return [over.get(q["qid"], {"qid": q["qid"], "value": q["options"][0]["key"]}) for q in qs]


def test_generate_encrypted_page_password_once_not_stored():
    code, c, pack, r = _setup()
    assert r.status_code == 201, r.text
    s = r.json()
    pw = s["password"]
    assert len(pw) >= 16 and s["code"] == "S-01" and s["version"] == 1
    html = s["html"]
    assert pw not in html and "告警是否分级" not in html            # 题面在密文里，口令不在页面里
    assert "connect-src 'none'" in html                              # 离线网页禁止联网
    env = _envelope(html)
    assert env["kind"] == "selfeval"
    body = _decrypt(env, pw)
    assert body["code"] == "S-01" and body["respondent_role"] == ROLE and body["pack_hash"] == s["pack_hash"]
    qids = {q["qid"] for q in body["questions"]}
    assert qids >= {Q1, Q2} and QO not in qids                       # 只含封闭事实题
    assert all("level" not in o for q in body["questions"] for o in q["options"])   # 不含评分锚定
    assert "口令" in s["mail_text"] and pw not in s["mail_text"]
    with SessionLocal() as db:
        row = db.scalar(select(SelfEvalPack).join(Project).where(Project.code == code))
        assert row.status == "issued" and pw not in json.dumps(
            {k: str(v) for k, v in row.__dict__.items() if not k.startswith("_")}, ensure_ascii=False)
        logs = db.scalars(select(AuditLog).where(AuditLog.action.like("survey.selfeval%"))).all()
        assert logs and all(pw not in a.detail for a in logs)
    lst = c.get(f"/api/projects/{code}/survey/selfeval").json()
    assert lst[0]["code"] == "S-01" and "password" not in lst[0]


def test_strict_tier_and_bad_input_rejected():
    code, c, pack, r = _setup("strict")
    assert r.status_code == 422 and "现场工具" in r.text
    code, c, pack, r = _setup()
    url = f"/api/projects/{code}/survey/selfeval"
    assert c.post(url, json={"pack": "P-99", "respondent_role": ROLE,
                             "due_date": date.today().isoformat()}).status_code == 404
    assert c.post(url, json={"pack": pack["code"], "respondent_role": ROLE,
                             "due_date": (date.today() - timedelta(days=1)).isoformat()}).status_code == 422
    assert c.post(url, json={"pack": pack["code"], "respondent_role": "张三 13812345678",
                             "due_date": date.today().isoformat()}).status_code == 422


def test_import_goes_to_pool_as_self_assess_and_follows_rules():
    code, c, pack, r = _setup()
    s = r.json()
    assert c.post(f"/api/projects/{code}/survey/selfeval/S-01/sent").status_code == 200
    # 资料预填 Q1=B（高可信，自动确认）
    c.post(f"/api/projects/{code}/survey/packs/{pack['code']}/results",
           json={"pack_hash": pack["pack_hash"], "channel": "prefill",
                 "responses": [{"respondent_role": "客户资料", "answers": [{"qid": Q1, "value": "B"}]}]})
    answers = _full(s, **{Q1: {"qid": Q1, "value": "B"}, Q2: {"qid": Q2, "value": "A", "note": "交接靠口头"}})
    out = _import(c, code, s, _body(s, answers))
    assert out["facts"] == len(answers) and out["open"] == 0
    assert [e["qid"] for e in out["extras"]] == [Q2]                 # 备注列出由顾问决定，不进答案池
    pool = {q["qid"]: q for q in c.get(f"/api/projects/{code}/survey/pool").json()}
    mine = [a for a in pool[Q2]["answers"] if a["channel"] == "self_assess"]
    assert mine and mine[0]["source"] == "self" and mine[0]["respondent_role"] == ROLE
    assert pool[Q2]["status"] == "pending"                           # 只有客户自评 → 留给人
    assert pool[Q1]["status"] == "auto"                              # 与资料一致 → 资料那条仍自动确认
    lst = c.get(f"/api/projects/{code}/survey/selfeval").json()
    assert lst[0]["status"] == "imported"
    # 重复导入
    _import(c, code, s, _body(s, [{"qid": Q1, "value": "B"}]), 409)
    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.action == "survey.selfeval_import")).all()
        assert logs and all("交接靠口头" not in a.detail for a in logs)


def test_import_checks_hash_version_guard_and_unknown_question():
    code, c, pack, r = _setup()
    s = r.json()
    _import(c, code, s, {**_body(s, [{"qid": Q1, "value": "B"}]), "pack_hash": "0" * 64}, 422)
    _import(c, code, s, _body(s, [{"qid": "SOC-ZZZ-999", "value": "B"}, {"qid": Q2, "value": "A"}]), 422)
    _import(c, code, s, _body(s, _full(s, **{Q2: {"qid": Q2, "value": "A",
                                                    "note": "堡垒机管理员口令为 Passw0rd!2026"}})), 422)
    with SessionLocal() as db:
        pid = db.scalar(select(Project.id).where(Project.code == code))
        assert not db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == pid)).all()
    # 同一角色重新生成 → 新版本，旧版本不能再导入
    r2 = c.post(f"/api/projects/{code}/survey/selfeval",
                json={"pack": pack["code"], "respondent_role": ROLE, "due_date": date.today().isoformat()})
    assert r2.json()["version"] == 2
    _import(c, code, s, _body(s, _full(s)), 409)


def test_too_many_unanswered_needs_confirm_then_undo():
    code, c, pack, r = _setup()
    s = r.json()
    out = _import(c, code, s, _body(s, [{"qid": Q1, "value": ""}]), 409)      # 全部未答：超过一半
    assert out["detail"]["needs_confirm"] is True and out["detail"]["unanswered"] >= 2
    _import(c, code, s, _body(s, [{"qid": Q1, "value": "B"}], confirm=True))
    with SessionLocal() as db:
        pid = db.scalar(select(Project.id).where(Project.code == code))
        assert len(db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == pid)).all()) == 1
    u = c.delete(f"/api/projects/{code}/survey/selfeval/S-01/import")
    assert u.status_code == 200, u.text
    with SessionLocal() as db:
        assert not db.scalars(select(SurveyAnswer).where(SurveyAnswer.project_id == pid)).all()
    assert c.get(f"/api/projects/{code}/survey/selfeval").json()[0]["status"] != "imported"
    _import(c, code, s, _body(s, _full(s)))                         # 撤销后可重新导入


def _xlsx(meta: list, rows: list[list]) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("自评")
    ws.write_row(0, 0, meta)
    ws.write_row(1, 0, ["题号", "题目", "选项", "回答", "备注"])
    for i, row in enumerate(rows):
        ws.write_row(2 + i, 0, row)
    wb.close()
    return buf.getvalue()


def test_excel_template_and_import_lists_extras():
    code, c, pack, r = _setup()
    s = r.json()
    t = c.get(f"/api/projects/{code}/survey/selfeval/S-01/template.xlsx")
    assert t.status_code == 200
    with zipfile.ZipFile(io.BytesIO(t.content)) as z:
        text = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.endswith(".xml"))
    assert Q1 in text and s["pack_hash"] in text
    data = _xlsx(["包号", "S-01", "版本", 1, "校验码", s["pack_hash"]],
                 [[Q1, "", "", "b", ""], [Q2, "", "", "有分级表且工单计时", "去年才建"], [QO, "", "", "交接靠口头", ""],
                  ["SOC-ZZZ-999", "客户自己加的题", "", "是", ""]])
    r = c.post(f"/api/projects/{code}/survey/selfeval/S-01/import-xlsx",
               files={"file": ("a.xlsx", data, "application/octet-stream")}, data={"confirm": "true"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["facts"] == 2 and out["open"] == 0
    assert {e["qid"] for e in out["extras"]} == {Q2, QO, "SOC-ZZZ-999"}   # 备注和表外题列出，由顾问决定
    with SessionLocal() as db:
        got = {a.qid: a.value for a in db.scalars(select(SurveyAnswer).join(Project).where(Project.code == code))}
    assert got == {Q1: "B", Q2: "B"} and all(a for a in got)          # 小写键、选项原文都认
    # 包号不对
    bad = _xlsx(["包号", "S-09", "版本", 1, "校验码", s["pack_hash"]], [[Q1, "", "", "B", ""]])
    r = c.post(f"/api/projects/{code}/survey/selfeval/S-01/import-xlsx",
               files={"file": ("a.xlsx", bad, "application/octet-stream")})
    assert r.status_code == 422
    r = c.post(f"/api/projects/{code}/survey/selfeval/S-01/import-xlsx",
               files={"file": ("a.xlsx", b"not a zip", "application/octet-stream")})
    assert r.status_code == 422


def test_reminders_and_escalation_go_to_my_tasks():
    due = date.today() + timedelta(days=3)
    code, c, pack, r = _setup(due=due)
    assert r.status_code == 201
    member = create_user(("consultant",), {code: "member"})
    with SessionLocal() as db:
        pid = db.scalar(select(Project.id).where(Project.code == code))
        db.add(SelfEvalPack(project_id=pid, pack_id=db.scalar(select(SelfEvalPack.pack_id)
                                                              .where(SelfEvalPack.project_id == pid)),
                            code="S-02", version=1, respondent_role="值班组长", pack_hash="0" * 64, qids=[Q1],
                            due_date=due, status="sent", created_by=member[0]))
        db.commit()
    c.post(f"/api/projects/{code}/survey/selfeval/S-01/sent")

    def stages():
        with SessionLocal() as db:
            return {s.code: s.remind_stage for s in db.scalars(select(SelfEvalPack).where(SelfEvalPack.project_id == pid))}

    def tasks(cl):
        return [t for t in cl.get("/api/my-tasks").json()["tasks"]
                if t["kind"] == "selfeval" and t["project_code"] == code]

    def scan(day):
        with SessionLocal() as db:
            return [m for m in selfeval.scan_reminders(db, day) if m[0] == code]

    assert scan(due - timedelta(days=3)) == []
    assert stages() == {"S-01": 0, "S-02": 0}
    assert len(scan(due - timedelta(days=1))) == 2
    assert scan(due - timedelta(days=1)) == []                                   # 可重复执行，不重复提醒
    assert stages() == {"S-01": 1, "S-02": 1}
    mt = tasks(c)
    assert [t["title"] for t in mt] and all("S-02" not in t["title"] for t in mt)  # 提醒给发包人
    with logged_in_client(app, member) as mc:
        assert any("S-02" in t["title"] for t in tasks(mc))
    scan(due)
    scan(due + timedelta(days=2))
    assert stages() == {"S-01": 3, "S-02": 3}
    assert any("S-02" in t["title"] and "升级" in t["title"] for t in tasks(c))     # 逾期升级项目经理
    with SessionLocal() as db:
        n = len(db.scalars(select(AuditLog).where(AuditLog.action == "survey.selfeval_remind_auto",
                                                 AuditLog.project_code == code)).all())
    assert n == 6
    s = r.json()
    _import(c, code, s, _body(s, _full(s)))
    assert all("S-01" not in t["title"] for t in tasks(c))                      # 交回导入后不再提醒


def test_project_deletion_removes_selfeval():
    code, c, pack, r = _setup()
    with SessionLocal() as db:
        pid = db.scalar(select(Project.id).where(Project.code == code))
        survey.delete_for_project(db, pid)
        db.commit()
        assert not db.scalars(select(SelfEvalPack).where(SelfEvalPack.project_id == pid)).all()
