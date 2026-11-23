"""三期 A13②：增量评审。第二轮起只把改过的章节发给评审 Agent，未改章节只留标题；上轮未关闭、落在改过章节或全文的
问题列给 Agent 复核；落在未改章节的问题原样沿用（不因 Agent 没看到就记已解决）。首轮、章节结构变了、全部章节都改了时全文评审。"""
from .test_quality import BODY1, BODY2, _draft, _review, _url, model, team  # noqa: F401
from .test_review_issues import HEAD, ROW_A, ROW_B, ROW_C, _codes, _ledger


def _user_msg(model) -> str:  # noqa: F811
    return model.requests[-1]["messages"][-1]["content"]


def test_second_round_sends_only_changed_sections_and_carries_untouched(team, model):  # noqa: F811
    code, member, *_ = team
    model.reply = HEAD.format(b=1, m=1) + ROW_A + ROW_B
    _draft(member, code)
    rv1 = _review(member, code)
    assert BODY1 in _user_msg(model) and BODY2 in _user_msg(model) and "增量评审" not in rv1["report"]
    c1 = _codes(rv1)
    qa, qb = c1["夜间差距结论没有证据编号"], c1["建议缺少验收标准"]

    # 只改第 2 章；Agent 这轮只报一条新问题 C
    model.reply = HEAD.format(b=0, m=1) + ROW_C
    assert member.put(_url(code, "sections/2"), json={"body": BODY2 + "补充值班表。"}).status_code == 201
    rv2 = _review(member, code)
    msg = _user_msg(model)
    assert BODY1 not in msg and "补充值班表" in msg and "未改" in msg
    assert qa in msg and qb not in msg            # 第 2 章的旧问题交 Agent 复核；第 1 章的不发
    c2 = _codes(rv2)
    assert c2["建议缺少验收标准"] == qb and c2["人数与第 1 章不一致"] == "Q-003"
    carried = next(i for i in rv2["issues"] if i.get("code") == qb)
    assert carried["carried"] and carried["severity"] == "重要"
    assert rv2["major"] == 2 and "增量评审" in rv2["report"]
    assert _ledger(code) == {qa: "resolved", qb: "open", "Q-003": "open"}


def test_full_review_when_all_sections_changed(team, model):  # noqa: F811
    code, member, *_ = team
    model.reply = HEAD.format(b=1, m=1) + ROW_A + ROW_B
    _draft(member, code)
    _review(member, code)
    model.reply = HEAD.format(b=0, m=0)
    for i, b in ((1, BODY1), (2, BODY2)):
        assert member.put(_url(code, f"sections/{i}"), json={"body": b + "改。"}).status_code == 201
    rv = _review(member, code)
    assert "增量评审" not in rv["report"] and BODY1 in _user_msg(model)
    assert not any(i.get("carried") for i in rv["issues"])
    assert set(_ledger(code).values()) == {"resolved"}


def test_model_metadata_survives_lock(team, model):  # noqa: F811
    """缺陷回归：评审写结果前加锁 refresh，曾把提示词版本、模型名、token 数冲掉（增量评审靠提示词版本判断）。"""
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models import QualityReview

    code, member, *_ = team
    _draft(member, code)
    rid = _review(member, code)["id"]
    with SessionLocal() as db:
        r = db.scalar(select(QualityReview).where(QualityReview.id == rid))
        assert r.prompt_version and r.model == "mock-model" and r.prompt_tokens == 100
