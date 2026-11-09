"""去标识：规则与词库、脱敏审查 Agent 复查、人工确认、原件删除；报告和审计不含原文。"""
import json

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import deid
from app.kb.deid_agent import review
from app.main import app
from app.models import AuditLog, KbDeidReport, KbUpload
from app.services import gateway, jobs, storage

from .conftest import create_user, logged_in_client, on_s3

SAMPLE = [
    ("江南农商行 SOC 建设复盘", "我们为江南农商行建设 SOC。江南农商行的张总要求夜间一级事件 15 分钟内响应，李处负责值班。"),
    ("联系与编号", "联系人邮箱 ops@jnrcb.com.cn，电话 13812345678；依据银发〔2023〕12号，合同编号：HT-2023-0098，"
                "预算 1200万元，门户 https://soc.jnrcb.com.cn/login。"),
    ("通用做法", "城市商业银行普遍没有夜班，某城商行和中国人民银行的要求见附录。"),
]
ORIGINALS = ["江南农商行", "张总", "李处", "ops@jnrcb.com.cn", "13812345678", "银发〔2023〕12号", "HT-2023-0098",
             "1200万元", "soc.jnrcb.com.cn"]


def _all(result: deid.DeidResult) -> str:
    return "\n".join(h + "\n" + b for h, b in result.sections) + result.filename


# ---- 规则 ----

def test_rules_replace_direct_identifiers_consistently():
    r = deid.deidentify(SAMPLE, "江南农商行SOC复盘.docx")
    text = _all(r)
    for o in ORIGINALS:
        assert o not in text, o
    assert text.count("【客户1】") == 4                       # 同一客户同一占位符（含标题和文件名）
    assert "【人员1】" in text and "【人员2】" in text
    assert r.filename == "【客户1】SOC复盘.docx"
    # 泛称和监管机构不替换
    assert "城市商业银行" in text and "某城商行" in text and "中国人民银行" in text


def test_common_words_not_taken_as_names():
    text = _all(deid.deidentify([("", "我们主张总体设计先行；安全公司和集团公司的做法不同；处理流程见附录。")], "a.md"))
    assert text == "\n我们主张总体设计先行；安全公司和集团公司的做法不同；处理流程见附录。a.md"


def test_report_has_no_original_text():
    r = deid.deidentify(SAMPLE, "江南农商行SOC复盘.docx")
    report = json.dumps([x.to_json() for x in r.replacements], ensure_ascii=False)
    for o in ORIGINALS:
        assert o not in report
    cats = {x.category for x in r.replacements}
    assert {"客户名称", "人名", "联系方式", "网络标识", "编号", "金额"} <= cats
    client = next(x for x in r.replacements if x.category == "客户名称")
    assert client.count == 4 and client.to_json()["sections"] == [-1, 0]   # -1 表示文件名


def test_extra_terms_longest_first_and_not_in_report():
    sections = [("", "华东某某数据的核心账务系统 CBS 和华东某某数据信息科技部都要纳入。")]
    terms = [deid.Term("华东某某数据", "客户名称"), deid.Term("华东某某数据信息科技部", "客户名称"),
             deid.Term("核心账务系统 CBS", "系统名称")]
    r = deid.deidentify(sections, "a.md", terms)
    text = _all(r)
    assert "华东某某数据" not in text and "CBS" not in text and "【系统1】" in text
    assert deid.residual_terms_after(r, [t.term for t in terms]) == []
    assert "华东" not in json.dumps([x.to_json() for x in r.replacements], ensure_ascii=False)


def test_unknown_category_rejected():
    with pytest.raises(ValueError, match="类别"):
        deid.deidentify([("", "x")], "a.md", [deid.Term("x", "客户")])


# ---- 脱敏审查 Agent ----

class _Model:
    def __init__(self, reply: str):
        self.reply, self.requests = reply, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4.1-flash"}]})
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": self.reply}}], "usage": {}})


@pytest.fixture()
def model(monkeypatch):
    def use(reply: str) -> _Model:
        m = _Model(reply)
        monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
        gateway.set_transport(httpx.MockTransport(m.handler))
        return m
    yield use
    gateway.set_transport(None)


def test_agent_skipped_without_model():
    with SessionLocal() as db:
        r = review(db, "某段已替换的文本", "tester", 1)
    assert r.status == "skipped" and r.manual_items


def test_agent_returns_residual_terms_and_strictest_conclusion(model):
    m = model(json.dumps({"conclusion": "有条件通过", "risk_level": "中",
                          "residual_terms": [{"term": "江南数据中心", "category": "客户名称"}],
                          "combination_risks": [{"description": "江南数据中心 + 华东地区可锁定客户", "handling": "泛化地区"}],
                          "manual_items": ["确认江南数据中心是否为客户自有"]}, ensure_ascii=False))
    with SessionLocal() as db:
        r = review(db, "文本", "tester", 1)
    assert r.status == "ok" and r.conclusion == "有条件通过" and r.risk_level == "中"
    assert [t.term for t in r.residual_terms] == ["江南数据中心"]
    system = m.requests[0]["messages"][0]["content"]
    assert "脱敏审查" in system and "residual_terms" in system         # 用 sanitizer.md 作系统提示


def test_agent_unparsable_reply_is_failed(model):
    model("好的，我审查完了，没有问题。")
    with SessionLocal() as db:
        r = review(db, "文本", "tester", 1)
    assert r.status == "failed" and "无法解析" in r.note and r.manual_items


# ---- 流程（接真实对象存储）----

def _client(roles):
    return logged_in_client(app, create_user(roles))


def _upload_and_process(client, text: str, name: str) -> int:
    r = client.post("/api/kb/intake", files={"file": (name, text.encode(), "text/markdown")})
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        while jobs.run_once(db, "test-worker", kinds=["kb.parse", "kb.deid"]):
            pass
    return r.json()["id"]


MD = "# 江南农商行值班复盘（{tag}）\n\n江南农商行的张总要求一级事件 15 分钟内响应。\n\n## 夜间\n\n华东某某数据负责夜间初筛。\n"


@on_s3
def test_full_deid_flow_pass_deletes_raw_and_hides_originals(model):
    model(json.dumps({"conclusion": "有条件通过", "risk_level": "低",
                      "residual_terms": [{"term": "华东某某数据", "category": "客户名称"}],
                      "combination_risks": [], "manual_items": []}, ensure_ascii=False))
    uploader, admin2, consultant = _client(("knowledge_admin",)), _client(("knowledge_admin",)), _client(("consultant",))
    try:
        uid = _upload_and_process(uploader, MD.format(tag="通过"), "江南农商行值班复盘.md")
        rep = uploader.get(f"/api/kb/intake/{uid}/deid").json()
        assert rep["status"] == "pending" and rep["agent_status"] == "ok"
        blob = json.dumps(rep, ensure_ascii=False)
        for o in ("江南农商行", "张总", "华东某某数据"):
            assert o not in blob, o                                   # 报告和预览都没有原文
        assert rep["safe_filename"].startswith("【客户1】")

        r = uploader.post(f"/api/kb/intake/{uid}/deid/rerun",
                          json={"extra_terms": [{"term": "值班复盘", "category": "其他"}]})
        assert r.status_code == 200 and "值班复盘" not in json.dumps(r.json(), ensure_ascii=False)
        assert consultant.post(f"/api/kb/intake/{uid}/deid/rerun", json={}).status_code == 404   # 看不到别人的投料

        assert uploader.post(f"/api/kb/intake/{uid}/deid/decision", json={"decision": "pass"}).status_code == 403
        r = admin2.post(f"/api/kb/intake/{uid}/deid/decision", json={"decision": "pass", "comment": "已核对"})
        assert r.status_code == 200 and r.json()["status"] == "passed"

        with SessionLocal() as db:
            up = db.get(KbUpload, uid)
            assert up.status == "deid_passed" and up.raw_deleted_at is not None
            assert "江南农商行" not in up.filename
            assert storage.stat("knowledge", up.object_key) is None            # 原件已删除
            assert storage.stat("knowledge", up.parsed_key) is None            # 未去标识的解析结果已删除
            reports = db.scalars(select(KbDeidReport).where(KbDeidReport.upload_id == uid)).all()
            assert sorted(x.status for x in reports) == ["passed", "superseded"]
            assert storage.stat("knowledge", reports[0].deid_key) is None or reports[0].status == "passed"
            details = [a.detail for a in db.scalars(select(AuditLog).where(AuditLog.action.like("kb.deid%")))]
        assert all("江南农商行" not in d and "华东某某数据" not in d and "值班复盘" not in d for d in details)
        detail = admin2.get(f"/api/kb/intake/{uid}").json()
        assert detail["preview"] and "江南农商行" not in json.dumps(detail, ensure_ascii=False)
        assert admin2.post(f"/api/kb/intake/{uid}/deid/decision", json={"decision": "pass"}).status_code == 409
    finally:
        for c in (uploader, admin2, consultant):
            c.__exit__(None, None, None)


@on_s3
def test_reject_deletes_everything_and_agent_reject_blocks_pass(model):
    model(json.dumps({"conclusion": "退回", "risk_level": "高", "residual_terms": [],
                      "combination_risks": [{"description": "事件与时间组合可锁定客户", "handling": "删除事件描述"}],
                      "manual_items": []}, ensure_ascii=False))
    uploader, admin2 = _client(("knowledge_expert",)), _client(("knowledge_admin",))
    try:
        uid = _upload_and_process(uploader, MD.format(tag="退回"), "复盘.md")
        rep = admin2.get(f"/api/kb/intake/{uid}/deid").json()
        assert rep["conclusion"] == "退回" and rep["combination_risks"]
        assert admin2.post(f"/api/kb/intake/{uid}/deid/decision", json={"decision": "pass"}).status_code == 409
        assert uploader.post(f"/api/kb/intake/{uid}/deid/decision", json={"decision": "reject"}).status_code == 403
        assert admin2.post(f"/api/kb/intake/{uid}/deid/decision", json={"decision": "reject"}).status_code == 200
        with SessionLocal() as db:
            up = db.get(KbUpload, uid)
            report = db.scalars(select(KbDeidReport).where(KbDeidReport.upload_id == uid)).one()
            assert up.status == "deid_rejected"
            for key in (up.object_key, up.parsed_key, report.deid_key):
                assert storage.stat("knowledge", key) is None
    finally:
        uploader.__exit__(None, None, None)
        admin2.__exit__(None, None, None)
