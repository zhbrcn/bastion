"""Flask entrypoint for Bastion."""

from __future__ import annotations

import logging
import shlex
import socket
from typing import Any

from flask import Flask, Response, jsonify, render_template, request

from auth_watcher import clear_auth_url, get_pending_auth_urls
from config_loader import (
    load_manual_servers,
    load_overrides,
    load_settings,
    merge_node_info,
    save_manual_servers,
    save_settings,
)
from tailscale import get_nodes, get_self_hostname, get_self_ip
from terminal_manager import TerminalManager
from tmux_manager import list_sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = Flask(__name__)
terminals = TerminalManager()


def _resolve_bind_address(settings: dict[str, Any]) -> str:
    """Resolve the address exposed in the UI."""
    bind_address = settings.get("web", {}).get("bind_address", "auto")
    if bind_address == "auto":
        return get_self_ip() or "127.0.0.1"
    return str(bind_address)


def _resolve_jumpbox_host(settings: dict[str, Any]) -> str:
    """Resolve the jumpbox host used in generated client commands."""
    host = settings.get("jumpbox", {}).get("host", "auto")
    if host == "auto":
        return get_self_hostname() or get_self_ip() or "jumpbox"
    return str(host)


def _probe_host(host: str, port: int) -> bool | None:
    """Best-effort TCP reachability probe."""
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def _build_jumpbox_command(
    jumpbox_host: str,
    jumpbox_user: str,
    tmux_prefix: str,
    hostname: str,
    target_user: str,
) -> str:
    """Build a client-side SSH command that enters the bastion then tmux."""
    session_name = f"{tmux_prefix}{hostname}"
    remote_command = (
        "tmux new-session -A -s "
        f"{shlex.quote(session_name)} "
        f"bastion-ssh {shlex.quote(hostname)} {shlex.quote(target_user)}"
    )
    return (
        f"ssh -t {shlex.quote(jumpbox_user)}@{shlex.quote(jumpbox_host)} "
        f"{shlex.quote(remote_command)}"
    )


def _build_direct_command(server: dict[str, Any]) -> str:
    """Build a client-side direct SSH command for manually added servers."""
    session_name = server.get("session_name") or server["hostname"]
    port = int(server.get("port", 22))
    remote_command = (
        "tmux new-session -A -s "
        f"{shlex.quote(session_name)}"
    )
    return (
        f"ssh -t -p {port} {shlex.quote(server['user'])}@{shlex.quote(server['host'])} "
        f"{shlex.quote(remote_command)}"
    )


def _build_server_command(server: dict[str, Any], settings: dict[str, Any]) -> str:
    """Build the copyable SSH command for any server."""
    if server["source"] == "tailscale":
        return _build_jumpbox_command(
            jumpbox_host=_resolve_jumpbox_host(settings),
            jumpbox_user=settings.get("jumpbox", {}).get("ssh_user", "root"),
            tmux_prefix=settings.get("defaults", {}).get("tmux_prefix", ""),
            hostname=server["hostname"],
            target_user=server["user"],
        )
    return _build_direct_command(server)


def _build_terminal_command(server: dict[str, Any], settings: dict[str, Any]) -> str:
    """Build a server-side command for the web terminal session."""
    if server["source"] == "tailscale":
        return f"bastion-ssh {shlex.quote(server['hostname'])} {shlex.quote(server['user'])}"

    port = int(server.get("port", 22))
    return f"ssh -tt -p {port} {shlex.quote(server['user'])}@{shlex.quote(server['host'])}"


def _collect_servers() -> dict[str, Any]:
    """Collect all servers visible in the panel."""
    settings = load_settings()
    overrides = load_overrides()
    manual_servers = load_manual_servers()
    sessions = {session["name"] for session in list_sessions()}
    auth_urls = get_pending_auth_urls()
    terminals.cleanup()

    servers: list[dict[str, Any]] = []
    all_tags: set[str] = set()

    for node in get_nodes():
        info = merge_node_info(node, overrides, settings)
        if info["hidden"]:
            continue
        session_name = f"{settings.get('defaults', {}).get('tmux_prefix', '')}{info['hostname']}"
        info["tmux_session_exists"] = session_name in sessions
        info["pending_auth_url"] = auth_urls.get(info["hostname"])
        info["command"] = _build_server_command(info, settings)
        servers.append(info)
        all_tags.update(info["tags"])

    for server in manual_servers:
        if server["hidden"]:
            continue
        session_name = server.get("session_name") or server["hostname"]
        server["tmux_session_exists"] = session_name in sessions
        server["pending_auth_url"] = None
        server["online"] = _probe_host(server["host"], int(server.get("port", 22)))
        server["command"] = _build_server_command(server, settings)
        servers.append(server)
        all_tags.update(server["tags"])

    servers.sort(
        key=lambda item: (
            item["source"] != "tailscale",
            item["online"] is False,
            item["display_name"].lower(),
        )
    )

    return {
        "title": settings.get("ui", {}).get("title", "Bastion"),
        "subtitle": settings.get("ui", {}).get("subtitle", "跳板机管理面板"),
        "bind_address": _resolve_bind_address(settings),
        "port": settings.get("web", {}).get("port", 1234),
        "refresh_interval": settings.get("ui", {}).get("refresh_interval", 10),
        "jumpbox_host": _resolve_jumpbox_host(settings),
        "jumpbox_user": settings.get("jumpbox", {}).get("ssh_user", "root"),
        "tmux_prefix": settings.get("defaults", {}).get("tmux_prefix", ""),
        "default_target_user": settings.get("defaults", {}).get("target_user", "root"),
        "available_tags": sorted(all_tags),
        "pending_auth": auth_urls,
        "servers": servers,
        "settings": {
            "jumpbox_host": settings.get("jumpbox", {}).get("host", "auto"),
            "jumpbox_user": settings.get("jumpbox", {}).get("ssh_user", "root"),
        },
    }


def _find_server(server_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Find a server by id, returning the server and current settings."""
    settings = load_settings()
    overrides = load_overrides()

    if server_id.startswith("ts:"):
        hostname = server_id.split(":", 1)[1]
        for node in get_nodes():
            info = merge_node_info(node, overrides, settings)
            if info["id"] == server_id:
                return info, settings
        return None, settings

    if server_id.startswith("manual:"):
        for server in load_manual_servers():
            if server["id"] == server_id:
                return server, settings
        return None, settings

    return None, settings


def _json_payload() -> dict[str, Any]:
    """Return the current panel payload."""
    return _collect_servers()


@app.route("/")
def index() -> str:
    """Render the main UI."""
    settings = load_settings()
    return render_template(
        "index.html",
        title=settings.get("ui", {}).get("title", "Bastion"),
        subtitle=settings.get("ui", {}).get("subtitle", "跳板机管理面板"),
        refresh_interval=settings.get("ui", {}).get("refresh_interval", 10),
    )


@app.route("/api/servers")
def api_servers() -> Response:
    """Return all visible servers and UI metadata."""
    return jsonify(_json_payload())


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings() -> Response:
    """Read or update panel settings."""
    if request.method == "GET":
        settings = load_settings()
        return jsonify(
            {
                "jumpbox_host": settings.get("jumpbox", {}).get("host", "auto"),
                "jumpbox_user": settings.get("jumpbox", {}).get("ssh_user", "root"),
            }
        )

    payload = request.get_json(silent=True) or {}
    settings = load_settings()
    settings.setdefault("jumpbox", {})
    settings["jumpbox"]["host"] = str(payload.get("jumpbox_host", "auto")).strip() or "auto"
    settings["jumpbox"]["ssh_user"] = str(payload.get("jumpbox_user", "root")).strip() or "root"
    save_settings(settings)
    return jsonify({"ok": True})


@app.route("/api/manual-servers", methods=["GET", "POST"])
def api_manual_servers() -> Response:
    """List or create manual servers."""
    if request.method == "GET":
        return jsonify({"servers": load_manual_servers()})

    payload = request.get_json(silent=True) or {}
    servers = load_manual_servers()
    display_name = str(payload.get("display_name") or payload.get("name") or "").strip()
    host = str(payload.get("host") or "").strip()
    if not display_name or not host:
        return jsonify({"ok": False, "error": "display_name and host are required"}), 400

    raw_id = str(payload.get("raw_id") or payload.get("hostname") or display_name).strip()
    server = {
        "raw_id": raw_id.lower().replace(" ", "-"),
        "display_name": display_name,
        "hostname": raw_id.lower().replace(" ", "-"),
        "host": host,
        "ip": host,
        "port": int(payload.get("port") or 22),
        "user": str(payload.get("user") or "root"),
        "note": str(payload.get("note") or ""),
        "tags": [str(tag) for tag in (payload.get("tags") or []) if str(tag).strip()],
        "network": str(payload.get("network") or "private"),
        "os": str(payload.get("os") or "unknown"),
        "hidden": bool(payload.get("hidden", False)),
        "connect_mode": "direct",
        "session_name": str(payload.get("session_name") or raw_id.lower().replace(" ", "-")),
    }
    servers = [item for item in servers if item["raw_id"] != server["raw_id"]]
    servers.append(server)
    save_manual_servers(servers)
    return jsonify({"ok": True})


@app.route("/api/manual-servers/<server_id>", methods=["PUT", "DELETE"])
def api_manual_server(server_id: str) -> Response:
    """Update or delete one manual server."""
    raw_id = server_id
    servers = load_manual_servers()

    if request.method == "DELETE":
        save_manual_servers([server for server in servers if server["raw_id"] != raw_id])
        return jsonify({"ok": True})

    payload = request.get_json(silent=True) or {}
    updated: list[dict[str, Any]] = []
    found = False
    for server in servers:
        if server["raw_id"] != raw_id:
            updated.append(server)
            continue
        found = True
        server.update(
            {
                "display_name": str(payload.get("display_name") or server["display_name"]).strip(),
                "host": str(payload.get("host") or server["host"]).strip(),
                "ip": str(payload.get("host") or server["host"]).strip(),
                "port": int(payload.get("port") or server["port"]),
                "user": str(payload.get("user") or server["user"]).strip(),
                "note": str(payload.get("note") or server["note"]),
                "tags": [
                    str(tag) for tag in (payload.get("tags") or server["tags"]) if str(tag).strip()
                ],
                "network": str(payload.get("network") or server["network"]),
                "os": str(payload.get("os") or server["os"]),
                "hidden": bool(payload.get("hidden", server["hidden"])),
                "session_name": str(
                    payload.get("session_name") or server.get("session_name") or server["raw_id"]
                ),
            }
        )
        updated.append(server)

    if not found:
        return jsonify({"ok": False, "error": "server not found"}), 404

    save_manual_servers(updated)
    return jsonify({"ok": True})


@app.route("/api/ssh-config")
def api_ssh_config() -> Response:
    """Download a local SSH config snippet."""
    data = _json_payload()
    lines = [
        "# Bastion generated SSH config",
        "",
        "Host jumpbox",
        f"  HostName {data['jumpbox_host']}",
        f"  User {data['jumpbox_user']}",
        "",
    ]

    for server in data["servers"]:
        lines.extend(
            [
                f"Host {server['display_name']}",
                f"# Source: {server['source']}",
                f"# Address: {server['host'] if server['source'] == 'manual' else server['hostname']}",
                "",
            ]
        )

    return Response(
        "\n".join(lines),
        mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=bastion-ssh-config"},
    )


@app.route("/api/terminal", methods=["POST"])
def api_terminal_create() -> Response:
    """Create a new browser terminal session."""
    payload = request.get_json(silent=True) or {}
    server_id = str(payload.get("server_id") or "")
    server, settings = _find_server(server_id)
    if server is None:
        return jsonify({"ok": False, "error": "server not found"}), 404

    command = _build_terminal_command(server, settings)
    session = terminals.create(command)
    initial_output = terminals.read(session.session_id)
    return jsonify(
        {
            "ok": True,
            "session_id": session.session_id,
            "command": command,
            "output": initial_output,
        }
    )


@app.route("/api/terminal/<session_id>", methods=["GET", "DELETE"])
def api_terminal_session(session_id: str) -> Response:
    """Poll or close an existing terminal session."""
    if request.method == "DELETE":
        terminals.close(session_id)
        return jsonify({"ok": True})

    session = terminals.get(session_id)
    if session is None:
        return jsonify({"ok": False, "error": "session not found"}), 404

    output = terminals.read(session_id)
    return jsonify(
        {
            "ok": True,
            "output": output,
            "closed": session.closed or session.process.poll() is not None,
        }
    )


@app.route("/api/terminal/<session_id>/input", methods=["POST"])
def api_terminal_input(session_id: str) -> Response:
    """Send input to a terminal session."""
    payload = request.get_json(silent=True) or {}
    data = str(payload.get("data") or "")
    if not terminals.write(session_id, data):
        return jsonify({"ok": False, "error": "session not writable"}), 404
    return jsonify({"ok": True})


@app.route("/api/clear-auth", methods=["POST"])
def api_clear_auth() -> Response:
    """Clear a pending auth URL."""
    data = request.get_json(silent=True) or {}
    hostname = str(data.get("hostname", "")).strip()
    if not hostname:
        return jsonify({"ok": False, "error": "hostname required"}), 400
    return jsonify({"ok": clear_auth_url(hostname)})


@app.route("/healthz")
def healthz() -> tuple[str, int]:
    """Health check."""
    return "ok", 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
