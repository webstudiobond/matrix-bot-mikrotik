#!/usr/bin/env python3
"""MikroTik Matrix Gateway Bot.

Listens for Matrix messages produced by matrix-cli (``--mode listen
--json``), parses commands of the form:

    !mtik <router_id> <command>

Transport selection is automatic based on ``port`` in config.yaml:

    port 443 / 80            → RouterOS REST API (HTTPS/HTTP)  — RouterOS 7.1+
    port 8729                → RouterOS API over TLS            — RouterOS 6.x+
    port 8728 (or any other) → RouterOS API plaintext           — RouterOS 3.x+

Security model
--------------
Layer 0 — Identity & room gate:
    * Messages from the bot's own Matrix account are silently dropped
      (prevents feedback loops).
    * Messages from rooms other than ``command_room`` are silently dropped.
    * Messages from users not in ``allowed_users`` trigger an alert to
      ``admin_room`` and receive no response.

Layer 1 — Input validation:
    * All input is matched against CMD_RE before any further processing.
    * router_id is restricted to [A-Za-z0-9_-] — no shell metacharacters.
    * command is restricted to printable ASCII — no control characters.

Layer 2 — Command whitelist:
    * API path must be listed in ``allowed_commands`` from config.yaml.
    * Unknown paths are rejected before any network I/O.

Layer 3 — Write gate:
    * Write operations (=key=value params) require ALLOW_WRITES=true env var.
    * Default is read-only regardless of command whitelist.

Layer 4 — Transport security:
    * REST: path validated against [A-Za-z0-9/_-], params sent as JSON body.
    * librouteros: params passed as structured kwargs — no shell involved.
    * Credentials are never logged.

Layer 5 — Process & filesystem:
    * Runs as UID 10001. Read-only rootfs. config.yaml mounted :ro.
    * subprocess calls use argument lists, never shell=True.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

if TYPE_CHECKING:
    from types import FrameType

import librouteros
import librouteros.exceptions
import requests
import urllib3
import yaml

from bot._matrix_cli import send_message, start_listener

CONFIG_PATH: Path = Path(os.getenv("ROUTERS_CONFIG", "/home/bot/config/config.yaml"))

CMD_RE: re.Pattern[str] = re.compile(
    r"^!mtik\s+(?P<router_id>[A-Za-z0-9_-]{1,64})\s+(?P<command>[\x20-\x7E]{1,512})$"
)

REST_PORTS: frozenset[int] = frozenset({80, 443})

ROUTEROS_API_TLS_PORT: int = 8729
ROUTEROS_REST_HTTPS_PORT: int = 443
HTTP_UNAUTHORIZED: int = 401
MAX_MESSAGE_LENGTH: int = 4000

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
log = logging.getLogger("matrix-mikrotik-bot")


RouterConfig = dict[str, object]
RoutersMap = dict[str, RouterConfig]


@dataclass(frozen=True)
class BotConfig:
    """Holds the full validated configuration loaded from config.yaml."""

    bot_user: str
    command_room: str
    admin_room: str
    allowed_users: list[str]
    allowed_commands: list[str]
    routers: RoutersMap


def validate_top_level(
    raw: dict[str, object],
) -> tuple[str, str, str, list[str], list[str]]:
    """Validate top-level ``bot_user``, ``command_room``, ``admin_room`` fields.

    Args:
        raw: Mapping of top-level config keys to their parsed values.

    Returns:
        Tuple of ``(bot_user, command_room, admin_room, allowed_users,
        allowed_commands)`` ready to be packed into ``BotConfig``.

    """
    for field in ("bot_user", "command_room", "admin_room"):
        if not raw.get(field):
            log.critical("Missing required config field: %r", field)
            sys.exit(1)

    allowed_users = cast("list[str]", raw.get("allowed_users", []))
    if not allowed_users:
        log.critical("allowed_users must contain at least one Matrix user ID")
        sys.exit(1)

    allowed_commands = cast("list[str]", raw.get("allowed_commands", []))
    if not allowed_commands:
        log.critical("allowed_commands must contain at least one path")
        sys.exit(1)

    for cmd in allowed_commands:
        if not re.fullmatch(r"[A-Za-z0-9/_-]+", cmd):
            log.critical(
                "Invalid allowed_commands entry %r: must match [A-Za-z0-9/_-]+",
                cmd,
            )
            sys.exit(1)

    return (
        str(raw["bot_user"]),
        str(raw["command_room"]),
        str(raw["admin_room"]),
        allowed_users,
        allowed_commands,
    )


def validate_routers(raw: dict[str, object], path: Path) -> RoutersMap:
    """Validate the ``routers`` section of the configuration.

    Args:
        raw: Top-level config mapping.
        path: Path to the YAML file, used for log messages.

    Returns:
        Mapping of ``router_id`` to its validated configuration dict.

    """
    routers = cast("dict[str, RouterConfig]", raw.get("routers", {}))
    if not routers:
        log.critical("No routers defined in %s", path)
        sys.exit(1)

    required_keys = {"host", "port", "username", "password"}
    for rid, cfg in routers.items():
        missing = required_keys - set(cfg.keys())
        if missing:
            log.critical("Router %r is missing required keys: %s", rid, missing)
            sys.exit(1)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid):
            log.critical("Invalid router_id %r: must match [A-Za-z0-9_-]{1,64}", rid)
            sys.exit(1)
    return routers


def load_config(path: Path) -> BotConfig:
    """Load and validate the full configuration from config.yaml.

    Validates all required fields, allowed lists, and router parameters.
    Exits the process (sys.exit) if any validation fails.
    """
    if not path.is_file():
        log.critical("Config file not found: %s", path)
        sys.exit(1)

    try:
        loaded: dict[str, object] | list[object] | str | int | float | bool | None
        loaded = cast(
            "dict[str, object] | list[object] | str | int | float | bool | None",
            yaml.safe_load(path.read_text(encoding="utf-8")),
        )
    except yaml.YAMLError as exc:
        log.critical("YAML parse error in %s: %s", path, exc)
        sys.exit(1)

    if not isinstance(loaded, dict):
        log.critical("Config file is empty or invalid: %s", path)
        sys.exit(1)

    raw: dict[str, object] = loaded

    bot_user, command_room, admin_room, allowed_users, allowed_commands = (
        validate_top_level(raw)
    )
    routers = validate_routers(raw, path)

    log.info("Loaded %d router(s): %s", len(routers), sorted(routers.keys()))
    log.info("Allowed users: %s", allowed_users)
    log.info("Allowed commands: %d paths", len(allowed_commands))

    return BotConfig(
        bot_user=bot_user,
        command_room=command_room,
        admin_room=admin_room,
        allowed_users=allowed_users,
        allowed_commands=allowed_commands,
        routers=routers,
    )


class RouterOSError(Exception):
    """Raised when any router transport returns an error or is unreachable."""


def rest_session(cfg: RouterConfig) -> requests.Session:
    """Build an authenticated requests.Session for the RouterOS REST API."""
    session = requests.Session()
    session.auth = (cast("str", cfg["username"]), cast("str", cfg["password"]))
    verify = cast("bool", cfg.get("tls_verify", True))
    session.verify = verify
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def parse_ros_kv(raw: str) -> dict[str, str]:
    """Parse RouterOS =key=value token pairs into a plain dict.

    Args:
        raw: String of the form ``=address=10.0.0.1/24 =interface=ether1``.

    Returns:
        Mapping of key → value extracted from ``=key=value`` tokens.

    """
    return dict(re.findall(r"=([^=\s]+)=([^\s]*)", raw))


def execute_rest(cfg: RouterConfig, raw_command: str) -> str:
    """Execute a command via the RouterOS REST API (RouterOS 7.1+).

    Args:
        cfg: Router configuration dict.
        raw_command: Validated command string from the Matrix message.

    Returns:
        JSON-formatted result, truncated to 4 000 chars.

    Raises:
        RouterOSError: On connection failure, auth error, or non-2xx response.

    """
    allow_writes: bool = os.getenv("ALLOW_WRITES", "false").lower() == "true"

    parts = raw_command.strip().split(None, 1)
    api_path = parts[0].strip("/")

    if not re.fullmatch(r"[A-Za-z0-9/_-]+", api_path):
        msg = f"Invalid API path: {api_path!r}"
        raise RouterOSError(msg)

    tls = cast("bool", cfg.get("tls_verify", True))
    scheme = "https" if (tls or cfg["port"] == ROUTEROS_REST_HTTPS_PORT) else "http"
    url = f"{scheme}://{cfg['host']}:{cfg['port']}/rest/{api_path}"

    session = rest_session(cfg)
    method = "GET"
    body: dict[str, str] | None = None

    if len(parts) > 1 and parts[1].strip().startswith("="):
        if not allow_writes:
            msg = "Write operations are disabled. Set ALLOW_WRITES=true to enable."
            raise RouterOSError(msg)
        method = "POST"
        body = parse_ros_kv(parts[1])

    try:
        resp = session.request(method, url, json=body, timeout=(5, 15))
    except requests.exceptions.ConnectionError as exc:
        msg = f"Cannot reach {cfg['host']}:{cfg['port']}: {exc}"
        raise RouterOSError(msg) from exc
    except requests.exceptions.Timeout:
        msg = f"Timeout connecting to {cfg['host']}:{cfg['port']}"
        raise RouterOSError(msg) from None

    if resp.status_code == HTTP_UNAUTHORIZED:
        msg = "Authentication failed — check credentials in config.yaml"
        raise RouterOSError(msg)
    if not resp.ok:
        msg = f"API error {resp.status_code}: {resp.text[:200]}"
        raise RouterOSError(msg)

    try:
        formatted = json.dumps(resp.json(), indent=2)
    except ValueError:
        formatted = resp.text

    if len(formatted) > MAX_MESSAGE_LENGTH:
        return formatted[: MAX_MESSAGE_LENGTH - 50] + "\n… (truncated)"
    return formatted


def routeros_api_connect(cfg: RouterConfig) -> librouteros.Api:
    """Open a librouteros connection to a router."""
    port = int(cast("int", cfg["port"]))
    use_tls: bool = port == ROUTEROS_API_TLS_PORT

    try:
        if use_tls:
            ctx = ssl.create_default_context()
            if not cfg.get("tls_verify", True):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn = librouteros.connect(
                str(cfg["host"]),
                username=str(cfg["username"]),
                password=str(cfg["password"]),
                port=port,
                ssl_wrapper=ctx.wrap_socket,
            )
        else:
            conn = librouteros.connect(
                str(cfg["host"]),
                username=str(cfg["username"]),
                password=str(cfg["password"]),
                port=port,
            )
    except librouteros.exceptions.TrapError as exc:
        msg = f"Authentication failed: {exc}"
        raise RouterOSError(msg) from exc
    except OSError as exc:
        msg = f"Cannot reach {cfg['host']}:{port}: {exc}"
        raise RouterOSError(msg) from exc

    return conn


def ros_path_to_api(api_path: str) -> tuple[str, ...]:
    """Convert a slash-separated path to a RouterOS API word tuple."""
    parts = [p for p in api_path.strip("/").split("/") if p]
    if not parts:
        msg = "Empty API path"
        raise RouterOSError(msg)
    return ("/" + parts[0], *parts[1:])


def execute_api(cfg: RouterConfig, raw_command: str) -> str:
    """Execute a command via the librouteros RouterOS API (RouterOS 3.x+)."""
    allow_writes: bool = os.getenv("ALLOW_WRITES", "false").lower() == "true"

    parts = raw_command.strip().split(None, 1)
    api_path = parts[0].strip("/")

    if not re.fullmatch(r"[A-Za-z0-9/_-]+", api_path):
        msg = f"Invalid API path: {api_path!r}"
        raise RouterOSError(msg)

    has_params = len(parts) > 1 and parts[1].strip().startswith("=")

    if has_params and not allow_writes:
        msg = "Write operations are disabled. Set ALLOW_WRITES=true to enable."
        raise RouterOSError(msg)

    conn = routeros_api_connect(cfg)

    try:
        path_words = ros_path_to_api(api_path)
        menu = conn.path(*path_words)

        if has_params:
            kwargs = parse_ros_kv(parts[1])
            result = list(menu.add(**kwargs))
        else:
            result = list(menu)

        formatted = json.dumps(result, indent=2, default=str)
    except librouteros.exceptions.TrapError as exc:
        msg = f"RouterOS API trap: {exc}"
        raise RouterOSError(msg) from exc
    finally:
        conn.close()

    if len(formatted) > MAX_MESSAGE_LENGTH:
        return formatted[: MAX_MESSAGE_LENGTH - 50] + "\n… (truncated)"
    return formatted


def execute_command(cfg: RouterConfig, raw_command: str) -> str:
    """Route a command to the correct transport based on port number."""
    if cfg["port"] in REST_PORTS:
        return execute_rest(cfg, raw_command)
    return execute_api(cfg, raw_command)


def parse_event(line: str) -> tuple[str | None, str | None, str | None]:
    """Extract (room_id, sender, body) from a matrix-cli JSON line.

    Returns (None, None, None) for non-text-message events or parse errors.
    """
    try:
        parsed: dict[str, object] | list[object] | str | int | float | bool | None
        parsed = cast(
            "dict[str, object] | list[object] | str | int | float | bool | None",
            json.loads(line),
        )
    except json.JSONDecodeError, TypeError:
        return None, None, None

    if not isinstance(parsed, dict):
        return None, None, None

    obj: dict[str, object] = parsed

    if obj.get("status") == "listening":
        return None, None, None

    if obj.get("type") != "m.room.message":
        return None, None, None
    content = cast("dict[str, object]", obj.get("content", {}))
    if content.get("msgtype") != "m.text":
        return None, None, None

    raw_room: object = obj.get("room_id")
    raw_sender: object = obj.get("sender")
    room_id: str | None = str(raw_room) if raw_room is not None else None
    sender: str | None = str(raw_sender) if raw_sender is not None else None
    body: str | None = str(content.get("body") or "").strip()
    return room_id, sender, body


def build_help(cfg: BotConfig) -> str:
    """Build the help message dynamically from allowed_commands."""
    routers_list = ", ".join(f"`{r}`" for r in sorted(cfg.routers.keys()))
    commands_list = "\n".join(f"  {c}" for c in sorted(cfg.allowed_commands))
    allow_writes = os.getenv("ALLOW_WRITES", "false").lower() == "true"
    writes_note = (
        "✅ enabled" if allow_writes else "❌ disabled (set ALLOW_WRITES=true)"
    )

    return (
        "**MikroTik Matrix Bot**\n\n"
        "**Usage:** `!mtik <router_id> <path> [=key=value ...]`\n\n"
        f"**Routers:** {routers_list}\n\n"
        f"**Write operations:** {writes_note}\n\n"
        f"**Allowed commands:**\n```\n{commands_list}\n```\n\n"
        "**Examples:**\n"
        "  `!mtik core-01 system/resource`\n"
        "  `!mtik branch-02 ip/address`\n"
        "  `!mtik core-01 ip/dhcp-server/lease`"
    )


def dispatch(
    body: str, room_id: str | None, sender: str | None, cfg: BotConfig
) -> tuple[str, str | None]:
    """Parse a Matrix message and return (response, target_room).

    Layer 0 — identity, room, and user gates are applied here.
    Returns ("", None) to remain silent.

    Args:
        body: Raw message text.
        room_id: Matrix room ID the message came from.
        sender: Matrix user ID of the message author.
        cfg: Full bot configuration.

    Returns:
        Tuple of ``(response_text, target_room_id)``. Empty string means
        no response should be sent.

    """
    response: str = ""
    target_room: str | None = None

    if sender == cfg.bot_user or room_id != cfg.command_room:
        return response, target_room

    if sender not in cfg.allowed_users:
        log.warning("Unauthorized access attempt from %s in %s", sender, room_id)
        alert = (
            f"⚠️ **SECURITY WARNING**\n\n"
            f"Unauthorized user: `{sender}`\n"
            f"Room: `{room_id}`\n"
            f"Attempted command: `{body[:200]}`"
        )
        return alert, cfg.admin_room

    if body.strip().lower() in ("help", "start", "!mtik help", "!mtik start"):
        return build_help(cfg), cfg.command_room

    m = CMD_RE.match(body)
    if not m:
        return response, target_room

    router_id = m.group("router_id")
    command = m.group("command")

    if router_id not in cfg.routers:
        known = ", ".join(f"`{r}`" for r in sorted(cfg.routers.keys()))
        response = f"❌ Unknown router `{router_id}`. Known IDs: {known}"
        target_room = cfg.command_room
    else:
        api_path = command.strip().split(None, 1)[0].strip("/")
        if api_path not in cfg.allowed_commands:
            allowed = "\n".join(f"  {c}" for c in sorted(cfg.allowed_commands))
            response = (
                f"❌ Command `{api_path}` is not in the allowed list.\n"
                f"Allowed commands:\n```\n{allowed}\n```"
            )
            target_room = cfg.command_room
        else:
            log.info(
                "Dispatching: sender=%s router=%s command=%r",
                sender,
                router_id,
                command,
            )
            try:
                result = execute_command(cfg.routers[router_id], command)
            except RouterOSError as exc:
                log.warning("RouterOSError router=%s: %s", router_id, exc)
                response = f"❌ Router `{router_id}`: {exc}"
                target_room = cfg.command_room
            else:
                response = f"✅ `{router_id}` → `{command}`\n```\n{result}\n```"
                target_room = cfg.command_room

    return response, target_room


def listen_loop(cfg: BotConfig) -> NoReturn:
    """Spawn matrix-cli in listen mode and dispatch events forever."""
    backoff = 2

    while True:
        log.info("Starting matrix-cli listener (back-off=%ds)", backoff)
        listener = start_listener()
        backoff = 2

        for raw_line in listener.lines:
            line: str = raw_line
            stripped = line.strip()
            if not stripped:
                continue
            room_id, sender, body = parse_event(stripped)
            if body is None:
                continue
            log.debug("Event sender=%s room=%s body=%r", sender, room_id, body)
            response, target_room = dispatch(body, room_id, sender, cfg)
            if response:
                send_message(response, room=target_room)

        _ = listener.wait()
        log.warning(
            "matrix-cli exited code=%d — restarting in %ds",
            listener.returncode,
            backoff,
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def handle_sigterm(_signum: int, _frame: FrameType | None) -> NoReturn:
    """Translate ``SIGTERM`` into a clean ``sys.exit(0)`` for graceful shutdown."""
    log.info("SIGTERM received — shutting down")
    sys.exit(0)


def main() -> None:
    """Bot entry point."""
    _ = signal.signal(signal.SIGTERM, handle_sigterm)
    cfg = load_config(CONFIG_PATH)
    listen_loop(cfg)


if __name__ == "__main__":
    main()
