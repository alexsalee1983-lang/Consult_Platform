#!/usr/bin/env python3
"""测试机 PR 调度：GitHub 标签驱动，每个 PR 一个一次性 Claude Code 会话（决策记录 2026-11-23）。

用法（测试机，cron 每 15 分钟一次即可，脚本本身不常驻）。环境目录会被切到各 PR 分支，
所以先把脚本拷到 ~/pr-runner/ 再跑，更新时从 main 重新拷：
    mkdir -p ~/pr-runner && git -C ~/consult-platform show origin/main:scripts/pr_test_runner.py > ~/pr-runner/pr_test_runner.py
    */15 * * * * python3 ~/pr-runner/pr_test_runner.py >> ~/pr-runner/cron.log 2>&1

每次运行：
1. 收尾：检查各环境上次启动的会话是否结束；结束了就核对 PR 上是否已有结论评论和新标签，
   发一行群通知（NOTIFY_CMD），没产出结论的记失败，连续两次失败留给人看。
2. 派单：`gh pr list --label 待测试`，按环境空闲情况每个空闲环境领一单，
   在该环境目录用 `claude -p` 起一个全新会话（后台运行，日志落文件），本脚本立即退出。

群会话只收一行结论，不在群会话里跑测试；大输出只进日志文件。
环境变量（都可不设）：
    RUNNER_HOME     状态与日志目录，默认 ~/pr-runner
    RUNNER_MODEL    claude 模型，默认 sonnet
    RUNNER_TIMEOUT  单 PR 最长分钟数，默认 150
    RUNNER_ENVS     启用的环境，默认 A,B,C,D
    NOTIFY_CMD      发群通知的命令，消息作为最后一个参数追加；须“直接发群消息”而不是回复某条消息
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
ENVS: dict[str, dict[str, str]] = {
    "A": {"dir": str(HOME / "consult-platform"), "web": "8180"},
    "B": {"dir": str(HOME / "cp-test-b"), "web": "8280"},
    "C": {"dir": str(HOME / "cp-test-c"), "web": "8380"},
    "D": {"dir": str(HOME / "cp-test-d"), "web": "8480"},
}
# 开发线 2 的 PR（content/ 分支或带“线2”标签）优先 B、D，其余优先 A、C（测试交接规范开头的表）
LINE2_ORDER = ["B", "D", "A", "C"]
LINE1_ORDER = ["A", "C", "B", "D"]

RUNNER_HOME = Path(os.environ.get("RUNNER_HOME", str(HOME / "pr-runner")))
STATE = RUNNER_HOME / "state.json"
MODEL = os.environ.get("RUNNER_MODEL", "sonnet")
TIMEOUT_MIN = int(os.environ.get("RUNNER_TIMEOUT", "150"))
ENABLED = [e.strip() for e in os.environ.get("RUNNER_ENVS", "A,B,C,D").split(",") if e.strip() in ENVS]
MAX_FAILS = 2

PROMPT = """你是测试机上的测试助手，这是一次性会话：只测 PR #{pr}，测完即结束。
环境 {env}：目录 {dir}，网页端口 {web}（冒烟：bash scripts/smoke_test.sh http://localhost:{web}）。
先读 docs/测试交接规范.md（第 1～4 节）并照做：检出分支 {branch} 并确认 HEAD 为 {head}、git status 干净；按 PR 描述“怎么测”
或最新一条标题为“复核说明”的评论逐项测；每次都做数据保护检查。
规则：
- 不改代码、不推送、不写数据库（只经平台接口和 app.cli）；需要清库、删数据卷先停下，评论里写明需要负责人决定。
- 控制上下文：命令输出一律重定向到 {logdir}/ 下的文件，只用 tail -50 / grep 看需要的部分，不要整段打印日志、pytest 或 docker 输出。
- 同一项试两次不达预期就记现象、测下一项。
- 结束前必须做两件事：
  1. 按第 3 节格式写结论到文件，`gh pr comment {pr} --body-file <文件>`；评论首行写明“环境 {env}”。
  2. 换标签：`gh pr edit {pr} --remove-label 待测试 --add-label 测试通过`（或 测试未通过）。
最后一行只输出：RESULT: 通过 或 RESULT: 不通过 —— 一句话原因。"""


def sh(args: list[str], cwd: str | None = None, check: bool = True) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def log(msg: str) -> None:
    print(time.strftime("%m-%d %H:%M:%S ") + msg, flush=True)


def notify(msg: str) -> None:
    log("通知：" + msg)
    cmd = os.environ.get("NOTIFY_CMD")
    if cmd:
        subprocess.run([*shlex.split(cmd), msg], capture_output=True)


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"running": {}, "fails": {}}


def save(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pr_labels(pr: int) -> list[str]:
    out = sh(["gh", "pr", "view", str(pr), "--json", "labels", "-q", "[.labels[].name]"])
    return json.loads(out or "[]")


def finish(st: dict) -> None:
    for env, job in list(st["running"].items()):
        pid, pr = job["pid"], job["pr"]
        over = time.time() - job["start"] > TIMEOUT_MIN * 60
        if alive(pid) and not over:
            continue
        if alive(pid):
            os.killpg(pid, signal.SIGTERM)
            log(f"#{pr} 环境 {env} 超时 {TIMEOUT_MIN} 分钟，已停")
        labels = pr_labels(pr)
        tail = ""
        try:
            lines = Path(job["log"]).read_text(encoding="utf-8", errors="replace").strip().splitlines()
            tail = next((ln for ln in reversed(lines) if ln.startswith("RESULT:")), "")
        except OSError:
            pass
        key = f"{pr}@{job['head']}"
        if "待测试" not in labels and ("测试通过" in labels or "测试未通过" in labels):
            res = "通过" if "测试通过" in labels else "不通过"
            reason = tail.split("——", 1)[-1].strip() if "——" in tail else ""
            notify(f"#{pr} 测试{res}（环境 {env}）{('：' + reason) if reason else ''}")
            st["fails"].pop(key, None)
        else:
            n = st["fails"].get(key, 0) + 1
            st["fails"][key] = n
            msg = f"#{pr} 环境 {env} 会话结束但没写结论（第 {n} 次），日志 {job['log']}"
            if n >= MAX_FAILS:
                msg += "；不再自动重试，请人工看"
            notify(msg)
        del st["running"][env]


def dispatch(st: dict) -> None:
    free = [e for e in ENABLED if e not in st["running"]]
    if not free:
        return
    busy = {j["pr"] for j in st["running"].values()}
    out = sh(["gh", "pr", "list", "--label", "待测试", "--state", "open", "--limit", "50", "--json",
              "number,headRefName,headRefOid,labels,createdAt"])
    prs = sorted(json.loads(out or "[]"), key=lambda p: p["createdAt"])
    for p in prs:
        if not free:
            break
        pr, head = p["number"], p["headRefOid"]
        if pr in busy or st["fails"].get(f"{pr}@{head}", 0) >= MAX_FAILS:
            continue
        names = {lb["name"] for lb in p["labels"]}
        line2 = p["headRefName"].startswith("content/") or "线2" in names
        env = next(e for e in (LINE2_ORDER if line2 else LINE1_ORDER) + free if e in free)
        free.remove(env)
        start(st, env, pr, p["headRefName"], head)


def start(st: dict, env: str, pr: int, branch: str, head: str) -> None:
    cfg = ENVS[env]
    logdir = RUNNER_HOME / "logs" / f"pr{pr}-{time.strftime('%m%d-%H%M')}"
    logdir.mkdir(parents=True, exist_ok=True)
    prompt = PROMPT.format(pr=pr, env=env, dir=cfg["dir"], web=cfg["web"], branch=branch,
                           head=head[:7], logdir=logdir)
    logf = logdir / "session.log"
    with logf.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            ["claude", "-p", prompt, "--model", MODEL, "--dangerously-skip-permissions"],
            cwd=cfg["dir"], stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
    st["running"][env] = {"pid": proc.pid, "pr": pr, "head": head, "start": time.time(), "log": str(logf)}
    log(f"#{pr} 派到环境 {env}（pid {proc.pid}），日志 {logf}")


def main() -> int:
    RUNNER_HOME.mkdir(parents=True, exist_ok=True)
    lock = RUNNER_HOME / "runner.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 600:
            return 0
        lock.unlink()
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        st = load()
        finish(st)
        save(st)
        if "--finish-only" not in sys.argv:
            dispatch(st)
            save(st)
    finally:
        lock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
