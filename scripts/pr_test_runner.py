#!/usr/bin/env python3
"""测试机 PR 调度：GitHub 标签驱动，每个 PR 一个一次性 Claude Code 会话（决策记录 2026-11-23）。

用法（测试机，cron 每 15 分钟一次即可，脚本本身不常驻）。环境目录会被切到各 PR 分支，
所以先把脚本拷到 ~/pr-runner/ 再跑，更新时从 main 重新拷：
    mkdir -p ~/pr-runner && git -C ~/consult-platform show origin/main:scripts/pr_test_runner.py > ~/pr-runner/pr_test_runner.py
    */15 * * * * python3 ~/pr-runner/pr_test_runner.py >> ~/pr-runner/cron.log 2>&1

每次运行：
1. 收尾：检查各环境上次启动的会话是否结束；结束了就核对 PR 上是否已有结论评论和新标签，
   发一行群通知（NOTIFY_CMD），没产出结论的记失败，连续两次失败留给人看。
2. 派单：`gh pr list --label 待测试`，按环境空闲情况每个空闲环境领一单，后台起本脚本的 --run-one（日志落文件），本脚本立即退出。
3. --run-one（token 优化，负责人 2026-11-24）：先由脚本跑 pr_test.sh（冒烟、迁移头、数据保护、PR 测试机命令块）。
   - 全部通过、“怎么测”里没有人工核对、真实模型、浏览器类项、也没有复核说明：脚本直接贴“测试通过”评论、换标签，不起模型会话；
   - 环境问题未测：脚本贴 summary、不换标签，不起会话；
   - 其余（有失败、需人工判断）：才起一次性会话（默认 `claude -p`，可用 RUNNER_AGENT_CMD 换成 OpenClaw 等），
     只给 summary 路径、“怎么测”段、复核说明和失败日志 tail -50。

群会话只收一行结论，不在群会话里跑测试；大输出只进日志文件。
环境变量（都可不设）：
    RUNNER_HOME     状态与日志目录，默认 ~/pr-runner
    RUNNER_MODEL    claude 模型，默认 sonnet
    RUNNER_TIMEOUT  单 PR 最长分钟数，默认 150
    RUNNER_ENVS     启用的环境，默认 A,B,C,D
    RUNNER_AGENT_CMD 起一次性测试会话的命令模板，默认用 claude -p。可用占位符（按空格拆分后逐段替换，
                    每个占位符保持为一个参数）：{prompt} 提示词全文、{prompt_file} 提示词文件、
                    {session} 本单会话名 pr<号>-<提交前 7 位>（同一提交重试复用同一会话）、{model} RUNNER_MODEL。
                    例（OpenClaw，每单新会话）：openclaw agent --session-id {session} --message {prompt}
    RUNNER_SETSID   默认 1：会话进程放进独立进程组，超时整组停掉。WSL 调 Windows 程序时 setsid 会让互操作超时，设 0
    NOTIFY_CMD      发群通知的命令，消息作为最后一个参数追加；须“直接发群消息”而不是回复某条消息
"""
from __future__ import annotations

import json
import os
import re
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
# 会话进程退出但代理网关侧那一轮可能仍在跑（CLI 与网关断连），此时不能释放环境
DETACHED_MARKS = ("may still be running this turn", "Lost the connection")
SETSID = os.environ.get("RUNNER_SETSID", "1") != "0"

PROMPT = """你是测试机上的测试助手，这是一次性会话：只测 PR #{pr}，测完即结束。
环境 {env}：目录 {dir}，网页端口 {web}。日志目录 {logdir}。
调度脚本已跑完 pr_test.sh（检出 PR 头提交 {head}、独立 compose 项目新库起整套、冒烟、迁移头、数据保护、PR 测试机命令块），
不要重跑这些步骤。脚本结论：{verdict}。起会话的原因：{why}。服务{up}。
摘要在 {logdir}/summary.md，各项日志在 {logdir}/ 下，需要时只看 tail -50 / grep。

PR 描述“怎么测”段：
{howto}
{recheck}
失败日志（failed.txt 末 50 行，无失败则为空）：
{failed}

要做的事：
1. 有失败项：看对应日志 tail -50 定位原因，把一句话原因写进 summary.md 对应行的“说明”。
2. 补做“怎么测”里的人工核对项和真实模型评测（多轮规则见 docs/测试交接规范.md 第 5 节，只读需要的节），
   结果替换 summary.md 里“（待填）”行，改写首行总结论。额外 pytest 一律走 scripts/pytest_iso.sh
   （--repo {dir} --tag {env}-<标签>，PostgreSQL 加 --pg），不要在环境目录里直接跑 pytest。
规则：
- 不改代码、不推送、不写数据库（只经平台接口和 app.cli）；需要清库、删数据卷先停下，评论里写明需要负责人决定。
- 命令输出一律重定向到 {logdir}/ 下的文件，只用 tail -50 / grep 看；不用 tmux、screen、nohup、& 等后台方式。
- 同一项试两次不达预期就记现象、测下一项；数据保护或冒烟失败如实写，不为通过而改动。
- 结束前必须：`gh pr comment {pr} --body-file {logdir}/summary.md`；再换标签
  `gh pr edit {pr} --remove-label 待测试 --add-label 测试通过`（或 测试未通过）。
最后一行只输出：RESULT: 通过 / RESULT: 不通过 / RESULT: 环境问题未测 —— 一句话原因（环境问题未测时不换标签，只评论）。"""

# “怎么测”里出现这些词说明需要模型或人判断；同一行带否定词（无、不需要、模拟模型即可）不算
HUMAN_RE = re.compile(r"人工核对|人工检查|人工确认|手动|需真实模型|需要真实模型|真实模型评测|浏览器|截图|界面核对"
                      r"|agent-eval|kb-eval|md 评测|D[1-8] ?评测")
NEG_RE = re.compile(r"[：:]\s*无|不需要|不需|无需|模拟模型即可|不涉及|免测")
HEAD_RE = re.compile(r"^\s*(#{1,6}\s|\*\*[^*]+\*\*[：:]?\s*$)")


def _level(line: str) -> int:
    t = line.lstrip()
    return len(t) - len(t.lstrip("#")) if t.startswith("#") else 7


def how_to_test(body: str) -> str:
    """PR 描述里“怎么测”一节（到下一个同级或更高级标题为止）；没有这一节返回空串。"""
    lines = body.splitlines()
    for k, ln in enumerate(lines):
        if "怎么测" in ln and HEAD_RE.match(ln):
            level, out = _level(ln), []
            for nxt in lines[k + 1:]:
                if HEAD_RE.match(nxt) and _level(nxt) <= level:
                    break
                out.append(nxt)
            return "\n".join(out).strip()
    return ""


def needs_session(body: str, has_recheck: bool) -> str:
    """需要起模型会话的原因；返回空串表示脚本项全过即可直接出结论。"""
    if has_recheck:
        return "PR 有复核说明，只测点名项"
    howto = how_to_test(body)
    if not howto:
        return "PR 描述没有“怎么测”一节"
    in_code = False
    for ln in howto.splitlines():
        if ln.strip().startswith("```"):
            in_code = not in_code
            continue
        m = None if in_code else HUMAN_RE.search(ln)
        if m and not NEG_RE.search(ln):
            return f"“怎么测”含需判断项（{m.group(0)}）"
    return ""


def finalize_summary(text: str) -> str:
    """脚本直接出结论时改写 summary：人工核对项填“无”，去掉“由测试助手补写”。"""
    text = text.replace("| | 人工核对项 | （待填） | |",
                        "| | 人工核对项 | 无 | “怎么测”无人工核对或真实模型项，调度脚本直接出结论、未起模型会话 |")
    return text.replace("人工核对项和真实模型评测由测试助手补写", "无人工核对项，调度脚本直接出结论")


def verdict_of(run_log: str) -> str:
    """pr_test.sh 输出里最后一条 RESULT 行的结论；没有 RESULT 行按环境问题处理。"""
    line = next((ln for ln in reversed(run_log.splitlines()) if ln.startswith("RESULT:")), "")
    return next((v for v in ("环境问题未测", "不通过", "通过") if line.startswith(f"RESULT: {v}")), "环境问题未测")



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
        st = json.loads(STATE.read_text(encoding="utf-8"))
        st.setdefault("held", {})
        return st
    return {"running": {}, "fails": {}, "held": {}}


def save(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def orphan_tmux(pr: int) -> str:
    """会话退出后残留的 tmux 会话（名字含 pr<号>），有则返回名字。没装 tmux 或没有会话返回空。"""
    try:
        out = subprocess.run(["tmux", "ls", "-F", "#S"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return next((n for n in out.split() if f"pr{pr}" in n), "")


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
            try:
                os.killpg(pid, signal.SIGTERM) if job.get("setsid", True) else os.kill(pid, signal.SIGTERM)
            except OSError:
                os.kill(pid, signal.SIGTERM)
            log(f"#{pr} 环境 {env} 超时 {TIMEOUT_MIN} 分钟，已停")
        labels = pr_labels(pr)
        tail = ""
        lines: list[str] = []
        try:
            lines = Path(job["log"]).read_text(encoding="utf-8", errors="replace").strip().splitlines()
            tail = next((ln for ln in reversed(lines) if ln.startswith("RESULT:")), "")
        except OSError:
            pass
        key = f"{pr}@{job['head']}"
        concluded = "待测试" not in labels and ("测试通过" in labels or "测试未通过" in labels)
        if not tail and not concluded and any(m in ln for ln in lines[-20:] for m in DETACHED_MARKS):
            # 断连：网关侧可能还在跑，挂起本环境和本 PR，人工确认后从 state.json 的 held 删掉再派
            st["held"][env] = job
            del st["running"][env]
            notify(f"#{pr} 环境 {env} 会话与网关断连、可能仍在跑，已挂起该环境和该 PR，"
                   f"确认网关侧已停后删 state.json 的 held.{env} 再派，日志 {job['log']}")
            continue
        orphan = "" if (tail or concluded) else orphan_tmux(pr)
        if orphan:
            # 会话把活丢进 tmux 后自己退出：环境仍被占，挂起，人工处理孤儿后从 held 删掉再派
            st["held"][env] = job
            del st["running"][env]
            notify(f"#{pr} 环境 {env} 会话已退出但留下 tmux 会话 {orphan}、环境仍被占，已挂起；"
                   f"等它跑完人工写结论或停掉后删 state.json 的 held.{env} 再派，日志 {job['log']}")
            continue
        reason = tail.split("——", 1)[-1].strip() if "——" in tail else ""
        if tail.startswith("RESULT: 环境问题未测"):
            # 环境问题不算 PR 缺陷：不重试本 HEAD，等人工修好环境后清 state.json 的 fails 再派
            st["fails"][key] = MAX_FAILS
            notify(f"#{pr} 环境问题未测（环境 {env}）{('：' + reason) if reason else ''}；标签留 待测试，修好环境后人工重派")
        elif "待测试" not in labels and ("测试通过" in labels or "测试未通过" in labels):
            res = "通过" if "测试通过" in labels else "不通过"
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
    held = st.get("held", {})
    free = [e for e in ENABLED if e not in st["running"] and e not in held]
    if not free:
        return
    busy = {j["pr"] for j in st["running"].values()} | {j["pr"] for j in held.values()}
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
        if "攒批评测" in names:  # md/评测题类 PR 等批次候选分支统一评测，不单独派单
            continue
        line2 = p["headRefName"].startswith("content/") or "线2" in names
        env = next(e for e in (LINE2_ORDER if line2 else LINE1_ORDER) + free if e in free)
        free.remove(env)
        start(st, env, pr, p["headRefName"], head)


def start(st: dict, env: str, pr: int, branch: str, head: str) -> None:
    logdir = RUNNER_HOME / "logs" / f"pr{pr}-{time.strftime('%m%d-%H%M')}"
    logdir.mkdir(parents=True, exist_ok=True)
    logf = logdir / "session.log"
    cmd = [sys.executable, str(Path(__file__).resolve()), "--run-one", env, str(pr), head, str(logdir)]
    with logf.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            cmd, cwd=ENVS[env]["dir"], stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=SETSID)
    st["running"][env] = {"pid": proc.pid, "pr": pr, "head": head, "start": time.time(), "log": str(logf),
                         "setsid": SETSID}
    log(f"#{pr} 派到环境 {env}（pid {proc.pid}），日志 {logf}")


def down(env: str, pr: int) -> None:
    """KEEP_UP 起的服务在会话结束后收掉（不带 -v，保留数据卷）。"""
    subprocess.run(["docker", "compose", "down"], cwd=ENVS[env]["dir"], capture_output=True,
                   env={**os.environ, "COMPOSE_PROJECT_NAME": f"cp{env.lower()}-pr{pr}"})


def run_one(env: str, pr: int, head: str, logdir: Path) -> int:
    """后台进程：先跑 pr_test.sh；只有失败或需判断时才起模型会话。最后一行打印 RESULT 供 finish() 读。"""
    cfg = ENVS[env]
    info = json.loads(sh(["gh", "pr", "view", str(pr), "--json", "body,comments"]) or "{}")
    body = info.get("body") or ""
    rechecks = [c.get("body") or "" for c in info.get("comments") or [] if "复核说明" in (c.get("body") or "")[:40]]
    why = needs_session(body, bool(rechecks))
    keep = "1" if why else os.environ.get("KEEP_UP", "0")
    run_log = logdir / "run.log"
    with run_log.open("w", encoding="utf-8") as fh:
        subprocess.run(["bash", "scripts/pr_test.sh", str(pr), env, str(logdir)], cwd=cfg["dir"],
                       stdout=fh, stderr=subprocess.STDOUT, env={**os.environ, "KEEP_UP": keep})
    verdict = verdict_of(run_log.read_text(encoding="utf-8", errors="replace"))
    summary = logdir / "summary.md"
    if verdict == "环境问题未测":
        if keep == "1":
            down(env, pr)
        if summary.exists():
            sh(["gh", "pr", "comment", str(pr), "--body-file", str(summary)], check=False)
        print("RESULT: 环境问题未测 —— 见 summary（脚本判定，未起模型会话）", flush=True)
        return 0
    if verdict == "通过" and not why:
        summary.write_text(finalize_summary(summary.read_text(encoding="utf-8")), encoding="utf-8")
        sh(["gh", "pr", "comment", str(pr), "--body-file", str(summary)])
        sh(["gh", "pr", "edit", str(pr), "--remove-label", "待测试", "--add-label", "测试通过"])
        print("RESULT: 通过 —— 脚本项全部通过，无人工核对项（未起模型会话）", flush=True)
        return 0
    failed = logdir / "failed.txt"
    prompt = PROMPT.format(
        pr=pr, env=env, dir=cfg["dir"], web=cfg["web"], head=head[:7], logdir=logdir, verdict=verdict,
        why=why or "脚本项有失败，需定位原因",
        up="仍在线（KEEP_UP=1），会话结束后由调度脚本 down" if keep == "1" else "已 down，需要时自己 docker compose up -d",
        howto=how_to_test(body) or "（无）",
        recheck=("\n最新复核说明（以此为准，只测点名项）：\n" + rechecks[-1] + "\n") if rechecks else "",
        failed="\n".join(failed.read_text(encoding="utf-8", errors="replace").splitlines()[-50:])
        if failed.exists() else "")
    pfile = logdir / "prompt.txt"
    pfile.write_text(prompt, encoding="utf-8")
    sub = {"prompt": prompt, "prompt_file": str(pfile), "session": f"pr{pr}-{head[:7]}",  # 同一提交重试复用同一会话
           "model": MODEL}
    tmpl = os.environ.get("RUNNER_AGENT_CMD") or "claude -p {prompt} --model {model} --dangerously-skip-permissions"
    print(f"起模型会话：{why or verdict}", flush=True)
    try:
        subprocess.run([part.format(**sub) for part in shlex.split(tmpl)], cwd=cfg["dir"], stdin=subprocess.DEVNULL)
    finally:
        if keep == "1":
            down(env, pr)
    return 0


def main() -> int:
    if len(sys.argv) == 6 and sys.argv[1] == "--run-one":
        return run_one(sys.argv[2], int(sys.argv[3]), sys.argv[4], Path(sys.argv[5]))
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
