"""加载 YAML 配置。"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path("/etc/bastion")

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
    """深度合并字典。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings() -> dict[str, Any]:
    """加载 settings.yaml，并与默认值合并。"""
    path = CONFIG_DIR / "settings.yaml"
    if not path.exists():
        logger.warning("%s not found, using defaults", path)
        return copy.deepcopy(DEFAULT_SETTINGS)

    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(content, dict):
            logger.warning("%s has invalid structure, using defaults", path)
            return copy.deepcopy(DEFAULT_SETTINGS)
        return _deep_merge(copy.deepcopy(DEFAULT_SETTINGS), content)
    except (OSError, yaml.YAMLError) as exc:
        logger.error("Failed to load settings: %s", exc)
        return copy.deepcopy(DEFAULT_SETTINGS)


def load_overrides() -> dict[str, Any]:
    """加载 overrides.yaml 中的 overrides 字典。"""
    path = CONFIG_DIR / "overrides.yaml"
    if not path.exists():
        return {}

    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
    """合并节点状态、用户覆盖项和全局默认值。"""
    hostname = str(node.get("hostname", "unknown"))
    override = overrides.get(hostname, {}) or {}
    defaults = settings.get("defaults", {}) or {}

    merged_tags = list(
        dict.fromkeys((node.get("tags") or []) + (override.get("tags") or []))
    )

    return {
        "hostname": hostname,
        "dns_name": node.get("dns_name", ""),
        "ip": node.get("ip", ""),
        "online": bool(node.get("online", False)),
        "os": node.get("os", "unknown"),
        "user": override.get("user") or defaults.get("target_user", "root"),
        "note": override.get("note", ""),
        "tags": merged_tags,
        "hidden": bool(override.get("hidden", False)),
    }
