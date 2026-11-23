#!/usr/bin/env bash
# 测试机一键测试：bash scripts/pr_test.sh <PR号> <环境字母> <日志目录>
# 做完：检出 PR 头提交 -> 独立 compose 项目起整套 -> 冒烟 -> 迁移头核对 -> 数据保护检查
#       -> 执行 PR 描述里“测试机命令”代码块 -> summary.md（第 3 节格式预填）-> docker compose down（不带 -v）。
# 环境目录 / 网页端口：ENV_DIR / WEB_PORT_OVERRIDE 环境变量优先，否则按 A..D 约定（与 pr_test_runner.py 的 ENVS 一致）。
# KEEP_UP=1：结束时不 down（PR 有需要服务在线的人工核对项或真实模型评测时用）。
# 大输出只落 <日志目录>/*.log，终端只打印每步一行结论。
set -u
PR="${1:?用法：pr_test.sh <PR号> <环境字母> <日志目录>}"
ENVL="${2:?缺环境字母}"
LOGDIR="${3:?缺日志目录}"
ENVL="$(echo "$ENVL" | tr a-z A-Z)"
case "$ENVL" in
  A) D=consult-platform; P=8180;;
  B) D=cp-test-b; P=8280;;
  C) D=cp-test-c; P=8380;;
  D) D=cp-test-d; P=8480;;
  *) echo "未知环境 $ENVL" >&2; exit 2;;
esac
ENV_DIR="${ENV_DIR:-$HOME/$D}"
PORT="${WEB_PORT_OVERRIDE:-$P}"
mkdir -p "$LOGDIR"
LOGDIR="$(cd "$LOGDIR" && pwd)"
SUMMARY="$LOGDIR/summary.md"
ENVLC="$(echo "$ENVL" | tr A-Z a-z)"
export COMPOSE_PROJECT_NAME="cp${ENVLC}-pr${PR}"
export WEB_PORT="$PORT"
RESULTS=()
FAILS=0
SMOKE_LINE="未执行"
DP="未执行"
HEAD_SHORT="?"
ROWN=0
CLEAN="是"

row() {  # row <名称> <通过|失败> <说明>
  ROWN=$((ROWN+1))
  RESULTS+=("| $ROWN | $1 | $2 | $3 |")
  echo "[$2] $1：$3"
  if [ "$2" = "失败" ]; then FAILS=$((FAILS+1)); fi
  return 0
}

write_summary() {  # write_summary <结论> <原因>
  local verdict="$1" why="$2"
  {
    echo "## 测试结果：$verdict"
    echo
    echo "环境 $ENVL（pr_test.sh 自动生成；人工核对项和真实模型评测由测试助手补写）"
    echo
    echo "- 测试机：$(hostname)，提交：$HEAD_SHORT，环境干净：$CLEAN"
    echo "- 冒烟测试：$SMOKE_LINE"
    echo "- 数据保护检查：$DP"
    if [ -n "$why" ]; then echo "- 说明：$why"; fi
    echo
    echo "| # | 测试项 | 结果 | 说明 |"
    echo "|---|---|---|---|"
    if [ "${#RESULTS[@]}" -gt 0 ]; then printf '%s\n' "${RESULTS[@]}"; fi
    echo "| | 人工核对项 | （待填） | |"
    echo
    echo "日志目录：$LOGDIR"
    if [ -s "$LOGDIR/failed.txt" ]; then
      echo
      echo "### 失败详情"
      echo '```'
      tail -50 "$LOGDIR/failed.txt"
      echo '```'
    fi
  } > "$SUMMARY"
}

env_fail() {  # 环境问题：写 summary 并退出
  echo "[环境问题未测] $1"
  write_summary "环境问题未测" "$1"
  if [ -d "$ENV_DIR" ]; then (cd "$ENV_DIR" && docker compose down >>"$LOGDIR/down.log" 2>&1); fi
  echo "RESULT: 环境问题未测 —— $1"
  exit 3
}

[ -d "$ENV_DIR/.git" ] || env_fail "环境目录 $ENV_DIR 不存在"
cd "$ENV_DIR" || env_fail "进不了 $ENV_DIR"
command -v gh >/dev/null || env_fail "缺 gh"
command -v docker >/dev/null || env_fail "缺 docker"

# 1. 检出 PR 头提交
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then CLEAN="否"; env_fail "工作区有未提交改动"; fi
HEAD_OID="$(gh pr view "$PR" --json headRefOid -q .headRefOid 2>"$LOGDIR/gh.log")" || env_fail "gh pr view 失败"
git fetch origin >"$LOGDIR/git.log" 2>&1 || env_fail "git fetch 失败"
git fetch origin "pull/$PR/head" >>"$LOGDIR/git.log" 2>&1 || env_fail "git fetch pull/$PR/head 失败"
git checkout --detach "$HEAD_OID" >>"$LOGDIR/git.log" 2>&1 || env_fail "检出 $HEAD_OID 失败"
HEAD_SHORT="$(git rev-parse --short HEAD)"
if [ "$(git rev-parse HEAD)" = "$HEAD_OID" ]; then row "检出 PR 头提交" 通过 "$HEAD_SHORT"; else env_fail "HEAD 与 PR 头不一致"; fi
gh pr view "$PR" --json body -q .body > "$LOGDIR/pr_body.md" 2>/dev/null

# 2. 停本环境旧项目（不带 -v），起新项目（独立数据卷 = 新库）
for old in $(docker compose ls -q 2>/dev/null | grep "^cp${ENVLC}-" | grep -v "^${COMPOSE_PROJECT_NAME}$"); do
  docker compose -p "$old" down >>"$LOGDIR/down.log" 2>&1
done
[ -f .env ] || env_fail ".env 不存在"
if ! docker compose up -d --build >"$LOGDIR/up.log" 2>&1; then
  tail -20 "$LOGDIR/up.log" > "$LOGDIR/failed.txt"
  env_fail "docker compose up --build 失败（见 up.log）"
fi
BASE="http://localhost:$PORT"
ok=0
for _ in $(seq 1 90); do
  bad="$(docker compose ps --format '{{.Name}} {{.Health}}' 2>/dev/null | grep -c -E 'starting|unhealthy')"
  if curl -s -o /dev/null --max-time 5 "$BASE/api/health" && [ "$bad" = "0" ]; then ok=1; break; fi
  sleep 5
done
docker compose ps > "$LOGDIR/ps.log" 2>&1
if [ "$ok" = 1 ]; then
  row "整套启动并健康" 通过 "项目 $COMPOSE_PROJECT_NAME，端口 $PORT"
else
  docker compose logs --no-color backend 2>&1 | tail -50 > "$LOGDIR/failed.txt"
  env_fail "服务 7.5 分钟内未全部健康"
fi

# 3. 冒烟
bash scripts/smoke_test.sh "$BASE" > "$LOGDIR/smoke.log" 2>&1
SRC=$?
SP="$(grep -o -E '通过[ :：]*[0-9]+' "$LOGDIR/smoke.log" | tail -1 | grep -o '[0-9]*$')"
SF="$(grep -o -E '失败[ :：]*[0-9]+' "$LOGDIR/smoke.log" | tail -1 | grep -o '[0-9]*$')"
SMOKE_LINE="通过 ${SP:-?} 项，失败 ${SF:-?} 项"
if [ "$SRC" = 0 ]; then
  row "冒烟测试" 通过 "$SMOKE_LINE"
elif [ "$SRC" = 2 ]; then
  env_fail "冒烟连不上 $BASE"
else
  row "冒烟测试" 失败 "$SMOKE_LINE（smoke.log）"
  grep -E "FAIL|失败|✗" "$LOGDIR/smoke.log" | head -30 >> "$LOGDIR/failed.txt"
fi

# 4. 迁移版本：库当前版本 == 分支代码里的头（主链、知识区链各一个头）
cat > "$LOGDIR/migcheck.py" <<'PY'
import os
import sys

from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.core.migrate import alembic_config

bad = 0
for chain, url, table in (("main", "DATABASE_URL", "alembic_version"), ("kb", "KB_DATABASE_URL", "alembic_version_kb")):
    heads = ScriptDirectory.from_config(alembic_config(chain=chain)).get_heads()
    try:
        with create_engine(os.environ[url]).connect() as c:
            cur = sorted(r[0] for r in c.execute(text(f"select version_num from {table}")))
    except Exception as e:
        cur = [f"读取失败:{type(e).__name__}"]
    ok = len(heads) == 1 and cur == sorted(heads)
    bad += 0 if ok else 1
    print(f"{chain} 头={heads} 库={cur} {'OK' if ok else 'BAD'}")
sys.exit(1 if bad else 0)
PY
docker compose exec -T backend python - < "$LOGDIR/migcheck.py" > "$LOGDIR/migrate.log" 2>&1
MR=$?
MSG="$(tr '\n' ' ' < "$LOGDIR/migrate.log" | cut -c1-200)"
if [ "$MR" = 0 ]; then
  row "迁移版本（主链/知识区各一个头且与库一致）" 通过 "$MSG"
else
  row "迁移版本（主链/知识区各一个头且与库一致）" 失败 "$MSG"
  cat "$LOGDIR/migrate.log" >> "$LOGDIR/failed.txt"
fi

# 5. 数据保护检查：审计链完整；backend/worker 日志不得出现 .env 里的口令、密钥原文
docker compose exec -T backend python -m app.cli verify-audit > "$LOGDIR/audit.log" 2>&1
AR=$?
docker compose logs --no-color backend worker > "$LOGDIR/app_logs.log" 2>&1
LEAK=0
rm -f "$LOGDIR/leak.txt"
while IFS='=' read -r k v; do
  v="${v%\"}"
  v="${v#\"}"
  [ "${#v}" -ge 8 ] || continue
  if grep -qF -- "$v" "$LOGDIR/app_logs.log"; then
    LEAK=$((LEAK+1))
    echo "日志含 $k 的值" >> "$LOGDIR/leak.txt"
  fi
done < <(grep -E '^[A-Z_]*(PASSWORD|SECRET|API_KEY|TOKEN|KEY)[A-Z_]*=' .env)
CANARY="$(grep -c -E 'PRTEST-CANARY' "$LOGDIR/app_logs.log")"
if [ "$AR" = 0 ] && [ "$LEAK" = 0 ] && [ "$CANARY" = 0 ]; then
  DP="通过"
  row "数据保护检查" 通过 "审计链完整；日志无口令/密钥原文（泄露 0，素材命中 0）"
else
  DP="不通过"
  row "数据保护检查" 失败 "审计链退出码 $AR；日志含密钥值 $LEAK 处；素材命中 $CANARY（只记个数，见 leak.txt）"
fi

# 6. PR 描述里的“测试机命令”代码块：块前一行含“测试机命令”；前 3 行注释含“宿主机”或 host 则在宿主机执行，否则进 backend 容器
cat > "$LOGDIR/extract.py" <<'PY'
import pathlib
import re
import sys

p = pathlib.Path(sys.argv[1])
body = p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""
out = pathlib.Path(sys.argv[2])
n = 0
for m in re.finditer(r"^[^\n]*测试机命令[^\n]*\n+```bash[^\n]*\n(.*?)\n```", body, re.S | re.M):
    n += 1
    (out / f"cmd{n}.sh").write_text(m.group(1) + "\n", encoding="utf-8")
(out / "cmd_count").write_text(str(n))
PY
python3 "$LOGDIR/extract.py" "$LOGDIR/pr_body.md" "$LOGDIR"
NCMD="$(cat "$LOGDIR/cmd_count" 2>/dev/null || echo 0)"
for i in $(seq 1 "$NCMD"); do
  f="$LOGDIR/cmd$i.sh"
  if head -3 "$f" | grep -qiE '^#.*(宿主机|host)'; then
    bash "$f" > "$LOGDIR/cmd$i.out" 2>&1
    CR=$?
    where="宿主机"
  else
    docker compose exec -T backend bash -s < "$f" > "$LOGDIR/cmd$i.out" 2>&1
    CR=$?
    where="backend 容器"
  fi
  if [ "$CR" = 0 ]; then
    row "PR 命令块 $i（$where）" 通过 "退出码 0，输出 cmd$i.out（末行：$(tail -1 "$LOGDIR/cmd$i.out" | cut -c1-80)）"
  else
    row "PR 命令块 $i（$where）" 失败 "退出码 $CR，输出 cmd$i.out"
    tail -30 "$LOGDIR/cmd$i.out" >> "$LOGDIR/failed.txt"
  fi
done
if [ "$NCMD" = 0 ]; then echo "（PR 描述无“测试机命令”代码块）"; fi

# 7. 收尾：down 不带 -v（保留数据卷）
if [ "${KEEP_UP:-0}" != 1 ]; then docker compose down >>"$LOGDIR/down.log" 2>&1; fi
if [ "$FAILS" = 0 ]; then
  write_summary "通过" ""
  echo "RESULT: 通过 —— 脚本项全部通过，待补人工核对项；summary：$SUMMARY"
else
  write_summary "不通过" "$FAILS 项失败"
  echo "RESULT: 不通过 —— $FAILS 项失败；summary：$SUMMARY"
fi
exit 0
