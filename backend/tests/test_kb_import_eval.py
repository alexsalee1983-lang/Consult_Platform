"""知识条目文件、批量导入、Agent 起草（条款号只能来自原文）、评测。"""
import json
import shutil
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import drafting, evaluate, importer
from app.kb import review as rv
from app.kb.files import EntryError, format_entry, parse_entry
from app.models import AuditLog, KbItem, User
from app.services import gateway

from .conftest import create_user

REPO = Path(__file__).resolve().parents[2]
SAMPLES = REPO / "knowledge" / "samples"
QUESTIONS = REPO / "knowledge" / "eval" / "samples.yaml"


def _user(roles):
    name, *_ = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


def _entry(key="t-entry", layer="L2", body="## 做法\n\n交接班核对未闭环事件。", **extra):
    head = {"key": key, "title": "测试条目", "layer": layer, "domain": "OPS", "product": "SOC", "doc_type": "MTD",
            "stage": "设计", "capabilities": "[运营]", "source_type": "public", **extra}
    return "---\n" + "\n".join(f"{k}: {v}" for k, v in head.items()) + f"\n---\n\n{body}\n"


# ---- 条目文件 ----

def test_all_samples_parse_and_roundtrip():
    files = sorted(SAMPLES.glob("*.md"))
    assert len(files) == 10
    for f in files:
        e = parse_entry(f.read_text(encoding="utf-8"))
        assert e.key == f.stem and e.content.source_type == "public"
        again = parse_entry(format_entry(e))
        assert (again.key, again.title, again.content.body, again.meta.layer) == (e.key, e.title, e.content.body,
                                                                                 e.meta.layer)


@pytest.mark.parametrize(("text", "msg"), [
    ("没有元数据头", "元数据头"),
    (_entry(key="Bad_Key"), "key 不合法"),
    (_entry(stage="汇报"), "交付阶段"),
    (_entry(layer="L4"), "文件名和版本号"),
    (_entry(body=""), "正文为空"),
    ("---\nkey: [unclosed\n---\n\n正文", "YAML"),
])
def test_bad_entries_rejected(text, msg):
    with pytest.raises(EntryError, match=msg):
        parse_entry(text)


# ---- 批量导入 ----

@pytest.fixture()
def sample_dir(tmp_path):
    d = tmp_path / "kb"
    shutil.copytree(SAMPLES, d)
    return d


def test_import_samples_then_idempotent_and_revision(sample_dir):
    expert, expert2, admin = _user(("knowledge_expert",)), _user(("knowledge_expert",)), _user(("knowledge_admin",))
    with SessionLocal() as db:
        u = db.merge(expert)
        rs = importer.import_dir(db, u, sample_dir)
        assert [r.action for r in rs] == ["created"] * 10, [(r.file, r.message) for r in rs]
        rs = importer.import_dir(db, u, sample_dir)
        assert {r.action for r in rs} == {"skipped"}                        # 草稿还没处理，跳过
        # 把一条走完审批发布
        item = db.scalar(select(KbItem).where(KbItem.import_key == "soc-night-duty"))
        v = item.versions[-1]
        rv.submit(db, u, v)
        rv.review(db, db.merge(expert2), v, "content", "approve")
        rv.review(db, db.merge(admin), v, "compliance", "approve")
    (sample_dir / "soc-alert-grading.md").unlink()                          # 只看这一条
    for f in sample_dir.glob("*.md"):
        if f.stem != "soc-night-duty":
            f.unlink()
    with SessionLocal() as db:
        u = db.merge(expert)
        assert importer.import_dir(db, u, sample_dir)[0].action == "unchanged"
        p = sample_dir / "soc-night-duty.md"
        p.write_text(p.read_text(encoding="utf-8") + "\n夜间升级须留通话记录。\n", encoding="utf-8")
        r = importer.import_dir(db, u, sample_dir)[0]
        assert r.action == "revised"
        assert db.scalar(select(KbItem).where(KbItem.import_key == "soc-night-duty")).versions[-1].version == 2


def test_import_rules(tmp_path):
    expert = _user(("knowledge_expert",))
    (tmp_path / "a.md").write_text(_entry(key="t-proj", source_detail="x", **{"source_type": "project"})
                                   .replace("source_type: public\n", ""), encoding="utf-8")
    (tmp_path / "b.md").write_text(_entry(key="t-dup"), encoding="utf-8")
    (tmp_path / "c.md").write_text(_entry(key="t-dup"), encoding="utf-8")
    (tmp_path / "d.md").write_text(_entry(key="t-secret", body="## 配置\n\n管理口令 Abc@12345"), encoding="utf-8")
    (tmp_path / "_rejected").mkdir()
    (tmp_path / "_rejected" / "x.md").write_text("不是条目", encoding="utf-8")
    (tmp_path / "e.new.md").write_text("不是条目", encoding="utf-8")
    with SessionLocal() as db:
        rs = {r.file: r for r in importer.import_dir(db, db.merge(expert), tmp_path)}
    assert set(rs) == {"a.md", "b.md", "c.md", "d.md"}                      # _rejected/ 和 *.new.md 跳过
    assert rs["a.md"].action == "error" and "公开资料" in rs["a.md"].message
    assert rs["b.md"].action == "created" and rs["c.md"].action == "error" and "重复" in rs["c.md"].message
    assert rs["d.md"].action == "error" and "机密级" in rs["d.md"].message


def test_cli_import_requires_knowledge_user(sample_dir, capsys):
    consultant = _user(("consultant",))
    assert cli_main(["kb-import", str(sample_dir), "--as", consultant.username]) == 1
    expert = _user(("knowledge_expert",))
    one = sample_dir / "only"
    one.mkdir()
    shutil.copy(sample_dir / "soc-metrics.md", one / "soc-metrics-cli.md")
    text = (one / "soc-metrics-cli.md").read_text(encoding="utf-8").replace("key: soc-metrics", "key: soc-metrics-cli")
    (one / "soc-metrics-cli.md").write_text(text, encoding="utf-8")
    assert cli_main(["kb-import", str(one), "--as", expert.username, "--submit"]) == 0
    assert "新建 1" in capsys.readouterr().out
    with SessionLocal() as db:
        assert db.scalar(select(KbItem).where(KbItem.import_key == "soc-metrics-cli")).versions[-1].status == "in_review"


# ---- Agent 起草 ----

SOURCE = """# 某某法（公开示例文本）

第二十一条 网络运营者应当采取监测、记录网络运行状态的技术措施，并按照规定留存网络日志不少于六个月。

第二十五条 网络运营者应当制定网络安全事件应急预案。
"""

AGENT_OUTPUT = """---
key: demo-art21
title: 日志留存要求
layer: L4
domain: CMP
product: GEN
doc_type: REF
stage: 通用
capabilities: [管理]
source_type: public
ref_document: 某某法
ref_version: '2016'
ref_status: current
---
## 第二十一条 日志留存

网络日志留存不少于六个月。
=====条目分隔=====
---
key: demo-art99
title: 编造的条款
layer: L4
domain: CMP
product: GEN
doc_type: REF
stage: 通用
capabilities: [管理]
source_type: public
ref_document: 某某法
ref_version: '2016'
ref_status: current
---
## 第九十九条 不存在的条款

这一条原文里没有。
=====条目分隔=====
---
key: demo-method
title: 日志留存方案设计
layer: L2
domain: OPS
product: SOC
doc_type: MTD
stage: 设计
capabilities: [运营]
source_type: public
---
## 做法

日志集中采集，按六个月以上留存。
=====条目分隔=====
这一段不是条目格式
"""


class _Model:
    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4.1-flash"}]})
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": self.reply}}], "usage": {}})


@pytest.fixture()
def model(monkeypatch):
    def use(reply):
        m = _Model(reply)
        monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
        gateway.set_transport(httpx.MockTransport(m.handler))
        return m
    yield use
    gateway.set_transport(None)


def test_draft_rejects_clauses_not_in_source(model, tmp_path):
    m = model(AGENT_OUTPUT)
    expert = _user(("knowledge_expert",))
    with SessionLocal() as db:
        report = drafting.draft(db, db.merge(expert), "law.md", SOURCE.encode(), source_detail="某某法全文（公开）")
    assert [e.key for e in report.accepted] == ["demo-art21", "demo-method"]
    reasons = [r for _, r in report.rejected]
    assert any("第九十九条" in r for r in reasons) and len(reasons) == 2       # 编造的条款号 + 格式不对
    assert all(e.content.source_detail == "某某法全文（公开）" for e in report.accepted)
    system = m.requests[0]["messages"][0]["content"]
    assert "知识库建设" in system and "不得凭记忆补写" in system
    written = drafting.write_report(report, tmp_path / "out")
    assert sorted(p.name for p in written) == ["demo-art21.md", "demo-method.md"]
    assert len(list((tmp_path / "out" / "_rejected").glob("*.md"))) == 2
    with SessionLocal() as db:
        a = db.scalars(select(AuditLog).where(AuditLog.action == "kb.draft_agent").order_by(AuditLog.id.desc())).first()
    assert json.loads(a.detail)["accepted"] == 2 and "网络日志" not in a.detail


def test_draft_refuses_confidential_source_and_demo_mode(tmp_path):
    expert = _user(("knowledge_expert",))
    with SessionLocal() as db:
        with pytest.raises(drafting.DraftError, match="机密级"):
            drafting.draft(db, db.merge(expert), "a.md", "核心交换机 10.1.20.1 口令 Abc@12345".encode(), source_detail="x")
        r = drafting.draft(db, db.merge(expert), "law.md", SOURCE.encode(), source_detail="x")
    assert r.accepted == [] and "演示模式" in r.note


# ---- 评测 ----

def test_eval_questions_file_is_valid():
    qs = evaluate.load_questions(QUESTIONS)
    sample_keys = {f.stem for f in SAMPLES.glob("*.md")}
    assert {k for q in qs for k in q.get("expect", [])} <= sample_keys
    assert sum(1 for q in qs if q["mode"] == "basis") >= 8 and any(not q["expect"] for q in qs)


def test_eval_runs_and_reports(sample_dir):
    expert, expert2, admin = _user(("knowledge_expert",)), _user(("knowledge_expert",)), _user(("knowledge_admin",))
    consultant = _user(("consultant",))
    with SessionLocal() as db:
        for f in sample_dir.glob("*.md"):                                     # 换成本测试专用的 key，避免和其他测试冲突
            f.write_text(f.read_text(encoding="utf-8").replace("key: ", "key: ev-", 1), encoding="utf-8")
        for r in importer.import_dir(db, db.merge(expert), sample_dir, submit=True):
            v = db.get(KbItem, db.scalar(select(KbItem.id).where(KbItem.import_key == r.key))).versions[-1]
            rv.review(db, db.merge(expert2), v, "content", "approve")
            rv.review(db, db.merge(admin), v, "compliance", "approve")
        qs = evaluate.load_questions(QUESTIONS)
        for q in qs:
            q["expect"] = [f"ev-{k}" for k in q.get("expect", [])]
        report = evaluate.run(db, db.merge(consultant), qs)
    assert report.total == len(qs) and set(report.by_mode) == {"basis", "search"}
    b001 = next(r for r in report.results if r.id == "B001")
    assert b001.correct and b001.rank is not None
    assert 0.0 <= report.accuracy <= 1.0
    text = evaluate.format_report(report)
    assert "依据查询准确率" in text and "相似度分布" in text


def test_eval_reports_rerank_scores(monkeypatch):
    """启用重排时：报告带重排分数分布和“重排前进了前 N”；被门槛挡掉的依据题以门槛 0 再查取分数。"""
    import json as _json

    import httpx

    from app.core.config import get_settings
    from app.services import gateway

    def handler(request: httpx.Request) -> httpx.Response:
        docs = _json.loads(request.content)["documents"]
        return httpx.Response(200, json={"results": [{"index": i, "relevance_score": 0.9 if "日志" in d else 0.001}
                                                     for i, d in enumerate(docs)]})

    monkeypatch.setattr(get_settings(), "rerank_base_url", "http://rerank.internal/v1")
    gateway.set_transport(httpx.MockTransport(handler))
    try:
        qs = [{"id": "R1", "mode": "basis", "question": "网络日志 留存", "expect": ["ev-csl-art21-mlps"]},
              {"id": "R2", "mode": "basis", "question": "应急预案 外汇", "expect": []},
              # 最后一题完全没有候选：报告的重排状态不能被它改成“未启用”（测试机 #34 发现）
              {"id": "R3", "mode": "search", "question": "乙丙丁戊己庚辛", "expect": []}]
        with SessionLocal() as db:
            report = evaluate.run(db, db.merge(_user(("consultant",))), qs)
    finally:
        gateway.set_transport(None)
    assert report.rerank == "ok" and report.rerank_candidates == get_settings().kb_rerank_candidates
    r1, r2, r3 = report.results
    assert r3.correct and not r3.got
    assert r1.correct and r1.expected_rerank == 0.9 and r1.expected_rank_before is not None
    assert r2.correct and r2.top_wrong_rerank == 0.001                       # 门槛挡掉了，但分数取到了
    assert report.in_candidates == 1.0 and report.rerank_scores["negatives_max"]["max"] == 0.001
    assert "重排分数分布" in evaluate.format_report(report)
