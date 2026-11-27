"""三期 B25①：知识源清单——来源确认队列的缺口经网关交 kb-builder（模式 F）列清单，程序核对不编造网址。"""
import json

import httpx
import pytest
from sqlalchemy import delete, select

from app import cli
from app.core.config import get_settings
from app.core.db import SessionLocal, utcnow
from app.kb import gap_pool, source_list
from app.kb.acquisition import Signal
from app.models import AuditLog, KbGap, KbSource
from app.services import gateway

GAP = "https://sl.example.gov.cn/law/2026/a.html"
REG = "https://sl.example.gov.cn/law/"

TABLE = """知识源清单

| 序号 | 缺口编号 | 缺什么 | 发布机构 | 来源编号 | 网址 | 合法获取与使用判断 | 权威等级 | 优先级 | 预计量 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | G{gid} | 示范条例 | 示范部 | {code} | {url} | 可用：政府公开发布 | 法律 | 高：在办交付件 | 约 1 份 |
| 2 | G{gid} | 示范条例解读 | 示范部 | | https://made-up.example.com/x | 可用 | 白皮书 | 低 | 待估 |
| 3 | G{gid} | 示范标准 | 示范标委会 | | 待查（需登记来源） | 可用 | 国家标准 | 中 | 待估 |
| 4 | G99999 | 别的缺口 | 示范部 | {code} | {url} | 可用 | 法律 | 低 | 待估 |
| 5 | G{gid} | 示范标准全文 | 示范标委会 | | {url} | 待法务 | 国家标准 | 中 | 待估 |

## 待法务
| 来源 | 原因 |
|---|---|
| 示范标准全文 | 标准全文能否入库无定论 |
"""


class _Model:
    def __init__(self, text: str):
        self.text, self.requests = text, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "mock-model", "choices": [{"message": {"content": self.text}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_base_url", "http://model.internal/v1")
    monkeypatch.setattr(get_settings(), "llm_max_retries", 0)
    with SessionLocal() as db:
        db.execute(delete(KbGap))
        db.execute(delete(KbSource).where(KbSource.url.like("https://sl.example.gov.cn/%")))
        db.commit()
    yield
    gateway.set_transport(None)


def _confirm_gap(db) -> KbGap:
    gap_pool.record(db, [Signal(GAP, "external", "ev-sl-1", impact=3, authority=1),
                         Signal(GAP, "external", "ev-sl-2", impact=3, authority=1)])
    gap_pool.run_week(db, utcnow().date(), thresholds={"external": 1})
    row = db.scalar(select(KbGap).where(KbGap.gap_key == GAP))
    assert row.plan == "confirm"
    return row


def _registry(db) -> str:
    now = utcnow()
    db.add(KbSource(code="SR-L0001", name="示范部法规库", url=REG, host="sl.example.gov.cn", license="公开发布",
                    authority_kind="法律", authority_level=1, status="candidate", created_by="t",
                    created_at=now, updated_at=now))
    db.commit()
    return "SR-L0001"


def test_suggest_keeps_only_checked_rows_and_goes_through_gateway():
    with SessionLocal() as db:
        gid = _confirm_gap(db).id
        code = _registry(db)
    m = _Model(TABLE.format(gid=gid, code=code, url=GAP))
    gateway.set_transport(httpx.MockTransport(m.handler))
    with SessionLocal() as db:
        rep = source_list.suggest(db, actor="kb-admin-t")
    assert len(m.requests) == 1                                   # 只经网关调一次
    prompt = m.requests[0]["messages"][-1]["content"]
    assert f"G{gid}" in prompt and GAP in prompt and REG in prompt
    assert [(r.gap_id, r.url) for r in rep.rows] == [(gid, GAP)]
    assert rep.rows[0].source_code == code and rep.rows[0].authority_kind == "法律"
    assert [r.title for r in rep.needs_registration] == ["示范标准"]
    assert rep.problems == {"fabricated_url": 1, "unknown_gap": 1, "license": 1}
    with SessionLocal() as db:
        a = db.scalar(select(AuditLog).where(AuditLog.action == "kb.gap.source_list").order_by(AuditLog.id.desc()))
        d = json.loads(a.detail)
        assert a.result == "allowed" and d["rows"] == 1 and d["problems"]["fabricated_url"] == 1
        assert "made-up" not in a.detail and "示范" not in a.detail   # 审计不记清单原文


def test_suggest_without_confirm_gaps_does_not_call_model():
    m = _Model("不应调用")
    gateway.set_transport(httpx.MockTransport(m.handler))
    with SessionLocal() as db:
        rep = source_list.suggest(db, actor="kb-admin-t")
    assert rep.rows == [] and m.requests == []


def test_cli_writes_report(tmp_path):
    with SessionLocal() as db:
        gid = _confirm_gap(db).id
        code = _registry(db)
    gateway.set_transport(httpx.MockTransport(_Model(TABLE.format(gid=gid, code=code, url=GAP)).handler))
    out = tmp_path / "sources.json"
    assert cli.main(["kb-acquire-sources", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["rows"][0]["url"] == GAP and data["problems"]["fabricated_url"] == 1
