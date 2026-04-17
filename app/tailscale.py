"""封装 tailscale 命令调用。"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _run_tailscale(args: list[str]) -> Optional[str]:
    """运行 tailscale 命令，失败时返回 None。"""
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
    """调用 `tailscale status --json` 并返回解析后的结果。"""
    output = _run_tailscale(["status", "--json"])
    if output is None:
        return None

    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse tailscale status JSON: %s", exc)
        return None


def _extract_ipv4(addresses: list[str]) -> Optional[str]:
    """从地址列表中提取第一个 IPv4 地址。"""
    for address in addresses:
        if ":" not in address:
            return address
    return None


def get_nodes() -> list[dict[str, Any]]:
    """返回当前 Tailscale 网络中的其他节点信息列表。"""
    status = get_status()
    if status is None:
        return []

    nodes: list[dict[str, Any]] = []
    peer_map = status.get("Peer", {}) or {}

    for peer in peer_map.values():
        ipv4 = _extract_ipv4(peer.get("TailscaleIPs", []) or [])
        if not ipv4:
            continue

        raw_tags = peer.get("Tags", []) or []
        tags = [tag.removeprefix("tag:") for tag in raw_tags]
        dns_name = str(peer.get("DNSName", "")).rstrip(".")

        nodes.append(
            {
                "hostname": peer.get("HostName", "unknown"),
                "dns_name": dns_name,
                "ip": ipv4,
                "online": bool(peer.get("Online", False)),
                "os": peer.get("OS", "unknown"),
                "tags": tags,
            }
        )

    return nodes


def get_self_hostname() -> Optional[str]:
    """返回当前跳板机的 Tailscale MagicDNS 主机名。"""
    status = get_status()
    if status is None:
        return None

    self_node = status.get("Self", {}) or {}
    dns_name = str(self_node.get("DNSName", "")).rstrip(".")
    if dns_name:
        return dns_name
    return self_node.get("HostName")


def get_self_ip() -> Optional[str]:
    """返回当前跳板机的 Tailscale IPv4 地址。"""
    output = _run_tailscale(["ip", "-4"])
    if output is None:
        return None

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[0] if lines else None
