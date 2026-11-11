#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/tg115}"
cd "$INSTALL_DIR"

fail() {
  printf '[TG115][ERROR] %s\n' "$*" >&2
  exit 1
}

env_value() {
  local key="$1" default="${2:-}" value=""
  if [[ -f .env ]]; then
    value="$(
      awk -v key="$key" '
        { sub(/\r$/, "") }
        index($0, key "=") == 1 { sub(/^[^=]*=/, ""); print; exit }
      ' .env
    )"
  fi
  printf '%s' "${value:-$default}"
}

# 把界面/旧配置里的写法归一成 Bot 使用的两个内部取值：
# 115 与 clouddrive2 等价；google_drive 与 gdrive 等价。
normalize_destination() {
  local raw
  raw="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  case "$raw" in
    clouddrive2|115) printf 'clouddrive2' ;;
    gdrive|google_drive|google-drive) printf 'gdrive' ;;
    *) return 1 ;;
  esac
}

destination_label() {
  case "$1" in
    gdrive) printf 'Google Drive' ;;
    *) printf '115' ;;
  esac
}

# 两条路线共用同一个 CloudDrive2 WebDAV 根目录，只有相对路径不同。
cd2_target_path() {
  local encoded decoded
  encoded="$(env_value CD2_TARGET_PATH_B64)"
  [[ -n "$encoded" ]] || return 0
  decoded="$(printf '%s' "$encoded" | base64 -d 2>/dev/null)" || return 0
  printf '%s' "$decoded"
}

# 与部署脚本、Bot 使用同一套目标网盘取值，未知值直接拒绝执行。
DESTINATION_TYPE="$(normalize_destination "$(env_value DESTINATION_TYPE clouddrive2)")" \
  || fail "DESTINATION_TYPE 只能是 clouddrive2(115) 或 gdrive(google_drive)，当前值：$(env_value DESTINATION_TYPE)"
DEPLOY_CLOUDDRIVE2="$(env_value DEPLOY_CLOUDDRIVE2 true)"
case "$DEPLOY_CLOUDDRIVE2" in
  true|false) ;;
  *) fail "DEPLOY_CLOUDDRIVE2 只能是 true 或 false，当前值：$DEPLOY_CLOUDDRIVE2" ;;
esac

case "${1:-status}" in
  status)
    docker compose ps
    ;;
  destination)
    printf 'DESTINATION_TYPE=%s\n' "$DESTINATION_TYPE"
    printf '目标网盘：%s\n' "$(destination_label "$DESTINATION_TYPE")"
    if [[ -n "$(cd2_target_path)" ]]; then
      printf 'CloudDrive2 WebDAV 相对路径：%s\n' "$(cd2_target_path)"
    else
      printf 'CloudDrive2 WebDAV 相对路径：（空，直接写入 WebDAV 根目录）\n'
    fi
    printf 'rclone remote: cd2:（type=webdav，两条路线共用）\n'
    printf 'DEPLOY_CLOUDDRIVE2=%s\n' "$DEPLOY_CLOUDDRIVE2"
    ;;
  logs)
    docker compose logs --tail=200 -f tg115-bot
    ;;
  restart)
    docker compose restart tg115-bot
    ;;
  stop)
    docker compose stop tg115-bot
    ;;
  start)
    docker compose up -d tg115-bot
    ;;
  update)
    docker compose build tg115-bot
    docker compose up -d tg115-bot
    ;;
  verify)
    docker compose exec -T tg115-bot python -m app.verify_destination
    ;;
  *)
    echo "用法：$0 {status|destination|logs|restart|stop|start|update|verify}" >&2
    exit 2
    ;;
esac
