#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/bastion"
CONFIG_DIR="/etc/bastion"
SERVICE_NAME="bastion"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_AVAILABLE="/etc/nginx/sites-available/${SERVICE_NAME}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${SERVICE_NAME}"
WRAPPER_PATH="/usr/local/bin/bastion-ssh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() {
    printf "${GREEN}[INFO]${NC} %s\n" "$1"
}

log_warn() {
    printf "${YELLOW}[WARN]${NC} %s\n" "$1"
}

log_error() {
    printf "${RED}[ERROR]${NC} %s\n" "$1" >&2
}

on_error() {
    local line_number="$1"
    log_error "安装失败，出错行号: ${line_number}"
    exit 1
}

trap 'on_error "${LINENO}"' ERR

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        log_error "请以 root 身份运行: sudo ./install.sh"
        exit 1
    fi
}

check_system() {
    if [[ ! -f /etc/os-release ]]; then
        log_error "无法识别系统类型"
        exit 1
    fi

    # shellcheck disable=SC1091
    source /etc/os-release
    case "${ID:-}" in
        debian|ubuntu)
            ;;
        *)
            log_error "仅支持 Debian / Ubuntu，当前系统: ${ID:-unknown}"
            exit 1
            ;;
    esac

    if ! command -v tailscale >/dev/null 2>&1; then
        log_error "未检测到 tailscale，请先安装并完成登录"
        exit 1
    fi
}

install_packages() {
    log_info "安装系统依赖"
    apt-get update
    apt-get install -y tmux nginx python3 python3-pip python3-venv
}

install_app() {
    log_info "同步项目文件到 ${INSTALL_DIR}"
    mkdir -p "${INSTALL_DIR}"
    rm -rf "${INSTALL_DIR}/app"
    cp -R "${PROJECT_ROOT}/app" "${INSTALL_DIR}/app"

    if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
        log_info "创建 Python 虚拟环境"
        python3 -m venv "${INSTALL_DIR}/venv"
    fi

    log_info "安装 Python 依赖"
    "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
    "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/app/requirements.txt"
}

install_config() {
    log_info "初始化配置目录 ${CONFIG_DIR}"
    mkdir -p "${CONFIG_DIR}"

    if [[ ! -f "${CONFIG_DIR}/settings.yaml" ]]; then
        cp "${PROJECT_ROOT}/config/settings.yaml.example" "${CONFIG_DIR}/settings.yaml"
    else
        log_warn "已存在 ${CONFIG_DIR}/settings.yaml，跳过覆盖"
    fi

    if [[ ! -f "${CONFIG_DIR}/overrides.yaml" ]]; then
        cp "${PROJECT_ROOT}/config/overrides.yaml.example" "${CONFIG_DIR}/overrides.yaml"
    else
        log_warn "已存在 ${CONFIG_DIR}/overrides.yaml，跳过覆盖"
    fi
}

install_wrapper() {
    log_info "安装 bastion-ssh wrapper"
    install -m 0755 "${PROJECT_ROOT}/scripts/bastion-ssh" "${WRAPPER_PATH}"
}

install_systemd() {
    log_info "安装 systemd 服务"
    install -m 0644 "${PROJECT_ROOT}/deploy/bastion.service" "${SERVICE_PATH}"
    systemctl daemon-reload
    systemctl enable --now "${SERVICE_NAME}"
}

install_nginx() {
    local tailscale_ip
    tailscale_ip="$(tailscale ip -4 | head -n1 | tr -d '[:space:]')"

    if [[ -z "${tailscale_ip}" ]]; then
        log_error "无法获取 Tailscale IPv4 地址"
        exit 1
    fi

    log_info "写入 Nginx 配置，监听 ${tailscale_ip}:1234"
    sed "s/{{TAILSCALE_IP}}/${tailscale_ip}/g" \
        "${PROJECT_ROOT}/deploy/nginx.conf" > "${NGINX_AVAILABLE}"

    ln -sfn "${NGINX_AVAILABLE}" "${NGINX_ENABLED}"

    nginx -t
    systemctl reload nginx
}

show_summary() {
    local tailscale_ip
    tailscale_ip="$(tailscale ip -4 | head -n1 | tr -d '[:space:]')"

    printf "\n${BLUE}Bastion 安装完成${NC}\n"
    printf "安装目录: %s\n" "${INSTALL_DIR}"
    printf "配置目录: %s\n" "${CONFIG_DIR}"
    printf "访问地址: http://%s:1234\n" "${tailscale_ip}"
    printf "服务状态: systemctl status %s\n" "${SERVICE_NAME}"
    printf "查看日志: journalctl -u %s -f\n" "${SERVICE_NAME}"
    printf "Nginx 测试: nginx -t\n"
}

main() {
    require_root
    check_system
    install_packages
    install_app
    install_config
    install_wrapper
    install_systemd
    install_nginx
    show_summary
}

main "$@"
