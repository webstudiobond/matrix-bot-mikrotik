"""Unit tests for ``bot.bot``.

All network and process I/O is mocked via ``unittest.mock`` and
``responses`` so the test suite never performs real requests.
"""

from __future__ import annotations

import json
import ssl
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import librouteros.exceptions
import pytest
import requests
import responses
import yaml

from bot.bot import (
    MAX_MESSAGE_LENGTH,
    BotConfig,
    RouterOSError,
    RoutersMap,
    build_help,
    dispatch,
    execute_api,
    execute_command,
    execute_rest,
    load_config,
    parse_event,
    parse_ros_kv,
    ros_path_to_api,
    validate_routers,
    validate_top_level,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

VALID_ROUTER: dict[str, object] = {
    "host": "router.example.com",
    "port": 443,
    "username": "admin",
    "password": "secret",
    "tls_verify": True,
}


def _valid_config() -> dict[str, object]:
    """Return a fully valid configuration dict for happy-path tests."""
    return {
        "bot_user": "@bot:example.com",
        "command_room": "!cmd:example.com",
        "admin_room": "!admin:example.com",
        "allowed_users": ["@alice:example.com", "@bob:example.com"],
        "allowed_commands": ["system/resource", "ip/address"],
        "routers": {"core-01": dict(VALID_ROUTER)},
    }


def _write_config(tmp_path: Path, data: dict[str, object] | str) -> Path:
    """Write ``data`` to a temporary YAML file and return its path."""
    path = tmp_path / "config.yaml"
    if isinstance(data, str):
        written: int = path.write_text(data, encoding="utf-8")
    else:
        written = path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert written >= 0
    return path


# ---------------------------------------------------------------------------
# BotConfig
# ---------------------------------------------------------------------------


class TestBotConfig:
    """The ``BotConfig`` dataclass must be frozen and have all declared fields."""

    def test_frozen_rejects_mutation(self) -> None:
        """Mutating a frozen ``BotConfig`` must raise ``FrozenInstanceError``."""
        cfg = BotConfig(
            bot_user="b",
            command_room="c",
            admin_room="a",
            allowed_users=["u"],
            allowed_commands=["p"],
            routers={},
        )
        with pytest.raises(FrozenInstanceError):
            cast("object", cfg).__setattr__("bot_user", "x")


# ---------------------------------------------------------------------------
# validate_top_level
# ---------------------------------------------------------------------------


class TestValidateTopLevel:
    """Tests for ``validate_top_level``."""

    @pytest.mark.parametrize(
        "missing_field",
        ["bot_user", "command_room", "admin_room"],
    )
    def test_missing_top_level_field_exits(self, missing_field: str) -> None:
        """Each top-level field must be present and non-empty."""
        cfg = _valid_config()
        cfg[missing_field] = ""
        result: tuple[str, str, str, list[str], list[str]] = ("", "", "", [], [])
        with pytest.raises(SystemExit) as exc_info:
            result = validate_top_level(cfg)
        assert exc_info.value.code == 1
        assert result == ("", "", "", [], [])

    def test_empty_allowed_users_exits(self) -> None:
        """``allowed_users`` must contain at least one user."""
        cfg = _valid_config()
        cfg["allowed_users"] = []
        result: tuple[str, str, str, list[str], list[str]] = ("", "", "", [], [])
        with pytest.raises(SystemExit) as exc_info:
            result = validate_top_level(cfg)
        assert exc_info.value.code == 1
        assert result == ("", "", "", [], [])

    def test_empty_allowed_commands_exits(self) -> None:
        """``allowed_commands`` must contain at least one path."""
        cfg = _valid_config()
        cfg["allowed_commands"] = []
        result: tuple[str, str, str, list[str], list[str]] = ("", "", "", [], [])
        with pytest.raises(SystemExit) as exc_info:
            result = validate_top_level(cfg)
        assert exc_info.value.code == 1
        assert result == ("", "", "", [], [])

    @pytest.mark.parametrize(
        "bad_path",
        ["with spaces", "with;semi", "with$dollar", "with.dot", ""],
    )
    def test_invalid_allowed_command_format_exits(self, bad_path: str) -> None:
        """``allowed_commands`` entries must match ``[A-Za-z0-9/_-]+``."""
        cfg = _valid_config()
        cfg["allowed_commands"] = [bad_path]
        result: tuple[str, str, str, list[str], list[str]] = ("", "", "", [], [])
        with pytest.raises(SystemExit) as exc_info:
            result = validate_top_level(cfg)
        assert exc_info.value.code == 1
        assert result == ("", "", "", [], [])

    def test_returns_five_tuple_on_valid_input(self) -> None:
        """On success all five values must be returned as a tuple."""
        result = validate_top_level(_valid_config())
        assert result == (
            "@bot:example.com",
            "!cmd:example.com",
            "!admin:example.com",
            ["@alice:example.com", "@bob:example.com"],
            ["system/resource", "ip/address"],
        )


# ---------------------------------------------------------------------------
# validate_routers
# ---------------------------------------------------------------------------


class TestValidateRouters:
    """Tests for ``validate_routers``."""

    def test_empty_routers_exits(self, tmp_path: Path) -> None:
        """An empty routers mapping must trigger ``sys.exit(1)``."""
        result: RoutersMap = {}
        with pytest.raises(SystemExit) as exc_info:
            result = validate_routers({"routers": {}}, tmp_path)
        assert exc_info.value.code == 1
        assert result == {}

    def test_missing_required_key_exits(self, tmp_path: Path) -> None:
        """Any router missing ``host/port/username/password`` must abort."""
        bad = dict(VALID_ROUTER)
        del bad["password"]
        result = {}
        with pytest.raises(SystemExit) as exc_info:
            result = validate_routers({"routers": {"core-01": bad}}, tmp_path)
        assert exc_info.value.code == 1
        assert result == {}

    @pytest.mark.parametrize(
        "bad_rid",
        ["with space", "with;semi", "x" * 65, ""],
    )
    def test_invalid_router_id_exits(self, bad_rid: str, tmp_path: Path) -> None:
        """Router IDs must match ``[A-Za-z0-9_-]{1,64}``."""
        result = {}
        with pytest.raises(SystemExit) as exc_info:
            result = validate_routers(
                {"routers": {bad_rid: dict(VALID_ROUTER)}},
                tmp_path,
            )
        assert exc_info.value.code == 1
        assert result == {}

    def test_valid_input_returns_routers(self, tmp_path: Path) -> None:
        """On success, the routers mapping is returned unchanged."""
        result = validate_routers(
            {"routers": {"core-01": dict(VALID_ROUTER)}},
            tmp_path,
        )
        assert "core-01" in result


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """End-to-end tests for ``load_config``."""

    def test_missing_file_exits(self, tmp_path: Path) -> None:
        """If the path does not exist, ``sys.exit(1)`` is called."""
        result: BotConfig = BotConfig(
            bot_user="",
            command_room="",
            admin_room="",
            allowed_users=[],
            allowed_commands=[],
            routers={},
        )
        with pytest.raises(SystemExit) as exc_info:
            result = load_config(tmp_path / "nope.yaml")
        assert exc_info.value.code == 1
        assert result.bot_user == ""

    def test_yaml_parse_error_exits(self, tmp_path: Path) -> None:
        """Invalid YAML must trigger ``sys.exit(1)``."""
        path = _write_config(tmp_path, ":\n  bad: [unterminated")
        result = BotConfig(
            bot_user="",
            command_room="",
            admin_room="",
            allowed_users=[],
            allowed_commands=[],
            routers={},
        )
        with pytest.raises(SystemExit) as exc_info:
            result = load_config(path)
        assert exc_info.value.code == 1
        assert result.bot_user == ""

    @pytest.mark.parametrize(
        "empty_yaml",
        ["", "just a string", "- a\n- list", "42", "null"],
    )
    def test_non_mapping_yaml_exits(self, empty_yaml: str, tmp_path: Path) -> None:
        """A YAML payload that is not a mapping must be rejected."""
        path = _write_config(tmp_path, empty_yaml)
        result = BotConfig(
            bot_user="",
            command_room="",
            admin_room="",
            allowed_users=[],
            allowed_commands=[],
            routers={},
        )
        with pytest.raises(SystemExit) as exc_info:
            result = load_config(path)
        assert exc_info.value.code == 1
        assert result.bot_user == ""

    def test_happy_path_returns_bot_config(self, tmp_path: Path) -> None:
        """A fully valid file yields a populated ``BotConfig``."""
        path = _write_config(tmp_path, _valid_config())
        cfg = load_config(path)
        assert isinstance(cfg, BotConfig)
        assert cfg.bot_user == "@bot:example.com"
        assert cfg.command_room == "!cmd:example.com"
        assert cfg.admin_room == "!admin:example.com"
        assert cfg.allowed_users == ["@alice:example.com", "@bob:example.com"]
        assert cfg.allowed_commands == ["system/resource", "ip/address"]
        assert "core-01" in cfg.routers


# ---------------------------------------------------------------------------
# parse_ros_kv
# ---------------------------------------------------------------------------


class TestParseRosKv:
    """Tests for the ``=key=value`` RouterOS argument parser."""

    def test_single_pair(self) -> None:
        """A single ``=k=v`` token must parse to ``{k: v}``."""
        assert parse_ros_kv("=name=eth0") == {"name": "eth0"}

    def test_multiple_pairs(self) -> None:
        """Several tokens are merged into one dict, in input order."""
        assert parse_ros_kv("=a=1 =b=2 =c=3") == {"a": "1", "b": "2", "c": "3"}

    def test_empty_value(self) -> None:
        """``=key=`` (no value) must yield an empty string."""
        assert parse_ros_kv("=flag=") == {"flag": ""}

    def test_empty_input(self) -> None:
        """An empty string returns an empty dict, never raises."""
        assert parse_ros_kv("") == {}

    def test_value_with_special_characters(self) -> None:
        """Values may contain dots, colons and slashes."""
        assert parse_ros_kv("=addr=10.0.0.1/24") == {"addr": "10.0.0.1/24"}


# ---------------------------------------------------------------------------
# parse_event
# ---------------------------------------------------------------------------


class TestParseEvent:
    """Tests for the matrix-cli JSON event parser."""

    def test_valid_text_message(self) -> None:
        """A complete ``m.room.message/m.text`` event yields all three fields."""
        line = json.dumps(
            {
                "type": "m.room.message",
                "room_id": "!r:example.com",
                "sender": "@u:example.com",
                "content": {"msgtype": "m.text", "body": "  hello  "},
            },
        )
        assert parse_event(line) == ("!r:example.com", "@u:example.com", "hello")

    @pytest.mark.parametrize(
        "line",
        [
            "not json at all",
            "",
            "[1, 2, 3]",
            '"just a string"',
            "42",
            "null",
        ],
    )
    def test_invalid_or_non_object_json_returns_none_tuple(self, line: str) -> None:
        """Any unparseable or non-object JSON yields ``(None, None, None)``."""
        assert parse_event(line) == (None, None, None)

    def test_listening_status_is_skipped(self) -> None:
        """A ``status: listening`` event from matrix-cli is ignored."""
        line = json.dumps({"status": "listening"})
        assert parse_event(line) == (None, None, None)

    @pytest.mark.parametrize(
        "event_type",
        ["m.room.member", "m.room.power_levels", "m.typing", None],
    )
    def test_non_message_events_are_skipped(self, event_type: str | None) -> None:
        """Events whose ``type`` is not ``m.room.message`` are ignored."""
        payload: dict[str, object] = {
            "type": event_type,
            "room_id": "!r:example.com",
            "sender": "@u:example.com",
            "content": {"msgtype": "m.text", "body": "x"},
        }
        line = json.dumps(payload)
        assert parse_event(line) == (None, None, None)

    def test_non_text_msgtype_is_skipped(self) -> None:
        """Messages of type other than ``m.text`` are ignored."""
        line = json.dumps(
            {
                "type": "m.room.message",
                "room_id": "!r:example.com",
                "sender": "@u:example.com",
                "content": {"msgtype": "m.image", "body": "img.png"},
            },
        )
        assert parse_event(line) == (None, None, None)

    def test_missing_optional_fields_yield_none(self) -> None:
        """Missing ``room_id`` / ``sender`` must produce ``None`` for them."""
        line = json.dumps(
            {
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "x"},
            },
        )
        room_id, sender, body = parse_event(line)
        assert room_id is None
        assert sender is None
        assert body == "x"


# ---------------------------------------------------------------------------
# ros_path_to_api
# ---------------------------------------------------------------------------


class TestRosPathToApi:
    """Tests for ``ros_path_to_api``."""

    def test_single_segment(self) -> None:
        """A single-segment path is converted to a one-tuple."""
        assert ros_path_to_api("ip") == ("/ip",)

    def test_nested_path(self) -> None:
        """Slashes separate path words; the first is prefixed with ``/``."""
        assert ros_path_to_api("ip/dhcp-server/lease") == (
            "/ip",
            "dhcp-server",
            "lease",
        )

    def test_leading_and_trailing_slashes_are_stripped(self) -> None:
        """Slashes at the boundaries are stripped before splitting."""
        assert ros_path_to_api("/system/resource/") == ("/system", "resource")

    @pytest.mark.parametrize("empty", ["", "/", "///"])
    def test_empty_path_raises(self, empty: str) -> None:
        """An empty or all-slash path raises ``RouterOSError``."""
        path_words: tuple[str, ...] = ()
        with pytest.raises(RouterOSError, match="Empty API path"):
            path_words = ros_path_to_api(empty)
        assert path_words == ()


# ---------------------------------------------------------------------------
# execute_command
# ---------------------------------------------------------------------------


class TestExecuteCommand:
    """Tests for the transport-routing helper ``execute_command``."""

    @pytest.mark.parametrize("port", [80, 443])
    def test_rest_ports_route_to_execute_rest(self, port: int) -> None:
        """Ports 80 and 443 must select the REST transport."""
        cfg = dict(VALID_ROUTER, port=port)
        with (
            patch("bot.bot.execute_rest", return_value="ok") as rest_mock,
            patch("bot.bot.execute_api") as api_mock,
        ):
            result = execute_command(cfg, "system/resource")
        assert result == "ok"
        rest_mock.assert_called_once_with(cfg, "system/resource")
        api_mock.assert_not_called()

    @pytest.mark.parametrize("port", [8728, 8729, 9999])
    def test_non_rest_ports_route_to_execute_api(self, port: int) -> None:
        """Any port other than 80/443 selects the librouteros transport."""
        cfg = dict(VALID_ROUTER, port=port)
        with (
            patch("bot.bot.execute_api", return_value="ok") as api_mock,
            patch("bot.bot.execute_rest") as rest_mock,
        ):
            result = execute_command(cfg, "system/resource")
        assert result == "ok"
        api_mock.assert_called_once_with(cfg, "system/resource")
        rest_mock.assert_not_called()


# ---------------------------------------------------------------------------
# execute_rest
# ---------------------------------------------------------------------------


class TestExecuteRest:
    """Tests for the REST transport."""

    def test_get_returns_formatted_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A successful GET returns a pretty-printed JSON string."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        with responses.RequestsMock() as rsps:
            assert (
                rsps.add(
                    responses.GET,
                    "https://router.example.com:443/rest/system/resource",
                    json={"version": "7.10"},
                    status=200,
                )
                is not None
            )
            result = execute_rest(VALID_ROUTER, "system/resource")
        assert json.loads(result) == {"version": "7.10"}
        assert "\n" in result  # pretty-printed

    def test_get_via_http_for_port_80(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Port 80 must use the ``http://`` scheme, not ``https://``."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        cfg = dict(VALID_ROUTER, port=80, tls_verify=False)
        with responses.RequestsMock() as rsps:
            assert (
                rsps.add(
                    responses.GET,
                    "http://router.example.com:80/rest/system/resource",
                    json={"ok": True},
                    status=200,
                )
                is not None
            )
            result = execute_rest(cfg, "system/resource")
        assert json.loads(result) == {"ok": True}

    @pytest.mark.parametrize(
        "bad_path",
        ["with;semi", "with$dollar", "with.dot", "with&amp"],
    )
    def test_invalid_api_path_raises(
        self,
        bad_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """API paths must match ``[A-Za-z0-9/_-]+``; else ``RouterOSError``."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        with pytest.raises(RouterOSError, match="Invalid API path") as exc_info:
            execute_rest(VALID_ROUTER, bad_path)
        assert "Invalid API path" in str(exc_info.value)

    def test_write_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Write attempts without ``ALLOW_WRITES=true`` must be rejected."""
        monkeypatch.delenv("ALLOW_WRITES", raising=False)
        with pytest.raises(
            RouterOSError, match="Write operations are disabled"
        ) as exc_info:
            execute_rest(VALID_ROUTER, "ip/address =address=10.0.0.1/24")
        assert "Write operations are disabled" in str(exc_info.value)

    def test_write_enabled_posts_with_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With writes enabled, ``=key=value`` becomes a POST with JSON body."""
        monkeypatch.setenv("ALLOW_WRITES", "true")
        with responses.RequestsMock() as rsps:
            assert (
                rsps.add(
                    responses.POST,
                    "https://router.example.com:443/rest/ip/address",
                    json={"status": "created"},
                    status=201,
                )
                is not None
            )
            result = execute_rest(VALID_ROUTER, "ip/address =address=10.0.0.1/24")
            sent = rsps.calls[0].request
            assert sent.method == "POST"
            assert sent.body is not None
            assert json.loads(sent.body) == {"address": "10.0.0.1/24"}
        assert json.loads(result) == {"status": "created"}

    def test_401_maps_to_auth_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An HTTP 401 response becomes a ``RouterOSError`` about credentials."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        with responses.RequestsMock() as rsps:
            assert (
                rsps.add(
                    responses.GET,
                    "https://router.example.com:443/rest/system/resource",
                    body="unauthorized",
                    status=401,
                )
                is not None
            )
            with pytest.raises(
                RouterOSError, match="Authentication failed"
            ) as exc_info:
                execute_rest(VALID_ROUTER, "system/resource")
        assert "Authentication failed" in str(exc_info.value)

    def test_500_maps_to_api_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any non-2xx (other than 401) becomes a generic ``RouterOSError``."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        with responses.RequestsMock() as rsps:
            assert (
                rsps.add(
                    responses.GET,
                    "https://router.example.com:443/rest/system/resource",
                    body="boom",
                    status=500,
                )
                is not None
            )
            with pytest.raises(RouterOSError, match="API error 500") as exc_info:
                execute_rest(VALID_ROUTER, "system/resource")
        assert "API error 500" in str(exc_info.value)

    def test_connection_error_wrapped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ConnectionError`` is converted into ``RouterOSError``."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        with responses.RequestsMock() as rsps:
            assert (
                rsps.add(
                    responses.GET,
                    "https://router.example.com:443/rest/system/resource",
                    body=requests.exceptions.ConnectionError("nope"),
                )
                is not None
            )
            with pytest.raises(RouterOSError, match="Cannot reach") as exc_info:
                execute_rest(VALID_ROUTER, "system/resource")
        assert "Cannot reach" in str(exc_info.value)

    def test_timeout_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``Timeout`` is converted into ``RouterOSError``."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        with responses.RequestsMock() as rsps:
            assert (
                rsps.add(
                    responses.GET,
                    "https://router.example.com:443/rest/system/resource",
                    body=requests.exceptions.Timeout("slow"),
                )
                is not None
            )
            with pytest.raises(RouterOSError, match="Timeout") as exc_info:
                execute_rest(VALID_ROUTER, "system/resource")
        assert "Timeout" in str(exc_info.value)

    def test_long_response_is_truncated(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Responses longer than ``MAX_MESSAGE_LENGTH`` are truncated."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        payload = {"data": "x" * (MAX_MESSAGE_LENGTH + 1000)}
        with responses.RequestsMock() as rsps:
            assert (
                rsps.add(
                    responses.GET,
                    "https://router.example.com:443/rest/system/resource",
                    json=payload,
                    status=200,
                )
                is not None
            )
            result = execute_rest(VALID_ROUTER, "system/resource")
        assert len(result) <= MAX_MESSAGE_LENGTH
        assert "truncated" in result

    def test_non_json_response_falls_back_to_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-JSON 2xx body is returned as plain text."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        with responses.RequestsMock() as rsps:
            assert (
                rsps.add(
                    responses.GET,
                    "https://router.example.com:443/rest/system/resource",
                    body="<html>oops</html>",
                    status=200,
                )
                is not None
            )
            result = execute_rest(VALID_ROUTER, "system/resource")
        assert result == "<html>oops</html>"

    def test_tls_disabled_calls_disable_warnings(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``tls_verify: false`` must trigger ``urllib3.disable_warnings``."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        cfg = dict(VALID_ROUTER, tls_verify=False)
        with (
            patch("bot.bot.urllib3.disable_warnings") as warn_mock,
            responses.RequestsMock() as rsps,
        ):
            assert (
                rsps.add(
                    responses.GET,
                    "https://router.example.com:443/rest/system/resource",
                    json={"ok": True},
                    status=200,
                )
                is not None
            )
            execute_rest(cfg, "system/resource")
        warn_mock.assert_called_once()


# ---------------------------------------------------------------------------
# execute_api
# ---------------------------------------------------------------------------


def _make_librouteros_api(
    menu_iterable: list[dict[str, str]] | None = None,
) -> MagicMock:
    """Return a mock ``librouteros.Api`` with a single ``path`` menu."""
    api = MagicMock(spec=librouteros.Api)
    menu: MagicMock = MagicMock()
    menu_iter: MagicMock = cast("MagicMock", menu.__iter__)
    menu_iter.return_value = iter(menu_iterable or [])
    menu_add: MagicMock = cast("MagicMock", menu.add)
    menu_add.return_value = iter([])
    api_path: MagicMock = cast("MagicMock", api.path)
    api_path.return_value = menu
    api.close = MagicMock()
    return api


class TestExecuteApi:
    """Tests for the librouteros transport."""

    def test_read_returns_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A read against the API returns the menu list as JSON."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        api = _make_librouteros_api([{"name": "ether1"}, {"name": "ether2"}])
        with patch("bot.bot.librouteros.connect", return_value=api):
            result = execute_api(VALID_ROUTER, "ip/address")
        data: list[dict[str, str]] = cast("list[dict[str, str]]", json.loads(result))
        assert data == [{"name": "ether1"}, {"name": "ether2"}]
        api_close: MagicMock = cast("MagicMock", api.close)
        api_close.assert_called_once_with()

    def test_tls_path_uses_ssl_wrapper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Port 8729 must wrap the socket with an SSL context."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        cfg = dict(VALID_ROUTER, port=8729)
        api = _make_librouteros_api([])
        with (
            patch("bot.bot.librouteros.connect", return_value=api) as connect_mock,
            patch("bot.bot.ssl.create_default_context") as ctx_mock,
        ):
            ctx = MagicMock(spec=ssl.SSLContext)
            ctx_mock.return_value = ctx
            execute_api(cfg, "ip/address")
        kwargs: dict[str, object] = cast(
            "dict[str, object]", connect_mock.call_args.kwargs
        )
        assert "ssl_wrapper" in kwargs
        ctx_wrap: MagicMock = cast("MagicMock", ctx.wrap_socket)
        assert kwargs["ssl_wrapper"] == ctx_wrap

    def test_tls_disabled_relaxes_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``tls_verify: false`` must disable hostname check and verification."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        cfg = dict(VALID_ROUTER, port=8729, tls_verify=False)
        api = _make_librouteros_api([])
        with (
            patch("bot.bot.librouteros.connect", return_value=api),
            patch("bot.bot.ssl.create_default_context") as ctx_mock,
        ):
            ctx = MagicMock(spec=ssl.SSLContext)
            ctx_mock.return_value = ctx
            execute_api(cfg, "ip/address")
        ctx_ssl: ssl.SSLContext = cast("ssl.SSLContext", ctx)
        assert ctx_ssl.check_hostname is False
        assert ctx_ssl.verify_mode == ssl.CERT_NONE

    def test_trap_error_raises_router_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``librouteros.exceptions.TrapError`` becomes ``RouterOSError``."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        with (
            patch(
                "bot.bot.librouteros.connect",
                side_effect=librouteros.exceptions.TrapError("permission denied"),
            ),
            pytest.raises(RouterOSError, match="Authentication failed"),
        ):
            execute_api(VALID_ROUTER, "ip/address")

    def test_os_error_raises_router_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ``OSError`` (network unreachable) becomes ``RouterOSError``."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        with (
            patch(
                "bot.bot.librouteros.connect",
                side_effect=OSError("no route to host"),
            ),
            pytest.raises(RouterOSError, match="Cannot reach"),
        ):
            execute_api(VALID_ROUTER, "ip/address")

    def test_write_disabled_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Write attempts without ``ALLOW_WRITES=true`` must be rejected."""
        monkeypatch.delenv("ALLOW_WRITES", raising=False)
        with pytest.raises(RouterOSError, match="Write operations are disabled"):
            execute_api(VALID_ROUTER, "ip/address =address=10.0.0.1/24")

    def test_write_calls_menu_add(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With writes enabled, ``=key=value`` calls ``menu.add(**kwargs)``."""
        monkeypatch.setenv("ALLOW_WRITES", "true")
        api = _make_librouteros_api([])
        with patch("bot.bot.librouteros.connect", return_value=api):
            execute_api(VALID_ROUTER, "ip/address =address=10.0.0.1/24")
        api_path: MagicMock = cast("MagicMock", api.path)
        menu_mock: MagicMock = cast("MagicMock", api_path.return_value)
        menu_add: MagicMock = cast("MagicMock", menu_mock.add)
        menu_add.assert_called_once_with(address="10.0.0.1/24")

    def test_connection_closed_on_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``conn.close()`` must run even when the path call raises."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        api = _make_librouteros_api([])
        api_path: MagicMock = cast("MagicMock", api.path)
        menu_mock: MagicMock = cast("MagicMock", api_path.return_value)
        menu_iter: MagicMock = cast("MagicMock", menu_mock.__iter__)
        menu_iter.side_effect = RuntimeError("kaboom")
        with (
            patch("bot.bot.librouteros.connect", return_value=api),
            pytest.raises(RuntimeError, match="kaboom"),
        ):
            execute_api(VALID_ROUTER, "ip/address")
        api_close: MagicMock = cast("MagicMock", api.close)
        api_close.assert_called_once_with()

    def test_invalid_api_path_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid characters in the path must raise ``RouterOSError``."""
        monkeypatch.setenv("ALLOW_WRITES", "false")
        with pytest.raises(RouterOSError, match="Invalid API path"):
            execute_api(VALID_ROUTER, "ip/../etc")


# ---------------------------------------------------------------------------
# build_help
# ---------------------------------------------------------------------------


class TestBuildHelp:
    """Tests for the dynamic help text builder."""

    def test_contains_routers_and_commands(self) -> None:
        """Help text must list every router ID and allowed command."""
        cfg = BotConfig(
            bot_user="b",
            command_room="c",
            admin_room="a",
            allowed_users=["u"],
            allowed_commands=["system/resource", "ip/address"],
            routers={"core-01": dict(VALID_ROUTER), "branch-02": dict(VALID_ROUTER)},
        )
        help_text = build_help(cfg)
        assert "core-01" in help_text
        assert "branch-02" in help_text
        assert "system/resource" in help_text
        assert "ip/address" in help_text
        assert "Usage" in help_text
        assert "Examples" in help_text

    def test_write_disabled_message(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without ``ALLOW_WRITES`` the help text shows the disabled notice."""
        monkeypatch.delenv("ALLOW_WRITES", raising=False)
        cfg = BotConfig(
            bot_user="b",
            command_room="c",
            admin_room="a",
            allowed_users=["u"],
            allowed_commands=["p"],
            routers={},
        )
        assert "disabled" in build_help(cfg)
        assert "ALLOW_WRITES=true" in build_help(cfg)

    def test_write_enabled_message(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With ``ALLOW_WRITES=true`` the help text shows the enabled notice."""
        monkeypatch.setenv("ALLOW_WRITES", "true")
        cfg = BotConfig(
            bot_user="b",
            command_room="c",
            admin_room="a",
            allowed_users=["u"],
            allowed_commands=["p"],
            routers={},
        )
        assert "enabled" in build_help(cfg)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _dispatch_cfg() -> BotConfig:
    """Return a small in-memory ``BotConfig`` for dispatch tests."""
    return BotConfig(
        bot_user="@bot:example.com",
        command_room="!cmd:example.com",
        admin_room="!admin:example.com",
        allowed_users=["@alice:example.com"],
        allowed_commands=["system/resource", "ip/address"],
        routers={"core-01": dict(VALID_ROUTER)},
    )


class TestDispatch:
    """Tests for ``dispatch`` - the Layer 0-3 gate pipeline."""

    def test_bot_own_message_is_silent(self) -> None:
        """Messages sent by the bot itself must be silently dropped."""
        response, target = dispatch(
            "hello",
            "!cmd:example.com",
            "@bot:example.com",
            _dispatch_cfg(),
        )
        assert response == ""
        assert target is None

    def test_wrong_room_is_silent(self) -> None:
        """Messages from rooms other than ``command_room`` are silently dropped."""
        response, target = dispatch(
            "hello",
            "!other:example.com",
            "@alice:example.com",
            _dispatch_cfg(),
        )
        assert response == ""
        assert target is None

    def test_unauthorized_user_alerts_admin_room(self) -> None:
        """An unknown sender triggers a security alert to ``admin_room``."""
        response, target = dispatch(
            "!mtik core-01 system/resource",
            "!cmd:example.com",
            "@mallory:example.com",
            _dispatch_cfg(),
        )
        assert "SECURITY WARNING" in response
        assert "mallory" in response
        assert target == "!admin:example.com"

    @pytest.mark.parametrize(
        "trigger",
        ["help", "start", "!mtik help", "!mtik start", " HELP "],
    )
    def test_help_triggers_return_command_room(self, trigger: str) -> None:
        """Help-like messages return the help text addressed to ``command_room``."""
        response, target = dispatch(
            trigger,
            "!cmd:example.com",
            "@alice:example.com",
            _dispatch_cfg(),
        )
        assert "MikroTik Matrix Bot" in response
        assert target == "!cmd:example.com"

    def test_unparseable_body_is_silent(self) -> None:
        """Random text that is not a command must yield an empty response."""
        response, target = dispatch(
            "just chatting",
            "!cmd:example.com",
            "@alice:example.com",
            _dispatch_cfg(),
        )
        assert response == ""
        assert target is None

    def test_unknown_router_returns_error(self) -> None:
        """An unknown router ID produces an error response in command_room."""
        response, target = dispatch(
            "!mtik ghost system/resource",
            "!cmd:example.com",
            "@alice:example.com",
            _dispatch_cfg(),
        )
        assert "Unknown router" in response
        assert "ghost" in response
        assert target == "!cmd:example.com"

    def test_disallowed_command_returns_error(self) -> None:
        """A known router but disallowed path returns an error in command_room."""
        response, target = dispatch(
            "!mtik core-01 system/something/private",
            "!cmd:example.com",
            "@alice:example.com",
            _dispatch_cfg(),
        )
        assert "not in the allowed list" in response
        assert target == "!cmd:example.com"

    def test_successful_command_runs_and_responds(self) -> None:
        """A valid command calls ``execute_command`` and returns its result."""
        with patch("bot.bot.execute_command", return_value="version: 7.10") as run_mock:
            response, target = dispatch(
                "!mtik core-01 system/resource",
                "!cmd:example.com",
                "@alice:example.com",
                _dispatch_cfg(),
            )
        run_mock.assert_called_once_with(
            _dispatch_cfg().routers["core-01"],
            "system/resource",
        )
        assert "version: 7.10" in response
        assert "core-01" in response
        assert target == "!cmd:example.com"

    def test_router_error_is_caught_and_reported(self) -> None:
        """A ``RouterOSError`` from ``execute_command`` becomes a user error."""
        with patch("bot.bot.execute_command", side_effect=RouterOSError("nope")):
            response, target = dispatch(
                "!mtik core-01 system/resource",
                "!cmd:example.com",
                "@alice:example.com",
                _dispatch_cfg(),
            )
        assert "nope" in response
        assert "core-01" in response
        assert target == "!cmd:example.com"

    def test_command_extracts_first_token_as_api_path(self) -> None:
        """The first whitespace-separated token of ``command`` is the API path."""
        with patch("bot.bot.execute_command", return_value="ok") as run_mock:
            dispatch(
                "!mtik core-01 system/resource extra=stuff",
                "!cmd:example.com",
                "@alice:example.com",
                _dispatch_cfg(),
            )
        assert run_mock.call_args.args[1] == "system/resource extra=stuff"
