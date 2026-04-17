"""Tailscale command helpers."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _run_tailscale(args: list[str]) -> Optional[str]:
    """Run a tailscale command and return stdout on success."""
    try:
        result = subprocess.run(
            ["tailscale", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        logger.error("tailscale %s failed: %s", " ".join(args), exc)
        return None


def get_status() -> Optional[dict[str, Any]]:
    """Return parsed output from `tailscale status --json`."""
    output = _run_tailscale(["status", "--json"])
    if output is None:
        return None

    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse tailscale status JSON: %s", exc)
        return None


def _extract_ipv4(addresses: list[str]) -> str:
    """Return the first IPv4 address from a list, or an empty string."""
    for address in addresses:
        if ":" not in address:
            return address
    return ""


def _normalize_hostname(hostname: str, dns_name: str) -> str:
    """Choose a stable hostname from HostName / DNSName."""
    hostname = str(hostname or "").strip()
    if hostname:
        return hostname

    dns_name = str(dns_name or "").strip().rstrip(".")
    if dns_name:
        return dns_name.split(".", 1)[0]

    return "unknown"


def _status_from_json() -> list[dict[str, Any]]:
    """Parse peers from `tailscale status --json`."""
    status = get_status()
    if status is None:
        return []

    nodes: list[dict[str, Any]] = []
    for peer in (status.get("Peer", {}) or {}).values():
        dns_name = str(peer.get("DNSName", "")).rstrip(".")
        hostname = _normalize_hostname(peer.get("HostName", ""), dns_name)
        ipv4 = _extract_ipv4(peer.get("TailscaleIPs", []) or [])

        nodes.append(
            {
                "hostname": hostname,
                "dns_name": dns_name,
                "ip": ipv4,
                "online": bool(peer.get("Online", False)),
                "os": peer.get("OS", "unknown"),
                "tags": [
                    str(tag).removeprefix("tag:")
                    for tag in (peer.get("Tags", []) or [])
                ],
            }
        )

    return nodes


def _status_from_text() -> list[dict[str, Any]]:
    """Parse peers from plain `tailscale status` as a fallback source."""
    output = _run_tailscale(["status"])
    if output is None:
        return []

    nodes: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        ip = parts[0]
        hostname = parts[1]
        if "." in hostname:
            hostname = hostname.split(".", 1)[0]

        if ":" in ip or ip.lower() in {"100.x.x.x", "ip"}:
            continue

        lower_line = line.lower()
        online = "offline" not in lower_line

        nodes.append(
            {
                "hostname": hostname,
                "dns_name": "",
                "ip": ip,
                "online": online,
                "os": "unknown",
                "tags": [],
            }
        )

    return nodes


def get_nodes() -> list[dict[str, Any]]:
    """Return peers, combining JSON data with plain-text fallback parsing."""
    merged: dict[str, dict[str, Any]] = {}

    for node in _status_from_text():
        key = node["hostname"] or node["ip"]
        merged[key] = node

    for node in _status_from_json():
        key = node["hostname"] or node["ip"]
        existing = merged.get(key, {})
        merged[key] = {
            "hostname": node.get("hostname") or existing.get("hostname", "unknown"),
            "dns_name": node.get("dns_name") or existing.get("dns_name", ""),
            "ip": node.get("ip") or existing.get("ip", ""),
            "online": bool(node.get("online", existing.get("online", False))),
            "os": node.get("os") or existing.get("os", "unknown"),
            "tags": node.get("tags") or existing.get("tags", []),
        }

    nodes = [node for node in merged.values() if node.get("hostname") and node.get("ip")]
    nodes.sort(key=lambda item: (not item["online"], item["hostname"]))
    return nodes


def get_self_hostname() -> Optional[str]:
    """Return the local node MagicDNS hostname or HostName."""
    status = get_status()
    if status is None:
        return None

    self_node = status.get("Self", {}) or {}
    dns_name = str(self_node.get("DNSName", "")).rstrip(".")
    if dns_name:
        return dns_name
    return self_node.get("HostName")


def get_self_ip() -> Optional[str]:
    """Return the local node Tailscale IPv4 address."""
    output = _run_tailscale(["ip", "-4"])
    if output is None:
        return None

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[0] if lines else None
