#!/bin/bash

set -euo pipefail

SERVICE_NAME="bastion"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_AVAILABLE="/etc/nginx/sites-available/${SERVICE_NAME}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${SERVICE_NAME}"
INSTALL_DIR="/opt/bastion"
CONFIG_DIR="/etc/bastion"
WRAPPER_PATH="/usr/local/bin/bastion-ssh"

if [[ "${EUID}" -ne 0 ]]; then
    echo "请以 root 身份运行: sudo ./uninstall.sh" >&2
    exit 1
fi

if systemctl list-unit-files | grep -q "^${SERVICE_NAME}\.service"; then
    systemctl disable --now "${SERVICE_NAME}" || true
fi

rm -f "${SERVICE_PATH}"
systemctl daemon-reload

rm -f "${NGINX_ENABLED}" "${NGINX_AVAILABLE}"
nginx -t && systemctl reload nginx || true

rm -rf "${INSTALL_DIR}"
rm -f "${WRAPPER_PATH}"

read -r -p "是否删除 ${CONFIG_DIR} 配置目录? [y/N]: " REMOVE_CONFIG
if [[ "${REMOVE_CONFIG}" =~ ^[Yy]$ ]]; then
    rm -rf "${CONFIG_DIR}"
    echo "已删除配置目录: ${CONFIG_DIR}"
else
    echo "保留配置目录: ${CONFIG_DIR}"
fi

echo "卸载完成。tmux sessions 未清理。"
