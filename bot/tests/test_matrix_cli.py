"""Unit tests for ``bot._matrix_cli``.

All subprocess invocations are mocked via ``unittest.mock`` so the test
suite never spawns a real process and never touches the network.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import pytest

from bot import _matrix_cli
from bot._matrix_cli import Listener, send_message, start_listener

if TYPE_CHECKING:
    from collections.abc import Iterable


def _make_proc(stdout: MagicMock | None = None) -> MagicMock:
    """Return a mock ``Popen`` whose ``.stdout`` is the given value."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = stdout
    proc.pid = 4242
    proc.returncode = 0
    wait_mock: MagicMock = cast("MagicMock", proc.wait)
    wait_mock.return_value = 0
    return proc


class TestListener:
    """Tests for the thin ``Listener`` wrapper class."""

    def test_init_stores_proc_and_lines(self) -> None:
        """Both constructor arguments must be retained on the instance."""
        proc = _make_proc()
        lines: Iterable[str] = iter([])
        listener = Listener(proc, lines)
        assert listener.proc is proc
        assert listener.lines is lines

    def test_wait_delegates_to_proc(self) -> None:
        """``Listener.wait`` must return whatever ``proc.wait`` returns."""
        proc = _make_proc()
        wait_mock: MagicMock = cast("MagicMock", proc.wait)
        wait_mock.return_value = 7
        listener = Listener(proc, iter([]))
        assert listener.wait() == 7
        assert_called: MagicMock = cast("MagicMock", wait_mock.assert_called_once_with)
        assert_called()

    def test_returncode_proxies_to_proc(self) -> None:
        """``returncode`` must reflect the current ``proc.returncode``."""
        proc = _make_proc()
        proc.returncode = 13
        listener = Listener(proc, iter([]))
        assert listener.returncode == 13

    def test_returncode_is_none_while_running(self) -> None:
        """``proc.returncode`` is ``None`` until the process exits."""
        proc = _make_proc()
        proc.returncode = None
        listener = Listener(proc, iter([]))
        assert listener.returncode is None


class TestStartListener:
    """Tests for ``start_listener``."""

    def test_spawns_popen_with_expected_argv(self) -> None:
        """``subprocess.Popen`` must be called with the matrix-cli listen argv."""
        stdout_mock = MagicMock()
        proc = _make_proc(stdout=stdout_mock)
        with patch("bot._matrix_cli.subprocess.Popen", return_value=proc) as popen_mock:
            listener = start_listener()
        popen_mock.assert_called_once()
        argv: list[str] = cast("list[str]", popen_mock.call_args.args[0])
        assert argv[0] == _matrix_cli.MC_BIN
        assert "--mode" in argv
        assert "listen" in argv
        assert "--json" in argv
        assert listener.proc is proc

    def test_uses_line_buffered_text_mode(self) -> None:
        """``Popen`` must be invoked with ``text=True`` and ``bufsize=1``."""
        stdout_mock = MagicMock()
        proc = _make_proc(stdout=stdout_mock)
        with patch("bot._matrix_cli.subprocess.Popen", return_value=proc) as popen_mock:
            _ = start_listener()
        kwargs: dict[str, object] = cast(
            "dict[str, object]", popen_mock.call_args.kwargs
        )
        assert kwargs["text"] is True
        assert kwargs["bufsize"] == 1
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is sys.stderr

    def test_logs_pid_after_spawn(self) -> None:
        """The PID of the spawned process must be logged at INFO level."""
        stdout_mock = MagicMock()
        proc = _make_proc(stdout=stdout_mock)
        proc.pid = 9999
        with (
            patch("bot._matrix_cli.subprocess.Popen", return_value=proc),
            patch.object(_matrix_cli.log, "info") as info_mock,
        ):
            _ = start_listener()
        info_mock.assert_any_call("matrix-cli PID %d", 9999)

    def test_exits_when_binary_missing(self) -> None:
        """``FileNotFoundError`` from ``Popen`` must trigger ``sys.exit(1)``."""
        with (
            patch(
                "bot._matrix_cli.subprocess.Popen",
                side_effect=FileNotFoundError,
            ),
            patch.object(sys, "exit", side_effect=SystemExit(1)) as exit_mock,
            patch.object(_matrix_cli.log, "critical") as critical_mock,
        ):
            with pytest.raises(SystemExit):
                _ = start_listener()
        critical_mock.assert_called_once()
        exit_mock.assert_called_once_with(1)

    def test_raises_runtime_error_if_stdout_is_none(self) -> None:
        """``RuntimeError`` must be raised if ``Popen.stdout`` is ``None``."""
        proc = _make_proc(stdout=None)
        with (
            patch("bot._matrix_cli.subprocess.Popen", return_value=proc),
            pytest.raises(RuntimeError, match=r"proc.stdout is None"),
        ):
            _ = start_listener()


class TestSendMessage:
    """Tests for ``send_message``."""

    def test_sends_without_room(self) -> None:
        """Without a ``room`` argument, no ``--rooms`` flag is passed."""
        with patch("bot._matrix_cli.subprocess.run") as run_mock:
            send_message("hello world")
        run_mock.assert_called_once()
        argv: list[str] = cast("list[str]", run_mock.call_args.args[0])
        assert "--rooms" not in argv
        assert "--message" in argv
        msg_index: int = argv.index("--message")
        assert argv[msg_index + 1] == "hello world"

    def test_sends_with_room(self) -> None:
        """With ``room`` set, ``--rooms <room>`` must be appended to argv."""
        with patch("bot._matrix_cli.subprocess.run") as run_mock:
            send_message("hi", room="!room:example.com")
        argv = cast("list[str]", run_mock.call_args.args[0])
        idx: int = argv.index("--rooms")
        assert argv[idx + 1] == "!room:example.com"

    def test_uses_timeout_and_capture(self) -> None:
        """The subprocess call must use a 30s timeout and capture stdout/stderr."""
        with patch("bot._matrix_cli.subprocess.run") as run_mock:
            send_message("ok")
        kwargs: dict[str, object] = cast("dict[str, object]", run_mock.call_args.kwargs)
        assert kwargs["timeout"] == 30
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is True

    @pytest.mark.parametrize(
        "stderr_value",
        [
            b"binary error",
            "text error",
            None,
        ],
        ids=["bytes", "str", "none"],
    )
    def test_logs_on_called_process_error(
        self,
        stderr_value: bytes | str | None,
    ) -> None:
        """``CalledProcessError`` from ``run`` must be logged and not propagated."""
        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/local/bin/matrix-cli"],
            stderr=stderr_value,
        )
        with (
            patch("bot._matrix_cli.subprocess.run", side_effect=exc),
            patch.object(_matrix_cli.log, "exception") as exc_mock,
        ):
            send_message("oops")
        exc_mock.assert_called_once()

    def test_logs_on_timeout(self) -> None:
        """``TimeoutExpired`` from ``run`` must be logged as an exception."""
        exc = subprocess.TimeoutExpired(cmd=["mc"], timeout=30)
        with (
            patch("bot._matrix_cli.subprocess.run", side_effect=exc),
            patch.object(_matrix_cli.log, "exception") as exc_mock,
        ):
            send_message("stuck")
        exc_mock.assert_called_once()
        first_arg: str = cast("str", exc_mock.call_args.args[0])
        assert "timed out" in first_arg

    def test_binary_stderr_is_decoded_with_replace(self) -> None:
        """Binary ``stderr`` must be decoded with the ``replace`` error handler."""
        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/local/bin/matrix-cli"],
            stderr=b"\xff\xfe bad bytes",
        )
        with (
            patch("bot._matrix_cli.subprocess.run", side_effect=exc),
            patch.object(_matrix_cli.log, "exception") as exc_mock,
        ):
            send_message("oops")
        logged_text: str = cast("str", exc_mock.call_args.args[1])
        assert "\ufffd" in logged_text
