#!/usr/bin/env bash
# 并发 pytest 隔离跑：每份 pytest 在自己的 git worktree 里跑（SQLite 各自 backend/tests/test.db）；
# PG 模式每次建独立库，用 TEST_DATABASE_URL / TEST_KB_DATABASE_URL 传进去，结束自动清理。
#
# 背景：测试机会同时跑多份 pytest（broad / broad2 / broad_pg、环境 A/B）。共用同一个
# backend/tests/test.db 时，后起的一份会在 setup 阶段清库，把前一份的用户和任务清掉，
# 表现为随机 401/409 失败（issue #382，本机单跑不复现）。
#
# 用法：
#   bash scripts/pytest_iso.sh --repo <环境目录> --tag <标签> [选项] -- <pytest 参数...>
# 例：
#   bash scripts/pytest_iso.sh --repo /home/pc/consult-platform --tag A-broad -- -q tests
#   bash scripts/pytest_iso.sh --repo /home/pc/consult-platform --tag A-broad-pg --pg -- -q tests
# 说明：pytest 的工作目录是 <worktree>/backend，所以参数里的路径按 backend 相对写。
#
# 选项（同名环境变量可替代，括号内为默认值）：
#   --pg                 用 PostgreSQL 跑（默认 SQLite）
#   --python PATH        (PYTEST_PY=/home/pc/venv-b/bin/python)
#   --pg-port N          (PT_PG_PORT=55432)       宿主机 PG 端口
#   --pg-container NAME  (PT_PG_CONTAINER=prtest-pg) 建库/删库用的容器
#   --db-prefix P        (PT_DB_PREFIX=pt)        库名前缀，库名为 P_<标签>_<时间戳>
#   --pg-user U          (PT_PG_USER=consult)
#   --pg-password P      (PT_PG_PASSWORD=consult)
#   --pg-url URL         (PYTEST_PG_URL)          自定义 SQLAlchemy URL 前缀
#   --keep               排障用：保留 worktree 和数据库
set -uo pipefail

REPO=""; TAG=""; PG=0; KEEP=0
PY="${PYTEST_PY:-/home/pc/venv-b/bin/python}"
PG_PORT="${PT_PG_PORT:-55432}"
PGC="${PT_PG_CONTAINER:-prtest-pg}"
DB_PREFIX="${PT_DB_PREFIX:-pt}"
PG_USER="${PT_PG_USER:-consult}"
PG_PASS="${PT_PG_PASSWORD:-consult}"
PG_URL="${PYTEST_PG_URL:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2;;
    --tag) TAG="$2"; shift 2;;
    --pg) PG=1; shift;;
    --python) PY="$2"; shift 2;;
    --pg-port) PG_PORT="$2"; shift 2;;
    --pg-container) PGC="$2"; shift 2;;
    --db-prefix) DB_PREFIX="$2"; shift 2;;
    --pg-user) PG_USER="$2"; shift 2;;
    --pg-password) PG_PASS="$2"; shift 2;;
    --pg-url) PG_URL="$2"; shift 2;;
    --keep) KEEP=1; shift;;
    --) shift; break;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) break;;
  esac
done

[ -n "$REPO" ] || { echo "用法: $0 --repo <环境目录> --tag <标签> [--pg] -- <pytest 参数>" >&2; exit 2; }
[ -d "$REPO/.git" ] || [ -f "$REPO/.git" ] || { echo "不是 git 目录: $REPO" >&2; exit 2; }
[ -n "$TAG" ] || TAG="$(basename "$REPO")"
[ -x "$PY" ] || { echo "python 不可执行: $PY（用 --python 指定）" >&2; exit 2; }

STAMP="$(date +%m%d%H%M%S)_$$"
SAFE="$(printf '%s' "$TAG" | tr -c 'A-Za-z0-9_.-' '_')"
RUN="${TMPDIR:-/tmp}/pytest-iso/${SAFE}-${STAMP}"
mkdir -p "$RUN/tmp" || exit 2

cleanup() {
  if [ "$KEEP" = 0 ]; then
    if [ -n "${PGDB:-}" ]; then
      for db in "$PGDB" "${PGDB}_kb"; do
        docker exec "$PGC" psql -qU "$PG_USER" -d postgres \
          -c "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" >/dev/null 2>&1
      done
    fi
    git -C "$REPO" worktree remove --force "$RUN/wt" >/dev/null 2>&1
    rm -rf "$RUN"
  else
    echo "KEEP=1，worktree 保留在 $RUN/wt"
  fi
}
trap cleanup EXIT

# 独立工作目录：worktree 指向当前 HEAD，SQLite 的 test.db 自然落在本 worktree 的 backend/tests 下
ok=0
for _ in 1 2 3; do
  if git -C "$REPO" worktree add --detach --quiet "$RUN/wt" HEAD; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "git worktree add 失败: $REPO" >&2; exit 2; }

if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
  echo "WARN: $REPO 有未提交改动，隔离跑只测 HEAD 提交内容" >&2
fi

export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$RUN/tmp"
unset TEST_DATABASE_URL TEST_KB_DATABASE_URL TEST_KB_SPLIT

if [ "$PG" = 1 ]; then
  if ! docker inspect -f '{{.State.Running}}' "$PGC" 2>/dev/null | grep -q true; then
    echo "PG 容器 $PGC 没在跑。可以先起一个专用实例：" >&2
    echo "  docker run -d --name $PGC -p 127.0.0.1:$PG_PORT:5432 \\" >&2
    echo "    -e POSTGRES_USER=$PG_USER -e POSTGRES_PASSWORD=$PG_PASS -e POSTGRES_DB=consult \\" >&2
    echo "    consult-platform-postgres:pg16-zh" >&2
    exit 2
  fi
  PGDB="${DB_PREFIX}_$(printf '%s' "$TAG" | tr -c 'A-Za-z0-9_' '_')_${STAMP}"
  for db in "$PGDB" "${PGDB}_kb"; do
    docker exec "$PGC" psql -qU "$PG_USER" -d postgres -v ON_ERROR_STOP=1 \
      -c "CREATE DATABASE \"$db\"" >/dev/null || { echo "建库失败: $db" >&2; exit 2; }
  done
  BASE="${PG_URL:-postgresql+psycopg://${PG_USER}:${PG_PASS}@127.0.0.1:${PG_PORT}}"
  export TEST_DATABASE_URL="$BASE/$PGDB"
  export TEST_KB_DATABASE_URL="$BASE/${PGDB}_kb"
  echo "ISO_PG db=$PGDB kb=${PGDB}_kb"
fi

cd "$RUN/wt/backend" || exit 2
"$PY" -m pytest -p no:cacheprovider "$@"
rc=$?
echo "ISO_RUN tag=$TAG rc=$rc db=${PGDB:-sqlite:$RUN/wt/backend/tests/test.db}"
exit $rc
