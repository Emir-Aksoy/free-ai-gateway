#!/usr/bin/env bash
# ai-gateway 安装脚本：幂等，可以在同一台机器上反复执行。
#
#   GATEWAY_DIR    安装目录，默认 /opt/ai-gateway
#                  非默认目录时密钥文件放在 $GATEWAY_DIR/env，不会碰 /etc 下正式实例的密钥
#   SERVICE_NAME   systemd 服务名，默认取目录名
#   GATEWAY_PORT   监听端口，默认 8090（只在首次创建服务 env 文件时写入）
#   GATEWAY_HOST   监听地址，默认 0.0.0.0（同上）
#
# 做的事：装依赖 → 建目录 → 同步代码 → 修正 config.yaml 的密钥路径 → 服务 env 文件 → unit → enable。
# 不启动服务：密钥由 `manage.py install run` 写入后再启动。

set -euo pipefail

GATEWAY_DIR="${GATEWAY_DIR:-/opt/ai-gateway}"
GATEWAY_DIR="${GATEWAY_DIR%/}"
# 服务名优先级：环境变量 > 上次安装写下的 .service-name > 目录名（否则自定义名的实例重跑会跳到另一个 unit）
SERVICE_NAME="${SERVICE_NAME:-$(cat "$GATEWAY_DIR/.service-name" 2>/dev/null || true)}"
SERVICE_NAME="${SERVICE_NAME:-$(basename "$GATEWAY_DIR")}"
case "$SERVICE_NAME" in
  ""|*[!A-Za-z0-9_.-]*) echo "SERVICE_NAME 不合法: $SERVICE_NAME" >&2; exit 1 ;;
esac
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
GATEWAY_HOST="${GATEWAY_HOST:-0.0.0.0}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$GATEWAY_DIR" = "/opt/ai-gateway" ]; then
  ENV_DIR="/etc"
else
  ENV_DIR="$GATEWAY_DIR/env"
fi

SERVICE_ENV="$ENV_DIR/$SERVICE_NAME.env"
UNIT="/etc/systemd/system/$SERVICE_NAME.service"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] 错误：%s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "需要 root 权限"
command -v systemctl >/dev/null 2>&1 || die "未找到 systemctl，本脚本只支持 systemd 系统"
command -v python3 >/dev/null 2>&1 || {
  command -v apt-get >/dev/null 2>&1 || die "没有 python3 也没有 apt-get"
}

# 1. 依赖：用发行版的 python3-requests / python3-yaml，避开 PEP 668 对 pip 的限制
if python3 -c 'import requests, yaml' >/dev/null 2>&1; then
  log "依赖已满足：python3 + requests + yaml"
else
  command -v apt-get >/dev/null 2>&1 || die "缺少 requests/yaml 且没有 apt-get，请手动安装 python3-requests python3-yaml"
  log "安装依赖：python3 python3-requests python3-yaml"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends python3 python3-requests python3-yaml
fi

# 2. 目录
mkdir -p "$GATEWAY_DIR/data" "$GATEWAY_DIR/logs" "$ENV_DIR"
chmod 700 "$GATEWAY_DIR/data" "$GATEWAY_DIR/logs"

# 3. 代码：脚本不在安装目录里时把代码同步过去（不动 data/ logs/ env/ 和已有的 config.yaml）
if [ "$SRC_DIR" != "$GATEWAY_DIR" ]; then
  log "同步代码 $SRC_DIR -> $GATEWAY_DIR"
  (cd "$SRC_DIR" && tar cf - \
      --exclude=./.git --exclude=./data --exclude=./logs --exclude=./env \
      --exclude=./desktop --exclude=./tests --exclude=./internal --exclude=./.claude \
      --exclude=./.public-release --exclude=./.private-backups --exclude=./release-artifacts --exclude=./.githooks \
      --exclude=__pycache__ --exclude=./config.yaml --exclude='*.bak.*' .) \
    | (cd "$GATEWAY_DIR" && tar xf -)
  [ -f "$GATEWAY_DIR/config.yaml" ] || cp "$SRC_DIR/config.yaml" "$GATEWAY_DIR/config.yaml"
fi

[ -f "$GATEWAY_DIR/server.py" ] || die "$GATEWAY_DIR 里没有 server.py"
[ -f "$GATEWAY_DIR/config.yaml" ] || die "$GATEWAY_DIR 里没有 config.yaml"

# 默认评分与运行数据分开打包；首次安装补入，升级绝不覆盖用户评分。
if [ ! -f "$GATEWAY_DIR/data/capability.json" ] && [ -f "$GATEWAY_DIR/defaults/capability.json" ]; then
  cp "$GATEWAY_DIR/defaults/capability.json" "$GATEWAY_DIR/data/capability.json"
  chmod 600 "$GATEWAY_DIR/data/capability.json"
  log "已写入默认模型评分"
fi


# 4. 非默认目录：config.yaml 里的密钥文件路径改到本实例的 env 目录
if [ "$ENV_DIR" != "/etc" ]; then
  ENV_DIR_SED="$(printf '%s' "$ENV_DIR" | sed 's/[&#\\]/\\&/g')"
  # 只改 providers: 段内的 env 行（范围到下一个顶层键为止）
  sed -i -E "/^providers:/,/^[^[:space:]#-]/ s#^([[:space:]]*env:[[:space:]]*)/etc/([A-Za-z0-9_.-]+\.env)[[:space:]]*\$#\1$ENV_DIR_SED/\2#" "$GATEWAY_DIR/config.yaml"
  log "config.yaml 的密钥路径已指向 $ENV_DIR"
fi

# 5. 服务 env 文件：不存在才创建；存在则只补缺失项，不改已有值
if [ ! -f "$SERVICE_ENV" ]; then
  printf 'GATEWAY_HOST=%s\nGATEWAY_PORT=%s\n' "$GATEWAY_HOST" "$GATEWAY_PORT" > "$SERVICE_ENV"
  log "创建 $SERVICE_ENV"
else
  grep -q '^GATEWAY_HOST=' "$SERVICE_ENV" || printf 'GATEWAY_HOST=%s\n' "$GATEWAY_HOST" >> "$SERVICE_ENV"
  grep -q '^GATEWAY_PORT=' "$SERVICE_ENV" || printf 'GATEWAY_PORT=%s\n' "$GATEWAY_PORT" >> "$SERVICE_ENV"
fi
chmod 644 "$SERVICE_ENV"

# 6. unit：内容不同才覆盖
PYTHON_BIN="$(command -v python3)"
UNIT_CONTENT="[Unit]
Description=AI Gateway ($SERVICE_NAME)
After=network.target

[Service]
Type=simple
WorkingDirectory=$GATEWAY_DIR
EnvironmentFile=$SERVICE_ENV
ExecStart=$PYTHON_BIN $GATEWAY_DIR/server.py
Restart=always
RestartSec=5
User=root
StandardOutput=append:$GATEWAY_DIR/logs/system.log
StandardError=append:$GATEWAY_DIR/logs/system-error.log

[Install]
WantedBy=multi-user.target"

if [ -f "$UNIT" ] && [ "$(cat "$UNIT")" = "$UNIT_CONTENT" ]; then
  log "unit 未变化：$UNIT"
else
  printf '%s\n' "$UNIT_CONTENT" > "$UNIT"
  systemctl daemon-reload
  log "写入 unit：$UNIT"
fi

systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

# 服务名与目录名不一致时记下来，manage.py 据此找 unit
if [ "$SERVICE_NAME" != "$(basename "$GATEWAY_DIR")" ]; then
  printf '%s\n' "$SERVICE_NAME" > "$GATEWAY_DIR/.service-name"
else
  rm -f "$GATEWAY_DIR/.service-name"
fi

# 7. 摘要
STATE="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"
log "完成：目录=$GATEWAY_DIR 服务=$SERVICE_NAME 端口=$GATEWAY_PORT 密钥目录=$ENV_DIR 服务状态=${STATE:-inactive}"
log "下一步：echo '{\"env\":{...}}' | python3 $GATEWAY_DIR/manage.py install run  写入密钥并启动"
