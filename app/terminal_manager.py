"""Process-backed web terminal sessions."""

from __future__ import annotations

import os
import pty
import re
import select
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class TerminalSession:
    """Mutable terminal session state."""

    session_id: str
    command: str
    master_fd: int
    process: subprocess.Popen[str]
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    closed: bool = False


class TerminalManager:
    """Manage pseudo-terminal subprocesses for web clients."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = threading.Lock()

    def create(self, command: str) -> TerminalSession:
        """Spawn a new PTY subprocess."""
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        process = subprocess.Popen(
            ["/bin/bash", "-lc", command],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=True,
            close_fds=True,
            preexec_fn=os.setsid,
            env=env,
        )
        os.close(slave_fd)

        session = TerminalSession(
            session_id=uuid.uuid4().hex,
            command=command,
            master_fd=master_fd,
            process=process,
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> TerminalSession | None:
        """Return a tracked session by id."""
        with self._lock:
            return self._sessions.get(session_id)

    def read(self, session_id: str) -> str:
        """Read currently available output from a PTY."""
        session = self.get(session_id)
        if session is None:
            return ""

        chunks: list[str] = []
        while True:
            ready, _, _ = select.select([session.master_fd], [], [], 0)
            if not ready:
                break
            try:
                data = os.read(session.master_fd, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(self._sanitize_output(data.decode("utf-8", errors="replace")))

        session.updated_at = time.time()
        if session.process.poll() is not None:
            session.closed = True

        return "".join(chunks)

    @staticmethod
    def _sanitize_output(output: str) -> str:
        """Strip ANSI escape/control sequences for the simplified web terminal."""
        ansi_csi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        ansi_osc = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
        ansi_ss3 = re.compile(r"\x1bO.")
        other_esc = re.compile(r"\x1b[@-_]")

        cleaned = ansi_osc.sub("", output)
        cleaned = ansi_csi.sub("", cleaned)
        cleaned = ansi_ss3.sub("", cleaned)
        cleaned = other_esc.sub("", cleaned)
        cleaned = cleaned.replace("\r", "")
        return cleaned

    def write(self, session_id: str, data: str) -> bool:
        """Write input bytes to a PTY."""
        session = self.get(session_id)
        if session is None or session.closed:
            return False

        try:
            os.write(session.master_fd, data.encode("utf-8"))
        except OSError:
            session.closed = True
            return False

        session.updated_at = time.time()
        return True

    def close(self, session_id: str) -> None:
        """Terminate and forget a session."""
        with self._lock:
            session = self._sessions.pop(session_id, None)

        if session is None:
            return

        session.closed = True
        try:
            if session.process.poll() is None:
                os.killpg(os.getpgid(session.process.pid), signal.SIGTERM)
        except OSError:
            pass

        try:
            os.close(session.master_fd)
        except OSError:
            pass

    def cleanup(self, max_age_seconds: int = 3600) -> None:
        """Drop stale terminal sessions."""
        now = time.time()
        stale_ids: list[str] = []
        with self._lock:
            for session_id, session in self._sessions.items():
                exited = session.process.poll() is not None
                expired = now - session.updated_at > max_age_seconds
                if exited or expired:
                    stale_ids.append(session_id)

        for session_id in stale_ids:
            self.close(session_id)
