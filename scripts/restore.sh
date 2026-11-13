#!/usr/bin/env bash
# 恢复（M5 ④，docs/备份与恢复.md）：用 scripts/backup.sh 的一个备份目录覆盖当前数据库和对象存储。
#   BACKUP_PASSPHRASE_FILE=/etc/consult-platform/backup.pass bash scripts/restore.sh backups/20270110T020000Z
# 会清空当前数据库和对象存储里的全部数据，执行前要输入备份目录名确认（非交互时设 RESTORE_CONFIRM=<目录名>）。
# 步骤：核对清单 sha256 → 解密到临时目录 → 停 backend / worker / frontend → 重建数据库并 pg_restore →
# 清空并还原 MinIO 数据卷 → 启动（后端自动升级迁移）→ verify-audit 核对备份时的链头仍在链上 → 记审计。
# 会话（Redis）不备份，恢复后所有人重新登录。.env（MINIO_KMS_SECRET_KEY、TOTP_KEYS 等）要和备份时一致。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SRC="${1:?用法：bash scripts/restore.sh <备份目录>}"
SRC="${SRC%/}"
PASS_FILE="${BACKUP_PASSPHRASE_FILE:-}"
stamp="$(basename "$SRC")"

die() { echo "恢复失败：$*" >&2; exit 2; }
[ -f "$SRC/manifest.json" ] && [ -f "$SRC/db.dump.gpg" ] || die "$SRC 不是完整的备份目录"
[ -n "$PASS_FILE" ] && [ -r "$PASS_FILE" ] || die "要设置 BACKUP_PASSPHRASE_FILE"
command -v gpg >/dev/null || die "没有 gpg"

manifest() {  # 按键路径取清单里的值，例如 manifest files db.dump.gpg sha256
  python3 -c 'import json,sys
v = json.load(open(sys.argv[1], encoding="utf-8"))
for k in sys.argv[2:]:
    v = v[k]
print("" if v is None else v)' "$SRC/manifest.json" "$@"
}
for f in "$SRC"/*.gpg; do
  n="$(basename "$f")"
  [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$(manifest files "$n" sha256)" ] || die "$n 的 sha256 与清单不一致"
done
head_hash="$(manifest audit_head hash)"
head_id="$(manifest audit_head id)"
echo "备份 $stamp：迁移 $(manifest alembic)，审计链头 id=$head_id；文件校验通过"

if [ "${RESTORE_CONFIRM:-}" != "$stamp" ]; then
  read -r -p "将清空当前数据库和对象存储，用备份 $stamp 覆盖。确认请输入目录名：" answer
  [ "$answer" = "$stamp" ] || die "未确认，已取消"
fi

umask 077
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
dec() { gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASS_FILE" -d "$1" > "$2"; }
dec "$SRC/db.dump.gpg" "$tmp/db.dump" || die "解密失败（口令不对或文件损坏）"
[ -f "$SRC/objects.tar.gpg" ] && { dec "$SRC/objects.tar.gpg" "$tmp/objects.tar" || die "对象存储备份解密失败"; }

started_at="$(date +%s)"
docker compose stop backend worker frontend >/dev/null

# 数据库：删库重建后还原（审计表的只追加触发器随备份一起还原）
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U consult -d postgres -qc 'DROP DATABASE IF EXISTS consult WITH (FORCE)'
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U consult -d postgres -qc 'CREATE DATABASE consult OWNER consult'
docker compose exec -T postgres pg_restore -U consult -d consult --exit-on-error < "$tmp/db.dump"

# 对象存储：停 MinIO，清空数据卷后解包
if [ -f "$tmp/objects.tar" ]; then
  mid="$(docker compose ps -aq minio)"
  [ -n "$mid" ] || die "备份里有对象存储，但当前没有 minio 容器"
  img="$(docker inspect -f '{{.Config.Image}}' "$mid")"
  docker compose stop minio >/dev/null
  docker run --rm -i --volumes-from "$mid" --user 0 --entrypoint sh "$img" \
    -c 'find /data -mindepth 1 -delete && tar -C /data -xpf -' < "$tmp/objects.tar"
  docker compose start minio >/dev/null
fi

docker compose up -d --wait backend worker frontend >/dev/null
docker compose exec -T backend python -m app.cli verify-audit --expect-head "$head_hash" \
  || die "恢复后审计链校验未通过（备份时的链头 $head_hash 不在链上）"
docker compose exec -T backend python -m app.cli backup-record --op restore --stamp "$stamp" \
  --head-id "${head_id:-0}" --bytes "$(du -sb "$SRC" | cut -f1)" >/dev/null
echo "恢复完成：备份 $stamp，用时 $(( $(date +%s) - started_at )) 秒。请运行 bash scripts/smoke_test.sh"
