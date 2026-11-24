#!/usr/bin/env bash
# 三期 B25 五步端到端（v3.0 验收第 12 条），可重复：在测试库上跑 backend/tests/test_b25_e2e.py，证据落目录。
#   bash scripts/b25_e2e.sh [证据目录]          # 默认 /tmp/b25-e2e-<时间>
#   TEST_DATABASE_URL=postgresql+psycopg://... bash scripts/b25_e2e.sh   # 与 CI 一致的 PostgreSQL
# 全程虚拟缺口、虚拟来源（example 域），模型用网关的模拟传输，不联网、不连真实模型。
# 采集机真机闸门复核（可选）：COLLECTOR_SSH=root@<采集机> COLLECTOR_KEY=~/.ssh/<管理密钥> 时，
# 把本次下发包和反例包传到采集机 /srv/collector/gate/inbox，以 collector 账号跑闸门，审计行追加到证据。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EV="${1:-/tmp/b25-e2e-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$EV"
cd "$ROOT/backend"
B25_E2E_EVIDENCE="$EV" python -m pytest -q tests/test_b25_e2e.py -p no:cacheprovider > "$EV/pytest.log" 2>&1 \
  || { tail -30 "$EV/pytest.log"; echo "B25 端到端：未通过（日志 $EV/pytest.log）"; exit 1; }
if [ -n "${COLLECTOR_SSH:-}" ]; then
  K=(-i "${COLLECTOR_KEY:?要设 COLLECTOR_KEY}" -o BatchMode=yes -q)
  scp "${K[@]}" "$EV/dispatch.json" "$EV/bad.json" "$COLLECTOR_SSH:/srv/collector/gate/inbox/"
  ssh "${K[@]}" "$COLLECTOR_SSH" 'chown collector:collector /srv/collector/gate/inbox/*.json; su -s /bin/bash collector -c "cd /srv/collector/gate && sha256sum collector_gate.py && { python3 collector_gate.py gate inbox/bad.json --whitelist whitelist.txt --audit audit.jsonl --out inbox/tasks_bad.json; echo bad_exit=\$?; } ; python3 collector_gate.py gate inbox/dispatch.json --whitelist whitelist.txt --audit audit.jsonl --out inbox/tasks.json; echo ok_exit=\$?; tail -2 audit.jsonl"' \
    > "$EV/collector_gate_remote.txt" 2>&1 || true
  grep -q "bad_exit=2" "$EV/collector_gate_remote.txt" && grep -q "ok_exit=0" "$EV/collector_gate_remote.txt" \
    || { cat "$EV/collector_gate_remote.txt"; echo "采集机真机闸门复核未通过"; exit 1; }
fi
tail -1 "$EV/pytest.log"
echo "B25 端到端：通过；证据 $EV（steps.json、dispatch.json、collector_audit.jsonl、manifest.json、publish.json）"
