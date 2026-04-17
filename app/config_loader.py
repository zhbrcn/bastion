"""YAML-backed configuration helpers."""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path("/etc/bastion")
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
OVERRIDES_PATH = CONFIG_DIR / "overrides.yaml"
MANUAL_SERVERS_PATH = CONFIG_DIR / "servers.yaml"

DEFAULT_SETTINGS: dict[str, Any] = {
    "web": {"port": 1234, "bind_address": "auto"},
    "jumpbox": {"host": "auto", "ssh_user": "root"},
    "defaults": {"target_user": "root", "tmux_prefix": ""},
    "ui": {
        "title": "Bastion",
        "subtitle": "跳板机管理面板",
        "refresh_interval": 10,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge dictionaries."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> Any:
    """Read YAML from disk and return the parsed value."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    """Write YAML to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def load_settings() -> dict[str, Any]:
    """Load settings and merge with defaults."""
    if not SETTINGS_PATH.exists():
        logger.warning("%s not found, using defaults", SETTINGS_PATH)
        return copy.deepcopy(DEFAULT_SETTINGS)

    try:
        content = _read_yaml(SETTINGS_PATH) or {}
        if not isinstance(content, dict):
            logger.warning("%s has invalid structure, using defaults", SETTINGS_PATH)
            return copy.deepcopy(DEFAULT_SETTINGS)
        return _deep_merge(copy.deepcopy(DEFAULT_SETTINGS), content)
    except (OSError, yaml.YAMLError) as exc:
        logger.error("Failed to load settings: %s", exc)
        return copy.deepcopy(DEFAULT_SETTINGS)


def save_settings(settings: dict[str, Any]) -> None:
    """Persist the full settings document."""
    merged = _deep_merge(copy.deepcopy(DEFAULT_SETTINGS), settings)
    _write_yaml(SETTINGS_PATH, merged)


def load_overrides() -> dict[str, Any]:
    """Load per-host overrides."""
    if not OVERRIDES_PATH.exists():
        return {}

    try:
        content = _read_yaml(OVERRIDES_PATH) or {}
        if not isinstance(content, dict):
            return {}
        overrides = content.get("overrides", {}) or {}
        return overrides if isinstance(overrides, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error("Failed to load overrides: %s", exc)
        return {}


def merge_node_info(
    node: dict[str, Any],
    overrides: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Merge node state, overrides, and defaults into one server dict."""
    hostname = str(node.get("hostname", "unknown"))
    override = overrides.get(hostname, {}) or {}
    defaults = settings.get("defaults", {}) or {}

    merged_tags = list(
        dict.fromkeys((node.get("tags") or []) + (override.get("tags") or []))
    )

    return {
        "id": f"ts:{hostname}",
        "source": "tailscale",
        "kind": "tailscale",
        "hostname": hostname,
        "display_name": hostname,
        "dns_name": node.get("dns_name", ""),
        "host": hostname,
        "ip": node.get("ip", ""),
        "port": 22,
        "online": bool(node.get("online", False)),
        "os": node.get("os", "unknown"),
        "user": override.get("user") or defaults.get("target_user", "root"),
        "note": override.get("note", ""),
        "tags": merged_tags,
        "hidden": bool(override.get("hidden", False)),
        "network": "tailscale",
        "connect_mode": "jumpbox",
    }


def _slugify(value: str) -> str:
    """Generate a filesystem-safe identifier."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()
    return slug or "server"


def load_manual_servers() -> list[dict[str, Any]]:
    """Load user-managed servers from YAML."""
    if not MANUAL_SERVERS_PATH.exists():
        return []

    try:
        content = _read_yaml(MANUAL_SERVERS_PATH) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error("Failed to load manual servers: %s", exc)
        return []

    if not isinstance(content, dict):
        return []

    servers = content.get("manual_servers", []) or []
    if not isinstance(servers, list):
        return []

    result: list[dict[str, Any]] = []
    for item in servers:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name") or item.get("hostname") or item.get("host") or "")
        host = str(item.get("host") or "").strip()
        if not name or not host:
            continue

        identifier = str(item.get("id") or _slugify(name))
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            tags = []

        result.append(
            {
                "id": f"manual:{identifier}",
                "raw_id": identifier,
                "source": "manual",
                "kind": "manual",
                "hostname": identifier,
                "display_name": name,
                "dns_name": "",
                "host": host,
                "ip": str(item.get("ip") or host),
                "port": int(item.get("port") or 22),
                "online": None,
                "os": str(item.get("os") or "unknown"),
                "user": str(item.get("user") or "root"),
                "note": str(item.get("note") or ""),
                "tags": [str(tag) for tag in tags],
                "hidden": bool(item.get("hidden", False)),
                "network": str(item.get("network") or "private"),
                "connect_mode": str(item.get("connect_mode") or "direct"),
                "session_name": str(item.get("session_name") or identifier),
            }
        )

    return result


def save_manual_servers(servers: list[dict[str, Any]]) -> None:
    """Persist manual servers to YAML."""
    payload: list[dict[str, Any]] = []
    for server in servers:
        payload.append(
            {
                "id": str(server.get("raw_id") or _slugify(server.get("display_name", ""))),
                "name": str(server.get("display_name") or server.get("hostname") or ""),
                "host": str(server.get("host") or ""),
                "port": int(server.get("port") or 22),
                "user": str(server.get("user") or "root"),
                "note": str(server.get("note") or ""),
                "tags": list(server.get("tags") or []),
                "network": str(server.get("network") or "private"),
                "os": str(server.get("os") or "unknown"),
                "hidden": bool(server.get("hidden", False)),
                "connect_mode": str(server.get("connect_mode") or "direct"),
                "session_name": str(
                    server.get("session_name")
                    or server.get("raw_id")
                    or _slugify(server.get("display_name", ""))
                ),
            }
        )

    _write_yaml(MANUAL_SERVERS_PATH, {"manual_servers": payload})
