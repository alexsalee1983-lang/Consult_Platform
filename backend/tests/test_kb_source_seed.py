"""四期 B25：kb-source-seed 按资料仓的已批准来源清单批量登记为候选/试用；转正式仍须知识管理员在页面批准。"""
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.core.permissions import CONSULTANT, KNOWLEDGE_EXPERT
from app.models import AuditLog, KbSource, KbSourceEvent

from .conftest import create_user

HEAD = "name,url,publisher,license,authority_kind,note\n"
ROWS = ("种子来源甲：法规库,https://seed-a.example.org/,测试机关甲,法律不受著作权保护，可内部引用并注明出处,法律,\n"
        "种子来源乙：标准公开,https://seed-b.example.org/std/,测试机关乙,公开发布，可内部引用并注明出处,国家标准,备注\n")


def _csv(tmp_path, body: str) -> str:
    p = tmp_path / "seed.csv"
    p.write_text(HEAD + body, encoding="utf-8")
    return str(p)


def _src(url: str) -> KbSource | None:
    with SessionLocal() as db:
        return db.scalar(select(KbSource).where(KbSource.url == url))


def test_seed_registers_trial_and_is_idempotent(tmp_path, capsys):
    expert = create_user((KNOWLEDGE_EXPERT,))[0]
    path = _csv(tmp_path, ROWS)
    assert cli_main(["kb-source-seed", path, "--as", expert, "--to", "trial"]) == 0
    a, b = _src("https://seed-a.example.org/"), _src("https://seed-b.example.org/std/")
    assert a and b and a.status == b.status == "trial"
    assert a.authority_level == 1 and b.authority_level == 2 and a.created_by == expert
    with SessionLocal() as db:
        evs = db.scalars(select(KbSourceEvent.to_status).where(KbSourceEvent.source_id == a.id)).all()
        assert list(evs) == ["candidate", "trial"]
        acts = db.scalars(select(AuditLog.action).where(AuditLog.actor == expert)).all()
        assert acts.count("kb.source.create") == 2 and acts.count("kb.source.status") == 2
    out = capsys.readouterr().out
    assert "新登记 2" in out
    # 再跑一次：已登记的跳过，不重复、不改状态
    assert cli_main(["kb-source-seed", path, "--as", expert, "--to", "trial"]) == 0
    out = capsys.readouterr().out
    assert "新登记 0" in out and "跳过 2" in out


def test_seed_default_candidate_and_never_active(tmp_path, capsys):
    expert = create_user((KNOWLEDGE_EXPERT,))[0]
    path = _csv(tmp_path, "种子来源丙,https://seed-c.example.org/,机关丙,公开发布，可内部引用,白皮书,\n")
    assert cli_main(["kb-source-seed", path, "--as", expert]) == 0
    assert _src("https://seed-c.example.org/").status == "candidate"
    try:
        cli_main(["kb-source-seed", path, "--as", expert, "--to", "active"])
        raise AssertionError("--to active 应被 argparse 拒绝")
    except SystemExit as e:
        assert e.code == 2


def test_seed_bad_row_reported_others_kept(tmp_path, capsys):
    expert = create_user((KNOWLEDGE_EXPERT,))[0]
    body = ("坏行：缺许可,https://seed-d.example.org/,机关丁,,法律,\n"
            "种子来源戊,https://seed-e.example.org/,机关戊,公开发布，可内部引用,行业标准,\n")
    assert cli_main(["kb-source-seed", _csv(tmp_path, body), "--as", expert, "--to", "trial"]) == 3
    err = capsys.readouterr().err
    assert "第 2 行" in err and "许可必填" in err
    assert _src("https://seed-d.example.org/") is None
    assert _src("https://seed-e.example.org/").status == "trial"


def test_seed_requires_knowledge_staff(tmp_path):
    consultant = create_user((CONSULTANT,))[0]
    path = _csv(tmp_path, "种子来源己,https://seed-f.example.org/,机关己,公开发布，可内部引用,法律,\n")
    assert cli_main(["kb-source-seed", path, "--as", consultant]) == 1
    assert cli_main(["kb-source-seed", path, "--as", "no-such-user"]) == 1
    assert _src("https://seed-f.example.org/") is None
