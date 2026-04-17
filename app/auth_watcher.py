"""监控 Tailscale 认证 URL 文件。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

AUTH_DIR = Path("/tmp/bastion-auth")


def get_pending_auth_urls() -> dict[str, str]:
    """读取全部待认证 URL。"""
    if not AUTH_DIR.exists():
        return {}

    pending: dict[str, str] = {}
    try:
        for url_file in AUTH_DIR.glob("*.url"):
            try:
                content = url_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning("Failed to read auth file %s: %s", url_file, exc)
                continue

            if content.startswith("https://login.tailscale.com/"):
                pending[url_file.stem] = content
    except OSError as exc:
        logger.error("Failed to scan auth directory: %s", exc)

    return pending


def clear_auth_url(hostname: str) -> bool:
    """清理指定 hostname 的认证 URL 记录。"""
    if not hostname or "/" in hostname or "\\" in hostname or ".." in hostname:
        return False

    path = AUTH_DIR / f"{hostname}.url"
    try:
        if path.exists():
            path.unlink()
        return True
    except OSError as exc:
        logger.error("Failed to clear auth URL for %s: %s", hostname, exc)
        return False
