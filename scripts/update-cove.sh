#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${COVE_INSTALL_ROOT:-${HOME}/.local/share/cove-companion}"
SERVICE_NAME="cove-companion.service"
[[ -d "$INSTALL_ROOT" ]] || { echo '尚未安装 Cove。'; exit 1; }
node -e 'if(Number(process.versions.node.split(".")[0])!==22) process.exit(1)' || { echo '需要 Node.js 22.x。' >&2; exit 1; }
if command -v systemctl >/dev/null && systemctl --user status >/dev/null 2>&1; then systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true; fi
rm -rf "$INSTALL_ROOT/src" "$INSTALL_ROOT/test"; cp -a "$ROOT_DIR/service/src" "$INSTALL_ROOT/src"; cp -a "$ROOT_DIR/service/test" "$INSTALL_ROOT/test" 2>/dev/null || true
cp "$ROOT_DIR/service/package.json" "$ROOT_DIR/service/package-lock.json" "$ROOT_DIR/service/tsconfig.json" "$INSTALL_ROOT/"
cd "$INSTALL_ROOT"; npm ci --omit=optional; npm run build
if command -v systemctl >/dev/null && systemctl --user status >/dev/null 2>&1; then systemctl --user start "$SERVICE_NAME"; fi
echo 'Cove 已更新；令牌和数据目录未覆盖。'
