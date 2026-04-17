"""封装 tmux session 查询。"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def list_sessions() -> list[dict[str, Any]]:
    """返回当前 tmux session 列表。"""
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-sessions",
                "-F",
                "#{session_name}|#{session_created}|#{?session_attached,1,0}",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        logger.error("tmux list-sessions failed: %s", exc)
        return []

    if result.returncode != 0:
        return []

    sessions: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        try:
            sessions.append(
                {
                    "name": parts[0],
                    "created_at": int(parts[1]),
                    "attached": parts[2] == "1",
                }
            )
        except ValueError:
            logger.warning("Invalid tmux session row: %s", line)

    return sessions


def session_exists(name: str) -> bool:
    """检查指定 session 是否存在。"""
    return any(session["name"] == name for session in list_sessions())
