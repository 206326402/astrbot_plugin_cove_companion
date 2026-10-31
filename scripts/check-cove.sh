#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
INSTALL_ROOT="${COVE_INSTALL_ROOT:-${HOME}/.local/share/cove-companion}"
DATA_DIR="${COVE_DATA_DIR:-${HOME}/.local/share/cove-companion-data}"
URL="${COVE_HEALTH_URL:-http://127.0.0.1:8787/astrbot/v1/health/ready}"
[[ -d "$INSTALL_ROOT" ]] || { echo 'Cove 尚未安装。'; exit 1; }
echo "安装目录：$INSTALL_ROOT"; echo "数据目录：$DATA_DIR"
if [[ -s "$DATA_DIR/cove-bridge-token" ]]; then echo '令牌文件：存在（内容已隐藏）'; elif [[ -f "$DATA_DIR/cove-bridge-token" ]]; then echo '令牌文件：存在但为空'; else echo '令牌文件：缺失'; fi
TOKEN=''; [[ -f "$DATA_DIR/cove-bridge-token" ]] && TOKEN="$(cat "$DATA_DIR/cove-bridge-token")"
if [[ -n "$TOKEN" ]] && curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" "$URL" >/dev/null 2>&1; then echo 'Cove ready：通过'; else echo "Cove ready：未通过（$URL）"; exit 2; fi
