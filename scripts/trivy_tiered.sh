#!/usr/bin/env bash
# 镜像漏洞分层扫描（四期 G8，规则见 docs/安全扫描.md「分层规则」）。
# 用法：TRIVY=./trivy EVENT_NAME=... REF=... BASE_SHA=... bash scripts/trivy_tiered.sh
#   push 到 main、打 tag、schedule：整套镜像严格阻断；
#   pull_request：只对本 PR 改动过的镜像阻断，未改动的镜像照扫但只告警；
#   其他分支的推送：只告警（合并闸门是该分支 PR 上的那次运行）。
# 报告写到 $REPORT（默认 /tmp/trivy_report.md），并追加到 $GITHUB_STEP_SUMMARY；有阻断项时退出码为 1。
set -uo pipefail
TRIVY="${TRIVY:-./trivy}"
EVENT_NAME="${EVENT_NAME:-push}"
REF="${REF:-}"
BASE_SHA="${BASE_SHA:-}"
REPORT="${REPORT:-/tmp/trivy_report.md}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
PROFILE_ARGS=(--profile monitoring)   # 模拟模型只用于 CI 和测试机，不扫

mode=tiered
case "$EVENT_NAME" in
  schedule) mode=strict ;;
  pull_request) mode=tiered ;;
  push) case "$REF" in refs/heads/main|refs/tags/v*) mode=strict ;; *) mode=warn ;; esac ;;
esac

# 本 PR 改动的文件；取不到基线时按“全部改动”处理（保守，不放松阻断）
changed_files=""
all_changed=0
if [ "$mode" = tiered ]; then
  if [ -n "$BASE_SHA" ] && { git cat-file -e "$BASE_SHA^{commit}" 2>/dev/null || git fetch -q --depth=1 origin "$BASE_SHA"; } \
     && changed_files=$(git diff --name-only "$BASE_SHA" HEAD); then
    compose_added=$(git diff -U0 "$BASE_SHA" HEAD -- docker-compose.yml | grep '^+' | grep -v '^+++' || true)
  else
    echo "取不到基线提交，所有镜像按已改动处理"; all_changed=1; compose_added=""
  fi
fi

# 镜像 -> 构建上下文（相对路径；非自建镜像为空）
mapfile -t rows < <(docker compose "${PROFILE_ARGS[@]}" config --format json \
  | jq -r --arg pwd "$PWD" '.services[] | [.image, ((.build.context // "") | sub("^" + $pwd + "/?"; ""))] | @tsv' | sort -u)

is_changed() {  # $1 镜像 $2 构建上下文
  [ "$mode" = strict ] && return 0
  [ "$all_changed" = 1 ] && return 0
  # 基础镜像或镜像标签在 docker-compose.yml 里被改动
  if [ -n "$compose_added" ] && printf '%s\n' "$compose_added" | grep -qF "$1"; then return 0; fi
  [ -n "$2" ] || return 1
  local ctx="$2"
  if [ "$ctx" = "" ] || [ "$ctx" = "." ]; then   # 以仓库根为上下文的镜像（backend、worker）：Dockerfile、源码、agents/、.dockerignore
    printf '%s\n' "$changed_files" | grep -qE '^(backend/|agents/|\.dockerignore$)' && return 0
    return 1
  fi
  ctx="${ctx#./}"
  printf '%s\n' "$changed_files" | grep -qE "^${ctx%/}/" && return 0
  return 1
}

fail=0
{
  echo "## 镜像漏洞扫描（模式：$mode）"
  echo
} > "$REPORT"
for row in "${rows[@]}"; do
  img="${row%%$'\t'*}"; ctx="${row#*$'\t'}"
  [ "$ctx" = "$row" ] && ctx=""
  out=$(mktemp)
  echo "== $img"
  if is_changed "$img" "$ctx"; then blocking=1; else blocking=0; fi
  [ "$mode" = warn ] && blocking=0
  if "$TRIVY" image --quiet --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
       --ignorefile .trivyignore.yaml --exit-code 1 "$img" > "$out" 2>&1; then
    echo "- 通过：\`$img\`" >> "$REPORT"
  else
    cat "$out"
    if [ "$blocking" = 1 ]; then
      fail=1
      echo "- **阻断**：\`$img\`（本次改动涉及该镜像，或严格模式）" >> "$REPORT"
    else
      echo "- 告警（不阻断）：\`$img\`，未改动的镜像，由每日定时扫描开 issue 跟进" >> "$REPORT"
    fi
    { echo; echo '```'; tail -n 60 "$out"; echo '```'; echo; } >> "$REPORT"
  fi
  rm -f "$out"
done
cat "$REPORT" >> "$SUMMARY"
echo "$fail" > "${REPORT%.md}.fail"
exit $fail
