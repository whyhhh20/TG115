#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SOURCE_DIR=""
CONFIG_FILE=""
CHECK_CONFIG_ONLY="false"
if [[ "${1:-}" == "--check-config" ]]; then
  # 只校验配置文件，不需要 root、Docker 或网络；供部署前预检与自动化测试使用。
  CHECK_CONFIG_ONLY="true"
  shift
  CONFIG_FILE="${1:-}"
else
  SOURCE_DIR="${1:-}"
  CONFIG_FILE="${2:-}"
fi
INSTALL_DIR="${INSTALL_DIR:-/opt/tg115}"
# 输出会显示在部署器的纯文本日志窗口里：让 compose/BuildKit 输出不带颜色和光标控制的纯文本进度。
export COMPOSE_ANSI=never COMPOSE_PROGRESS=plain BUILDKIT_PROGRESS=plain

log() {
  printf '[TG115] %s\n' "$*"
}

fail() {
  trap - ERR
  printf '[TG115][ERROR] %s\n' "$*" >&2
  printf 'TG115_RESULT=FAILED\n' >&2
  exit 1
}

# 只读取 KEY=VALUE，不 source 未校验的配置文件。
config_value() {
  local key="$1" default="${2:-}" value
  [[ -f "$CONFIG_FILE" ]] || return 0
  value="$(
    awk -v key="$key" '
      { sub(/\r$/, "") }
      index($0, key "=") == 1 { sub(/^[^=]*=/, ""); print; exit }
    ' "$CONFIG_FILE"
  )"
  if [[ -z "$value" ]]; then
    printf '%s' "$default"
  else
    printf '%s' "$value"
  fi
}

require_b64() {
  local name="$1" value="$2"
  [[ -n "$value" ]] || fail "缺少必填配置：$name"
  printf '%s' "$value" | base64 -d >/dev/null 2>&1 \
    || fail "配置 $name 不是有效的 Base64 数据"
}

optional_b64() {
  local name="$1" value="$2"
  if [[ -z "$value" ]]; then
    return 0
  fi
  printf '%s' "$value" | base64 -d >/dev/null 2>&1 \
    || fail "配置 $name 不是有效的 Base64 数据"
}

# 把界面/旧配置里的写法归一成 Bot 使用的两个内部取值。
# 115 与 clouddrive2 等价（115 走 CloudDrive2）；google_drive 与 gdrive 等价。
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

# 独立验证目标网盘，避免脚本与 Bot 对 DESTINATION_TYPE 的理解不一致。
# 两条路线都经过 CloudDrive2：都必须有 CD2 WebDAV 地址、用户名和密码，
# 都不需要 Google OAuth token / client ID / secret。
validate_destination() {
  local raw deploy_cd2
  raw="$(config_value DESTINATION_TYPE clouddrive2)"
  DESTINATION_TYPE="$(normalize_destination "$raw")" || fail "DESTINATION_TYPE 只能是 clouddrive2(115) 或 gdrive(google_drive)，当前值：$raw"

  require_b64 CD2_WEBDAV_URL_B64 "$(config_value CD2_WEBDAV_URL_B64)"
  require_b64 CD2_WEBDAV_USERNAME_B64 "$(config_value CD2_WEBDAV_USERNAME_B64)"
  require_b64 CD2_WEBDAV_PASSWORD_B64 "$(config_value CD2_WEBDAV_PASSWORD_B64)"
  # 空路径表示用户已把 CD2 WebDAV 根目录设到目标网盘，不能自动猜测挂载名。
  optional_b64 CD2_TARGET_PATH_B64 "$(config_value CD2_TARGET_PATH_B64)"

  deploy_cd2="$(config_value DEPLOY_CLOUDDRIVE2 true)"
  case "$deploy_cd2" in
    true|false) ;;
    *) fail "DEPLOY_CLOUDDRIVE2 只能是 true 或 false，当前值：$deploy_cd2" ;;
  esac
  DEPLOY_CLOUDDRIVE2="$deploy_cd2"

  TRANSFER_MODE="$(config_value TRANSFER_MODE auto)"
  case "$TRANSFER_MODE" in
    auto|stream) ;;
    *) fail "TRANSFER_MODE 只能是 auto 或 stream，当前值：$TRANSFER_MODE" ;;
  esac
}

[[ -f "$CONFIG_FILE" ]] || fail "找不到配置文件：$CONFIG_FILE"
validate_destination

if [[ "$CHECK_CONFIG_ONLY" == "true" ]]; then
  printf 'TG115_CONFIG=OK\n'
  printf 'TG115_DESTINATION_TYPE=%s\n' "$DESTINATION_TYPE"
  printf 'TG115_DESTINATION_LABEL=%s\n' "$(destination_label "$DESTINATION_TYPE")"
  printf 'TG115_DEPLOY_CLOUDDRIVE2=%s\n' "$DEPLOY_CLOUDDRIVE2"
  printf 'TG115_TRANSFER_MODE=%s\n' "$TRANSFER_MODE"
  exit 0
fi

trap 'fail "安装在第 ${LINENO} 行失败。请保留完整日志。"' ERR

[[ "$(id -u)" -eq 0 ]] || fail "remote_install.sh 必须以 root 或 sudo 运行"
[[ -d "$SOURCE_DIR" ]] || fail "找不到部署源目录：$SOURCE_DIR"
[[ "$INSTALL_DIR" =~ ^/opt/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]] \
  || fail "安装目录必须是 /opt/ 下的安全绝对路径"
[[ "/$INSTALL_DIR/" != *"/../"* && "/$INSTALL_DIR/" != *"/./"* ]] \
  || fail "安装目录不能包含 . 或 .. 路径段"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
else
  fail "无法识别 Linux 系统"
fi
case "${ID:-}" in
  ubuntu|debian) ;;
  *) fail "目前只自动支持 Ubuntu 或 Debian，当前系统：${ID:-unknown}" ;;
esac
[[ -n "${VERSION_CODENAME:-}" ]] || fail "系统缺少 VERSION_CODENAME，无法配置软件源"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64|aarch64|arm64) ;;
  *) fail "当前 CPU 架构暂不支持：$ARCH" ;;
esac

# 资源数值只作参考日志：VPS 规格由用户自行选择，安装器不用固定内存/磁盘容量
# 拦截安装。探测失败（精简系统缺少 /proc/meminfo、df 不可用）同样不能变成新的
# 硬门槛，因此全部容错并允许显示“未知”。
TOTAL_MEM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || true)"
ROOT_TOTAL_KB="$(df -Pk / 2>/dev/null | awk 'NR==2 {print $2}' || true)"
ROOT_FREE_KB="$(df -Pk / 2>/dev/null | awk 'NR==2 {print $4}' || true)"
log "系统资源参考（仅记录，不作为安装门槛）：内存约 ${TOTAL_MEM_MB:-未知}MB，系统盘总容量 ${ROOT_TOTAL_KB:-未知}KB，可用 ${ROOT_FREE_KB:-未知}KB"

export DEBIAN_FRONTEND=noninteractive
log "安装基础软件"
apt-get update -y
APT_PACKAGES=(ca-certificates curl gnupg tar gzip openssh-client)
if [[ "$DEPLOY_CLOUDDRIVE2" == "true" ]]; then
  # FUSE 只用于由本脚本托管的 CloudDrive2 挂载；复用已有 CD2 时不需要。
  APT_PACKAGES+=(fuse3 kmod)
fi
apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"

configure_docker_repository() {
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  printf '%s\n' \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
}

if ! command -v docker >/dev/null 2>&1; then
  log "通过 Docker 官方 APT 仓库安装 Docker"
  configure_docker_repository
  apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
    docker-compose-plugin
fi
systemctl enable --now docker

if ! docker compose version >/dev/null 2>&1; then
  log "Docker Compose 插件不存在，尝试从 Docker 官方 APT 仓库安装"
  configure_docker_repository
  apt-get install -y docker-compose-plugin
fi
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 不可用"

log "准备持久化目录"
PERSIST_DIRS=(
  "$INSTALL_DIR"
  "$INSTALL_DIR/data"
  "$INSTALL_DIR/downloads"
  "$INSTALL_DIR/logs"
  "$INSTALL_DIR/config/rclone"
)
if [[ "$DEPLOY_CLOUDDRIVE2" == "true" ]]; then
  # 只在本机托管 CloudDrive2 时创建挂载目录；复用已有 CD2 时不碰它的数据。
  PERSIST_DIRS+=(
    "$INSTALL_DIR/clouddrive/config"
    "$INSTALL_DIR/clouddrive/mounts"
  )
fi
mkdir -p "${PERSIST_DIRS[@]}"
install -d -m 700 /opt/tg115-backups

if [[ -f "$INSTALL_DIR/docker-compose.yml" ]]; then
  BACKUP="/opt/tg115-backups/config-$(date +%Y%m%d-%H%M%S).tar.gz"
  log "备份现有程序配置到 $BACKUP"
  tar -czf "$BACKUP" -C "$INSTALL_DIR" \
    --exclude='./data' \
    --exclude='./downloads' \
    --exclude='./logs' \
    --exclude='./clouddrive' \
    . || true
fi

log "安装或更新程序文件"
rm -rf -- "$INSTALL_DIR/app"
rm -f -- \
  "$INSTALL_DIR/.dockerignore" \
  "$INSTALL_DIR/Dockerfile" \
  "$INSTALL_DIR/docker-compose.yml" \
  "$INSTALL_DIR/manage.sh" \
  "$INSTALL_DIR/remote_install.sh" \
  "$INSTALL_DIR/repair_clouddrive_network.sh" \
  "$INSTALL_DIR/requirements.txt"
cp -a "$SOURCE_DIR/." "$INSTALL_DIR/"
install -m 600 "$CONFIG_FILE" "$INSTALL_DIR/.env"
# Accept configuration produced by older Windows deployers as well.
sed -i 's/\r$//' "$INSTALL_DIR/.env"

set_env_value() {
  local key="$1" value="$2" file="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

# 把本次实际生效的目标写入 .env，供 Bot、manage.sh 和修复脚本读取。
set_env_value DESTINATION_TYPE "$DESTINATION_TYPE" "$INSTALL_DIR/.env"
set_env_value DEPLOY_CLOUDDRIVE2 "$DEPLOY_CLOUDDRIVE2" "$INSTALL_DIR/.env"

CHMOD_DIRS=(
  "$INSTALL_DIR"
  "$INSTALL_DIR/data"
  "$INSTALL_DIR/downloads"
  "$INSTALL_DIR/logs"
  "$INSTALL_DIR/config"
  /opt/tg115-backups
)
if [[ "$DEPLOY_CLOUDDRIVE2" == "true" ]]; then
  CHMOD_DIRS+=(
    "$INSTALL_DIR/clouddrive"
    "$INSTALL_DIR/clouddrive/config"
    "$INSTALL_DIR/clouddrive/mounts"
  )
fi
chmod 700 "${CHMOD_DIRS[@]}"
chown -R 10001:10001 \
  "$INSTALL_DIR/data" \
  "$INSTALL_DIR/downloads" \
  "$INSTALL_DIR/logs" \
  "$INSTALL_DIR/config"
chmod +x \
  "$INSTALL_DIR/remote_install.sh" \
  "$INSTALL_DIR/manage.sh" \
  "$INSTALL_DIR/repair_clouddrive_network.sh"

cd "$INSTALL_DIR"
docker compose config --quiet

# shellcheck disable=SC1091
source "$INSTALL_DIR/.env"
DESTINATION_TYPE="${DESTINATION_TYPE:-clouddrive2}"
DEPLOY_CLOUDDRIVE2="${DEPLOY_CLOUDDRIVE2:-true}"
DESTINATION_TYPE="${DESTINATION_TYPE%$'\r'}"
DEPLOY_CLOUDDRIVE2="${DEPLOY_CLOUDDRIVE2%$'\r'}"
DESTINATION_TYPE="$(normalize_destination "$DESTINATION_TYPE")" \
  || fail "DESTINATION_TYPE 只能是 clouddrive2(115) 或 gdrive(google_drive)"
case "$DEPLOY_CLOUDDRIVE2" in
  true|false) ;;
  *) fail "DEPLOY_CLOUDDRIVE2 只能是 true 或 false，当前值：$DEPLOY_CLOUDDRIVE2" ;;
esac
DESTINATION_LABEL="$(destination_label "$DESTINATION_TYPE")"
log "目标网盘：$DESTINATION_LABEL（经 CloudDrive2 WebDAV 传输）"
if [[ "$DEPLOY_CLOUDDRIVE2" == "true" ]]; then
  modprobe fuse 2>/dev/null || true
  [[ -c /dev/fuse ]] || fail "VPS 没有提供 /dev/fuse；请让服务商开启 FUSE 后重试"
  mapfile -t CD2_MATCHES < <(
    docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}' \
      | awk -F'|' '
          tolower($1) ~ /clouddrive2/ ||
          tolower($2) ~ /(^|\/)clouddrive2([:@]|$)/ ||
          tolower($2) ~ /cloudnas\/clouddrive2([:@]|$)/ {
            print $1
          }
        '
  )
  [[ "${#CD2_MATCHES[@]}" -le 1 ]] \
    || fail "发现多个正在运行的 CloudDrive2 容器，请只保留目标容器后重试"
  EXISTING_CD2="${CD2_MATCHES[0]:-}"
  PORT_19798_CONTAINER="$(
    docker ps --format '{{.Names}}|{{.Ports}}' \
      | awk -F'|' '$2 ~ /:19798->/ {print $1; exit}'
  )"
  if [[ -z "$EXISTING_CD2" && -n "$PORT_19798_CONTAINER" ]]; then
    fail "容器 $PORT_19798_CONTAINER 占用 19798 端口，但无法确认它是 CloudDrive2；拒绝自动修改"
  fi
  if [[ -n "$EXISTING_CD2" ]]; then
    log "发现现有 CloudDrive2 容器 $EXISTING_CD2；保留登录和挂载数据"
  else
    log "拉取并启动 CloudDrive2"
    docker compose --profile clouddrive2 pull clouddrive2
    docker compose --profile clouddrive2 up -d clouddrive2
  fi
fi

log "构建 Telegram → $DESTINATION_LABEL Bot 容器（经 CloudDrive2 WebDAV）"
docker compose build tg115-bot
docker compose up -d tg115-bot

# 只看一次 running 不够：配置错误（例如 Bot Token 错）时容器会启动后立刻退出，
# 再被 restart: unless-stopped 拉起，刚 up 完的那一刻仍可能显示 running。
# 因此在约 15 秒的观察窗口内反复确认：状态必须一直是 running，且重启次数不增加。
BOT_STABLE_CHECKS=5
BOT_STABLE_INTERVAL=3
bot_state() {
  docker inspect --format '{{.State.Status}} {{.RestartCount}}' tg115-bot 2>/dev/null || true
}

# require_stable_bot <阶段说明>：对“当前这个” tg115-bot 容器做稳定性观察，失败直接 fail。
require_stable_bot() {
  local phase="$1" check
  log "确认 Bot 容器已稳定运行（${phase}，观察约 $((BOT_STABLE_CHECKS * BOT_STABLE_INTERVAL)) 秒）"
  BOT_STATUS=""
  BOT_RESTARTS=""
  BOT_RESTARTS_START=""
  read -r BOT_STATUS BOT_RESTARTS_START _ <<<"$(bot_state)" || true
  BOT_FAILURE=""
  for ((check = 1; check <= BOT_STABLE_CHECKS; check++)); do
    read -r BOT_STATUS BOT_RESTARTS _ <<<"$(bot_state)" || true
    if [[ "$BOT_STATUS" != "running" ]]; then
      BOT_FAILURE="Bot 容器未启动（${phase}，当前状态：${BOT_STATUS:-unknown}）"
      break
    fi
    if [[ "$BOT_RESTARTS_START" =~ ^[0-9]+$ && "$BOT_RESTARTS" =~ ^[0-9]+$ ]] \
      && (( BOT_RESTARTS > BOT_RESTARTS_START )); then
      BOT_FAILURE="Bot 容器启动后反复重启（${phase}，重启次数 ${BOT_RESTARTS_START} → ${BOT_RESTARTS}）"
      break
    fi
    if (( check < BOT_STABLE_CHECKS )); then
      sleep "$BOT_STABLE_INTERVAL"
    fi
  done

  if [[ -n "$BOT_FAILURE" ]]; then
    log "Bot 当前状态：${BOT_STATUS:-unknown}，重启次数：${BOT_RESTARTS:-unknown}"
    docker compose ps || true
    docker compose logs --tail=120 tg115-bot || true
    fail "${BOT_FAILURE}，请根据上面的日志修正启动错误"
  fi
}

require_stable_bot "首次启动"

if [[ "${DEPLOY_CLOUDDRIVE2:-true}" == "true" ]]; then
  log "迁移或修复 CloudDrive2 Docker 网络并执行硬性连通检查"
  export INSTALL_DIR
  bash "$INSTALL_DIR/repair_clouddrive_network.sh" --network-only
  # 修复脚本可能用 --force-recreate 换掉了 Bot 容器（例如切到 host 网络），
  # 成功标记必须基于最终这个容器，所以对它再观察一次。
  require_stable_bot "网络修复后"
fi

log "部署完成"
docker compose ps
printf 'TG115_INSTALL_DIR=%s\n' "$INSTALL_DIR"
printf 'TG115_DESTINATION_TYPE=%s\n' "$DESTINATION_TYPE"
printf 'TG115_DESTINATION_LABEL=%s\n' "$DESTINATION_LABEL"
printf 'TG115_DEPLOY_CLOUDDRIVE2=%s\n' "$DEPLOY_CLOUDDRIVE2"
printf 'TG115_TRANSFER_MODE=%s\n' "${TRANSFER_MODE:-auto}"
printf 'TG115_BOT_STATUS=%s\n' "$BOT_STATUS"
printf 'TG115_RESULT=SUCCESS\n'
