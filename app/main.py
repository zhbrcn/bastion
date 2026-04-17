"""Flask entrypoint for Bastion."""

from __future__ import annotations

import io
import json
import logging
import os
import pty
import re
import select
import shlex
import signal
import socket
import struct
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import fcntl
import termios
from flask import Flask, Response, jsonify, render_template, request
from flask_sock import Sock

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
from tmux_manager import list_sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

app = Flask(__name__)
sock = Sock(app)

_PROBE_CACHE_SECONDS = 30.0
_PROBE_CACHE: dict[tuple[str, int], tuple[float, bool]] = {}
_PROBE_CACHE_LOCK = threading.Lock()
_WINDOWS_INVALID_FILENAME = re.compile(r'[\\/:*?"<>|]+')

LAUNCHER_INSTALL_BAT = r"""@echo off
setlocal
set "INSTALL_DIR=%LOCALAPPDATA%\bastion"
set "LAUNCHER_BAT=%INSTALL_DIR%\bastion-launcher.bat"
set "LAUNCHER_PS1=%INSTALL_DIR%\bastion-launcher.ps1"

echo Installing to %INSTALL_DIR%...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

copy /Y "%~dp0bastion-launcher.bat" "%LAUNCHER_BAT%" >nul || goto :fail
copy /Y "%~dp0bastion-launcher.ps1" "%LAUNCHER_PS1%" >nul || goto :fail

echo Registering bastion:// protocol...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$launcher = [IO.Path]::Combine($env:LOCALAPPDATA, 'bastion', 'bastion-launcher.bat');" ^
  "$protocol = 'HKCU:\Software\Classes\bastion';" ^
  "$command = 'HKCU:\Software\Classes\bastion\shell\open\command';" ^
  "New-Item -Path $protocol -Force | Out-Null;" ^
  "Set-Item -Path $protocol -Value 'URL:Bastion Protocol';" ^
  "New-ItemProperty -Path $protocol -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null;" ^
  "New-Item -Path $command -Force | Out-Null;" ^
  "$open = '""' + $launcher + '"" ""%%1""';" ^
  "Set-Item -Path $command -Value $open;" || goto :fail

echo.
echo [OK] Installation complete.
echo You can now click the "Open Terminal" button on the Bastion panel.
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Installation failed.
echo.
pause
exit /b 1
"""

LAUNCHER_EXECUTOR_BAT = r"""@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bastion-launcher.ps1" "%~1"
"""

LAUNCHER_EXECUTOR_PS1 = r"""param([string]$Url)

# 解析 bastion://connect?...
if ($Url -notmatch '^bastion://connect\?(.+)$') {
    Write-Host "Invalid URL: $Url"
    Read-Host "Press Enter to exit"
    exit 1
}

$query = $matches[1]
$params = @{
    host = ""; user = "root"; session = ""; mode = "resume"
    jumpbox = ""; via = "tailscale"; port = "22"
}

foreach ($pair in $query -split '&') {
    $kv = $pair -split '=', 2
    if ($kv.Count -eq 2) {
        $key = $kv[0]
        $val = [System.Uri]::UnescapeDataString($kv[1])
        if ($params.ContainsKey($key)) {
            $params[$key] = $val
        }
    }
}

function Quote-Arg {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) {
        return '""'
    }
    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

# 构造 tmux 子命令
$tmuxCmd = switch ($params.mode) {
    "direct" { "" }
    "new"    { "tmux new-session -s $($params.session)" }
    default  { "tmux new-session -A -s $($params.session)" }
}

# 构造完整 ssh 命令
if ($params.via -eq "tailscale") {
    if ($params.mode -eq "direct") {
        $remote = "bastion-ssh $($params.host) $($params.user)"
    } else {
        $remote = "$tmuxCmd bastion-ssh $($params.host) $($params.user)"
    }
    $sshArgs = @("-t", $params.jumpbox, $remote)
} else {
    if ($params.mode -eq "direct") {
        $sshArgs = @("-t", "-p", $params.port, "$($params.user)@$($params.host)")
    } else {
        $sshArgs = @("-t", "-p", $params.port, "$($params.user)@$($params.host)", $tmuxCmd)
    }
}

# 组装成单条 ssh 命令，交给终端里的 cmd /k 执行
$sshCmd = 'ssh ' + (($sshArgs | ForEach-Object { Quote-Arg $_ }) -join ' ')

# 优先用 Windows Terminal
$wt = Get-Command wt.exe -ErrorAction SilentlyContinue
if ($wt) {
    $wtArgs = @("new-tab", "--title", $params.host, "cmd", "/k", $sshCmd)
    Start-Process wt.exe -ArgumentList $wtArgs
} else {
    Start-Process cmd.exe -ArgumentList @("/k", $sshCmd)
}
"""

LAUNCHER_README = r"""Bastion Windows Launcher
This package lets the browser open Windows Terminal directly from the Bastion panel.

Install:
1. Extract all files to any folder.
2. Keep bastion-launcher.bat and bastion-launcher.ps1 together.
3. Double-click bastion-install.bat. Admin rights are not required.
4. Return to the Bastion panel and click "Open Terminal".

First use:
Your browser may ask whether this site can open the bastion:// protocol.
Choose Allow, and optionally enable Always allow.

Requirements:
- Windows 10 or Windows 11
- OpenSSH client installed
- Windows Terminal recommended

Uninstall:
- Delete HKCU\Software\Classes\bastion
- Delete %LOCALAPPDATA%\bastion
"""


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


def _probe_host(host: str, port: int) -> bool:
    """Best-effort TCP reachability probe with a short in-memory cache."""
    cache_key = (host, port)
    now = time.time()

    with _PROBE_CACHE_LOCK:
        cached = _PROBE_CACHE.get(cache_key)
        if cached and now - cached[0] < _PROBE_CACHE_SECONDS:
            return cached[1]

    online = False
    try:
        with socket.create_connection((host, port), timeout=1.5):
            online = True
    except OSError:
        online = False

    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[cache_key] = (now, online)

    return online


def _build_jumpbox_command(
    jumpbox_host: str,
    jumpbox_user: str,
    tmux_prefix: str,
    hostname: str,
    target_user: str,
    session_mode: str = "resume",
    session_name: str = "",
) -> str:
    """Build a client-side SSH command that enters the bastion then tmux."""
    effective_session = session_name or f"{tmux_prefix}{hostname}"
    if session_mode == "direct":
        remote_command = f"bastion-ssh {shlex.quote(hostname)} {shlex.quote(target_user)}"
    elif session_mode == "new":
        remote_command = (
            "tmux new-session -s "
            f"{shlex.quote(effective_session)} "
            f"bastion-ssh {shlex.quote(hostname)} {shlex.quote(target_user)}"
        )
    else:
        remote_command = (
            "tmux new-session -A -s "
            f"{shlex.quote(effective_session)} "
            f"bastion-ssh {shlex.quote(hostname)} {shlex.quote(target_user)}"
        )
    return (
        f"ssh -t {shlex.quote(jumpbox_user)}@{shlex.quote(jumpbox_host)} "
        f"{shlex.quote(remote_command)}"
    )


def _build_direct_command(
    server: dict[str, Any],
    session_mode: str = "resume",
    session_name: str = "",
) -> str:
    """Build a client-side direct SSH command for manually added servers."""
    effective_session = session_name or server.get("session_name") or server["hostname"]
    port = int(server.get("port", 22))
    if session_mode == "direct":
        remote_command = ""
    elif session_mode == "new":
        remote_command = f"tmux new-session -s {shlex.quote(effective_session)}"
    else:
        remote_command = f"tmux new-session -A -s {shlex.quote(effective_session)}"
    command = f"ssh -t -p {port} {shlex.quote(server['user'])}@{shlex.quote(server['host'])}"
    if remote_command:
        command += f" {shlex.quote(remote_command)}"
    return command


def _build_server_command(
    server: dict[str, Any],
    settings: dict[str, Any],
    session_mode: str = "resume",
    session_name: str = "",
) -> str:
    """Build the copyable SSH command for any server."""
    if server["source"] == "tailscale":
        return _build_jumpbox_command(
            jumpbox_host=_resolve_jumpbox_host(settings),
            jumpbox_user=settings.get("jumpbox", {}).get("ssh_user", "root"),
            tmux_prefix=settings.get("defaults", {}).get("tmux_prefix", ""),
            hostname=server["hostname"],
            target_user=server["user"],
            session_mode=session_mode,
            session_name=session_name,
        )
    return _build_direct_command(server, session_mode=session_mode, session_name=session_name)


def _build_terminal_command(
    server: dict[str, Any],
    settings: dict[str, Any],
    session_mode: str = "resume",
    session_name: str = "",
) -> str:
    """Build a server-side command for the websocket terminal session."""
    if server["source"] == "tailscale":
        effective_session = (
            session_name
            or f"{settings.get('defaults', {}).get('tmux_prefix', '')}{server['hostname']}"
        )
        if session_mode == "direct":
            return f"bastion-ssh {shlex.quote(server['hostname'])} {shlex.quote(server['user'])}"
        if session_mode == "new":
            return (
                "tmux new-session -s "
                f"{shlex.quote(effective_session)} "
                f"bastion-ssh {shlex.quote(server['hostname'])} {shlex.quote(server['user'])}"
            )
        return (
            "tmux new-session -A -s "
            f"{shlex.quote(effective_session)} "
            f"bastion-ssh {shlex.quote(server['hostname'])} {shlex.quote(server['user'])}"
        )

    effective_session = session_name or server.get("session_name") or server["hostname"]
    port = int(server.get("port", 22))
    base_command = f"ssh -tt -p {port} {shlex.quote(server['user'])}@{shlex.quote(server['host'])}"
    if session_mode == "direct":
        return base_command

    tmux_command = "tmux new-session -A -s " if session_mode != "new" else "tmux new-session -s "
    remote_command = tmux_command + shlex.quote(effective_session)
    return f"{base_command} {shlex.quote(remote_command)}"


def _manual_probe_result(server: dict[str, Any]) -> tuple[str, bool]:
    """Return online status for one manual server."""
    raw_id = str(server.get("raw_id") or server.get("hostname") or "")
    return raw_id, _probe_host(server["host"], int(server.get("port", 22)))


def _collect_servers() -> dict[str, Any]:
    """Collect all servers visible in the panel."""
    settings = load_settings()
    overrides = load_overrides()
    manual_servers = load_manual_servers()
    sessions = {session["name"] for session in list_sessions()}
    auth_urls = get_pending_auth_urls()

    manual_online: dict[str, bool] = {}
    visible_manuals = [server for server in manual_servers if not server["hidden"]]
    if visible_manuals:
        with ThreadPoolExecutor(max_workers=min(16, len(visible_manuals))) as executor:
            for raw_id, online in executor.map(_manual_probe_result, visible_manuals):
                manual_online[raw_id] = online

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
        info["session_name"] = session_name
        servers.append(info)
        all_tags.update(info["tags"])

    for server in manual_servers:
        if server["hidden"]:
            continue
        session_name = server.get("session_name") or server["hostname"]
        server["tmux_session_exists"] = session_name in sessions
        server["pending_auth_url"] = None
        server["online"] = manual_online.get(server["raw_id"], False)
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
        "port": int(settings.get("web", {}).get("port", 1234)),
        "refresh_interval": int(settings.get("ui", {}).get("refresh_interval", 10)),
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


def _set_pty_size(master_fd: int, rows: int, cols: int) -> None:
    """Apply PTY window size to the child process."""
    if rows <= 0 or cols <= 0:
        return
    fcntl.ioctl(
        master_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", rows, cols, 0, 0),
    )


def _stream_terminal_output(ws: Any, master_fd: int, process: subprocess.Popen[str]) -> None:
    """Read from a PTY and forward raw bytes to the websocket."""
    try:
        while process.poll() is None:
            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if not ready:
                continue
            data = os.read(master_fd, 4096)
            if not data:
                break
            ws.send(data.decode("utf-8", errors="replace"))
    except OSError:
        pass


def _sanitize_windows_filename(name: str) -> str:
    """Return a Windows-safe .bat base name."""
    sanitized = _WINDOWS_INVALID_FILENAME.sub("_", name).strip(" .")
    return sanitized or "server"


def _windows_batch_body(server: dict[str, Any], settings: dict[str, Any]) -> str:
    """Build one Windows batch shortcut."""
    session_name = server.get("session_name") or server["hostname"]

    if server["source"] == "tailscale":
        host = f"{settings.get('jumpbox', {}).get('ssh_user', 'root')}@{_resolve_jumpbox_host(settings)}"
        remote_command = (
            f"tmux new-session -A -s {shlex.quote(session_name)} "
            f"bastion-ssh {shlex.quote(server['hostname'])} {shlex.quote(server['user'])}"
        )
        ssh_command = f'ssh -t {host} "{remote_command}"'
    else:
        host = f"{server['user']}@{server['host']}"
        port = int(server.get("port", 22))
        remote_command = f"tmux new-session -A -s {shlex.quote(session_name)}"
        ssh_command = f'ssh -t -p {port} {host} "{remote_command}"'

    return "\r\n".join(
        [
            "@echo off",
            "setlocal",
            f"set \"TITLE={server['display_name']}\"",
            f"set \"SSH_COMMAND={ssh_command}\"",
            "where wt.exe >nul 2>nul",
            "if %ERRORLEVEL%==0 (",
            "  wt.exe new-tab cmd /k \"%SSH_COMMAND%\"",
            ") else (",
            "  %SSH_COMMAND%",
            ")",
            "endlocal",
            "",
        ]
    )


@sock.route("/ws/terminal/<path:server_id>")
def ws_terminal(ws: Any, server_id: str) -> None:
    """Run a full interactive terminal session over websocket."""
    server, settings = _find_server(server_id)
    if server is None:
        ws.send("\r\n[error] server not found\r\n")
        return

    init_message = ws.receive()
    session_mode = "resume"
    session_name = ""
    initial_rows = 24
    initial_cols = 120
    if isinstance(init_message, str):
        try:
            payload = json.loads(init_message)
            if payload.get("type") == "init":
                session_mode = str(payload.get("session_mode") or "resume")
                session_name = str(payload.get("session_name") or "").strip()
                initial_rows = max(1, int(payload.get("rows") or initial_rows))
                initial_cols = max(1, int(payload.get("cols") or initial_cols))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    master_fd, slave_fd = pty.openpty()
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")

    process: subprocess.Popen[str] | None = None
    try:
        _set_pty_size(master_fd, initial_rows, initial_cols)
        _set_pty_size(slave_fd, initial_rows, initial_cols)

        process = subprocess.Popen(
            [
                "/bin/bash",
                "-lc",
                _build_terminal_command(
                    server,
                    settings,
                    session_mode=session_mode,
                    session_name=session_name,
                ),
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=os.setsid,
            env=env,
        )
        os.close(slave_fd)

        thread = threading.Thread(
            target=_stream_terminal_output,
            args=(ws, master_fd, process),
            daemon=True,
        )
        thread.start()

        while True:
            message = ws.receive()
            if message is None:
                break

            if isinstance(message, bytes):
                os.write(master_fd, message)
                continue

            if not isinstance(message, str):
                continue

            if message.startswith("{"):
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and payload.get("type") == "resize":
                    rows = max(1, int(payload.get("rows") or 24))
                    cols = max(1, int(payload.get("cols") or 80))
                    _set_pty_size(master_fd, rows, cols)
                    continue

            os.write(master_fd, message.encode("utf-8"))
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            os.close(slave_fd)
        except OSError:
            pass
        if process is not None and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except OSError:
                pass


@app.route("/")
def index() -> str:
    """Render the main UI."""
    settings = load_settings()
    return render_template(
        "index.html",
        title=settings.get("ui", {}).get("title", "Bastion"),
        subtitle=settings.get("ui", {}).get("subtitle", "跳板机管理面板"),
        refresh_interval=int(settings.get("ui", {}).get("refresh_interval", 10)),
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
    session_name = str(payload.get("session_name") or raw_id.lower().replace(" ", "-")).strip()
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
        "session_name": session_name,
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
                ).strip(),
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


@app.route("/api/batch-download")
def api_batch_download() -> Response:
    """Download Windows batch shortcuts for all visible servers."""
    payload = _json_payload()
    settings = load_settings()
    archive = io.BytesIO()

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for server in payload["servers"]:
            filename = f"{_sanitize_windows_filename(server['display_name'])}.bat"
            zf.writestr(filename, _windows_batch_body(server, settings))

    archive.seek(0)
    return Response(
        archive.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": "attachment; filename=bastion-shortcuts.zip",
        },
    )


@app.route("/api/launcher-setup")
def api_launcher_setup() -> Response:
    """Download Windows protocol handler setup package."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bastion-install.bat", LAUNCHER_INSTALL_BAT)
        zf.writestr("bastion-launcher.bat", LAUNCHER_EXECUTOR_BAT)
        zf.writestr("bastion-launcher.ps1", LAUNCHER_EXECUTOR_PS1)
        zf.writestr("README.txt", LAUNCHER_README)
    archive.seek(0)
    return Response(
        archive.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": "attachment; filename=bastion-win-launcher.zip",
        },
    )


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
