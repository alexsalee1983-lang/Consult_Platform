"""二期 v2.0 验收（docs/二期开发计划.md 第五节）：5 个核心产品（SOC、ISO、DSM、NSG、IRE）各立一个虚拟客户项目，
用模拟模型从 D1 跑到 D8，全部签署。CI 可复跑，不连真实模型。

每个产品：项目负责人按产品快照立项（勾模块，SOC、ISO、NSG 勾等保 DJBH）→ 项目经理录入虚拟客户纪要和现场观察 →
发起交付链（第 3～7 步，写 D3～D8；第 4 步模型发一张“方案取舍”卡，按推荐选项处理后继续）→ 自动推进（AUTO_FLOW 打开）
交付链写完立即评审，D6、D8 门禁通过自动提交，D3、D4、D5、D7（L2）停在起草中 → 项目经理手写 D1、D2 并评审（自动提交）→
在 D4 里故意写入通用层、产品层、模块层的禁用语，评审三层都判阻断且不能驳回，改回后通过 → 项目经理提交 L2 交付件并签署全部 8 份。

断言：配置声明的交付件都生成并签署；门禁按“通用 + 产品 + 模块”三层跑过，签署版无资质边界用语；
所有模型调用都经网关（模拟模型收到的请求数与用量记录一致）；审计不含正文、证据、卡片原文。
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal, utcnow
from app.core.permissions import CONSULTANT, PROJECT_LEAD
from app.main import app
from app.models import AuditLog, Deliverable, Job, LlmUsage, Project
from app.project import configs, deliverables, quality, runs
from app.services import gateway, jobs

from .conftest import create_user, logged_in_client
from .test_quality import REVIEW

CLIENTS = Path(__file__).resolve().parents[2] / "knowledge" / "virtual-clients"
MARK = "端到端样例正文"
CARD_TITLE = "端到端样例：整改优先级采用哪种排序？"
CARD = ('\n\n```确认卡\n[{"kind": "方案取舍", "title": "' + CARD_TITLE + '", '
        '"options": ["A 按业务影响排序", "B 按整改成本排序"], "recommendation": "A", '
        '"reason": "业务影响大的差距先补", "impact": "影响路线图阶段划分"}]\n```')
FALLBACK_OUTLINE = ["现状概述", "主要差距与整改建议"]

# (产品, 勾选模块, 虚拟客户代号前缀, 素材目录；SOC 没有素材目录，用内置纪要)
CASES = [
    ("SOC", ["DJBH", "GFYL"], "BANK", None),
    ("ISO", ["DJBH"], "GRID", "iso"),
    ("DSM", [], "BANK", "dsm"),
    ("NSG", ["DJBH"], "MFG", "nsg"),
    ("IRE", ["GFYL"], "MFG", "ire"),
]
SOC_NOTES = [
    ("interview", "SOC 负责人访谈", "SOC 现有 6 人，全部白班；夜间告警次日处理，交接记录为纸质。"),
    ("observation", "值班室巡查", "值班台交接记录为纸质，夜间无人值守，告警平台无升级规则。"),
]


def _body(title: str) -> str:
    return f"{title}：{MARK}，根据访谈纪要和现场观察整理现状、差距与整改建议要点 [I-01] [O-01]。"


class _Model:
    """模拟模型：交付链各步按快照里该步的说明认出是哪一步，按交付件大纲逐章回写；第 4 步附一张确认卡；
    质量评审回一份 0 阻断的评审意见。所有请求都记下来，用来核对“只经网关”。"""

    def __init__(self, snap: dict):
        self.requests: list[dict] = []
        self.snap = snap
        self.steps = runs.chain_of(snap)
        self.outlines = {d["code"]: list(d.get("outline") or []) or FALLBACK_OUTLINE for d in snap["deliverables"]}

    def outline(self, dcode: str) -> list[str]:
        return self.outlines[dcode]

    def sections(self, dcode: str) -> list[tuple[str, str]]:
        out = [(t, _body(t)) for t in self.outline(dcode)]
        for g in self.snap["gates"]:                       # 产品层 coverage（ISO 的 SoA 93 项）写进最后一章
            if g["kind"] == "coverage" and g.get("deliverable") == dcode:
                t, b = out[-1]
                out[-1] = (t, b + "\n逐项：" + "；".join(f"{i} 适用，现状见差距分析 [I-01]" for i in g["items"]) + "。")
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        msg = body["messages"][-1]["content"]
        if msg.startswith(quality.INSTRUCTIONS):
            text = REVIEW
        else:
            step = max((s for s in self.steps if s.instruction and msg.startswith(s.instruction)),
                       key=lambda s: len(s.instruction))
            if step.deliverable:
                text = "\n\n".join(f"## {t}\n{b}" for t, b in self.sections(step.deliverable))
            else:
                text = f"## {step.name}产出\n{_body(step.name)}"
                if step.key == "gap":
                    text += CARD
        return httpx.Response(200, json={"model": "mock-model", "choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 50}})

    def prompt(self, key: str) -> str:
        step = next(s for s in self.steps if s.key == key)
        return next(r["messages"][-1]["content"] for r in self.requests
                    if r["messages"][-1]["content"].startswith(step.instruction))


def _work() -> None:
    kinds = [runs.JOB_KIND, quality.JOB_KIND]
    with SessionLocal() as db:
        for _ in range(50):
            while jobs.run_once(db, "e2e-worker", kinds=kinds):
                pass
            # 系统时钟回拨容忍（#418）：测试机上 created_at/run_after 比循环结束晚 30 多秒，入队后时钟被往回校，
            # 0 延迟任务一时“未到期”。这里只把 attempts=0 的 0 延迟任务拨到现在再领；出错退避（attempts>0）照常报现场
            late = db.scalars(select(Job).where(Job.status == jobs.QUEUED, Job.kind.in_(kinds),
                                                Job.attempts == 0)).all()
            if not late:
                break
            for j in late:
                j.run_after = utcnow()
            db.commit()
        loop_end = utcnow()
        # 循环停下后仍有 queued 任务就报现场（#418）：attempts>0 是出错退避；attempts=0 看 run_after/created_at
        # 与 loop_end 的先后、再领一次能否领到，区分“循环结束后才入队”和“到期了却领不到”
        stuck = db.scalars(select(Job).where(Job.status == jobs.QUEUED, Job.kind.in_(kinds))).all()
        info = [(j.id, j.kind, j.attempts, j.last_error, str(j.run_after), str(j.created_at)) for j in stuck]
        if stuck:
            again = jobs.claim(db, "e2e-worker", kinds=kinds)
            raise AssertionError({"loop_end": str(loop_end), "now": str(utcnow()), "stuck": info,
                                  "reclaim": again.id if again else None})


def _notes(product_dir: str | None) -> list[tuple[str, str, str]]:
    if product_dir is None:
        return SOC_NOTES
    out = []
    for p in sorted((CLIENTS / product_dir).iterdir()):
        if p.name.startswith("notes-"):
            out.append(("interview", p.stem, p.read_text(encoding="utf-8")))
        elif p.name.startswith("obs-"):
            out.append(("observation", p.stem, p.read_text(encoding="utf-8")))
    return out


def _url(code: str, dcode: str, tail: str) -> str:
    return f"/api/projects/{code}/deliverables/{dcode}/{tail}"


def _review(c, code: str, dcode: str) -> dict:
    r = c.post(_url(code, dcode, "reviews"))
    assert r.status_code == 202, r.text
    _work()
    return c.get(_url(code, dcode, f"reviews/{r.json()['id']}")).json()


def _state(c, code: str, dcode: str) -> dict:
    r = c.get(_url(code, dcode, "signoff"))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def model_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "auto_flow", True)
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
    holder: dict = {}

    def install(snap: dict) -> _Model:
        m = _Model(snap)
        gateway.set_transport(httpx.MockTransport(m.handler))
        holder["m"] = m
        return m

    yield install
    gateway.set_transport(None)


@pytest.mark.parametrize(("product", "modules", "client", "material"), CASES, ids=[c[0] for c in CASES])
def test_product_d1_to_d8(product: str, modules: list[str], client: str, material: str | None, model_on) -> None:
    # 立项：项目负责人按产品快照立项、勾模块，项目经理是顾问
    lead = logged_in_client(app, create_user((PROJECT_LEAD,)))
    mgr_user = create_user((CONSULTANT,))
    mgr = logged_in_client(app, mgr_user)
    code = f"{client}-E{secrets.token_hex(2).upper()}"
    try:
        r = lead.post("/api/projects", json={"code": code, "tier": "standard", "product": product,
                                             "modules": modules, "manager": mgr_user[0], "industry": "其他"})
        assert r.status_code == 201, r.text
        assert r.json()["product"] == product and [m["code"] for m in r.json()["modules"]] == modules
        with SessionLocal() as db:
            snap = configs.snapshot(db, db.scalar(select(Project).where(Project.code == code)))
        declared = [d["code"] for d in snap["deliverables"]]
        assert declared == [f"D{i}" for i in range(1, 9)]
        model = model_on(snap)

        # 证据：虚拟客户纪要（I-xx）和现场观察（O-xx）
        notes = _notes(material)
        assert {k for k, _, _ in notes} == {"interview", "observation"}
        for kind, title, content in notes:
            r = mgr.post(f"/api/projects/{code}/notes", json={"kind": kind, "title": title, "content": content,
                                                              "interviewee_role": "受访者" if kind == "interview" else ""})
            assert r.status_code == 201, r.text

        # 交付链：第 4 步发卡停下，按推荐选项处理后继续到结束
        r = mgr.post(f"/api/projects/{code}/runs")
        assert r.status_code == 202, r.text
        rid = r.json()["id"]
        _work()
        run = mgr.get(f"/api/projects/{code}/runs/{rid}").json()
        assert run["status"] == "waiting" and len(run["waiting_cards"]) == 1, run
        card = mgr.get(f"/api/projects/{code}/confirmations?status=open").json()
        rec = next(x for x in card if x["id"] == run["waiting_cards"][0])
        assert rec["title"] == CARD_TITLE and rec["recommendation"] == "A"
        assert mgr.post(f"/api/confirmations/{rec['id']}/resolve", json={"choice": "A"}).status_code == 200
        _work()
        run = mgr.get(f"/api/projects/{code}/runs/{rid}").json()
        assert run["status"] == "done", run
        assert [s["step"] for s in run["steps"] if s["status"] == "done"] == [s["key"] for s in snap["chain"]]
        written = [s["output"] for s in snap["chain"] if s["output"]]
        assert sorted(written) == ["D3", "D4", "D5", "D6", "D7", "D8"]
        later = next(s["key"] for s in snap["chain"] if s["key"] not in ("facts", "gap"))
        assert "顾问已确认事项" in model.prompt(later)
        for mod in snap["modules"]:                               # 模块追加的说明进了提示
            for key, extra in (mod.get("instructions") or {}).items():
                assert extra in model.prompt(key), (mod["code"], key)

        # 自动推进：交付链写完自动评审；L3（D6、D8）门禁通过自动提交，L2（D3、D4、D5、D7）停在起草中
        for dcode in ("D3", "D4", "D5", "D6", "D7", "D8"):
            st = _state(mgr, code, dcode)
            assert st["latest_review"] and st["latest_review"]["status"] == "done", (dcode, st)
            if dcode in ("D6", "D8"):
                assert st["status"] == "review" and st["history"][-1]["actor"] == "platform", (dcode, st)
            else:
                assert st["status"] == "drafting" and st["gate"], (dcode, st)

        # D1、D2：交付链不写，项目经理按大纲手写，评审后自动提交（L3）
        for dcode in ("D1", "D2"):
            titles = model.outline(dcode)
            assert mgr.put(_url(code, dcode, "outline"), json={"titles": titles}).status_code == 200
            for i, t in enumerate(titles, 1):
                assert mgr.put(_url(code, dcode, f"sections/{i}"), json={"body": _body(t)}).status_code == 201
            rv = _review(mgr, code, dcode)
            assert rv["status"] == "done" and rv["blocking"] == 0, rv
            assert _state(mgr, code, dcode)["status"] == "review"

        # 三层门禁：D4 第 1 章写入通用层（资质边界）、产品层、模块层禁用语 → 三层各判阻断、不能驳回；改回后通过
        layer_terms = {g["source"]: g["terms"][0] for g in snap["gates"] if g["kind"] == "forbidden_terms"}
        d4_1 = model.outline("D4")[0]
        probe = _body(d4_1) + "本报告已形成测评结论。" + "".join(f"文中写了{t}。" for t in layer_terms.values())
        assert mgr.put(_url(code, "D4", "sections/1"), json={"body": probe}).status_code == 201
        rv = _review(mgr, code, "D4")
        dims = {i["dimension"] for i in rv["issues"] if i["severity"] == "阻断" and i["by"] == "rule"}
        assert "范围与资质边界" in dims, rv["issues"]
        expected = {f"产品规则 {src}/{g['id']}" for g in snap["gates"] if g["kind"] == "forbidden_terms"
                    for src in [g["source"]]}
        assert expected <= dims, (expected, dims)
        assert (product in layer_terms) == any(d.startswith(f"产品规则 {product}/") for d in dims)
        assert any(m in layer_terms for m in modules) == any(
            d.startswith(f"产品规则 {m}/") for m in modules for d in dims)
        assert not _state(mgr, code, "D4")["gate"]
        assert all(not i["waivable"] for i in rv["issues"] if i["by"] == "rule")
        assert mgr.put(_url(code, "D4", "sections/1"), json={"body": _body(d4_1) + "（已改）"}).status_code == 201
        rv = _review(mgr, code, "D4")
        assert rv["blocking"] == 0 and _state(mgr, code, "D4")["gate"], rv["issues"]

        # L2 交付件由项目经理看过后提交；全部 8 份由项目经理签署
        for dcode in ("D3", "D4", "D5", "D7"):
            r = mgr.post(_url(code, dcode, "signoff/submit"), json={"comment": "已逐章核对评审标出的问题"})
            assert r.status_code == 200 and r.json()["status"] == "review", (dcode, r.text)
        for dcode in declared:
            r = mgr.post(_url(code, dcode, "signoff/sign"), json={"comment": "同意"})
            assert r.status_code == 200 and r.json()["status"] == "signed", (dcode, r.text)

        with SessionLocal() as db:
            p = db.scalar(select(Project).where(Project.code == code))
            ds = db.scalars(select(Deliverable).where(Deliverable.project_id == p.id)).all()
            assert sorted(d.code for d in ds) == declared and all(d.status == "signed" for d in ds)
            for d in ds:
                bodies = [deliverables.get_version(s).body for s in d.sections if s.current_version]
                assert bodies and all(MARK in b for b in bodies), d.code
                assert all(quality.qualification_hits(b) == 0 for b in bodies), d.code
                gates = [g for g in snap["gates"] if g.get("deliverable") in ("", d.code)]
                versions = {str(s.seq): s.current_version for s in d.sections}
                assert quality.gate_issues(d, versions, gates) == [], d.code

            # 只经网关：模拟模型收到的请求数 = 本项目的模型用量记录；交付链 Agent 和质量评审 Agent 都调到了
            used = db.scalars(select(LlmUsage).where(LlmUsage.project_code == code)).all()
            assert len(used) == len(model.requests) > 0
            assert {s["agent"] for s in snap["chain"]} | {quality.AGENT} == {u.agent_code for u in used}

            # 审计不记原文：正文、证据、卡片标题和选项、禁用语探针都不出现
            logs = db.scalars(select(AuditLog).where(AuditLog.project_code == code)).all()
            secrets_ = [MARK, CARD_TITLE, "按业务影响排序", "本报告已形成测评结论",
                        *[content.strip()[:12] for _, _, content in notes]]
            leaked = [(a.action, s) for a in logs for s in secrets_ if s in (a.detail or "")]
            assert not leaked, leaked
    finally:
        lead.__exit__(None, None, None)
        mgr.__exit__(None, None, None)
