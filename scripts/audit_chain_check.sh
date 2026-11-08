#!/usr/bin/env bash
# 审计哈希链并发检查：起多个进程同时写审计，再校验哈希链没有分叉或断开。
# 用来验证 PostgreSQL 上的 advisory lock（多进程、多副本场景）。在部署机仓库根目录执行：
#   bash scripts/audit_chain_check.sh        # 默认 4 个进程，每个写 50 条
# 会写入 action=test.concurrency 的测试记录，生产环境（APP_ENV=prod）拒绝执行。
set -eu
PROCS="${1:-4}"
PER="${2:-50}"

for i in $(seq 1 "$PROCS"); do
  docker compose exec -T backend python - "$i" "$PER" <<'PY' &
import sys
from app.core import audit
from app.core.config import get_settings
from app.core.db import SessionLocal

if get_settings().is_prod:
    sys.exit("生产环境不执行并发写入测试")
proc, per = int(sys.argv[1]), int(sys.argv[2])
with SessionLocal() as db:
    for k in range(per):
        audit.record(db, actor="chaincheck", action="test.concurrency", result="allowed", proc=proc, k=k)
PY
done
wait
echo "并发写入完成：$PROCS 个进程 × $PER 条"
docker compose exec -T backend python -m app.cli verify-audit
