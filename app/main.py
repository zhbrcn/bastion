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
_SAFE_LAUNCH_ARG = re.compile(r'^[A-Za-z0-9._\-@:+]+$')


def _require_safe_launch_arg(value: str, field: str) -> str:
    """Reject anything that would need escaping inside a .bat file."""
    if not value or not _SAFE_LAUNCH_ARG.match(value):
        raise ValueError(f"unsafe value for {field}: {value!r}")
    return value


def _build_launch_batch(
    via: str,
    host: str,
    user: str,
    session: str,
    mode: str,
    jumpbox: str,
    port: str,
) -> str:
    """Build a minimal Windows .bat that opens an SSH session."""
    if via == "tailscale":
        if mode == "direct":
            inner = f"bastion-ssh {host} {user}"
        elif mode == "new":
            inner = f"tmux new-session -s {session} bastion-ssh {host} {user}"
        else:
            inner = f"tmux new-session -A -s {session} bastion-ssh {host} {user}"
        ssh_cmd = f'ssh -t {jumpbox} "{inner}"'
    else:
        target = f"{user}@{host}"
        if mode == "direct":
            ssh_cmd = f"ssh -t -p {port} {target}"
        elif mode == "new":
            ssh_cmd = f'ssh -t -p {port} {target} "tmux new-session -s {session}"'
        else:
            ssh_cmd = f'ssh -t -p {port} {target} "tmux new-session -A -s {session}"'

    return "\r\n".join(
        [
            "@echo off",
            f"title Bastion - {host}",
            ssh_cmd,
            "if errorlevel 1 pause",
            "",
        ]
    )

AGENT_PS1 = r"""$ErrorActionPreference = 'Continue'
$port = 18722
$logDir = Join-Path $env:LOCALAPPDATA 'bastion'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir 'agent.log'

function Write-AgentLog([string]$msg) {
    try {
        $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        [System.IO.File]::AppendAllText($logPath, "[$ts] $msg`r`n")
    } catch {}
}

try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
    $listener.Start()
    Write-AgentLog "Agent listening on 127.0.0.1:$port"
} catch {
    Write-AgentLog "Bind failed: $($_.Exception.Message)"
    exit 1
}

$pattern = '^[A-Za-z0-9._\-@:+]+$'

function Build-SshCommand($via, $mode, $h, $u, $s, $j, $p) {
    if ($via -eq 'tailscale') {
        switch ($mode) {
            'direct' { $inner = "bastion-ssh $h $u" }
            'new'    { $inner = "tmux new-session -s $s bastion-ssh $h $u" }
            default  { $inner = "tmux new-session -A -s $s bastion-ssh $h $u" }
        }
        return 'ssh -t ' + $j + ' "' + $inner + '"'
    } else {
        $t = "$u@$h"
        switch ($mode) {
            'direct' { return "ssh -t -p $p $t" }
            'new'    { return 'ssh -t -p ' + $p + ' ' + $t + ' "tmux new-session -s ' + $s + '"' }
            default  { return 'ssh -t -p ' + $p + ' ' + $t + ' "tmux new-session -A -s ' + $s + '"' }
        }
    }
}

function Launch-Terminal($h, $sshCmd) {
    $dir = Join-Path $env:LOCALAPPDATA 'bastion'
    $bat = Join-Path $dir 'session.cmd'
    $body = "@echo off`r`ntitle Bastion - $h`r`n$sshCmd`r`nif errorlevel 1 pause`r`n"
    [System.IO.File]::WriteAllText($bat, $body, [System.Text.Encoding]::Default)
    Write-AgentLog "Launch: $sshCmd"
    $wt = Get-Command wt.exe -ErrorAction SilentlyContinue
    if ($wt) {
        Start-Process -FilePath 'wt.exe' -ArgumentList "-w 0 nt --title `"$h`" cmd /k `"$bat`""
    } else {
        Start-Process -FilePath 'cmd.exe' -ArgumentList "/k `"$bat`""
    }
}

while ($true) {
    try { $client = $listener.AcceptTcpClient() } catch { break }
    try {
        $stream = $client.GetStream()
        $stream.ReadTimeout = 3000
        $buf = New-Object byte[] 8192
        $sb = [System.Text.StringBuilder]::new()
        while ($true) {
            $n = $stream.Read($buf, 0, $buf.Length)
            if ($n -le 0) { break }
            [void]$sb.Append([System.Text.Encoding]::ASCII.GetString($buf, 0, $n))
            if ($sb.ToString().Contains("`r`n`r`n")) { break }
            if ($sb.Length -gt 16384) { break }
        }
        $lines = $sb.ToString() -split "`r`n"
        $parts = $lines[0] -split ' '
        $method = $parts[0]
        $path = if ($parts.Count -ge 2) { $parts[1] } else { '/' }

        $origin = '*'
        for ($i = 1; $i -lt $lines.Count; $i++) {
            $hdr = $lines[$i]
            if ([string]::IsNullOrEmpty($hdr)) { break }
            if ($hdr -match '^Origin:\s*(.+)$') { $origin = $matches[1].Trim() }
        }

        $pathOnly = $path
        $qstr = ''
        $qIdx = $path.IndexOf('?')
        if ($qIdx -ge 0) {
            $pathOnly = $path.Substring(0, $qIdx)
            $qstr = $path.Substring($qIdx + 1)
        }
        $q = @{}
        foreach ($pair in ($qstr -split '&')) {
            if (-not $pair) { continue }
            $kv = $pair -split '=', 2
            if ($kv.Count -eq 2) { $q[$kv[0]] = [System.Uri]::UnescapeDataString($kv[1]) }
        }

        $status = '200 OK'
        $bodyText = ''
        $shouldQuit = $false

        if ($method -eq 'OPTIONS') {
            $status = '204 No Content'
        } elseif ($pathOnly -eq '/ping') {
            $bodyText = 'ok'
        } elseif ($pathOnly -eq '/quit') {
            $bodyText = 'bye'
            $shouldQuit = $true
        } elseif ($pathOnly -eq '/launch' -and $method -eq 'GET') {
            $via = if ($q['via']) { $q['via'] } else { 'tailscale' }
            $mode = if ($q['mode']) { $q['mode'] } else { 'resume' }
            $h = $q['host']
            $u = if ($q['user']) { $q['user'] } else { 'root' }
            $s = if ($q['session']) { $q['session'] } else { $h }
            $j = $q['jumpbox']
            $p = if ($q['port']) { $q['port'] } else { '22' }

            $ok = $h -match $pattern -and $u -match $pattern -and $s -match $pattern -and $p -match $pattern
            if ($via -eq 'tailscale') { $ok = $ok -and $j -match $pattern }

            if (-not $ok) {
                $status = '400 Bad Request'
                $bodyText = 'invalid params'
                Write-AgentLog "Reject: bad params host=$h user=$u session=$s port=$p jumpbox=$j"
            } else {
                $sshCmd = Build-SshCommand $via $mode $h $u $s $j $p
                Launch-Terminal $h $sshCmd
                $bodyText = 'ok'
            }
        } else {
            $status = '404 Not Found'
            $bodyText = 'not found'
        }

        $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($bodyText)
        $resp = @(
            "HTTP/1.1 $status",
            "Access-Control-Allow-Origin: $origin",
            "Access-Control-Allow-Methods: GET, OPTIONS",
            "Access-Control-Allow-Headers: *",
            "Access-Control-Allow-Private-Network: true",
            "Vary: Origin",
            "Content-Type: text/plain; charset=utf-8",
            "Content-Length: $($bodyBytes.Length)",
            "Connection: close",
            ""
        ) -join "`r`n"
        $headerBytes = [System.Text.Encoding]::ASCII.GetBytes($resp + "`r`n")
        $stream.Write($headerBytes, 0, $headerBytes.Length)
        if ($bodyBytes.Length -gt 0) { $stream.Write($bodyBytes, 0, $bodyBytes.Length) }
        $stream.Flush()

        if ($shouldQuit) {
            try { $client.Close() } catch {}
            Write-AgentLog "Quit requested"
            $listener.Stop()
            exit 0
        }
    } catch {
        Write-AgentLog "Request error: $($_.Exception.Message)"
    } finally {
        try { $client.Close() } catch {}
    }
}
"""

AGENT_INSTALL_PS1 = r"""$ErrorActionPreference = 'Stop'
$taskName = 'BastionAgent'
$installDir = Join-Path $env:LOCALAPPDATA 'bastion'
$agentPs1 = Join-Path $installDir 'bastion-agent.ps1'

if (-not (Test-Path $agentPs1)) {
    Write-Error "Agent script not found: $agentPs1"
    exit 1
}

# Stop any previous agent.
try { (New-Object Net.WebClient).DownloadString('http://127.0.0.1:18722/quit') | Out-Null } catch {}
Start-Sleep -Seconds 1

$userId = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }

$argString = '-WindowStyle Hidden -ExecutionPolicy Bypass -NoProfile -File "' + $agentPs1 + '"'
$action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argString
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $userId
$settings  = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$task      = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Write-Host "[OK] Scheduled task '$taskName' registered for $userId."

Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 2

$verified = $false
for ($i = 0; $i -lt 5; $i++) {
    try {
        $r = (New-Object Net.WebClient).DownloadString('http://127.0.0.1:18722/ping')
        if ($r -eq 'ok') { $verified = $true; break }
    } catch {}
    Start-Sleep -Seconds 1
}

if ($verified) {
    Write-Host "[OK] Agent responded on 127.0.0.1:18722"
    exit 0
} else {
    Write-Warning "Agent did not respond on port 18722."
    Write-Warning "Check $installDir\agent.log"
    exit 1
}
"""

AGENT_INSTALL_BAT = r"""@echo off
setlocal
set "INSTALL_DIR=%LOCALAPPDATA%\bastion"

echo Installing Bastion Agent...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
copy /Y "%~dp0bastion-agent.ps1" "%INSTALL_DIR%\bastion-agent.ps1" >nul || goto :fail

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-agent.ps1"
if errorlevel 1 goto :fail

echo.
echo [OK] Installation complete.
echo Auto-starts on next logon. Log: %LOCALAPPDATA%\bastion\agent.log
echo Uninstall: run uninstall-agent.bat
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Installation failed.
pause
exit /b 1
"""

AGENT_UNINSTALL_BAT = r"""@echo off
echo Stopping agent...
powershell -NoProfile -Command "try { (New-Object Net.WebClient).DownloadString('http://127.0.0.1:18722/quit') | Out-Null } catch {}"
echo Removing scheduled task...
powershell -NoProfile -Command "try { Unregister-ScheduledTask -TaskName 'BastionAgent' -Confirm:$false } catch {}"
echo Deleting %LOCALAPPDATA%\bastion ...
rmdir /S /Q "%LOCALAPPDATA%\bastion"
echo Done.
pause
"""

AGENT_README = r"""Bastion Agent for Windows

What it does:
    A tiny local HTTP service on 127.0.0.1:18722. When you click
    "Open Terminal" in the Bastion panel, the page sends a GET to
    the agent, which launches Windows Terminal (wt.exe) with the
    correct ssh + tmux command. True one-click, no registry,
    no VBScript.

Install (no admin required):
    1. Extract all files to one folder.
    2. Double-click install-agent.bat.
    3. Wait for "[OK] Agent responded on 127.0.0.1:18722".
    4. Go click "Open Terminal" in the panel.

How it survives reboots:
    A user-scope Scheduled Task named "BastionAgent" is registered
    with an "At log on" trigger. It runs PowerShell hidden.
    You can see it in Task Scheduler under Task Scheduler Library.

Files dropped:
    %LOCALAPPDATA%\bastion\bastion-agent.ps1
    %LOCALAPPDATA%\bastion\agent.log     (runtime log)
    %LOCALAPPDATA%\bastion\session.cmd   (last launched ssh command)

Troubleshoot:
    - Panel shows "Agent 未运行，已下载 .bat" -> agent isn't running.
      Run install-agent.bat again, or manually:
          Start-ScheduledTask -TaskName BastionAgent
    - Check log:  %LOCALAPPDATA%\bastion\agent.log
    - Port 18722 in use? Edit $port in bastion-agent.ps1 (and the
      panel's fetch URL in index.html) and re-run install.

Uninstall:
    Double-click uninstall-agent.bat.
    (Or: browse http://127.0.0.1:18722/quit, then run
     schtasks /Delete /TN BastionAgent /F, then delete
     %LOCALAPPDATA%\bastion.)

Requirements:
    - Windows 10/11 with PowerShell 5+ (built-in).
    - OpenSSH client (Settings -> Apps -> Optional features).
    - Windows Terminal recommended (wt.exe); falls back to cmd.exe.
"""


LAUNCHER_INSTALL_BAT = r"""@echo off
setlocal
set "INSTALL_DIR=%LOCALAPPDATA%\bastion"
set "LAUNCHER_PS1=%INSTALL_DIR%\bastion-launcher.ps1"

echo Installing to %INSTALL_DIR%...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

copy /Y "%~dp0bastion-launcher.ps1" "%LAUNCHER_PS1%" >nul || goto :fail

echo Registering bastion:// protocol...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$launcher = [IO.Path]::Combine($env:SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');" ^
  "$script = [IO.Path]::Combine($env:LOCALAPPDATA, 'bastion', 'bastion-launcher.ps1');" ^
  "$protocol = 'HKCU:\Software\Classes\bastion';" ^
  "$command = 'HKCU:\Software\Classes\bastion\shell\open\command';" ^
  "New-Item -Path $protocol -Force | Out-Null;" ^
  "Set-Item -Path $protocol -Value 'URL:Bastion Protocol';" ^
  "New-ItemProperty -Path $protocol -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null;" ^
  "New-Item -Path $command -Force | Out-Null;" ^
  "$percent = [char]37;" ^
  "$open = '""' + $launcher + '"" -NoProfile -ExecutionPolicy Bypass -File ""' + $script + '"" ""' + $percent + '1""';" ^
  "$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Classes\bastion\shell\open\command');" ^
  "$key.SetValue('', $open, [Microsoft.Win32.RegistryValueKind]::String);" ^
  "$key.Close();" || goto :fail

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

# Parse bastion://connect?... and bastion://connect/?...
if ($Url -notmatch '^bastion://connect/?\?(.+)$') {
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

$logPath = Join-Path $env:LOCALAPPDATA 'bastion\launcher.log'
New-Item -ItemType Directory -Force -Path (Split-Path $logPath) | Out-Null
function Write-LauncherLog {
    param([string]$Message)
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    [System.IO.File]::AppendAllText($logPath, "[$timestamp] $Message`r`n")
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

# Build tmux sub-command
$tmuxCmd = switch ($params.mode) {
    "direct" { "" }
    "new"    { "tmux new-session -s $($params.session)" }
    default  { "tmux new-session -A -s $($params.session)" }
}

# Build ssh arguments
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

# Build one ssh command string
$sshCmd = 'ssh ' + (($sshArgs | ForEach-Object { Quote-Arg $_ }) -join ' ')
Write-LauncherLog "Resolved ssh command: $sshCmd"

# Persist to a .cmd file so we never hit nested-quote issues in wt/cmd argv parsing.
$sessionDir = Join-Path $env:LOCALAPPDATA 'bastion'
New-Item -ItemType Directory -Force -Path $sessionDir | Out-Null
$sessionBat = Join-Path $sessionDir 'last-session.cmd'
$safeTitle = ($params.host -replace '["`]', '')
$batBody = "@echo off`r`ntitle Bastion - $safeTitle`r`n$sshCmd`r`n"
[System.IO.File]::WriteAllText($sessionBat, $batBody, [System.Text.Encoding]::Default)
Write-LauncherLog "Wrote session cmd: $sessionBat"

try {
    $wt = Get-Command wt.exe -ErrorAction SilentlyContinue
    if ($wt) {
        Write-LauncherLog "Launching Windows Terminal: $($wt.Source)"
        $wtArgLine = "-w 0 nt --title `"$safeTitle`" cmd /k `"$sessionBat`""
        Start-Process -FilePath 'wt.exe' -ArgumentList $wtArgLine
    } else {
        Write-LauncherLog "wt.exe not found, falling back to cmd.exe"
        Start-Process -FilePath 'cmd.exe' -ArgumentList "/k `"$sessionBat`""
    }
} catch {
    $msg = $_.Exception.Message
    Write-LauncherLog "Launch failed: $msg"
    Start-Process cmd.exe -ArgumentList "/k echo Bastion launcher failed: $msg & echo Log: $logPath & pause"
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

Behavior:
- Launches Windows Terminal (wt.exe) with the SSH command.
- If wt.exe is not found, falls back to cmd.exe.

Requirements:
- Windows 10 or Windows 11
- OpenSSH client installed
- Windows Terminal recommended

Troubleshoot:
- If clicking "Open Terminal" does nothing, check %LOCALAPPDATA%\bastion\launcher.log
- The last resolved command is saved to %LOCALAPPDATA%\bastion\last-session.cmd
  You can double-click that file to run it manually and see any ssh error.
- Make sure OpenSSH client is installed (Settings -> Apps -> Optional features -> OpenSSH Client)

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


@app.route("/api/launch")
def api_launch() -> Response:
    """Return a single-server .bat that opens wt.exe / cmd with the SSH session."""
    via = (request.args.get("via") or "tailscale").strip() or "tailscale"
    mode = (request.args.get("mode") or "resume").strip() or "resume"
    host = (request.args.get("host") or "").strip()
    user = (request.args.get("user") or "root").strip() or "root"
    session = (request.args.get("session") or "").strip() or host
    jumpbox = (request.args.get("jumpbox") or "").strip()
    port = (request.args.get("port") or "22").strip() or "22"

    try:
        _require_safe_launch_arg(host, "host")
        _require_safe_launch_arg(user, "user")
        _require_safe_launch_arg(session, "session")
        _require_safe_launch_arg(port, "port")
        if via == "tailscale":
            _require_safe_launch_arg(jumpbox, "jumpbox")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    body = _build_launch_batch(via, host, user, session, mode, jumpbox, port)
    filename = _sanitize_windows_filename(f"bastion-{host}") + ".bat"
    return Response(
        body,
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/api/agent-setup")
def api_agent_setup() -> Response:
    """Download the Windows local-agent package (one-click terminal launch)."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("install-agent.bat", AGENT_INSTALL_BAT)
        zf.writestr("install-agent.ps1", AGENT_INSTALL_PS1)
        zf.writestr("uninstall-agent.bat", AGENT_UNINSTALL_BAT)
        zf.writestr("bastion-agent.ps1", AGENT_PS1)
        zf.writestr("README.txt", AGENT_README)
    archive.seek(0)
    return Response(
        archive.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": "attachment; filename=bastion-agent.zip",
        },
    )


@app.route("/api/launcher-setup")
def api_launcher_setup() -> Response:
    """Legacy protocol-handler package (kept for backward compatibility)."""
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
