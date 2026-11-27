"""向量模型对比评测（kb-eval --embed-compare，决策记录 2026-11-18：bge-m3 与现用模型在测试机上对比）。

同一批评测题、同一批已发布现行切片，分别用现用向量模型（A）和候选模型（B）向量化，只比向量召回：
命中@1/3/5/10、MRR。都经网关 gateway.embed（每条过机密级检测），不写库、不改配置；审计只记题数和指标。
"""
import json
import secrets
import shutil
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.kb import embed_compare, importer
from app.kb import review as rv
from app.models import AuditLog, KbItem, User
from app.services import gateway

from .conftest import create_user

SAMPLES = Path(__file__).resolve().parents[2] / "knowledge" / "samples"
WORDS = ["日志", "留存", "应急", "预案", "等级保护", "个人信息", "密码", "数据"]


def _user(roles):
    name, *_ = create_user(roles)
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.username == name))


def _publish(tmp_path) -> str:
    """发布一份样例，key 加本次独有前缀（同一批样例在别的用例里可能已下线），返回前缀。"""
    pre = f"ec{secrets.token_hex(4)}-"
    d = tmp_path / "kb"
    shutil.copytree(SAMPLES, d)
    for f in d.glob("*.md"):
        f.write_text(f.read_text(encoding="utf-8").replace("key: ", f"key: {pre}", 1), encoding="utf-8")
    expert, expert2, admin = _user(("knowledge_expert",)), _user(("knowledge_expert",)), _user(("knowledge_admin",))
    with SessionLocal() as db:
        for r in importer.import_dir(db, db.merge(expert), d, submit=True):
            v = db.get(KbItem, db.scalar(select(KbItem.id).where(KbItem.import_key == r.key))).versions[-1]
            if v.status != "published":
                rv.review(db, db.merge(expert2), v, "content", "approve")
                rv.review(db, db.merge(admin), v, "compliance", "approve")
    return pre


@pytest.fixture(autouse=True)
def _retire_after():
    """样例条目用完就下线，免得挤占其他检索测试的结果（按文件名顺序本文件排在 test_kb_search 前面）。"""
    yield
    with SessionLocal() as db:
        for item in db.scalars(select(KbItem).where(KbItem.import_key.like("ec%-%"), KbItem.retired.is_(False))):
            item.retired = True
        db.commit()


def _vec(text: str, dim: int, good: bool) -> list[float]:
    """B（good）按关键词出向量，能区分；A 对所有文本给几乎相同的向量，区分不开。"""
    v = [0.0] * dim
    if good:
        for i, w in enumerate(WORDS):
            v[i] = float(text.count(w))
    v[dim - 1] = 1.0
    return v


@pytest.fixture()
def models(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(f"{request.url.host}:{body['model']}")
        good = request.url.host == "bge-m3.internal"
        dim = 1024 if good else gateway.EMBED_DIM
        return httpx.Response(200, json={"data": [{"index": i, "embedding": _vec(t, dim, good)}
                                                  for i, t in enumerate(body["input"])]})
    monkeypatch.setattr(get_settings(), "embed_base_url", "http://embed.internal/v1")
    gateway.set_transport(httpx.MockTransport(handler))
    yield seen
    gateway.set_transport(None)


def _qs(pre: str) -> list[dict]:
    return [{"id": "E1", "mode": "basis", "question": "网络日志 留存 多久", "expect": [f"{pre}csl-art21-mlps"]},
            {"id": "E2", "mode": "basis", "question": "应急 交接班 外汇", "expect": []},      # 负例不计入召回
            {"id": "E3", "mode": "basis", "question": "不存在的条目", "expect": ["ec-no-such-key"]}]   # 库里没有的不计入


def test_embed_override_params(models) -> None:
    out = gateway.embed(["网络日志"], base_url="http://bge-m3.internal/v1", model="BAAI/bge-m3", dim=1024)
    assert out.model == "BAAI/bge-m3" and len(out.vectors[0]) == 1024
    with pytest.raises(gateway.GatewayError):                     # 维度与声明的不符照样拒绝
        gateway.embed(["网络日志"], base_url="http://bge-m3.internal/v1", model="BAAI/bge-m3", dim=512)
    assert gateway.embed(["网络日志"]).model == get_settings().embed_model     # 不传照旧用配置
    assert models == ["bge-m3.internal:BAAI/bge-m3"] * 2 + [f"embed.internal:{get_settings().embed_model}"]


def test_embed_override_still_guarded(models) -> None:
    with pytest.raises(gateway.BlockedError):
        gateway.embed(["口令：Admin@12345"], base_url="http://bge-m3.internal/v1", model="BAAI/bge-m3", dim=1024)
    assert models == []                                            # 命中机密级不发出


def test_compare_recall_metrics(tmp_path, models) -> None:
    pre = _publish(tmp_path)
    with SessionLocal() as db:
        rep = embed_compare.run(db, db.merge(_user(("consultant",))), _qs(pre),
                                b_url="http://bge-m3.internal/v1", b_model="BAAI/bge-m3", b_dim=1024)
    assert rep.questions == 1 and rep.skipped == 2                 # 负例和库里没有的不计入
    assert rep.chunks > 0
    a, b = rep.models
    assert a["model"] == get_settings().embed_model and b["model"] == "BAAI/bge-m3" and b["dim"] == 1024
    assert b["hit@1"] == 1.0 and b["mrr"] == 1.0
    assert set(a) >= {"hit@1", "hit@3", "hit@5", "hit@10", "mrr", "seconds"}
    assert rep.per_question[0]["id"] == "E1" and rep.per_question[0]["rank_b"] == 1
    with SessionLocal() as db:
        log = db.scalars(select(AuditLog).where(AuditLog.action == "kb.eval_embed").order_by(AuditLog.id.desc())).first()
    dump = log.detail
    assert "日志" not in dump and "留存" not in dump               # 审计不记检索词和片段
    assert json.loads(dump)["questions"] == 1 and "BAAI/bge-m3" in dump
    assert "bge-m3.internal" not in dump                           # 不记地址


def test_cli_embed_compare(tmp_path, models, capsys) -> None:
    pre = _publish(tmp_path)
    qf = tmp_path / "q.yaml"
    qf.write_text(f"- id: E1\n  mode: basis\n  question: 网络日志 留存 多久\n  expect: [{pre}csl-art21-mlps]\n",
                  encoding="utf-8")
    name = _user(("consultant",)).username
    out = tmp_path / "r.json"
    code = cli_main(["kb-eval", str(qf), "--as", name, "--embed-compare", "--b-url", "http://bge-m3.internal/v1",
                     "--b-model", "BAAI/bge-m3", "--b-dim", "1024", "--out", str(out)])
    assert code == 0
    text = capsys.readouterr().out
    assert "BAAI/bge-m3" in text and "MRR" in text
    assert json.loads(out.read_text(encoding="utf-8"))["models"][1]["model"] == "BAAI/bge-m3"


def test_compare_needs_embedding_configured(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "embed_base_url", "")
    with SessionLocal() as db, pytest.raises(embed_compare.EmbedCompareError):
        embed_compare.run(db, db.merge(_user(("consultant",))), _qs("ec-"),
                          b_url="http://bge-m3.internal/v1", b_model="BAAI/bge-m3", b_dim=1024)
