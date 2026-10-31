#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
INSTALL_ROOT="${COVE_INSTALL_ROOT:-${HOME}/.local/share/cove-companion}"
DATA_DIR="${COVE_DATA_DIR:-${HOME}/.local/share/cove-companion-data}"
SERVICE_NAME="cove-companion.service"
PURGE=false; [[ "${1:-}" == --purge ]] && PURGE=true
[[ -n "$INSTALL_ROOT" && "$INSTALL_ROOT" != "/" && "$INSTALL_ROOT" != "$HOME" ]] || { echo '拒绝删除不安全的安装路径。' >&2; exit 1; }
if command -v systemctl >/dev/null && systemctl --user status >/dev/null 2>&1; then systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true; rm -f "${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user/$SERVICE_NAME"; systemctl --user daemon-reload || true; fi
rm -rf "$INSTALL_ROOT"
if $PURGE; then read -r -p "输入 DELETE DATA 才会删除 $DATA_DIR：" confirm; [[ "$confirm" == 'DELETE DATA' ]] || { echo '未删除数据。'; exit 0; }; [[ -n "$DATA_DIR" && "$DATA_DIR" != "/" && "$DATA_DIR" != "$HOME" ]] || { echo '拒绝删除不安全的数据路径。' >&2; exit 1; }; rm -rf "$DATA_DIR"; echo '代码与数据均已删除。'; else echo "代码已卸载；数据保留在 $DATA_DIR。"; fi
