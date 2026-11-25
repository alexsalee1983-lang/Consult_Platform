#!/usr/bin/env bash
# 四期 B25：试用机 / 部署机上的采集闭环定时同步（平台侧）。平台从不访问采集机，只经私有资料仓 Consult_Knowledge 交换。
#   1) 拉资料仓；2) kb-acquire-dispatch 出本周下发包，有任务就写 dispatch/<日期>-<主机>.json 提交推送（采集机每小时取）；
#   3) 资料仓 sources/returns/ 下新出现的回传批次（含 manifest.json）逐个 kb-acquire-import --auto-manifest 入库。
# 配置（环境变量或 scripts/kb_acquire_sync.env）：
#   COMPOSE_DIR   平台 compose 目录（默认本仓库根）
#   KB_REPO       资料仓在宿主机的路径（须就是 .env 里 KB_DOWNLOADS_DIR，或其上级 / 下级按 KB_CONTAINER_ROOT 对应）
#   KB_CONTAINER_ROOT  KB_REPO 在后端容器里的路径（默认 /srv/knowledge/downloads）
#   KB_IMPORT_AS  入库发起人（知识管理员账号，必填）
#   KB_SYNC_STATE 已导入批次记录（默认 $HOME/.kb_acquire_sync.done）
#   KB_SYNC_HOST  下发包文件名里的主机标识（默认 hostname -s）
# 建议 cron：17 * * * * bash <仓库>/scripts/kb_acquire_sync.sh >> $HOME/kb_acquire_sync.log 2>&1
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$ROOT/scripts/kb_acquire_sync.env" ] && . "$ROOT/scripts/kb_acquire_sync.env"
COMPOSE_DIR="${COMPOSE_DIR:-$ROOT}"
KB_REPO="${KB_REPO:?要设 KB_REPO（资料仓路径）}"
KB_CONTAINER_ROOT="${KB_CONTAINER_ROOT:-/srv/knowledge/downloads}"
KB_IMPORT_AS="${KB_IMPORT_AS:?要设 KB_IMPORT_AS（知识管理员账号）}"
STATE="${KB_SYNC_STATE:-$HOME/.kb_acquire_sync.done}"
HOST="${KB_SYNC_HOST:-$(hostname -s)}"
LOCK="${KB_SYNC_LOCK:-/tmp/kb_acquire_sync.lock}"
exec 9>"$LOCK"
flock -n 9 || { echo "$(date -Is) 上一轮还在跑，跳过"; exit 0; }
touch "$STATE"
cli() { (cd "$COMPOSE_DIR" && docker compose exec -T backend python -m app.cli "$@"); }
echo "$(date -Is) 同步开始"
git -C "$KB_REPO" pull -q --ff-only

# 下发：同一天同主机只写一份；内容与已有同名文件相同则不提交
cli kb-acquire-dispatch --out /tmp/kb_dispatch.json
PKG="$(cd "$COMPOSE_DIR" && docker compose exec -T backend cat /tmp/kb_dispatch.json)"
N="$(printf '%s' "$PKG" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("tasks", [])))')"
if [ "$N" -gt 0 ]; then
  mkdir -p "$KB_REPO/dispatch"
  F="dispatch/$(date +%Y%m%d)-$HOST.json"
  printf '%s\n' "$PKG" > "$KB_REPO/$F"
  if [ -n "$(git -C "$KB_REPO" status --porcelain -- "$F")" ]; then
    git -C "$KB_REPO" add "$F"
    git -C "$KB_REPO" commit -q -m "B25 下发包：$N 个任务（$HOST）"
    git -C "$KB_REPO" push -q
    echo "下发包已推送：$F，$N 个任务"
  fi
else
  echo "本周无直接采集任务"
fi

# 回传：新批次逐个导入；失败的不记已完成，下轮重试并在日志里留原因
shopt -s nullglob
for m in "$KB_REPO"/sources/returns/*/manifest.json; do
  id="$(basename "$(dirname "$m")")"
  grep -qxF "$id" "$STATE" && continue
  if cli kb-acquire-import "$KB_CONTAINER_ROOT/sources/returns/$id" --auto-manifest --as "$KB_IMPORT_AS"; then
    echo "$id" >> "$STATE"
    echo "回传批次 $id 已导入"
  else
    echo "回传批次 $id 导入失败，下轮重试"
  fi
done
echo "$(date -Is) 同步结束"
