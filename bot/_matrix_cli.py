"""Thin wrapper around the matrix-cli subprocess.

All subprocess invocations of ``matrix-cli`` live in this module. Keeping them
in one place makes the security-relevant claim easy to audit: every command
argument is either a hard-coded constant or a value explicitly constructed
by the caller from validated inputs — no user-supplied strings are ever
forwarded to the shell. The ``S603`` lint suppression is scoped to this file
so that any new subprocess call outside it would still be flagged by ruff.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger("matrix-mikrotik-bot.matrix-cli")

MC_BIN: str = "/usr/local/bin/matrix-cli"


class Listener:
    """Context manager wrapping a running matrix-cli listen process.

    Exposes the JSON-line stream as ``self.lines`` and the underlying
    process handle as ``self.proc`` for back-off accounting.
    """

    def __init__(self, proc: subprocess.Popen[str], lines: Iterable[str]) -> None:
        """Store the process handle and its stdout iterator."""
        self.proc: subprocess.Popen[str] = proc
        self.lines: Iterable[str] = lines

    def wait(self) -> int:
        """Block until matrix-cli exits and return its return code."""
        return self.proc.wait()

    @property
    def returncode(self) -> int | None:
        """Return the matrix-cli exit code, or ``None`` if still running."""
        return self.proc.returncode


def start_listener() -> Listener:
    """Spawn matrix-cli in listen mode and return a ``Listener`` handle.

    The returned object's ``lines`` attribute yields one JSON string per
    event published by matrix-cli. The caller is responsible for consuming
    the iterator and for restarting on EOF (see ``listen_loop``).

    Raises:
        SystemExit: If ``matrix-cli`` is not found on ``PATH``.

    """
    try:
        proc: subprocess.Popen[str] = subprocess.Popen(
            [MC_BIN, "--mode", "listen", "--json"],
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        log.critical("%s not found — is the base image correct?", MC_BIN)
        sys.exit(1)

    if proc.stdout is None:
        msg = "proc.stdout is None"
        raise RuntimeError(msg)

    log.info("matrix-cli PID %d", proc.pid)
    lines: Iterable[str] = cast("Iterable[str]", proc.stdout)
    return Listener(proc, lines)


def send_message(text: str, room: str | None = None) -> None:
    """Send a plain-text message to Matrix via matrix-cli.

    Args:
        text: Message body.
        room: Matrix room ID or alias override. If ``None`` the matrix-cli
            default room is used.

    The ``subprocess.run`` call below is intentional: ``matrix-cli`` is a
    third-party binary invoked with an argument vector, never a shell
    string, and all inputs are constructed from validated configuration
    rather than raw user input.

    """
    cmd: list[str] = [MC_BIN, "--mode", "send", "--json", "--message", text]
    if room:
        cmd += ["--rooms", room]
    try:
        _ = subprocess.run(cmd, check=True, timeout=30, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr: bytes | str | None = cast("bytes | str | None", exc.stderr)
        stderr_bytes = stderr if isinstance(stderr, bytes) else b""
        log.exception(
            "Failed to send Matrix message: %s",
            stderr_bytes.decode(errors="replace"),
        )
    except subprocess.TimeoutExpired:
        log.exception("matrix-cli send timed out")
