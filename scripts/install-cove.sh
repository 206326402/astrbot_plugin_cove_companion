#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${COVE_INSTALL_ROOT:-${HOME}/.local/share/cove-companion}"
DATA_DIR="${COVE_DATA_DIR:-${HOME}/.local/share/cove-companion-data}"
PORT="${COVE_PORT:-8787}"
SERVICE_NAME="cove-companion.service"
fail(){ printf '安装失败：%s\n' "$*" >&2; exit 1; }
command -v node >/dev/null || fail '需要 Node.js 22.x。'
command -v npm >/dev/null || fail '需要 npm。'
command -v curl >/dev/null || fail '需要 curl。'
[[ "$(uname -s)" == Linux ]] || fail '首发正式支持 Linux。'
node -e 'if(Number(process.versions.node.split(".")[0])!==22) process.exit(1)' || fail "需要 Node.js 22.x，当前是 $(node --version)。"
[[ -f "$ROOT_DIR/service/package.json" ]] || fail '缺少 service/package.json。'
mkdir -p "$INSTALL_ROOT" "$DATA_DIR"; chmod 700 "$DATA_DIR"
rm -rf "$INSTALL_ROOT/src" "$INSTALL_ROOT/test" "$INSTALL_ROOT/package.json" "$INSTALL_ROOT/package-lock.json" "$INSTALL_ROOT/tsconfig.json"
cp -a "$ROOT_DIR/service/src" "$INSTALL_ROOT/src"
cp -a "$ROOT_DIR/service/test" "$INSTALL_ROOT/test" 2>/dev/null || true
cp "$ROOT_DIR/service/package.json" "$ROOT_DIR/service/package-lock.json" "$ROOT_DIR/service/tsconfig.json" "$INSTALL_ROOT/"
cd "$INSTALL_ROOT"; npm ci --omit=optional; npm run build
if [[ ! -f "$INSTALL_ROOT/.env" ]]; then
  cat > "$INSTALL_ROOT/.env" <<EOF
BIND_HOST=127.0.0.1
PORT=${PORT}
HOST_MODE=astrbot
ASTROBOT_COVE_DB=${DATA_DIR}/astrbot-cove.sqlite3
ASTROBOT_BRIDGE_TOKEN_FILE=${DATA_DIR}/cove-bridge-token
COVE_CREDENTIAL_FILE=${DATA_DIR}/private/credentials.enc
COVE_CREDENTIAL_KEY_FILE=${DATA_DIR}/keys/credentials.key
EOF
  chmod 600 "$INSTALL_ROOT/.env"
fi
# The service creates a random 256-bit token on first start. Do not pre-create an empty file.
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
if command -v systemctl >/dev/null && systemctl --user status >/dev/null 2>&1; then
  mkdir -p "$UNIT_DIR"
  sed -e "s#@COVE_INSTALL_ROOT@#${INSTALL_ROOT}#g" -e "s#@COVE_DATA_DIR@#${DATA_DIR}#g" "$ROOT_DIR/service/systemd/cove-companion.service" > "$UNIT_DIR/$SERVICE_NAME"
  systemctl --user daemon-reload; systemctl --user enable --now "$SERVICE_NAME"
else echo "未检测到用户级 systemd；可手动运行：cd $INSTALL_ROOT && npm start"; fi
echo "Cove 安装目录：$INSTALL_ROOT"; echo "数据目录：$DATA_DIR"; echo "AstrBot 插件的 Cove 令牌文件请填写：$DATA_DIR/cove-bridge-token"; echo '下一步：在 AstrBot 页面完成配置向导，检查通过后手动开启自动功能。'
