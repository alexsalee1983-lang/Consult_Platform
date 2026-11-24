"""测试机调度脚本：全绿且无人工判断项时不起模型会话（token 优化，负责人 2026-11-24）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pr_test_runner.py"


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pr_test_runner", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _runner()

BODY_GREEN = """## 改了什么
加了个字段。

## 怎么测
- 新增 tests/test_x.py，CI 覆盖。
- 人工核对项：无
- 涉及模型的项：模拟模型即可

测试机命令：
```bash
python -m app.cli foo  # 需真实模型 写在代码块里不算
```

## 对应计划
三期 X1
"""

BODY_HUMAN = """## 怎么测
1. 脚本项同上。
2. 浏览器打开交付件审阅页，人工核对确认卡按钮。
"""


def test_how_to_test_extracts_section_until_next_heading() -> None:
    sec = R.how_to_test(BODY_GREEN)
    assert sec.startswith("- 新增 tests/test_x.py")
    assert "对应计划" not in sec


def test_green_body_needs_no_session() -> None:
    assert R.needs_session(BODY_GREEN, has_recheck=False) == ""


def test_human_items_need_session() -> None:
    assert "人工核对" in R.needs_session(BODY_HUMAN, has_recheck=False) or "浏览器" in R.needs_session(
        BODY_HUMAN, has_recheck=False)


def test_real_model_eval_needs_session() -> None:
    body = "## 怎么测\n- 跑 agent-eval 一轮，需真实模型\n"
    assert R.needs_session(body, has_recheck=False)


def test_recheck_or_missing_section_needs_session() -> None:
    assert "复核说明" in R.needs_session(BODY_GREEN, has_recheck=True)
    assert R.needs_session("## 改了什么\n只改文档\n", has_recheck=False)


def test_bold_heading_section() -> None:
    body = "**怎么测**\n- CI 覆盖，人工核对项：无\n\n**对应计划**\n- 浏览器看一眼\n"
    assert R.how_to_test(body) == "- CI 覆盖，人工核对项：无"
    assert R.needs_session(body, has_recheck=False) == ""


def test_verdict_of_reads_last_result_line() -> None:
    assert R.verdict_of("[通过] 冒烟\nRESULT: 通过 —— 脚本项全部通过") == "通过"
    assert R.verdict_of("RESULT: 不通过 —— 2 项失败\n") == "不通过"
    assert R.verdict_of("RESULT: 环境问题未测 —— 目录不存在") == "环境问题未测"
    assert R.verdict_of("脚本中途崩了") == "环境问题未测"


def test_finalize_summary_fills_manual_row() -> None:
    text = ("环境 A（pr_test.sh 自动生成；人工核对项和真实模型评测由测试助手补写）\n"
            "| | 人工核对项 | （待填） | |\n")
    out = R.finalize_summary(text)
    assert "（待填）" not in out
    assert "由测试助手补写" not in out
    assert "| | 人工核对项 | 无 |" in out


def test_prompt_formats_with_all_fields() -> None:
    txt = R.PROMPT.format(pr=1, env="A", dir="/d", web="8180", head="abc1234", logdir="/l", verdict="不通过",
                          why="x", up="已 down", howto="h", recheck="", failed="", gh="gh")
    assert "RESULT:" in txt


def test_comment_sends_body_via_stdin_with_configured_gh(tmp_path: Path, monkeypatch) -> None:
    """#428 测试机：Windows 版 gh.exe 打不开 WSL 路径，正文须走标准输入；gh 可用 RUNNER_GH 指定。"""
    import subprocess

    calls: list[tuple[list[str], str | None]] = []

    def fake_run(args, **kw):  # type: ignore[no-untyped-def]
        calls.append((list(args), kw.get("input")))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(R, "GH", "/usr/local/bin/gh")
    monkeypatch.setattr(R.subprocess, "run", fake_run)
    f = tmp_path / "summary.md"
    f.write_text("## 结论 通过", encoding="utf-8")
    R.comment_file(5, f)
    args, stdin = calls[0]
    assert args[0] == "/usr/local/bin/gh"
    assert args[-2:] == ["--body-file", "-"] and str(f) not in args
    assert stdin == "## 结论 通过"


def test_comment_missing_summary_raises_clearly(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(RuntimeError, match="摘要文件不存在"):
        R.comment_file(5, tmp_path / "none.md")


def test_sh_failure_reports_rc(monkeypatch) -> None:
    import subprocess

    import pytest

    monkeypatch.setattr(R.subprocess, "run",
                        lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr=""))
    with pytest.raises(RuntimeError, match="rc=1"):
        R.sh(["gh", "pr", "view", "1"])


# ---- #438：OpenClaw 会话上下文超限 ----

def test_prompt_length_capped_for_huge_pr(tmp_path: Path) -> None:
    howto = "## 怎么测\n" + "- 人工核对项：逐条核对很长的说明\n" * 2000
    recheck = "## 复核说明\n" + "第 N 项复测细节。" * 3000
    failed = tmp_path / "failed.txt"
    failed.write_text("x" * 400 + "\n" * 1 + ("错误行\n" * 500), encoding="utf-8")
    prompt = R.build_prompt(env="A", pr=1, head="abcdef1234", logdir=tmp_path, verdict="不通过", why="x",
                            keep="1", body=howto, rechecks=[recheck])
    assert len(prompt) <= R.PROMPT_MAX
    # 全文落文件，提示里只给路径
    assert (tmp_path / "howto.md").read_text(encoding="utf-8").count("逐条核对") == 2000
    assert "复核说明" in (tmp_path / "recheck.md").read_text(encoding="utf-8")
    assert str(tmp_path / "howto.md") in prompt and str(tmp_path / "recheck.md") in prompt


def test_session_id_unique_per_run(tmp_path: Path) -> None:
    a = R.session_id(1, "abcdef1234", tmp_path / "pr1-1125-0130")
    b = R.session_id(1, "abcdef1234", tmp_path / "pr1-1125-0200")
    assert a != b  # 上次会话超限后重试不能复用同一会话


def test_salvage_posts_partial_summary_when_no_conclusion(tmp_path: Path, monkeypatch) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("## 测试结果：未通过\n| | 人工核对项 | ML-03 命中 |\n", encoding="utf-8")
    posted: list[str] = []
    monkeypatch.setattr(R, "pr_labels", lambda pr: ["待测试"])
    monkeypatch.setattr(R, "comment_file", lambda pr, p, check=True: posted.append(p.read_text(encoding="utf-8")))
    assert R.salvage(7, summary, before="旧内容")
    assert posted and "会话异常结束" in posted[0] and "ML-03" in posted[0]


def test_salvage_skips_when_concluded_or_unchanged(tmp_path: Path, monkeypatch) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("同样", encoding="utf-8")
    monkeypatch.setattr(R, "comment_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应评论")))
    monkeypatch.setattr(R, "pr_labels", lambda pr: ["待测试"])
    assert not R.salvage(7, summary, before="同样")
    summary.write_text("改过", encoding="utf-8")
    monkeypatch.setattr(R, "pr_labels", lambda pr: ["测试未通过"])
    assert not R.salvage(7, summary, before="同样")
