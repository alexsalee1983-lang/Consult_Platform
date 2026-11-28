"""三期 B25 五步端到端（v3.0 验收第 12 条）：虚拟缺口 → 知识源清单（经网关，模拟模型）→ 来源确认 →
下发包（只有网址与规则）→ 采集机闸门（白名单外整包拒收并记审计）→ 下载 → 单向回传 → 平台核对批次哈希 →
自动核验入库 → 缺口关闭；全程模型调用只经网关。

可重复：scripts/b25_e2e.sh 设 B25_E2E_EVIDENCE=<目录> 跑本文件，每步结果写进 <目录>/steps.json，
再整理成 docs/验收/三期-B25端到端.md。采集机闸门用 tools/collector/collector_gate.py 本体（与部署到采集机的是同一份）。
"""
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from app import cli
from app.core.config import get_settings
from app.core.db import SessionLocal, utcnow
from app.kb import gap_pool, source_list
from app.kb import source_registry as reg
from app.kb.acquisition import Signal
from app.models import AuditLog, KbGap, KbSource, KbVersion, User
from app.services import gateway

from .conftest import create_user

_GATE = Path(__file__).resolve().parents[2] / "tools" / "collector" / "collector_gate.py"
_spec = importlib.util.spec_from_file_location("collector_gate_e2e", _GATE)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)  # type: ignore[union-attr]

HOST = "e2e.example.gov.cn"
PREFIX = f"https://{HOST}/law/"
GAP = f"{PREFIX}2026/demo-reg.html"
OFFLIST = "https://not-listed.example.com/x.html"
TITLE = "示范数据安全管理条例（虚拟）"
LAW = (f"{TITLE}\n第一条 为了规范示范数据安全管理，制定本条例。\n第二条 运营者应当建立数据分类分级制度。\n"
       "第三条 本条例自公布之日起施行。\n")
TABLE = ("| 序号 | 缺口编号 | 缺什么 | 发布机构 | 来源编号 | 网址 | 合法获取与使用判断 | 权威等级 | 优先级 | 预计量 |\n"
         "|---|---|---|---|---|---|---|---|---|---|\n"
         "| 1 | G{gid} | 示范条例原文 | 示范部 | | {url} | 可用：政府公开发布 | 法律 | 高：在办交付件 | 约 1 份 |\n"
         "| 2 | G{gid} | 示范条例解读 | 某机构 | | https://made-up.example.com/a | 可用 | 白皮书 | 低 | 待估 |\n")


class _Model:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.gid = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        text = TABLE.format(gid=self.gid, url=GAP)
        return httpx.Response(200, json={"model": "mock-model", "choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
    monkeypatch.setattr(get_settings(), "kb_sample_fail_max", 1.0)
    with SessionLocal() as db:
        db.execute(delete(KbGap))
        db.execute(delete(KbSource).where(KbSource.host == HOST))
        db.commit()
    yield
    gateway.set_transport(None)


def _user(role: str) -> User:
    name = create_user((role,))[0]
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


def _last(db, action: str) -> AuditLog:
    return db.scalar(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.desc()))


def test_b25_five_steps_end_to_end(tmp_path):
    steps: list[dict] = []
    model = _Model()
    gateway.set_transport(httpx.MockTransport(model.handler))
    admin, expert = _user("knowledge_admin"), _user("knowledge_expert")

    # ① 虚拟缺口进缺口池，白名单外 → 来源确认；知识源清单经网关交 kb-builder，程序核对丢弃编造网址
    with SessionLocal() as db:
        gap_pool.record(db, [Signal(GAP, "external", "e2e-ev-1", impact=3, authority=1),
                             Signal(GAP, "usage", "e2e-ev-2", impact=2, authority=1)])
        week = gap_pool.run_week(db, utcnow().date(), thresholds={"external": 1, "usage": 1})
        gid = db.scalar(select(KbGap.id).where(KbGap.gap_key == GAP))
        model.gid = gid
        assert week["confirm"] == 1
        rep = source_list.suggest(db, actor=admin.username)
    assert len(model.requests) == 1 and [r.url for r in rep.rows] == [GAP]
    assert rep.problems == {"fabricated_url": 1}
    steps.append({"step": "① 缺口 → 知识源清单", "ok": True, "gap_id": gid, "plan": week,
                  "rows": len(rep.rows), "dropped": rep.problems, "model_calls": len(model.requests)})

    # ② 来源确认：知识管理员批准并登记候选来源；另一位知识管理员转正式（进白名单）
    with SessionLocal() as db:
        out = gap_pool.decide_confirm(db, admin, [gid], "approve",
                                      {"name": "示范部法规库（虚拟）", "url": PREFIX, "license": "政府公开发布",
                                       "authority_kind": "法律"})
        code = out["source"]
        # 四期 B43：官方域名白名单内的法律类来源规则核对通过，登记即自动转正式，不再等另一位知识管理员
        src = reg.get(db, code)
        assert src.status == "active" and src.approved_by == reg.AUTO_ACTOR
        week2 = gap_pool.run_week(db, utcnow().date(), thresholds={"external": 1, "usage": 1})
        assert db.scalar(select(KbGap.plan).where(KbGap.id == gid)) == "direct"
    steps.append({"step": "② 来源确认", "ok": True, "source": code, "auto_activated": True,
                  "plan_after": week2})

    # ③ 下发包：只含网址与规则，程序检查无条目正文
    pkg_path = tmp_path / "dispatch.json"
    assert cli.main(["kb-acquire-dispatch", "--out", str(pkg_path)]) == 0
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    assert set(pkg) == {"format", "tasks"} and [t["url"] for t in pkg["tasks"]] == [GAP]
    assert all(set(t) <= {"url", "rule"} for t in pkg["tasks"])
    raw = pkg_path.read_bytes()
    assert "第一条".encode() not in raw and TITLE.encode() not in raw
    pkg_sha = hashlib.sha256(raw).hexdigest()
    steps.append({"step": "③ 下发包", "ok": True, "tasks": len(pkg["tasks"]), "keys": sorted(pkg),
                  "pkg_sha256": pkg_sha})

    # ④ 采集机闸门：本地白名单（不随包更新）；反例：白名单外网址整包拒收并记审计（只记主机名）
    wl, audit_log = tmp_path / "whitelist.txt", tmp_path / "collector_audit.jsonl"
    wl.write_text(f"# 采集机本地白名单（虚拟）\n{PREFIX}\n", encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format": pkg["format"], "tasks": [*pkg["tasks"], {"url": OFFLIST, "rule": pkg["tasks"][0]["rule"]}]}),
                   encoding="utf-8")
    tasks_bad = tmp_path / "tasks_bad.json"
    assert gate.main(["gate", str(bad), "--whitelist", str(wl), "--audit", str(audit_log),
                      "--out", str(tasks_bad)]) == 2
    assert not tasks_bad.exists()
    tasks = tmp_path / "tasks.json"
    assert gate.main(["gate", str(pkg_path), "--whitelist", str(wl), "--audit", str(audit_log),
                      "--out", str(tasks)]) == 0
    recs = [json.loads(x) for x in audit_log.read_text(encoding="utf-8").splitlines()]
    assert [r["result"] for r in recs] == ["refused", "accepted"]
    assert recs[0]["refused_hosts"] == ["not-listed.example.com"] and "/x.html" not in audit_log.read_text("utf-8")
    assert recs[1]["pkg_sha256"] == pkg_sha
    steps.append({"step": "④ 采集机闸门", "ok": True, "refused": recs[0]["problems"],
                  "refused_hosts": recs[0]["refused_hosts"], "accepted": recs[1]["accepted"]})

    # 下载（测试环境不联网：按任务写虚拟原件）→ 回传批次 manifest.json + 下载清单 CSV
    batch = tmp_path / "batch"
    batch.mkdir()
    data = LAW.encode()
    files = []
    for i, t in enumerate(json.loads(tasks.read_text(encoding="utf-8"))["tasks"]):
        name = f"doc{i + 1}.txt"
        (batch / name).write_bytes(data)
        files.append({"file": name, "sha256": hashlib.sha256(data).hexdigest(), "url": t["url"]})
    (batch / "manifest.json").write_text(json.dumps({"files": files}, ensure_ascii=False), encoding="utf-8")
    assert gate.main(["check-return", str(batch), "--whitelist", str(wl), "--audit", str(audit_log)]) == 0
    batch_sha = hashlib.sha256((batch / "manifest.json").read_bytes()).hexdigest()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["文件名", "名称", "文号/标准号", "现行状态", "原网址", "获取方式", "sha256"])
    for f in files:
        w.writerow([f["file"], TITLE, "示范令第1号", "现行", f["url"], "已下载", f["sha256"]])
    listing = tmp_path / "list.csv"
    listing.write_text(buf.getvalue(), encoding="utf-8")

    # 反例：回传后被改动 → 平台核对哈希不过，整批不入库，缺口不关
    tampered = tmp_path / "tampered"
    tampered.mkdir()
    (tampered / "manifest.json").write_bytes((batch / "manifest.json").read_bytes())
    (tampered / "doc1.txt").write_bytes(data + b"x")
    assert cli.main(["kb-acquire-import", str(tampered)]) == 2

    # ⑤ 单向回传 → 平台核对批次哈希 → 自动核验入库（kb-source-publish）→ 缺口关闭
    report = tmp_path / "publish.json"
    assert cli.main(["kb-acquire-import", str(batch), "--manifest", str(listing), "--as", expert.username,
                     "--report", str(report)]) == 0
    with SessionLocal() as db:
        g = db.get(KbGap, gid)
        assert g.status == "closed" and g.close_reason == "collected"
        ret = json.loads(_last(db, "kb.gap.return").detail)
        assert ret == {"files": 1, "closed": 1}
        blocked = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.return",
                                                   AuditLog.result == "blocked").order_by(AuditLog.id.desc()))
        assert json.loads(blocked.detail)["problems"] == {"sha256": 1}
        versions = list(db.scalars(select(KbVersion).where(KbVersion.ref_document == TITLE)))
    pub = json.loads(report.read_text(encoding="utf-8"))
    assert not pub["skipped"] and len(pub["docs"]) == 1 and len(versions) >= 3      # 三条切出并入库（发布或转人工）
    assert len(model.requests) == 1                     # 全程唯一一次模型调用是第①步，经网关
    steps.append({"step": "⑤ 回传 → 核对 → 入库 → 缺口关闭", "ok": True, "batch_manifest_sha256": batch_sha,
                  "tamper_rejected": True, "gap_status": "closed/collected", "publish": pub["docs"],
                  "kb_versions": len(versions), "model_calls_total": len(model.requests)})

    ev = os.environ.get("B25_E2E_EVIDENCE")
    if ev:
        d = Path(ev)
        d.mkdir(parents=True, exist_ok=True)
        (d / "steps.json").write_text(json.dumps(steps, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        for f in (pkg_path, bad, audit_log, batch / "manifest.json", listing, report):
            (d / f.name).write_bytes(f.read_bytes())
