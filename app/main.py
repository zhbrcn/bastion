"""Flask 主程序。"""

from __future__ import annotations

import logging
import shlex
from typing import Any

from flask import Flask, Response, jsonify, render_template, request

from auth_watcher import clear_auth_url, get_pending_auth_urls
from config_loader import load_overrides, load_settings, merge_node_info
from tailscale import get_nodes, get_self_hostname, get_self_ip
from tmux_manager import list_sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = Flask(__name__)


def _resolve_bind_address(settings: dict[str, Any]) -> str:
    """解析 Web 绑定地址显示值。"""
    bind_address = settings.get("web", {}).get("bind_address", "auto")
    if bind_address == "auto":
        return get_self_ip() or "127.0.0.1"
    return str(bind_address)


def _resolve_jumpbox_host(settings: dict[str, Any]) -> str:
    """解析跳板机访问地址。"""
    host = settings.get("jumpbox", {}).get("host", "auto")
    if host == "auto":
        return get_self_hostname() or get_self_ip() or "jumpbox"
    return str(host)


def _build_command(
    jumpbox_host: str,
    jumpbox_user: str,
    tmux_prefix: str,
    hostname: str,
    target_user: str,
) -> str:
    """生成前端展示的 SSH 命令。"""
    tmux_session = tmux_prefix + hostname
    remote_command = (
        "tmux new-session -A -s "
        f"{shlex.quote(tmux_session)} "
        f"bastion-ssh {shlex.quote(hostname)} {shlex.quote(target_user)}"
    )
    return (
        f"ssh -t {shlex.quote(jumpbox_user)}@{shlex.quote(jumpbox_host)} "
        f"{shlex.quote(remote_command)}"
    )


def _collect_servers() -> dict[str, Any]:
    """汇总节点、tmux、认证状态与面板配置。"""
    settings = load_settings()
    overrides = load_overrides()
    sessions = {session["name"] for session in list_sessions()}
    auth_urls = get_pending_auth_urls()
    jumpbox_host = _resolve_jumpbox_host(settings)
    jumpbox_user = settings.get("jumpbox", {}).get("ssh_user", "root")
    tmux_prefix = settings.get("defaults", {}).get("tmux_prefix", "")

    servers: list[dict[str, Any]] = []
    all_tags: set[str] = set()

    for node in get_nodes():
        info = merge_node_info(node, overrides, settings)
        if info["hidden"]:
            continue

        session_name = f"{tmux_prefix}{info['hostname']}"
        info["tmux_session_exists"] = session_name in sessions
        info["pending_auth_url"] = auth_urls.get(info["hostname"])
        info["command"] = _build_command(
            jumpbox_host=jumpbox_host,
            jumpbox_user=jumpbox_user,
            tmux_prefix=tmux_prefix,
            hostname=info["hostname"],
            target_user=info["user"],
        )
        servers.append(info)
        all_tags.update(info["tags"])

    servers.sort(key=lambda item: (not item["online"], item["hostname"]))

    return {
        "title": settings.get("ui", {}).get("title", "Bastion"),
        "subtitle": settings.get("ui", {}).get("subtitle", "跳板机管理面板"),
        "bind_address": _resolve_bind_address(settings),
        "port": settings.get("web", {}).get("port", 1234),
        "refresh_interval": settings.get("ui", {}).get("refresh_interval", 10),
        "jumpbox_host": jumpbox_host,
        "jumpbox_user": jumpbox_user,
        "tmux_prefix": tmux_prefix,
        "default_target_user": settings.get("defaults", {}).get("target_user", "root"),
        "servers": servers,
        "available_tags": sorted(all_tags),
        "pending_auth": auth_urls,
    }


@app.route("/")
def index() -> str:
    """渲染首页。"""
    settings = load_settings()
    return render_template(
        "index.html",
        title=settings.get("ui", {}).get("title", "Bastion"),
        subtitle=settings.get("ui", {}).get("subtitle", "跳板机管理面板"),
        refresh_interval=settings.get("ui", {}).get("refresh_interval", 10),
    )


@app.route("/api/servers")
def api_servers() -> Response:
    """返回节点与面板状态。"""
    return jsonify(_collect_servers())


@app.route("/api/ssh-config")
def api_ssh_config() -> Response:
    """导出可写入本地 ~/.ssh/config 的配置片段。"""
    data = _collect_servers()
    lines = [
        "# Bastion generated SSH config",
        "",
        "Host jumpbox",
        f"  HostName {data['jumpbox_host']}",
        f"  User {data['jumpbox_user']}",
        "",
    ]

    for server in data["servers"]:
        session_name = f"{data['tmux_prefix']}{server['hostname']}"
        lines.extend(
            [
                f"Host {server['hostname']}",
                f"  HostName {data['jumpbox_host']}",
                f"  User {data['jumpbox_user']}",
                (
                    "  RemoteCommand tmux new-session -A -s "
                    f"{shlex.quote(session_name)} "
                    f"bastion-ssh {shlex.quote(server['hostname'])} "
                    f"{shlex.quote(server['user'])}"
                ),
                "  RequestTTY yes",
                "",
            ]
        )

    text = "\n".join(lines)
    return Response(
        text,
        mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=bastion-ssh-config"},
    )


@app.route("/api/clear-auth", methods=["POST"])
def api_clear_auth() -> Response:
    """清理指定节点的待认证 URL。"""
    data = request.get_json(silent=True) or {}
    hostname = str(data.get("hostname", "")).strip()
    if not hostname:
        return jsonify({"ok": False, "error": "hostname required"}), 400

    return jsonify({"ok": clear_auth_url(hostname)})


@app.route("/healthz")
def healthz() -> tuple[str, int]:
    """健康检查。"""
    return "ok", 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
